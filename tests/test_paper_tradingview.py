import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import main as app
import paper_tradingview as paper

NOW = datetime(2026, 10, 14, 10, 30, 30, tzinfo=paper.NY)


def payload(now=NOW):
    end = now.replace(second=0, microsecond=0)
    start = end - timedelta(minutes=30)
    return {
        "secret": "test-only-secret", "ticker": "NASDAQ:AAPL",
        "signal_type": paper.SIGNAL_TYPE, "timeframe": "30", "bar_closed": True,
        "regular_session": True, "tenkan_period": 9, "tenkan_timeframe": "30",
        "tenkan_method": "HIGH_LOW_MIDPOINT",
        "ema200_period": 200, "ema200_timeframe": "5",
        "vwap_anchor": "US_REGULAR_SESSION", "vwap_timeframe": "5",
        "bar_time_ms": int(start.timestamp() * 1000),
        "bar_close_time_ms": int(end.timestamp() * 1000),
        "previous_bar_close_time_ms": int(start.timestamp() * 1000),
        "previous_close": 99, "close": 101,
        "previous_levels": {"tenkan9_30m": 100, "ema200_5m": None, "session_vwap": None},
        "levels": {"tenkan9_30m": 100, "ema200_5m": None, "session_vwap": None},
    }


def parse(body, now=NOW):
    return paper.from_webhook(body, now, ["AAPL"], {"MSTR"}, app._paper_session_bounds)


class PaperValidationTests(unittest.TestCase):
    def test_each_indicator_can_trigger_without_other_gates(self):
        for name in ("tenkan9_30m", "session_vwap"):
            for before, close, expected in ((99, 101, "CALL"), (101, 99, "PUT")):
                body = payload()
                body.update(previous_close=before, close=close, direction="FAKE")
                body["previous_levels"] = {name: 100}
                body["levels"] = {name: 100}
                result = parse(body)
                self.assertEqual(result["direction"], expected)
                self.assertNotIn("secret", result)

    def test_ema200_never_triggers_entry_or_vetoes_other_crosses(self):
        body = payload()
        body.update(previous_levels={"ema200_5m": 100}, levels={"ema200_5m": 100})
        self.assertIsNone(parse(body)["direction"])
        for direction, before, close in (("CALL", 99, 101), ("PUT", 101, 99)):
            for average, expected in (
                (90, "ALIGNED" if direction == "CALL" else "OPPOSED"),
                (110, "OPPOSED" if direction == "CALL" else "ALIGNED"),
                (close, "NEUTRAL"), (None, "UNAVAILABLE"),
            ):
                with self.subTest(direction=direction, average=average):
                    body = payload()
                    body.update(previous_close=before, close=close)
                    body["levels"]["ema200_5m"] = average
                    result = parse(body)
                    self.assertEqual(result["direction"], direction)
                    self.assertEqual(result["ema200_context"]["preference"], expected)
                    self.assertFalse(result["ema200_context"]["entry_gate"])

    def test_conflict_open_missing_and_no_cross(self):
        body = payload()
        body["previous_levels"]["session_vwap"] = 98
        body["levels"]["session_vwap"] = 102
        self.assertEqual(parse(body)["status"], "WAIT - CONFLICTING CROSSES")
        for levels, close, status in (
            ({}, 101, "WAIT - INDICATOR DATA"),
            ({"tenkan9_30m": 100}, 99, "WAIT - NO CROSS"),
        ):
            body = payload()
            body.update(levels=levels, previous_levels=levels, close=close)
            self.assertEqual(parse(body)["status"], status)
        with self.assertRaises(paper.PaperDataError):
            parse({**payload(), "bar_closed": False})

    def test_exact_rules_timestamps_and_numbers_are_required(self):
        bad = [
            {"ticker": "MSTR"}, {"timeframe": "60"}, {"timeframe": "5"},
            {"tenkan_timeframe": "5"}, {"tenkan_timeframe": "60"},
            {"ema200_timeframe": "30"}, {"vwap_timeframe": "30"},
            {"tenkan_method": "EMA"}, {"tenkan_method": "SMA"},
            {"tenkan_period": 26}, {"ema200_period": 2000},
            {"signal_type": "MACD_1H_EMA200"}, {"regular_session": False},
            {"signal_type": "ORCA_PAPER_CROSS_V1"},
            {"signal_type": "ORCA_PAPER_CROSS_V2"},
            {"signal_type": "ORCA_PAPER_CROSS_V3"},
            {"signal_type": "ORCA_PAPER_CROSS_V4"},
            {"vwap_anchor": "WEEK"}, {"close": True}, {"close": float("nan")},
            {"close": 0}, {"previous_close": float("inf")},
            {"bar_time_ms": True}, {"bar_close_time_ms": payload()["bar_close_time_ms"] + 1000},
            {"previous_bar_close_time_ms": payload()["bar_time_ms"] - 300000},
            {"previous_levels": []}, {"levels": {"tenkan9_30m": float("nan")}},
        ]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(paper.PaperDataError):
                parse({**payload(), **changes})
        for offset in (-31, 91):
            with self.assertRaises(paper.PaperDataError):
                parse(payload(), NOW + timedelta(seconds=offset))

    def test_weekend_holiday_and_short_session(self):
        for date in ((2026, 10, 17, 10, 5), (2026, 12, 25, 10, 5),
                     (2026, 11, 27, 13, 5), (2026, 10, 14, 9, 30)):
            now = datetime(*date, 30, tzinfo=paper.NY)
            with self.assertRaises(paper.PaperDataError):
                parse(payload(now), now)
        now = datetime(2026, 11, 27, 13, 0, 30, tzinfo=paper.NY)
        self.assertEqual(parse(payload(now), now)["direction"], "CALL")

    def test_opening_bar_keeps_tenkan_cross_but_never_crosses_yesterdays_vwap(self):
        now = NOW.replace(hour=10, minute=0)
        body = payload(now)
        body["previous_bar_close_time_ms"] = int(
            (now - timedelta(days=1)).replace(hour=16, minute=0, second=0).timestamp() * 1000
        )
        body["previous_levels"]["session_vwap"] = 98
        body["levels"]["session_vwap"] = 102
        self.assertEqual(parse(body, now)["direction"], "CALL")
        body["previous_bar_close_time_ms"] -= 86400000
        with self.assertRaises(paper.PaperDataError):
            parse(body, now)

    def test_five_minute_bar_and_unaligned_thirty_minute_bar_are_rejected(self):
        body = payload()
        body["bar_time_ms"] = body["bar_close_time_ms"] - 300000
        body["previous_bar_close_time_ms"] = body["bar_time_ms"]
        with self.assertRaises(paper.PaperDataError):
            parse(body)
        now = NOW.replace(minute=35)
        with self.assertRaises(paper.PaperDataError):
            parse(payload(now), now)
        now = NOW.replace(hour=10, minute=0)
        with self.assertRaises(paper.PaperDataError):
            parse(payload(now), now)  # No regular-session bar ends at 09:30.


class PaperStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.recorder = paper.PaperRecorder(Path(self.temp.name) / "paper.json")

    def test_dedup_order_conflict_and_restart(self):
        candle = parse(payload())
        self.recorder.record(candle, NOW)
        self.assertTrue(self.recorder.record(candle, NOW)["duplicate"])
        with self.assertRaises(paper.PaperConflictError):
            self.recorder.record({**candle, "close": 102}, NOW)
        with self.assertRaises(paper.PaperConflictError):
            self.recorder.record({**candle, "bar_close_time_ms": candle["bar_close_time_ms"] - 300000}, NOW)
        recovered = paper.PaperRecorder(self.recorder.path).snapshot(NOW)
        self.assertEqual(recovered["signal_count"], 1)
        self.assertEqual(recovered["status"], "receiving")
        self.assertNotIn("test-only-secret", self.recorder.path.read_text())

    def test_stock_outcomes_and_missing_horizons_not_option_profit(self):
        first = parse(payload())
        self.recorder.record(first, NOW)
        for minutes in (30, 90):
            now = NOW + timedelta(minutes=minutes)
            body = payload(now)
            body.update(previous_close=102, close=102)
            self.recorder.record(parse(body, now), now)
        snapshot = self.recorder.snapshot(now)
        self.assertEqual(snapshot["signal_count"], 1)
        row = snapshot["recent_signals"][0]
        self.assertAlmostEqual(row["outcomes"]["30"]["directional_move_pct"], 100 / 101, places=5)
        self.assertEqual(row["outcomes"]["60"]["status"], "missing_observation")
        self.assertNotIn("15", row["outcomes"])
        self.assertEqual(snapshot["outcome_horizons_minutes"], [30, 60])
        self.assertIsNone(row["option_profitability"])
        self.assertEqual(row["contract_status"], "NOT_EVALUATED")

    def test_put_outcome_sign_and_heartbeat_without_signal(self):
        body = payload()
        body.update(previous_close=101, close=99)
        self.recorder.record(parse(body), NOW)
        now = NOW + timedelta(minutes=60)
        body = payload(now)
        body.update(previous_close=98, close=98)
        self.recorder.record(parse(body, now), now)
        outcomes = self.recorder.snapshot(now)["recent_signals"][0]["outcomes"]
        self.assertGreater(outcomes["60"]["directional_move_pct"], 0)
        self.assertEqual(outcomes["30"]["status"], "missing_observation")

    def test_storage_errors_are_visible_and_never_reset(self):
        for data in ('broken', '[]', '{"version":1,"latest":{"AAPL":{}},"signals":[]}'):
            self.recorder.path.write_text(data)
            with self.assertRaises(RuntimeError):
                self.recorder.record(parse(payload()), NOW)
            self.assertEqual(self.recorder.path.read_text(), data)
            self.assertEqual(self.recorder.snapshot(NOW)["status"], "storage_error")

    def test_fresh_status_expires_and_empty_status_is_not_running(self):
        self.assertEqual(self.recorder.snapshot(NOW)["status"], "awaiting_tradingview_alert")
        self.recorder.record(parse(payload()), NOW)
        self.assertEqual(self.recorder.snapshot(NOW + timedelta(minutes=31))["status"], "receiving")
        self.assertEqual(self.recorder.snapshot(NOW + timedelta(minutes=33))["status"], "waiting_for_fresh_candles")

    def test_preference_is_saved_and_legacy_strategy_is_not_mixed(self):
        body = payload()
        body["levels"]["ema200_5m"] = 110
        self.recorder.record(parse(body), NOW)
        current = self.recorder.snapshot(NOW)
        self.assertEqual(current["recent_signals"][0]["ema200_context"]["preference"], "OPPOSED")
        state = json.loads(self.recorder.path.read_text())
        old = {**state["signals"][0], "signal_type": "ORCA_PAPER_CROSS_V4"}
        state["signals"].append(old)
        self.recorder.path.write_text(json.dumps(state))
        results = self.recorder.snapshot(NOW)
        self.assertEqual(results["signal_count"], 1)
        self.assertEqual(results["legacy_signal_count"], 1)
        self.assertEqual(len(json.loads(self.recorder.path.read_text())["signals"]), 2)


class PaperHttpTests(unittest.TestCase):
    def test_http_auth_record_results_script_and_no_live_side_effects(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(app, "_PAPER_RECORDER", paper.PaperRecorder(Path(temp) / "paper.json")),
            patch.object(app, "_PAPER_OPTIONS", app.paper_options.PaperOptionEngine(
                Path(temp) / "options.json", select=app._paper_select_contract,
                quote=app._paper_option_quote, clock=lambda: NOW,
                session_bounds=app._paper_session_bounds,
            )),
            patch.object(app.bot, "env", return_value="test-only-secret") as secret,
            patch.object(app.bot, "now_new_york", return_value=NOW),
            patch.object(app.bot, "WATCHLIST", ["AAPL"]),
            patch.object(app.bot, "EXCLUDED_SYMBOLS", {"MSTR"}),
            patch.object(app, "create_trade") as trade,
            patch.object(app, "massive_chain") as chain,
            patch.object(app.bot, "telegram_send_card") as telegram,
            patch.object(app.CanonicalHandler, "log_message"),
        ):
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.CanonicalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            def send(body):
                request = Request(base + "/paper-signal", json.dumps(body).encode(),
                                  {"Content-Type": "application/json"})
                try:
                    with urlopen(request, timeout=5) as response:
                        return response.status, json.load(response)
                except HTTPError as error:
                    return error.code, json.load(error)
            try:
                self.assertEqual(send({**payload(), "secret": "wrong"})[0], 401)
                self.assertEqual(send([])[0], 400)
                self.assertEqual(send(payload())[0], 200)
                self.assertTrue(send(payload())[1]["duplicate"])
                self.assertEqual(send({**payload(), "close": 102})[0], 409)
                self.assertEqual(send({**payload(), "bar_closed": False})[0], 400)
                secret.return_value = ""
                self.assertEqual(send(payload())[0], 503)
                with urlopen(base + "/paper-results") as response:
                    results = json.load(response)
                    self.assertEqual(results["signal_count"], 1)
                    self.assertEqual(results["paper_options"]["status_counts"], {"QUEUED": 1})
                    self.assertEqual(results["recent_signals"][0]["contract_status"], "QUEUED")
                with urlopen(base + "/paper-option-results") as response:
                    self.assertEqual(json.load(response)["total_signals"], 1)
                with urlopen(base + "/paper-script") as response:
                    self.assertIn(paper.SIGNAL_TYPE, response.read().decode())
                trade.assert_not_called()
                chain.assert_not_called()
                telegram.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


class PaperPineContractTests(unittest.TestCase):
    def test_script_has_thirty_minute_tenkan_confirmation_and_preserves_five_minute_context(self):
        source = (Path(__file__).resolve().parents[1] / "ORCA_PAPER_TENKAN_VWAP_30M.pine").read_text()
        for text in (
            'timeframe.multiplier != 30', 'not chart.is_standard',
            'syminfo.session != session.regular',
            'tenkanHigh9 = ta.highest(high, 9)',
            'tenkanLow9 = ta.lowest(low, 9)',
            'tenkan9 = bar_index >= 8 ? (tenkanHigh9 + tenkanLow9) / 2.0 : na',
            'ta.ema(close, 200)', 'ta.vwap(hlc3, session.isfirstbar_regular)',
            'request.security(syminfo.tickerid, "5", ta.ema(close, 200)',
            'request.security(syminfo.tickerid, "5", ta.vwap(hlc3, session.isfirstbar_regular)',
            'lookahead=barmerge.lookahead_off',
            '\\"timeframe\\":\\"30\\"', '\\"tenkan_timeframe\\":\\"30\\"',
            'session.isfirstbar_regular ? na : sessionVwap[1]',
            'barstate.isrealtime and barstate.isconfirmed',
            'sendPaper and secretReady', 'alert(body, alert.freq_once_per_bar_close)',
            'lastSentClose := time_close',
        ):
            self.assertIn(text, source)
        self.assertNotIn("alertcondition(", source)
        self.assertNotIn("MACD", source)
        self.assertNotIn("crossUp(ema200", source)
        self.assertNotIn("crossDown(ema200", source)
        self.assertIn(paper.SIGNAL_TYPE, source)
        self.assertNotIn("barmerge.lookahead_on", source)
        self.assertNotIn('request.security(syminfo.tickerid, "60"', source)
        self.assertNotIn("ema9_daily", source)
        self.assertNotIn("ta.ema(close, 9)", source)
        self.assertNotIn("ta.sma(close, 9)", source)
        self.assertIn('crossUp(tenkan9, tenkan9[1])', source)
        self.assertIn('crossDown(tenkan9, tenkan9[1])', source)

    def test_old_ema_levels_cannot_trigger_the_new_tenkan_strategy(self):
        body = payload()
        body["previous_levels"] = {"ema9_5m": 100}
        body["levels"] = {"ema9_5m": 100}
        self.assertIsNone(parse(body)["direction"])
        self.assertEqual(parse(body)["status"], "WAIT - INDICATOR DATA")


if __name__ == "__main__":
    unittest.main()
