"""Loopback-only HTTP adapter for the pretrained price forecasting model."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from threading import Lock
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .forecast import (
    ChronosForecaster, DEFAULT_MODEL_DIR, MODEL_VERSION, SERVICE_ID,
    interval_seconds, regular_context, verify_download,
)


class Candle(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, strict=True)
    time: int = Field(ge=0, strict=True)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)

    @model_validator(mode="after")
    def valid_range(self):
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("Invalid candle price range")
        return self


class PredictionRequest(BaseModel):
    schema_version: Literal["1.0"]
    request_id: str = Field(min_length=1, max_length=200)
    symbol: str = Field(min_length=1, max_length=100)
    interval: str = Field(min_length=2, max_length=10)
    candles: list[Candle] = Field(min_length=20, max_length=2000)
    features: dict = Field(default_factory=dict)


def create_app(forecaster=None, model_dir=DEFAULT_MODEL_DIR):
    @asynccontextmanager
    async def lifespan(app):
        if forecaster is None:
            verify_download(model_dir)
            app.state.forecaster = ChronosForecaster(model_dir)
            app.state.forecaster.predict([100 + i * .05 for i in range(200)])
        else:
            app.state.forecaster = forecaster
        yield

    app = FastAPI(title="Axiom local model", lifespan=lifespan)
    inference_lock = Lock()

    @app.get("/health")
    def health():
        return {"status": "ready", "service": SERVICE_ID, "model_version": MODEL_VERSION}

    @app.post("/predict")
    def predict(payload: PredictionRequest):
        try:
            seconds = interval_seconds(payload.interval)
            rows = [c.model_dump() for c in payload.candles]
            if rows[-1]["time"] + seconds > time.time():
                raise ValueError("Only completed candles may be used")
            context = regular_context(rows, payload.interval)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        # Fail promptly when another request is running instead of building a queue.
        if not inference_lock.acquire(blocking=False):
            raise HTTPException(503, "Model is busy. Try again after the current forecast.")
        try:
            result = app.state.forecaster.predict(context)
        except Exception as exc:
            raise HTTPException(503, "Model inference failed. Check the model service window.") from exc
        finally:
            inference_lock.release()
        return {"request_id": payload.request_id, "model_version": MODEL_VERSION, **result}

    return app


def main():
    import uvicorn

    print("Loading the local model. Wait for 'Application startup complete'.", flush=True)
    uvicorn.run(create_app(), host="127.0.0.1", port=9001, access_log=False)


if __name__ == "__main__":
    main()
