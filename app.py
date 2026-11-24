"""Browser dashboard for the crypto spot monitor."""
import asyncio
import itertools
import json
import math
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from crypto_monitor import listen, opportunity

VENUES = ("Binance", "Bybit", "OKX")
DEFAULT_PAIRS = "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT"
PAGE = Path(__file__).with_name("index.html")


def number(name, default, minimum, maximum):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


class Market:
    def __init__(self):
        pairs = os.getenv("PAIRS", DEFAULT_PAIRS).upper().split(",")
        self.pairs = list(dict.fromkeys(pair.strip() for pair in pairs))
        if len(self.pairs) > 50 or any(not re.fullmatch(r"[A-Z0-9]{1,20}USDT", pair) for pair in self.pairs):
            raise ValueError("PAIRS must contain 1–50 comma-separated USDT spot symbols")
        self.fees = {venue: number(f"{venue.upper()}_FEE_PCT", 0.1, 0, 10) / 100 for venue in VENUES}
        self.buffer = number("BUFFER_PCT", 0.1, 0, 10) / 100
        self.amount = number("NOTIONAL_USDT", 100, 0.01, 1_000_000)
        self.max_age = number("MAX_AGE_MS", 1000, 50, 60000) / 1000
        self.max_skew = number("MAX_SKEW_MS", 500, 50, 60000) / 1000
        self.quotes = {}
        self.status = {}
        self.started = {}
        self.frame = "{}"

    def snapshot(self, now):
        prices = []
        rows = []
        active = {}
        for pair in self.pairs:
            fresh = {}
            for venue in VENUES:
                quote = self.quotes.get((venue, pair))
                if quote is None or now - quote.received > self.max_age:
                    continue
                fresh[venue] = quote
                prices.append([pair, venue, quote.bid, quote.ask, round((now - quote.received) * 1000)])
            for buy, sell in itertools.permutations(fresh, 2):
                bid = fresh[sell]
                ask = fresh[buy]
                skew = abs(ask.received - bid.received)
                if skew > self.max_skew:
                    continue
                estimate = opportunity(ask, bid, self.amount, self.fees[buy], self.fees[sell], self.buffer)
                if estimate is None:
                    continue
                gross, net, rate = estimate
                duration = 0
                if net > 0:
                    key = (pair, buy, sell)
                    active[key] = self.started.get(key, now)
                    duration = round((now - active[key]) * 1000)
                capacity = min(ask.ask_qty, bid.bid_qty) * ask.ask
                rows.append([pair, buy, sell, gross, rate, net, capacity, duration])
        self.started = active
        rows.sort(key=lambda row: row[4], reverse=True)
        return {"at": time.time(), "status": self.status, "amount": self.amount,
                "fees": self.fees, "buffer": self.buffer, "prices": prices, "rows": rows[:30]}

    async def publish(self):
        while True:
            self.frame = json.dumps(self.snapshot(time.monotonic()), ensure_ascii=False, allow_nan=False)
            await asyncio.sleep(0.2)


@asynccontextmanager
async def lifespan(app):
    market = Market()
    app.state.market = market
    tasks = [asyncio.create_task(listen(venue, market.pairs, market.quotes, market.status))
             for venue in VENUES]
    tasks.append(asyncio.create_task(market.publish()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
async def index():
    return FileResponse(PAGE)


@app.get("/api/snapshot")
async def snapshot(request: Request):
    return Response(request.app.state.market.frame, media_type="application/json",
                    headers={"Cache-Control": "no-store"})


@app.get("/events")
async def events(request: Request):
    async def stream():
        previous = None
        while True:
            frame = request.app.state.market.frame
            if frame != previous:
                yield f"data: {frame}\n\n"
                previous = frame
            await asyncio.sleep(0.1)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})
