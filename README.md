# Axiom Workstation

For the downloadable pretrained price model, follow [Model quickstart](docs/MODEL-QUICKSTART.md). It includes one-time setup, a local CPU launcher, and the app's **Connect local model** button.

**Status: 0.2.3 Windows x64 installer built with local pretrained price forecasts and independent Bitfinex Paper/Live workspaces.** Python tests, frontend checks, production frontend build, native packaging and isolated bundled-engine startup/authentication checks pass. The Chronos-Bolt Small download and CPU inference have been tested through the model API with public Bitfinex data. Installed-app acceptance and real-account production acceptance remain unverified. See [the model quickstart](docs/MODEL-QUICKSTART.md), [the workspace guide](docs/BITFINEX-WORKSPACES.md), and [Windows verification](docs/WINDOWS-VERIFICATION.md).

A single-user trading workstation with a React/TypeScript interface, a Python local engine, SQLite state, DuckDB/Parquet historical storage, and a Tauri 2 desktop shell. It runs on your own computer. There is no hosted backend or account system.

The desktop opens an online Bitfinex workspace with independent Paper and Live engines. Use the mode selector to view either engine; switching views leaves both running. See [the workspace guide](docs/BITFINEX-WORKSPACES.md). Public charts use exchange data; connect and synchronize your account to load balances. Offline fixtures remain in developer/browser previews only. See [online workspace changes](docs/ONLINE-WORKSPACE.md). The developer preview uses deterministic, explicitly labeled offline fixtures. It includes paper execution, probability-threshold decisions, risk overrides, backtesting, a model API registry, provider adapters, and manual live orders and session-authorized autonomous Bitfinex strategies. Market and model credentials must be configured before using external services.

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
- Bitfinex USDT strategies can execute autonomously after explicit session activation. Manual drafts retain separate confirmation. Restart, pause, connection replacement, strategy/model changes, and uncertain submissions disable autonomous execution.
- OS credential storage is used for provider and model secrets. Secrets are not stored in SQLite or frontend local storage.
- The desktop creates a new random local session token for each launch, passes it to Python through stdin, and keeps it in application memory.
- This source includes a Windows installer build path. A Windows installer should only be described as built after the Windows job or local Windows build succeeds.
- Broker compatibility, exchange permissions, market-data subscriptions, order types, regional availability, and fees depend on the provider and account. Test a provider sandbox before real-money use.
- Bitfinex is the only exchange provider. Paper uses its public market data with simulated fills. Live uses the authenticated account, manual order review, or explicitly activated autonomous strategies.

Closing the desktop stops its engine. Keep the desktop open (it may be minimized) and disable PC sleep for continuous local paper operation.

## Bitfinex USDT spot trading

Open **Connections > Bitfinex > Connect account** in the desktop app. Enter credentials there; verified keys are stored in the OS credential vault. Use a dedicated API key with wallet/order/trade-history read access and trading permissions; withdrawals are unnecessary. Choose **Live**, click **Sync account**, and select **Bitfinex market data** for USDT spot pairs. A manual trade creates a draft for review. To execute strategy decisions automatically, open **Risk & Controls**, enter a maximum USDT order budget, and click **Activate autonomous trading** while in Live mode. Configure enabled USDT strategies with the built-in deterministic momentum model or an HTTP model. The built-in score is an uncalibrated heuristic; legacy offline mocks are refused for autonomous live execution. Old drafts and unresolved submissions must be cancelled or reconciled before activation. Orders execute as exchange fill-or-kill limits within your configured price bound of the fresh confirmation quote (default 0.5%). They may cancel without filling when liquidity is insufficient. Minimum/maximum quantities and 8-decimal amount precision are checked. Buys reserve a 0.4% fee buffer in addition to the price bound; this is a conservative reservation, not a claim about your account fee tier.

USDT maps to Bitfinex's API currency `UST`. Equity and positions are denominated in USDT, without assuming USDT equals USD. Exchange wallet balances, available funds, trade fees, and USDT acquisition history establish holdings and moving-average entry prices. Funding/margin wallets are excluded. Non-USDT holdings with missing or mismatched acquisition history, transferred-in assets without an audited opening-basis adjustment, unsupported fee currencies, unavailable balance information, or stale valuations block execution with a reconciliation explanation. Connection metadata, fills/cursors, nonce, exit intents, protective-order identities and account equity history are persisted. Trading always restarts disarmed. Settings provides verified backup and restore-on-restart for the selected mode; back up each mode separately. Trade-history scans are bounded to 250,000 fills and fail closed on a saturated timestamp boundary. Order recovery searches the latest 2,500 historical orders; known exchange order IDs are queried directly, while older submissions without a known exchange ID can still require operator reconciliation. Ambiguous submissions are never automatically resubmitted.

Account sync also runs every 15 seconds while live Bitfinex is selected, including while strategies are paused. Sync before reviewing account state or after a cancellation. Open exchange orders are visible and can be cancelled from the Bitfinex connection card after separate confirmation. Use USDT-pair strategies; the untouched starter strategy migrates to BTC/USDT. Existing custom USD strategies must be disabled or replaced before starting. When autonomy is active, new strategy and local stop decisions automatically pass account/quote/risk checks and submit without per-order confirmation. In manual mode they create review proposals. Active autonomous holdings receive exchange-hosted protective stop orders. The app verifies and adjusts their quantity and trailing floor while active. Pause/close retains already accepted exchange stops; it stops new strategy decisions and local trailing updates. Exchange stops can execute beyond the strategy price bound during a gap; that bound applies to strategy fill-or-kill orders. Protective-order behavior still requires real-account acceptance. Paper execution remains a local simulation.

Status: implemented and tested against simulated API responses, including a buy/sync/sell cycle. This is not a production certification. Live API permissions, exchange execution/cancellation, restart recovery against a real account, and extended real-world fault testing still require verification. No real order was submitted during development. See [the verification procedure](docs/BITFINEX-LIVE.md).

API references: [authentication](https://docs.bitfinex.com/docs/rest-auth), [wallets](https://docs.bitfinex.com/reference/rest-auth-wallets), [trade history](https://docs.bitfinex.com/reference/rest-auth-trades), [order submission](https://docs.bitfinex.com/reference/rest-auth-submit-order).

### Autonomous execution session

Autonomy currently supports Bitfinex USDT spot strategies. Activation authorizes new strategy decisions for the current process session; it never automatically confirms old drafts. Each order has a durable claim before submission, a planned USDT budget including a price/fee buffer, portfolio risk checks, fresh account/quote checks, and the configured fill-or-kill price bound. Quantities round down to 8 decimals. Exit intent is persisted so budget-sized child orders continue between completed candles until the reconciled holding is closed; minimum sizes, dust and cancellations can still require attention. Minimum exchange size requirements still apply.

Use **Stop autonomous trading** or **Pause engine** to disarm. An exchange request already sent may still complete. Restart disables autonomy. Switching the viewed mode leaves the other mode’s execution unchanged. Editing strategies/models or replacing/disconnecting a connection also disables it. Network/stream errors and ambiguous submissions stop automatic execution; unknown submissions are not retried. Account and exchange limits can prevent a trade. Signal evaluation, reconciliation, API rate limits, model latency and network latency mean execution is automatic, not instantaneous. Real-account validation remains outstanding.

### Configurable Bitfinex price bound

In **Risk & Controls > Bitfinex price bound**, enter 0-10 percent and click **Save limits**. Default: 0.5%. Zero permits no adverse price movement; it does not remove the price protection. The setting persists across restarts and applies to manual orders, autonomous order sizing/budgets, and live cash/exposure checks. Each submitted order captures its bound when claimed. Already sent orders are unchanged. Saving limits stops autonomous execution; reactivate it when ready. Paper simulation slippage remains a separate setting.
