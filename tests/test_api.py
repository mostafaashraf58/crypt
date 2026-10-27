from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from engine.api import create_app
from engine.models import ModelError, ModelRegistry, mock_prediction, validate_endpoint, validate_probability
from engine.providers import ProviderError, Quote


TOKEN = "test-session-token-" + "a" * 48
AUTH = {"Authorization":"Bearer " + TOKEN}


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name,TOKEN,tick_seconds=.02)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url="http://127.0.0.1")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None,None,None)
        self.temp.cleanup()

    async def test_auth_and_dns_rebinding_protection(self):
        self.assertEqual((await self.client.get("/health")).status_code,200)
        self.assertEqual((await self.client.get("/snapshot")).status_code,401)
        self.assertEqual((await self.client.get("/snapshot",headers={"Authorization":"Bearer wrong"})).status_code,401)
        self.assertEqual((await self.client.get("/snapshot",headers=AUTH)).status_code,200)
        self.assertEqual((await self.client.get("/health",headers={"Host":"evil.example"})).status_code,403)

    async def test_fixture_is_explicit_and_never_live(self):
        result = (await self.client.get("/candles",headers=AUTH)).json()
        self.assertEqual(result["source"],"fixture")
        self.assertEqual(len(result["candles"]),180)
        self.assertTrue(all(row["source"] == "fixture" for row in result["candles"]))
        self.assertEqual((await self.client.post("/mode",headers=AUTH,json={"mode":"live"})).status_code,200)
        self.assertEqual((await self.client.post("/engine/start",headers=AUTH,json={"source":"fixture"})).status_code,400)
        self.assertEqual((await self.client.post("/orders",headers=AUTH,json={"symbol":"BTC/USD","side":"BUY","qty":1,"price":100})).status_code,400)

    async def test_strategy_settings_and_invalid_probability(self):
        payload = {"symbol":"ETH/USD","threshold":.7,"allocation":.1,"model_id":"mock-momentum"}
        response = await self.client.put("/strategy",headers=AUTH,json=payload)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()["exit_threshold"],.7)
        self.assertEqual((await self.client.put("/strategy",headers=AUTH,json=payload|{"threshold":1.1})).status_code,422)
        self.assertEqual((await self.client.put("/strategy",headers=AUTH,json=payload|{"model_id":"missing"})).status_code,400)
        self.assertEqual((await self.client.put("/strategy",headers=AUTH,json=payload|{"interval":"nonsense"})).status_code,400)
        self.assertEqual((await self.client.put("/settings",headers=AUTH,json={"risk":{"unknown":1}})).status_code,400)

    async def test_paper_order_idempotency_and_restart(self):
        payload={"symbol":"AAPL","side":"BUY","qty":10,"price":100,"id":"paper-test-unique"}
        first=await self.client.post("/orders",headers=AUTH,json=payload)
        second=await self.client.post("/orders",headers=AUTH,json=payload)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json(),second.json())
        snap=(await self.client.get("/snapshot",headers=AUTH)).json()
        self.assertEqual(len(snap["orders"]),1)
        self.assertLess(snap["cash"],99000)

    async def test_backtest_offline_no_lookahead(self):
        response=await self.client.post("/backtest",headers=AUTH,json={"symbol":"BTC/USD","threshold":.6})
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertFalse(result["lookahead"])
        self.assertEqual(result["source"],"fixture")
        self.assertEqual(len(result["equity_curve"]),500)

    async def test_missing_provider_and_confirmation(self):
        response=await self.client.post("/engine/start",headers=AUTH,json={"source":"alpaca"})
        self.assertEqual(response.status_code,400)
        response=await self.client.post("/proposals/missing/confirm",headers=AUTH,json={"provider":"alpaca","confirm":False})
        self.assertEqual(response.status_code,400)

    async def test_models_registry_health_and_secret_omission(self):
        rows=(await self.client.get("/models",headers=AUTH)).json()["models"]
        self.assertEqual(rows[0]["id"],"mock-momentum")
        result=await self.client.post("/models/mock-momentum/health",headers=AUTH)
        self.assertEqual(result.json()["status"],"healthy")
        result=await self.client.post("/models",headers=AUTH,json={"name":"Local test","endpoint":"http://127.0.0.1:8001/predict"})
        self.assertEqual(result.status_code,200,result.text)
        self.assertNotIn("api_key",result.json())
        result=await self.client.post("/models",headers=AUTH,json={"name":"Unsafe remote","endpoint":"http://models.example/predict"})
        self.assertEqual(result.status_code,400)

    async def test_accelerated_fixture_scheduler_and_pause(self):
        response=await self.client.put("/strategy",headers=AUTH,json={"symbol":"BTC/USD","threshold":.1})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual((await self.client.post("/engine/start",headers=AUTH,json={"source":"fixture"})).status_code,200)
        deadline=time.monotonic()+1
        while time.monotonic()<deadline:
            await asyncio.sleep(.03)
            snap=(await self.client.get("/snapshot",headers=AUTH)).json()
            if len(snap["signals"])>=2:
                break
        self.assertGreaterEqual(len(snap["signals"]),2)
        self.assertGreater(len(snap["orders"]),0)
        self.assertEqual(snap["data_source"],"fixture")
        await self.client.post("/engine/pause",headers=AUTH)
        count=len((await self.client.get("/snapshot",headers=AUTH)).json()["signals"])
        await asyncio.sleep(.05)
        self.assertEqual(len((await self.client.get("/snapshot",headers=AUTH)).json()["signals"]),count)

    async def _prepare_live(self,*,fail_submit=False):
        class FakeProvider:
            paper=False
            def __init__(self): self.sent=[]
            async def account(self): return {"cash":100000,"equity":100000,"positions":[],"orders":[]}
            async def quote(self,symbol): return Quote(symbol,100,time.time(),"alpaca")
            async def candles(self,*args): return []
            async def close(self): pass
            async def stream(self,symbols):
                await asyncio.sleep(100)
                if False: yield None
            async def submit_order(self,order):
                self.sent.append(order)
                if fail_submit: raise ProviderError("Network timeout or connection failure")
                return {"id":"broker-1","client_order_id":order["id"],"status":"accepted"}
        adapter=FakeProvider()
        with patch("engine.api.provider_factory",return_value=adapter),patch("engine.api.secure_credentials"):
            response=await self.client.post("/connections",headers=AUTH,json={"provider":"alpaca","key":"fake-key","secret":"fake-secret","paper":False})
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotIn("fake-secret",response.text)
        await self.client.post("/mode",headers=AUTH,json={"mode":"live"})
        response=await self.client.post("/engine/start",headers=AUTH,json={"source":"alpaca"})
        self.assertEqual(response.status_code,200,response.text)
        core=self.app.state.core
        core.set_running(True)
        signal=core.tick("BTC/USD",100,.99,timestamp=time.time(),event_id="live-proposal-test",source="alpaca",interval_id=100)
        self.assertEqual(signal["action"],"BUY",signal)
        core.set_running(False)
        return adapter,signal["proposal"]["id"]

    async def test_live_order_requires_explicit_confirmation_and_cannot_duplicate(self):
        adapter,proposal_id=await self._prepare_live()
        self.assertEqual(adapter.sent,[])
        path=f"/proposals/{proposal_id}/confirm"
        self.assertEqual((await self.client.post(path,headers=AUTH,json={"provider":"alpaca","confirm":False})).status_code,400)
        response=await self.client.post(path,headers=AUTH,json={"provider":"alpaca","confirm":True})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(adapter.sent),1)
        self.assertTrue(adapter.sent[0]["confirmed"])
        self.assertEqual((await self.client.post(path,headers=AUTH,json={"provider":"alpaca","confirm":True})).status_code,409)
        self.assertEqual(len(adapter.sent),1)

    async def test_ambiguous_live_submission_is_durable_and_never_retried(self):
        adapter,proposal_id=await self._prepare_live(fail_submit=True)
        path=f"/proposals/{proposal_id}/confirm"
        response=await self.client.post(path,headers=AUTH,json={"provider":"alpaca","confirm":True})
        self.assertEqual(response.status_code,400,response.text)
        snap=(await self.client.get("/snapshot",headers=AUTH)).json()
        proposal=next(p for p in snap["proposals"] if p["id"]==proposal_id)
        self.assertEqual(proposal["status"],"reconcile_required")
        self.assertEqual((await self.client.post(path,headers=AUTH,json={"provider":"alpaca","confirm":True})).status_code,409)
        self.assertEqual(len(adapter.sent),1)

    async def test_manual_live_draft_requires_real_provider_and_uses_user_quantity(self):
        payload={"symbol":"AAPL","side":"buy","qty":2.5,"price":1,"id":"manual-draft-test"}
        self.assertEqual((await self.client.post("/proposals",headers=AUTH,json=payload)).status_code,400)
        await self.client.post("/mode",headers=AUTH,json={"mode":"live"})
        self.assertEqual((await self.client.post("/proposals",headers=AUTH,json=payload)).status_code,400)
        # Fixture-generated AI proposals are removed from this independent manual scenario.
        adapter,signal_id=await self._prepare_live()
        await self.client.post(f"/proposals/{signal_id}/cancel",headers=AUTH,json={})
        response=await self.client.post("/proposals",headers=AUTH,json=payload|{"provider":"alpaca"})
        self.assertEqual(response.status_code,200,response.text)
        draft=response.json()
        self.assertEqual(draft["status"],"awaiting_review")
        self.assertEqual(draft["qty"],2.5)
        self.assertEqual(draft["price"],100)  # Provider quote replaces an untrusted browser reference.
        self.assertEqual(adapter.sent,[])
        path=f"/proposals/{draft['id']}/confirm"
        response=await self.client.post(path,headers=AUTH,json={"provider":"alpaca","confirm":True})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(adapter.sent),1)
        self.assertEqual(adapter.sent[0]["qty"],2.5)

    async def test_provider_stream_stop_remains_active_when_model_engine_is_paused(self):
        queue=asyncio.Queue()
        class StreamingProvider:
            async def account(self): return {"cash":100000,"equity":100000,"positions":[],"orders":[]}
            async def quote(self,symbol): return Quote(symbol,100,time.time(),"alpaca")
            async def candles(self,*args): return []
            async def close(self): pass
            async def stream(self,symbols):
                while True:
                    yield await queue.get()
        adapter=StreamingProvider()
        with patch("engine.api.provider_factory",return_value=adapter),patch("engine.api.secure_credentials"):
            response=await self.client.post("/connections",headers=AUTH,json={"provider":"alpaca","key":"fake-key","secret":"fake-secret","paper":True})
        self.assertEqual(response.status_code,200,response.text)
        await self.client.post("/orders",headers=AUTH,json={"symbol":"BTC/USD","side":"buy","qty":1,"price":100})
        response=await self.client.post("/engine/start",headers=AUTH,json={"source":"alpaca"})
        self.assertEqual(response.status_code,200,response.text)
        await self.client.post("/engine/pause",headers=AUTH)
        await queue.put(Quote("BTC/USD",90,time.time(),"alpaca"))
        deadline=time.monotonic()+1
        while time.monotonic()<deadline:
            await asyncio.sleep(.01)
            snap=(await self.client.get("/snapshot",headers=AUTH)).json()
            if not snap["positions"]: break
        self.assertFalse(snap["running"])
        self.assertEqual(snap["positions"],[])
        self.assertEqual(snap["orders"][0]["side"],"SELL")
        self.assertIn("stop",snap["orders"][0]["reason"].lower())


class ModelContractTests(unittest.TestCase):
    def test_probability_contract(self):
        for value in [None,True,-.1,1.01,float("nan"),float("inf"),"0.5"]:
            with self.assertRaises(ModelError):
                validate_probability(value)
        self.assertEqual(validate_probability(.65),.65)

    def test_endpoint_security(self):
        self.assertEqual(validate_endpoint("http://localhost:8001/predict"),"http://localhost:8001/predict")
        for endpoint in ["http://example.com/predict","https://user:secret@example.com/predict","file:///tmp/model"]:
            with self.assertRaises(ModelError):
                validate_endpoint(endpoint)

    def test_mock_is_deterministic_and_uses_observations(self):
        candles=[{"close":100+i} for i in range(10)]
        self.assertEqual(mock_prediction(candles),mock_prediction(candles))
        self.assertGreater(mock_prediction(candles),.5)

    def test_invalid_response_and_timeout_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ModelRegistry(directory)
            model=registry.add({"name":"Test","endpoint":"http://localhost:8001/predict"})
            class Response:
                def raise_for_status(self): pass
                def json(self): return {"request_id":"wrong","probability_up":.99,"model_version":"1"}
            async def fake_post(*args,**kwargs): return Response()
            with patch("httpx.AsyncClient.post",side_effect=fake_post):
                with self.assertRaises(ModelError):
                    asyncio.run(registry.predict(model["id"],"AAPL","1h",[{"close":100}]))
            with patch("httpx.AsyncClient.post",side_effect=httpx.ReadTimeout("timeout")):
                with self.assertRaises(ModelError):
                    asyncio.run(registry.predict(model["id"],"AAPL","1h",[{"close":100}]))
            self.assertEqual(registry.history(),[])
            registry.close()


if __name__ == "__main__":
    unittest.main()
