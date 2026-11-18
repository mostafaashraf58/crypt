"""Real market/account adapters. No fixture fallback; execution requires authorization.

Only OS keyring stores provider secrets. All network methods are asynchronous and
raise ProviderError without echoing sensitive response bodies or authentication.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
import time
from decimal import Decimal, InvalidOperation
from dataclasses import asdict, dataclass
from typing import AsyncIterator

import httpx
import websockets


class ProviderError(RuntimeError):
    pass


class OrderNotSubmitted(ProviderError):
    """Definitive local rejection before an exchange write request."""


class ExchangeRejected(ProviderError):
    """Exchange explicitly rejected a write request."""


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    timestamp: float
    provider: str
    stale: bool = False

    def to_dict(self):
        return asdict(self)


def parse_interval(interval: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([mhdw])", interval)
    if not match:
        raise ProviderError("Interval must be a positive number followed by m, h, d, or w")
    seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]
    if seconds > 31536000:
        raise ProviderError("Interval cannot exceed one year")
    return seconds


def secure_credentials(provider: str, key: str, secret: str) -> None:
    if provider != "bitfinex" or not key.strip() or not secret.strip():
        raise ProviderError("A supported provider and both credential values are required")
    try:
        import keyring
        # The JSON bundle is one atomic credential record in Windows Credential Manager.
        keyring.set_password("AxiomTrading", provider, json.dumps({"key": key.strip(), "secret": secret.strip()}))
    except Exception as exc:
        raise ProviderError("OS credential storage unavailable. Credentials were not saved.") from exc


def get_credentials(provider: str) -> dict | None:
    if provider != "bitfinex":
        raise ProviderError("Unknown provider")
    try:
        import keyring
        value = keyring.get_password("AxiomTrading", provider)
        if not value:
            return None
        result = json.loads(value)
        if not isinstance(result, dict) or not all(isinstance(result.get(k), str) for k in ("key", "secret")):
            raise ValueError("Invalid OS credential record")
        return result
    except Exception as exc:
        raise ProviderError("OS credential storage unavailable") from exc


def delete_credentials(provider: str) -> None:
    if provider != "bitfinex":
        raise ProviderError("Only Bitfinex is supported")
    try:
        import keyring
        keyring.delete_password("AxiomTrading", provider)
    except Exception as exc:
        raise ProviderError("Unable to remove OS credential record") from exc


def resample(candles: list[dict], seconds: int, *, completed_at: float | None = None) -> list[dict]:
    """UTC anchored aggregation, omitting still-forming target bars."""
    groups: dict[int, dict] = {}
    for c in sorted(candles, key=lambda x: x["time"]):
        try:
            values = [float(c[key]) for key in ("open", "high", "low", "close", "volume")]
            if not all(math.isfinite(v) for v in values) or min(values[:4]) <= 0 or values[4] < 0:
                raise ValueError()
            if c["high"] < max(c["open"], c["close"]) or c["low"] > min(c["open"], c["close"]):
                raise ValueError()
        except (ValueError, TypeError, KeyError) as exc:
            raise ProviderError("Provider returned malformed OHLCV data") from exc
        bucket = int(c["time"] // seconds * seconds)
        if completed_at is not None and bucket + seconds > completed_at:
            continue
        if bucket not in groups:
            groups[bucket] = {**c, "time": bucket}
        else:
            row = groups[bucket]
            row.update(high=max(row["high"], c["high"]), low=min(row["low"], c["low"]), close=c["close"], volume=row["volume"] + c["volume"])
    return list(groups.values())


def validate_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,19}(?:/[A-Z0-9][A-Z0-9._-]{0,11})?", symbol):
        raise ProviderError("Invalid instrument symbol")
    return symbol


def _price(value) -> float:
    try:
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError()
        return result
    except (ValueError, TypeError) as exc:
        raise ProviderError("Provider returned an invalid price") from exc






class BaseProvider:
    name = "base"
    timeout = 15.0

    def __init__(self, credentials: dict | None = None, *, client: httpx.AsyncClient | None = None, paper: bool = True):
        self.credentials = credentials or {}
        self.paper = paper
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=self.timeout, follow_redirects=False)
        self.status = "disconnected"
        self.last_error: str | None = None
        self.last_quote_at: float | None = None

    def _require_credentials(self):
        if not self.credentials.get("key") or not self.credentials.get("secret"):
            raise ProviderError(f"{self.name.title()} credentials are not configured")

    async def _request(self, method: str, url: str, **kwargs):
        attempts = 3 if method.upper() == "GET" else 1
        for attempt in range(attempts):
            try:
                response = await self.client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                self.status, self.last_error = "disconnected", "Network timeout or connection failure"
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.25 * 2 ** attempt)
                    continue
                raise ProviderError(self.last_error) from exc
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < attempts:
                    try:
                        wait = min(max(float(response.headers.get("Retry-After", "1")), .1), 5)
                    except ValueError:
                        wait = 1
                    await asyncio.sleep(wait * (attempt + 1))
                    continue
            if response.status_code >= 400:
                self.status, self.last_error = "error", f"{self.name.title()} request failed (HTTP {response.status_code})"
                raise ProviderError(self.last_error)
            try:
                result = response.json()
            except ValueError as exc:
                raise ProviderError("Provider returned malformed JSON") from exc
            self.status, self.last_error = "connected", None
            return result
        raise ProviderError("Provider request failed")

    def _validate_order(self, order: dict):
        if order.get("confirmed") is not True:
            raise ProviderError("Explicit execution authorization is required for each real order")
        if order.get("side") not in ("buy", "sell", "BUY", "SELL"):
            raise ProviderError("Order side must be buy or sell")
        _price(order.get("qty"))
        if not order.get("symbol") or not order.get("id"):
            raise ProviderError("Order requires symbol and unique client order ID")
        validate_symbol(order["symbol"])
        if order.get("type", "market") not in ("market", "limit"):
            raise ProviderError("Unsupported order type")
        if order.get("type") == "limit":
            _price(order.get("limit_price"))

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    async def stream(self, symbols: list[str]) -> AsyncIterator[Quote]:
        """Reconnect indefinitely, yielding stale markers during outages.

        Cancellation closes the socket. Consumers must reject stale quotes. A
        20-second lack of updates emits a stale marker using the previous quote.
        """
        delay = 1
        last: dict[str, Quote] = {}
        while True:
            try:
                async with websockets.connect(self.ws_url, open_timeout=15, close_timeout=3, ping_interval=20) as ws:
                    await self._subscribe(ws, symbols)
                    self.status = "connecting"
                    while True:
                        try:
                            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                        except asyncio.TimeoutError:
                            self.status = "stale"
                            for quote in last.values():
                                yield Quote(quote.symbol, quote.price, quote.timestamp, self.name, True)
                            continue
                        for quote in self._parse_stream(message):
                            self.status, self.last_error = "streaming", None
                            self.last_quote_at = quote.timestamp
                            last[quote.symbol] = quote
                            delay = 1
                            yield quote
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status, self.last_error = "reconnecting", "Market stream disconnected; reconnecting"
                for quote in last.values():
                    yield Quote(quote.symbol, quote.price, quote.timestamp, self.name, True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)






class BitfinexProvider(BaseProvider):
    name = "bitfinex"
    ws_url = "wss://api-pub.bitfinex.com/ws/2"
    public_url = "https://api-pub.bitfinex.com/v2/"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._nonce = int(time.time() * 1_000_000)
        self._private_lock = asyncio.Lock()
        self._account_lock = asyncio.Lock()
        self._last_private = 0.0
        self._trades = {}
        self._trade_cursor = 0
        self._store = None
        self._channels = {}
        self._latest = {}
        self._quote_lock = asyncio.Lock()
        self._last_quote_request = 0.0
        self._account_id = None
        self._order_cache = {}
        self._ledger_cursor = 0
        self._last_ledger_sync = 0.0
        self._public_limits = {}
        self._public_locks = {}
        self._cashflows = {}

    def bind_store(self, store):
        self._store=store
        self._store_key=hashlib.sha256(self.credentials["key"].encode()).hexdigest()
        self._nonce=max(self._nonce,store.record("bitfinex-nonce",self._store_key) or 0)
        saved=store.record("bitfinex-history",self._store_key) or {}
        self._trades={int(k):v for k,v in saved.get("trades",{}).items()}
        self._trade_cursor=saved.get("cursor",0)
        self._account_id=saved.get("account_id")
        self._cashflows=saved.get("cashflows",{})
        self._ledger_cursor=saved.get("ledger_cursor",0)

    def _persist_history(self):
        if self._store:
            self._store.record("bitfinex-history",self._store_key,{"trades":self._trades,"cursor":self._trade_cursor,"account_id":self._account_id,"cashflows":self._cashflows,"ledger_cursor":self._ledger_cursor},write=True)

    @staticmethod
    def _pair(symbol):
        validate_symbol(symbol)
        parts = ["UST" if p == "USDT" else p for p in symbol.split("/")]
        if len(parts) != 2:
            raise ProviderError("Bitfinex requires a BASE/QUOTE spot symbol")
        return "t" + ("".join(parts) if all(len(p) == 3 for p in parts) else ":".join(parts))

    @staticmethod
    def _symbol(pair):
        pair = pair.removeprefix("t")
        if ":" in pair:
            return "/".join("USDT" if p == "UST" else p for p in pair.split(":"))
        if len(pair) != 6:
            raise ProviderError("Unsupported Bitfinex pair format")
        return "/".join("USDT" if p == "UST" else p for p in (pair[:3], pair[3:]))

    async def _request(self, *args, **kwargs):
        data = await super()._request(*args, **kwargs)
        if isinstance(data, list) and data and data[0] == "error":
            raise ProviderError("Bitfinex rejected the request; check permissions and account state")
        return data

    async def _public(self,path,**params):
        group=path.split("/",1)[0]
        interval={"trades":4.1,"candles":2.1}.get(group,.75)
        lock=self._public_locks.setdefault(group,asyncio.Lock())
        async with lock:
            await asyncio.sleep(max(0,interval-(time.monotonic()-self._public_limits.get(group,0))))
            self._public_limits[group]=time.monotonic()
            return await self._request("GET",self.public_url+path,params=params)

    async def _private(self, path, _guard=None, **params):
        self._require_credentials()
        async with self._private_lock:
            await asyncio.sleep(max(0, .75 - (time.monotonic() - self._last_private)))
            if _guard is not None:
                try:
                    _guard()
                except ProviderError as exc:
                    raise OrderNotSubmitted(str(exc)) from exc
            self._last_private = time.monotonic()
            self._nonce = max(self._nonce + 1, int(time.time() * 1_000_000))
            if self._store:
                self._store.record("bitfinex-nonce",self._store_key,self._nonce,write=True)
            nonce = str(self._nonce)
            body = json.dumps(params, separators=(",", ":"))
            path = "v2/auth/" + path
            signature = hmac.new(self.credentials["secret"].encode(),
                                 ("/api/" + path + nonce + body).encode(), hashlib.sha384).hexdigest()
            return await self._request("POST", "https://api.bitfinex.com/" + path, content=body,
                                       headers={"bfx-apikey": self.credentials["key"], "bfx-nonce": nonce,
                                                "bfx-signature": signature, "Content-Type": "application/json"})

    async def fetch_assets(self):
        data = await self._public("conf/pub:list:pair:exchange")
        return [{"symbol": self._symbol(p), "name": self._symbol(p), "type": "crypto",
                 "tradable": True, "provider": self.name} for p in data[0] if self._symbol(p).endswith("/USDT")]

    async def candles(self, symbol, interval="1h", limit=200):
        seconds = parse_interval(interval)
        frames = {60:"1m", 300:"5m", 900:"15m", 1800:"30m", 3600:"1h", 10800:"3h",
                  21600:"6h", 43200:"12h", 86400:"1D", 604800:"1W"}
        base = max(n for n in frames if seconds % n == 0)
        count = max(1, min(int(limit), 2000))
        needed = (count + 1) * (seconds // base)
        if needed > 10000:
            raise ProviderError("Bitfinex candle request exceeds 10000 source bars; reduce limit")
        rows = await self._public(f"candles/trade:{frames[base]}:{self._pair(symbol)}/hist", limit=needed, sort=-1)
        bars = [{"time": int(r[0] / 1000), "open": float(r[1]), "close": float(r[2]),
                 "high": float(r[3]), "low": float(r[4]), "volume": float(r[5])} for r in reversed(rows)]
        # Native frames retain exact exchange timestamps, including weekly anchors.
        validated = resample(bars, seconds)
        if base == seconds:
            return sorted({bar["time"]:bar for bar in bars}.values(), key=lambda bar:bar["time"])[-count:]
        return validated[-count:]

    async def quote(self, symbol):
        cached=self._latest.get(symbol)
        if cached and not cached.stale and time.time()-cached.timestamp<5:
            return cached
        async with self._quote_lock:
            await asyncio.sleep(max(0,4.1-(time.monotonic()-self._last_quote_request)))
            self._last_quote_request=time.monotonic()
            rows=await self._public(f"trades/{self._pair(symbol)}/hist",limit=1,sort=-1)
        if not rows:
            raise ProviderError("Bitfinex returned no recent trades")
        stamp=float(rows[0][1])/1000
        result=Quote(symbol,_price(rows[0][3]),stamp,self.name,time.time()-stamp>60)
        self._latest[symbol]=result
        return result

    async def historical_range(self,symbol,interval,start,end):
        seconds=parse_interval(interval)
        frames={60:"1m",300:"5m",900:"15m",1800:"30m",3600:"1h",10800:"3h",21600:"6h",43200:"12h",86400:"1D"}
        base=max(n for n in frames if seconds%n==0)
        if end<=start or start<0 or end>time.time()+86400:
            raise ProviderError("Invalid historical range")
        if (end-start)/base>50000:
            raise ProviderError("Select at most 50000 source bars per historical replay")
        cursor=int((start//seconds)*seconds*1000)
        stop=int(min(end,time.time())*1000)
        bars={}
        while cursor<stop:
            rows=await self._public(f"candles/trade:{frames[base]}:{self._pair(symbol)}/hist",start=cursor,end=stop,limit=10000,sort=1)
            for row in rows:
                bars[int(row[0])]={"time":int(row[0]/1000),"open":float(row[1]),"close":float(row[2]),"high":float(row[3]),"low":float(row[4]),"volume":float(row[5]),"source":self.name}
            if len(rows)<10000:
                break
            next_cursor=max(int(r[0]) for r in rows)+1
            if next_cursor<=cursor:
                raise ProviderError("Historical candle pagination did not advance")
            cursor=next_cursor
        return [r for r in resample(sorted(bars.values(),key=lambda r:r["time"]),seconds,completed_at=min(end,time.time())) if r["time"]>=start]

    async def market_clock(self):
        return {"is_open": True, "timestamp": time.time(), "calendar": "24/7 cryptocurrency spot market"}

    @staticmethod
    def _number(value, label):
        try:
            result = Decimal(str(value))
            if not result.is_finite():
                raise ValueError()
            return result
        except (InvalidOperation, ValueError):
            raise ProviderError(f"Invalid Bitfinex {label}") from None

    @classmethod
    def _order(cls, row):
        if not isinstance(row, list) or len(row) < 18:
            raise ProviderError("Malformed Bitfinex order response")
        status = str(row[13])
        state = ("filled" if status.startswith("EXECUTED") else
                 "canceled" if status.startswith("CANCELED") else
                 "rejected" if status.startswith(("INSUFFICIENT", "ERROR")) else "open")
        return {"id": str(row[0]), "client_order_id": str(row[2]), "symbol": cls._symbol(row[3]),
                "type":row[8], "status": state, "side": "buy" if float(row[7]) > 0 else "sell",
                "qty": abs(float(row[7])), "filled_qty": abs(float(row[7])) - abs(float(row[6])),
                "price": float(row[16]), "avg_price": float(row[17]), "exchange_status": status,
                "created_at":float(row[4]) / 1000 if row[4] else None,
                "updated_at":float(row[5]) / 1000 if row[5] else None,
                "remaining_qty":abs(float(row[6])), "raw":row}

    async def order_history(self, *, end=None, limit=250):
        params = {"limit":limit}
        if end is not None:
            params["end"] = end
        rows = await self._private("r/orders/hist", **params)
        if not isinstance(rows, list):
            raise ProviderError("Malformed Bitfinex order history")
        # Include the boundary on the next page and deduplicate by exchange ID.
        # Refuse a saturated single-timestamp page instead of silently skipping orders.
        stamps = [int(row[4]) for row in rows]
        next_end = min(stamps) if len(rows) == limit and stamps else None
        if next_end is not None and (next_end == end or min(stamps) == max(stamps)):
            raise ProviderError("Order history timestamp boundary is saturated; use an exchange report for this boundary")
        return {"orders":[self._order(row) for row in rows], "next_end":next_end,
                "complete":next_end is None, "source":"bitfinex", "coverage":"Available Bitfinex API history (documented two-week window)"}

    async def _trade_history(self):
        # Inclusive cursor and ID deduplication preserve trades sharing a timestamp.
        cursor = max(0,self._trade_cursor-60000)
        for _ in range(100):
            rows = await self._private("r/trades/hist", start=cursor, sort=1, limit=2500)
            if not isinstance(rows, list):
                raise ProviderError("Malformed Bitfinex trade history")
            for row in rows:
                if len(row) < 11:
                    raise ProviderError("Incomplete Bitfinex trade history")
                self._trades[row[0]] = row
            if len(rows) < 2500:
                if any(r[9] is None or r[10] is None for r in self._trades.values()):
                    raise ProviderError("Trade fees are not finalized; refresh before trading")
                self._trade_cursor = max([cursor] + [int(r[2]) for r in rows])
                self._persist_history()
                return sorted(self._trades.values(), key=lambda r: (r[2], r[0]))
            next_cursor = int(rows[-1][2])
            if next_cursor <= cursor:
                raise ProviderError("Bitfinex history timestamp page is saturated; reconciliation required")
            cursor = next_cursor
        raise ProviderError("Bitfinex history exceeds reconciliation scan limit")

    async def account(self):
        async with self._account_lock:
            if self._store and self._account_id is None:
                user=await self._private("r/info/user")
                if isinstance(user,list) and user and isinstance(user[0],int):
                    self._account_id=user[0]
            wallets = await self._private("r/wallets")
            orders = await self._private("r/orders")
            spot = [w for w in wallets if w[0] == "exchange"]
            balances = {("USDT" if w[1] == "UST" else w[1]): self._number(w[2], "balance") for w in spot}
            available = {("USDT" if w[1] == "UST" else w[1]): self._number(w[4], "available balance")
                         for w in spot if w[4] is not None}
            if any(v < 0 for v in balances.values()) or any(v < 0 for v in available.values()):
                raise ProviderError("Negative spot balances require exchange reconciliation")
            missing = [c for c, v in balances.items() if v and c not in available]
            reasons = [f"Available balance unavailable: {c}" for c in missing]
            positions = []
            equity = balances.get("USDT", Decimal(0))
            adjustments=(self._store.record("basis-adjustments",str(self._account_id or self._store_key)) or {}) if self._store else {}
            holdings = {c: q for c, q in balances.items() if c != "USDT" and q}
            history = await self._trade_history() if holdings or self._store else []
            for currency, qty in holdings.items():
                symbol = currency + "/USDT"
                # Reconstruct moving average using exchange fills, including base/quote fees.
                adjustment=adjustments.get(currency,{})
                held=self._number(adjustment.get("qty",0),"opening quantity")
                cost=self._number(adjustment.get("cost",0),"opening cost")
                for trade in history:
                    if int(trade[2])<=adjustment.get("asof",-1):
                        continue
                    if self._symbol(trade[1]) != symbol:
                        continue
                    if not str(trade[6]).startswith("EXCHANGE "):
                        continue
                    amount = self._number(trade[4], "fill amount")
                    price = self._number(trade[5], "fill price")
                    fee = self._number(trade[9], "fill fee")
                    fee_currency = "USDT" if trade[10] == "UST" else trade[10]
                    if price <= 0 or fee_currency not in (currency, "USDT"):
                        reasons.append(f"Unsupported fee or fill for {symbol}")
                        continue
                    delta = amount + (fee if fee_currency == currency else 0)
                    if amount > 0:
                        held += delta
                        cost += amount * price - (fee if fee_currency == "USDT" else 0)
                    else:
                        if held <= 0 or held + delta < Decimal("-0.00000001"):
                            reasons.append(f"Trade history does not establish {symbol} cost basis")
                            break
                        cost *= max(Decimal(0), held + delta) / held
                        held += delta
                if abs(held - qty) > Decimal("0.00000001") or held <= 0 or cost <= 0:
                    reasons.append(f"Wallet and fill history differ for {symbol}; deposits/transfers need reconciliation")
                    continue
                quote = await self.quote(symbol)
                if quote.stale or quote.timestamp > time.time() + 5:
                    reasons.append(f"Fresh valuation unavailable for {symbol}")
                    continue
                equity += qty * Decimal(str(quote.price))
                positions.append({"symbol": symbol, "qty": float(qty), "available_qty": float(available.get(currency, 0)),
                                  "avg_entry_price": float(cost / held), "current_price": quote.price})
            if self._store and time.monotonic()-self._last_ledger_sync>=60:
                if not self._ledger_cursor:
                    self._ledger_cursor=int(time.time()*1000)
                for category in (51,101,104,105):
                    end=int(time.time()*1000)
                    for _ in range(100):
                        ledger=await self._private("r/ledgers/UST/hist",category=category,wallet="exchange",start=max(0,self._ledger_cursor-60000),end=end,limit=2500)
                        for row in ledger:
                            if len(row)>=9 and row[2]=="exchange" and row[1]=="UST":
                                self._cashflows[str(row[0])]=row
                        if len(ledger)<2500:
                            break
                        next_end=min(int(r[3]) for r in ledger)
                        if next_end>=end:
                            raise ProviderError("Cash-flow ledger boundary saturated")
                        end=next_end
                    else:
                        raise ProviderError("Cash-flow synchronization limit reached")
                self._ledger_cursor=int(time.time()*1000)
                self._last_ledger_sync=time.monotonic()
                self._persist_history()
            # Reject a snapshot that changed while history and marks were read.
            check = await self._private("r/wallets")
            if check != wallets:
                reasons.append("Wallet changed during reconciliation; refresh before trading")
            return {"provider": self.name, "account_id": str(self._account_id or hashlib.sha256(self.credentials["key"].encode()).hexdigest()), "paper": False, "currency": "USDT",
                    "cash": float(balances.get("USDT", 0)), "available_cash": float(available.get("USDT", 0)),
                    "equity": float(equity) if not reasons else None,
                    "balances": {c: float(v) for c, v in balances.items()}, "positions": positions,
                    "wallets":[{"type":w[0], "currency":"USDT" if w[1]=="UST" else w[1],
                                "balance":float(w[2]), "unsettled_interest":float(w[3] or 0),
                                "available":float(w[4]) if w[4] is not None else None} for w in wallets],
                    "orders": [self._order(r) for r in orders], "reconciliation_complete": not reasons,
                    "reconciliation_errors": reasons, "fills":list(self._trades.values()), "cash_flow":sum(float(r[5]) for r in self._cashflows.values())+(self._store.record("manual-cashflows",str(self._account_id or self._store_key)) or 0 if self._store else 0), "synced_at": time.time()}

    @staticmethod
    def _cid(value):
        return int(hashlib.sha256(str(value).encode()).hexdigest()[:11], 16)

    async def submit_order(self, order):
        try:
            try:
                self._validate_order(order)
            except ProviderError as exc:
                raise OrderNotSubmitted(str(exc)) from exc
            if self.paper:
                raise OrderNotSubmitted("Use Axiom paper execution; this adapter submits only live Bitfinex spot orders")
            if not order["symbol"].endswith("/USDT"):
                raise OrderNotSubmitted("Bitfinex live trading uses USDT-quoted exchange spot pairs only")
            pair = self._pair(order["symbol"])
            qty = self._number(order["qty"], "quantity")
            if qty != qty.quantize(Decimal("0.00000001")):
                raise OrderNotSubmitted("Bitfinex quantity supports at most 8 decimals; review the quantity")
            configs = await self._public("conf/pub:info:pair")
            info = next((r[1] for r in configs[0] if r[0] == pair[1:]), None)
            if info is None or not self._number(info[3], "minimum order size") <= qty <= self._number(info[4], "maximum order size"):
                raise OrderNotSubmitted("Bitfinex pair unavailable or quantity outside exchange size limits")
            amount = qty * (1 if order["side"].lower() == "buy" else -1)
            # A marketable fill-or-kill limit prevents unlimited execution slippage.
            reference = self._number(order.get("confirmation_price"), "confirmation price")
            if reference <= 0:
                raise OrderNotSubmitted("Fresh confirmation price is required")
            tolerance = self._number(order.get("price_bound", .005), "price bound")
            if not Decimal(0) <= tolerance <= Decimal("0.1"):
                raise OrderNotSubmitted("Bitfinex price bound must be between 0% and 10%")
            bound = reference * (1 + tolerance if amount > 0 else 1 - tolerance)
            from decimal import ROUND_FLOOR, ROUND_CEILING
            tick = max(Decimal("0.00000001"), Decimal(10) ** (bound.adjusted() - 4))
            bound = bound.quantize(tick, rounding=ROUND_FLOOR if amount > 0 else ROUND_CEILING)
            if bound <= 0:
                raise OrderNotSubmitted("Execution price is below Bitfinex precision")
        except OrderNotSubmitted:
            raise
        except ProviderError as exc:
            raise OrderNotSubmitted(str(exc)) from exc
        except (TypeError,ValueError,KeyError,IndexError):
            raise OrderNotSubmitted("Exchange order preflight could not be validated") from None
        result = await self._private("w/order/submit", type="EXCHANGE FOK", symbol=pair,
                                     amount=str(amount), price=str(bound), cid=self._cid(order["id"]),
                                     _guard=order.get("_execution_guard"))
        if not isinstance(result, list) or len(result) < 8 or result[6] != "SUCCESS":
            if isinstance(result,list) and len(result)>=8 and result[6] in {"ERROR","FAILURE"}:
                raise ExchangeRejected("Bitfinex rejected the order")
            raise ProviderError("Bitfinex did not confirm order acceptance; reconcile before retrying")
        return self._order(result[4][0])

    async def get_order(self, client_order_id):
        cid = self._cid(client_order_id)
        cached=self._order_cache.get(str(cid))
        if cached and cached["status"] in {"filled","canceled","rejected"}:
            return cached
        for path in ("r/orders", "r/orders/hist"):
            rows = await self._private(path, **({"limit": 2500} if path.endswith("hist") else {}))
            for row in rows:
                if row[2] == cid:
                    return self._order(row)
        raise ProviderError("Bitfinex order not found; manual reconciliation required")

    async def place_protection(self, symbol, qty, stop_price, client_id, guard):
        amount=self._number(qty,"protective quantity")
        stop=self._number(stop_price,"protective price")
        if amount<=0 or stop<=0 or not symbol.endswith("/USDT"):
            raise OrderNotSubmitted("Invalid protective spot order")
        amount=amount.quantize(Decimal("0.00000001"),rounding="ROUND_DOWN")
        tick=max(Decimal("0.00000001"),Decimal(10)**(stop.adjusted()-4))
        stop=stop.quantize(tick,rounding="ROUND_DOWN")
        if amount<=0 or stop<=0:
            raise OrderNotSubmitted("Protective order is below exchange precision")
        result=await self._private("w/order/submit",type="EXCHANGE STOP",symbol=self._pair(symbol),amount=str(-amount),price=str(stop),cid=self._cid(client_id),_guard=guard)
        if not isinstance(result,list) or len(result)<8 or result[6]!="SUCCESS":
            if isinstance(result,list) and len(result)>=8 and result[6] in {"ERROR","FAILURE"}:
                raise ExchangeRejected("Exchange rejected protective order")
            raise ProviderError("Protective order acceptance is uncertain")
        return self._order(result[4][0])

    async def update_protection(self, order_id, qty, stop_price, guard):
        amount=self._number(qty,"protective quantity").quantize(Decimal("0.00000001"),rounding="ROUND_DOWN")
        price=self._number(stop_price,"protective price")
        tick=max(Decimal("0.00000001"),Decimal(10)**(price.adjusted()-4))
        price=price.quantize(tick,rounding="ROUND_DOWN")
        if amount<=0 or price<=0:
            raise OrderNotSubmitted("Protective amendment is below exchange precision")
        result=await self._private("w/order/update",id=int(order_id),amount=str(-amount),price=str(price),_guard=guard)
        if not isinstance(result,list) or len(result)<8 or result[6]!="SUCCESS":
            raise ProviderError("Protective amendment is not confirmed")
        row=result[4]
        if row and isinstance(row[0],list):
            row=row[0]
        return self._order(row)

    async def get_order_by_id(self, order_id):
        if not str(order_id).isdigit():
            raise ProviderError("Invalid exchange order identity")
        for path in ("r/orders","r/orders/hist"):
            rows=await self._private(path,id=[int(order_id)])
            for row in rows:
                if str(row[0])==str(order_id):
                    return self._order(row)
        raise ProviderError("Exchange order not found; reconciliation remains blocked")

    async def cancel_order(self, order_id):
        if not str(order_id).isdigit():
            raise ProviderError("Invalid Bitfinex order ID")
        result = await self._private("w/order/cancel", id=int(order_id))
        if not isinstance(result, list) or len(result) < 8 or result[6] != "SUCCESS":
            raise ProviderError("Cancellation unconfirmed; synchronize exchange orders")
        return {"id": str(order_id), "status": "cancellation_acknowledged"}

    async def _subscribe(self, ws, symbols):
        if len(symbols)>25:
            raise ProviderError("A Bitfinex trading session supports at most 25 subscribed instruments")
        self._channels={}
        for symbol in symbols:
            await ws.send(json.dumps({"event":"subscribe","channel":"trades","symbol":self._pair(symbol)}))

    def _parse_stream(self, message):
        if isinstance(message,dict):
            if message.get("event")=="error":
                raise ProviderError("Bitfinex market subscription failed")
            if message.get("event")=="subscribed":
                self._channels[message["chanId"]]=self._symbol(message["symbol"])
            return
        if not isinstance(message,list) or len(message)<2:
            return
        symbol=self._channels.get(message[0])
        if not symbol:
            return
        payload=message[1]
        if payload=="hb":
            old=self._latest.get(symbol)
            if old and time.time()-old.timestamp>60:
                yield Quote(symbol,old.price,old.timestamp,self.name,True)
            return
        if isinstance(payload,list):
            rows=payload
        elif payload in ("te","tu") and len(message)>2:
            rows=[message[2]]
        else:
            return
        if not rows:
            return
        row=max(rows,key=lambda r:r[1])
        stamp=float(row[1])/1000
        old=self._latest.get(symbol)
        if old and stamp<=old.timestamp:
            return
        quote=Quote(symbol,_price(row[3]),stamp,self.name,time.time()-stamp>60)
        self._latest[symbol]=quote
        yield quote

    async def stream(self, symbols):
        async for quote in super().stream(symbols):
            yield quote

    async def account_events(self):
        delay=1
        while True:
            try:
                async with websockets.connect("wss://api.bitfinex.com/ws/2",open_timeout=15,close_timeout=3,ping_interval=20) as ws:
                    async with self._private_lock:
                        self._nonce=max(self._nonce+1,int(time.time()*1_000_000))
                        if self._store:
                            self._store.record("bitfinex-nonce",self._store_key,self._nonce,write=True)
                        payload="AUTH"+str(self._nonce)
                        signature=hmac.new(self.credentials["secret"].encode(),payload.encode(),hashlib.sha384).hexdigest()
                        await ws.send(json.dumps({"event":"auth","apiKey":self.credentials["key"],"authNonce":self._nonce,"authPayload":payload,"authSig":signature,"filter":["trading","wallet"]}))
                    while True:
                        message=json.loads(await asyncio.wait_for(ws.recv(),timeout=45))
                        if isinstance(message,dict) and message.get("event")=="auth":
                            if message.get("status")!="OK":
                                raise ProviderError("Bitfinex account stream authentication failed")
                            self._account_id=message.get("userId",self._account_id)
                            self._persist_history()
                            delay=1
                            yield {"type":"connected"}
                        elif isinstance(message,list) and len(message)>2 and message[0]==0:
                            kind=message[1]
                            if kind in {"on","ou","oc"}:
                                row=message[2]
                                self._order_cache[str(row[2])]=self._order(row)
                                yield {"type":"order","order":self._order(row)}
                            elif kind in {"ws","wu","te","tu","os"}:
                                yield {"type":"account_changed"}
            except asyncio.CancelledError:
                raise
            except Exception:
                yield {"type":"disconnected"}
                await asyncio.sleep(delay)
                delay=min(delay*2,30)


def provider_factory(name: str, credentials: dict | None = None, *, paper: bool = True, client=None):
    cls = {"bitfinex": BitfinexProvider}.get(name)
    if not cls:
        raise ProviderError("Only Bitfinex is supported")
    return cls(credentials, paper=paper, client=client)
