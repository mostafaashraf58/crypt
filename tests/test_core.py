import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine.core import Engine
from engine.backtest import run_backtest
from engine.fixtures import generate_candles


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = Engine(self.directory.name)

    def tearDown(self):
        self.engine.close()
        self.directory.cleanup()

    def test_threshold_boundary_and_paper_accounting(self):
        self.engine.set_running(True)
        result = self.engine.tick("BTC/USD", 100, .65, event_id="signal-buy")
        self.assertEqual(result["action"], "BUY")
        snapshot = self.engine.snapshot()
        self.assertEqual(len(snapshot["positions"]), 1)
        self.assertAlmostEqual(snapshot["cash"] + snapshot["positions"][0]["qty"] * result["order"]["price"] + result["order"]["fee"], 100000)
        self.assertLess(snapshot["equity"], 100000)

    def test_exit_threshold_defaults_and_separate_threshold(self):
        config = self.engine.configure_strategy({"symbol": "BTC/USD", "threshold": .7})
        self.assertEqual(config["exit_threshold"], .7)
        config = self.engine.configure_strategy({"symbol": "BTC/USD", "threshold": .7, "exit_threshold": .45})
        self.assertEqual(config["exit_threshold"], .45)
        self.engine.paper_order("BTC/USD", "BUY", 10, 100)
        self.engine.set_running(True)
        self.assertEqual(self.engine.tick("BTC/USD", 101, .5)["action"], "HOLD")

    def test_fixed_stop_is_independent_of_model_and_pause(self):
        self.engine.paper_order("BTC/USD", "BUY", 10, 100)
        result = self.engine.tick("BTC/USD", 95, .99, completed=False)
        self.assertEqual(result["action"], "SELL")
        self.assertIn("Fixed stop", result["reason"])
        self.assertEqual(self.engine.snapshot()["positions"], [])

    def test_trailing_stop_uses_high_watermark(self):
        self.engine.configure_strategy({"symbol": "BTC/USD", "fixed_stop": None, "trailing_stop": .05})
        self.engine.paper_order("BTC/USD", "BUY", 10, 100)
        self.engine.tick("BTC/USD", 120, completed=False)
        result = self.engine.tick("BTC/USD", 113, completed=False)
        self.assertEqual(result["action"], "SELL")
        self.assertIn("Trailing stop", result["reason"])

    def test_invalid_and_stale_data_cannot_trade(self):
        self.engine.set_running(True)
        for q in (float("nan"), float("inf"), -.01, 1.01, True):
            with self.assertRaises(ValueError):
                self.engine.tick("BTC/USD", 100, q)
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.assertEqual(self.engine.tick("BTC/USD", 100, .99, old)["action"], "WAIT")
        self.assertFalse(self.engine.snapshot()["positions"])

    def test_duplicate_order_survives_restart(self):
        first = self.engine.paper_order("BTC/USD", "BUY", 10, 100, "order-1")
        duplicate = self.engine.paper_order("BTC/USD", "BUY", 10, 100, "order-1")
        self.assertEqual(first, duplicate)
        self.engine.close()
        self.engine = Engine(self.directory.name)
        self.assertEqual(self.engine.paper_order("BTC/USD", "BUY", 10, 100, "order-1"), first)
        self.assertEqual(len(self.engine.snapshot()["orders"]), 1)
        self.assertFalse(self.engine.snapshot()["running"])

    def test_second_engine_cannot_overwrite_same_account_state(self):
        with self.assertRaises(RuntimeError):
            Engine(self.directory.name)

    def test_per_interval_duplicate_is_blocked(self):
        self.engine.set_running(True)
        self.engine.tick("BTC/USD", 100, .7)
        result = self.engine.tick("BTC/USD", 100, .1)
        self.assertEqual(result["action"], "WAIT")
        self.assertIn("already been evaluated", result["reason"])

    def test_fixture_interval_id_allows_accelerated_replay(self):
        self.engine.set_running(True)
        self.assertEqual(self.engine.tick("BTC/USD", 100, .7, interval_id=1)["action"], "BUY")
        self.assertEqual(self.engine.tick("BTC/USD", 100, .1, interval_id=2)["action"], "SELL")

    def test_disabled_strategy_still_applies_stop_loss(self):
        self.engine.configure_strategy({"symbol": "BTC/USD", "enabled": False})
        self.engine.paper_order("BTC/USD", "BUY", 10, 100)
        self.assertEqual(self.engine.tick("BTC/USD", 90, completed=False)["action"], "SELL")

    def test_manual_holding_receives_default_risk_configuration(self):
        self.engine.paper_order("AAPL", "BUY", 1, 200)
        self.assertTrue(any(s["symbol"] == "AAPL" for s in self.engine.snapshot()["strategies"]))

    def test_risk_limits_and_oversell(self):
        with self.assertRaises(ValueError):
            self.engine.paper_order("BTC/USD", "BUY", 300, 100)
        self.engine.paper_order("BTC/USD", "BUY", 10, 100)
        with self.assertRaises(ValueError):
            self.engine.paper_order("BTC/USD", "SELL", 11, 100)
        with self.assertRaises(ValueError):
            self.engine.configure_strategy({"allocation": .5})

    def test_live_mode_never_executes_and_requires_real_reconciliation(self):
        self.engine.set_mode("live")
        self.engine.set_running(True)
        self.assertEqual(self.engine.tick("BTC/USD", 100, .9)["action"], "WAIT")
        with self.assertRaises(ValueError):
            self.engine.paper_order("BTC/USD", "BUY", 10, 100)
        self.engine.reconcile_live_account({"cash": 100000, "equity": 100000}, [])
        result = self.engine.tick("BTC/USD", 100, .9, source="provider", event_id="proposal-1")
        self.assertEqual(result["action"], "BUY")
        self.assertEqual(self.engine.snapshot()["orders"], [])
        proposal = self.engine.claim_proposal("proposal-1")
        self.assertEqual(proposal["status"], "submitting")
        with self.assertRaises(ValueError):
            self.engine.claim_proposal("proposal-1")
        self.engine.finish_proposal("proposal-1", "submitted", {"id": "broker-1"})

    def test_live_confirmation_rechecks_quote_and_cash(self):
        self.engine.set_mode("live")
        self.engine.set_running(True)
        self.engine.reconcile_live_account({"cash": 100000, "equity": 100000}, [])
        self.engine.tick("BTC/USD", 100, .9, source="alpaca", event_id="proposal-risk", interval_id=1)
        with self.assertRaises(ValueError):
            self.engine.claim_proposal("proposal-risk", quote_price=300)
        self.engine.reconcile_live_account({"cash": 100, "equity": 100000}, [])
        with self.assertRaises(ValueError):
            self.engine.claim_proposal("proposal-risk", quote_price=100)

    def test_submitted_order_blocks_new_proposal_until_reconciled(self):
        self.engine.set_mode("live")
        self.engine.set_running(True)
        self.engine.reconcile_live_account({"cash": 100000, "equity": 100000}, [])
        self.engine.tick("BTC/USD", 100, .9, source="alpaca", event_id="proposal-active", interval_id=1)
        self.engine.claim_proposal("proposal-active", quote_price=100)
        self.engine.finish_proposal("proposal-active", "submitted", {"id": "broker"})
        result = self.engine.tick("BTC/USD", 100, .9, source="alpaca", interval_id=2)
        self.assertEqual(result["action"], "WAIT")
        self.assertIn("unresolved", result["reason"])
        self.engine.finish_proposal("proposal-active", "filled", {"id": "broker"})

    def test_manual_live_draft_is_durable_and_never_executes(self):
        self.engine.set_mode("live")
        self.engine.reconcile_live_account({"provider": "alpaca", "cash": 100000, "equity": 100000}, [])
        proposal = self.engine.propose_order("AAPL", "BUY", 5, 200, "manual-draft", source="alpaca")
        self.assertEqual(proposal["status"], "awaiting_review")
        self.assertEqual(self.engine.snapshot()["orders"], [])
        self.assertEqual(self.engine.snapshot()["positions"], [])
        self.assertEqual(self.engine.propose_order("AAPL", "BUY", 5, 200, "manual-draft", source="alpaca"), proposal)
        self.engine.close()
        self.engine = Engine(self.directory.name)
        self.assertEqual(self.engine.propose_order("AAPL", "BUY", 5, 200, "manual-draft", source="alpaca"), proposal)
        self.assertEqual(len(self.engine.snapshot()["proposals"]), 1)

    def test_manual_live_draft_blocks_fake_or_stale_data_and_paper_mode(self):
        with self.assertRaises(ValueError):
            self.engine.propose_order("AAPL", "BUY", 5, 200)
        self.engine.set_mode("live")
        self.engine.reconcile_live_account({"provider": "alpaca", "cash": 100000, "equity": 100000}, [])
        with self.assertRaises(ValueError):
            self.engine.propose_order("AAPL", "BUY", 5, 200, source="fixture")
        with self.assertRaises(ValueError):
            self.engine.propose_order("AAPL", "BUY", 5, 200, timestamp=datetime.now(timezone.utc) - timedelta(minutes=5))
        with self.assertRaises(ValueError):
            self.engine.propose_order("AAPL", "BUY", float("nan"), 200)
        with self.assertRaises(ValueError):
            self.engine.propose_order("AAPL", "BUY", 5, 200, source="kraken")

    def test_manual_live_draft_checks_broker_orders_and_reserves_cash(self):
        self.engine.set_mode("live")
        account = {"provider": "alpaca", "cash": 1000, "equity": 100000,
                   "orders": [{"symbol": "AAPL", "side": "buy", "qty": "2", "filled_qty": "0", "limit_price": "200", "status": "new"}]}
        self.engine.reconcile_live_account(account, [])
        with self.assertRaises(ValueError):
            self.engine.propose_order("AAPL", "BUY", 1, 200)
        account["orders"] = []
        self.engine.reconcile_live_account(account, [])
        self.engine.propose_order("AAPL", "BUY", 3, 200)
        with self.assertRaises(ValueError):
            self.engine.propose_order("MSFT", "BUY", 3, 200)

    def test_manual_live_draft_checks_allocation_and_sell_holdings(self):
        self.engine.set_mode("live")
        self.engine.reconcile_live_account({"provider": "alpaca", "cash": 100000, "equity": 100000},
                                           [{"symbol": "BTCUSD", "qty": "1", "avg_entry_price": "100", "current_price": "100"}])
        with self.assertRaises(ValueError):
            self.engine.propose_order("AAPL", "BUY", 300, 100)
        with self.assertRaises(ValueError):
            self.engine.propose_order("BTC/USD", "SELL", 2, 100)
        result = self.engine.propose_order("BTC/USD", "SELL", .5, 100)
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(self.engine.snapshot()["positions"][0]["qty"], 1)

    def test_drawdown_blocks_live_buys_but_allows_risk_reducing_sell(self):
        self.engine.set_mode("live")
        self.engine.reconcile_live_account({"provider": "alpaca", "cash": 99900, "equity": 100000},
                                           [{"symbol": "AAPL", "qty": 1, "avg_entry_price": 100, "current_price": 100}])
        self.engine.reconcile_live_account({"provider": "alpaca", "cash": 79900, "equity": 80000},
                                           [{"symbol": "AAPL", "qty": 1, "avg_entry_price": 100, "current_price": 100}])
        with self.assertRaises(ValueError):
            self.engine.propose_order("MSFT", "BUY", 1, 100)
        proposal = self.engine.propose_order("AAPL", "SELL", 1, 100)
        self.assertEqual(self.engine.claim_proposal(proposal["id"], quote_price=100)["side"], "SELL")

    def test_automatic_live_signal_obeys_outstanding_broker_order_guard(self):
        self.engine.set_mode("live")
        self.engine.reconcile_live_account({"provider": "alpaca", "cash": 100000, "equity": 100000,
                                           "orders": [{"symbol": "BTC/USD", "side": "buy", "status": "new", "qty": 1}]}, [])
        self.engine.set_running(True)
        result = self.engine.tick("BTC/USD", 100, .9, source="alpaca", interval_id=1)
        self.assertEqual(result["action"], "WAIT")
        self.assertIn("outstanding broker order", result["reason"])

    def test_paper_holdings_do_not_become_live_holdings(self):
        self.engine.paper_order("BTC/USD", "BUY", 10, 100)
        self.engine.set_mode("live")
        self.assertEqual(self.engine.snapshot()["positions"], [])
        self.assertEqual(self.engine.snapshot()["equity"], 0)
        self.engine.set_mode("paper")
        self.assertEqual(self.engine.snapshot()["positions"][0]["qty"], 10)

    def test_audit_is_persistent(self):
        self.engine.configure_strategy({"threshold": .72})
        self.assertEqual(self.engine.snapshot()["audit"][0]["event"], "strategy.updated")


class BacktestTests(unittest.TestCase):
    def test_fixtures_are_deterministic_and_labeled(self):
        self.assertEqual(generate_candles(), generate_candles())
        self.assertEqual(generate_candles()[0]["source"], "fixture")

    def test_signal_executes_next_bar(self):
        candles = [dict(time=i * 3600 + 1000, open=p, high=p * 1.01, low=p * .99, close=p, volume=1, source="fixture") for i, p in enumerate((100, 200, 300, 400))]
        result = run_backtest(candles=candles, probabilities=[.9, .1, .1, .1], fixed_stop=None, trailing_stop=None, fee_rate=0, slippage=0)
        self.assertEqual(result["orders"][0]["price"], 200)
        self.assertEqual(result["orders"][1]["price"], 300)
        self.assertEqual(result["total_trades"], 1)
        self.assertAlmostEqual(result["final_equity"], 105000)

    def test_future_predictions_cannot_change_past_equity(self):
        candles = generate_candles(count=60)
        a = run_backtest(candles=candles, probabilities=[.9] * 60)
        b = run_backtest(candles=candles, probabilities=[.9] * 30 + [.1] * 30)
        self.assertEqual(a["equity_curve"][:31], b["equity_curve"][:31])

    def test_gap_stop_uses_worse_opening_price(self):
        candles = [dict(time=i * 3600 + 1000, open=o, high=h, low=l, close=c, volume=1) for i, (o, h, l, c) in enumerate(((100, 101, 99, 100), (100, 101, 99, 100), (80, 85, 75, 82)))]
        result = run_backtest(candles=candles, probabilities=[.9] * 3, fixed_stop=.03, trailing_stop=None, fee_rate=0, slippage=0)
        self.assertEqual(result["orders"][1]["price"], 80)
        self.assertIn("Fixed", result["orders"][1]["reason"])

    def test_backtest_outputs_are_finite(self):
        result = run_backtest()
        for key in ("final_equity", "return_pct", "max_drawdown", "sharpe", "win_rate"):
            self.assertTrue(math.isfinite(result[key]))


if __name__ == "__main__":
    unittest.main()
