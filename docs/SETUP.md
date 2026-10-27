# Setup and operation

## Windows prerequisites

Use Windows 11 x64 with Microsoft Edge WebView2. Development requires:

1. Node.js 22 LTS or newer.
2. Python 3.12 with its `py` launcher.
3. Rust stable using the MSVC toolchain.
4. Visual Studio 2022 Build Tools, the **Desktop development with C++** workload, and a Windows 10 or 11 SDK.

The installer build is intentionally performed on Windows. This project does not claim a verified Windows executable when built only in a Linux workspace.

Run `./scripts/setup-windows.ps1` from PowerShell. The script creates `.venv`, installs Python dependencies, and installs Node dependencies. If PowerShell blocks local scripts, review your organization policy; you can run the commands in the script directly without changing machine-wide policy.

Start the desktop with `npm run desktop:dev`. This script applies `src-tauri/tauri.dev.conf.json`, which disables sidecar bundling during development; Python runs directly from the virtual environment. Release builds retain the packaged sidecar. Set `AXIOM_PYTHON` to an explicit Python executable path if your development environment requires a different interpreter; this selector is used only in development builds.

## Local engine and credentials

The engine binds only to `127.0.0.1` on an available ephemeral port and retains that listening socket throughout startup. Its chosen port is announced to the desktop through the child's private stdout pipe. A 256-bit session token is generated using the OS random source and sent to the child process through its stdin. The frontend obtains the connection through the main-window capability's desktop command `get_engine_connection`. The token is not included in process arguments, URL parameters, stdout, or source files.

Engine startup runs outside the UI thread and times out after 45 seconds. Normal early closure cancels startup and waits for child cleanup. On Windows a kill-on-close Job Object covers the bundled Python process tree, including its PyInstaller bootloader.

The local engine API requires `Authorization: Bearer <session-token>` for application operations. Its public health route reveals only service health. Browser-origin and host checks add protection against unrelated web pages using the localhost service.

Desktop state lives in the per-user Tauri application data directory (on Windows, normally `%LOCALAPPDATA%\com.axiom.workstation`). The folder holds SQLite application state and historical files. The Windows credential vault stores API secrets through Python `keyring`; no fallback writes credentials to plaintext files. If the credential store is unavailable, connection setup must fail explicitly.

Do not copy application databases while writing transactions or share another user's credential store. Back up local state after closing the desktop. Provider balances and orders should be reconciled before trading after a restart.

## Providers

### Alpaca

Create a separate paper account/API key for initial testing. Select Alpaca in Connections and enter its key ID and secret. Use the paper environment first. Market-data access and websocket feeds depend on the account subscription. Stock markets observe exchange calendars; data availability outside market hours is provider-dependent.

The adapter separates market-data, paper-trading, and live-trading service origins. Selecting a live account is not an instruction to place a real order. Live execution requires the explicit order-review flow.

### Kraken

Public crypto market-data routes do not require account credentials. Private account synchronization requires a Kraken API key and signing secret with the necessary permissions. Use minimum permissions; withdrawal permissions are unnecessary. Tradable symbol formats and historical interval availability are determined by the exchange. The current Kraken adapter does not establish full marked-to-market equity and position cost basis, so the execution safety layer blocks Kraken live proposals and confirmation. Kraken remains available for market data and account inspection. Alpaca supports the manually confirmed live-order path.

Rate limits and service failures are surfaced as connection errors. An unavailable provider must not silently switch a live chart to fixture data. Offline fixture data stays labeled as such.

## Independent models

Register a local or remote HTTP model endpoint, then assign it to an asset and prediction interval. The app does not train or embed an AI model. The deterministic mock model is available for offline integration tests. See [AI-API.md](AI-API.md) for the request contract.

The desktop model form configures endpoint metadata and an optional bearer token. Credentials may also be configured through the authenticated local `POST /models` API using its `api_key` field. The API stores that value in the OS credential vault and omits it from returned registry records. The browser preview accepts no model secrets and does not make external predictions.

For remote models, use HTTPS. For local models, bind the model service to localhost unless you deliberately need broader network access. Each remote model receives candle/features data required for its assigned strategy; consider that data flow when selecting an endpoint.

## Strategy and operating modes

At a completed candle, the strategy compares `probability_up` with the entry threshold. An open position uses the configured exit threshold (the entry threshold by default). Fixed/trailing stops and portfolio risk limits can override model decisions. Intrabar price updates may trigger stop exits but must not be treated as newly completed intervals.

Paper mode applies simulated fees and slippage to a virtual cash ledger. Live mode creates proposed orders; it must not authorize autonomous submission. Review symbol, side, quantity, price context, risk, account, and provider before confirmation. Never treat a model probability or a backtest result as a guarantee of a future outcome.

Historical backtests use completed observations available at the decision time and execute at a later tradable price. The current backtest runner uses the deterministic momentum mock; historical replay through arbitrary HTTP models is not implemented. Keep enough warm-up history for models and indicators. Compare net results after fees and slippage, and inspect drawdown and trade count.

## Build an installer

Run `./scripts/build-windows.ps1`. It:

1. Installs dependencies unless `-SkipSetup` is supplied.
2. Runs Python tests and the frontend build.
3. Bundles the Python engine using PyInstaller.
4. Copies the sidecar to the host-triple filename expected by Tauri.
5. Builds a per-user NSIS installer.

The output is unsigned unless you configure your own code-signing process. Windows may display the standard unknown-publisher prompt. The included `.github/workflows/windows.yml` runs the same steps and retains the installer as a build artifact for 14 days.

## Troubleshooting

- **Engine fails during startup:** check that `.venv/Scripts/python.exe -m engine --help` works and dependencies are installed. The desktop times out after 45 seconds and does not expose a partially initialized engine.
- **Windows build cannot find a sidecar:** run the complete build script; a frontend build alone does not create the PyInstaller executable.
- **Credentials cannot be saved:** inspect Windows Credential Manager/keyring availability. Do not store secrets in configuration files as a workaround.
- **API timeout/stale quote:** pause affected strategies, check the provider or model health, and reconcile before resuming.
- **No fixtures after reconnect:** live and fixture data are intentionally separate. Switch explicitly to the offline workspace when you want deterministic test data.
- **Computer sleeps:** this is a local application. It cannot continue processing while Windows sleeps, powers off, or terminates the process.

## Validation scope

The offline test suite uses deterministic inputs and mocked HTTP transports. It can validate strategy boundaries, stops, simulated accounting, persistence, API validation, and provider error handling without broker credentials. It cannot establish that a real brokerage account is authorized, that a market feed is available under a subscription, or that a Windows installer runs correctly. Those checks require the corresponding account and Windows environment.
