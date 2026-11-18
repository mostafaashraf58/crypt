# Independent model HTTP API

Axiom connects to model services through HTTP. The optional local Chronos-Bolt Small service includes download and launch scripts. Follow [the model quickstart](MODEL-QUICKSTART.md) to use it without changing code. Models can also be replaced through the registry endpoint and strategy assignment.

## Prediction request

The registry stores a direct prediction URL, for example `http://127.0.0.1:9001/predict`. Each prediction is an HTTP `POST` with `Content-Type: application/json`. If configured, the local engine adds `Authorization: Bearer <model-api-key>` from OS credential storage.

```json
{
  "schema_version": "1.0",
  "request_id": "82d422ce-8566-4a52-a7ef-a48ca04099d7",
  "symbol": "BTC/USD",
  "interval": "1h",
  "candles": [
    {
      "time": 1793016000,
      "open": 67000,
      "high": 67300,
      "low": 66900,
      "close": 67200,
      "volume": 125.4
    }
  ],
  "features": {}
}
```

`time` is a Unix timestamp in seconds. Candles are in ascending time order and contain only observations available at the decision time. The interval describes the next completed period being predicted. Feature values, if supplied, must use the same information cutoff. A service must not infer the data source to be live merely because it receives a request.

The response is HTTP 200 with:

```json
{
  "request_id": "82d422ce-8566-4a52-a7ef-a48ca04099d7",
  "probability_up": 0.73,
  "model_version": "example-price-model-v1",
  "predicted_close": 67420,
  "prediction_lower": 66800,
  "prediction_upper": 68100,
  "interval_coverage": 0.8,
  "calibrated": false,
  "probability_method": "quantile_interpolation"
}
```

The response must echo the request ID, provide a finite numeric probability in the inclusive range `[0, 1]`, and identify a nonempty model version. The probability means the probability that the next interval's closing price exceeds the current completed interval's closing price. A probability is not a position allocation or an execution permission.

All response fields after `model_version` above are optional. Existing probability-only services remain compatible. Price fields must be finite positive numbers in the pair's quote currency. Bounds must be supplied together with `predicted_close`, and contain it. `interval_coverage`, when present, requires bounds and must be strictly between zero and one. The engine validates, returns, and persists these fields with the prediction. Historical records created before this extension retain their original probability-only results. Invalid optional fields fail the whole prediction.

The local model's point forecast is its median. Its 80% model range is formed from the 0.1 and 0.9 quantiles. The upward score uses linear interpolation of the model's quantiles, saturating at 0.1 and 0.9 in the tails. It returns `calibrated=false` because calibration on the selected market has not been measured. Missing candle periods remain missing positions in the time series rather than compressing time.

Invalid JSON, mismatched request IDs, missing versions, out-of-range probabilities, non-finite values, timeouts, and non-success HTTP responses fail the prediction. Failures must not become a default BUY signal. Existing risk controls continue to apply.

## Health check

Axiom performs `GET /health` at the origin of the prediction URL, for example `http://127.0.0.1:9001/health`:

```json
{
  "status": "ok",
  "model_version": "momentum-api-2026-10-01"
}
```

Accepted healthy status values are `ok`, `healthy`, or `ready`. A health check confirms service availability; it does not validate trading performance.

## Transport and credentials

- Plain HTTP is allowed for loopback endpoints. Remote endpoints require HTTPS.
- Redirects are disabled, preventing an endpoint from forwarding an authenticated request to another origin.
- Request timeouts are configurable between 0.1 and 60 seconds.
- Model API keys stay in OS credential storage. Registry responses, prediction history, logs, and database records must not contain the secret.
- The independent model service must implement its own access control when exposed beyond localhost.

## Registry, assignments, and history

Register the endpoint, descriptive name, version label, timeout, and optional bearer credential in the desktop AI Models screen. The offline browser preview configures metadata only and accepts no secrets. Assign the registry ID to each asset/interval strategy. Optional credentials can also be configured through the authenticated local engine API. Health and prediction errors are tracked per service. Changing a model assignment does not alter the probability-threshold rule or bypass risk controls.

For the included local service, **Connect local model** calls authenticated `POST /models/local/connect`. It verifies the service identity and readiness at `http://127.0.0.1:9001/health`, then creates or updates `local-chronos-bolt`, pins its version, and pauses the selected workspace. Connection failures leave the registry unchanged. Strategy assignments remain explicit. The browser allows 65 seconds for prediction and health requests, covering the supported model timeout of up to 60 seconds.

The local registry operation is `POST /models`, authenticated with the separate desktop session bearer token. Its JSON body accepts `id` (omit for a new record), `name`, `endpoint`, `version`, `timeout`, and optional `api_key`. `api_key` is the model-service credential; the engine stores it in the OS credential vault, never in returned registry metadata. Updating a record without an `api_key` preserves its existing saved credential. Invoke the local API only from the authorized desktop session; do not put either token in URLs, command-line arguments, or saved scripts.

Prediction history stores the request ID, input hash, output probability, and model version so that later reviews can identify which service version produced a decision. Reproducible backtests also require retaining the actual historical observations and corresponding model behavior; a version string alone cannot recreate an arbitrary remote service.

## Offline integration model

The built-in `mock-momentum` model deterministically computes a probability from recent closing-price momentum. It exists to test the interface and strategy. It is not a trained AI model, and its results must not be presented as evidence of predictive performance.

## Decision rule

For a completed interval, `q = probability_up`:

| Position | Condition | Signal |
| --- | --- | --- |
| Flat | `q >= entry_threshold` | BUY |
| Flat | `q < entry_threshold` | WAIT |
| Holding | `q >= exit_threshold` | HOLD |
| Holding | `q < exit_threshold` | SELL |

The default exit threshold equals the entry threshold. Stops, stale-data checks, cash availability, portfolio allocation, and other risk limits override this signal. Paper execution can be autonomous. Manual live drafts require separate confirmation. Activated Bitfinex USDT autonomous sessions submit new strategy decisions automatically under risk and order-budget controls; the deterministic built-in model or a real HTTP model is required; legacy offline mocks remain blocked. The model returns probabilities and never receives exchange credentials.

## Built-in momentum and direct predictions

`builtin-momentum` is an immutable local deterministic model (`builtin://momentum`, version `momentum-v1`). It accepts completed real Bitfinex candles and applies `clamp(0.5 + 7 * (last_close / first_close - 1), 0.1, 0.9)` to the most recent 20 closes. This is an uncalibrated direction score, not a trained model, guaranteed probability, or numeric next-price forecast.

`POST /models/{model_id}/predict` takes `{symbol, interval, source}` and returns `probability_up`, the last completed close, input cutoff, next target-close time, model version and source. At least 20 completed observations are required. Forming candles are excluded, stale online inputs fail closed, and prediction inputs/results are retained for inspection. The AI Models page exposes this check without trading.

Paper routes use `/workspaces/paper` before the same API paths. Live uses the root paths. Each mode has a separate model registry, strategy selection and prediction history.
