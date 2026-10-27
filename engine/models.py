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
            CREATE TABLE IF NOT EXISTS predictions(id TEXT PRIMARY KEY,model_id TEXT,symbol TEXT,interval TEXT,probability REAL,version TEXT,created REAL,input_hash TEXT);
        """)
        self.db.execute("INSERT OR IGNORE INTO models VALUES (?,?,?,?,?,?)", ("mock-momentum", "Momentum fixture", "mock://momentum", "fixture-v1", 5, "healthy"))
        self.db.commit()

    def list(self):
        return [dict(row) | {"kind": "mock" if row["endpoint"].startswith("mock:") else "http"} for row in self.db.execute("SELECT * FROM models ORDER BY rowid")]

    def get(self, model_id):
        row = self.db.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        if not row:
            raise ModelError("Unknown model")
        return dict(row)

    def add(self, config: dict):
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
        self.db.execute("INSERT INTO models VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,endpoint=excluded.endpoint,version=excluded.version,timeout=excluded.timeout,status='unchecked'", (model_id,name,endpoint,version,timeout,"unchecked"))
        self.db.commit()
        return self.get(model_id)

    def _headers(self, model_id):
        try:
            import keyring
            token = keyring.get_password("axiom-models", model_id)
        except Exception:
            token = None
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def health(self, model_id):
        model = self.get(model_id)
        start = time.monotonic()
        if model["endpoint"].startswith("mock:"):
            status, version = "healthy", "fixture-v1"
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
        if model["endpoint"].startswith("mock:"):
            q, version = mock_prediction(candles), "fixture-v1"
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
            except ModelError:
                raise
            except Exception as exc:
                raise ModelError("Prediction endpoint failed or timed out") from exc
        digest = hashlib.sha256(json.dumps(candles,sort_keys=True).encode()).hexdigest()
        self.db.execute("INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?)",(request_id,model_id,symbol,interval,q,version,time.time(),digest))
        self.db.execute("UPDATE models SET status='healthy',version=? WHERE id=?",(version,model_id))
        self.db.commit()
        return {"request_id":request_id,"probability_up":q,"model_version":version,"model_id":model_id}

    def history(self, limit=100):
        return [dict(row) for row in self.db.execute("SELECT * FROM predictions ORDER BY created DESC LIMIT ?",(min(1000,max(1,limit)),))]

    def close(self):
        self.db.close()

    def remove(self, model_id):
        if model_id == "mock-momentum":
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
