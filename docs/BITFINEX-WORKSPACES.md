# Bitfinex workspace guide

The desktop has two independent local runtimes. **Paper** simulates fills using fresh public Bitfinex prices and virtual funds. **Live** reads your real Bitfinex account and supports manually confirmed orders or explicitly activated autonomous strategies. Select either view in the header. Changing the viewed mode does not pause or disarm the other runtime. Closing the desktop stops local trading; both runtimes restart paused. Existing accepted exchange stops remain at Bitfinex.

Each mode has its own strategies, model registry, risk limits, balances, positions, signals, orders, prediction records and backups. Live retains the existing data directory; paper uses its `paper/` subdirectory. On first upgrade, the existing simulated portfolio and model metadata are copied to paper without changing the real account. Back up each mode separately in Settings.

## Candles and model checks

Public Bitfinex candles load in both modes without API credentials. Clicking a candle shows its exact UTC opening timestamp, OHLC, base-asset volume, symbol and interval. The current forming candle is visible and labeled. Native intervals preserve exchange timestamps, including weekly anchors. The 4h interval aggregates real 1h bars on UTC boundaries. Missing observations are never replaced with synthetic bars. Data errors are shown directly.

On AI Models, select an existing model and click **Get next interval probability** for the chart’s selected symbol and interval. The model receives completed candles only. The result includes its input cutoff and target closing time. `builtin-momentum` uses the last 20 closes and is an uncalibrated direction heuristic; it does not predict an exact price or establish predictive accuracy. HTTP models remain available. The old offline mock remains blocked from autonomous live execution.

## Account and orders

Connect credentials under **Live → Connections → Bitfinex**. The Portfolio page shows actual wallet balances, available amounts, interest, synchronization time, trade fills and reconciliation errors. All wallet types are listed. Trading totals cover supported exchange-wallet spot holdings valued in USDT; funding/margin balances are listed separately. Cost basis comes from reconciled acquisition history or verified opening-basis adjustments. Incomplete or stale totals display as unavailable instead of zero or a previous account value. Performance charts use recorded observations. Live P&L is change since the synchronized baseline adjusted for recorded cash flows, not lifetime account earnings.

Orders displays actual open Bitfinex orders and paginated closed-order history, including orders placed outside Axiom. Each record includes exchange ID, pair, side/type, original/filled/remaining quantities, limit/average prices, exact exchange status, timestamps and the raw exchange record. Local proposals are displayed separately. History pages overlap at the creation-time boundary and deduplicate by exchange ID; saturated boundaries produce an explicit error rather than silently dropping orders. Bitfinex documents a two-week window for the orders-history API, so older records require an exchange report. Account fills show their actual fee currency and amount. No unavailable fee is treated as a known zero.

## Trading controls

1. Select **Paper** or **Live** before creating strategies. In Strategies, choose an existing model, instrument, interval, thresholds, allocation, fixed/trailing stops and optional take-profit target. A zero profit target disables it.
2. Open **Trading**, select the existing strategies this mode may run, and save the selection. Saving pauses this mode.
3. Start paper trading or the live manual-review engine. Paper fills remain simulated; manual live drafts require confirmation on Orders.
4. To authorize automatic live execution, enter a maximum USDT order budget and click **Activate autonomous trading**. The selected strategies can then submit real orders subject to account reconciliation, fresh quotes, loss/exposure/allocation limits and price bounds.
5. Use **Pause this mode** to stop its strategy session. View each engine’s status, equity, P&L, available funds, protection, signals and order status. Configure risk limits through the linked controls. Profit targets require the local engine to run; accepted exchange stops can continue independently of the desktop.

No real exchange trade was submitted to implement or test these changes. Automated checks simulate account responses and execution; acceptance against the actual account remains necessary before describing this release as validated for that account.

References: [Bitfinex candles](https://docs.bitfinex.com/reference/rest-public-candles), [wallet response fields](https://docs.bitfinex.com/reference/rest-auth-wallets), [order history and creation-time filters](https://docs.bitfinex.com/reference/rest-auth-orders-history).
