"""Conservative OHLC backtesting with next-bar execution and no future features."""
from __future__ import annotations
import math
from statistics import mean, pstdev
from .core import finite
from .fixtures import generate_candles, interval_seconds


def mock_probability(observations: list[dict]) -> float:
    """A deterministic endpoint stand-in, calculated from past closing prices only."""
    if len(observations) < 3:
        return .5
    closes = [float(c["close"]) for c in observations[-12:]]
    momentum = closes[-1] / closes[0] - 1
    return min(.95, max(.05, .5 + momentum * 14))


def run_backtest(symbol: str = "BTC/USD", interval: str = "1h", threshold: float = .65,
                 exit_threshold: float | None = None, fixed_stop: float | None = .03,
                 trailing_stop: float | None = .05, allocation: float = .1,
                 initial_cash: float = 100000, candles: list[dict] | None = None,
                 probabilities: list[float] | None = None, fee_rate: float = .001,
                 slippage: float = .0005, **kwargs) -> dict:
    threshold = finite(threshold, "threshold", 0, 1)
    exit_threshold = threshold if exit_threshold is None else finite(exit_threshold, "exit_threshold", 0, 1)
    fixed_stop = None if fixed_stop is None else finite(fixed_stop, "fixed_stop", 0, 1)
    trailing_stop = None if trailing_stop is None else finite(trailing_stop, "trailing_stop", 0, 1)
    allocation = finite(allocation, "allocation", .00001, .25)
    initial_cash = finite(initial_cash, "initial_cash", .01)
    fee_rate, slippage = finite(fee_rate, "fee_rate", 0, .1), finite(slippage, "slippage", 0, .1)
    candles = candles if candles is not None else generate_candles(symbol, int(kwargs.get("count", 720)), interval)
    if len(candles) < 3:
        raise ValueError("Backtesting requires at least three candles")
    if probabilities is not None and len(probabilities) != len(candles):
        raise ValueError("Probability count must match candle count")
    previous_time = None
    for c in candles:
        for field in ("open", "high", "low", "close"):
            finite(c[field], field, .000000001)
        if c["low"] > min(c["open"], c["close"]) or c["high"] < max(c["open"], c["close"]):
            raise ValueError("Invalid candle range")
        if previous_time is not None and c["time"] <= previous_time:
            raise ValueError("Candles must be strictly ordered with unique timestamps")
        previous_time = c["time"]
    cash, qty, entry_price, watermark = initial_cash, 0.0, 0.0, 0.0
    pending = None
    orders, trades, equity_curve, drawdowns = [], [], [], []
    peak, entry_time, fees = initial_cash, None, 0.0

    def buy(price: float, timestamp, reason: str):
        nonlocal cash, qty, entry_price, watermark, entry_time, fees
        fill = price * (1 + slippage)
        budget = min(cash, (cash + qty * price) * allocation)
        quantity = budget / (fill * (1 + fee_rate))
        cost = quantity * fill
        fee = cost * fee_rate
        cash -= cost + fee
        qty, entry_price, watermark, entry_time = quantity, (cost + fee) / quantity, price, timestamp
        fees += fee
        orders.append({"time": timestamp, "symbol": symbol, "side": "BUY", "qty": quantity, "price": fill, "fee": fee, "reason": reason, "source": "fixture"})

    def sell(price: float, timestamp, reason: str):
        nonlocal cash, qty, entry_price, watermark, entry_time, fees
        fill = price * (1 - slippage)
        proceeds = qty * fill
        fee = proceeds * fee_rate
        pnl = proceeds - fee - qty * entry_price
        cash += proceeds - fee
        fees += fee
        orders.append({"time": timestamp, "symbol": symbol, "side": "SELL", "qty": qty, "price": fill, "fee": fee, "reason": reason, "source": "fixture"})
        trades.append({"entry_time": entry_time, "exit_time": timestamp, "symbol": symbol, "qty": qty, "entry_price": entry_price,
                       "exit_price": fill, "pnl": pnl, "return_pct": (fill * (1 - fee_rate) / entry_price - 1) * 100, "reason": reason})
        qty, entry_price, watermark, entry_time = 0.0, 0.0, 0.0, None

    for i, candle in enumerate(candles):
        opening, high, low, closing = [float(candle[x]) for x in ("open", "high", "low", "close")]
        timestamp = candle["time"]
        if pending == "BUY" and not qty:
            buy(opening, timestamp, "Prior completed-candle probability reached threshold")
        elif pending == "SELL" and qty:
            sell(opening, timestamp, "Prior completed-candle probability fell below exit threshold")
        pending = None
        if qty:
            # Only the watermark known before this bar can trigger this bar's trailing stop.
            # Gap-through fills use the less favorable opening price.
            stops = []
            if fixed_stop:
                stops.append((entry_price * (1 - fixed_stop), "Fixed stop-loss"))
            if trailing_stop:
                stops.append((watermark * (1 - trailing_stop), "Trailing stop-loss"))
            if stops:
                stop_price, reason = max(stops, key=lambda pair: pair[0])
                if low <= stop_price:
                    sell(min(opening, stop_price), timestamp, reason)
            if qty:
                watermark = max(watermark, high)
        equity = cash + qty * closing
        peak = max(peak, equity)
        drawdown = 1 - equity / peak
        equity_curve.append({"time": timestamp, "value": round(equity, 6)})
        drawdowns.append({"time": timestamp, "value": round(-drawdown * 100, 6)})
        q = finite(probabilities[i], "probability", 0, 1) if probabilities is not None else mock_probability(candles[max(0, i - 11):i + 1])
        if i < len(candles) - 1:
            pending = "SELL" if qty and q < exit_threshold else "BUY" if not qty and q >= threshold else None
    final_equity = cash + qty * float(candles[-1]["close"])
    returns = [equity_curve[i]["value"] / equity_curve[i - 1]["value"] - 1 for i in range(1, len(equity_curve))]
    sigma = pstdev(returns) if returns else 0
    periods_per_year = 365.25 * 86400 / interval_seconds(interval)
    sharpe = mean(returns) / sigma * math.sqrt(periods_per_year) if sigma > 0 else 0
    winners = [t for t in trades if t["pnl"] > 0]
    gains, losses = sum(t["pnl"] for t in winners), -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    total_return = (final_equity / initial_cash - 1) * 100
    max_drawdown = -min(p["value"] for p in drawdowns)
    return {"symbol": symbol, "interval": interval, "source": "fixture" if all(c.get("source", "fixture") == "fixture" for c in candles) else "historical",
            "initial_cash": initial_cash, "final_equity": final_equity, "total_return": total_return, "return_pct": total_return,
            "max_drawdown": max_drawdown, "win_rate": len(winners) / len(trades) * 100 if trades else 0,
            "sharpe": sharpe, "profit_factor": gains / losses if losses else None, "total_trades": len(trades),
            "fees_paid": fees, "open_position": {"qty": qty, "entry_price": entry_price} if qty else None,
            "equity_curve": equity_curve, "drawdown_curve": drawdowns, "orders": orders, "trades": trades,
            "assumptions": ["Signals use completed candles only; execution at next candle open", "Existing trailing watermark is used before each candle high", "Stop gap-through fills use opening price", "Constant proportional fees and slippage; no liquidity or partial-fill model", "Final open position is marked to market, not force-closed"]}
