"""Load the pinned Chronos-Bolt checkpoint and adapt its quantile forecasts."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

MODEL_ID = "amazon/chronos-bolt-small"
MODEL_REVISION = "772f3d25d38aec6d914c8949dab4462e2d46f5d8"
MODEL_VERSION = f"chronos-bolt-small-{MODEL_REVISION[:12]}-adapter-v1"
SERVICE_ID = "axiom-chronos"
DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[1] / "work" / "local-model"
QUANTILE_LEVELS = [i / 10 for i in range(1, 10)]


def interval_seconds(interval):
    match = re.fullmatch(r"([1-9][0-9]*)([mhdw])", interval.lower())
    if not match:
        raise ValueError("Use a candle interval such as 1m, 15m, 1h or 1d")
    return int(match[1]) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match[2]]


def regular_context(candles, interval):
    """Keep missing periods as NaNs instead of shifting subsequent observations."""
    seconds = interval_seconds(interval)
    times = [c["time"] for c in candles]
    if any(right <= left or (right - left) % seconds for left, right in zip(times, times[1:])):
        raise ValueError("Candles must be ordered and aligned to the selected interval")
    by_time = {c["time"]: c["close"] for c in candles}
    start = max(times[0], times[-1] - 199 * seconds)
    context = [by_time.get(stamp, math.nan) for stamp in range(start, times[-1] + 1, seconds)]
    if sum(math.isfinite(value) for value in context) < 20:
        raise ValueError("At least 20 completed closes within the latest 200 periods are required")
    return context


def forecast_fields(values, last_close):
    if len(values) != 9 or any(not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("Model produced invalid price quantiles")
    # Rearrangement corrects crossing quantiles while retaining the forecast values.
    prices = sorted(values)
    # Invert the quantiles with linear interpolation. Tails saturate at 10/90%.
    cdf = .1 if last_close < prices[0] else .9
    equal_levels = [q for q, price in zip(QUANTILE_LEVELS, prices) if price == last_close]
    if equal_levels:
        cdf = (equal_levels[0] + equal_levels[-1]) / 2
    for index in range(8):
        low, high = prices[index:index + 2]
        if not equal_levels and low < last_close < high:
            cdf = QUANTILE_LEVELS[index] + .1 * (last_close - low) / (high - low)
            break
    return {
        "predicted_close": prices[4],
        "prediction_lower": prices[0],
        "prediction_upper": prices[8],
        "interval_coverage": .8,
        "probability_up": round(1 - cdf, 6),
        "probability_method": "quantile_interpolation",
        "calibrated": False,
    }


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_download(model_dir):
    model_dir = Path(model_dir)
    try:
        manifest = json.loads((model_dir / "ready.json").read_text(encoding="utf-8"))
        if manifest["revision"] != MODEL_REVISION or manifest["model_id"] != MODEL_ID:
            raise ValueError("Unexpected model revision")
        for name in ("config.json", "model.safetensors"):
            if file_hash(model_dir / name) != manifest["sha256"][name]:
                raise ValueError("Downloaded model file has changed")
    except (OSError, KeyError, ValueError) as exc:
        raise ValueError("Model files are missing or invalid. Run scripts/setup-model.ps1 again.") from exc


class ChronosForecaster:
    def __init__(self, model_dir=DEFAULT_MODEL_DIR):
        # Heavy packages stay outside the desktop engine and are loaded once.
        import torch
        from chronos import ChronosBoltPipeline

        torch.set_num_threads(min(4, torch.get_num_threads()))
        self.torch = torch
        self.pipeline = ChronosBoltPipeline.from_pretrained(
            str(model_dir), device_map="cpu", dtype=torch.float32,
            local_files_only=True,
        )

    def predict(self, context):
        with self.torch.inference_mode():
            quantiles, _ = self.pipeline.predict_quantiles(
                self.torch.tensor(context, dtype=self.torch.float32),
                prediction_length=1, quantile_levels=QUANTILE_LEVELS,
            )
        return forecast_fields(quantiles[0, 0].tolist(), context[-1])
