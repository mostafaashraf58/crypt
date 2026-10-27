"""Real market/account adapters. No fixture fallback and no unattended submission.

Only OS keyring stores provider secrets. All network methods are asynchronous and
raise ProviderError without echoing sensitive response bodies or authentication.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from typing import AsyncIterator
from urllib.parse import urlencode

import httpx
import websockets


class ProviderError(RuntimeError):
    pass


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
    if provider not in ("alpaca", "kraken") or not key.strip() or not secret.strip():
        raise ProviderError("A supported provider and both credential values are required")
    try:
        import keyring
        # The JSON bundle is one atomic credential record in Windows Credential Manager.
        keyring.set_password("AxiomTrading", provider, json.dumps({"key": key.strip(), "secret": secret.strip()}))
    except Exception as exc:
        raise ProviderError("OS credential storage unavailable. Credentials were not saved.") from exc


def get_credentials(provider: str) -> dict | None:
    if provider not in ("alpaca", "kraken"):
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


def _timestamp(value: str) -> float:
    # Python supports six decimal places; exchanges occasionally send nanoseconds.
    cleaned = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned).timestamp()


def _alpaca_account_symbol(row: dict) -> str:
    symbol = str(row.get("symbol","")).upper()
    if row.get("asset_class",row.get("class")) == "crypto" and "/" not in symbol:
        for currency in ("USDT","USDC","USD","EUR","GBP"):
            if symbol.endswith(currency) and len(symbol) > len(currency):
                return symbol[:-len(currency)] + "/" + currency
        raise ProviderError("Cannot normalize this Alpaca crypto account symbol; live accounting is blocked")
    return symbol


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
            raise ProviderError("Explicit manual confirmation is required for each real order")
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


class AlpacaProvider(BaseProvider):
    name = "alpaca"
    ws_url = "wss://stream.data.alpaca.markets/v2/iex"

    @property
    def base_url(self):
        return "https://paper-api.alpaca.markets" if self.paper else "https://api.alpaca.markets"

    def _headers(self):
        self._require_credentials()
        return {"APCA-API-KEY-ID": self.credentials["key"], "APCA-API-SECRET-KEY": self.credentials["secret"]}

    async def fetch_assets(self):
        rows = await self._request("GET", self.base_url + "/v2/assets", params={"status": "active"}, headers=self._headers())
        return [{"symbol": r["symbol"], "name": r.get("name", r["symbol"]), "type": "crypto" if r.get("class") == "crypto" else "stock", "tradable": True, "provider": self.name} for r in rows if r.get("tradable")]

    async def candles(self, symbol: str, interval: str = "1h", limit: int = 200):
        validate_symbol(symbol)
        seconds = parse_interval(interval)
        limit = min(max(int(limit), 1), 1000)
        # Request one-minute source data for arbitrary minute-multiple intervals.
        base_seconds = 60 if seconds < 86400 else 86400
        timeframe = "1Min" if base_seconds == 60 else "1Day"
        start = datetime.now(timezone.utc) - timedelta(seconds=seconds * (limit + 3) * (2 if "/" not in symbol else 1))
        crypto = "/" in symbol
        url = "https://data.alpaca.markets/v1beta3/crypto/us/bars" if crypto else f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
        params = {"timeframe": timeframe, "start": start.isoformat(), "limit": 10000, "sort": "asc"}
        if crypto:
            params["symbols"] = symbol
        else:
            params["feed"] = "iex"
        bars = []
        for _ in range(100):
            data = await self._request("GET", url, params=params, headers=self._headers())
            source = data.get("bars", {})
            source = source.get(symbol, []) if isinstance(source, dict) else (source or [])
            bars.extend({"time": int(_timestamp(r["t"])), "open": float(r["o"]), "high": float(r["h"]), "low": float(r["l"]), "close": float(r["c"]), "volume": float(r["v"])} for r in source)
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return resample(bars, seconds, completed_at=time.time())[-limit:]

    async def quote(self, symbol: str):
        validate_symbol(symbol)
        if "/" in symbol:
            data = await self._request("GET", "https://data.alpaca.markets/v1beta3/crypto/us/latest/trades", params={"symbols": symbol}, headers=self._headers())
            row = data["trades"][symbol]
        else:
            data = await self._request("GET", f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest", params={"feed": "iex"}, headers=self._headers())
            row = data["trade"]
        timestamp = _timestamp(row["t"])
        return Quote(symbol, _price(row["p"]), timestamp, self.name, time.time() - timestamp > 60)

    async def market_clock(self):
        return await self._request("GET", self.base_url + "/v2/clock", headers=self._headers())

    async def account(self):
        data, positions, orders = await asyncio.gather(
            self._request("GET", self.base_url + "/v2/account", headers=self._headers()),
            self._request("GET", self.base_url + "/v2/positions", headers=self._headers()),
            self._request("GET", self.base_url + "/v2/orders", params={"status": "open"}, headers=self._headers()),
        )
        positions = [{**row,"symbol":_alpaca_account_symbol(row)} for row in positions]
        orders = [{**row,"symbol":_alpaca_account_symbol(row)} for row in orders]
        return {"provider": self.name, "paper": self.paper, "currency": data.get("currency", "USD"), "cash": float(data.get("cash", 0)), "equity": float(data.get("equity", 0)), "buying_power": float(data.get("buying_power", 0)), "status": data.get("status"), "positions": positions, "orders": orders, "synced_at": time.time()}

    async def submit_order(self, order: dict):
        self._validate_order(order)
        payload = {"symbol": order["symbol"], "qty": str(order["qty"]), "side": order["side"].lower(), "type": order.get("type", "market"), "time_in_force": order.get("time_in_force", "day"), "client_order_id": order["id"]}
        if payload["type"] == "limit":
            payload["limit_price"] = str(order["limit_price"])
        return await self._request("POST", self.base_url + "/v2/orders", headers=self._headers(), json=payload)

    async def get_order(self, client_order_id: str):
        return await self._request("GET",self.base_url + "/v2/orders:by_client_order_id",headers=self._headers(),params={"client_order_id":client_order_id})

    async def _subscribe(self, ws, symbols):
        self._require_credentials()
        await ws.send(json.dumps({"action": "auth", "key": self.credentials["key"], "secret": self.credentials["secret"]}))
        await ws.send(json.dumps({"action": "subscribe", "trades": [s for s in symbols if "/" not in s]}))

    def _parse_stream(self, message):
        for row in message if isinstance(message, list) else [message]:
            if row.get("T") == "error":
                raise ProviderError("Alpaca stream authentication or subscription failed")
            if row.get("T") == "t":
                timestamp = _timestamp(row["t"])
                yield Quote(row["S"], _price(row["p"]), timestamp, self.name, time.time() - timestamp > 60)


class KrakenProvider(BaseProvider):
    name = "kraken"
    ws_url = "wss://ws.kraken.com/v2"
    base_url = "https://api.kraken.com"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._nonce = int(time.time() * 1000)
        self._private_lock = asyncio.Lock()

    @staticmethod
    def _pair(symbol):
        validate_symbol(symbol)
        return symbol.replace("BTC", "XBT").replace("/", "")

    async def _public(self, name, **params):
        data = await self._request("GET", self.base_url + "/0/public/" + name, params=params)
        if data.get("error"):
            raise ProviderError("Kraken rejected the market-data request")
        return data["result"]

    async def _private(self, name, **params):
        self._require_credentials()
        async with self._private_lock:
            self._nonce = max(self._nonce + 1, int(time.time() * 1000))
            payload = {"nonce": str(self._nonce), **params}
            path = "/0/private/" + name
            encoded = urlencode(payload)
            try:
                digest = hashlib.sha256((payload["nonce"] + encoded).encode()).digest()
                signature = base64.b64encode(hmac.new(base64.b64decode(self.credentials["secret"], validate=True), path.encode() + digest, hashlib.sha512).digest()).decode()
            except (ValueError, TypeError) as exc:
                raise ProviderError("Kraken secret is not valid base64") from exc
            data = await self._request("POST", self.base_url + path, content=encoded, headers={"API-Key": self.credentials["key"], "API-Sign": signature, "Content-Type": "application/x-www-form-urlencoded"})
            if data.get("error"):
                raise ProviderError("Kraken rejected the authenticated request; check permissions and account state")
            return data["result"]

    async def fetch_assets(self):
        pairs = await self._public("AssetPairs")
        return [{"symbol": row.get("wsname", row["altname"]).replace("XBT", "BTC"), "name": row.get("wsname", row["altname"]).replace("XBT", "BTC"), "type": "crypto", "tradable": row.get("status", "online") == "online", "provider": self.name} for row in pairs.values() if row.get("status", "online") == "online" and ".d" not in row.get("altname", "")]

    async def candles(self, symbol: str, interval: str = "1h", limit: int = 200):
        validate_symbol(symbol)
        seconds = parse_interval(interval)
        minutes = seconds // 60
        supported = [1, 5, 15, 30, 60, 240, 1440, 10080, 21600]
        # Choose a source that evenly divides target interval, then aggregate.
        source = max(x for x in supported if minutes % x == 0)
        source_needed = int(limit) * minutes // source + 1
        if source_needed > 720:
            raise ProviderError("Kraken OHLC only supplies the latest 720 source bars; reduce limit or choose a supported interval")
        data = await self._public("OHLC", pair=self._pair(symbol), interval=source)
        rows = next((v for k, v in data.items() if k != "last"), [])
        # Kraken explicitly marks the final OHLC row as not committed.
        bars = [{"time": int(r[0]), "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]), "volume": float(r[6])} for r in rows[:-1]]
        return resample(bars, seconds, completed_at=time.time())[-max(1, min(int(limit), 720)):]

    async def quote(self, symbol: str):
        data = await self._public("Trades", pair=self._pair(symbol), count=1)
        rows = next((value for key, value in data.items() if key != "last"), [])
        if not rows:
            raise ProviderError("Kraken returned no recent trades")
        row = rows[-1]
        timestamp = float(row[2])
        return Quote(symbol, _price(row[0]), timestamp, self.name, time.time() - timestamp > 60)

    async def market_clock(self):
        return {"is_open": True, "timestamp": time.time(), "calendar": "24/7 cryptocurrency spot market"}

    async def account(self):
        balance, open_orders = await asyncio.gather(self._private("Balance"), self._private("OpenOrders"))
        balances = {key: float(value) for key, value in balance.items()}
        return {"provider": self.name, "paper": False, "currency": "USD", "cash": sum(balances.get(key, 0) for key in ("USD", "ZUSD")), "equity": None, "positions": [], "balances": balances, "orders": open_orders.get("open", {}), "reconciliation_complete": False, "limitation": "Spot balances do not establish cost basis or total USD equity. Use the exchange for authoritative accounting.", "synced_at": time.time()}

    async def submit_order(self, order: dict):
        self._validate_order(order)
        if self.paper:
            raise ProviderError("Kraken has no paper account API; use Axiom paper execution")
        payload = {"pair": self._pair(order["symbol"]), "type": order["side"].lower(), "ordertype": order.get("type", "market"), "volume": str(order["qty"]), "cl_ord_id": hashlib.sha256(str(order["id"]).encode()).hexdigest()[:18]}
        if payload["ordertype"] == "limit":
            payload["price"] = str(order["limit_price"])
        return await self._private("AddOrder", **payload)

    async def _subscribe(self, ws, symbols):
        await ws.send(json.dumps({"method": "subscribe", "params": {"channel": "ticker", "symbol": [s.replace("XBT", "BTC") for s in symbols], "snapshot": True}}))

    def _parse_stream(self, message):
        if message.get("success") is False:
            raise ProviderError("Kraken stream subscription failed")
        if message.get("channel") == "ticker":
            for row in message.get("data", []):
                timestamp = _timestamp(row["timestamp"]) if row.get("timestamp") else time.time()
                yield Quote(row["symbol"], _price(row["last"]), timestamp, self.name, time.time() - timestamp > 60)


def provider_factory(name: str, credentials: dict | None = None, *, paper: bool = True, client=None):
    cls = {"alpaca": AlpacaProvider, "kraken": KrakenProvider}.get(name)
    if not cls:
        raise ProviderError("Unknown provider")
    return cls(credentials, paper=paper, client=client)
