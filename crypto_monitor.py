"""Local, read-only spot arbitrage monitor."""
import argparse
import asyncio
import itertools
import json
import math
import time
from dataclasses import dataclass

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException


@dataclass(frozen=True)
class Quote:
    bid: float
    bid_qty: float
    ask: float
    ask_qty: float
    received: float


def feed_config(venue, pairs):
    if venue == "Binance":
        streams = "/".join(f"{pair.lower()}@bookTicker" for pair in pairs)
        return f"wss://stream.binance.com:443/stream?streams={streams}", None
    if venue == "Bybit":
        return "wss://stream.bybit.com/v5/public/spot", {
            "op": "subscribe", "args": [f"orderbook.1.{pair}" for pair in pairs]
        }
    return "wss://ws.okx.com:8443/ws/v5/public", {
        "op": "subscribe",
        "args": [{"channel": "bbo-tbt", "instId": f"{pair[:-4]}-USDT"} for pair in pairs],
    }


def decode(venue, msg):
    if venue == "Binance":
        data = msg.get("data", {})
        if "s" in data:
            yield data["s"], (data["b"], data["B"], data["a"], data["A"])
        return
    if venue == "Bybit":
        if not msg.get("topic", "").startswith("orderbook.1."):
            return
        data = msg["data"]
        pair = data["s"]
        entries = [data]
    else:
        if msg.get("arg", {}).get("channel") != "bbo-tbt":
            return
        pair = msg["arg"]["instId"].replace("-", "")
        entries = msg.get("data", [])
    for entry in entries:
        bids = entry.get("b", entry.get("bids", []))
        asks = entry.get("a", entry.get("asks", []))
        yield pair, (*bids[0][:2], *asks[0][:2]) if bids and asks else None


async def listen(venue, pairs, quotes, status):
    url, subscription = feed_config(venue, pairs)
    delay = 1
    while True:
        try:
            status[venue] = "連線中"
            async with connect(url, open_timeout=10, ping_interval=15) as ws:
                if subscription:
                    await ws.send(json.dumps(subscription))
                last_frame = time.monotonic()
                next_ping = last_frame + 15
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1)
                    except TimeoutError:
                        raw = None
                    received = time.monotonic()
                    if raw is not None:
                        last_frame = received
                        if raw != "pong":
                            msg = json.loads(raw)
                            if msg.get("event") == "error" or msg.get("success") is False:
                                raise ValueError(f"訂閱失敗 {msg}")
                            for pair, values in decode(venue, msg):
                                key = (venue, pair)
                                numbers = tuple(map(float, values)) if values else ()
                                if numbers and all(math.isfinite(n) and n > 0 for n in numbers):
                                    quotes[key] = Quote(*numbers, received)
                                    status[venue] = "接收盤口中"
                                    delay = 1
                                else:
                                    quotes.pop(key, None)
                    if received >= next_ping:
                        if venue == "Bybit":
                            await ws.send('{"op":"ping"}')
                        elif venue == "OKX":
                            await ws.send("ping")
                        next_ping = received + 15
                    if received - last_frame > 35:
                        raise TimeoutError("35 秒未收到資料")
        except (OSError, TimeoutError, WebSocketException, ValueError, KeyError, TypeError) as exc:
            status[venue] = f"重連中 ({str(exc)[:100]})"
        finally:
            for pair in pairs:
                quotes.pop((venue, pair), None)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


def opportunity(buy, sell, notional, buy_fee, sell_fee, buffer):
    quantity = notional / buy.ask
    if quantity > min(buy.ask_qty, sell.bid_qty):
        return None
    gross = sell.bid / buy.ask - 1
    cost = notional * (1 + buy_fee)
    net = quantity * sell.bid * (1 - sell_fee) - cost - notional * buffer
    return gross, net, net / cost


async def monitor(args, pairs):
    fees = {"Binance": args.binance_fee / 100, "Bybit": args.bybit_fee / 100,
            "OKX": args.okx_fee / 100}
    quotes, status = {}, {}
    async with asyncio.TaskGroup() as group:
        for venue in fees:
            group.create_task(listen(venue, pairs, quotes, status))
        while True:
            now = time.monotonic()
            lines = [time.strftime("%H:%M:%S") + "  " +
                     " | ".join(f"{name} {status.get(name, '啟動中')}" for name in fees),
                     f"試算買單 {args.usdt:g} USDT | 盤口買一 / 賣一［接收距今 ms］"]
            rows = []
            for pair in pairs:
                fresh = {}
                columns = []
                for name in fees:
                    quote = quotes.get((name, pair))
                    age = (now - quote.received) * 1000 if quote else math.inf
                    if age <= args.max_age_ms:
                        fresh[name] = quote
                        columns.append(f"{name} {quote.bid:g}/{quote.ask:g} [{age:.0f}]")
                    else:
                        columns.append(f"{name} 等待新盤口")
                lines.append(f"{pair:10} " + " | ".join(columns))
                for buy, sell in itertools.permutations(fresh, 2):
                    if abs(fresh[buy].received - fresh[sell].received) * 1000 > args.max_skew_ms:
                        continue
                    result = opportunity(fresh[buy], fresh[sell], args.usdt, fees[buy],
                                         fees[sell], args.buffer / 100)
                    if result is not None:
                        gross, net, rate = result
                        rows.append((rate, f"{pair:10} {buy} 買 → {sell} 賣  "
                                           f"毛 {gross:+.3%}  淨 {rate:+.3%}  {net:+.4f} USDT"))
            lines.append("\n估算淨價差排行（含兩邊費率及預留成本）")
            lines.extend(row for _, row in sorted(rows, reverse=True)[:10])
            if not rows:
                lines.append("等待兩邊有足夠數量且接收時間相近的盤口")
            print("\x1b[2J\x1b[H" + "\n".join(lines), flush=True)
            await asyncio.sleep(args.refresh_ms / 1000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", default="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT")
    parser.add_argument("--usdt", type=float, default=100)
    parser.add_argument("--max-age-ms", type=float, default=1000)
    parser.add_argument("--max-skew-ms", type=float, default=500)
    parser.add_argument("--refresh-ms", type=float, default=200)
    for flag in ("binance-fee", "bybit-fee", "okx-fee", "buffer"):
        parser.add_argument("--" + flag, type=float, default=0.10,
                            help="percentage points (0.10 means 0.10%%)")
    args = parser.parse_args()
    pairs = list(dict.fromkeys(p.strip().upper() for p in args.pairs.split(",")))
    if not all(p.isascii() and p.isalnum() and p.endswith("USDT") and len(p) > 4 for p in pairs):
        parser.error("--pairs 請輸入逗號分隔的 USDT 現貨交易對")
    for name in ("usdt", "max_age_ms", "max_skew_ms", "refresh_ms"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"{name} 必須大於零")
    for name in ("binance_fee", "bybit_fee", "okx_fee", "buffer"):
        if not 0 <= getattr(args, name) < 100:
            parser.error(f"{name} 必須介於 0 和 100 之間，且小於 100")
    try:
        asyncio.run(monitor(args, pairs))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
