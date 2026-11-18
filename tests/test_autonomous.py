"""Session-authorized strategy execution, exclusively with a simulated broker."""
import asyncio
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from engine.api import create_app
from engine.providers import ProviderError, Quote


class Broker:
    paper = False

    def __init__(self):
        self.cash, self.qty, self.price = 1000, 0, 100
        self.sent = []
        self.queue = asyncio.Queue()
        self.fail = False
        self.block = False
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def account(self):
        if self.block:
            self.entered.set()
            await self.release.wait()
        positions = [{"symbol":"BTC/USDT", "qty":self.qty, "available_qty":self.qty,
                      "entry_price":100, "current_price":self.price}] if self.qty else []
        return {"provider":"bitfinex", "currency":"USDT", "cash":self.cash, "available_cash":self.cash,
                "equity":self.cash+self.qty*self.price, "positions":positions, "orders":[], "reconciliation_complete":True}

    async def quote(self, symbol):
        return Quote(symbol,self.price,time.time(),"bitfinex")

    async def candles(self, *args):
        return [{"time":int(time.time()//60)*60-(30-i)*60,"open":100,"high":101,"low":99,"close":100,"volume":1} for i in range(30)]

    async def submit_order(self, order):
        order.get("_execution_guard",lambda:None)()
        self.sent.append(order.copy())
        if self.fail:
            raise ProviderError("Connection lost after sending")
        qty = order["qty"]
        if order["side"] == "BUY":
            self.qty += qty
            self.cash -= qty*self.price
        else:
            self.qty -= qty
            self.cash += qty*self.price
        return {"id":str(len(self.sent)),"status":"filled","avg_price":self.price}

    async def stream(self, symbols):
        while True:
            yield await self.queue.get()

    async def close(self):
        pass


class AutonomousTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._vault_isolation=patch("engine.api.get_credentials",return_value=None)
        self._vault_isolation.start()
        self.addCleanup(self._vault_isolation.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name,"x"*48,tick_seconds=.02)
        self.broker = Broker()
        self.core = self.app.state.core
        self.core.configure_strategy({"symbol":"BTC/USD","enabled":False})
        self.app.state.registry.add({"id":"live-model","name":"Real model", "endpoint":"http://127.0.0.1:9001/predict"})
        self.core.configure_strategy({"symbol":"BTC/USDT","model_id":"live-model","interval":"1m"})
        self.predict = patch.object(self.app.state.registry,"predict",return_value={"probability_up":.99})
        self.predict.start()
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url="http://127.0.0.1",headers={"Authorization":"Bearer "+"x"*48})
        with patch("engine.api.provider_factory",return_value=self.broker), patch("engine.api.secure_credentials"):
            self.assertEqual((await self.client.post("/connections",json={"provider":"bitfinex","key":"fake","secret":"fake","paper":False})).status_code,200)
        await self.client.post("/mode",json={"mode":"live"})

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.life.__aexit__(None,None,None)
        self.predict.stop()
        self.temp.cleanup()

    async def start(self, **updates):
        return await self.client.post("/engine/start",json={"source":"bitfinex","execution":"autonomous",
                                       "confirm_autonomous":True,"max_order_notional":20,**updates})

    async def wait_sent(self, count):
        for _ in range(150):
            if len(self.broker.sent)>=count:
                return
            await asyncio.sleep(.02)
        self.fail("Strategy did not submit expected order")

    async def test_buy_and_stop_exit_execute_without_per_order_confirmation(self):
        response = await self.start()
        self.assertEqual(response.status_code,200,response.text)
        await self.wait_sent(1)
        buy = self.broker.sent[0]
        self.assertEqual(buy["side"],"BUY")
        self.assertLessEqual(buy["qty"]*100*1.005*1.004,20)
        self.assertEqual(round(buy["qty"],8),buy["qty"])
        self.broker.price=90
        await self.broker.queue.put(Quote("BTC/USDT",90,time.time(),"bitfinex"))
        await self.wait_sent(2)
        self.assertEqual(self.broker.sent[1]["side"],"SELL")
        await self.client.post("/engine/pause")
        count = len(self.broker.sent)
        await self.broker.queue.put(Quote("BTC/USDT",80,time.time(),"bitfinex"))
        await asyncio.sleep(.1)
        self.assertEqual(len(self.broker.sent),count)
        snapshot=(await self.client.get("/snapshot")).json()
        self.assertEqual(snapshot["execution"]["mode"],"manual")
        self.assertFalse(snapshot["running"])

    async def test_uncertain_submission_disarms_and_never_retries(self):
        self.broker.fail=True
        self.assertEqual((await self.start()).status_code,200)
        await self.wait_sent(1)
        await asyncio.sleep(.15)
        snapshot=(await self.client.get("/snapshot")).json()
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["execution"]["mode"],"manual")
        self.assertEqual(snapshot["proposals"][0]["status"],"reconcile_required")
        self.assertEqual(len(self.broker.sent),1)
        self.assertEqual((await self.start()).status_code,400)

    async def test_activation_validates_authorization_budget_and_model(self):
        for changes in ({"confirm_autonomous":False},{"max_order_notional":0},{"max_order_notional":True},{"source":"fixture"}):
            self.assertEqual((await self.start(**changes)).status_code,400)
        self.core.configure_strategy({"symbol":"BTC/USDT","model_id":"mock-momentum"})
        response=await self.start()
        self.assertEqual(response.status_code,400)
        self.assertIn("mock",response.text)
        self.assertEqual(self.broker.sent,[])

    async def test_pause_cancels_activation_waiting_for_reconciliation(self):
        self.broker.block=True
        pending=asyncio.create_task(self.start())
        await asyncio.wait_for(self.broker.entered.wait(),2)
        await self.client.post("/engine/pause")
        self.broker.release.set()
        self.assertEqual((await pending).status_code,409)
        self.assertFalse(self.core.snapshot()["running"])
        self.assertEqual(self.broker.sent,[])

    async def test_restart_does_not_restore_autonomous_authorization(self):
        self.assertEqual((await self.start()).status_code,200)
        await self.wait_sent(1)
        await self.life.__aexit__(None,None,None)
        self.app=create_app(self.temp.name,"x"*48)
        self.life=self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url="http://127.0.0.1",headers={"Authorization":"Bearer "+"x"*48}) as client:
            snapshot=(await client.get("/snapshot")).json()
            self.assertEqual(snapshot["execution"]["mode"],"manual")
            self.assertFalse(snapshot["running"])
            self.assertEqual(snapshot["execution"]["max_order_notional"],0)

    async def test_custom_bound_sizes_orders_and_saving_limits_disarms(self):
        response=await self.client.put("/settings/risk",json={"price_bound":.02})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual((await self.start()).status_code,200)
        await self.wait_sent(1)
        order=self.broker.sent[0]
        self.assertEqual(order["price_bound"],.02)
        self.assertLessEqual(order["qty"]*100*1.02*1.004,20)
        response=await self.client.put("/settings/risk",json={"price_bound":0})
        self.assertEqual(response.status_code,200,response.text)
        snapshot=(await self.client.get("/snapshot")).json()
        self.assertEqual(snapshot["global_risk"]["price_bound"],0)
        self.assertEqual(snapshot["execution"]["mode"],"manual")
        self.assertFalse(snapshot["running"])
