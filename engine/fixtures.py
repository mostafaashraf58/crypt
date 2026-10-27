"""Deterministic synthetic OHLCV. These values are never market or live data."""
from __future__ import annotations
import hashlib
import math
import random

ASSETS = [
    {"symbol": "BTC/USD", "name": "Bitcoin", "kind": "crypto", "price": 67432.80, "change": 2.34},
    {"symbol": "ETH/USD", "name": "Ethereum", "kind": "crypto", "price": 3521.65, "change": 1.87},
    {"symbol": "SOL/USD", "name": "Solana", "kind": "crypto", "price": 172.42, "change": -0.64},
    {"symbol": "AAPL", "name": "Apple Inc.", "kind": "stock", "price": 224.18, "change": 1.24},
    {"symbol": "NVDA", "name": "NVIDIA Corporation", "kind": "stock", "price": 138.85, "change": 3.12},
    {"symbol": "MSFT", "name": "Microsoft Corporation", "kind": "stock", "price": 428.76, "change": -0.38},
    {"symbol": "TSLA", "name": "Tesla, Inc.", "kind": "stock", "price": 248.50, "change": 2.01},
    {"symbol": "SPY", "name": "SPDR S&P 500 ETF", "kind": "stock", "price": 578.16, "change": 0.62},
]
for asset in ASSETS:
    asset["volume"] = round(asset["price"] * 16420, 2)
    asset["source"] = "fixture"


def interval_seconds(interval: str | int) -> int:
    if isinstance(interval, int):
        if interval < 1:
            raise ValueError("Interval must be positive")
        return interval
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        value = int(interval[:-1]) * units[interval[-1]]
    except (KeyError, ValueError, IndexError):
        raise ValueError("Interval must be an integer followed by s, m, h, d or w") from None
    if value < 1:
        raise ValueError("Interval must be positive")
    return value


def generate_candles(symbol: str = "BTC/USD", count: int = 180, interval: str = "1h") -> list[dict]:
    if not 1 <= count <= 20000:
        raise ValueError("Candle count must be between 1 and 20000")
    seconds = interval_seconds(interval)
    seed = int(hashlib.sha256(symbol.encode()).hexdigest()[:12], 16)
    rng = random.Random(seed)
    base = next((x["price"] for x in ASSETS if x["symbol"] == symbol), 100.0)
    # Frozen date makes tests and offline comparisons reproducible.
    start = 1780272000
    price = base * .94
    candles = []
    for index in range(count):
        opening = price
        impulse = .00045 + math.sin(index / 15) * .0018 + rng.uniform(-.0048, .0048)
        price = max(.000001, opening * (1 + impulse))
        spread = abs(price - opening) + opening * rng.uniform(.0005, .002)
        candles.append({"time": start + index * seconds, "open": round(opening, 6),
                        "high": round(max(opening, price) + spread * .45, 6),
                        "low": round(min(opening, price) - spread * .45, 6),
                        "close": round(price, 6), "volume": round(rng.uniform(40, 2400), 2), "source": "fixture"})
    return candles
