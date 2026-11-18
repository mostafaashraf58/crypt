# Bitfinex USDT live verification

See [0.2.0 reliability release](RELIABILITY-RELEASE.md). Status: software changes implemented; live provider verification outstanding. No production-readiness claim follows from offline tests.

## Implemented scope

- USDT exchange spot wallet and pairs, with `USDT`/`UST` normalization.
- Total equity separated from available order funds; available asset quantities restrict sells.
- Moving-average cost basis rebuilt from exchange fills, with base and USDT fees. Deposits/transfers or unsupported fills that prevent matching the wallet block trading.
- Fresh marks, stable wallet snapshot check, exchange amount limits/precision, configurable price protection (default 0.5%, range 0-10%) through fill-or-kill orders, and a 0.4% buy fee reservation.
- Durable proposal claims, separate confirmation, no automatic replay of ambiguous submissions, and periodic account/order reconciliation.
- Explicit synchronization, open-order display, and separately confirmed cancellation in Connections.

## Checks before production approval

1. Build/install the current desktop package and verify the OS vault stores/restores/removes the Bitfinex key. Enter credentials only in the desktop connection form.
2. With account read permissions, sync the actual exchange wallet. Compare total USDT, available USDT, holdings, available quantities, entry prices, and open orders to Bitfinex. Investigate every reconciliation error.
3. Confirm the account uses USDT spot pairs and inspect the actual fee tier. The 0.4% reservation must cover the account's actual fee rate. Configure allocation/exposure limits for this account.
4. In Bitfinex paper trading or a separately authorized minimal-value live test, verify manual and autonomous buys and sells, fee deductions, fill-or-kill cancellation, partial/ambiguous responses, and restart reconciliation. Never authorize testing by giving the agent general trading discretion; select the pair, side, maximum amount, and price bounds explicitly.
5. Verify the cancellation workflow against an exchange order, including the case where it fills before cancellation reaches Bitfinex.
6. Test connectivity loss and restart with an unresolved order. Verify the app blocks duplicate submission and correctly recovers the exchange result.
7. Verify native UI cash/equity labels use USDT, balance changes refresh, and account errors remain visible. Verify the strategy engine only uses intended USDT strategies and an independently verified model.

## Operational limits

Manual drafts require confirmation. With an explicitly activated autonomous Bitfinex session, new strategy entries, exits and local stop decisions execute automatically within the configured order budget and risk limits. Local stop signals do not protect positions while the application is closed or disconnected. During an active autonomous session the app places exchange-hosted EXCHANGE STOP orders and amends their quantity/trailing floor. Already accepted stops remain at the exchange after pause/close. They are stop-market protection and can slip beyond the strategy fill-or-kill price bound. Validate placement, amendments, fill races, dust, cancellation and restart recovery against the actual exchange before production acceptance. Autonomy is session-scoped and requires an open, connected application; it is not an always-on hosted service.

Only exchange assets whose USDT acquisition history or explicitly acknowledged local opening-basis adjustment reconciles can trade. External coin deposits, transfers, non-USDT trading history, missing fees, and other unsupported account activity require operator reconciliation. Margin/funding assets are excluded from this portfolio. Historical scans and order lookup are bounded as documented in README.

Offline tests cannot establish exchange access, account permissions, real execution, uptime, or production reliability. Record the results of these checks before changing the deployment status.

## Autonomous mode verification

In Live mode, configure enabled USDT strategies with a real HTTP model. Cancel previous drafts and reconcile submitted orders. Under Risk & Controls, enter an order budget and activate autonomy. Verify automatic entry and stop/threshold exits without a per-trade prompt. Verify budget, allocation, exposure and available balance checks. Verify Stop/Pause prevents new submissions, including a pause racing with startup or a queued order. An already sent request may complete after pausing. Restart must show manual mode and a stopped engine. Verify ambiguous submissions stop the engine and are reconciled rather than retried. Mock models cannot activate autonomy. These paths have simulated-exchange coverage; actual account verification is still outstanding.

Verify the saved price bound affects both buy and sell limits, survives restart, and stops active autonomy when saved. Zero means no adverse price movement.
