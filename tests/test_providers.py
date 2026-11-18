"""Offline adapter contracts; no broker credentials or network are used."""
import asyncio
import base64
import hashlib
import hmac
import json
import sys
import types
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from engine.providers import (BitfinexProvider, ProviderError, get_credentials,
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
            secure_credentials("bitfinex", "key-1", "secret-1")
            self.assertEqual(get_credentials("bitfinex"), {"key": "key-1", "secret": "secret-1"})
        self.assertEqual(len(stored), 1)

    def test_unknown_provider(self):
        with self.assertRaises(ProviderError):
            provider_factory("bad")




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
            return httpx.Response(200, json=[[1,int(time.time()*1000),1,100]])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = BitfinexProvider(client=client)
            with patch("engine.providers.asyncio.sleep", new_callable=AsyncMock) as sleep:
                quote = await adapter.quote("BTC/USDT")
                self.assertEqual(quote.price, 100)
                self.assertEqual(count, 2)
                self.assertTrue(sleep.await_count >= 1)

    async def test_disconnect_yields_stale_marker(self):
        from datetime import datetime, timezone
        class Socket:
            count = 0
            async def send(self, value):
                pass
            async def recv(self):
                self.count += 1
                if self.count == 1:
                    return json.dumps({"event":"subscribed","chanId":1,"symbol":"tBTCUST"})
                if self.count == 2:
                    return json.dumps([1,[[1,int(time.time()*1000),1,100]]])
                raise OSError("offline fixture disconnect")
        socket = Socket()
        class Context:
            async def __aenter__(self):
                return socket
            async def __aexit__(self, *args):
                pass
        adapter = BitfinexProvider()
        try:
            with patch("engine.providers.websockets.connect", return_value=Context()):
                stream = adapter.stream(["BTC/USDT"])
                first = await anext(stream)
                self.assertFalse(first.stale)
                second = await anext(stream)
                self.assertTrue(second.stale)
                self.assertEqual(adapter.status, "reconnecting")
                await stream.aclose()
        finally:
            await adapter.close()


class BitfinexTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_account_and_incomplete_holdings(self):
        nonces = []
        def handler(request):
            nonce = request.headers["bfx-nonce"]
            nonces.append(int(nonce))
            signed = b"/api" + request.url.path.encode() + nonce.encode() + request.content
            self.assertEqual(request.headers["bfx-signature"], hmac.new(b"secret", signed, hashlib.sha384).hexdigest())
            return httpx.Response(200, json=[["exchange", "UST", 1000, 0, 900], ["exchange", "BTC", 1, 0, 1]] if request.url.path.endswith("wallets") else [])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = provider_factory("bitfinex", {"key":"key", "secret":"secret"}, paper=False, client=client)
            account = await adapter.account()
            self.assertEqual(account["cash"], 1000)
            self.assertEqual(account["available_cash"], 900)
            self.assertFalse(account["reconciliation_complete"])
            self.assertIsNone(account["equity"])
            self.assertEqual(nonces, sorted(set(nonces)))

    async def test_market_data_and_order_confirmation(self):
        def handler(request):
            path = request.url.path
            if "pub:info:pair" in path:
                return httpx.Response(200, json=[[["BTCUST", [0, 0, 0, .0001, 100]]]])
            if "conf/" in path:
                return httpx.Response(200, json=[["BTCUST", "DOGE:UST", "BTCUSD"]])
            if "trades/" in path:
                return httpx.Response(200, json=[[1, 1000, 1, 100]])
            if "candles/" in path:
                return httpx.Response(200, json=[[60000, 10, 11, 12, 9, 2], [0, 9, 10, 11, 8, 3]])
            payload = json.loads(request.content)
            self.assertEqual(payload["type"], "EXCHANGE FOK")
            self.assertEqual(payload["symbol"], "tBTCUST")
            self.assertEqual(payload["amount"], "-0.5")
            row = [123, None, payload["cid"], "tBTCUST"] + [0, 0, -.5, -.5, "EXCHANGE FOK", None, None, None, 0, "ACTIVE", None, None, 99.5, 0]
            return httpx.Response(200, json=[0, "on-req", None, None, [row], None, "SUCCESS", "ok"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = BitfinexProvider({"key":"key", "secret":"secret"}, client=client, paper=False)
            self.assertEqual([r["symbol"] for r in await adapter.fetch_assets()], ["BTC/USDT", "DOGE/USDT"])
            self.assertTrue((await adapter.quote("BTC/USDT")).stale)
            bars = await adapter.candles("BTC/USDT", "1m", 2)
            self.assertEqual(bars[0]["close"], 10)
            order = {"id":"draft-1", "symbol":"BTC/USDT", "side":"sell", "qty":.5, "confirmation_price":100}
            with self.assertRaises(ProviderError):
                await adapter.submit_order(order)
            result = await adapter.submit_order(order | {"confirmed":True})
            self.assertEqual(result["id"], "123")
            adapter.paper = True
            with self.assertRaises(ProviderError):
                await adapter.submit_order(order | {"confirmed":True})

    async def test_errors_are_sanitized(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=["error", 100, "secret detail"]))) as client:
            adapter = BitfinexProvider(client=client)
            with self.assertRaisesRegex(ProviderError, "Bitfinex rejected"):
                await adapter.fetch_assets()
