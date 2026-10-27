"""Offline adapter contracts; no broker credentials or network are used."""
import asyncio
import base64
import hashlib
import hmac
import json
import sys
import types
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from engine.providers import (AlpacaProvider, KrakenProvider, ProviderError, get_credentials,
                              parse_interval, provider_factory, resample, secure_credentials)


class ProviderUnitTests(unittest.TestCase):
    def test_intervals_and_invalid(self):
        self.assertEqual(parse_interval("4h"), 14400)
        self.assertEqual(parse_interval("7m"), 420)
        for value in ("0m", "1s", "-1h", "60", "1.5h", "99999d"):
            with self.assertRaises(ProviderError):
                parse_interval(value)

    def test_resample_excludes_unfinished_bucket(self):
        rows = [{"time": i * 60, "open": 10+i, "high": 12+i, "low": 9+i, "close": 11+i, "volume": 2} for i in range(7)]
        result = resample(rows, 300, completed_at=420)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], {"time": 0, "open": 10, "high": 16, "low": 9, "close": 15, "volume": 10})

    def test_os_credentials_only(self):
        stored = {}
        fake = types.SimpleNamespace(set_password=lambda app, name, value: stored.update({(app, name): value}), get_password=lambda app, name: stored.get((app, name)))
        with patch.dict(sys.modules, {"keyring": fake}):
            secure_credentials("alpaca", "key-1", "secret-1")
            self.assertEqual(get_credentials("alpaca"), {"key": "key-1", "secret": "secret-1"})
        self.assertEqual(len(stored), 1)

    def test_unknown_provider(self):
        with self.assertRaises(ProviderError):
            provider_factory("bad")


class ProviderAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_alpaca_assets_and_account(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.headers["APCA-API-KEY-ID"], "fixture-key")
            if request.url.path == "/v2/assets":
                return httpx.Response(200, json=[{"symbol": "AAPL", "name": "Apple", "tradable": True}, {"symbol": "NO", "tradable": False}])
            if request.url.path == "/v2/account":
                return httpx.Response(200, json={"cash": "100", "equity": "120", "status": "ACTIVE"})
            return httpx.Response(200, json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = AlpacaProvider({"key": "fixture-key", "secret": "fixture-secret"}, client=client)
            self.assertEqual((await adapter.fetch_assets())[0]["symbol"], "AAPL")
            self.assertEqual(len(await adapter.fetch_assets()), 1)
            self.assertEqual((await adapter.account())["cash"], 100)
            self.assertTrue(all(r.url.host == "paper-api.alpaca.markets" for r in requests))

    async def test_unauthenticated_alpaca_fails(self):
        adapter = AlpacaProvider()
        try:
            with self.assertRaises(ProviderError):
                await adapter.account()
        finally:
            await adapter.close()

    async def test_no_automatic_real_order_and_no_mutating_retry(self):
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(500, json={"message": "secret-server-response"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = AlpacaProvider({"key": "k", "secret": "s"}, client=client, paper=False)
            order = {"symbol": "AAPL", "side": "buy", "qty": 1, "id": "unique-order"}
            with self.assertRaises(ProviderError):
                await adapter.submit_order(order)
            self.assertEqual(count, 0)
            with self.assertRaises(ProviderError) as error:
                await adapter.submit_order({**order, "confirmed": True})
            self.assertNotIn("secret-server-response", str(error.exception))
            self.assertEqual(count, 1, "Real order submissions must never replay after ambiguous server failure")

    async def test_order_payload_contains_unique_id_not_confirmation(self):
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["client_order_id"], "manual-1")
            self.assertNotIn("confirmed", payload)
            self.assertEqual(payload["side"], "buy")
            return httpx.Response(200, json={"id": "broker-order", "status": "accepted"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = AlpacaProvider({"key": "k", "secret": "s"}, client=client)
            result = await adapter.submit_order({"symbol": "AAPL", "side": "BUY", "qty": 2, "id": "manual-1", "confirmed": True})
            self.assertEqual(result["status"], "accepted")

    async def test_kraken_ohlc_omits_unclosed_and_resamples(self):
        def handler(request):
            self.assertEqual(request.url.params["pair"], "XBTUSD")
            self.assertEqual(request.url.params["interval"], "5")
            bars = [[i*300, "100", "110", "90", str(101+i), "100", "2", 1] for i in range(6)]
            return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": bars, "last": 1500}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = KrakenProvider(client=client)
            candles = await adapter.candles("BTC/USD", "5m", 10)
            self.assertEqual(len(candles), 5)
            self.assertEqual(candles[-1]["time"], 1200)

    async def test_kraken_auth_signature_and_monotonic_nonce(self):
        secret = base64.b64encode(b"fixture-secret").decode()
        nonces = []
        def handler(request):
            payload = parse_qs(request.content.decode())
            nonce = payload["nonce"][0]
            nonces.append(int(nonce))
            digest = hashlib.sha256(nonce.encode() + request.content).digest()
            expected = base64.b64encode(hmac.new(b"fixture-secret", request.url.path.encode()+digest, hashlib.sha512).digest()).decode()
            self.assertEqual(request.headers["API-Sign"], expected)
            result = {"ZUSD": "100"} if request.url.path.endswith("Balance") else {"open": {}}
            return httpx.Response(200, json={"error": [], "result": result})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = KrakenProvider({"key": "fixture-key", "secret": secret}, client=client, paper=False)
            account = await adapter.account()
            self.assertEqual(account["balances"]["ZUSD"], 100)
            self.assertLess(nonces[0], nonces[1])

    async def test_kraken_history_limit_and_errors(self):
        adapter = KrakenProvider()
        try:
            with self.assertRaises(ProviderError):
                await adapter.candles("BTC/USD", "7m", 200)
            with self.assertRaises(ProviderError):
                await adapter.submit_order({"symbol": "BTC/USD", "qty": 1, "side": "buy", "confirmed": True, "id": "manual-1"})
        finally:
            await adapter.close()

    async def test_stream_parsing_stale_markers(self):
        adapter = AlpacaProvider()
        try:
            row = {"T": "t", "S": "AAPL", "p": 100, "t": "2020-01-01T00:00:00Z"}
            quote = list(adapter._parse_stream([row]))[0]
            self.assertTrue(quote.stale)
            self.assertEqual(quote.symbol, "AAPL")
            with self.assertRaises(ProviderError):
                list(adapter._parse_stream([{"T": "error", "msg": "secret"}]))
        finally:
            await adapter.close()


if __name__ == "__main__":
    unittest.main()

class RetryAndStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_retries_after_429(self):
        from unittest.mock import AsyncMock
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(429, headers={"Retry-After": "0.1"})
            return httpx.Response(200, json={"error": [], "result": {"XBTUSD": [["100", "1", 1000.0, "b", "m", ""]], "last": "1000000000000"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = KrakenProvider(client=client)
            with patch("engine.providers.asyncio.sleep", new_callable=AsyncMock) as sleep:
                quote = await adapter.quote("BTC/USD")
                self.assertEqual(quote.price, 100)
                self.assertEqual(count, 2)
                sleep.assert_awaited_once()

    async def test_disconnect_yields_stale_marker(self):
        from datetime import datetime, timezone
        class Socket:
            count = 0
            async def send(self, value):
                pass
            async def recv(self):
                self.count += 1
                if self.count == 1:
                    return json.dumps([{"T": "t", "S": "AAPL", "p": 100, "t": datetime.now(timezone.utc).isoformat()}])
                raise OSError("offline fixture disconnect")
        socket = Socket()
        class Context:
            async def __aenter__(self):
                return socket
            async def __aexit__(self, *args):
                pass
        adapter = AlpacaProvider({"key": "key", "secret": "secret"})
        try:
            with patch("engine.providers.websockets.connect", return_value=Context()):
                stream = adapter.stream(["AAPL"])
                first = await anext(stream)
                self.assertFalse(first.stale)
                second = await anext(stream)
                self.assertTrue(second.stale)
                self.assertEqual(adapter.status, "reconnecting")
                await stream.aclose()
        finally:
            await adapter.close()
