import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import main as app
from paper_options import PaperOptionEngine, PaperOptionUnavailable, executable_quote

NOW = datetime(2026, 10, 14, 10, 30, 30, tzinfo=app.paper_tradingview.NY)


def signal(symbol="AAPL", direction="CALL", now=NOW):
    return {"symbol": symbol, "direction": direction, "close": 100,
            "signal_type": app.PAPER_ENTRY_RULES["strategy_id"],
            "bar_close_time_ms": int(now.replace(second=0).timestamp() * 1000)}


def quote(now=NOW, ticker="AAPL261103C00100000", bid=4.9, ask=5):
    return {"ticker": ticker, "expiration": "2026-11-03", "strike": 100,
            "bid": bid, "ask": ask, "quote_timestamp": now.timestamp()}


class PaperOptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = NOW
        self.select = Mock(return_value={"contract": quote()})
        self.quotes = Mock(side_effect=lambda symbol, contract, stock: quote(
            self.now, ticker=contract["ticker"], bid=5.2, ask=5.3
        ))
        self.path = Path(self.temp.name) / "paper.json"
        self.engine = self.make_engine()

    def make_engine(self):
        return PaperOptionEngine(
            self.path, select=self.select, quote=self.quotes, clock=lambda: self.now,
            session_bounds=app._paper_session_bounds,
        )

    def rows(self):
        return self.engine.snapshot(self.now)["recent_records"]

    def enter(self, direction="CALL"):
        self.engine.submit(signal(direction=direction), self.now)
        self.engine.tick()
        self.assertEqual(self.rows()[0]["status"], "OPEN")

    def test_submit_is_asynchronous_deduplicated_and_not_for_non_signals(self):
        self.assertTrue(self.engine.submit(signal(), self.now)["queued"])
        self.assertTrue(self.engine.submit(signal(), self.now)["duplicate"])
        self.assertFalse(self.engine.submit(signal(direction=None), self.now)["queued"])
        self.select.assert_not_called()
        self.quotes.assert_not_called()
        self.assertEqual(len(self.rows()), 1)

    def test_ask_entry_bid_exit_and_fixed_sixty_minute_result_not_best_horizon(self):
        self.enter()
        self.assertEqual(self.rows()[0]["entry_cost_usd"], 500)
        self.now += timedelta(minutes=30)
        self.quotes.side_effect = lambda *_: quote(self.now, bid=6, ask=6.1)
        self.engine.tick()
        self.assertEqual(self.rows()[0]["outcomes"]["30"]["gross_pnl_usd"], 100)
        self.assertEqual(self.rows()[0]["status"], "OPEN")
        self.now += timedelta(minutes=30)
        self.quotes.side_effect = lambda *_: quote(self.now, bid=4, ask=4.1)
        self.engine.tick()
        result = self.engine.snapshot(self.now)
        self.assertEqual(result["completed_trades"], 1)
        self.assertEqual(result["gross_pnl_usd"], -100)
        self.assertEqual((result["wins"], result["losses"]), (0, 1))
        self.assertEqual(result["mean_gross_return_pct"], -20)
        self.assertFalse(result["fees_included"])
        self.assertIsNone(result["net_profit_usd"])

    def test_put_profit_is_option_value_increase_not_inverted(self):
        self.select.return_value = {"contract": quote(ticker="AAPL261103P00100000")}
        self.enter("PUT")
        self.now += timedelta(minutes=60)
        self.engine.tick()  # Missing 30-minute observation; no fake backfill.
        self.engine.tick()  # Fresh 60-minute bid.
        row = self.rows()[0]
        self.assertEqual(row["outcomes"]["30"]["status"], "missing_observation")
        self.assertAlmostEqual(row["outcomes"]["60"]["gross_pnl_usd"], 20)
        self.assertEqual(self.quotes.call_count, 1)

    def test_restart_preserves_open_contract_and_dedup(self):
        self.enter()
        self.engine = self.make_engine()
        self.assertTrue(self.engine.submit(signal(), self.now)["duplicate"])
        self.now += timedelta(minutes=30)
        self.engine.tick()
        self.assertEqual(self.select.call_count, 1)
        self.assertEqual(self.rows()[0]["outcomes"]["30"]["status"], "observed")

    def test_all_symbols_can_queue_and_distinct_contracts_coexist(self):
        self.engine.submit(signal("AAPL"), self.now)
        self.engine.submit(signal("NVDA"), self.now)
        self.engine.tick()
        self.select.return_value = {"contract": quote(ticker="NVDA261103C00100000")}
        self.engine.tick()
        self.assertEqual(self.engine.snapshot(self.now)["status_counts"], {"OPEN": 2})
        self.assertEqual(self.select.call_args[0][1], {"AAPL261103C00100000"})

    def test_exact_contract_cannot_be_reused_even_after_closed_trade(self):
        self.enter()
        self.now += timedelta(minutes=60)
        self.engine.tick()
        self.engine.tick()
        self.engine.submit(signal(now=self.now), self.now)
        self.engine.tick()
        self.assertEqual(self.rows()[-1]["reason"], "EXACT_CONTRACT_ALREADY_RECORDED")
        self.assertEqual(self.engine.snapshot(self.now)["completed_trades"], 1)

    def test_no_contract_reason_and_quality_diagnostics_are_retained(self):
        self.select.return_value = {"contract": None, "reason": "NO_ELIGIBLE_CONTRACT",
                                    "diagnostics": {"rejection_reasons": ["IV too high"]}}
        self.engine.submit(signal(), self.now)
        self.engine.tick()
        row = self.rows()[0]
        self.assertEqual(row["status"], "NO_ENTRY")
        self.assertEqual(row["diagnostics"]["rejection_reasons"], ["IV too high"])
        self.assertEqual(row["outcomes"], {})

    def test_stale_queued_entry_is_not_opened_late(self):
        self.engine.submit(signal(), self.now)
        self.now += timedelta(minutes=3)
        self.engine.tick()
        self.assertEqual(self.rows()[0]["reason"], "MISSED_ENTRY_WINDOW")
        self.select.assert_not_called()

    def test_no_entry_near_close_or_on_holiday(self):
        for now in (NOW.replace(hour=15, minute=30), NOW.replace(month=12, day=25)):
            self.now = now
            self.engine.submit(signal(now=now), now)
            self.engine.tick()
        self.select.assert_not_called()
        self.assertEqual(self.engine.snapshot(self.now)["status_counts"], {"NO_ENTRY": 2})

    def test_missed_exit_and_provider_failure_are_not_zero_or_profitable_trades(self):
        self.enter()
        self.now += timedelta(minutes=30)
        self.quotes.side_effect = RuntimeError("Authorization: private-do-not-store")
        self.engine.tick()
        self.now += timedelta(minutes=33)
        self.engine.tick()
        row = self.rows()[0]
        self.assertEqual(row["status"], "EXIT_UNAVAILABLE")
        self.assertEqual(row["outcomes"]["60"]["status"], "missing_observation")
        self.assertNotIn("gross_pnl_usd", row["outcomes"]["60"])
        self.assertNotIn("private-do-not-store", self.path.read_text())
        self.assertEqual(self.engine.snapshot(self.now)["completed_trades"], 0)

    def test_wrong_contract_or_stale_quote_cannot_produce_an_exit(self):
        for wrong in (quote(ticker="WRONG"), quote(now=NOW - timedelta(hours=1)),
                      {**quote(), "strike": 101}):
            with self.subTest(wrong=wrong):
                self.path.unlink(missing_ok=True)
                self.now = NOW
                self.enter()
                self.now += timedelta(minutes=60)
                self.quotes.side_effect = None
                self.quotes.return_value = wrong
                self.engine.tick()
                self.engine.tick()
                self.assertEqual(self.rows()[0]["status"], "EXIT_UNAVAILABLE")

    def test_exit_is_prioritized_over_new_selection(self):
        self.enter()
        self.now += timedelta(minutes=30)
        self.engine.submit(signal("NVDA", now=self.now), self.now)
        self.engine.tick()
        self.assertEqual(self.select.call_count, 1)
        self.assertEqual(self.quotes.call_count, 1)

    def test_fresh_zero_bid_exit_is_total_loss_not_excluded_from_results(self):
        self.enter()
        self.now += timedelta(minutes=60)
        self.quotes.side_effect = lambda *_: quote(self.now, bid=0, ask=0.05)
        self.engine.tick()
        self.engine.tick()
        result = self.engine.snapshot(self.now)
        self.assertEqual(result["gross_pnl_usd"], -500)
        self.assertEqual(result["losses"], 1)
        outcome = self.rows()[0]["outcomes"]["60"]
        self.assertEqual(outcome["liquidation_basis"], "zero_bid_zero_proceeds_not_a_fill")

    def test_interrupted_selection_does_not_repeat_paid_provider_requests(self):
        self.engine.submit(signal(), self.now)
        state = json.loads(self.path.read_text())
        row = next(iter(state["records"].values()))
        row["status"] = "SELECTING"
        self.path.write_text(json.dumps(state))
        self.engine = self.make_engine()
        self.engine.tick()
        self.select.assert_not_called()
        self.assertEqual(self.rows()[0]["reason"], "INTERRUPTED_ENTRY_SELECTION")

    def test_interrupted_exit_is_missing_not_fabricated_or_repeated(self):
        self.enter()
        state = json.loads(self.path.read_text())
        row = next(iter(state["records"].values()))
        row["outcomes"]["60"] = {"status": "OBSERVING"}
        self.path.write_text(json.dumps(state))
        self.engine = self.make_engine()
        self.engine.tick()
        self.quotes.assert_not_called()
        self.assertEqual(self.rows()[0]["status"], "EXIT_UNAVAILABLE")
        self.assertEqual(self.engine.snapshot(self.now)["exit_observation_coverage_pct"], 0)

    def test_corrupt_store_is_visible_and_not_overwritten(self):
        for text in ("broken", "[]", '{"version":1,"records":{"bad":{}}}'):
            self.path.write_text(text)
            with self.assertRaises(RuntimeError):
                self.engine.submit(signal(), self.now)
            self.assertEqual(self.engine.snapshot(self.now)["status"], "storage_error")
            self.assertEqual(self.path.read_text(), text)

    def test_worker_start_is_idempotent(self):
        first = self.engine.start()
        self.assertIs(first, self.engine.start())
        self.engine.stop_event.set()
        first.join(timeout=2)
        self.assertFalse(first.is_alive())

    def test_slow_provider_never_holds_webhook_storage_lock(self):
        entered, release = threading.Event(), threading.Event()
        def slow_selection(*_):
            entered.set()
            release.wait(5)
            return {"contract": quote()}
        self.select.side_effect = slow_selection
        self.engine.submit(signal(), self.now)
        thread = self.engine.start()
        try:
            self.assertTrue(entered.wait(2))
            start = time.monotonic()
            self.assertTrue(self.engine.submit(signal("NVDA"), self.now)["queued"])
            self.assertEqual(self.engine.snapshot(self.now)["total_signals"], 2)
            self.assertLess(time.monotonic() - start, 1)
        finally:
            self.engine.stop_event.set()
            release.set()
            thread.join(timeout=2)

    def test_quote_rejects_future_missing_nan_and_delayed_values(self):
        for changes in ({"bid": 0}, {"ask": 4}, {"ask": float("nan")},
                        {"bid": True}, {"quote_timestamp": None},
                        {"quote_timestamp": NOW.timestamp() + 1},
                        {"quote_timestamp": NOW.timestamp() - 31}):
            with self.subTest(changes=changes), self.assertRaises(PaperOptionUnavailable):
                executable_quote({**quote(), **changes}, NOW, 30)


class PaperOptionProviderTests(unittest.TestCase):
    def raw(self, side="CALL", iv=0.35, ask=5):
        ticker = f'AAPL261103{side[0]}00100000'
        return {
            "details": {"ticker": ticker, "expiration_date": "2026-11-03", "strike_price": 100},
            "last_quote": {"bid": ask - 0.1, "ask": ask, "last_updated": NOW.timestamp()},
            "day": {"volume": 5000, "close": ask}, "open_interest": 6000,
            "implied_volatility": iv, "greeks": {"delta": 0.5 if side == "CALL" else -0.5,
                                                 "theta": -0.1},
        }

    def test_current_provider_pipeline_keeps_side_and_never_sends_or_mutates_trades(self):
        for side in ("CALL", "PUT"):
            raw = self.raw(side)
            with (
                patch.object(app.bot, "now_new_york", return_value=NOW),
                patch.object(app.time, "time", return_value=NOW.timestamp()),
                patch.object(app.bot, "WATCHLIST", ["AAPL"]),
                patch.object(app.bot, "_load_trade_store", return_value={"trades": {}}),
                patch.object(app, "_existing_contract_tickers", return_value=set()),
                patch.object(app, "_marketdata_account_credit_limit_reason", return_value=None),
                patch.object(app, "massive_chain", return_value=[raw]) as chain,
                patch.object(app, "provider_contract_snapshot", return_value={"results": [raw]}) as refresh,
                patch.object(app, "create_trade") as live,
                patch.object(app.bot, "_save_trade_store") as save,
                patch.object(app.bot, "telegram_send_card") as telegram,
                patch.object(app, "OPTION_ENTRY_MIN_PREMIUM", 2.5),
                patch.object(app, "OPTION_ENTRY_MAX_PREMIUM", 6),
            ):
                selected = app._paper_select_contract(
                    {"symbol": "AAPL", "direction": side, "stock_close": 100}, set()
                )
                self.assertIsNotNone(selected["contract"])
                chain.assert_called_once_with("AAPL", side)
                refresh.assert_called_once()
                live.assert_not_called()
                save.assert_not_called()
                telegram.assert_not_called()

    def test_current_iv_and_budget_gates_reject_contracts_without_quote_retry(self):
        for iv, ask in ((0.5001, 5), (0.35, 6.01)):
            with (
                patch.object(app.bot, "now_new_york", return_value=NOW),
                patch.object(app.time, "time", return_value=NOW.timestamp()),
                patch.object(app.bot, "WATCHLIST", ["AAPL"]),
                patch.object(app.bot, "_load_trade_store", return_value={"trades": {}}),
                patch.object(app, "_existing_contract_tickers", return_value=set()),
                patch.object(app, "_marketdata_account_credit_limit_reason", return_value=None),
                patch.object(app, "massive_chain", return_value=[self.raw(iv=iv, ask=ask)]),
                patch.object(app, "provider_contract_snapshot") as refresh,
                patch.object(app, "OPTION_ENTRY_MIN_PREMIUM", 2.5),
                patch.object(app, "OPTION_ENTRY_MAX_PREMIUM", 6),
            ):
                selected = app._paper_select_contract(
                    {"symbol": "AAPL", "direction": "CALL", "stock_close": 100}, set()
                )
                self.assertIsNone(selected["contract"])
                refresh.assert_not_called()

    def test_credit_pause_and_excluded_symbol_make_no_chain_requests(self):
        with (
            patch.object(app.bot, "WATCHLIST", ["AAPL", "MSTR"]),
            patch.object(app.bot, "EXCLUDED_SYMBOLS", {"MSTR"}),
            patch.object(app, "DATA_PROVIDER", "marketdata"),
            patch.object(app, "_marketdata_account_credit_limit_reason", return_value="credit limit"),
            patch.object(app, "massive_chain") as chain,
        ):
            for symbol in ("AAPL", "MSTR", "NOT_ALLOWED"):
                with self.assertRaises(PaperOptionUnavailable):
                    app._paper_select_contract(
                        {"symbol": symbol, "direction": "CALL", "stock_close": 100}, set()
                    )
            chain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
