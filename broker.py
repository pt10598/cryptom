"""Read-only IG and OANDA quote adapters."""
import asyncio
import json
import math
import os
import re
import time
from datetime import UTC, datetime, timedelta

import httpx

SYMBOLS = ("EUR/USD", "USD/JPY", "GBP/USD", "AUD/USD", "EUR/GBP", "EUR/JPY",
           "GBP/JPY", "XAU/USD", "XAG/USD", "WTI/USD", "BRENT/USD")


class BrokerQuotes:
    def __init__(self):
        self.quotes = {"IG": {}, "OANDA": {}}
        self.status = {name: "未設定憑證" for name in self.quotes}
        self.password = os.getenv("MARKETS_PASSWORD", "")
        self.modes = {"IG": "demo" if os.getenv("IG_MODE", "demo") == "demo" else "live",
                      "OANDA": os.getenv("OANDA_MODE", "practice")}

    def update(self, venue, quotes):
        self.quotes[venue] = quotes
        self.status[venue] = "接收行情" if quotes else "目前沒有可用報價"

    def put(self, output, symbol, bid, ask, stamp=None):
        try:
            bid, ask = float(bid), float(ask)
        except (ValueError, TypeError):
            return
        if math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask:
            received = time.time()
            output[symbol] = {"bid": bid, "ask": ask, "at": received,
                              "source_time": stamp, "source_epoch": utc_timestamp(stamp, received)}

    def snapshot(self):
        now = time.time()
        available = {venue: {symbol: quote for symbol, quote in quotes.items()
                             if now - quote["at"] <= 15}
                     for venue, quotes in self.quotes.items()}
        rows = []
        same_mode = (self.modes["IG"] == "live") == (self.modes["OANDA"] == "live")
        for symbol in SYMBOLS:
            ig, oa = (available[venue].get(symbol) for venue in ("IG", "OANDA"))
            # Commodity benchmarks may have different pricing and roll conventions.
            comparable = (same_mode and ig and oa and symbol not in ("WTI/USD", "BRENT/USD")
                          and all(quote["source_epoch"] is not None and -2 <= now - quote["source_epoch"] <= 8
                                  for quote in (ig, oa))
                          and abs(ig["source_epoch"] - oa["source_epoch"]) <= 5)
            rows.append({"symbol": symbol, "ig": ig, "oanda": oa,
                         "ig_to_oanda": oa["bid"] - ig["ask"] if comparable else None,
                         "oanda_to_ig": ig["bid"] - oa["ask"] if comparable else None})
        return {"at": now, "status": self.status, "modes": self.modes, "rows": rows}

    async def run_oanda(self):
        token, account = os.getenv("OANDA_TOKEN"), os.getenv("OANDA_ACCOUNT_ID")
        if not token or not account:
            return
        mode = self.modes["OANDA"]
        if mode not in ("live", "practice") or not re.fullmatch(r"[0-9-]+", account):
            self.status["OANDA"] = "檢查環境或帳號設定"
            return
        domain = "fxtrade" if mode == "live" else "fxpractice"
        instruments = {name.replace("/", "_"): name for name in SYMBOLS[:-2]}
        instruments.update(WTICO_USD="WTI/USD", BCO_USD="BRENT/USD")
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=20) as client:
            while True:
                try:
                    self.status["OANDA"] = "連線中"
                    suffix = f"/v3/accounts/{account}"
                    response = await client.get(f"https://api-{domain}.oanda.com{suffix}/instruments")
                    response.raise_for_status()
                    selected = [item["name"] for item in response.json()["instruments"]
                                if item["name"] in instruments]
                    if not selected:
                        self.status["OANDA"] = "帳戶無支援商品"
                        return
                    async with client.stream("GET", f"https://stream-{domain}.oanda.com{suffix}/pricing/stream",
                                             params={"instruments": ",".join(selected)}) as stream:
                        stream.raise_for_status()
                        async for line in stream.aiter_lines():
                            if not line:
                                continue
                            item = json.loads(line)
                            symbol = instruments.get(item.get("instrument"))
                            if item.get("type") != "PRICE" or not symbol:
                                continue
                            quotes = self.quotes["OANDA"]
                            if not item.get("tradeable", item.get("status") == "tradeable") or not item.get("bids") or not item.get("asks"):
                                quotes.pop(symbol, None)
                                continue
                            self.put(quotes, symbol, item["bids"][0]["price"], item["asks"][0]["price"], item.get("time"))
                            self.update("OANDA", quotes)
                    self.status["OANDA"] = "串流中斷，等待重連"
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    self.status["OANDA"] = connection_error(exc)
                self.quotes["OANDA"].clear()
                await asyncio.sleep(15)

    async def run_ig(self):
        key, user, password = (os.getenv(name) for name in ("IG_API_KEY", "IG_IDENTIFIER", "IG_PASSWORD"))
        mapping = os.getenv("IG_MARKETS_JSON")
        if not all((key, user, password, mapping)):
            self.status["IG"] = "待設定憑證與商品 EPIC"
            return
        mode = os.getenv("IG_MODE", "demo")
        try:
            raw = json.loads(mapping)
            markets = {entry["epic"]: (symbol, float(entry["divisor"])) for symbol, entry in raw.items()
                       if symbol in SYMBOLS}
            if (mode not in ("demo", "live") or not markets or
                any(not re.fullmatch(r"[A-Za-z0-9._]{6,30}", epic) or not math.isfinite(divisor)
                    or divisor <= 0 for epic, (_, divisor) in markets.items())):
                raise ValueError
        except (AttributeError, ValueError, KeyError, TypeError):
            self.status["IG"] = "檢查 IG_MARKETS_JSON 與 IG_MODE"
            return
        origin = "https://demo-api.ig.com" if mode == "demo" else "https://api.ig.com"
        async with httpx.AsyncClient(base_url=origin + "/gateway/deal/",
                                     headers={"X-IG-API-KEY": key, "Accept": "application/json"}, timeout=10) as client:
            tokens = None
            while True:
                try:
                    if tokens is None:
                        self.status["IG"] = "連線中"
                        response = await client.post("session", headers={"Version": "2"},
                                                     json={"identifier": user, "password": password})
                        response.raise_for_status()
                        tokens = {name: response.headers[name] for name in ("CST", "X-SECURITY-TOKEN")}
                    response = await client.get("markets", headers={**tokens, "Version": "2"},
                                                params={"epics": ",".join(markets), "filter": "SNAPSHOT_ONLY"})
                    response.raise_for_status()
                    quotes = {}
                    for market in response.json()["marketDetails"]:
                        config = markets.get(market.get("instrument", {}).get("epic"))
                        snap = market.get("snapshot") or {}
                        if config and snap.get("marketStatus") == "TRADEABLE":
                            symbol, divisor = config
                            if snap.get("bid") is not None and snap.get("offer") is not None:
                                self.put(quotes, symbol, float(snap["bid"]) / divisor,
                                         float(snap["offer"]) / divisor,
                                         snap.get("updateTimeUTC"))
                    self.update("IG", quotes)
                    await asyncio.sleep(4)
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    self.status["IG"] = connection_error(exc)
                    self.quotes["IG"].clear()
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
                        tokens = None
                    await asyncio.sleep(60)


def connection_error(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        return f"行情暫停（HTTP {exc.response.status_code}）"
    return "行情暫停，等待重連"


def utc_timestamp(value, received):
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?", value):
            current = datetime.fromtimestamp(received, UTC)
            parsed = datetime.fromisoformat(f"{current.date()}T{value}+00:00")
            if parsed - current > timedelta(hours=12):
                parsed -= timedelta(days=1)
            if current - parsed > timedelta(hours=12):
                parsed += timedelta(days=1)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
        return parsed.timestamp()
    except ValueError:
        return None
