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
        "symbol": "AAPL", "direction": direction, "signal_type": "MACD_5M",
        "bar_time_ms": (int(NOW.timestamp()) // 300 * 300 - 300) * 1000,
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
        bars = [{"t": start - i * 300000, "c": 200} for i in range(3600)]
        bars.append({"t": start + 300000, "c": 10000})
        result = macd.from_bars("AAPL", bars, NOW)
        self.assertIsNone(result["direction"])
        self.assertEqual(result["bar_time_ms"], start)
        with self.assertRaises(macd.MacdDataError):
            macd.from_bars("AAPL", [b for b in bars if b["t"] != start], NOW)

    def test_direction_validation_rejects_wrong_side_and_old_candle(self):
        macd.validate_direction(signal(), "AAPL", "PUT", NOW)
        for bad in (
            {**signal(), "direction": "CALL"},
            {**signal(), "bar_time_ms": signal()["bar_time_ms"] - 300000},
            {**signal(), "histogram": 0},
            {**signal(), "symbol": "NVDA"},
        ):
            with self.assertRaises(macd.MacdDataError):
                macd.validate_direction(bad, "AAPL", "PUT", NOW)


class MacdEntryTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
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

    def scan(self, direction, bad=False, gamma_failure=False):
        put_raw = raw("PUT", implied_volatility=0.8) if bad else raw("PUT")
        gamma = {"status": "FOUND", "flip": 180, "regime": "POSITIVE"}
        with (
            patch.object(app, "_fetch_macd_5m_direction", return_value=signal(direction)),
            patch.object(bot, "_load_trade_store", return_value={"trades": {}}),
            patch.object(app, "massive_chain", side_effect=lambda _symbol, side:
                         [raw("CALL")] if side == "CALL" else [put_raw]),
            patch.object(app, "fetch_underlying_price", return_value=(200, "test")),
            patch.object(bot, "calculate_gamma_flip",
                         side_effect=RuntimeError("diagnostic unavailable") if gamma_failure else None,
                         return_value=gamma),
            patch.object(bot, "_direction_strength", side_effect=[100, 0]),
            patch.object(app, "_conservative_assessment", side_effect=AssertionError("Old direction gate")),
            patch.object(app, "_clear_pending_candidate"),
            patch.object(app, "create_trade", return_value={
                "contract_ticker": raw(direction)["details"]["ticker"],
                "message_id": 42, "trade_confidence": "HIGH",
            }) as send,
        ):
            return app.scan_symbol_options("AAPL"), send

    def test_put_wins_even_when_gamma_and_flow_support_call(self):
        result, create = self.scan("PUT")
        self.assertEqual(result["status"], "TRADE")
        self.assertEqual(result["direction"], "PUT")
        self.assertFalse(result["call"]["accepted"])
        self.assertEqual(result["put"]["gamma_direction_state"], "opposed")
        self.assertEqual(create.call_args.args[1], "PUT")

    def test_green_selects_call_and_gamma_failure_is_informational(self):
        result, _ = self.scan("CALL")
        self.assertEqual(result["direction"], "CALL")
        result, _ = self.scan("PUT", gamma_failure=True)
        self.assertEqual(result["direction"], "PUT")

    def test_bad_put_never_falls_back_to_good_call(self):
        result, create = self.scan("PUT", bad=True)
        self.assertNotEqual(result["status"], "TRADE")
        self.assertFalse(result["call"]["accepted"])
        create.assert_not_called()

    def test_missing_or_zero_macd_never_queries_options_or_sends(self):
        for data in (macd.MacdDataError("delayed"), {**signal(), "direction": None, "histogram": 0}):
            with (
                patch.object(app, "_fetch_macd_5m_direction",
                             side_effect=data if isinstance(data, Exception) else None,
                             return_value=data),
                patch.object(app, "massive_chain") as chain,
                patch.object(app, "create_trade") as create,
            ):
                result = app.scan_symbol_options("AAPL")
                self.assertIn(result["status"], {"WAIT - MACD DATA", "WAIT - MACD ZERO"})
                chain.assert_not_called()
                create.assert_not_called()

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

    def test_actual_send_metadata_and_new_candle_revalidation(self):
        candidates, _ = app._prepare_side([raw("PUT")], 200)
        contract = candidates[0]
        for turns in ([signal(), signal()], [signal(), signal("CALL")]):
            store = {"trades": {}}
            with (
                patch.object(app, "_fetch_macd_5m_direction", side_effect=turns),
                patch.object(bot, "_load_trade_store", side_effect=lambda: store),
                patch.object(bot, "_save_trade_store"),
                patch.object(app, "refresh_contract_quote_for_entry", return_value=copy.deepcopy(contract)),
                patch.object(app, "_paper_trade_record", return_value={"id": "test"}),
                patch.object(bot, "telegram_send_card", return_value=42) as send,
            ):
                if turns[-1]["direction"] == "PUT":
                    result = app.create_trade("AAPL", "PUT", 200, contract=contract,
                                              gamma_info={"status": "FOUND", "flip": 180})
                    self.assertEqual(result["entry_signal_type"], "MACD_5M")
                    self.assertEqual(result["macd"]["histogram"], -0.1)
                    self.assertEqual(result["gamma_policy"], "diagnostic_only")
                    send.assert_called_once()
                else:
                    with self.assertRaises(macd.MacdDataError):
                        app.create_trade("AAPL", "PUT", 200, contract=contract)
                    send.assert_not_called()

    def test_direct_entry_cannot_bypass_macd(self):
        with (
            patch.object(app, "_fetch_macd_5m_direction", return_value=signal("CALL")),
            patch.object(bot, "_load_trade_store") as load,
        ):
            with self.assertRaises(macd.MacdDataError):
                app.create_trade("AAPL", "PUT", 200)
            load.assert_not_called()


def webhook_payload():
    return {
        "secret": "test-only-secret", "ticker": "NASDAQ:AAPL",
        "signal_type": "MACD_5M", "timeframe": "5",
        "fast": 120, "slow": 180, "signal_length": 40,
        "signal_average": "SMA", "bar_closed": True,
        "bar_time_ms": signal()["bar_time_ms"],
        "bar_close_time_ms": signal()["bar_time_ms"] + 300000,
        "macd": -0.2, "signal": -0.1, "histogram": -0.1, "price": 200,
    }


class TradingViewMacdTests(unittest.TestCase):
    def test_payload_requires_exact_settings_freshness_and_matching_values(self):
        good = macd.from_webhook(webhook_payload(), NOW, ["AAPL"], set())
        self.assertEqual(good["direction"], "PUT")
        self.assertNotIn("secret", good)
        for changes in (
            {"timeframe": "30"}, {"fast": 12}, {"signal_average": "EMA"},
            {"bar_closed": False}, {"histogram": 0.5},
            {"price": float("nan")}, {"ticker": "MSTR"},
            {"bar_time_ms": signal()["bar_time_ms"] - 300000},
        ):
            with self.assertRaises(macd.MacdDataError):
                macd.from_webhook({**webhook_payload(), **changes}, NOW, ["AAPL"], {"MSTR"})

    def test_cache_expires_on_next_candle_without_any_delayed_fallback(self):
        fresh = macd.from_webhook(webhook_payload(), NOW, ["AAPL"], set())
        with (
            patch.dict(app._MACD_CACHE, {"AAPL": fresh}, clear=True),
            patch.object(app, "_massive_stock_get", side_effect=AssertionError("No delayed fallback")),
        ):
            self.assertEqual(app._fetch_macd_5m_direction("AAPL", NOW)["direction"], "PUT")
            with self.assertRaises(macd.MacdDataError):
                app._fetch_macd_5m_direction(
                    "AAPL", NOW.replace(minute=15),
                )

    def test_http_auth_cache_duplicate_and_conflict_without_sending_alerts(self):
        with (
            patch.object(bot, "env", return_value="test-only-secret"),
            patch.object(bot, "now_new_york", return_value=NOW),
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
                self.assertEqual(app._fetch_macd_5m_direction("AAPL", NOW)["direction"], "PUT")
                scanner.assert_not_called()
                telegram.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
