import tempfile
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
import httpx
from engine.api import create_app
from engine.core import Engine


class ProductionTests(IsolatedAsyncioTestCase):
    async def test_online_defaults_preserve_paper_data_and_refuse_fixtures(self):
        with tempfile.TemporaryDirectory() as temp:
            core=Engine(temp)
            core.paper_order("BTC/USD","BUY",1,100,"preserve-paper")
            core.close()
            app=create_app(temp,"x"*48,production=True)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://testserver",headers={"Authorization":"Bearer "+"x"*48}) as client:
                    state=(await client.get("/snapshot")).json()
                    self.assertEqual(state["mode"],"live")
                    self.assertEqual(state["data_source"],"bitfinex")
                    self.assertEqual(state["positions"],[])
                    self.assertFalse(state["running"])
                    self.assertEqual(state["execution"]["mode"],"manual")
                    self.assertEqual((await client.get("/candles?source=fixture")).status_code,400)
                    self.assertEqual((await client.post("/engine/start",json={"source":"fixture"})).status_code,400)
                    self.assertEqual((await client.post("/mode",json={"mode":"paper"})).status_code,400)
                    public=AsyncMock()
                    public.candles.return_value=[{"time":1,"open":100,"high":100,"low":100,"close":100,"volume":1}]
                    public.fetch_assets.return_value=[{"symbol":"BTC/USDT"}]
                    with patch("engine.api.provider_factory",return_value=public):
                        candles=await client.get("/candles?source=bitfinex&symbol=BTC/USDT")
                        self.assertEqual(candles.status_code,200,candles.text)
                        self.assertEqual(candles.json()["candles"][0]["source"],"bitfinex")
                        self.assertEqual((await client.get("/instruments?provider=bitfinex")).status_code,200)
                    self.assertEqual(public.close.await_count,0)  # Public adapter is reused until shutdown.
            core=Engine(temp)
            try:
                core.set_mode("paper")
                self.assertEqual(core.snapshot()["orders"][0]["id"],"preserve-paper")
            finally:
                core.close()
