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
from .providers import ProviderError, get_credentials, provider_factory, secure_credentials, delete_credentials, parse_interval


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class StrategyPayload(Payload):
    symbol: str
    interval: str = "1h"
    threshold: float = Field(.65,ge=0,le=1)
    exit_threshold: float | None = Field(None,ge=0,le=1)
    fixed_stop: float = Field(.03,ge=0,lt=1)
    trailing_stop: float = Field(.05,ge=0,lt=1)
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
    start: str | None = None
    end: str | None = None


def create_app(data_dir=None, token=None, *, tick_seconds=2):
    if token is None or len(token) < 32:
        raise ValueError("A random session token of at least 32 characters is required")
    directory = Path(data_dir or Path.home() / ".axiom")
    directory.mkdir(parents=True,exist_ok=True)
    core = Engine(directory)
    registry = ModelRegistry(directory)
    adapters = {}
    connection_meta = {}
    state = {"source":"fixture", "last_error":None, "last_tick":None, "replay_cursor":60}
    lock = asyncio.Lock()
    last_buckets = {}
    quote_cache = {}
    stream_tasks = {}
    history = None
    try:
        from .history import HistoryStore
        history = HistoryStore(directory / "history")
    except ImportError:
        state["history_status"] = "DuckDB unavailable; install requirements.txt for durable history"

    async def reconcile(provider):
        account = await adapters[provider].account()
        connection_meta[provider]["account"] = account
        if account.get("reconciliation_complete") is False or account.get("equity") is None:
            raise ProviderError("This provider cannot establish complete equity and cost basis; live proposals are blocked")
        core.reconcile_live_account(account,account.get("positions",[]))
        # Query by durable client ID, including ambiguous submissions recovered after restart.
        if hasattr(adapters[provider],"get_order"):
            for proposal in core.snapshot().get("proposals",[]):
                if proposal.get("source") != provider or proposal.get("status") not in {"submitting","submitted","unknown","reconcile_required"}:
                    continue
                try:
                    broker_order = await adapters[provider].get_order(proposal["id"])
                except ProviderError:
                    continue  # Preserve the blocker until the broker can establish a result.
                status = broker_order.get("status")
                resolved = {"filled":"filled","canceled":"cancelled","cancelled":"cancelled","expired":"expired","rejected":"rejected"}.get(status)
                if resolved:
                    core.finish_proposal(proposal["id"],resolved,broker_order)
        return account

    async def stream_prices(source,symbols):
        try:
            async for quote in adapters[source].stream(symbols):
                quote_cache[(source,quote.symbol)] = quote
                # Pausing model decisions preserves independent stop monitoring.
                if state["source"] != source:
                    continue
                if quote.stale:
                    state["last_error"] = "Market stream is stale; trading paused until provider recovers"
                    core.set_running(False)
                    continue
                async with lock:
                    core.tick(quote.symbol,quote.price,None,timestamp=quote.timestamp,event_id=str(uuid.uuid4()),completed=False,source=source)
                state["last_tick"] = time.time()
        except asyncio.CancelledError:
            raise
        except Exception:
            state["last_error"] = "Market stream failed; trading paused"
            core.set_running(False)

    async def ensure_stream(source):
        old = stream_tasks.pop(source,None)
        if old:
            old.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await old
        symbols = list({s["symbol"] for s in core.snapshot()["strategies"]})
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

    async def scheduler():
        while True:
            try:
                await asyncio.sleep(tick_seconds)
                snap = core.snapshot()
                if not snap["running"]:
                    continue
                if snap["mode"] == "live":
                    await reconcile(state["source"])
                held = {p["symbol"] for p in snap["positions"]}
                for strategy in snap["strategies"]:
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
                        if candles and history:
                            history.put(symbol,interval,candles)
                    q = None
                    if completed:
                        prediction = await registry.predict(strategy.get("model_id","mock-momentum"),symbol,interval,candles)
                        q = prediction["probability_up"]
                    async with lock:
                        quote_time = time.time() if source == "fixture" else quote_time
                        core.tick(symbol,price,q,timestamp=quote_time,event_id=str(uuid.uuid4()),completed=completed,source=source,interval_id=candles[-1]["time"]//seconds if candles and completed else None)
                    state["last_tick"] = time.time()
                    state["last_error"] = None
                state["replay_cursor"] = min(600,state["replay_cursor"]+1)
                if state["source"] == "fixture" and state["replay_cursor"] == 600:
                    core.set_running(False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Fail closed; user sees the reason and explicitly restarts after repair.
                core.set_running(False)
                state["last_error"] = str(exc) if isinstance(exc,(ValueError,ProviderError,ModelError)) else "Engine operation failed; trading paused"

    @asynccontextmanager
    async def lifespan(app):
        # Restart never automatically restores execution permission.
        core.set_running(False)
        task = asyncio.create_task(scheduler())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for stream_task in stream_tasks.values():
            stream_task.cancel()
        for stream_task in stream_tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await stream_task
        for adapter in adapters.values():
            await adapter.close()
        registry.close()
        if history:
            history.close()
        if hasattr(core,"close"):
            core.close()

    app = FastAPI(title="Axiom Local Engine",version="0.1.0",lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
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
        return {"status":"ok","version":"0.1.0","local":True,"data_source":state["source"]}

    @app.get("/snapshot",dependencies=[Depends(authorize)])
    async def snapshot():
        return core.snapshot() | {"models":registry.list(),"connections":list(connection_meta.values()),"data_source":state["source"],"system_health":state.copy(),"predictions":registry.history(50)}

    @app.get("/candles",dependencies=[Depends(authorize)])
    async def candles(symbol: str="BTC/USD",interval: str="1h",source: str="fixture",limit: int=180):
        try:
            parse_interval(interval)
            if not 2 <= limit <= 2000:
                raise ValueError("Candle limit must be between 2 and 2000")
            if source == "fixture":
                rows = generate_candles(symbol,count=limit,interval=interval)
            elif source in adapters:
                rows = await adapters[source].candles(symbol,interval,limit)
            else:
                raise ProviderError("Connect a provider to request real market data")
            if history and source != "fixture":
                history.put(symbol,interval,rows)
            return {"symbol":symbol,"interval":interval,"source":source,"candles":rows}
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/instruments",dependencies=[Depends(authorize)])
    async def instruments(provider: str="fixture"):
        if provider == "fixture":
            return {"source":"fixture","instruments":[{"symbol":s,"name":n,"class":c} for s,n,c in [("BTC/USD","Bitcoin","crypto"),("ETH/USD","Ethereum","crypto"),("SOL/USD","Solana","crypto"),("AAPL","Apple Inc.","stock"),("NVDA","NVIDIA","stock"),("MSFT","Microsoft","stock"),("SPY","S&P 500 ETF","stock")]]}
        if provider not in adapters:
            raise HTTPException(400,"Provider is not connected")
        return {"source":provider,"instruments":await protected_error(adapters[provider].fetch_assets())}

    @app.get("/history",dependencies=[Depends(authorize)])
    async def historical_cache(symbol: str="BTC/USD",interval: str="1h"):
        if not history:
            raise HTTPException(503,"DuckDB history dependency is unavailable")
        return {"symbol":symbol,"interval":interval,"candles":history.read(symbol,interval),"source":"cached provider data"}

    @app.post("/history/export",dependencies=[Depends(authorize)])
    async def export_history():
        if not history:
            raise HTTPException(503,"DuckDB history dependency is unavailable")
        path = directory / "history" / f"candles-{int(time.time())}.parquet"
        history.export_parquet(path)
        return {"path":str(path),"format":"parquet","source":"cached provider data"}

    @app.put("/strategy",dependencies=[Depends(authorize)])
    async def strategy(payload: StrategyPayload):
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

    @app.put("/settings",dependencies=[Depends(authorize)])
    async def settings(payload: dict):
        if set(payload) != {"risk"} or not isinstance(payload["risk"],dict):
            raise HTTPException(400,"Expected {risk:{...}}")
        if set(payload["risk"]) - {"max_position","max_exposure","max_drawdown","stale_after","fee_rate","slippage"}:
            raise HTTPException(400,"Unknown risk setting")
        try:
            async with lock:
                return core.configure_risk(payload["risk"])
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/mode",dependencies=[Depends(authorize)])
    async def mode(payload: dict):
        try:
            async with lock:
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
        state["source"] = "fixture"
        return core.reset()

    @app.post("/engine/start",dependencies=[Depends(authorize)])
    async def start(payload: dict | None=None):
        source = (payload or {}).get("source","fixture")
        if source not in {"fixture","alpaca","kraken"}:
            raise HTTPException(400,"Unknown market source")
        if source != "fixture" and source not in adapters:
            raise HTTPException(400,"Connect the selected market provider first")
        if source == "fixture" and core.snapshot()["mode"] == "live":
            raise HTTPException(400,"Live proposals require a real market provider")
        if core.snapshot()["mode"] == "live":
            try:
                await reconcile(source)
            except (ProviderError,ValueError) as exc:
                raise HTTPException(400,str(exc)) from exc
        state["source"] = source
        state["last_error"] = None
        core.set_running(True)
        if source in adapters:
            await ensure_stream(source)
        return {"running":True,"source":source}

    @app.post("/engine/pause",dependencies=[Depends(authorize)])
    async def pause():
        core.set_running(False)
        return {"running":False}

    @app.post("/orders",dependencies=[Depends(authorize)])
    async def order(payload: OrderPayload):
        if core.snapshot()["mode"] != "paper":
            raise HTTPException(400,"Real orders require a proposal and separate confirmation")
        try:
            async with lock:
                result = core.paper_order(payload.symbol,payload.side,payload.qty,payload.price,payload.id)
            if state["source"] in adapters:
                await ensure_stream(state["source"])
            return result
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.post("/backtest",dependencies=[Depends(authorize)])
    async def backtest(payload: BacktestPayload):
        try:
            config = payload.model_dump(exclude={"source","start","end"},exclude_none=True)
            parse_interval(payload.interval)
            if payload.source == "fixture":
                rows = generate_candles(payload.symbol,count=500,interval=payload.interval)
            elif payload.source in adapters:
                rows = await adapters[payload.source].candles(payload.symbol,payload.interval,1000)
                if history:
                    history.put(payload.symbol,payload.interval,rows)
            else:
                raise ValueError("Connect the selected historical provider first")
            from datetime import datetime
            def epoch(value):
                return datetime.fromisoformat(value.replace("Z","+00:00")).timestamp()
            if payload.start:
                rows = [r for r in rows if r["time"] >= epoch(payload.start)]
            if payload.end:
                rows = [r for r in rows if r["time"] <= epoch(payload.end)]
            if len(rows) < 25:
                raise ValueError("Selected range requires at least 25 candles")
            # Bounded to at most 1000 candles: short local calculation avoids a
            # persistent worker pool and keeps shutdown/restart deterministic.
            result = run_backtest(**config,candles=rows)
            return result | {"source":payload.source,"model":"deterministic momentum fixture","lookahead":False}
        except (ValueError,ProviderError) as exc:
            raise HTTPException(400,str(exc)) from exc

    @app.get("/models",dependencies=[Depends(authorize)])
    async def models():
        return {"models":registry.list()}

    @app.post("/models",dependencies=[Depends(authorize)])
    async def add_model(payload: ModelPayload):
        try:
            return registry.add(payload.model_dump(exclude_none=True))
        except ModelError as exc:
            raise HTTPException(400,str(exc)) from exc

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

    @app.get("/connections",dependencies=[Depends(authorize)])
    async def connections():
        return {"connections":list(connection_meta.values())}

    @app.post("/connections",dependencies=[Depends(authorize)])
    async def connect(payload: ConnectionPayload):
        if payload.provider not in {"alpaca","kraken"}:
            raise HTTPException(400,"Supported providers are Alpaca and Kraken")
        adapter = provider_factory(payload.provider,{"key":payload.key,"secret":payload.secret},paper=payload.paper)
        try:
            account = await adapter.account()
            # Verify access before storing credentials in the OS vault.
            secure_credentials(payload.provider,payload.key,payload.secret)
            previous = adapters.get(payload.provider)
            if previous:
                await previous.close()
            adapters[payload.provider] = adapter
            # Provider account response must never contain API credentials.
            metadata = {"provider":payload.provider,"status":"connected","paper":payload.paper,"account":account}
            connection_meta[payload.provider] = metadata
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

    @app.delete("/connections/{provider}",dependencies=[Depends(authorize)])
    async def disconnect(provider: str):
        if provider not in {"alpaca","kraken"}:
            raise HTTPException(400,"Unknown provider")
        if state["source"] == provider:
            core.set_running(False)
        task = stream_tasks.pop(provider,None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        adapter = adapters.pop(provider,None)
        if adapter:
            await adapter.close()
        connection_meta.pop(provider,None)
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
        if provider not in adapters or provider not in {"alpaca","kraken"}:
            raise HTTPException(400,"Select and connect a real provider before drafting an order")
        try:
            await reconcile(provider)
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
            await reconcile(provider)
            quote = await adapters[provider].quote(proposal["symbol"])
            if quote.stale or time.time() - quote.timestamp > core.snapshot()["global_risk"]["stale_after"]:
                raise ProviderError("Fresh market data is required before manual confirmation")
            async with lock:
                claimed = core.claim_proposal(proposal_id,quote_price=quote.price)
            result = await adapters[provider].submit_order(claimed | {"client_order_id":proposal_id,"confirmed":True})
            core.finish_proposal(proposal_id,"submitted",result)
            return {"proposal_id":proposal_id,"status":"submitted","order":result}
        except (ValueError,ProviderError) as exc:
            current = next((p for p in core.snapshot()["proposals"] if p["id"] == proposal_id),{})
            if current.get("status") == "submitting":
                core.finish_proposal(proposal_id,"reconcile_required",{"message":"Verify broker account before retrying"})
            raise HTTPException(400,str(exc)) from exc

    return app
