import copy
import math
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as app
import macd_direction as macd

bot = app.bot
NOW = datetime(2026, 10, 12, 11, 12, tzinfo=bot.NEW_YORK_TZ)


def signal(direction="PUT"):
    return {
        "symbol": "AAPL", "direction": direction, "signal_type": "MACD_1H_EMA200",
        "timeframe": "60", "ema_length": 200, "ema_average": "EMA",
        "price": 200, "ema200": 210 if direction == "PUT" else 190,
        "bar_time_ms": int(NOW.replace(hour=9, minute=30).timestamp()) * 1000,
        "bar_close_time_ms": int(NOW.replace(hour=10, minute=30).timestamp()) * 1000,
        "histogram": -0.1 if direction == "PUT" else 0.1,
    }


def raw(direction, **changes):
    record = {
        "details": {"ticker": f"AAPL261023{'C' if direction == 'CALL' else 'P'}00200000",
                    "expiration_date": "2026-10-23", "strike_price": 200},
        "day": {"volume": 5000}, "open_interest": 5000,
        "greeks": {"delta": 0.5 if direction == "CALL" else -0.5, "theta": -0.05},
        "implied_volatility": 0.3,
        "last_quote": {"bid": 4.45, "ask": 4.55, "last_updated": NOW.timestamp() - 1},
    }
    record.update(changes)
    return record


class MacdMathTests(unittest.TestCase):
    def test_ema_120_180_and_sma_40_match_independent_reference(self):
        closes = [200 + 0.00001 * i * i for i in range(1300)]
        f = s = closes[0]
        differences = []
        for price in closes:
            f = price * (2 / 121) + f * (119 / 121)
            s = price * (2 / 181) + s * (179 / 181)
            differences.append(f - s)
        expected_signal = math.fsum(differences[-40:]) / 40
        line, average, hist = macd.histogram(closes)
        self.assertAlmostEqual(line, differences[-1], places=10)
        self.assertAlmostEqual(average, expected_signal, places=10)
        self.assertAlmostEqual(hist, differences[-1] - expected_signal, places=10)
        self.assertGreater(hist, 0)
        self.assertLess(macd.histogram([400 - p for p in closes])[2], 0)
        # A falling price can still have a positive histogram if descent slows.
        self.assertGreater(macd.histogram(list(reversed(closes)))[2], 0)

    def test_flat_histogram_is_zero_and_short_history_is_rejected(self):
        self.assertEqual(macd.histogram([200] * 1080)[2], 0)
        with self.assertRaises(macd.MacdDataError):
            macd.histogram([200] * 1079)
        with self.assertRaises(macd.MacdDataError):
            macd.histogram([200] * 1079 + [float("nan")])

    def test_only_latest_completed_candle_is_used(self):
        start = signal()["bar_time_ms"]
        # Historical rows are enough for warm-up; latest is exact, future excluded.
        bars = [{"t": start - i * 3600000, "c": 200} for i in range(7000)]
        bars.append({"t": start + 3600000, "c": 10000})
        result = macd.from_bars("AAPL", bars, NOW)
        self.assertIsNone(result["direction"])
        self.assertEqual(result["bar_time_ms"], start)
        with self.assertRaises(macd.MacdDataError):
            macd.from_bars("AAPL", [b for b in bars if b["t"] != start], NOW)

    def test_direction_validation_rejects_wrong_side_and_old_candle(self):
        macd.validate_direction(signal(), "AAPL", "PUT", NOW)
        for bad in (
            {**signal(), "direction": "CALL"},
            {**signal(), "bar_time_ms": signal()["bar_time_ms"] - 3600000},
            {**signal(), "histogram": 0},
            {**signal(), "symbol": "NVDA"},
        ):
            with self.assertRaises(macd.MacdDataError):
                macd.validate_direction(bad, "AAPL", "PUT", NOW)


class AutonomousEntryTests(unittest.TestCase):
    """Compatibility coverage explicitly opts into the former Gamma gate."""
    def setUp(self):
        self.patches = [
            patch.object(app, "GAMMA_DIRECTION_GATE_ENABLED", True),
            patch.object(bot, "now_new_york", return_value=NOW),
            patch.object(app.time, "time", return_value=NOW.timestamp()),
            patch.object(app, "OPTION_ENTRY_MIN_PREMIUM", 2.5),
            patch.object(app, "OPTION_ENTRY_MAX_PREMIUM", 6.0),
            patch.object(app, "SCORE_MIN_DTE", 7),
            patch.object(app, "SCORE_MAX_DTE", 30),
            patch.object(app, "MAX_OPTION_IMPLIED_VOLATILITY", 0.5),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def scan(self, direction, bad=False, gamma_failure=False,
             strengths=(100, 0), gamma_flip=180):
        put_raw = raw("PUT", implied_volatility=0.8) if bad else raw("PUT")
        gamma = {"status": "FOUND", "flip": gamma_flip, "regime": "POSITIVE"}
        with (
            patch.object(app, "_fetch_macd_1h_direction", side_effect=AssertionError("MACD must not gate the scan")),
            patch.object(bot, "_load_trade_store", return_value={"trades": {}}),
            patch.object(app, "massive_chain", side_effect=lambda _symbol, side:
                         [raw("CALL")] if side == "CALL" else [put_raw]),
            patch.object(app, "fetch_underlying_price", return_value=(200, "test")),
            patch.object(bot, "calculate_gamma_flip",
                         side_effect=RuntimeError("diagnostic unavailable") if gamma_failure else None,
                         return_value=gamma),
            patch.object(bot, "_direction_strength", side_effect=list(strengths)),
            patch.object(app, "_clear_pending_candidate"),
            patch.object(app, "create_trade", return_value={
                "contract_ticker": raw(direction)["details"]["ticker"],
                "message_id": 42, "trade_confidence": "HIGH",
            }) as send,
        ):
            return app.scan_symbol_options("AAPL"), send

    def test_flow_and_gamma_select_call_without_consulting_macd(self):
        result, create = self.scan("PUT")
        self.assertEqual(result["status"], "TRADE")
        self.assertEqual(result["direction"], "CALL")
        self.assertFalse(result["put"]["accepted"])
        self.assertEqual(result["put"]["gamma_direction_state"], "opposed")
        self.assertEqual(create.call_args.args[1], "CALL")
        self.assertEqual(result["entry_signal_mode"], "ORCA_SCORE_GAMMA")
        self.assertNotIn("macd", result)

    def test_old_gamma_policy_is_restored(self):
        result, _ = self.scan("CALL")
        self.assertEqual(result["direction"], "CALL")
        with self.assertRaisesRegex(RuntimeError, "diagnostic unavailable"):
            self.scan("PUT", gamma_failure=True)

    def test_put_can_qualify_without_a_tradingview_signal(self):
        result, create = self.scan("CALL", strengths=(0, 100), gamma_flip=220)
        self.assertEqual(result["status"], "TRADE")
        self.assertEqual(result["direction"], "PUT")
        self.assertFalse(result["call"]["accepted"])
        self.assertEqual(create.call_args.args[1], "PUT")

    def test_ambiguous_direction_still_blocks_both_sides(self):
        result, create = self.scan("PUT", strengths=(100, 100))
        self.assertFalse(result["call"]["accepted"])
        self.assertFalse(result["put"]["accepted"])
        self.assertEqual(result["status"], "WAIT - DIRECTION UNCLEAR")
        create.assert_not_called()

    def test_bad_put_does_not_block_qualified_call(self):
        result, create = self.scan("PUT", bad=True)
        self.assertEqual(result["status"], "TRADE")
        self.assertTrue(result["call"]["accepted"])
        self.assertFalse(result["put"]["accepted"])
        create.assert_called_once()

    def test_missing_stale_or_neutral_macd_does_not_block_options_scan(self):
        for cached in ({}, {"AAPL": signal()}, {"AAPL": {**signal(), "direction": None, "histogram": 0}}):
            with self.subTest(cached=cached), patch.dict(app._MACD_CACHE, cached, clear=True):
                result, create = self.scan("PUT")
                self.assertEqual(result["status"], "TRADE")
                create.assert_called_once()

    def test_existing_quality_filters_are_preserved(self):
        for changes in (
            {"implied_volatility": 0.51},
            {"day": {"volume": 49}},
            {"open_interest": 99},
            {"greeks": {"delta": -0.5, "theta": -0.26}},
            {"last_quote": {"bid": 6.2, "ask": 6.4, "last_updated": NOW.timestamp()}},
            {"details": {"ticker": "AAPL261113P00200000", "expiration_date": "2026-11-13", "strike_price": 200}},
        ):
            candidates, _ = app._prepare_side([raw("PUT", **changes)], 200)
            self.assertFalse(candidates, changes)

    def test_actual_entry_needs_contract_quality_not_macd(self):
        candidates, _ = app._prepare_side([raw("PUT")], 200)
        contract = candidates[0]
        store = {"trades": {}}
        with (
            patch.object(app, "_fetch_macd_1h_direction", side_effect=AssertionError("MACD must not gate entry")),
            patch.object(bot, "_load_trade_store", side_effect=lambda: store),
            patch.object(bot, "_save_trade_store"),
            patch.object(app, "refresh_contract_quote_for_entry", return_value=copy.deepcopy(contract)),
            patch.object(app, "_paper_trade_record", return_value={"id": "test"}),
            patch.object(bot, "telegram_send_card", return_value=42) as send,
        ):
            result = app.create_trade("AAPL", "PUT", 200, contract=contract,
                                      gamma_info={"status": "FOUND", "flip": 220},
                                      directional_edge=100)
            self.assertEqual(result["entry_signal_type"], "ORCA_SCORE_GAMMA")
            self.assertNotIn("macd", result)
            self.assertIsNone(result["entry_signal_bar_time_ms"])
            send.assert_called_once()

    def test_direct_entry_cannot_bypass_quality_or_fresh_quote(self):
        candidates, _ = app._prepare_side([raw("PUT")], 200)
        with (
            patch.object(app, "_fetch_macd_1h_direction", side_effect=AssertionError("MACD must not gate entry")),
            patch.object(bot, "_load_trade_store", return_value={"trades": {}}),
            patch.object(bot, "_save_trade_store"),
            patch.object(app, "refresh_contract_quote_for_entry",
                         side_effect=RuntimeError("WAIT - FRESH OPTION QUOTE REQUIRED")),
            patch.object(bot, "telegram_send_card") as send,
        ):
            with self.assertRaisesRegex(RuntimeError, "FRESH OPTION QUOTE"):
                app.create_trade("AAPL", "PUT", 200, contract=candidates[0],
                                 directional_edge=100)
            send.assert_not_called()


def webhook_payload():
    return {
        "secret": "test-only-secret", "ticker": "NASDAQ:AAPL",
        "signal_type": "MACD_1H_EMA200", "timeframe": "60",
        "fast": 120, "slow": 180, "signal_length": 40,
        "signal_average": "SMA", "bar_closed": True,
        "bar_time_ms": signal()["bar_time_ms"],
        "bar_close_time_ms": signal()["bar_close_time_ms"],
        "ema_length": 200, "ema_average": "EMA", "ema200": 210,
        "macd": -0.2, "signal": -0.1, "histogram": -0.1, "price": 200,
    }


class GammaDisabledEntryTests(unittest.TestCase):
    def setUp(self):
        AutonomousEntryTests.setUp(self)
        gate = patch.object(app, "GAMMA_DIRECTION_GATE_ENABLED", False)
        gate.start()
        self.addCleanup(gate.stop)

    def test_call_can_qualify_below_old_flip(self):
        result, create = AutonomousEntryTests.scan(
            self, "CALL", strengths=(100, 0), gamma_flip=220,
        )
        self.assertEqual(result["direction"], "CALL")
        self.assertEqual(result["gamma_policy"], "disabled")
        self.assertEqual(result["entry_signal_mode"], "ORCA_SCORE_FLOW")
        self.assertEqual(result["call"]["gamma_direction_state"], "disabled")
        self.assertIsNone(result["call"]["checks"]["gamma_flip"])
        create.assert_called_once()

    def test_put_can_qualify_above_old_flip(self):
        result, create = AutonomousEntryTests.scan(
            self, "PUT", strengths=(0, 100), gamma_flip=180,
        )
        self.assertEqual(result["direction"], "PUT")
        self.assertEqual(create.call_args.args[1], "PUT")
        self.assertEqual(result["put"]["gamma_direction_state"], "disabled")

    def test_gamma_calculation_is_not_required(self):
        result, create = AutonomousEntryTests.scan(
            self, "PUT", strengths=(0, 100), gamma_failure=True,
        )
        self.assertEqual(result["status"], "TRADE")
        self.assertIsNone(result["gamma_flip"])
        create.assert_called_once()

    def test_unclear_flow_and_bad_contracts_still_cannot_enter(self):
        result, create = AutonomousEntryTests.scan(
            self, "PUT", strengths=(100, 100),
        )
        self.assertEqual(result["status"], "WAIT - DIRECTION UNCLEAR")
        create.assert_not_called()
        result, create = AutonomousEntryTests.scan(
            self, "PUT", strengths=(0, 100), bad=True,
        )
        self.assertNotEqual(result["status"], "TRADE")
        create.assert_not_called()

    def test_gamma_has_no_confidence_vote_or_missing_data_penalty(self):
        contracts, _ = app._prepare_side([raw("PUT")], 200)
        contract = contracts[0]
        scores = []
        for gamma_state in ("confirmed", "opposed", "unavailable", "disabled"):
            confidence = app.calculate_trade_confidence(
                contract, 100, gamma_state, "Fresh Bid/Ask Midpoint",
            )
            self.assertNotIn("gamma_state", confidence["components"])
            self.assertEqual(confidence["level"], "HIGH")
            scores.append(confidence["score"])
        self.assertEqual(len(set(scores)), 1)
        for gamma in ({}, {"status": "FOUND", "flip": 180},
                      {"status": "FOUND", "flip": 220}):
            assessment = app._conservative_assessment("PUT", contract, 100, gamma, 200)
            self.assertTrue(assessment["can_open"])
            self.assertEqual(assessment["gamma_direction_state"], "disabled")
        contract["quote_fresh"] = False
        assessment = app._conservative_assessment("PUT", contract, 100, {}, 200)
        self.assertFalse(assessment["can_open"])
        self.assertIn("WAIT - LIVE OPTION PRICE REQUIRED", assessment["wait_reasons"])

    def test_direct_entry_does_not_reintroduce_gamma_or_macd(self):
        contracts, _ = app._prepare_side([raw("PUT")], 200)
        contract = contracts[0]
        store = {"trades": {}}
        with (
            patch.object(app, "_fetch_macd_1h_direction",
                         side_effect=AssertionError("MACD was not authorized")),
            patch.object(bot, "_load_trade_store", side_effect=lambda: store),
            patch.object(bot, "_save_trade_store"),
            patch.object(app, "refresh_contract_quote_for_entry",
                         return_value=copy.deepcopy(contract)) as quote,
            patch.object(app, "_paper_trade_record", return_value={"id": "test"}),
            patch.object(bot, "telegram_send_card", return_value=42) as send,
        ):
            trade = app.create_trade(
                "AAPL", "PUT", 200, contract=contract,
                gamma_info={"status": "FOUND", "flip": 180},
                directional_edge=100,
            )
        self.assertEqual(trade["gamma_policy"], "disabled")
        self.assertEqual(trade["gamma_direction_state"], "disabled")
        self.assertEqual(trade["entry_signal_type"], "ORCA_SCORE_FLOW")
        quote.assert_called_once()
        send.assert_called_once()

    def test_card_does_not_present_disabled_gamma_as_confirmation(self):
        self.assertEqual(
            app._card_gamma_values({
                "gamma_policy": "disabled", "gamma_status": "FOUND",
                "gamma_flip": 180, "stock_price": 200, "gamma_regime": "POSITIVE",
            }),
            ("OFF", "DISABLED"),
        )


class TradingViewMacdTests(unittest.TestCase):
    def test_all_price_positions_and_histogram_signs_without_a_cross(self):
        for hist, price, expected in (
            (0.1, 220, "CALL"), (-0.1, 200, "PUT"),
            (0.1, 200, None), (-0.1, 220, None),
            (0.1, 210, None), (-0.1, 210, None),
            (0, 220, None), (0, 200, None),
        ):
            with self.subTest(hist=hist, price=price):
                payload = {**webhook_payload(), "macd": hist + 0.2,
                           "signal": 0.2, "histogram": hist, "price": price}
                result = macd.from_webhook(payload, NOW, ["AAPL"], set())
                self.assertEqual(result["direction"], expected)
                if expected:
                    macd.validate_direction(result, "AAPL", expected, NOW)

    def test_position_confirmation_is_rechecked_before_entry(self):
        for direction in ("CALL", "PUT"):
            good = signal(direction)
            for price in (good["ema200"], good["ema200"] - 10 if direction == "CALL" else good["ema200"] + 10):
                with self.assertRaises(macd.MacdDataError):
                    macd.validate_direction({**good, "price": price}, "AAPL", direction, NOW)

    def test_session_aligned_candles_and_short_session_end(self):
        for start, end, current in (
            ((9, 30), (10, 30), (11, 12)),
            ((10, 0), (11, 0), (11, 12)),
            ((9, 0), (9, 30), (9, 45)),
            ((15, 30), (16, 0), (16, 1)),
        ):
            with self.subTest(start=start, end=end):
                payload = {**webhook_payload(),
                           "bar_time_ms": int(NOW.replace(hour=start[0], minute=start[1]).timestamp()) * 1000,
                           "bar_close_time_ms": int(NOW.replace(hour=end[0], minute=end[1]).timestamp()) * 1000}
                result = macd.from_webhook(
                    payload, NOW.replace(hour=current[0], minute=current[1]), ["AAPL"], set())
                self.assertEqual(result["direction"], "PUT")
        for changes in (
            {"bar_close_time_ms": int(NOW.replace(hour=11, minute=30).timestamp()) * 1000},
            {"bar_close_time_ms": signal()["bar_close_time_ms"] - 1800000},
            {"bar_time_ms": signal()["bar_time_ms"] + 1000},
        ):
            with self.assertRaises(macd.MacdDataError):
                macd.from_webhook({**webhook_payload(), **changes}, NOW, ["AAPL"], set())

    def test_ema200_matches_independent_reference(self):
        closes = [200 + i * 0.01 for i in range(1300)]
        expected = closes[0]
        for price in closes:
            expected = price * (2 / 201) + expected * (199 / 201)
        self.assertAlmostEqual(macd.ema(closes), expected, places=10)

    def test_payload_requires_exact_settings_freshness_and_matching_values(self):
        good = macd.from_webhook(webhook_payload(), NOW, ["AAPL"], set())
        self.assertEqual(good["direction"], "PUT")
        self.assertNotIn("secret", good)
        for changes in (
            {"timeframe": "30"}, {"fast": 12}, {"signal_average": "EMA"},
            {"bar_closed": False}, {"histogram": 0.5},
            {"price": float("nan")}, {"ticker": "MSTR"},
            {"bar_time_ms": signal()["bar_time_ms"] - 3600000},
            {"signal_type": "MACD_5M", "timeframe": "5"},
            {"ema_length": 20}, {"ema_average": "SMA"},
            {"ema200": None}, {"ema200": float("nan")}, {"ema200": 0},
        ):
            with self.assertRaises(macd.MacdDataError):
                macd.from_webhook({**webhook_payload(), **changes}, NOW, ["AAPL"], {"MSTR"})

    def test_cache_expires_on_next_candle_without_any_delayed_fallback(self):
        fresh = macd.from_webhook(webhook_payload(), NOW, ["AAPL"], set())
        with (
            patch.dict(app._MACD_CACHE, {"AAPL": fresh}, clear=True),
            patch.object(app, "_massive_stock_get", side_effect=AssertionError("No delayed fallback")),
        ):
            self.assertEqual(app._fetch_macd_1h_direction("AAPL", NOW)["direction"], "PUT")
            with self.assertRaises(macd.MacdDataError):
                app._fetch_macd_1h_direction(
                    "AAPL", NOW.replace(minute=30),
                )

    def test_http_auth_cache_duplicate_and_conflict_without_sending_alerts(self):
        with (
            patch.object(bot, "env", return_value="test-only-secret"),
            patch.object(bot, "now_new_york", return_value=NOW) as clock,
            patch.object(bot, "WATCHLIST", ["AAPL"]),
            patch.object(bot, "EXCLUDED_SYMBOLS", set()),
            patch.dict(app._MACD_CACHE, {}, clear=True),
            patch.object(app, "scan_watchlist_once") as scanner,
            patch.object(bot, "telegram_send_card") as telegram,
            patch.object(app.CanonicalHandler, "log_message"),
        ):
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.CanonicalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def post(payload):
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}/macd-signal",
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"},
                    )
                    try:
                        response = urlopen(request, timeout=3)
                    except HTTPError as error:
                        response = error
                    with response:
                        return response.status, json.loads(response.read())
                self.assertEqual(post({**webhook_payload(), "secret": "wrong"})[0], 401)
                self.assertEqual(post({**webhook_payload(), "fast": 12})[0], 400)
                code, reply = post(webhook_payload())
                self.assertEqual(code, 200)
                self.assertEqual(reply["signal"]["direction"], "PUT")
                self.assertNotIn("secret", reply["signal"])
                self.assertEqual(post(webhook_payload())[0], 200)
                self.assertEqual(post({
                    **webhook_payload(), "macd": -0.3, "histogram": -0.2,
                })[0], 409)
                self.assertEqual(app._fetch_macd_1h_direction("AAPL", NOW)["direction"], "PUT")
                with urlopen(f"http://127.0.0.1:{server.server_port}/health") as health:
                    health_payload = json.load(health)
                    self.assertFalse(health_payload["macd_entry_gate_enabled"])
                    self.assertEqual(health_payload["configuration"]["entry_signal_mode"], "ORCA_SCORE_FLOW")
                    self.assertFalse(health_payload["gamma_direction_gate_enabled"])
                    self.assertFalse(health_payload["configuration"]["card_gamma_section_visible"])
                    self.assertEqual(health_payload["configuration"]["card_style"], "white_reference")
                    snapshot = health_payload["tradingview_signals"]["AAPL"]
                    self.assertTrue(snapshot["fresh"])
                    self.assertIn("received_at", snapshot)
                    self.assertEqual(snapshot["ema200"], 210)
                    self.assertNotIn("secret", snapshot)
                clock.return_value = NOW.replace(hour=12)
                next_payload = {
                    **webhook_payload(), "ema200": 190,
                    "bar_time_ms": signal()["bar_time_ms"] + 3600000,
                    "bar_close_time_ms": signal()["bar_close_time_ms"] + 3600000,
                }
                code, reply = post(next_payload)
                self.assertEqual(code, 200)
                self.assertIsNone(reply["signal"]["direction"])
                with (
                    patch.object(app, "_provider_is_marketdata", return_value=False),
                    patch.object(bot, "_load_trade_store", return_value={"trades": {}}),
                    patch.object(app, "massive_chain", side_effect=[[], []]) as chain,
                    patch.object(app, "fetch_underlying_price", return_value=(0, "test")),
                    patch.object(app, "create_trade") as create,
                ):
                    result = app.scan_symbol_options("AAPL")
                    self.assertEqual(result["ignored"], "underlying price unavailable")
                    self.assertEqual(chain.call_count, 2)
                    create.assert_not_called()
                scanner.assert_not_called()
                telegram.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
