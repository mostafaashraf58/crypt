"""Probability strategy and independent risk controls for a local workstation.

This module never submits an exchange order. The API execution controller handles
manually confirmed drafts and session-authorized autonomous strategy decisions.
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
            "model_id": "mock-momentum", "take_profit": 0}


class Engine:
    def __init__(self, data_dir: str | Path | None = None, initial_cash: float = 100000):
        self.lock = RLock()
        self.store = Store(data_dir)
        self.state = self.store.load() or self._fresh(finite(initial_cash, "initial_cash", .01))
        if self.state.get("version",1) not in (1,2):
            self.store.close()
            raise RuntimeError("Unsupported workspace version; do not overwrite newer data")
        self.state["version"]=2
        self.state["global_risk"].setdefault("price_bound", .005)
        self.state.setdefault("exit_intents", {})
        self.state.setdefault("watchlist", ["BTC/USDT", "ETH/USDT"])
        self.state.setdefault("connections", {})
        self.state.setdefault("equity_history", [])
        self.state.setdefault("risk_status", "normal")
        self.state.setdefault("protection", {})
        for key,value in {"max_daily_loss":.05,"max_daily_trades":100,"max_daily_turnover":5}.items():
            self.state["global_risk"].setdefault(key,value)
        self.state.setdefault("daily",{})
        self.state.setdefault("account_states",{})
        # Recovery is explicit: persisted orders/positions survive, autonomous operation pauses.
        self.state["running"] = False
        self._save("recovery", {"message": "Engine recovered; autonomous paper trading is paused"})

    @staticmethod
    def _fresh(cash: float) -> dict:
        return {"version": 2, "exit_intents":{},"watchlist":["BTC/USDT","ETH/USDT"],"connections":{},"equity_history":[],"risk_status":"normal","protection":{},"daily":{},"account_states":{}, "cash": cash, "initial_cash": cash, "peak_equity": cash,
                "positions": {}, "orders": [], "signals": [], "strategies": {"BTC/USD": default_strategy()},
                "proposals": [], "prices": {}, "running": False, "mode": "paper", "realized_pnl": 0,
                "fees_paid": 0, "last_completed": {}, "global_risk": {"max_position": .25,
                "max_exposure": .8, "max_drawdown": .15, "stale_after": 90, "fee_rate": .001, "slippage": .0005, "price_bound": .005,"max_daily_loss":.05,"max_daily_trades":100,"max_daily_turnover":5}}

    def _save(self, event: str, payload: dict, event_id: str | None = None, result: dict | None = None) -> None:
        if self.state["mode"] == "paper":
            history = self.state.setdefault("paper_equity_history", [])
            stamp = datetime.now(timezone.utc).timestamp()
            if not history or stamp - history[-1]["time"] >= 60 or event in {"paper.filled", "paper.reset", "paper.migrated"} or payload.get("order"):
                history.append({"time":stamp, "value":self._equity(), "provider":"paper"})
                self.state["paper_equity_history"] = history[-10000:]
        terminal={"filled","cancelled","rejected","expired","failed"}
        resolved=[p for p in self.state["proposals"] if p["status"] in terminal]
        if len(resolved)>200:
            archive=resolved[:-200]
            for proposal in archive:
                self.store.record("order-history",proposal["id"],proposal,write=True)
            remove={p["id"] for p in archive}
            self.state["proposals"]=[p for p in self.state["proposals"] if p["id"] not in remove]
        self.state["signals"]=self.state["signals"][-500:]
        self.store.save(self.state, now_iso(), event, payload, event_id, result)

    def _equity(self) -> float:
        if self.state["mode"] == "live":
            account = self.state.get("live_account")
            return account["cash"] + sum(p["qty"]*p["price"] for p in account["positions"].values()) if account else 0.0
        return self.state["cash"] + sum(p["qty"] * p["price"] for p in self.state["positions"].values())

    def snapshot(self) -> dict:
        with self.lock:
            state = deepcopy(self.state)
            if state["mode"] == "live":
                account = state.get("live_account") or {"cash": 0, "equity": 0, "positions": {}}
                state["cash"], state["positions"] = account["cash"], account["positions"]
                state["orders"] = []
                state["broker_orders"] = deepcopy(account.get("orders", []))
            state["currency"] = (self.state.get("live_account") or {}).get("currency", "USD") if state["mode"] == "live" else "USD"
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
            if state["mode"] == "paper":
                state["equity_history"] = deepcopy(state.get("paper_equity_history", []))
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
            if "take_profit" in config:
                strategy["take_profit"] = finite(config["take_profit"], "take_profit", 0, 10)
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
                    maximum = 86400 if field == "stale_after" else .1 if field == "price_bound" else 100000 if field == "max_daily_trades" else 1000 if field == "max_daily_turnover" else 1
                    risk[field] = finite(config[field], field, .001 if field == "stale_after" else 0, maximum)
                    if field=="max_daily_trades" and not risk[field].is_integer():
                        raise ValueError("Daily order limit must be a whole number")
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
            day=datetime.now(timezone.utc).date().isoformat()
            previous=self.state.get("live_account") or {}
            identity=(account.get("provider"),account.get("currency","USD"),account.get("account_id"))
            old_identity=(previous.get("provider"),previous.get("currency","USD"),previous.get("account_id"))
            if identity!=old_identity and previous:
                self.state["account_states"]["|".join(str(v) for v in old_identity)]={"initial":self.state.get("live_initial_equity"),"peak":self.state.get("live_peak_equity"),"exits":deepcopy(self.state["exit_intents"]),"protection":deepcopy(self.state["protection"])}
                saved=self.state["account_states"].get("|".join(str(v) for v in identity),{})
                self.state["exit_intents"]=deepcopy(saved.get("exits",{}))
                self.state["protection"]=deepcopy(saved.get("protection",{}))
            normalized = {}
            for p in positions:
                symbol = str(p["symbol"]).upper()
                quantity = finite(p.get("qty", p.get("quantity")), "live quantity", 0)
                entry = finite(p.get("avg_entry_price", p.get("entry_price", 0)), "live entry price", .000000001)
                price = finite(p.get("current_price", p.get("price", entry)), "live mark", .000000001)
                if quantity:
                    old = previous.get("positions",{}).get(symbol,{}) if identity==old_identity else {}
                    normalized[symbol] = {"qty": quantity, "entry_price": entry, "price": price,
                                          "high_watermark": max(price, old.get("high_watermark", price)),
                                          "opened_at": old.get("opened_at", now_iso()), "source": "provider",
                                          "available_qty": finite(p.get("available_qty", quantity), "available quantity", 0)}
            cash = finite(account.get("cash", account.get("buying_power", 0)), "live cash", 0)
            equity = finite(account.get("equity", cash + sum(p["qty"] * p["price"] for p in normalized.values())), "live equity", 0)
            identity = (account.get("provider"), account.get("currency", "USD"), account.get("account_id"))
            previous = self.state.get("live_account") or {}
            cash_flow=float(account.get("cash_flow",0))
            cash_delta=cash_flow-float(previous.get("cash_flow",0)) if previous.get("provider")==account.get("provider") else 0
            if cash_delta:
                self.state["live_initial_equity"]=self.state.get("live_initial_equity",equity)+cash_delta
                self.state["live_peak_equity"]=self.state.get("live_peak_equity",equity)+cash_delta
            if identity != (previous.get("provider"), previous.get("currency", "USD"), previous.get("account_id")):
                saved=self.state["account_states"].get("|".join(str(v) for v in identity),{})
                self.state["live_initial_equity"] = saved.get("initial") or equity
                self.state["live_peak_equity"] = saved.get("peak") or equity
            self.state["live_account"] = {"currency": account.get("currency", "USD"), "account_id": account.get("account_id"),
                                          "cash_flow":cash_flow, "available_cash": finite(account.get("available_cash", cash), "available cash", 0), "cash": cash, "equity": equity, "positions": normalized, "orders": deepcopy(account.get("orders", [])), "timestamp": now_iso(), "source": "provider", "provider": account.get("provider")}
            identity_key="|".join(str(v) for v in identity)
            daily_key=day+":"+identity_key
            daily=self.state["daily"].setdefault(daily_key,{"initial_equity":equity,"cash_flow":cash_flow,"orders":0,"turnover":0})
            self.state["daily_key"]=daily_key
            for key,intent in list(self.state["exit_intents"].items()):
                if intent["provider"] == account.get("provider") and intent["symbol"] not in normalized:
                    del self.state["exit_intents"][key]
            self.state["equity_history"].append({"time":int(datetime.now(timezone.utc).timestamp()),"value":equity,"currency":account.get("currency","USD"),"provider":account.get("provider"),"account_id":account.get("account_id")})
            self.store.record("equity-history",str(int(datetime.now(timezone.utc).timestamp()*1000000))+"-"+str(uuid4()),self.state["equity_history"][-1],write=True)
            self.state["equity_history"] = self.state["equity_history"][-1000:]
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
        if proposal.get("account_id") is not None and proposal["account_id"] != account.get("account_id"):
            raise ValueError("Order belongs to a different account; create a fresh decision for this account")
        equity, cash = account["equity"], account["cash"]
        if proposal.get("source") == "bitfinex" and not proposal["symbol"].endswith("/USDT"):
            raise ValueError("Bitfinex live trading requires a USDT-quoted pair")
        peak = self.state.get("live_peak_equity", equity)
        drawdown = 1 - equity / peak if peak else 0
        if drawdown >= risk["max_drawdown"]:
            self.state["risk_status"] = "entries_blocked"
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
        broker_active = [o for o in broker_orders if not o.get("protective_owned") and o.get("status") not in ("filled", "canceled", "cancelled", "expired", "rejected")]
        if any(canonical(o.get("symbol", "")) == canonical(proposal["symbol"]) for o in broker_active):
            raise ValueError("An outstanding broker order exists for this asset; reconcile it first")
        position = next((p for s, p in account["positions"].items() if canonical(s) == canonical(proposal["symbol"])), None)
        notional = proposal["qty"] * price
        fee_rate = max(risk["fee_rate"], .004) if proposal.get("source") == "bitfinex" else risk["fee_rate"]
        if proposal.get("source") == "bitfinex" and proposal["side"] == "BUY":
            notional *= 1 + risk["price_bound"]
        if proposal["side"] == "BUY":
            daily=self.state["daily"].get(self.state.get("daily_key"),{})
            start=daily.get("initial_equity",equity)+(account.get("cash_flow",0)-daily.get("cash_flow",0))
            if start>0 and 1-equity/start>=risk["max_daily_loss"]:
                raise ValueError("Daily loss limit reached; new entries blocked")
            if daily.get("orders",0)>=risk["max_daily_trades"]:
                raise ValueError("Daily order count limit reached; new entries blocked")
            if daily.get("turnover",0)+notional>max(start,0)*risk["max_daily_turnover"]:
                raise ValueError("Daily turnover limit reached; new entries blocked")
            reserved = sum(p["qty"] * p.get("confirmation_price", p["price"]) *
                           ((1 + p.get("price_bound", risk["price_bound"])) * (1 + max(.004, risk["fee_rate"]))
                            if p.get("source") == "bitfinex" else 1 + risk["fee_rate"])
                           for p in other if p["side"] == "BUY")
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
            cost = notional * (1 + fee_rate)
            if cost > account.get("available_cash", cash) + 1e-8:
                raise ValueError("Insufficient available exchange cash including execution and fee buffer")
            if cost + reserved > cash + 1e-8:
                raise ValueError("Insufficient live cash after outstanding order reservations")
            if held_value + notional > equity * risk["max_position"] + 1e-8:
                raise ValueError("Live per-position risk limit exceeded at confirmation price")
            if exposure + notional + reserved > equity * risk["max_exposure"] + 1e-8:
                raise ValueError("Live portfolio exposure limit exceeded at confirmation price")
        elif not position or proposal["qty"] > min(position["qty"], position.get("available_qty", position["qty"])) + 1e-9:
            raise ValueError("Live position changed; cannot confirm this sell quantity")

    def propose_order(self, symbol: str, side: str, qty: float, price: float,
                      event_id: str | None = None, source: str = "bitfinex", timestamp=None) -> dict:
        """Create a reviewed-order draft only. Does not claim or submit the order."""
        symbol, side = str(symbol).strip().upper(), str(side).upper()
        if not symbol or len(symbol) > 40 or side not in ("BUY", "SELL"):
            raise ValueError("A symbol and BUY or SELL side are required")
        if source != "bitfinex":
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
                        "account_id":(self.state.get("live_account") or {}).get("account_id"), "mode": "live", "reason": "Manual order draft; confirmation required"}
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
            proposal["price_bound"] = self.state["global_risk"]["price_bound"]
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
            if proposal["status"] == status or (proposal["status"] in {"filled","cancelled","rejected","expired"} and status == "submitted"):
                return deepcopy(proposal)
            if proposal["status"] not in ("awaiting_review", "submitting", "submitted", "unknown", "reconcile_required"):
                raise ValueError("Proposal already resolved")
            if status in {"submitted","filled","cancelled","rejected"} and (result or {}).get("id") and not proposal.get("daily_counted"):
                daily=self.state["daily"].get(self.state.get("daily_key"))
                if daily is not None:
                    daily["orders"]+=1
                    daily["turnover"]+=proposal["qty"]*proposal.get("confirmation_price",proposal["price"])
                proposal["daily_counted"]=True
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
            if self.state["mode"]!="paper":
                raise ValueError("Only the simulated paper portfolio can be reset")
            old=self.state
            fresh=self._fresh(old["initial_cash"])
            paper_fields={"cash","initial_cash","peak_equity","positions","orders","signals","prices","running","mode","realized_pnl","fees_paid","last_completed","paper_equity_history"}
            for key,value in old.items():
                if key not in paper_fields:
                    fresh[key]=value
            self.state=fresh
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
             source: str = "fixture", interval_id: str | int | None = None, max_order_notional: float | None = None) -> dict:
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
                    elif self.state["running"] and strategy.get("take_profit") and price >= position["entry_price"] * (1 + strategy["take_profit"]):
                        stop_reason = "Take-profit target triggered"
                equity = self._equity()
                peak_key = "live_peak_equity" if self.state["mode"] == "live" else "peak_equity"
                self.state[peak_key] = max(self.state.get(peak_key, equity), equity)
                drawdown = 1 - equity / self.state[peak_key] if self.state[peak_key] else 0
                risk_block = drawdown >= self.state["global_risk"]["max_drawdown"]
                self.state["risk_status"] = "entries_blocked" if risk_block else "normal"
                if risk_block and self.state["mode"] == "paper":
                    self.state["running"] = False
                if stop_reason:
                    signal.update(action="SELL", reason=stop_reason)
                elif position and self.state["exit_intents"].get(source+":"+symbol):
                    signal.update(action="SELL", reason=self.state["exit_intents"][source+":"+symbol]["reason"])
                elif risk_block and not position:
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
                            fee = max(.004,risk["fee_rate"]) if source == "bitfinex" else risk["fee_rate"]
                            bound = risk["price_bound"] if source == "bitfinex" else 0
                            outstanding = [p for p in self.state["proposals"] if p["status"] in {"awaiting_review","submitting","submitted","unknown","reconcile_required"} and p["side"] == "BUY"]
                            reserved = sum(p["qty"]*p.get("confirmation_price",p["price"])*(1+p.get("price_bound",bound))*(1+fee) for p in outstanding)
                            funds = max(0,min(live_account.get("available_cash",cash),cash-reserved))
                            exposure = sum(p["qty"]*p["price"] for p in positions.values())
                            budget = min(equity*strategy["allocation"], funds/(1+fee), max(0,equity*risk["max_position"]), max(0,equity*risk["max_exposure"]-exposure-reserved))
                            qty = budget / (price*(1+bound))
                    if self.state["mode"] == "live" and signal["action"] == "SELL" and position:
                        qty = min(qty,position.get("available_qty",qty))
                        self.state["exit_intents"].setdefault(source+":"+symbol,{"symbol":symbol,"provider":source,"reason":signal["reason"],"created_at":now_iso()})
                    if self.state["mode"] == "live" and source == "bitfinex":
                        from decimal import Decimal, ROUND_DOWN
                        if max_order_notional is not None:
                            qty = min(qty, max_order_notional / (price * (1+self.state["global_risk"]["price_bound"]) * (1+max(.004,self.state["global_risk"]["fee_rate"]))))
                        qty = float(Decimal(str(qty)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN))
                    if self.state["mode"] == "live":
                        # Live account execution and balances belong to the broker adapter.
                        proposal = {"id": event_id, "symbol": symbol, "side": signal["action"], "qty": qty,
                                    "price": price, "status": "awaiting_review", "reason": signal["reason"], "origin":"strategy",
                                    "timestamp": stamp.isoformat(), "source": source, "mode": "live", "account_id":live_account.get("account_id")}
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

    def remember_connection(self, provider, metadata):
        with self.lock:
            self.state["connections"][provider] = deepcopy(metadata)
            self._save("connection.saved", {"provider":provider})

    def forget_connection(self, provider):
        with self.lock:
            self.state["connections"].pop(provider,None)
            self._save("connection.removed", {"provider":provider})

    def set_watchlist(self, symbols):
        from .providers import validate_symbol
        if not isinstance(symbols,list) or len(symbols)>100:
            raise ValueError("Watchlist must contain at most 100 symbols")
        cleaned=list(dict.fromkeys(validate_symbol(str(s).upper()) for s in symbols))
        with self.lock:
            self.state["watchlist"]=cleaned
            self._save("watchlist.updated", {"symbols":cleaned})
        return cleaned

    def close(self) -> None:
        self.store.close()
