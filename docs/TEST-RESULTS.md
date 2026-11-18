# Test results

Bitfinex workspace checkpoint (30 October 2026): **99 Python tests, 47 frontend checks and 41 Edge browser checks pass**, and the TypeScript/Vite production build succeeds. The other exchange implementations and their obsolete tests were removed. New checks cover independent persisted runtimes, public-quote paper fills, completed-candle model input, weekly timestamp preservation, order pagination by creation time, raw account/order records, unavailable reconciliation totals, take-profit exits, model selection, candle clicks and background paper operation while Live is viewed. Exchange submissions and account responses are simulated. No real order was submitted; actual-account acceptance and a new native installer build were not performed for this checkpoint. See [the workspace guide](BITFINEX-WORKSPACES.md).

0.2.0 reliability checkpoint (29 October 2026): **99 Python tests, 45 frontend checks and 34 Edge browser checks pass**; TypeScript/Vite and the bundled Windows installer build pass. Two native desktop restart cycles preserve the seeded databases and verify automatic backups, local authentication and shutdown cleanup. A disposable Windows vault save/read/delete test passes. npm audit reports zero vulnerabilities. Exchange tests are simulated; real account production acceptance remains outstanding. See [release verification and limits](RELIABILITY-RELEASE.md). Earlier checkpoints below are retained as history.

Configurable price-bound checkpoint: 80 Python tests, 45 frontend checks and 34 offline browser checks. Coverage includes saved 0-10% bounds, buy/sell exchange limit prices, zero tolerance, restart persistence, cash and pending-buy reservations, autonomous budget sizing, and disarming when limits are saved. All exchange submissions in tests are simulated.

Autonomous Bitfinex checkpoint: 76 Python tests, 45 frontend checks and 33 offline browser checks pass; the Windows installer rebuilt successfully. Simulated tests cover automatic entry and stop exit, order-budget sizing, mock-model rejection, pause racing with activation, uncertain-submission shutdown without retry, and restart without restoring session authorization. Autonomy was not activated against a real account. Prior USDT checkpoint results below are retained as history.

Bitfinex USDT checkpoint, 29 October 2026: `.venv/Scripts/python.exe -m pytest tests -q` passed **71 tests**, frontend checks passed **45**, and offline Edge browser checks passed **32**, including the Bitfinex connection form. The native Windows installer rebuilt successfully. The bundled engine passed startup, authenticated snapshot, and unauthenticated rejection checks; temporary process cleanup required elevated Windows permissions and then completed. Real account access, fills, and exchange-side cancellation remain unverified. No saved Bitfinex key was found and no real orders were submitted. See [the live verification procedure](BITFINEX-LIVE.md).

Latest Windows checkpoint (29 October 2026): 60 Python tests and 45 frontend checks passed, the production and native installer builds succeeded, and installed-app lifecycle checks passed. See [Windows verification](WINDOWS-VERIFICATION.md). The Linux results below are retained as an earlier checkpoint.

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
