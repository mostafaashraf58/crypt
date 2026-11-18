import json
import tempfile
import unittest

import httpx

from engine.core import Engine
from engine.providers import BitfinexProvider, ProviderError


class PriceBoundTests(unittest.TestCase):
    def test_pending_buys_reserve_their_price_and_fee_buffers(self):
        with tempfile.TemporaryDirectory() as directory:
            core=Engine(directory)
            try:
                core.configure_risk({"price_bound":.1})
                core.set_mode("live")
                core.reconcile_live_account({"provider":"bitfinex","cash":215,"equity":1000},[])
                core.propose_order("BTC/USDT","BUY",1,100,source="bitfinex")
                with self.assertRaisesRegex(ValueError,"reservations"):
                    core.propose_order("ETH/USDT","BUY",1,100,source="bitfinex")
            finally:
                core.close()

    def test_persistence_validation_and_cash_checks_use_configured_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Engine(directory)
            try:
                self.assertEqual(core.snapshot()["global_risk"]["price_bound"], .005)
                for invalid in (-.01, .11, True, float("nan")):
                    with self.assertRaises(ValueError):
                        core.configure_risk({"price_bound":invalid})
                core.configure_risk({"price_bound":.1})
                core.set_mode("live")
                core.reconcile_live_account({"provider":"bitfinex", "cash":1000, "available_cash":105, "equity":1000}, [])
                with self.assertRaisesRegex(ValueError, "available exchange cash"):
                    core.propose_order("BTC/USDT", "BUY", 1, 100, source="bitfinex")
                core.configure_risk({"price_bound":0})
                draft=core.propose_order("BTC/USDT", "BUY", 1, 100, source="bitfinex")
                self.assertEqual(core.claim_proposal(draft["id"],100)["price_bound"],0)
                core.configure_risk({"price_bound":.02})
            finally:
                core.close()
            reopened=Engine(directory)
            try:
                self.assertEqual(reopened.snapshot()["global_risk"]["price_bound"],.02)
            finally:
                reopened.close()


class PriceBoundAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_buy_sell_and_zero_tolerance_reach_exchange_with_correct_prices(self):
        sent=[]
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200,json=[[["BTCUST",[0,0,0,.001,10]]]])
            body=json.loads(request.content)
            sent.append(body)
            row=[1,None,body["cid"],"tBTCUST",0,0,0,float(body["amount"]),"EXCHANGE FOK",None,None,None,0,"EXECUTED",None,None,float(body["price"]),float(body["price"])]
            return httpx.Response(200,json=[0,0,0,0,[row],0,"SUCCESS","ok"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter=BitfinexProvider({"key":"fake","secret":"fake"},client=client,paper=False)
            for side,bound,expected in (("buy",.02,102),("sell",.02,98),("buy",0,100)):
                order={"id":str(len(sent)),"symbol":"BTC/USDT","side":side,"qty":1,"confirmed":True,"confirmation_price":100,"price_bound":bound}
                await adapter.submit_order(order)
                self.assertEqual(float(sent[-1]["price"]),expected)
            with self.assertRaises(ProviderError):
                await adapter.submit_order(order | {"price_bound":.11})
            self.assertEqual(len(sent),3)
