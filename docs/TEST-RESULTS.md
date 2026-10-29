# Test results

Latest Windows checkpoint (29 October 2026): 59 Python tests and 44 frontend checks passed, the production and native installer builds succeeded, and installed-app lifecycle checks passed. See [Windows verification](WINDOWS-VERIFICATION.md). The Linux results below are retained as an earlier checkpoint.

Validation performed in the Linux build workspace using Python 3.12 on 26 October 2026.

Full Python command: `.venv/bin/python -m pytest tests -q`

Result at the latest Python checkpoint: **59 passed in 8.41 seconds**. This includes pytest-style API/provider/model tests as well as the unittest-style core/history cases below. Run the full pytest command to include every test file.

## Python trading core and history

Command: `.venv/bin/python -m unittest discover -s tests -p 'test_core.py' -v`

28 core tests passed. Coverage includes:

- Entry threshold equality, explicit/default exit thresholds and hold decisions.
- Fee/slippage cash ledger and portfolio mark-to-market accounting.
- Fixed and trailing stops independent of model signals, paused execution and disabled strategies.
- NaN/infinite/out-of-range predictions, stale quotes and per-interval signal deduplication.
- Position allocation caps, oversell prevention and manual-holding risk configuration.
- SQLite state recovery, restart pause, durable order idempotency and a single-writer process lock.
- Real-data/reconciliation requirements for live proposals, paper/live portfolio isolation, durable proposal claims, fresh-price/cash revalidation and outstanding-order guards.
- Manual live drafts remain unsubmitted, persist idempotently across restart, reject stale/simulated data and provider mismatches, enforce cash reservations and holdings, and share the same risk checks with automatic proposals and confirmations.
- Drawdown blocks additional live buys while allowing confirmed sells that reduce existing risk.
- Deterministic fixture labels, next-bar backtest execution, independence of past equity from future predictions, gap-through stop fills and finite analytics.

Command: `.venv/bin/python -m unittest discover -s tests -p 'test_history.py' -v`

DuckDB duplicate upsert, bounded history read, database reopen and Parquet export/readback passed.

The backtest evaluates models using completed candles only and fills signals on the following candle's open. An existing trailing watermark is checked before the current candle high. Final open positions are marked to market. These are OHLC simulations with constant fee/slippage assumptions; they do not model liquidity, partial fills or real brokerage execution.

## Verification limits

No real brokerage credentials were supplied. Authenticated brokerage/exchange operations and real model endpoints require verification with the user's accounts. No real-money order was submitted. Windows native compilation and installed-engine lifecycle were subsequently verified locally; see `WINDOWS-VERIFICATION.md`.

## Frontend and runtime

`npm run build` passes TypeScript and the Vite production build. `node tests/frontend.mjs` passes **43 checks**, including all fixture OHLCV invariants, accounting/fees/slippage, allocation/exposure rejection, no-look-ahead regression, model credential exclusion, instrument discovery without simulated price leakage, and provider-specific proposal payloads.

A real Python child-process smoke test passed authenticated HTTP, unauthorized rejection, exact localhost CORS, weekly fixture candles, exclusive port-zero binding and token redaction from stdout/stderr. An invalid bootstrap token exits without echoing its value. The Tauri configuration, capability declaration and Cargo manifest validate; native Windows compilation subsequently passed.

Python dependencies tested: FastAPI 0.162.0, Uvicorn 0.54.0, httpx 0.28.1, websockets 16.1.1, keyring 25.7.0, DuckDB 1.5.6, PyArrow 24.0.0, pytest 9.1.1. JavaScript dependency versions are retained in `package-lock.json`.

See `UI-TEST-RESULTS.md` for browser workflow checks.
