# Review against the autonomous trading vision

Historical pre-fix review. Follow-up implementation and current acceptance status: [0.2.0 reliability release](RELIABILITY-RELEASE.md).

Reviewed 29 October 2026. Target: a Windows desktop workstation that connects to the user's accounts and independent prediction models, makes strategy decisions, executes them automatically within configured limits, protects positions, and recovers with an accurate account view.

**Verdict: a working development application with meaningful execution controls, but not ready for unattended production use.** Recent feature delivery and successful packaging do not establish that the complete trading lifecycle is reliable. This review found defects in the autonomous path added during development.

## Scope and evidence

Reviewed the React interface and API client, Python strategy/risk engine, execution coordinator, all provider adapters, model registry, backtesting, candle storage, SQLite persistence, Tauri lifecycle, installer configuration, CI and existing verification reports. No application trading behavior was changed in this review.

Re-ran:

- `.venv/Scripts/python.exe -m pytest tests -q`: **80 passed**.
- `npm run test:frontend`: **45 passed**.
- Offline Edge browser suite: **34 passed**, including navigation, forms, preview trading boundaries and price-bound persistence.
- `npm run build`: passed; Vite reports a bundle-size advisory.

Additional isolated simulations reproduced four gaps described below. Reproduction script: [audit-repro.py](../work/audit-repro.py). Captured results: [audit-evidence.json](../work/audit-evidence.json). These use a fake broker and temporary databases; no exchange account or real order was used. The simulations demonstrate current behavior; they are not new passing regression coverage for fixes.

Latest native installer creation was verified in the preceding implementation work. The native installed UI, real credential-vault workflows, exchange permissions/fills, long-running operation and external model service were not newly validated in this review. Dependency vulnerability scanning and independent penetration testing were not performed.

## What is already useful

The app has ten working interface sections, a local authenticated engine, credential-vault integration, durable order claims, restart pause, a real model probability contract, paper execution, configurable strategy/risk parameters, configurable Bitfinex price bounds, manual live drafts, session-authorized Bitfinex USDT automation, and an unsigned Windows installer. The engine separates paper and live balances and blocks unsupported reconciliation instead of inventing account values. These foundations are worth keeping.

Provider completion differs: Bitfinex autonomy is limited to USDT exchange spot; Alpaca has manual execution; Kraken live execution remains blocked by incomplete equity/cost-basis reconciliation. Only one connection per provider and one active engine source are represented.

## Release blockers

### 1. A drawdown pause can prevent an automatic protective exit

**Reproduced.** A simulated account held 10 BTC at a reference price of 100, with equity 1,000. Price fell to 80, crossing both the fixed stop and the 15% drawdown limit. The result was **zero submitted sells**, a stopped engine, manual execution mode, and a proposal marked `reconcile_required` despite no exchange submission.

The core sets `running=False` at the drawdown threshold, including while validating a SELL. The execution coordinator then refuses automatic submission because `running` is false. References: [core.py](../engine/core.py), `_validate_live_proposal` and `tick`; [api.py](../engine/api.py), `execute_proposal.check_session`.

**Recommendation:** separate permission to open new exposure from permission to reduce existing exposure. Define explicit states such as active, entries blocked, exit-only, disarmed, and reconciliation blocked. A loss limit must not accidentally disable an otherwise authorized protective exit. Keep explicit operator disarm distinct from a risk-triggered entry pause. Add a regression reproducing the simultaneous stop/drawdown case.

### 2. An exit decision does not reliably finish an oversized exit

**Reproduced.** A 10-unit position received a threshold SELL, with a 20 USDT order budget and price 100. Only 0.19821212 units were selected. After simulating that fill, 9.80178788 remained; the next between-candle tick returned WAIT. Threshold evaluation happens once per completed interval, so another threshold exit can wait until the next candle. This is not a complete position-exit workflow.

References: [core.py](../engine/core.py), interval deduplication and `max_order_notional` quantity cap; [api.py](../engine/api.py), `process_tick`.

**Recommendation:** persist a parent exit instruction with a target remaining quantity. Track child orders, fills, cancellation, retries, rate limits and remaining exposure until the exit completes or an explicit failure condition occurs. Distinguish maximum entry budget, child-order budget and emergency exit policy. Do not simply remove all bounds or blindly retry uncertain submissions.

### 3. A local stop is not protection during an outage

**Confirmed missing capability.** Stops are generated by the running local engine. Closing the desktop terminates the engine. PC sleep, internet failure, engine failure or a disarmed session can therefore leave a holding without an active exit mechanism.

**Recommendation:** implement and verify exchange-hosted protective orders where supported, with quantity adjustment after fills, cancellation/replacement rules and orphan-order reconciliation. Explicitly choose between stop-market and stop-limit behavior: protection does not guarantee a particular fill price. Bitfinex documents exchange stop and trailing-stop order types in its [order API](https://docs.bitfinex.com/reference/rest-auth-submit-order); their availability does not mean this app has implemented or validated their lifecycle.

### 4. Pre-submission validation failures can become false unknown orders

**Code-confirmed failure path.** The execution coordinator claims an order before the adapter checks exchange minimum/maximum quantity. The adapter can reject the request before sending anything, but the coordinator categorizes any ProviderError after the claim as `reconcile_required`. Exchange lookup then cannot find an order that was never submitted. The UI only cancels drafts awaiting review, and autonomy refuses unresolved submissions.

References: [api.py](../engine/api.py), `execute_proposal`, startup blockers and `cancel_proposal`; [providers.py](../engine/providers.py), Bitfinex `submit_order` preflight checks.

**Recommendation:** introduce explicit outcomes: rejected before send, accepted, partially filled, fully filled, cancelled, rejected by exchange, and genuinely ambiguous. Persist outbound request identity and exchange order identity. Add an audited operator recovery flow for unknown outcomes, using exchange evidence rather than a blind “clear error” button.

### 5. Real-account acceptance evidence is still absent

Offline tests exercise the covered software contracts, not actual account permissions, exact fee behavior, live fills, cancellation races or recovery against Bitfinex. Browser checks exercise preview mode rather than the native authenticated trading workflow.

**Recommendation:** validate read-only account access first; then recorded exchange replay and shadow operation; then separately operator-authorized exchange acceptance tests with explicitly bounded parameters. Include dropped responses after exchange acceptance, crash immediately after sending, partial fills, stale data, API throttling, sleep/resume and clock changes. Do not label the release production-ready until results and remaining limits are recorded.

## Important correctness and completeness work

| Area | Finding | Recommended completion |
|---|---|---|
| Entry sizing | **Reproduced:** a strategy allocation equal to the allowed 25% position maximum returned WAIT because sizing uses paper slippage while live risk validation adds the larger execution-price bound. | One shared sizing function incorporating available funds, existing holdings, pending reservations, fees, execution bound, lot precision and exchange minimums. Test allocation and exposure boundaries. |
| Held-position coverage | Streams and scheduling derive their symbols from configured strategies. An account holding without a matching strategy is not automatically included in stop monitoring. | Build the monitoring universe from the union of account holdings, active orders and strategies; make unprotected holdings visible. |
| Candle isolation | **Reproduced:** storing Alpaca and Kraken candles for the same symbol/interval/time overwrote the first row. The history primary key excludes provider. | Include provider and market identity in storage keys, queries, export and backtest selection; migrate existing rows explicitly. See [history.py](../engine/history.py). |
| Market-data latency | Bitfinex `stream` is sequential REST polling, with five seconds between symbols. More symbols increase the cycle time. Account valuation and execution add further REST quote requests. | Public WebSocket market data, authenticated wallet/order/trade updates, shared subscriptions, endpoint-aware request budgets, bounded reconnection and REST recovery. Bitfinex exposes [account streams](https://docs.bitfinex.com/docs/ws-auth); its [public trade endpoint](https://docs.bitfinex.com/reference/rest-public-trades) is documented at 15 requests/minute, making combined polling a rate-limit concern. |
| Accounting | Cost basis is reconstructed from supported USDT fills; external coin transfers, other quote currencies and some fee histories block reconciliation. Cash movements are not separated from investment performance. API-key rotation can also reset the performance baseline because identity uses the API-key hash. | A durable account ledger for fills, fees, deposits, withdrawals and transfers; stable account identity; audited basis import/adjustments; cash-flow-adjusted performance. Unknown basis must remain explicit. |
| History recovery | Trade cache/cursor is in memory, and historical order lookup is limited to the latest 2,500 orders. Late fee corrections outside the current cursor can remain unresolved until a broader rebuild. | Durable incremental synchronization with overlap/deduplication, backfill and correction handling; stable lookup by stored exchange order ID; recovery progress visible to the operator. |
| Live order display | Frontend normalizes a live proposal using its reference price and quantity, sets fee to zero, and does not use its exchange result for actual fill display. | Show decision price, limit, average fill, filled/remaining quantity, actual fees, exchange ID, timestamps and terminal reason separately. Never present a reference quote as confirmed fill accounting. See [src/api.ts](../src/api.ts). |
| Portfolio interface | Performance history is explicitly illustrative; watchlist uses fixed fixture prices; some labels still refer to paper/manual execution during live use. | Real equity snapshots and fill-derived P&L, provider-backed persistent watchlists, accurate currency labels and a persistent live/autonomous/account status banner. |
| Chart interface | Charts poll history every 20 seconds; App and TradingChart each call the candle hook. The chart is recreated on candle changes and fits content again. | A shared market-data store, incremental chart updates, preserved zoom, visible quote age and connection state. |
| Backtesting | Browser UI does not send a provider source and only offers fixture instruments; backend can fetch real candles but still uses its deterministic momentum calculation, not the assigned HTTP model. | Replay the same strategy/model/risk logic over real provider-isolated historical data. Preserve model versions and inputs; use holdout/walk-forward evaluation and realistic fees, liquidity, cancellations and execution bounds. |
| Model governance | The registry validates response shape and request IDs, but accepts returned versions without pinning an approved production version. Health is not evidence of model quality. | Approved model/version selection, schema and feature checks, prediction timing/freshness, performance monitoring and a controlled promotion path from replay to shadow to live. No model profitability can be inferred from current tests. |
| Operational controls | Position/exposure/drawdown and per-order budgets exist, but there are no explicit daily loss/trade-count/turnover limits, notification delivery or remote operational status. | Configurable daily risk limits, a clear pause versus exit-only versus liquidation distinction, actionable local alerts and optional user-configured notifications. |
| Storage and recovery | Events/audit/proposals grow without a retention policy, and each signal saves the whole state blob. Exported UI JSON is not a complete database backup/restore system. | Versioned migrations, incremental records, bounded logs/retention, tested backup/restore and disk-full/corruption handling. |
| Provider scope | Autonomy is Bitfinex-only; Alpaca manual execution and Kraken inspection do not fulfill multi-provider autonomous trading. | Finish one provider fully before duplicating the lifecycle. Then isolate account, strategy, risk, currency and data state per provider/account. |
| Delivery/security | Current installer is unsigned; native vault and full live UI verification remain incomplete. Current build CI does not establish long-running exchange reliability. | Signed/versioned releases, integrity/update verification, rollback and upgrade tests, vault tests, dependency scanning, secret-redaction checks, recovery drills and live-mode native UI acceptance coverage. |

## Recommended delivery order

1. **Make the Bitfinex position lifecycle correct.** Fix the drawdown/exit interaction, persistent multi-order exits, preflight versus ambiguous failure classification, held-position coverage and sizing boundaries. Add regression tests reproducing every defect before changing behavior. Acceptance: each position can be entered, protected, exited and reconciled without silent abandonment or duplicate submission.
2. **Build the account and event foundation.** Add authenticated exchange events, durable fills/order/fee/cash-flow records, provider-isolated candles, actual fee lookup and complete recovery tools. Acceptance: the app and exchange agree on funds, holdings, orders, fees and remaining exposure after restart and injected failures.
3. **Make simulation representative.** Use the same execution state machine in recorded replay and shadow operation, and replay the assigned model over real history. Acceptance: realistic cancellation/partial-fill behavior and explicit differences between simulation and live operation.
4. **Make the operator interface truthful and complete.** Replace illustrative performance and fixture watchlists in live views, display actual fills, explain blockers, expose protection status and preserve an obvious autonomous/account banner. Acceptance: every displayed financial value has a traceable source and every blocked state has an actionable explanation.
5. **Prove and package the release.** Run multi-day fault-injected operation, native UI/vault/upgrade/restore checks and separately authorized exchange acceptance tests. Sign and version the release. Only then expand automation to Alpaca/Kraken or add optional always-on hosting.

## Definition of the first complete release

One fully verified Bitfinex USDT spot implementation is a better first release than three partially completed trading providers. Completion means: real model inputs and signals; deterministic strategy/risk decisions; automatic bounded execution; independently maintained protection; accurate fills/fees/performance; recovery from unknown orders and restarts; useful alerts; and a verified native installer.

No blanket guarantee of profit, zero slippage, uninterrupted execution or loss prevention is possible. The release claim should describe tested operating conditions and residual risks rather than equate test count with production readiness.
