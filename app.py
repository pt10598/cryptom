"""Browser dashboard for the crypto spot monitor."""
import asyncio
import itertools
import json
import math
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from crypto_monitor import listen, opportunity
from history import History

VENUES = ("Bybit", "OKX", "Bitget", "Kraken")
DEFAULT_PAIRS = ",".join(f"{coin}/USDT" for coin in
                         ("BTC ETH SOL XRP DOGE LINK AVAX ADA SUI APT NEAR DOT LTC "
                          "BCH UNI AAVE ATOM ARB OP INJ FIL ICP TRX XLM ETC HBAR ALGO "
                          "SEI TIA WLD PEPE BONK SHIB TON JUP").split())
DEFAULT_PAIRS += ",ETH/BTC,SOL/BTC,BTC/USDC,ETH/USDC"
QUOTE_DEFAULTS = {"USDT": 100, "USDC": 100, "BTC": 0.001, "ETH": 0.05}
PAGE = Path(__file__).with_name("index.html")


def number(name, default, minimum, maximum):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


class Market:
    def __init__(self):
        pairs = os.getenv("PAIRS", DEFAULT_PAIRS).upper().split(",")
        normalized = []
        for pair in pairs:
            pair = pair.strip()
            if "/" not in pair and pair.endswith("USDT"):
                pair = f"{pair[:-4]}/USDT"
            if not re.fullmatch(r"[A-Z0-9]{1,20}/[A-Z0-9]{1,20}", pair):
                raise ValueError("PAIRS must use BASE/QUOTE (or legacy BASEUSDT)")
            base, quote = pair.split("/")
            if base == quote or quote not in QUOTE_DEFAULTS:
                raise ValueError("Use distinct currencies and a USDT, USDC, BTC or ETH quote")
            normalized.append(pair)
        self.pairs = list(dict.fromkeys(normalized))
        if len(self.pairs) > 50:
            raise ValueError("PAIRS accepts up to 50 symbols")
        self.fees = {venue: number(f"{venue.upper()}_FEE_PCT", 0.1, 0, 10) / 100 for venue in VENUES}
        self.buffer = number("BUFFER_PCT", 0.1, 0, 10) / 100
        self.amounts = {quote: number(f"NOTIONAL_{quote}", default, 1e-8, 1_000_000)
                        for quote, default in QUOTE_DEFAULTS.items()}
        self.max_age = number("MAX_AGE_MS", 1000, 50, 60000) / 1000
        self.max_skew = number("MAX_SKEW_MS", 500, 50, 60000) / 1000
        self.quotes = {}
        self.status = {}
        self.active = {}
        self.pending = {}
        self.history = History()
        self.history_error = None
        self.frame = "{}"

    def snapshot(self, now):
        prices = []
        rows = []
        active = {}
        at = time.time()
        for pair in self.pairs:
            currency = pair.split("/")[1]
            amount = self.amounts[currency]
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
                estimate = opportunity(ask, bid, amount, self.fees[buy], self.fees[sell], self.buffer)
                if estimate is None:
                    continue
                gross, net, rate = estimate
                duration = 0
                if net > 0:
                    key = (pair, buy, sell)
                    event = self.active.get(key)
                    if event is None:
                        event = {"id": uuid.uuid4().hex, "pair": pair, "buy": buy, "sell": sell,
                                 "currency": currency, "amount": amount, "started_at": at,
                                 "started_mono": now, "peak_net": net, "peak_rate": rate,
                                 "peak_capacity": 0}
                    duration = round((now - event["started_mono"]) * 1000)
                    event.update(last_at=at, duration_ms=duration,
                                 peak_net=max(event["peak_net"], net),
                                 peak_rate=max(event["peak_rate"], rate),
                                 peak_capacity=max(event["peak_capacity"], min(ask.ask_qty, bid.bid_qty) * ask.ask))
                    active[key] = event
                    self.pending[event["id"]] = event.copy()
                capacity = min(ask.ask_qty, bid.bid_qty) * ask.ask
                rows.append([pair, buy, sell, gross, rate, net, capacity, duration, currency, amount])
        self.active = active
        rows.sort(key=lambda row: row[4], reverse=True)
        return {"at": at, "status": self.status, "amounts": self.amounts,
                "fees": self.fees, "buffer": self.buffer, "prices": prices, "rows": rows[:30]}

    async def publish(self):
        while True:
            self.frame = json.dumps(self.snapshot(time.monotonic()), ensure_ascii=False, allow_nan=False)
            await asyncio.sleep(0.2)


    async def write_history(self):
        ready = False
        while True:
            batch = list(self.pending.values())
            try:
                if not ready:
                    await asyncio.to_thread(self.history.initialize)
                    ready = True
                if batch:
                    await asyncio.to_thread(self.history.save, batch)
                    for event in batch:
                        if self.pending.get(event["id"]) is event:
                            self.pending.pop(event["id"])
                self.history_error = None
            except Exception as exc:
                # Bound in-memory backlog if the database remains unavailable.
                while len(self.pending) > 5000:
                    self.pending.pop(next(iter(self.pending)))
                self.history_error = type(exc).__name__
                ready = False
            await asyncio.sleep(5 if self.history_error else 1)


@asynccontextmanager
async def lifespan(app):
    market = Market()
    app.state.market = market
    tasks = [asyncio.create_task(listen(venue, market.pairs, market.quotes, market.status))
             for venue in VENUES]
    tasks.append(asyncio.create_task(market.publish()))
    tasks.append(asyncio.create_task(market.write_history()))
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


@app.get("/api/history")
async def history(request: Request, limit: int = Query(default=100, ge=1, le=500)):
    market = request.app.state.market
    try:
        rows = await asyncio.to_thread(market.history.recent, limit)
    except Exception as exc:
        return JSONResponse({"error": type(exc).__name__, "storage": market.history.label, "rows": []},
                            status_code=503, headers={"Cache-Control": "no-store"})
    return JSONResponse({"error": market.history_error, "storage": market.history.label, "rows": rows},
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
