"""Forecast extension, migration and local-service contracts without downloading weights."""
import math
import sqlite3
import tempfile
import time
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import httpx

from engine.api import create_app
from engine.models import ModelError, ModelRegistry, validate_forecast
from model_service.forecast import MODEL_VERSION, forecast_fields, regular_context
from model_service.server import create_app as create_model_app


def candles():
    end = int(time.time() // 3600) * 3600 - 3600
    return [{"time": end - (39 - i) * 3600, "open": 100., "high": 102.,
             "low": 99., "close": 101., "volume": 1.} for i in range(40)]


class ForecastValidationTests(TestCase):
    def test_optional_fields_and_invalid_prices(self):
        self.assertEqual(validate_forecast({"probability_up": .6}), {})
        valid = {"predicted_close": 100, "prediction_lower": 90, "prediction_upper": 110,
                 "interval_coverage": .8, "calibrated": False}
        self.assertEqual(validate_forecast(valid), valid)
        for change in ({"predicted_close": True}, {"predicted_close": math.inf},
                       {"predicted_close": 0}, {"prediction_lower": 105},
                       {"prediction_upper": None}, {"interval_coverage": 1},
                       {"calibrated": "false"}):
            with self.subTest(change=change), self.assertRaises(ModelError):
                validate_forecast(valid | change)
        with self.assertRaises(ModelError):
            validate_forecast({"prediction_lower": 90})

    def test_quantile_inversion_and_missing_periods(self):
        self.assertEqual(forecast_fields([100.] * 9, 100.)["probability_up"], .5)
        self.assertEqual(forecast_fields(list(range(91, 100)), 95.5)["probability_up"], .45)
        self.assertEqual(forecast_fields(list(range(91, 100)), 80)["probability_up"], .9)
        self.assertEqual(forecast_fields(list(range(91, 100)), 110)["probability_up"], .1)
        rows = candles()
        del rows[20]
        context = regular_context(rows, "1h")
        self.assertEqual(len(context), 40)
        self.assertTrue(math.isnan(context[20]))
        with self.assertRaises(ValueError):
            regular_context(list(reversed(rows)), "1h")


class LocalModelTests(IsolatedAsyncioTestCase):
    async def test_old_registry_migrates_and_persists_forecast(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(directory + "/models.sqlite3")
            db.execute("CREATE TABLE predictions(id TEXT PRIMARY KEY,model_id TEXT,symbol TEXT,interval TEXT,probability REAL,version TEXT,created REAL,input_hash TEXT)")
            db.execute("INSERT INTO predictions VALUES ('old','test','BTC/USDT','1h',.5,'v0',0,'hash')")
            db.commit()
            db.close()
            registry = ModelRegistry(directory)
            model = registry.add({"name": "Price model", "endpoint": "http://127.0.0.1:9001/predict"})

            async def respond(client, url, **kwargs):
                return httpx.Response(200, json={"request_id": kwargs["json"]["request_id"],
                    "probability_up": .6, "model_version": "v1", "predicted_close": 102,
                    "prediction_lower": 99, "prediction_upper": 104, "interval_coverage": .8,
                    "calibrated": False}, request=httpx.Request("POST", url))

            with patch.object(httpx.AsyncClient, "post", respond):
                result = await registry.predict(model["id"], "BTC/USDT", "1h", candles())
            self.assertEqual(result["predicted_close"], 102)
            registry.close()
            registry = ModelRegistry(directory)
            self.assertEqual(registry.history()[0]["prediction_upper"], 104)
            self.assertNotIn("predicted_close", registry.history()[1])
            registry.close()

    async def test_service_validates_requests_and_uses_pretrained_adapter(self):
        class Forecaster:
            calls = []
            def predict(self, context):
                self.calls.append(context)
                return forecast_fields([97., 98., 99., 100., 101., 102., 103., 104., 105.], context[-1])

        model = Forecaster()
        app = create_model_app(model)
        payload = {"schema_version": "1.0", "request_id": "check", "symbol": "BTC/USDT",
                   "interval": "1h", "candles": candles()}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://model") as client:
                self.assertEqual((await client.get("/health")).json()["model_version"], MODEL_VERSION)
                result = await client.post("/predict", json=payload)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["request_id"], "check")
                self.assertEqual(result.json()["predicted_close"], 101.)
                self.assertFalse(result.json()["calibrated"])
                bad = payload | {"candles": list(reversed(payload["candles"]))}
                self.assertEqual((await client.post("/predict", json=bad)).status_code, 422)
                bad = payload | {"candles": [row | {"time": row["time"] + 86400} for row in candles()]}
                self.assertEqual((await client.post("/predict", json=bad)).status_code, 422)
                self.assertEqual(len(model.calls), 1)

    async def test_connect_and_price_forecast_through_paper_workspace(self):
        token = "forecast-test-" + "x" * 48
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(directory, token, production=True, tick_seconds=100)
            async def health(client, url, **kwargs):
                return httpx.Response(200, json={"status": "ready", "service": "axiom-chronos", "model_version": MODEL_VERSION}, request=httpx.Request("GET", url))
            async def response(client, url, **kwargs):
                return httpx.Response(200, json={"request_id": kwargs["json"]["request_id"], "model_version": MODEL_VERSION,
                    **forecast_fields([97., 98., 99., 100., 101., 102., 103., 104., 105.], 101.)}, request=httpx.Request("POST", url))
            class PublicMarket:
                async def candles(self, *args): return candles()
                async def close(self): pass
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver", headers={"Authorization": "Bearer " + token}) as client:
                    with patch.object(httpx.AsyncClient, "get", AsyncMock(side_effect=httpx.ConnectError("offline"))):
                        self.assertEqual((await client.post("/workspaces/paper/models/local/connect")).status_code, 400)
                    with patch.object(httpx.AsyncClient, "get", health):
                        # Use request() so only the model health call is intercepted.
                        result = await client.request("POST", "/workspaces/paper/models/local/connect")
                        self.assertEqual(result.status_code, 200, result.text)
                        self.assertEqual(result.json()["approved_version"], MODEL_VERSION)
                    with patch("engine.api.provider_factory", return_value=PublicMarket()), patch.object(httpx.AsyncClient, "post", response):
                        result = await client.request("POST", "/workspaces/paper/models/local-chronos-bolt/predict", json={"symbol": "BTC/USDT", "interval": "1h", "source": "bitfinex"})
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertEqual(result.json()["predicted_close"], 101.)
                    self.assertFalse(result.json()["calibrated"])
                    snapshot = (await client.get("/workspaces/paper/snapshot")).json()
                    self.assertEqual(snapshot["orders"], [])
                    self.assertFalse(snapshot["running"])
                    live = (await client.get("/models")).json()["models"]
                    self.assertFalse(any(m["id"] == "local-chronos-bolt" for m in live))
                    with patch.object(httpx.AsyncClient, "get", health):
                        await client.request("POST", "/workspaces/paper/models/local/connect")
                    models = (await client.get("/workspaces/paper/models")).json()["models"]
                    self.assertEqual(sum(m["id"] == "local-chronos-bolt" for m in models), 1)
