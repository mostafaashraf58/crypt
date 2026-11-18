# Axiom Workstation 0.2.0 reliability release

29 October 2026. This implements the software recovery and execution work from the [pre-fix review](FINAL-VISION-REVIEW.md). It is not a certification for unattended real-money production. No real exchange order was placed during this work.

## Implemented

| Area | Result |
|---|---|
| Protective exits | Drawdown blocks new entries without blocking authorized exits. Durable exit intent continues budget-sized child orders between candles. Entry sizing includes existing exposure, reservations, price bounds and fee buffer. |
| Order outcomes | Known pre-send rejection and explicit exchange rejection are terminal. An uncertain submission stays blocked for reconciliation and is never blindly retried. Stored exchange IDs support direct lookup. |
| Exchange protection | Bitfinex EXCHANGE STOP placement, persisted identity, quantity/trailing-floor amendment, cancellation confirmation before strategy sells, and remainder protection. Already accepted stops survive desktop closure. |
| Market/account data | Public trade and authenticated account WebSockets, coalesced quote processing, endpoint request pacing and REST recovery. Held assets are included in monitoring. |
| Accounting | Durable Bitfinex fills, overlap synchronization, late-fee retries, stable user identity, supported USDT cash movements and acknowledged opening-basis adjustments. Unsupported or mismatched holdings still block trading. |
| Provider separation | Provider-inclusive candle keys and legacy migration prevent cross-provider overwrites. Account-specific protection, exit intents, baselines and proposal identity prevent reuse across accounts. |
| Operator views | Actual fills, remaining quantity and known fees, persisted account equity history, source-backed watchlist, account/currency/autonomous status, daily limits, protective-order status and persistent operational alerts. |
| Model/replay | Approved model-version pinning, stored prediction inputs, past-only HTTP-model replay over provider candles, durable progress, resume and saved results. |
| Data recovery | SQLite FULL/WAL commits, single-writer locking, integrity/schema checks, preserved order idempotency after abrupt exit, bounded hot-state history with durable archives, verified database backups and deferred restore under the startup lock. |
| Packaging | Version 0.2.0, dependency lock capture, smaller sidecar runtime and split frontend bundles. Existing desktop application identifier/data location retained. |

## What survives restart

Strategies, risk/price-bound settings, paper positions/orders, live proposals/results, exit intents, protective-order identities, account baselines/history, watchlist, connection metadata, models/version pins, prediction records, replay progress and provider candle history are stored locally. Provider/model secrets remain in Windows Credential Manager. UI source/symbol/page/interval/theme preferences use WebView local storage.

The standard desktop data folder remains `%LOCALAPPDATA%\com.axiom.workstation`. Reinstalling/upgrading over the existing app retains the same identifier; do not select an installer option that removes application data. Local files cannot survive disk destruction or explicit deletion without an independent copy.

Autonomous authorization intentionally does **not** survive restart. Reopening restores data, reconnects saved providers read-only, and starts disarmed. Reconcile the account and activate a new session. Existing exchange stops may execute independently while the app is closed.

Use **Settings → Create backup** to pause autonomy and snapshot the engine, model database and provider history. Startup also attempts a backup when no backup exists or the latest is older than 24 hours. ZIP manifests verify each database hash; backup files exclude secrets. Copy needed ZIPs to separate storage. **Restore on restart** validates a selected workspace backup, blocks trading and applies it on the next launch; pre-restore files are retained as `.before-restore`. Restore rewinds local records and cannot rewind exchange activity: sync before reactivating. Credentials must remain available in the same OS vault or be entered again.

## Verification

- Python: 99 tests pass, including abrupt process termination, verified restore, corruption recovery, future-schema preservation, provider history migration, account separation, sizing boundaries, continued exits, drawdown-stop interaction, protective lifecycle, replay resumption and saved alerts/results.
- Frontend logic: 45 checks pass. Edge browser: 34 checks pass, zero runtime errors. TypeScript/Vite production build passes.
- Windows credential vault: disposable credential save/read/delete passes outside the restricted execution logon session; existing keys untouched.
- npm dependency audit: zero reported vulnerabilities. Python vulnerability scanning and independent security review remain outstanding.
- Packaged Windows build succeeds: installer is 34,642,070 bytes (33.04 MiB), unsigned. Two actual desktop open/close cycles pass with isolated QA data: version/health, loopback-only engine, unauthenticated-state rejection, sidecar cleanup, preserved positions/orders/idempotency/strategies/risk/watchlist/models/provider history, and packaged automatic backup manifest/hash verification. QA folder: `work/native-qa-141a8023344d45a8af8d2b4130ad13c9`.
- Installer SHA-256: `95d2e017125b2b4f28d71bd9adea0edd83fc555e301c79ba5ecb7989931dca26`. Path: `src-tauri/target/release/bundle/nsis/Axiom Workstation_0.2.0_x64-setup.exe`.
- `git diff --check` passes. The native QA run did not install over or migrate the user's actual profile; installer upgrade/uninstall of the user's populated profile remains unverified.

Tests use fake exchange responses and isolated test databases. They establish the covered contracts, not actual exchange acceptance or profitability.

## Remaining release acceptance and scope

Real Bitfinex credentials, permissions, balances, fees, protective stops/amendments, fills and cancellation/crash races must be verified with explicitly bounded exchange acceptance tests. Multi-day operation with outages, sleep/resume, throttling and clock changes remains unproven. The installer is unsigned; signing needs the owner's certificate. Automated signed updates/rollback are not implemented.

Autonomy is Bitfinex USDT spot only. Alpaca and supported reconciled Kraken accounts retain manual execution; autonomous support across those providers remains separate work. This is one local active source, not simultaneous multi-account management or hosted 24/7 execution. Alerts are local persistent activity; there is no external notification service or remote monitoring. Candle backtests use next-bar OHLC fills with constant fees/slippage; they do not reproduce order-book liquidity, partial fills, the full live coordinator or walk-forward promotion. Old unknown submissions beyond bounded history without a stored exchange ID still require operator exchange reconciliation. Fee tiers above the reservation, unsupported assets/transfers, exchange minimums and dust can block execution.

The configurable price bound applies to strategy/manual fill-or-kill orders. Exchange stop-market protection favors exiting and can fill beyond that bound. See Bitfinex's [submit-order](https://docs.bitfinex.com/reference/rest-auth-submit-order) and [update-order](https://docs.bitfinex.com/reference/rest-auth-update-order) contracts; API support does not establish a passed real-account lifecycle test.

## Local machine condition

The system drive ran out of space during verification; one abandoned engine extraction was removed after approval and build/test temporary files were placed on D:. At the final check C: had approximately 0.63 GiB free and D: 3.16 GiB. Free additional system-drive space before unattended use, because the default app data, Windows vault/profile and temporary extraction depend on C:. No trading/account data was deleted.
