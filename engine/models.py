"""Independent prediction API contract, local registry, and deterministic test model."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx


class ModelError(ValueError):
    pass


def validate_probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ModelError("probability_up must be a finite number between 0 and 1")
    return float(value)


def validate_forecast(body):
    """Optional price fields extend the existing probability-only contract."""
    forecast = {}
    for name in ("predicted_close", "prediction_lower", "prediction_upper"):
        if name in body:
            value = body[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ModelError(f"{name} must be a finite positive price")
            forecast[name] = float(value)
    bounds = {"prediction_lower", "prediction_upper"} & forecast.keys()
    if bounds:
        if len(bounds) != 2 or "predicted_close" not in forecast:
            raise ModelError("A price range requires both bounds and predicted_close")
        if not forecast["prediction_lower"] <= forecast["predicted_close"] <= forecast["prediction_upper"]:
            raise ModelError("Price range must contain predicted_close")
    if "interval_coverage" in body:
        coverage = validate_probability(body["interval_coverage"])
        if not bounds or not 0 < coverage < 1:
            raise ModelError("interval_coverage requires a price range and must be between 0 and 1")
        forecast["interval_coverage"] = coverage
    if "calibrated" in body:
        if not isinstance(body["calibrated"], bool):
            raise ModelError("calibrated must be a boolean")
        forecast["calibrated"] = body["calibrated"]
    if "probability_method" in body:
        method = body["probability_method"]
        if not isinstance(method, str) or not method.strip() or len(method) > 100:
            raise ModelError("probability_method must be a short nonempty string")
        forecast["probability_method"] = method
    return forecast


def validate_endpoint(endpoint: str):
    parsed = urlparse(endpoint)
    if parsed.username or parsed.password or parsed.fragment or parsed.query or not parsed.hostname:
        raise ModelError("Endpoint must be an absolute URL without embedded credentials")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ModelError("Remote model APIs require HTTPS; HTTP is allowed only on loopback")
    return endpoint.rstrip("/")


def mock_prediction(candles):
    """No training or future observations: a stable momentum fixture."""
    if not candles:
        return .5
    closes = [float(c["close"]) for c in candles[-20:]]
    if any(not math.isfinite(c) or c <= 0 for c in closes):
        raise ModelError("Candle closes must be finite and positive")
    momentum = (closes[-1] / closes[0] - 1) if len(closes) > 1 else 0
    return round(max(.1, min(.9, .5 + momentum * 7)), 6)


class ModelRegistry:
    def __init__(self, data_dir: Path):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data_dir / "models.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY,name TEXT,endpoint TEXT,version TEXT,timeout REAL,status TEXT);
            CREATE TABLE IF NOT EXISTS prediction_inputs(id TEXT PRIMARY KEY,payload BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS predictions(id TEXT PRIMARY KEY,model_id TEXT,symbol TEXT,interval TEXT,probability REAL,version TEXT,created REAL,input_hash TEXT);
        """)
        self.db.execute("INSERT OR IGNORE INTO models(id,name,endpoint,version,timeout,status) VALUES (?,?,?,?,?,?)", ("mock-momentum", "Momentum fixture", "mock://momentum", "fixture-v1", 5, "healthy"))
        self.db.execute("INSERT OR IGNORE INTO models(id,name,endpoint,version,timeout,status) VALUES (?,?,?,?,?,?)", ("builtin-momentum", "Deterministic momentum", "builtin://momentum", "momentum-v1", 5, "healthy"))
        columns={r[1] for r in self.db.execute("PRAGMA table_info(models)")}
        if "approved_version" not in columns:
            self.db.execute("ALTER TABLE models ADD COLUMN approved_version TEXT")
        prediction_columns = {r[1] for r in self.db.execute("PRAGMA table_info(predictions)")}
        if "forecast" not in prediction_columns:
            self.db.execute("ALTER TABLE predictions ADD COLUMN forecast TEXT")
        self.db.commit()

    def list(self):
        return [dict(row) | {"kind": "deterministic" if row["endpoint"].startswith("builtin:") else "mock" if row["endpoint"].startswith("mock:") else "http"} for row in self.db.execute("SELECT * FROM models ORDER BY rowid")]

    def get(self, model_id):
        row = self.db.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        if not row:
            raise ModelError("Unknown model")
        return dict(row)

    def add(self, config: dict):
        if config.get("id") in {"mock-momentum", "builtin-momentum"}:
            raise ModelError("Built-in deterministic models cannot be replaced")
        name = str(config.get("name", "")).strip()[:100]
        if not name:
            raise ModelError("A model name is required")
        endpoint = validate_endpoint(str(config.get("endpoint", "")))
        timeout = float(config.get("timeout", 5))
        if not math.isfinite(timeout) or not .1 <= timeout <= 60:
            raise ModelError("Timeout must be between 0.1 and 60 seconds")
        model_id = str(config.get("id") or uuid.uuid4())
        if model_id == "mock-momentum":
            raise ModelError("The fixture model cannot be replaced")
        version = str(config.get("version", "unverified"))[:100]
        # A bearer secret is stored only in the operating system credential vault.
        if config.get("api_key"):
            try:
                import keyring
                keyring.set_password("axiom-models", model_id, str(config["api_key"]))
            except Exception as exc:
                raise ModelError("Operating system credential storage is unavailable") from exc
        self.db.execute("INSERT INTO models(id,name,endpoint,version,timeout,status) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,endpoint=excluded.endpoint,version=excluded.version,timeout=excluded.timeout,status='unchecked'", (model_id,name,endpoint,version,timeout,"unchecked"))
        if "approved_version" in config:
            self.db.execute("UPDATE models SET approved_version=? WHERE id=?",(config.get("approved_version") or None,model_id))
        self.db.commit()
        return self.get(model_id)

    def _headers(self, model_id):
        try:
            import keyring
            token = keyring.get_password("axiom-models", model_id)
        except Exception:
            token = None
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def connect_local(self):
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                response = await client.get("http://127.0.0.1:9001/health")
                response.raise_for_status()
                body = response.json()
            version = body.get("model_version")
            if body.get("status") != "ready" or body.get("service") != "axiom-chronos" or not isinstance(version, str) or not version or len(version) > 100:
                raise ValueError("Unexpected local service")
        except Exception as exc:
            raise ModelError("Local model is unavailable. Run scripts/start-model.ps1 and wait for startup to complete, then connect again.") from exc
        model = self.add({
            "id": "local-chronos-bolt", "name": "Chronos-Bolt Small (local)",
            "endpoint": "http://127.0.0.1:9001/predict", "version": version,
            "approved_version": version, "timeout": 60,
        })
        self.db.execute("UPDATE models SET status='healthy' WHERE id=?", (model["id"],))
        self.db.commit()
        return self.get(model["id"])

    async def health(self, model_id):
        model = self.get(model_id)
        start = time.monotonic()
        if model["endpoint"].startswith(("mock:", "builtin:")):
            status, version = "healthy", "momentum-v1" if model["endpoint"].startswith("builtin:") else "fixture-v1"
        else:
            parsed = urlparse(model["endpoint"])
            origin = f"{parsed.scheme}://{parsed.netloc}"
            try:
                async with httpx.AsyncClient(timeout=model["timeout"], follow_redirects=False) as client:
                    response = await client.get(origin + "/health", headers=self._headers(model_id))
                    response.raise_for_status()
                    body = response.json()
                    if body.get("status") not in {"ok", "healthy", "ready"}:
                        raise ModelError("Model health endpoint did not report ready")
                    status = "healthy"
                    version = str(body.get("model_version", model["version"]))[:100]
            except Exception as exc:
                self.db.execute("UPDATE models SET status='unhealthy' WHERE id=?",(model_id,))
                self.db.commit()
                raise ModelError("Model health check failed or timed out") from exc
        self.db.execute("UPDATE models SET status=?,version=? WHERE id=?",(status,version,model_id))
        self.db.commit()
        return {"id":model_id,"status":status,"model_version":version,"latency_ms":round((time.monotonic()-start)*1000,2)}

    async def predict(self, model_id, symbol, interval, candles):
        model = self.get(model_id)
        request_id = str(uuid.uuid4())
        payload = {"schema_version":"1.0","request_id":request_id,"symbol":symbol,"interval":interval,"candles":candles,"features":{}}
        forecast = {}
        if model["endpoint"].startswith(("mock:", "builtin:")):
            q, version = mock_prediction(candles), "momentum-v1" if model["endpoint"].startswith("builtin:") else "fixture-v1"
        else:
            try:
                async with httpx.AsyncClient(timeout=model["timeout"], follow_redirects=False) as client:
                    response = await client.post(model["endpoint"],json=payload,headers=self._headers(model_id))
                    response.raise_for_status()
                    body = response.json()
                if body.get("request_id") != request_id:
                    raise ModelError("Model must echo the request_id")
                q = validate_probability(body.get("probability_up"))
                version = str(body.get("model_version", ""))[:100]
                if not version:
                    raise ModelError("Model response requires model_version")
                if model.get("approved_version") and version!=model["approved_version"]:
                    raise ModelError("Model version differs from the approved production version")
                forecast = validate_forecast(body)
            except ModelError:
                raise
            except Exception as exc:
                raise ModelError("Prediction endpoint failed or timed out") from exc
        import zlib
        self.db.execute("INSERT INTO prediction_inputs VALUES (?,?)",(request_id,zlib.compress(json.dumps(payload,sort_keys=True).encode())))
        digest = hashlib.sha256(json.dumps(candles,sort_keys=True).encode()).hexdigest()
        self.db.execute("INSERT INTO predictions(id,model_id,symbol,interval,probability,version,created,input_hash,forecast) VALUES (?,?,?,?,?,?,?,?,?)",(request_id,model_id,symbol,interval,q,version,time.time(),digest,json.dumps(forecast)))
        self.db.execute("UPDATE models SET status='healthy',version=? WHERE id=?",(version,model_id))
        self.db.commit()
        return {"request_id":request_id,"probability_up":q,"model_version":version,"model_id":model_id,**forecast}

    def history(self, limit=100):
        rows = self.db.execute("SELECT * FROM predictions ORDER BY created DESC LIMIT ?",(min(1000,max(1,limit)),))
        records = []
        for row in rows:
            record = dict(row)
            forecast = record.pop("forecast")
            records.append(record | json.loads(forecast or "{}"))
        return records

    def close(self):
        self.db.close()

    def remove(self, model_id):
        if model_id in {"mock-momentum", "builtin-momentum"}:
            raise ModelError("The deterministic fixture model cannot be removed")
        self.get(model_id)
        self.db.execute("DELETE FROM models WHERE id=?",(model_id,))
        self.db.commit()
        try:
            import keyring
            if keyring.get_password("axiom-models",model_id):
                keyring.delete_password("axiom-models",model_id)
        except Exception:
            pass
        return {"id":model_id,"removed":True}
