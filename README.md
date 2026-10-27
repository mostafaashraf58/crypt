# Axiom Workstation

**Status: working development build; not production-ready.** See [the completeness audit](docs/COMPLETENESS-AUDIT.md) before connecting an account. The Windows installer has not yet been built or verified.

A single-user trading workstation with a React/TypeScript interface, a Python local engine, SQLite state, DuckDB/Parquet historical storage, and a Tauri 2 desktop shell. It runs on your own computer. There is no hosted backend or account system.

The default workspace uses deterministic, explicitly labeled offline fixtures. It includes paper execution, probability-threshold decisions, risk overrides, backtesting, a model API registry, provider adapters, and a manually reviewed live-order workflow. Market and model credentials must be configured before using external services.

## Clone onto your PC

```powershell
git clone https://github.com/mostafaashraf58/crypt.git
cd crypt
```

For an offline browser preview, run `npm ci` followed by `npm run dev`, or use `npm run build:preview` to generate a self-contained HTML preview.

## Run on Windows 11

Install Node.js 22 LTS, Python 3.12 (with the `py` launcher), Rust stable, and Visual Studio 2022 Build Tools with **Desktop development with C++** and a Windows SDK. Then open PowerShell in this directory:

```powershell
./scripts/setup-windows.ps1
npm run desktop:dev
```

To produce an unsigned x64 NSIS installer on Windows:

```powershell
./scripts/build-windows.ps1
```

The installer is written to `src-tauri/target/release/bundle/nsis/`. The build bundles the Python engine, so the installed application does not require a separate Python installation. The installer provisions Microsoft WebView2 when it is missing; that first installation requires internet access.

The included GitHub Actions workflow can also build the Windows installer on a Windows runner. It uploads a build artifact and does not publish a release or deploy a service.

## Development and verification

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pytest
npm install
.venv/bin/python -m pytest tests -q
npm run build
```

On Windows, use `.venv/Scripts/python.exe` in place of `.venv/bin/python`. Desktop development starts the authenticated engine automatically. A plain `npm run dev` starts the browser interface; the browser uses local demo state when no desktop engine connection is available.

See [setup and operation](docs/SETUP.md) and the [independent model API contract](docs/AI-API.md). See [delivery notes](docs/DELIVERY.md), [Python/frontend test results](docs/TEST-RESULTS.md), and [browser results](docs/UI-TEST-RESULTS.md). Generate a self-contained offline HTML preview using `npm run build:preview`; the resulting file is `dist-offline/Axiom-Preview.html`.

## Operational boundaries

- Offline fixtures are not live market prices, and paper fills are simulations.
- The strategy never submits unattended real-money orders. Live orders require a human confirmation through the execution layer.
- OS credential storage is used for provider and model secrets. Secrets are not stored in SQLite or frontend local storage.
- The desktop creates a new random local session token for each launch, passes it to Python through stdin, and keeps it in application memory.
- This source includes a Windows installer build path. A Windows installer should only be described as built after the Windows job or local Windows build succeeds.
- Broker compatibility, exchange permissions, market-data subscriptions, order types, regional availability, and fees depend on the provider and account. Test a provider sandbox before real-money use.
- Manually confirmed live execution is implemented for Alpaca. Kraken currently provides market data and account inspection; incomplete equity/cost-basis reconciliation blocks its live execution.

Closing the desktop stops its engine. Keep the desktop open (it may be minimized) and disable PC sleep for continuous local paper operation.
