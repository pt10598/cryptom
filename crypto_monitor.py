"""Local, read-only spot arbitrage monitor."""
import asyncio
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
    symbols = [pair.replace("/", "") for pair in pairs]
    if venue == "Bitget":
        return "wss://ws.bitget.com/v3/ws/public", {
            "op": "subscribe",
            "args": [{"instType": "spot", "topic": "books1", "symbol": symbol}
                     for symbol in symbols],
        }
    if venue == "Kraken":
        return "wss://ws.kraken.com/v2", {
            "method": "subscribe",
            "params": {"channel": "ticker", "event_trigger": "bbo", "symbol": pairs},
        }
    if venue == "Bybit":
        return "wss://stream.bybit.com/v5/public/spot", {
            "op": "subscribe", "args": [f"orderbook.1.{symbol}" for symbol in symbols]
        }
    return "wss://ws.okx.com:8443/ws/v5/public", {
        "op": "subscribe",
        "args": [{"channel": "bbo-tbt", "instId": pair.replace("/", "-")} for pair in pairs],
    }


def decode(venue, msg):
    if venue == "Kraken":
        if msg.get("channel") == "ticker":
            for entry in msg.get("data", []):
                yield entry["symbol"], (entry["bid"], entry["bid_qty"], entry["ask"], entry["ask_qty"])
        return
    if venue == "Bitget":
        arg = msg.get("arg", {})
        if arg.get("topic") != "books1":
            return
        pair = arg["symbol"]
        entries = msg.get("data", [])
    elif venue == "Bybit":
        if not msg.get("topic", "").startswith("orderbook.1."):
            return
        data = msg["data"]
        pair = data["s"]
        entries = [data]
    else:
        if msg.get("arg", {}).get("channel") != "bbo-tbt":
            return
        pair = msg["arg"]["instId"].replace("-", "/")
        entries = msg.get("data", [])
    for entry in entries:
        bids = entry.get("b", entry.get("bids", []))
        asks = entry.get("a", entry.get("asks", []))
        yield pair, (*bids[0][:2], *asks[0][:2]) if bids and asks else None


async def listen(venue, pairs, quotes, status):
    url, subscription = feed_config(venue, pairs)
    aliases = {pair.replace("/", ""): pair for pair in pairs}
    delay = 1
    while True:
        try:
            status[venue] = "連線中"
            async with connect(url, open_timeout=10, ping_interval=15) as ws:
                if venue == "Kraken":
                    await ws.send(json.dumps(subscription))
                else:
                    for item in subscription["args"]:
                        await ws.send(json.dumps({"op": "subscribe", "args": [item]}))
                        await asyncio.sleep(0.15)
                rejected = 0
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
                                error = msg.get("error") or msg.get("msg") or msg.get("ret_msg") or "請檢查幣對"
                                rejected += 1
                                status[venue] = f"訂閱遭拒 ({str(error)[:80]})"
                            for pair, values in decode(venue, msg):
                                pair = aliases.get(pair.replace("/", ""))
                                if pair is None:
                                    continue
                                key = (venue, pair)
                                numbers = tuple(map(float, values)) if values else ()
                                if numbers and all(math.isfinite(n) and n > 0 for n in numbers):
                                    quotes[key] = Quote(*numbers, received)
                                    status[venue] = "接收盤口中" + (f" · {rejected} 筆訂閱遭拒" if rejected else "")
                                    delay = 1
                                else:
                                    quotes.pop(key, None)
                    if received >= next_ping:
                        if venue == "Bybit":
                            await ws.send('{"op":"ping"}')
                        elif venue in ("OKX", "Bitget"):
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


