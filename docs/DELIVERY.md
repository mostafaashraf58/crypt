# Delivery notes

Axiom is delivered as local application source and a working offline browser preview. No cloud backend is deployed.

## Open the preview

Open `Axiom-Preview.html` in a modern desktop browser. It is self-contained and needs no server or internet connection. The default simulated portfolio, candles, and AI signals are deterministic fixtures, labeled in the interface. Paper trades, strategy configuration, model endpoint metadata, backtests, risk settings, themes, and local exports work in the preview. Its local storage is separate from the Python desktop database. Account secrets and real order execution are disabled in the preview.

## Run the desktop

Extract `Axiom-Workstation-Source.zip`, open PowerShell in its `axiom-workstation` directory, and follow `docs/SETUP.md`. `scripts/setup-windows.ps1` installs dependencies; `npm run desktop:dev` starts Tauri and its private Python engine. `scripts/build-windows.ps1` creates an unsigned NSIS installer on Windows. The included Windows CI workflow can produce the same artifact without deploying a service.

## Delivered functionality

- React/TypeScript/Tailwind dashboard, dark and light themes, Lightweight Charts candlesticks/volume/SMA, search, fixture watchlist, provider instrument discovery and historical chart source selection.
- Python asynchronous local service, Tauri 2 shell, SQLite portfolio/strategy/order/audit persistence, single-writer lock, crash recovery, DuckDB candle history, Parquet export.
- Interchangeable Alpaca and Kraken adapters, market-data/account contracts, HTTP retries, rate-limit handling, reconnecting WebSocket feeds, freshness checks, and provider failures surfaced to the interface.
- Independent HTTP model registry with OS-vault authentication, strict prediction validation, timeouts, version checks, health checks, history and deterministic mocks. No model training.
- Original upward-probability strategy with separate exit threshold, fixed/trailing stops, allocations, maximum position/exposure/drawdown guards, fees/slippage, durable duplicate prevention, and autonomous paper execution.
- Separate live account state, real-data/reconciliation requirements, manual and strategy-generated proposals, separate explicit confirmation, durable submission claims and ambiguous-submission reconciliation blockers.
- Past-only mock-model backtests with next-candle execution, equity and drawdown curves, net returns, win rate, Sharpe and trade records.

## Verification and remaining limits

See `docs/TEST-RESULTS.md` and `docs/UI-TEST-RESULTS.md` for executed checks. Real-money orders were never submitted. No brokerage credentials or real external model service were provided, so those integrations remain unverified against a real account. Adapter behavior was tested using offline transports and fixtures.

The Windows installer has **not been compiled or run** in this Linux environment. Tauri configuration and command permissions were checked against the installed schema, but native Rust/MSVC compilation and WebView2 runtime behavior require the supplied Windows build and a Windows test run. This is source with a build path, not an already verified Windows installer.

Kraken market data and account inspection are supported; live Kraken proposals are blocked until complete equity/cost-basis reconciliation exists. Alpaca supplies the manually confirmed live path. Backtests currently use the deterministic momentum model; arbitrary HTTP-model historical replay is not implemented. Local operation requires the application and PC to remain running.
