"""Loopback-only authenticated API for the single-user desktop client."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import math
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from .core import Engine
from .fixtures import generate_candles
from .backtest import run_backtest
from .models import ModelRegistry, ModelError
from .providers import OrderNotSubmitted, ExchangeRejected, ProviderError, get_credentials, provider_factory, secure_credentials, delete_credentials, parse_interval


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class StrategyPayload(Payload):
    symbol: str
    interval: str = "1h"
    threshold: float = Field(.65,ge=0,le=1)
    exit_threshold: float | None = Field(None,ge=0,le=1)
    fixed_stop: float = Field(.03,ge=0,lt=1)
    trailing_stop: float = Field(.05,ge=0,lt=1)
    take_profit: float = Field(0,ge=0,le=10)
    allocation: float = Field(.1,gt=0,le=1)
    model_id: str = "mock-momentum"
    enabled: bool = True


class OrderPayload(Payload):
    symbol: str
    side: str
    qty: float = Field(gt=0)
    price: float = Field(gt=0)
    id: str | None = None


class ModelPayload(Payload):
    id: str | None = None
    name: str
    endpoint: str
    version: str = "unverified"
    timeout: float = Field(5,ge=.1,le=60)
    api_key: str | None = None
    approved_version: str | None = None


class ProposalPayload(OrderPayload):
    provider: str | None = None


class ConnectionPayload(Payload):
    provider: str
    key: str
    secret: str
    paper: bool = True


class BacktestPayload(Payload):
    symbol: str = "BTC/USD"
    interval: str = "1h"
    threshold: float = Field(.65,ge=0,le=1)
    exit_threshold: float | None = Field(None,ge=0,le=1)
    fixed_stop: float = Field(.03,ge=0,lt=1)
    trailing_stop: float = Field(.05,ge=0,lt=1)
    allocation: float = Field(.1,gt=0,le=1)
    initial_cash: float = Field(100000,gt=0)
    fee_rate: float = Field(.001,ge=0,lt=.1)
    slippage: float = Field(.0005,ge=0,lt=.1)
    source: str = "fixture"
    model_id: str = "mock-momentum"
    start: str | None = None
    end: str | None = None


def _create_workspace(data_dir=None, token=None, *, tick_seconds=2, production=False, fixed_mode=None):
    if token is None or len(token) < 32:
        raise ValueError("A random session token of at least 32 characters is required")
    directory = Path(data_dir or Path.home() / ".axiom")
    directory.mkdir(parents=True,exist_ok=True)
    core = Engine(directory)
    registry = ModelRegistry(directory)
    from .jobs import BacktestJobs
    replay_jobs=BacktestJobs(core.store,registry)
    adapters = {}
    connection_meta = {p:{**m,"status":"saved"} for p,m in core.state.get("connections",{}).items() if p == "bitfinex" and fixed_mode != "paper"}
    last_account_sync = {}
    state = {"source":core.store.record("preferences","source") or "fixture", "last_error":None, "last_tick":None, "replay_cursor":60}
    if state["source"] not in {"fixture", "bitfinex"}:
        state["source"] = "bitfinex" if production else "fixture"
    if production:
        if state["source"]=="fixture":
            state["source"]="bitfinex"
        core.set_mode(fixed_mode or "live")
        # Migrate the untouched starter strategy without changing user strategies.
        from .core import default_strategy
        if {k:{**default_strategy(k), **v} for k,v in core.state["strategies"].items()} == {"BTC/USD": default_strategy()}:
            core.state["strategies"] = {"BTC/USDT": {**default_strategy("BTC/USDT"), "model_id":"builtin-momentum"}}
            core._save("strategy.starter_migrated", {})
    if fixed_mode:
        core.set_mode(fixed_mode)
    lock = asyncio.Lock()
    execution_lock = asyncio.Lock()
    execution = {"mode":"manual", "provider":None, "max_order_notional":0, "generation":0,"account_id":None}

    async def public_adapter():
        if "bitfinex" not in adapters:
            adapters["bitfinex"] = provider_factory("bitfinex", {}, paper=False)
        return adapters["bitfinex"]

    def disarm():
        was_active = execution["mode"] == "autonomous"
        execution.update(mode="manual", provider=None, account_id=None,max_order_notional=0, generation=execution["generation"]+1)
        if was_active:
            core.state["running"] = False
            try:
                core.set_running(False)
            except Exception:
                pass  # In-memory authority is already revoked even on disk failure.

    protection_lock=asyncio.Lock()

    async def release_protection(provider, symbol):
        key=provider+":"+symbol
        async with protection_lock:
            record=core.state["protection"].get(key)
            if not record or record.get("status") in {"cancelled","filled","rejected"}:
                return
            adapter=adapters[provider]
            if not record.get("id"):
                order=await adapter.get_order(record["client_id"])
                record.update(id=order["id"],status=order["status"])
            if record["status"] not in {"filled","cancelled","canceled","rejected"}:
                await adapter.cancel_order(record["id"])
                order=await adapter.get_order_by_id(record["id"])
                if order["status"] not in {"filled","canceled","rejected"}:
                    raise ProviderError("Protective cancellation is not final; exit waits for reconciliation")
                record["status"]="filled" if order["status"]=="filled" else "cancelled"
                core._save("protection.released",record)

    async def protect_positions(provider):
        adapter=adapters.get(provider)
        if execution["mode"]!="autonomous" or not hasattr(adapter,"place_protection"):
            return
        generation=execution["generation"]
        def guard():
            if execution["generation"]!=generation or execution["mode"]!="autonomous":
                raise OrderNotSubmitted("Protective placement activation was revoked")
        async with protection_lock:
            snapshot=core.snapshot()
            configs={s["symbol"]:s for s in snapshot["strategies"]}
            for position in snapshot["positions"]:
                symbol=position["symbol"]
                strategy=configs.get(symbol)
                if not strategy:
                    from .core import default_strategy
                    strategy=default_strategy(symbol)
                stops=[]
                if strategy.get("fixed_stop"):
                    stops.append(position["entry_price"]*(1-strategy["fixed_stop"]))
                if strategy.get("trailing_stop"):
                    stops.append(position["high_watermark"]*(1-strategy["trailing_stop"]))
                if not stops:
                    continue
                key=provider+":"+symbol
                record=core.state["protection"].get(key)
                if record and record.get("status") not in {"cancelled","filled","rejected"}:
                    target=max(max(stops),record.get("stop_price",0))
                    amend=abs(record.get("qty",0)-position["qty"])>1e-8 or target>record.get("stop_price",0)*1.001
                    if not amend and time.time()-record.get("verified_at",0)<10:
                        continue
                    order=await (adapter.get_order_by_id(record["id"]) if record.get("id") else adapter.get_order(record["client_id"]))
                    record.update(id=order["id"],status=order["status"])
                    if order["status"] not in {"filled","canceled","rejected"}:
                        if amend and hasattr(adapter,"update_protection"):
                            record["status"]="updating"
                            core._save("protection.amending",record)
                            order=await adapter.update_protection(record["id"],position["qty"],target,guard)
                            record.update(status=order["status"],qty=position["qty"],stop_price=target)
                        record["verified_at"]=time.time()
                        core._save("protection.verified",record)
                        continue
                    record["status"]="filled" if order["status"]=="filled" else "cancelled"
                    core._save("protection.terminal",record)
                    if order["status"]=="filled":
                        last_account_sync[provider]=0
                        continue
                guard()
                client_id="protect-"+str(uuid.uuid4())
                record={"client_id":client_id,"symbol":symbol,"provider":provider,"qty":position["qty"],"stop_price":max(stops),"status":"submitting"}
                core.state["protection"][key]=record
                core._save("protection.claimed",record)
                try:
                    result=await adapter.place_protection(symbol,position["qty"],max(stops),client_id,guard)
                    record.update(id=result["id"],status=result["status"],verified_at=time.time())
                    core._save("protection.accepted",record)
                    last_account_sync[provider]=0
                except (OrderNotSubmitted,ExchangeRejected):
                    record["status"]="rejected"
                    core._save("protection.rejected",record)
                    raise

    async def execute_proposal(proposal_id, provider, generation=None):
        if (directory/"pending-restore.json").exists():
            raise ProviderError("Restart to finish the pending restore")
        automatic = generation is not None
        quote = None
        def check_session():
            if automatic and (execution["generation"] != generation or execution["mode"] != "autonomous" or
                              execution["provider"] != provider or (core.snapshot().get("live_account") or {}).get("account_id")!=execution["account_id"] or not core.snapshot()["running"] or core.snapshot()["mode"] != "live"):
                raise ProviderError("Autonomous execution is no longer active")
            if automatic and quote is not None and not -5 <= time.time()-quote.timestamp <= core.snapshot()["global_risk"]["stale_after"]:
                raise ProviderError("Execution quote expired before submission")
        async with execution_lock:
            try:
                check_session()
                proposal = next(p for p in core.snapshot()["proposals"] if p["id"] == proposal_id)
                if automatic and proposal.get("origin") != "strategy":
                    raise ProviderError("Only new strategy decisions can execute autonomously")
                await require_market_session(provider, proposal["symbol"])
                if hasattr(adapters[provider],"place_protection") and (proposal["side"]=="SELL" or not any(p["symbol"]==proposal["symbol"] for p in core.snapshot()["positions"])):
                    await release_protection(provider,proposal["symbol"])
                await reconcile(provider)
                quote = await adapters[provider].quote(proposal["symbol"])
                if quote.stale or not -5 <= time.time()-quote.timestamp <= core.snapshot()["global_risk"]["stale_after"]:
                    raise ProviderError("Fresh market data is required before execution")
                check_session()
                if automatic:
                    risk = core.snapshot()["global_risk"]
                    cost = proposal["qty"] * quote.price * (1+risk["price_bound"]) * (1+max(.004,risk["fee_rate"]))
                    if cost > execution["max_order_notional"] + 1e-8:
                        raise ProviderError("Order exceeds the autonomous session order budget")
                claimed = core.claim_proposal(proposal_id, quote_price=quote.price)
                check_session()
                order = claimed | {"client_order_id":proposal_id, "confirmed":True}
                if automatic:
                    order["_execution_guard"] = check_session
                result = await adapters[provider].submit_order(order)
                status = {"filled":"filled", "canceled":"cancelled", "rejected":"rejected"}.get(result.get("status"), "submitted")
                core.finish_proposal(proposal_id,status,result)
                last_account_sync[provider] = 0
                if automatic and status in {"filled","cancelled"}:
                    await reconcile(provider)
                    await protect_positions(provider)
                return {"proposal_id":proposal_id,"status":status,"order":result}
            except (ValueError,ProviderError) as exc:
                current = next((p for p in core.snapshot()["proposals"] if p["id"] == proposal_id),{})
                if current.get("status") == "submitting":
                    core.finish_proposal(proposal_id,"rejected" if isinstance(exc,(OrderNotSubmitted,ExchangeRejected)) else "reconcile_required",{"message":str(exc) if isinstance(exc,(OrderNotSubmitted,ExchangeRejected)) else "Verify exchange result before retrying"})
                if automatic:
                    if current.get("status")=="awaiting_review":
                        core.finish_proposal(proposal_id,"rejected",{"message":str(exc)})
                    disarm()
                    core.set_running(False)
                raise

    async def process_tick(*args, **kwargs):
        generation = execution["generation"] if execution["mode"] == "autonomous" else None
        async with lock:
            signal = core.tick(*args, **kwargs, max_order_notional=execution["max_order_notional"] if generation is not None else None)
        if generation is not None and signal.get("proposal"):
            await execute_proposal(signal["proposal"]["id"], kwargs["source"], generation)
        return signal
    last_buckets = {}
    quote_cache = {}
    stream_tasks = {}
    account_tasks = {}
    pending_quotes = {}
    price_event = asyncio.Event()
    history = None
    try:
        from .history import HistoryStore
        history = HistoryStore(directory / "history")
    except ImportError:
        state["history_status"] = "DuckDB unavailable; install requirements.txt for durable history"

    async def reconcile(provider):
        account = await adapters[provider].account()
        connection_meta[provider]["account"] = account
        core.remember_connection(provider, {**connection_meta[provider],"status":"saved"})
        if account.get("reconciliation_complete") is False or account.get("equity") is None:
            raise ProviderError("This provider cannot establish complete equity and cost basis; live proposals are blocked. " + "; ".join(account.get("reconciliation_errors", [])))
        owned={str(v.get("id")) for k,v in core.state["protection"].items() if k.startswith(provider+":") and v.get("id")}
        for order in account.get("orders",[]) if isinstance(account.get("orders",[]),list) else []:
            if str(order.get("id")) in owned:
                order["protective_owned"]=True
                for position in account.get("positions",[]):
                    if position["symbol"]==order["symbol"]:
                        position["available_qty"]=min(float(position["qty"]),float(position.get("available_qty",0))+max(0,float(order.get("qty",0))-float(order.get("filled_qty",0))))
        core.reconcile_live_account(account,account.get("positions",[]))
        # Query by durable client ID, including ambiguous submissions recovered after restart.
        if hasattr(adapters[provider],"get_order"):
            for proposal in core.snapshot().get("proposals",[]):
                if proposal.get("source") != provider or proposal.get("status") not in {"submitting","submitted","unknown","reconcile_required"}:
                    continue
                try:
                    if hasattr(adapters[provider],"get_order_by_id") and proposal.get("result",{}).get("id"):
                        broker_order=await adapters[provider].get_order_by_id(proposal["result"]["id"])
                    else:
                        broker_order = await adapters[provider].get_order(proposal["id"])
                except ProviderError:
                    continue  # Preserve the blocker until the broker can establish a result.
                status = broker_order.get("status")
                resolved = {"filled":"filled","canceled":"cancelled","cancelled":"cancelled","expired":"expired","rejected":"rejected"}.get(status)
                if resolved:
                    core.finish_proposal(proposal["id"],resolved,broker_order)
        changed=False
        if provider=="bitfinex":
            fills=account.get("fills",[])
            for proposal in core.state["proposals"]:
                result=proposal.get("result",{})
                if proposal.get("source")!=provider or not result.get("id"):
                    continue
                trades=[r for r in fills if str(r[3])==str(result["id"])]
                if not trades:
                    continue
                qty=sum(abs(float(r[4])) for r in trades)
                notional=sum(abs(float(r[4]))*float(r[5]) for r in trades)
                fees={}
                fee_quote=0.0
                for row in trades:
                    currency="USDT" if row[10]=="UST" else row[10]
                    fees[currency]=fees.get(currency,0)-float(row[9])
                    if currency=="USDT":
                        fee_quote-=float(row[9])
                    elif currency==proposal["symbol"].split("/")[0]:
                        fee_quote-=float(row[9])*float(row[5])
                result.update(filled_qty=qty,avg_price=notional/qty if qty else 0,fees_by_currency=fees,fee=fee_quote)
                changed=True
        if changed:
            core._save("execution.fills_reconciled",{"provider":provider})
        last_account_sync[provider] = time.monotonic()
        return account

    async def stream_prices(source,symbols):
        try:
            async for quote in adapters[source].stream(symbols):
                quote_cache[(source,quote.symbol)] = quote
                # Pausing model decisions preserves independent stop monitoring.
                if state["source"] != source:
                    continue
                monitored={s["symbol"] for s in core.snapshot()["strategies"] if s.get("enabled",True)}|{p["symbol"] for p in core.snapshot()["positions"]}
                if quote.symbol not in monitored:
                    continue
                if quote.stale:
                    state["last_error"] = "Market stream is stale; trading paused until provider recovers"
                    disarm()
                    core.set_running(False)
                    continue
                pending_quotes[(source,quote.symbol)]=quote
                price_event.set()
                state["last_tick"] = time.time()
        except asyncio.CancelledError:
            raise
        except Exception:
            disarm()
            state["last_error"] = "Market stream failed; trading paused"
            core.set_running(False)

    async def price_worker():
        while True:
            await price_event.wait()
            price_event.clear()
            batch=list(pending_quotes.items())
            pending_quotes.clear()
            for (source,symbol),quote in batch:
                if state["source"]!=source:
                    continue
                try:
                    await process_tick(symbol,quote.price,None,timestamp=quote.timestamp,event_id=str(uuid.uuid4()),completed=False,source=source)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    disarm()
                    state["last_error"]=str(exc) if isinstance(exc,(ValueError,ProviderError)) else "Price monitoring failed; trading stopped"
            await asyncio.sleep(.1)

    async def ensure_account_events(provider):
        if provider in account_tasks:
            account_tasks[provider].cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await account_tasks.pop(provider)
        adapter=adapters.get(provider)
        if not adapter or not hasattr(adapter,"account_events") or getattr(adapter,"_owns_client",True) is False:
            return
        async def monitor():
            async for event in adapter.account_events():
                last_account_sync[provider]=0
                connection_meta[provider]["account_stream"]=event["type"]
                if event["type"]=="disconnected":
                    core.store.record("alerts",str(time.time_ns()),{"time":time.time(),"message":"Account stream disconnected; REST reconciliation remains required","provider":provider},write=True)
                order=event.get("order")
                if order:
                    for proposal in core.snapshot()["proposals"]:
                        if proposal["source"]!=provider or proposal["status"] not in {"submitting","submitted","unknown","reconcile_required"}:
                            continue
                        if str(adapter._cid(proposal["id"]))==str(order["client_order_id"]):
                            terminal={"filled":"filled","canceled":"cancelled","rejected":"rejected"}.get(order["status"])
                            if terminal:
                                core.finish_proposal(proposal["id"],terminal,order)
        account_tasks[provider]=asyncio.create_task(monitor())

    async def ensure_stream(source):
        old = stream_tasks.pop(source,None)
        if old:
            old.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await old
        snap = core.snapshot()
        symbols = sorted({s["symbol"] for s in snap["strategies"]} | {p["symbol"] for p in snap["positions"]})
        if source=="bitfinex":
            symbols=[s for s in symbols if s.endswith("/USDT")]
            symbols+= [s for s in snap["watchlist"] if s.endswith("/USDT") and s not in symbols][:max(0,25-len(symbols))]
        if source == "bitfinex":
            symbols = [s for s in symbols if s.endswith("/USDT")]
        if source in adapters and symbols:
            stream_tasks[source] = asyncio.create_task(stream_prices(source,symbols))

    async def authorize(authorization: str | None = Header(default=None)):
        expected = "Bearer " + token
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(401,"Invalid desktop session")

    async def protected_error(action):
        try:
            return await action
        except (ValueError, ModelError, ProviderError) as exc:
            raise HTTPException(400, str(exc)) from exc

    async def require_market_session(provider, symbol):
        if provider != "bitfinex":
            raise ProviderError("Only Bitfinex is supported")

    async def scheduler():
        while True:
            try:
                await asyncio.sleep(tick_seconds)
                snap = core.snapshot()
                if snap["mode"] == "live" and state["source"] == "bitfinex" and connection_meta.get("bitfinex",{}).get("status") == "connected":
                    if time.monotonic() - last_account_sync.get("bitfinex", 0) >= 15:
                        last_account_sync["bitfinex"] = time.monotonic()
                        await reconcile("bitfinex")
                        snap = core.snapshot()
                if not snap["running"]:
                    continue
                if snap["mode"] == "live" and time.monotonic() - last_account_sync.get(state["source"], 0) >= 10:
                    await reconcile(state["source"])
                held = {p["symbol"] for p in snap["positions"]}
                strategies = {s["symbol"]:s for s in snap["strategies"]}
                for symbol in held-strategies.keys():
                    from .core import default_strategy
                    strategies[symbol] = {**default_strategy(symbol),"enabled":False}
                for strategy in strategies.values():
                    enabled = strategy.get("enabled",True)
                    if not enabled and strategy["symbol"] not in held:
                        continue
                    symbol = strategy["symbol"]
                    interval = strategy.get("interval","1h")
                    seconds = parse_interval(interval)
                    source = state["source"]
                    if source == "fixture":
                        # Explicit accelerated fixture playback, no live-price claim.
                        candles = generate_candles(symbol,count=600,interval=interval)[:state["replay_cursor"]]
                        price = candles[-1]["close"]
                        completed = enabled
                    else:
                        adapter = adapters.get(source)
                        if not adapter:
                            raise ProviderError("Connect the selected provider before starting")
                        await require_market_session(source, symbol)
                        quote = quote_cache.get((source,symbol)) or await adapter.quote(symbol)
                        price = quote["price"] if isinstance(quote,dict) else quote.price
                        quote_time = quote.get("timestamp",time.time()) if isinstance(quote,dict) else quote.timestamp
                        if getattr(quote,"stale",False) or time.time() - float(quote_time) > core.snapshot().get("global_risk",{}).get("stale_after",90):
                            raise ProviderError("Provider quote is stale; trading is blocked")
                        bucket = int(time.time() // seconds)
                        completed = enabled and last_buckets.get(symbol) != bucket
                        last_buckets[symbol] = bucket
                        candles = await adapter.candles(symbol,interval,200) if completed else []
                        candles = [c for c in candles if c["time"] + seconds <= time.time()]
                        if completed and not candles:
                            raise ProviderError("No completed historical candles are available")
                        if completed and candles[-1]["time"]+seconds < time.time()-seconds*2:
                            raise ProviderError("Historical observations are stale; new decisions blocked")
                        candles = [{**r,"source":source} for r in candles]
                        if candles and history:
                            history.put(symbol,interval,candles)
                    q = None
                    if completed:
                        prediction = await registry.predict(strategy.get("model_id","mock-momentum"),symbol,interval,candles)
                        q = prediction["probability_up"]
                    quote_time = time.time() if source == "fixture" else quote_time
                    await process_tick(symbol,price,q,timestamp=quote_time,event_id=str(uuid.uuid4()),completed=completed,source=source,interval_id=candles[-1]["time"]//seconds if candles and completed else None)
                    state["last_tick"] = time.time()
                    state["last_error"] = None
                if execution["mode"]=="autonomous":
                    async with execution_lock:
                        await protect_positions(state["source"])
                state["replay_cursor"] = min(600,state["replay_cursor"]+1)
                if state["source"] == "fixture" and state["replay_cursor"] == 600:
                    core.set_running(False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Fail closed; restart requires explicit session activation.
                disarm()
                core.set_running(False)
                state["last_error"] = str(exc) if isinstance(exc,(ValueError,ProviderError,ModelError)) else "Engine operation failed; trading paused"
                with contextlib.suppress(Exception):
                    core.store.record("alerts",str(time.time_ns()),{"time":time.time(),"message":state["last_error"],"provider":state["source"]},write=True)

    @asynccontextmanager
    async def lifespan(app):
        # Restart never automatically restores execution permission.
        core.set_running(False)
        async def restore_saved():
            for provider,metadata in list(connection_meta.items()):
                adapter=None
                try:
                    credentials=get_credentials(provider)
                    if credentials:
                        adapter=provider_factory(provider,credentials,paper=metadata.get("paper",True))
                        if hasattr(adapter,"bind_store"):
                            adapter.bind_store(core.store)
                        account=await adapter.account()
                        adapters[provider]=adapter
                        connection_meta[provider]={**metadata,"status":"connected","account":account}
                        if account.get("reconciliation_complete") is not False and account.get("equity") is not None and state["source"]==provider:
                            core.reconcile_live_account(account,account.get("positions",[]))
                        await ensure_account_events(provider)
                except Exception:
                    connection_meta[provider]["status"]="restore_failed"
                    if adapter is not None:
                        if adapters.get(provider) is adapter:
                            adapters.pop(provider,None)
                        await adapter.close()
        from .backup import create_backup
        backups=list((directory/"backups").glob("backup-*.zip"))
        if not backups or time.time()-max(p.stat().st_mtime for p in backups)>86400:
            try:
                create_backup(directory,core,registry,history)
            except Exception:
                state["last_error"]="Automatic backup failed; check disk space and create a verified backup"
        restore_task=asyncio.create_task(restore_saved())
        worker_task=asyncio.create_task(price_worker())
        task = asyncio.create_task(scheduler())
        try:
            async with contextlib.AsyncExitStack() as stack:
                for workspace in getattr(app.state, "workspaces", {}).values():
                    await stack.enter_async_context(workspace.router.lifespan_context(workspace))
                yield
        finally:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
            restore_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await restore_task
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            for task in account_tasks.values():
                task.cancel()
            for task in account_tasks.values():
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for stream_task in stream_tasks.values():
                stream_task.cancel()
            for stream_task in stream_tasks.values():
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task
            for adapter in adapters.values():
                await adapter.close()
            await replay_jobs.close()
            registry.close()
            if history:
                history.close()
            if hasattr(core,"close"):
                core.close()

    app = FastAPI(title="Axiom Local Engine",version="0.2.2",lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(CORSMiddleware,allow_origins=["tauri://localhost","http://tauri.localhost","https://tauri.localhost"],allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",allow_credentials=False,allow_methods=["GET","POST","PUT","DELETE"],allow_headers=["Authorization","Content-Type"])
    app.state.core = core
    app.state.registry = registry
    app.state.adapters = adapters

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Also validates Host to mitigate DNS rebinding against the loopback API.
        host = request.headers.get("host","").split(":")[0]
        if host not in {"localhost","127.0.0.1","testserver"}:
            from starlette.responses import JSONResponse
            return JSONResponse({"detail":"Loopback access only"},status_code=403)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status":"ok","version":"0.2.2","local":True,"data_source":state["source"]}

    @app.get("/snapshot",dependencies=[Depends(authorize)])
    async def snapshot():
        result = core.snapshot()
        meta = connection_meta.get("bitfinex", {})
        account = meta.get("account", {}) if meta.get("status") == "connected" else {}
        ready = bool(account and account.get("reconciliation_complete") is not False and account.get("equity") is not None and result.get("live_account") and time.time()-float(account.get("synced_at",time.time())) <= core.state["global_risk"]["stale_after"])
        if result["mode"] == "live" and not ready:
            result.update(cash=None, equity=None, pnl=None, initial_cash=None, positions=[],
                          broker_orders=account.get("orders", []), live_account=None)
        result["account_ready"] = ready if result["mode"] == "live" else True
        result["currency"] = "USDT" if production or result["mode"] == "live" else result["currency"]
        return result | {"production":production,"models":registry.list(),"connections":list(connection_meta.values()),"data_source":state["source"],"system_health":state.copy(),"execution":execution.copy(),"backups":[{"name":p.name,"created":p.stat().st_mtime} for p in sorted((directory/"backups").glob("backup-*.zip"),reverse=True)][:50],"restore_pending":(directory/"pending-restore.json").exists(),"predictions":registry.history(50),"backtests":(await replay_list())["jobs"],"alerts":(await alerts())["alerts"]}

    @app.get("/candles",dependencies=[Depends(authorize)])
    async def candles(symbol: str="BTC/USD",interval: str="1h",source: str="fixture",limit: int=180):
        try:
            if production and source=="fixture":
                raise ProviderError("Select an online market-data provider")
            parse_interval(interval)
            if not 2 <= limit <= 2000:
                raise ValueError("Candle limit must be between 2 and 2000")
            if source == "fixture":
                rows = generate_candles(symbol,count=limit,interval=interval)
            elif source=="bitfinex":
                public=await public_adapter()
                rows=[{**r,"source":source} for r in await public.candles(symbol,interval,limit)]
            elif source in adapters:
                rows = await adapters[source].candles(symbol,interval,limit)
                rows = [{**r,"source":source} for r in rows]
            else:
                raise ProviderError("Connect a provider to request real market data")
            if history and source != "fixture":
                history.put(symbol,interval,rows)
            return {"symbol":symbol,"interval":interval,"source":source,"candles":rows}
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/instruments",dependencies=[Depends(authorize)])
    async def instruments(provider: str="fixture"):
        if production and provider=="fixture":
            raise HTTPException(400,"Select an online provider")
        if provider=="bitfinex":
            public=await public_adapter()
            return {"source":provider,"instruments":await protected_error(public.fetch_assets())}
        if provider == "fixture":
            return {"source":"fixture","instruments":[{"symbol":s,"name":n,"class":c} for s,n,c in [("BTC/USD","Bitcoin","crypto"),("ETH/USD","Ethereum","crypto"),("SOL/USD","Solana","crypto"),("AAPL","Apple Inc.","stock"),("NVDA","NVIDIA","stock"),("MSFT","Microsoft","stock"),("SPY","S&P 500 ETF","stock")]]}
        if provider not in adapters:
            raise HTTPException(400,"Provider is not connected")
        return {"source":provider,"instruments":await protected_error(adapters[provider].fetch_assets())}

    @app.get("/history",dependencies=[Depends(authorize)])
    async def historical_cache(symbol: str="BTC/USD",interval: str="1h",provider: str | None=None):
        if not history:
            raise HTTPException(503,"DuckDB history dependency is unavailable")
        return {"symbol":symbol,"interval":interval,"candles":history.read(symbol,interval,provider=provider),"source":"cached provider data"}

    @app.post("/history/export",dependencies=[Depends(authorize)])
    async def export_history():
        if not history:
            raise HTTPException(503,"DuckDB history dependency is unavailable")
        path = directory / "history" / f"candles-{int(time.time())}.parquet"
        history.export_parquet(path)
        return {"path":str(path),"format":"parquet","source":"cached provider data"}

    @app.put("/strategy",dependencies=[Depends(authorize)])
    async def strategy(payload: StrategyPayload):
        disarm()
        try:
            parse_interval(payload.interval)
            registry.get(payload.model_id)
            async with lock:
                result = core.configure_strategy(payload.model_dump(exclude_none=True))
            if state["source"] in adapters and core.snapshot()["running"]:
                await ensure_stream(state["source"])
            return result
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.put("/strategies/select", dependencies=[Depends(authorize)])
    async def select_strategies(payload: dict):
        symbols = payload.get("symbols")
        if not isinstance(symbols, list) or any(not isinstance(s,str) or s not in core.state["strategies"] for s in symbols):
            raise HTTPException(400, "Select existing strategies")
        async with lock:
            disarm()
            core.set_running(False)
            for symbol, config in core.state["strategies"].items():
                config["enabled"] = symbol in symbols
            core._save("strategies.selected", {"symbols":symbols})
        return {"symbols":symbols, "running":False}

    @app.put("/settings",dependencies=[Depends(authorize)])
    async def settings(payload: dict):
        if set(payload) != {"risk"} or not isinstance(payload["risk"],dict):
            raise HTTPException(400,"Expected {risk:{...}}")
        if set(payload["risk"]) - {"max_position","max_exposure","max_drawdown","stale_after","fee_rate","slippage","price_bound","max_daily_loss","max_daily_trades","max_daily_turnover"}:
            raise HTTPException(400,"Unknown risk setting")
        try:
            async with lock:
                disarm()
                return core.configure_risk(payload["risk"])
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/mode",dependencies=[Depends(authorize)])
    async def mode(payload: dict):
        if fixed_mode and payload.get("mode") != fixed_mode:
            raise HTTPException(400, "Select the other workspace to view its independent trading mode")
        disarm()
        try:
            async with lock:
                if fixed_mode and payload.get("mode") != fixed_mode:
                    raise ValueError("Select the other workspace to view its independent trading mode")
                return core.set_mode(payload.get("mode"))
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.put("/settings/risk",dependencies=[Depends(authorize)])
    async def risk_settings(payload: dict):
        mapped = {( "max_position" if k == "max_allocation" else k):v for k,v in payload.items()}
        return await settings({"risk":mapped})

    @app.post("/reset",dependencies=[Depends(authorize)])
    async def reset():
        if core.snapshot()["mode"] != "paper":
            raise HTTPException(400,"Only the virtual paper portfolio can be reset")
        state["replay_cursor"] = 60
        state["source"] = "bitfinex" if production else "fixture"
        return core.reset()

    @app.post("/engine/start",dependencies=[Depends(authorize)])
    async def start(payload: dict | None=None):
        if (directory/"pending-restore.json").exists():
            raise HTTPException(409,"Restart to finish the pending restore before trading")
        payload = payload or {}
        source = payload.get("source",state["source"] if production else "fixture")
        if production and source=="fixture":
            raise HTTPException(400,"Offline fixture execution is disabled in the production desktop")
        autonomous = payload.get("execution") == "autonomous"
        if autonomous:
            monitored={s["symbol"] for s in core.snapshot()["strategies"] if s.get("enabled",True)}|{p["symbol"] for p in core.snapshot()["positions"]}
            if len(monitored)>25:
                raise HTTPException(400,"Limit an autonomous session to 25 monitored instruments")
            cap = payload.get("max_order_notional")
            if source != "bitfinex" or core.snapshot()["mode"] != "live" or payload.get("confirm_autonomous") is not True:
                raise HTTPException(400,"Autonomous execution requires live Bitfinex and explicit session activation")
            if isinstance(cap,bool) or not isinstance(cap,(int,float)) or not math.isfinite(cap) or cap <= 0:
                raise HTTPException(400,"Set a positive maximum order budget in USDT")
            selected = [s for s in core.snapshot()["strategies"] if s.get("enabled",True)]
            if not selected:
                raise HTTPException(400,"Enable a USDT strategy first")
            for strategy in selected:
                if registry.get(strategy["model_id"])["endpoint"].startswith("mock:"):
                    raise HTTPException(400,"Autonomous live strategies require a real model endpoint; mock models are disabled")
            if any(p["status"] in {"awaiting_review","submitting","submitted","unknown","reconcile_required"} for p in core.snapshot()["proposals"]):
                raise HTTPException(400,"Reconcile outstanding submissions before activating autonomy")
        disarm()
        start_generation = execution["generation"]
        if source not in {"fixture","bitfinex"}:
            raise HTTPException(400,"Unknown market source")
        if source == "bitfinex" and core.snapshot()["mode"] == "paper":
            await public_adapter()
        if source != "fixture" and source not in adapters:
            raise HTTPException(400,"Connect the selected market provider first")
        if source == "fixture" and core.snapshot()["mode"] == "live":
            raise HTTPException(400,"Live proposals require a real market provider")
        if source == "bitfinex" and any(s.get("enabled", True) and not s["symbol"].endswith("/USDT") for s in core.snapshot()["strategies"]):
            raise HTTPException(400,"Disable non-USDT strategies before starting the Bitfinex engine; create a strategy for a USDT spot pair")
        if core.snapshot()["mode"] == "live":
            try:
                await reconcile(source)
            except (ProviderError,ValueError) as exc:
                raise HTTPException(400,str(exc)) from exc
        if execution["generation"] != start_generation:
            raise HTTPException(409,"Engine activation cancelled by another control action")
        state["source"] = source
        core.store.record("preferences","source",source,write=True)
        state["last_error"] = None
        if autonomous:
            execution.update(mode="autonomous",provider=source,max_order_notional=float(cap),account_id=(core.snapshot().get("live_account") or {}).get("account_id"))
        core.set_running(True)
        if autonomous:
            try:
                async with execution_lock:
                    await protect_positions(source)
            except ProviderError as exc:
                disarm()
                raise HTTPException(400,str(exc)) from exc
        if source in adapters:
            await ensure_stream(source)
        return {"running":True,"source":source}

    @app.post("/engine/pause",dependencies=[Depends(authorize)])
    async def pause():
        disarm()
        core.set_running(False)
        return {"running":False}

    @app.post("/orders",dependencies=[Depends(authorize)])
    async def order(payload: OrderPayload):
        if (directory/"pending-restore.json").exists():
            raise HTTPException(409,"Restart to finish the pending restore")
        if core.snapshot()["mode"] != "paper":
            raise HTTPException(400,"Real orders require a proposal and separate confirmation")
        try:
            async with lock:
                reference = payload.price
                if state["source"] == "bitfinex":
                    adapter = await public_adapter()
                    quote = await adapter.quote(payload.symbol)
                    if quote.stale or time.time() - quote.timestamp > core.state["global_risk"]["stale_after"] or quote.timestamp > time.time()+5:
                        raise ProviderError("Fresh Bitfinex quote required for a paper fill")
                    reference = quote.price
                    core.state["prices"][payload.symbol] = {"price":reference,"timestamp":time.time(),"source":"bitfinex"}
                result = core.paper_order(payload.symbol,payload.side,payload.qty,reference,payload.id)
            if state["source"] in adapters:
                await ensure_stream(state["source"])
            return result
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/backtest",dependencies=[Depends(authorize)])
    async def backtest(payload: BacktestPayload):
        try:
            config = payload.model_dump(exclude={"source","start","end","model_id"},exclude_none=True)
            parse_interval(payload.interval)
            if payload.source == "fixture":
                rows = generate_candles(payload.symbol,count=500,interval=payload.interval)
            elif payload.source in adapters:
                adapter=adapters[payload.source]
                if payload.start and payload.end and hasattr(adapter,"historical_range"):
                    from .core import timestamp_value
                    start=timestamp_value(payload.start+"T00:00:00+00:00" if len(payload.start)==10 else payload.start).timestamp()
                    end=timestamp_value(payload.end+"T00:00:00+00:00" if len(payload.end)==10 else payload.end).timestamp()+(86400 if len(payload.end)==10 else 0)
                    rows=await adapter.historical_range(payload.symbol,payload.interval,start,end)
                else:
                    rows = await adapter.candles(payload.symbol,payload.interval,1000)
                rows=[{**r,"source":payload.source} for r in rows]
                if history:
                    history.put(payload.symbol,payload.interval,rows)
            else:
                raise ValueError("Connect the selected historical provider first")
            from datetime import datetime
            def epoch(value):
                return datetime.fromisoformat((value+"T00:00:00+00:00" if len(value)==10 else value).replace("Z","+00:00")).timestamp()
            if payload.start:
                rows = [r for r in rows if r["time"] >= epoch(payload.start)]
            if payload.end:
                ending=epoch(payload.end)+(86400 if len(payload.end)==10 else 0)
                rows = [r for r in rows if r["time"]+parse_interval(payload.interval) <= ending]
            if len(rows) < 25:
                raise ValueError("Selected range requires at least 25 candles")
            # Bounded to at most 1000 candles: short local calculation avoids a
            # persistent worker pool and keeps shutdown/restart deterministic.
            model=registry.get(payload.model_id)
            if not model["endpoint"].startswith("mock:"):
                return replay_jobs.create(config,rows,payload.model_id,payload.source)
            probabilities=[]
            for i in range(len(rows)):
                if i<20:
                    probabilities.append(.5)
                    continue
                prediction=await registry.predict(payload.model_id,payload.symbol,payload.interval,rows[max(0,i-199):i+1])
                probabilities.append(prediction["probability_up"])
            result = run_backtest(**config,candles=rows,probabilities=probabilities)
            result.update(source=payload.source,model=payload.model_id,lookahead=False)
            from uuid import uuid4
            ident=str(uuid4())
            core.store.record("backtests",ident,{"id":ident,"status":"completed","progress":1,"created":time.time(),"config":config,"model_id":payload.model_id,"source":payload.source,"result":result},write=True)
            return result
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/backtests",dependencies=[Depends(authorize)])
    async def replay_list():
        rows=core.store.db.execute("SELECT key FROM records WHERE namespace='backtests' ORDER BY json_extract(value,'$.created') DESC LIMIT 30").fetchall()
        return {"jobs":[{k:v for k,v in replay_jobs.public(r[0]).items() if k!='result'} for r in rows]}

    @app.get("/backtests/{ident}",dependencies=[Depends(authorize)])
    async def replay_status(ident: str):
        try:
            return replay_jobs.public(ident)
        except ValueError as exc:
            raise HTTPException(404,str(exc)) from exc

    @app.post("/backtests/{ident}/resume",dependencies=[Depends(authorize)])
    async def replay_resume(ident: str):
        try:
            replay_jobs.start(ident)
            return replay_jobs.public(ident)
        except ValueError as exc:
            raise HTTPException(404,str(exc)) from exc

    @app.get("/models",dependencies=[Depends(authorize)])
    async def models():
        return {"models":registry.list()}

    @app.post("/models",dependencies=[Depends(authorize)])
    async def add_model(payload: ModelPayload):
        disarm()
        try:
            return registry.add(payload.model_dump(exclude_none=True))
        except ModelError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/models/local/connect", dependencies=[Depends(authorize)])
    async def connect_local_model():
        model = await protected_error(registry.connect_local())
        disarm()
        core.set_running(False)
        return model

    @app.post("/models/{model_id}/health",dependencies=[Depends(authorize)])
    async def model_health(model_id: str):
        return await protected_error(registry.health(model_id))

    @app.delete("/models/{model_id}",dependencies=[Depends(authorize)])
    async def remove_model(model_id: str):
        if any(s.get("model_id") == model_id for s in core.snapshot()["strategies"]):
            raise HTTPException(400,"Reassign strategies before removing this model")
        try:
            return registry.remove(model_id)
        except ModelError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/quotes",dependencies=[Depends(authorize)])
    async def watch_quotes(source: str="fixture"):
        if source=="fixture":
            return {"quotes":core.snapshot().get("prices",{})}
        if source == "bitfinex":
            await public_adapter()
        if source not in adapters:
            raise HTTPException(400,"Connect the selected market provider")
        symbols=core.state["watchlist"][:25]
        quotes={}
        adapter=adapters[source]
        if source=="bitfinex" and getattr(adapter,"_owns_client",False):
            if source not in stream_tasks:
                await ensure_stream(source)
            return {"quotes":{symbol:q.to_dict() for symbol in symbols if (q:=quote_cache.get((source,symbol))) is not None}}
        for symbol in symbols:
            try:
                quote=quote_cache.get((source,symbol)) or await adapters[source].quote(symbol)
                quotes[symbol]=quote.to_dict()
            except ProviderError:
                continue
        return {"quotes":quotes}

    @app.put("/watchlist",dependencies=[Depends(authorize)])
    async def watchlist(payload: dict):
        try:
            symbols=core.set_watchlist(payload.get("symbols"))
            if state["source"] in stream_tasks:
                await ensure_stream(state["source"])
            return {"symbols":symbols}
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/orders/history",dependencies=[Depends(authorize)])
    async def archived_orders(offset: int=0):
        rows=core.store.db.execute("SELECT value FROM records WHERE namespace='order-history' ORDER BY key LIMIT 200 OFFSET ?",(max(0,offset),)).fetchall()
        return {"orders":[json.loads(r[0]) for r in rows]}

    @app.get("/equity/history",dependencies=[Depends(authorize)])
    async def equity_history(provider: str,offset: int=0):
        rows=core.store.db.execute("SELECT value FROM records WHERE namespace='equity-history' ORDER BY key DESC LIMIT 1000 OFFSET ?",(max(0,offset),)).fetchall()
        return {"observations":[p for p in (json.loads(r[0]) for r in rows) if p.get("provider")==provider]}

    @app.get("/alerts",dependencies=[Depends(authorize)])
    async def alerts():
        rows=core.store.db.execute("SELECT value FROM records WHERE namespace='alerts' ORDER BY key DESC LIMIT 100").fetchall()
        return {"alerts":[json.loads(r[0]) for r in rows]}

    @app.get("/backups",dependencies=[Depends(authorize)])
    async def list_backups():
        return {"backups":[{"name":p.name,"size":p.stat().st_size,"created":p.stat().st_mtime} for p in sorted((directory/"backups").glob("backup-*.zip"),reverse=True)][:50],"restore_pending":(directory/"pending-restore.json").exists()}

    @app.post("/backups",dependencies=[Depends(authorize)])
    async def backup_workspace():
        from .backup import create_backup
        async with execution_lock:
            disarm()
            return create_backup(directory,core,registry,history)

    @app.post("/backups/{name}/restore",dependencies=[Depends(authorize)])
    async def restore_workspace(name: str, payload: dict):
        if payload.get("confirm") is not True:
            raise HTTPException(400,"Explicit restore confirmation required")
        from .backup import schedule_restore
        async with execution_lock:
            disarm()
            core.set_running(False)
            try:
                schedule_restore(directory,name)
            except (ValueError,OSError) as exc:
                raise HTTPException(400,"Backup validation failed; current data was preserved") from exc
        return {"restart_required":True,"message":"Close and reopen the application to restore this verified backup"}

    @app.get("/connections",dependencies=[Depends(authorize)])
    async def connections():
        return {"connections":list(connection_meta.values())}

    @app.post("/connections",dependencies=[Depends(authorize)])
    async def connect(payload: ConnectionPayload):
        if fixed_mode == "paper":
            raise HTTPException(400,"Connect your real Bitfinex account in the live workspace; paper uses public market data")
        if payload.provider != "bitfinex":
            raise HTTPException(400,"Only Bitfinex is supported")
        disarm()
        core.set_running(False)
        adapter = provider_factory(payload.provider,{"key":payload.key,"secret":payload.secret},paper=payload.paper)
        if hasattr(adapter,"bind_store"):
            adapter.bind_store(core.store)
        try:
            account = await adapter.account()
            json.dumps(account,allow_nan=False)
            # Verify access before storing credentials in the OS vault.
            secure_credentials(payload.provider,payload.key,payload.secret)
            previous = adapters.get(payload.provider)
            if previous:
                await previous.close()
            adapters[payload.provider] = adapter
            await ensure_account_events(payload.provider)
            # Provider account response must never contain API credentials.
            metadata = {"provider":payload.provider,"status":"connected","paper":payload.paper,"account":account}
            connection_meta[payload.provider] = metadata
            core.remember_connection(payload.provider,{**metadata,"status":"saved"})
            if core.state["mode"] == "live" and account.get("reconciliation_complete") is not False and account.get("equity") is not None:
                core.reconcile_live_account(account, account.get("positions", []))
            if payload.provider in stream_tasks:
                await ensure_stream(payload.provider)
            return metadata
        except Exception as exc:
            await adapter.close()
            if isinstance(exc,ProviderError):
                raise HTTPException(400,str(exc)) from exc
            raise HTTPException(400,"Provider connection failed; credentials were not saved") from exc

    @app.post("/connections/{provider}/restore",dependencies=[Depends(authorize)])
    async def restore(provider: str,payload: dict | None=None):
        try:
            credentials = get_credentials(provider)
        except ProviderError as exc:
            raise HTTPException(400,str(exc)) from exc
        if not credentials:
            raise HTTPException(400,"No saved operating system credentials")
        return await connect(ConnectionPayload(provider=provider,**credentials,paper=(payload or {}).get("paper",True)))

    @app.post("/connections/bitfinex/basis",dependencies=[Depends(authorize)])
    async def import_opening_basis(payload: dict):
        from .providers import validate_symbol
        from .core import finite,timestamp_value
        if "bitfinex" not in adapters or payload.get("confirm") is not True:
            raise HTTPException(400,"Connect Bitfinex and acknowledge the local accounting adjustment")
        disarm()
        try:
            symbol=validate_symbol(str(payload.get("symbol","")).upper())
            if not symbol.endswith("/USDT"):
                raise ValueError("Opening basis requires a USDT spot pair")
            qty=finite(payload.get("qty"),"opening quantity",0)
            cost=finite(payload.get("cost"),"total opening cost",0)
            asof=int(timestamp_value(payload.get("asof")).timestamp()*1000)
            if asof>time.time()*1000:
                raise ValueError("Opening basis cannot be dated in the future")
            reason=str(payload.get("reason","")).strip()
            if len(reason)<5:
                raise ValueError("Record the source of the opening cost basis")
            flow=float(payload.get("cash_flow_usdt",0))
            if not math.isfinite(flow):
                raise ValueError("Cash flow must be finite")
            adapter=adapters["bitfinex"]
            account=await adapter.account()
            key=str(account["account_id"])
            records=core.store.record("basis-adjustments",key) or {}
            records[symbol.split("/")[0]]={"qty":qty,"cost":cost,"asof":asof,"reason":reason,"recorded_at":time.time()}
            core.store.record("basis-adjustments",key,records,write=True)
            core.store.record("manual-cashflows",key,(core.store.record("manual-cashflows",key) or 0)+flow,write=True)
            core._save("account.basis_adjusted",{"account_id":key,"symbol":symbol,"qty":qty,"cost":cost,"asof":asof,"reason":reason,"cash_flow_usdt":flow})
            return {"saved":True,"message":"Opening basis saved locally. Synchronize and verify before reactivating trading."}
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/connections/{provider}/sync",dependencies=[Depends(authorize)])
    async def sync_account(provider: str):
        if provider not in adapters:
            raise HTTPException(400,"Connect the provider first")
        account = await protected_error(reconcile(provider))
        if execution["mode"] == "autonomous" and execution["provider"] != provider:
            disarm()
        state["source"] = provider
        core.store.record("preferences","source",provider,write=True)
        state["last_error"] = None
        return account

    @app.post("/connections/{provider}/orders/{order_id}/cancel",dependencies=[Depends(authorize)])
    async def cancel_exchange_order(provider: str, order_id: str, payload: dict):
        if provider != "bitfinex" or provider not in adapters or payload.get("confirm") is not True:
            raise HTTPException(400,"Connected Bitfinex account and explicit cancellation confirmation required")
        result = await protected_error(adapters[provider].cancel_order(order_id))
        last_account_sync[provider] = 0
        return result

    @app.delete("/connections/{provider}",dependencies=[Depends(authorize)])
    async def disconnect(provider: str):
        disarm()
        if provider != "bitfinex":
            raise HTTPException(400,"Unknown provider")
        if state["source"] == provider:
            core.set_running(False)
        task = stream_tasks.pop(provider,None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        event_task=account_tasks.pop(provider,None)
        if event_task:
            event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await event_task
        adapter = adapters.pop(provider,None)
        if adapter:
            await adapter.close()
        connection_meta.pop(provider,None)
        core.forget_connection(provider)
        try:
            delete_credentials(provider)
        except ProviderError as exc:
            return {"provider":provider,"disconnected":True,"warning":str(exc)}
        return {"provider":provider,"disconnected":True}

    @app.get("/proposals",dependencies=[Depends(authorize)])
    async def proposals():
        return {"proposals":core.snapshot().get("proposals",[])}

    @app.post("/proposals",dependencies=[Depends(authorize)])
    async def propose(payload: ProposalPayload):
        provider = payload.provider or state["source"]
        if core.snapshot()["mode"] != "live":
            raise HTTPException(400,"Manual live drafts require live review mode")
        if provider not in adapters or provider != "bitfinex":
            raise HTTPException(400,"Select and connect a real provider before drafting an order")
        try:
            await reconcile(provider)
            if execution["mode"] == "autonomous" and execution["provider"] != provider:
                disarm()
            state["source"] = provider
            core.store.record("preferences","source",provider,write=True)
            quote = await adapters[provider].quote(payload.symbol)
            if quote.stale or time.time() - quote.timestamp > core.snapshot()["global_risk"]["stale_after"]:
                raise ProviderError("A fresh provider quote is required before drafting an order")
            async with lock:
                proposal = core.propose_order(payload.symbol,payload.side,payload.qty,quote.price,event_id=payload.id,source=provider,timestamp=quote.timestamp)
            return proposal
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/proposals/{proposal_id}/cancel",dependencies=[Depends(authorize)])
    async def cancel_proposal(proposal_id: str):
        try:
            proposal = next((p for p in core.snapshot().get("proposals",[]) if p["id"] == proposal_id),None)
            if not proposal or proposal.get("status") != "awaiting_review":
                raise ValueError("Only a proposal awaiting review can be cancelled here; submitted orders require broker reconciliation")
            return core.finish_proposal(proposal_id,"rejected",{"reason":"Cancelled manually before submission"})
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/proposals/{proposal_id}/confirm",dependencies=[Depends(authorize)])
    async def confirm(proposal_id: str,payload: dict):
        if payload.get("confirm") is not True:
            raise HTTPException(400,"Manual confirmation is required")
        provider = payload.get("provider")
        if core.snapshot()["mode"] != "live" or provider not in adapters:
            raise HTTPException(400,"Select live mode and the connected proposal market provider")
        proposals = core.snapshot().get("proposals",[])
        proposal = next((p for p in proposals if p.get("id") == proposal_id),None)
        if not proposal:
            raise HTTPException(404,"Proposal not found")
        if proposal.get("source") != provider:
            raise HTTPException(400,"Confirmation provider must match the order proposal")
        if proposal.get("status") not in {"pending","proposed","pending_review","awaiting_review"}:
            raise HTTPException(409,"Proposal has already been handled")
        # Claim persisted ID before submitting: an ambiguous failure is never retried automatically.
        if not hasattr(core,"claim_proposal"):
            raise HTTPException(501,"Live confirmation is unavailable in this build")
        try:
            return await execute_proposal(proposal_id, provider)
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/models/{model_id}/predict", dependencies=[Depends(authorize)])
    async def model_prediction(model_id: str, payload: dict):
        symbol = str(payload.get("symbol", "BTC/USDT"))
        interval = str(payload.get("interval", "1h"))
        source = payload.get("source", state["source"])
        result = await candles(symbol, interval, source, 200)
        seconds = parse_interval(interval)
        completed = [c for c in result["candles"] if c["time"] + seconds <= time.time()]
        if len(completed) < 20:
            raise HTTPException(400, "At least 20 completed candles are required")
        if source != "fixture" and completed[-1]["time"] + seconds < time.time() - seconds * 2:
            raise HTTPException(400, "Completed candles are stale; prediction unavailable")
        prediction = await protected_error(registry.predict(model_id, symbol, interval, completed))
        return {**prediction, "symbol":symbol, "interval":interval, "source":source,
                "last_close":completed[-1]["close"], "as_of":completed[-1]["time"] + seconds,
                "target_time":completed[-1]["time"] + 2 * seconds,
                "meaning":"Score for the next interval closing above the last completed close",
                "calibrated":False if registry.get(model_id)["endpoint"].startswith(("mock:", "builtin:")) else prediction.get("calibrated")}

    @app.get("/exchange/orders", dependencies=[Depends(authorize)])
    async def exchange_order_history(end: int | None = None, limit: int = 250):
        if core.state["mode"] != "live" or connection_meta.get("bitfinex",{}).get("status") != "connected":
            raise HTTPException(400, "Connect Bitfinex in live mode to read account orders")
        if not 1 <= limit <= 2500 or (end is not None and end < 0):
            raise HTTPException(400, "Invalid order history page")
        return await protected_error(adapters["bitfinex"].order_history(end=end, limit=limit))

    return app


def create_app(data_dir=None, token=None, *, tick_seconds=2, production=False):
    """Two persistent runtimes: selecting a view never changes execution authority."""
    directory = Path(data_dir or Path.home() / ".axiom")
    live = _create_workspace(directory, token, tick_seconds=tick_seconds,
                             production=production, fixed_mode="live" if production else None)
    paper_existed = (directory / "paper" / "axiom.sqlite3").exists()
    if not paper_existed:
        import sqlite3
        (directory / "paper").mkdir(exist_ok=True)
        target = sqlite3.connect(directory / "paper" / "models.sqlite3")
        try:
            live.state.registry.db.backup(target)
        finally:
            target.close()
    paper = _create_workspace(directory / "paper", token, tick_seconds=tick_seconds,
                              production=production, fixed_mode="paper")
    if not paper_existed:
        from copy import deepcopy
        for key in ("cash", "initial_cash", "peak_equity", "positions", "orders", "signals", "strategies", "global_risk", "realized_pnl", "fees_paid", "paper_equity_history", "watchlist"):
            if key in live.state.core.state:
                paper.state.core.state[key] = deepcopy(live.state.core.state[key])
        paper.state.core.state["signals"] = [s for s in paper.state.core.state["signals"] if s.get("mode") == "paper"]
        paper.state.core._save("paper.migrated", {"message":"Existing simulated portfolio copied into its independent workspace"})
    live.state.workspaces = {"paper":paper}
    live.mount("/workspaces/paper", paper)

    @live.get("/workspaces/status")
    async def workspace_status(authorization: str | None = Header(default=None)):
        if not authorization or not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "Invalid session token")
        return {"live":{"running":live.state.core.state["running"], "mode":"live"},
                "paper":{"running":paper.state.core.state["running"], "mode":"paper"}}

    return live
