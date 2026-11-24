"""Browser dashboard for the crypto spot monitor."""
import asyncio
import itertools
import json
import math
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from broker import BrokerQuotes
from crypto_monitor import listen, opportunity
from history import History
from transfer import OrderBookSamples, TransferError, estimate

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
        self.samples = OrderBookSamples()
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
            now = time.monotonic()
            self.samples.capture(now, self.quotes, self.max_age)
            self.frame = json.dumps(self.snapshot(now), ensure_ascii=False, allow_nan=False)
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
    brokers = app.state.brokers = BrokerQuotes()
    tasks = [asyncio.create_task(listen(venue, market.pairs, market.quotes, market.status))
             for venue in VENUES]
    tasks.append(asyncio.create_task(market.publish()))
    tasks.append(asyncio.create_task(market.write_history()))
    if brokers.password:
        tasks.extend([asyncio.create_task(brokers.run_oanda()), asyncio.create_task(brokers.run_ig())])
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


basic = HTTPBasic(auto_error=False)


def check_markets(request: Request, credentials: HTTPBasicCredentials | None = Depends(basic)):
    password = request.app.state.brokers.password
    if password and (credentials is None or credentials.username != "viewer" or
                     not secrets.compare_digest(credentials.password.encode(), password.encode())):
        raise HTTPException(401, "請登入行情頁", headers={"WWW-Authenticate": "Basic"})


@app.get("/markets", dependencies=[Depends(check_markets)])
async def markets_page():
    return FileResponse(PAGE.with_name("markets.html"), headers={"Cache-Control": "no-store"})


@app.get("/api/markets", dependencies=[Depends(check_markets)])
async def broker_snapshot(request: Request):
    return JSONResponse(request.app.state.brokers.snapshot(), headers={"Cache-Control": "no-store"})


@app.get("/transfer")
async def transfer_page():
    return FileResponse(PAGE.with_name("transfer.html"))


@app.get("/api/transfer/options")
async def transfer_options(request: Request):
    market = request.app.state.market
    return {"pairs": [pair for pair in market.pairs if pair.endswith("/USDT")], "venues": VENUES}


@app.get("/api/transfer")
async def transfer_estimate(request: Request, pair: str, buy: str, sell: str,
                            amount: float = Query(100, gt=0, le=1e8),
                            funding_fee: float = Query(ge=0, le=1e8),
                            coin_fee: float = Query(ge=0, le=1e10),
                            return_fee: float = Query(ge=0, le=1e8),
                            funding_seconds: int = Query(0, ge=0, le=1800),
                            transfer_seconds: int = Query(300, ge=1, le=1800),
                            replay: bool = False, start_at: float | None = None):
    market = request.app.state.market
    if pair not in market.pairs or not pair.endswith("/USDT") or buy == sell or buy not in VENUES or sell not in VENUES:
        raise HTTPException(400, "請選擇受監控的 USDT 幣對及兩個不同平台")
    now, wall = time.monotonic(), time.time()
    buy_key, sell_key = (buy, pair), (sell, pair)
    try:
        if replay:
            if start_at is None or not math.isfinite(start_at) or not 0 <= wall - start_at <= 3600:
                raise TransferError("選擇最近一小時內的歷史起始時間")
            buy_target = now - (wall - start_at) + funding_seconds
            buy_at, ask = market.samples.at_or_after(buy_key, buy_target)
            sell_at, bid = market.samples.at_or_after(sell_key, buy_at + transfer_seconds)
        else:
            ask, bid = market.quotes.get(buy_key), market.quotes.get(sell_key)
            if not ask or not bid or now - min(ask.received, bid.received) > market.max_age:
                raise TransferError("目前沒有足夠新鮮的雙邊報價")
            if abs(ask.received - bid.received) > market.max_skew:
                raise TransferError("雙邊報價時間差過大，請再試一次")
            buy_at, sell_at = ask.received, bid.received
        result = estimate(amount, funding_fee, coin_fee, return_fee, ask, bid,
                          market.fees[buy], market.fees[sell])
    except TransferError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse({**result, "buy_at": wall + buy_at - now, "sell_at": wall + sell_at - now,
                         "buy_fee": market.fees[buy], "sell_fee": market.fees[sell]},
                        headers={"Cache-Control": "no-store"})


@app.get("/tools.css")
async def tools_styles():
    return FileResponse(PAGE.with_name("tools.css"), media_type="text/css")
