"""Probability strategy and independent risk controls for a local workstation.

This module never submits a real-money order. Live mode creates proposals only.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import math
from pathlib import Path
from threading import RLock
from uuid import uuid4
from .storage import Store


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def finite(value, name: str, minimum: float = 0, maximum: float = float("inf")) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric") from None
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def timestamp_value(value=None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return result.astimezone(timezone.utc)


def default_strategy(symbol: str = "BTC/USD") -> dict:
    return {"symbol": symbol, "interval": "1h", "threshold": .65, "exit_threshold": .65,
            "fixed_stop": .03, "trailing_stop": .05, "allocation": .1, "enabled": True,
            "model_id": "mock-momentum"}


class Engine:
    def __init__(self, data_dir: str | Path | None = None, initial_cash: float = 100000):
        self.lock = RLock()
        self.store = Store(data_dir)
        self.state = self.store.load() or self._fresh(finite(initial_cash, "initial_cash", .01))
        # Recovery is explicit: persisted orders/positions survive, autonomous operation pauses.
        self.state["running"] = False
        self._save("recovery", {"message": "Engine recovered; autonomous paper trading is paused"})

    @staticmethod
    def _fresh(cash: float) -> dict:
        return {"version": 1, "cash": cash, "initial_cash": cash, "peak_equity": cash,
                "positions": {}, "orders": [], "signals": [], "strategies": {"BTC/USD": default_strategy()},
                "proposals": [], "prices": {}, "running": False, "mode": "paper", "realized_pnl": 0,
                "fees_paid": 0, "last_completed": {}, "global_risk": {"max_position": .25,
                "max_exposure": .8, "max_drawdown": .15, "stale_after": 90, "fee_rate": .001, "slippage": .0005}}

    def _save(self, event: str, payload: dict, event_id: str | None = None, result: dict | None = None) -> None:
        self.store.save(self.state, now_iso(), event, payload, event_id, result)

    def _equity(self) -> float:
        if self.state["mode"] == "live":
            account = self.state.get("live_account")
            return float(account["equity"]) if account else 0.0
        return self.state["cash"] + sum(p["qty"] * p["price"] for p in self.state["positions"].values())

    def snapshot(self) -> dict:
        with self.lock:
            state = deepcopy(self.state)
            if state["mode"] == "live":
                account = state.get("live_account") or {"cash": 0, "equity": 0, "positions": {}}
                state["cash"], state["positions"] = account["cash"], account["positions"]
                state["orders"] = []
                state["broker_orders"] = deepcopy(account.get("orders", []))
            state["equity"] = self._equity()
            baseline = self.state.get("live_initial_equity", state["equity"]) if state["mode"] == "live" else state["initial_cash"]
            if state["mode"] == "live":
                state["initial_cash"] = baseline
                state["realized_pnl"] = None
                state["fees_paid"] = None
            state["pnl"] = state["equity"] - baseline
            state["pnl_pct"] = state["pnl"] / baseline * 100 if baseline else 0
            state["exposure"] = (state["equity"] - state["cash"]) / state["equity"] if state["equity"] > 0 else 0
            state["positions"] = [dict(p, symbol=s, market_value=p["qty"] * p["price"],
                                        unrealized_pnl=p["qty"] * (p["price"] - p["entry_price"]))
                                  for s, p in state["positions"].items()]
            state["unrealized_pnl"] = sum(p["unrealized_pnl"] for p in state["positions"])
            state["strategies"] = list(state["strategies"].values())
            state["orders"] = list(reversed(state["orders"][-200:]))
            state["signals"] = list(reversed([s for s in state["signals"] if s["mode"] == state["mode"]][-200:]))
            state["source"] = "fixture" if all(p.get("source") == "fixture" for p in state["prices"].values()) else "provider"
            state["audit"] = self.store.audit(40)
            return state

    def configure_strategy(self, config: dict) -> dict:
        with self.lock:
            symbol = str(config.get("symbol", "BTC/USD")).strip().upper()
            if not symbol or len(symbol) > 40:
                raise ValueError("Invalid symbol")
            strategy = deepcopy(self.state["strategies"].get(symbol, default_strategy(symbol)))
            for field in ("threshold", "exit_threshold", "fixed_stop", "trailing_stop", "allocation"):
                if field in config:
                    strategy[field] = None if config[field] is None and field in ("fixed_stop", "trailing_stop", "exit_threshold") else finite(config[field], field, 0, 1)
            if "threshold" in config and "exit_threshold" not in config:
                strategy["exit_threshold"] = strategy["threshold"]
            if strategy["exit_threshold"] is None:
                strategy["exit_threshold"] = strategy["threshold"]
            if "interval" in config:
                from .fixtures import interval_seconds
                interval_seconds(config["interval"])
                strategy["interval"] = config["interval"]
            for field in ("model_id", "enabled"):
                if field in config:
                    strategy[field] = str(config[field]) if field == "model_id" else bool(config[field])
            if strategy["allocation"] > self.state["global_risk"]["max_position"]:
                raise ValueError("Allocation exceeds the per-position risk limit")
            self.state["strategies"][symbol] = strategy
            self._save("strategy.updated", strategy)
            return deepcopy(strategy)

    def configure_risk(self, config: dict) -> dict:
        with self.lock:
            risk = deepcopy(self.state["global_risk"])
            for field in risk:
                if field in config:
                    maximum = 86400 if field == "stale_after" else 1
                    risk[field] = finite(config[field], field, .001 if field == "stale_after" else 0, maximum)
            if any(s["allocation"] > risk["max_position"] for s in self.state["strategies"].values()):
                raise ValueError("Existing strategy allocation exceeds the new position limit")
            self.state["global_risk"] = risk
            self._save("risk.updated", risk)
            return deepcopy(risk)

    def set_running(self, enabled: bool) -> dict:
        with self.lock:
            self.state["running"] = bool(enabled)
            self._save("engine.started" if enabled else "engine.paused", {"running": bool(enabled)})
            return self.snapshot()

    def reconcile_live_account(self, account: dict, positions: list[dict]) -> dict:
        """Persist a read-only broker snapshot. Never accepts credentials or submits orders."""
        with self.lock:
            normalized = {}
            for p in positions:
                symbol = str(p["symbol"]).upper()
                quantity = finite(p.get("qty", p.get("quantity")), "live quantity", 0)
                entry = finite(p.get("avg_entry_price", p.get("entry_price", 0)), "live entry price", .000000001)
                price = finite(p.get("current_price", p.get("price", entry)), "live mark", .000000001)
                if quantity:
                    old = (self.state.get("live_account") or {}).get("positions", {}).get(symbol, {})
                    normalized[symbol] = {"qty": quantity, "entry_price": entry, "price": price,
                                          "high_watermark": max(price, old.get("high_watermark", price)),
                                          "opened_at": old.get("opened_at", now_iso()), "source": "provider"}
            cash = finite(account.get("cash", account.get("buying_power", 0)), "live cash", 0)
            equity = finite(account.get("equity", cash + sum(p["qty"] * p["price"] for p in normalized.values())), "live equity", .01)
            self.state["live_account"] = {"cash": cash, "equity": equity, "positions": normalized, "orders": deepcopy(account.get("orders", [])), "timestamp": now_iso(), "source": "provider", "provider": account.get("provider")}
            self.state.setdefault("live_initial_equity", equity)
            self.state["live_peak_equity"] = max(self.state.get("live_peak_equity", equity), equity)
            self._save("live.reconciled", {"cash": cash, "equity": equity, "positions": len(normalized)})
            return self.snapshot()

    def _validate_live_proposal(self, proposal: dict, price: float) -> None:
        if self.state["mode"] != "live":
            raise ValueError("Live proposals are unavailable in paper mode")
        account = self.state.get("live_account")
        risk = self.state["global_risk"]
        if not account or (datetime.now(timezone.utc) - timestamp_value(account["timestamp"])).total_seconds() > risk["stale_after"]:
            raise ValueError("Reconcile the live account before confirming an order")
        if account.get("provider") and proposal.get("source") != account["provider"]:
            raise ValueError("Proposal provider does not match the reconciled account")
        equity, cash = account["equity"], account["cash"]
        drawdown = 1 - equity / self.state.get("live_peak_equity", equity)
        if drawdown >= risk["max_drawdown"]:
            self.state["running"] = False
            self._save("risk.confirmation_blocked", {"proposal_id": proposal["id"], "reason": "drawdown"})
            if proposal["side"] == "BUY":
                raise ValueError("Portfolio drawdown limit reached; live buys are blocked")
        active_statuses = ("awaiting_review", "submitting", "submitted", "unknown", "reconcile_required")
        other = [p for p in self.state["proposals"] if p["id"] != proposal["id"] and p["status"] in active_statuses]
        canonical = lambda symbol: str(symbol).upper().replace("/", "").replace("-", "")
        if any(canonical(p["symbol"]) == canonical(proposal["symbol"]) for p in other):
            raise ValueError("Another unresolved live order exists for this asset; reconcile it first")
        broker_orders = account.get("orders", [])
        if not isinstance(broker_orders, list):
            raise ValueError("Broker outstanding orders could not be reconciled")
        broker_active = [o for o in broker_orders if o.get("status") not in ("filled", "canceled", "cancelled", "expired", "rejected")]
        if any(canonical(o.get("symbol", "")) == canonical(proposal["symbol"]) for o in broker_active):
            raise ValueError("An outstanding broker order exists for this asset; reconcile it first")
        position = next((p for s, p in account["positions"].items() if canonical(s) == canonical(proposal["symbol"])), None)
        notional = proposal["qty"] * price
        if proposal["side"] == "BUY":
            reserved = sum(p["qty"] * p.get("confirmation_price", p["price"]) * (1 + risk["fee_rate"]) for p in other if p["side"] == "BUY")
            for order in broker_active:
                if str(order.get("side", "")).upper() != "BUY":
                    continue
                known_ids = {str(p["id"]) for p in other}
                if str(order.get("client_order_id", "")) in known_ids:
                    continue
                order_price = order.get("limit_price") or order.get("stop_price") or order.get("price")
                if order_price is None:
                    raise ValueError("Reconcile outstanding market buys before another live buy")
                remaining = max(0, finite(order.get("qty", 0), "broker order quantity") - finite(order.get("filled_qty", 0), "broker filled quantity"))
                reserved += remaining * finite(order_price, "broker order price", .000000001) * (1 + risk["fee_rate"])
            exposure = sum(p["qty"] * p["price"] for p in account["positions"].values())
            held_value = position["qty"] * price if position else 0
            cost = notional * (1 + risk["fee_rate"])
            if cost + reserved > cash + 1e-8:
                raise ValueError("Insufficient live cash after outstanding order reservations")
            if held_value + notional > equity * risk["max_position"] + 1e-8:
                raise ValueError("Live per-position risk limit exceeded at confirmation price")
            if exposure + notional + reserved > equity * risk["max_exposure"] + 1e-8:
                raise ValueError("Live portfolio exposure limit exceeded at confirmation price")
        elif not position or proposal["qty"] > position["qty"] + 1e-9:
            raise ValueError("Live position changed; cannot confirm this sell quantity")

    def propose_order(self, symbol: str, side: str, qty: float, price: float,
                      event_id: str | None = None, source: str = "alpaca", timestamp=None) -> dict:
        """Create a reviewed-order draft only. Does not claim or submit the order."""
        symbol, side = str(symbol).strip().upper(), str(side).upper()
        if not symbol or len(symbol) > 40 or side not in ("BUY", "SELL"):
            raise ValueError("A symbol and BUY or SELL side are required")
        if not source or source in ("fixture", "demo", "mock", "simulation"):
            raise ValueError("Live proposals require actual provider market data")
        qty, price = finite(qty, "quantity", 1e-12), finite(price, "price", 1e-12)
        stamp = timestamp_value(timestamp)
        event_id = str(event_id or uuid4())
        with self.lock:
            previous = self.store.event(event_id)
            if previous is not None:
                return previous
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            if age > self.state["global_risk"]["stale_after"] or age < -5:
                raise ValueError("A fresh provider quote is required for a live proposal")
            proposal = {"id": event_id, "symbol": symbol, "side": side, "qty": qty, "price": price,
                        "status": "awaiting_review", "timestamp": stamp.isoformat(), "source": source,
                        "mode": "live", "reason": "Manual order draft; confirmation required"}
            self._validate_live_proposal(proposal, price)
            self.state["proposals"].append(proposal)
            self._save("proposal.created", proposal, event_id, proposal)
            return deepcopy(proposal)

    def claim_proposal(self, proposal_id: str, quote_price: float | None = None) -> dict:
        """Atomic, durable claim before the caller's explicitly confirmed broker request."""
        with self.lock:
            proposal = next((p for p in self.state["proposals"] if p["id"] == proposal_id), None)
            if proposal is None:
                raise ValueError("Proposal not found")
            if proposal["status"] != "awaiting_review":
                raise ValueError("Proposal has already been claimed or resolved; reconcile before retrying")
            if (datetime.now(timezone.utc) - timestamp_value(proposal["timestamp"])).total_seconds() > self.state["global_risk"]["stale_after"]:
                proposal["status"] = "expired"
                self._save("proposal.expired", proposal)
                raise ValueError("Proposal expired; obtain a fresh signal and quote")
            price = finite(quote_price if quote_price is not None else proposal["price"], "confirmation price", .000000001)
            self._validate_live_proposal(proposal, price)
            proposal["confirmation_price"] = price
            proposal["status"] = "submitting"
            self._save("proposal.claimed", proposal)
            return deepcopy(proposal)

    def finish_proposal(self, proposal_id: str, status: str, result: dict | None = None) -> dict:
        if status not in ("submitted", "filled", "failed", "rejected", "expired", "unknown", "reconcile_required", "cancelled"):
            raise ValueError("Invalid proposal status")
        with self.lock:
            proposal = next((p for p in self.state["proposals"] if p["id"] == proposal_id), None)
            if proposal is None:
                raise ValueError("Proposal not found")
            if proposal["status"] not in ("awaiting_review", "submitting", "submitted", "unknown", "reconcile_required"):
                raise ValueError("Proposal already resolved")
            proposal.update(status=status, result=result or {}, resolved_at=now_iso())
            self._save("proposal.resolved", proposal)
            return deepcopy(proposal)

    def set_mode(self, mode: str) -> dict:
        if mode not in ("paper", "live"):
            raise ValueError("Mode must be paper or live")
        with self.lock:
            if mode != self.state["mode"]:
                self.state["running"] = False
            self.state["mode"] = mode
            self._save("mode.changed", {"mode": mode, "execution": "manual confirmation required" if mode == "live" else "simulated"})
            return self.snapshot()

    def reset(self) -> dict:
        with self.lock:
            self.state = self._fresh(self.state["initial_cash"])
            self._save("paper.reset", {"initial_cash": self.state["cash"]})
            return self.snapshot()

    def _fill(self, symbol: str, side: str, qty: float, price: float, event_id: str, reason: str = "manual") -> dict:
        if self.state["mode"] != "paper":
            raise ValueError("Paper execution is unavailable in live mode")
        risk = self.state["global_risk"]
        fill_price = price * (1 + risk["slippage"] if side == "BUY" else 1 - risk["slippage"])
        notional = qty * fill_price
        fee = notional * risk["fee_rate"]
        positions = self.state["positions"]
        existing = positions.get(symbol)
        if side == "BUY":
            if notional + fee > self.state["cash"] + 1e-8:
                raise ValueError("Insufficient paper cash including fees and slippage")
            equity = self._equity()
            old_value = existing["qty"] * price if existing else 0
            exposure = sum(p["qty"] * p["price"] for p in positions.values())
            if old_value + notional > equity * risk["max_position"] + 1e-8:
                raise ValueError("Per-position allocation limit exceeded")
            if exposure + notional > equity * risk["max_exposure"] + 1e-8:
                raise ValueError("Portfolio exposure limit exceeded")
            old_qty = existing["qty"] if existing else 0
            old_cost = old_qty * existing["entry_price"] if existing else 0
            positions[symbol] = {"qty": old_qty + qty, "entry_price": (old_cost + notional + fee) / (old_qty + qty),
                                 "price": price, "high_watermark": max(price, existing["high_watermark"] if existing else price),
                                 "opened_at": existing["opened_at"] if existing else now_iso(), "source": self.state["prices"].get(symbol, {}).get("source", "fixture")}
            self.state["cash"] -= notional + fee
            self.state["strategies"].setdefault(symbol, default_strategy(symbol))
            realized = 0.0
        else:
            if not existing or qty > existing["qty"] + 1e-9:
                raise ValueError("Cannot sell more than the held paper position")
            realized = notional - fee - qty * existing["entry_price"]
            self.state["realized_pnl"] += realized
            self.state["cash"] += notional - fee
            existing["qty"] -= qty
            if existing["qty"] < 1e-9:
                del positions[symbol]
        self.state["fees_paid"] += fee
        order = {"id": event_id, "symbol": symbol, "side": side, "qty": qty, "price": fill_price,
                 "notional": notional, "fee": fee, "realized_pnl": realized, "status": "filled",
                 "timestamp": now_iso(), "mode": "paper", "source": self.state["prices"].get(symbol, {}).get("source", "fixture"), "reason": reason}
        self.state["orders"].append(order)
        self.state["orders"] = self.state["orders"][-5000:]
        return order

    def paper_order(self, symbol: str, side: str, qty: float, price: float, event_id: str | None = None) -> dict:
        symbol, side = str(symbol).strip().upper(), str(side).upper()
        if side not in ("BUY", "SELL") or not symbol:
            raise ValueError("A symbol and BUY or SELL side are required")
        qty, price = finite(qty, "quantity", 1e-12), finite(price, "price", 1e-12)
        event_id = str(event_id or uuid4())
        with self.lock:
            previous = self.store.event(event_id)
            if previous is not None:
                return previous
            result = self._fill(symbol, side, qty, price, event_id)
            self._save("paper.filled", result, event_id, result)
            return deepcopy(result)

    def tick(self, symbol: str, price: float, probability: float | None = None,
             timestamp=None, event_id: str | None = None, *, completed: bool = True,
             source: str = "fixture", interval_id: str | int | None = None) -> dict:
        symbol = str(symbol).strip().upper()
        price = finite(price, "price", 1e-12)
        q = finite(probability, "probability", 0, 1) if probability is not None else None
        stamp = timestamp_value(timestamp)
        event_id = str(event_id or uuid4())
        with self.lock:
            previous = self.store.event(event_id)
            if previous is not None:
                return previous
            strategy = self.state["strategies"].get(symbol, default_strategy(symbol))
            signal = {"id": event_id, "symbol": symbol, "price": price, "probability": q,
                      "threshold": strategy["threshold"], "timestamp": stamp.isoformat(), "source": source,
                      "mode": self.state["mode"], "action": "WAIT", "reason": "Engine paused"}
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            last_price = self.state["prices"].get(symbol)
            stale = age > self.state["global_risk"]["stale_after"] or age < -5
            out_of_order = bool(last_price and stamp < timestamp_value(last_price["timestamp"]))
            live_account = self.state.get("live_account")
            live_unavailable = self.state["mode"] == "live" and (source == "fixture" or not live_account or
                (datetime.now(timezone.utc) - timestamp_value(live_account["timestamp"])).total_seconds() > self.state["global_risk"]["stale_after"])
            if live_unavailable:
                signal["reason"] = "Live trading requires provider data and a freshly reconciled broker account"
            elif stale or out_of_order:
                signal["reason"] = "Stale or out-of-order market data; trading blocked"
            else:
                self.state["prices"][symbol] = {"price": price, "timestamp": stamp.isoformat(), "source": source}
                positions = live_account["positions"] if self.state["mode"] == "live" else self.state["positions"]
                position = positions.get(symbol)
                stop_reason = None
                if position:
                    position["price"] = price
                    position["high_watermark"] = max(position["high_watermark"], price)
                    if strategy["fixed_stop"] and price <= position["entry_price"] * (1 - strategy["fixed_stop"]):
                        stop_reason = "Fixed stop-loss triggered"
                    elif strategy["trailing_stop"] and price <= position["high_watermark"] * (1 - strategy["trailing_stop"]):
                        stop_reason = "Trailing stop-loss triggered"
                equity = self._equity()
                peak_key = "live_peak_equity" if self.state["mode"] == "live" else "peak_equity"
                self.state[peak_key] = max(self.state.get(peak_key, equity), equity)
                drawdown = 1 - equity / self.state[peak_key] if self.state[peak_key] else 0
                risk_block = drawdown >= self.state["global_risk"]["max_drawdown"]
                if risk_block:
                    self.state["running"] = False
                if stop_reason:
                    signal.update(action="SELL", reason=stop_reason)
                elif risk_block:
                    signal["reason"] = "Portfolio drawdown limit reached; engine paused"
                elif completed and self.state["running"] and strategy["enabled"]:
                    if q is None:
                        signal["reason"] = "No valid model prediction; trading blocked"
                    else:
                        from .fixtures import interval_seconds
                        boundary = interval_id if interval_id is not None else int(stamp.timestamp()) // interval_seconds(strategy["interval"])
                        if self.state["last_completed"].get(symbol) == boundary:
                            signal["reason"] = "This interval has already been evaluated"
                        else:
                            self.state["last_completed"][symbol] = boundary
                            if position:
                                signal.update(action="HOLD" if q >= strategy["exit_threshold"] else "SELL",
                                              reason="Probability above exit threshold" if q >= strategy["exit_threshold"] else "Probability below exit threshold")
                            else:
                                signal.update(action="BUY" if q >= strategy["threshold"] else "WAIT",
                                              reason="Entry threshold reached" if q >= strategy["threshold"] else "Probability below entry threshold")
                elif not completed and not stop_reason:
                    signal["reason"] = "Monitoring prices between completed intervals"
                if signal["action"] in ("BUY", "SELL"):
                    qty = position["qty"] if signal["action"] == "SELL" and position else 0
                    if signal["action"] == "BUY":
                        risk = self.state["global_risk"]
                        cash = live_account["cash"] if self.state["mode"] == "live" else self.state["cash"]
                        available = min(equity * strategy["allocation"], cash / (1 + risk["fee_rate"]))
                        qty = available / (price * (1 + risk["slippage"]))
                    if self.state["mode"] == "live":
                        # Live account execution and balances belong to the broker adapter.
                        proposal = {"id": event_id, "symbol": symbol, "side": signal["action"], "qty": qty,
                                    "price": price, "status": "awaiting_review", "reason": signal["reason"],
                                    "timestamp": stamp.isoformat(), "source": source, "mode": "live"}
                        try:
                            finite(qty, "quantity", 1e-12)
                            self._validate_live_proposal(proposal, price)
                        except ValueError as exc:
                            signal.update(action="WAIT", reason=f"Risk manager: {exc}")
                            self.state["signals"].append(signal)
                            self._save("strategy.signal", signal, event_id, signal)
                            return deepcopy(signal)
                        self.state["proposals"].append(proposal)
                        signal["proposal"] = proposal
                    else:
                        try:
                            signal["order"] = self._fill(symbol, signal["action"], qty, price, event_id, signal["reason"])
                        except ValueError as exc:
                            signal.update(action="WAIT", reason=f"Risk manager: {exc}")
            self.state["signals"].append(signal)
            self.state["signals"] = self.state["signals"][-5000:]
            self._save("strategy.signal", signal, event_id, signal)
            return deepcopy(signal)

    def close(self) -> None:
        self.store.close()
