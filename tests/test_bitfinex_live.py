"""USDT live execution contracts. All exchange requests are simulated."""
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from engine.api import create_app
from engine.providers import BitfinexProvider, ProviderError


def fill(ident, amount, price=100, fee=0, currency="UST"):
    return [ident, "tBTCUST", ident * 1000, ident, amount, price,
            "EXCHANGE FOK", price, -1, fee, currency, ident]


class BitfinexLiveTests(unittest.IsolatedAsyncioTestCase):
    async def adapter(self, wallets, trades=()):
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=[[1, int(time.time()*1000), 1, 100]])))
        self.addAsyncCleanup(client.aclose)
        adapter = BitfinexProvider({"key":"key", "secret":"secret"}, client=client, paper=False)
        async def private(path, **kwargs):
            return wallets if path == "r/wallets" else list(trades) if path == "r/trades/hist" else []
        adapter._private = AsyncMock(side_effect=private)
        return adapter

    async def test_usdt_cash_and_fee_adjusted_basis_survive_restart(self):
        trades = [fill(1, 1, fee=-.001, currency="BTC"), fill(2, -.499, fee=-.1)]
        wallets = [["exchange", "UST", 949.8, 0, 949.8], ["exchange", "BTC", .5, 0, .4]]
        for _ in range(2):  # Fresh adapters rebuild basis from exchange history.
            adapter = await self.adapter(wallets, trades)
            account = await adapter.account()
            self.assertTrue(account["reconciliation_complete"], account)
            self.assertEqual(account["currency"], "USDT")
            self.assertAlmostEqual(account["equity"], 999.8)
            self.assertAlmostEqual(account["positions"][0]["avg_entry_price"], 100/.999)
            self.assertEqual(account["positions"][0]["available_qty"], .4)

    async def test_unexplained_deposit_and_missing_availability_fail_closed(self):
        adapter = await self.adapter([["exchange", "UST", 100, 0, None], ["exchange", "BTC", 1, 0, 1]])
        account = await adapter.account()
        self.assertFalse(account["reconciliation_complete"])
        self.assertIsNone(account["equity"])
        self.assertTrue(any("history differ" in e for e in account["reconciliation_errors"]))

    async def test_changing_wallet_fails_closed(self):
        adapter = await self.adapter([])
        adapter._private = AsyncMock(side_effect=[[["exchange", "UST", 100, 0, 100]], [],
                                                [["exchange", "UST", 90, 0, 90]]])
        self.assertFalse((await adapter.account())["reconciliation_complete"])

    async def test_alias_precision_size_and_ambiguous_submission(self):
        adapter = await self.adapter([])
        self.assertEqual(adapter._pair("BTC/USDT"), "tBTCUST")
        self.assertEqual(adapter._pair("DOGE/USDT"), "tDOGE:UST")
        adapter._public = AsyncMock(return_value=[[["BTCUST", [0,0,0,.001,10]]]])
        adapter._private = AsyncMock(return_value=[0,0,0,0,[],0,"ERROR","sensitive exchange error"])
        order = {"id":"draft", "symbol":"BTC/USDT", "side":"buy", "qty":1,
                 "confirmed":True, "confirmation_price":100}
        with self.assertRaisesRegex(ProviderError, "rejected"):
            await adapter.submit_order(order)
        self.assertEqual(adapter._private.call_count, 1)
        self.assertEqual(adapter._private.call_args.kwargs["type"], "EXCHANGE FOK")
        self.assertEqual(adapter._private.call_args.kwargs["price"], "100.50")
        for qty in (.00001, 11, .123456789):
            with self.assertRaises(ProviderError):
                await adapter.submit_order(order | {"qty":qty})
        self.assertEqual(adapter._private.call_count, 1)

    async def test_partial_fill_recovery_and_cancel_acknowledgement(self):
        adapter = await self.adapter([])
        cid = adapter._cid("recovered-draft")
        row = [12, None, cid, "tBTCUST", 0, 0, .3, 1,
               "EXCHANGE LIMIT", None, None, None, 0,
               "CANCELED was: PARTIALLY FILLED @ 100", None, None, 100, 100]
        async def private(path, **kwargs):
            if path == "r/orders/hist":
                return [row]
            if path == "w/order/cancel":
                return [0, "oc-req", None, None, [row], None, "SUCCESS", "ok"]
            return []
        adapter._private = AsyncMock(side_effect=private)
        recovered = await adapter.get_order("recovered-draft")
        self.assertEqual(recovered["status"], "canceled")
        self.assertAlmostEqual(recovered["filled_qty"], .7)
        self.assertEqual((await adapter.cancel_order("12"))["status"], "cancellation_acknowledged")
        with self.assertRaises(ProviderError):
            await adapter.cancel_order("invalid")
        with self.assertRaisesRegex(ProviderError, "not found"):
            await adapter.get_order("different-draft")

    async def test_available_funds_and_locked_assets_block_confirmation(self):
        from engine.core import Engine
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory)
            try:
                engine.set_mode("live")
                engine.reconcile_live_account({"provider":"bitfinex", "currency":"USDT", "cash":1000,
                                               "available_cash":1, "equity":1100, "orders":[]},
                                              [{"symbol":"BTC/USDT", "qty":1, "available_qty":.1,
                                                "entry_price":100, "current_price":100}])
                with self.assertRaisesRegex(ValueError, "available exchange cash"):
                    engine.propose_order("BTC/USDT", "BUY", .5, 100, source="bitfinex")
                with self.assertRaisesRegex(ValueError, "sell quantity"):
                    engine.propose_order("BTC/USDT", "SELL", .2, 100, source="bitfinex")
                with self.assertRaisesRegex(ValueError, "USDT-quoted"):
                    engine.propose_order("BTC/USD", "BUY", .01, 100, source="bitfinex")
                self.assertEqual(engine.snapshot()["currency"], "USDT")
            finally:
                engine.close()

    async def test_confirmed_buy_sync_sell_and_duplicate_protection(self):
        wallets = [["exchange", "UST", 1000, 0, 1000]]
        trades = []
        adapter = await self.adapter(wallets)
        async def public(path, **kwargs):
            if "conf/" in path:
                return [[["BTCUST", [0,0,0,.001,10]]]]
            return [[1, int(time.time()*1000), 1, 100]]
        submits = []
        async def private(path, **kwargs):
            if path == "r/wallets":
                return [w[:] for w in wallets]
            if path == "r/trades/hist":
                return trades[:]
            if path == "w/order/submit":
                submits.append(kwargs)
                buying = float(kwargs["amount"]) > 0
                ident = len(submits)
                trades.append(fill(ident, 1 if buying else -.999, fee=-.001 if buying else -.1,
                                   currency="BTC" if buying else "UST"))
                wallets[:] = ([["exchange", "UST", 900, 0, 900], ["exchange", "BTC", .999, 0, .999]]
                              if buying else [["exchange", "UST", 999.8, 0, 999.8]])
                row = [ident, None, kwargs["cid"], "tBTCUST", 0, 0, 0,
                       float(kwargs["amount"]), "EXCHANGE FOK", None, None, None, 0,
                       "EXECUTED @ 100", None, None, 100, 100]
                return [0,"on-req",None,None,[row],None,"SUCCESS","ok"]
            return []
        adapter._private = AsyncMock(side_effect=private)
        adapter._public = AsyncMock(side_effect=public)
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(directory, "test-token-" + "a"*48)
            headers = {"Authorization":"Bearer test-token-" + "a"*48}
            async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", headers=headers) as client:
                with patch("engine.api.provider_factory", return_value=adapter), patch("engine.api.secure_credentials"):
                    response = await client.post("/connections", json={"provider":"bitfinex", "key":"key", "secret":"secret", "paper":False})
                    self.assertEqual(response.status_code, 200, response.text)
                await client.post("/mode", json={"mode":"live"})
                for side, qty in (("buy", 1), ("sell", .999)):
                    draft = await client.post("/proposals", json={"provider":"bitfinex", "symbol":"BTC/USDT", "side":side, "qty":qty, "price":100})
                    self.assertEqual(draft.status_code, 200, draft.text)
                    path = "/proposals/" + draft.json()["id"] + "/confirm"
                    self.assertEqual((await client.post(path, json={"provider":"bitfinex", "confirm":False})).status_code, 400)
                    response = await client.post(path, json={"provider":"bitfinex", "confirm":True})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual((await client.post(path, json={"provider":"bitfinex", "confirm":True})).status_code, 409)
                    self.assertEqual((await client.post("/connections/bitfinex/sync")).status_code, 200)
                snapshot = (await client.get("/snapshot")).json()
                self.assertEqual(snapshot["currency"], "USDT")
                self.assertAlmostEqual(snapshot["equity"], 999.8)
                self.assertEqual(snapshot["positions"], [])
                self.assertEqual(len(submits), 2)
