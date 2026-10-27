# Independent model HTTP API

Axiom is a client of separately operated model services. It does not train, host, or bundle a trained AI model. Models can be replaced by changing their registry endpoint and asset assignment, without modifying the strategy logic.

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
  "model_version": "momentum-api-2026-10-01"
}
```

The response must echo the request ID, provide a finite numeric probability in the inclusive range `[0, 1]`, and identify a nonempty model version. The probability means the probability that the next interval's closing price exceeds the current completed interval's closing price. A probability is not a position allocation or an execution permission.

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

The default exit threshold equals the entry threshold. Stops, stale-data checks, cash availability, portfolio allocation, and other risk limits override this signal. Paper execution can be autonomous. Real-money execution always requires separate manual confirmation.
