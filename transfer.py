"""Replay the two execution legs using sampled crypto order books."""
from bisect import bisect_left
from collections import defaultdict, deque
from math import isfinite


class TransferError(ValueError):
    pass


class OrderBookSamples:
    def __init__(self):
        self.books = defaultdict(lambda: deque(maxlen=1801))
        self.last_sample = float("-inf")

    def capture(self, now, quotes, max_age):
        if now - self.last_sample < 2:
            return
        self.last_sample = now
        for key, quote in quotes.items():
            if now - quote.received <= max_age:
                self.books[key].append((now, quote))

    def at_or_after(self, key, target):
        series = self.books.get(key, ())
        index = bisect_left(series, target, key=lambda entry: entry[0])
        if index == len(series) or series[index][0] - target > 5:
            raise TransferError("目標時間附近沒有新鮮的一檔報價")
        return series[index]


def estimate(amount, funding_fee, coin_fee, return_fee, ask, bid, buy_fee, sell_fee):
    """Use quote currency for funding, sale and return fees; deduct buy fee in base currency."""
    spend = amount - funding_fee
    if spend <= 0:
        raise TransferError("USDT 入金費已用完本金")
    bought = spend / ask.ask
    received = bought * (1 - buy_fee) - coin_fee
    if received <= 0:
        raise TransferError("提幣費已用完買到的幣量")
    if bought > ask.ask_qty or received > bid.bid_qty:
        raise TransferError("買入或賣出的一檔掛單量不足")
    end = received * bid.bid * (1 - sell_fee) - return_fee
    values = {"bought": bought, "received": received, "end": end, "net": end - amount,
              "break_even": (amount + return_fee) / (received * (1 - sell_fee)),
              "ask": ask.ask, "bid": bid.bid}
    if not all(isfinite(value) for value in values.values()):
        raise TransferError("計算超出可表示範圍")
    return values
