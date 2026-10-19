import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as app
import orca_whale_options_bot_FAST_60S_5S as bot


class TradingViewOrcaEntrySignalTests(unittest.TestCase):
    def setUp(self):
        with app._TV_ENTRY_EVENT_CACHE_LOCK:
            app._TV_ENTRY_EVENT_CACHE.clear()
        while True:
            try:
                app.TV_ENTRY_QUEUE.get_nowait()
                app.TV_ENTRY_QUEUE.task_done()
            except app.queue.Empty:
                break

    def tearDown(self):
        self.setUp()

    def payload(self, direction="CALL", bar_close_time_ms=None):
        now = bot.now_new_york()
        return {
            "secret": "test-only-secret",
            "source": "GOLD_ORCA_PINE",
            "ticker": "NASDAQ:AAPL",
            "direction": direction,
            "timeframe": "1",
            "signal_price": 215.25,
            "bar_close_time_ms": (
                bar_close_time_ms
                if bar_close_time_ms is not None
                else int(now.timestamp() * 1000)
            ),
        }

    def test_parser_accepts_only_fresh_watchlist_direction_signals(self):
        now = bot.now_new_york()
        with (
            patch.object(bot, "WATCHLIST", ["AAPL"]),
            patch.object(bot, "EXCLUDED_SYMBOLS", {"MSTR"}),
        ):
            event = app._parse_tv_entry_signal(self.payload(), now=now)
            self.assertEqual(event["symbol"], "AAPL")
            self.assertEqual(event["direction"], "CALL")
            self.assertEqual(event["timeframe"], "1")

            with self.assertRaisesRegex(ValueError, "direction"):
                app._parse_tv_entry_signal(self.payload("HOLD"), now=now)
            with self.assertRaisesRegex(ValueError, "watchlist"):
                outside = self.payload()
                outside["ticker"] = "NASDAQ:TSLA"
                app._parse_tv_entry_signal(outside, now=now)
            with self.assertRaisesRegex(ValueError, "stale"):
                stale = self.payload(
                    bar_close_time_ms=int((now.timestamp() - 301) * 1000)
                )
                app._parse_tv_entry_signal(stale, now=now)

    def test_authenticated_endpoint_enqueues_once_and_does_not_scan_inline(self):
        state = {"trades": {}}
        with (
            patch.object(bot, "env", return_value="test-only-secret"),
            patch.object(bot, "WATCHLIST", ["AAPL"]),
            patch.object(bot, "EXCLUDED_SYMBOLS", {"MSTR"}),
            patch.object(bot, "market_is_open_now", return_value=True),
            patch.object(bot, "_load_trade_store", side_effect=lambda: state),
            patch.object(bot, "_save_trade_store"),
            patch.object(app.CanonicalHandler, "log_message"),
            patch.object(app, "scan_symbol_options") as scan,
        ):
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.CanonicalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def send(payload):
                request = Request(
                    base + "/tv-entry-signal",
                    json.dumps(payload).encode("utf-8"),
                    {"Content-Type": "application/json"},
                )
                try:
                    with urlopen(request, timeout=3) as response:
                        return response.status, json.load(response)
                except HTTPError as error:
                    return error.code, json.load(error)

            try:
                bad_status, _ = send({**self.payload(), "secret": "wrong"})
                self.assertEqual(bad_status, 401)

                put_signal = self.payload("PUT")
                status, first = send(put_signal)
                self.assertEqual(status, 202)
                self.assertTrue(first["queued"])
                self.assertEqual(first["direction"], "PUT")

                duplicate_status, duplicate = send(put_signal)
                self.assertEqual(duplicate_status, 202)
                self.assertTrue(duplicate["duplicate"])

                with app._TV_ENTRY_EVENT_CACHE_LOCK:
                    app._TV_ENTRY_EVENT_CACHE.clear()
                persisted_duplicate_status, persisted_duplicate = send(put_signal)
                self.assertEqual(persisted_duplicate_status, 202)
                self.assertTrue(persisted_duplicate["duplicate"])

                event = app.TV_ENTRY_QUEUE.get_nowait()
                app.TV_ENTRY_QUEUE.task_done()
                self.assertEqual(event["symbol"], "AAPL")
                self.assertEqual(event["direction"], "PUT")
                scan.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_authenticated_setup_signal_is_queued_as_preparation(self):
        state = {"trades": {}}
        with (
            patch.object(bot, "env", return_value="test-only-secret"),
            patch.object(bot, "WATCHLIST", ["AAPL"]),
            patch.object(bot, "EXCLUDED_SYMBOLS", {"MSTR"}),
            patch.object(bot, "market_is_open_now", return_value=False),
            patch.object(bot, "_load_trade_store", side_effect=lambda: state),
            patch.object(bot, "_save_trade_store"),
            patch.object(app.CanonicalHandler, "log_message"),
        ):
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.CanonicalHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            payload = {**self.payload("CALL"), "signal_type": "PREPARE_CALL"}
            request = Request(
                base + "/tv-entry-signal",
                json.dumps(payload).encode("utf-8"),
                {"Content-Type": "application/json"},
            )
            try:
                with urlopen(request, timeout=3) as response:
                    status, result = response.status, json.load(response)
                self.assertEqual(status, 202)
                self.assertEqual(result["event_kind"], "PREPARATION")
                self.assertEqual(result["signal_type"], "PREPARE_CALL")
                self.assertTrue(result["queued"])
                event = app.TV_ENTRY_QUEUE.get_nowait()
                app.TV_ENTRY_QUEUE.task_done()
                self.assertEqual(event["event_kind"], "PREPARATION")
                self.assertEqual(event["direction"], "CALL")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_setup_event_sends_notification_without_scanning_contracts(self):
        now = bot.now_new_york()
        event = {
            "event_id": "AAPL|1|PREPARE_PUT|1",
            "symbol": "AAPL",
            "direction": "PUT",
            "signal_type": "PREPARE_PUT",
            "event_kind": "PREPARATION",
            "timeframe": "1",
            "bar_close_time_ms": int(now.timestamp() * 1000),
            "signal_price": 215.25,
        }
        with (
            patch.object(bot, "now_new_york", return_value=now),
            patch.object(app, "telegram_send_tv_signal_card", return_value=55) as card,
            patch.object(app, "scan_symbol_options") as scan,
            patch.object(app, "_set_tv_entry_event_status") as set_status,
        ):
            result = app.process_tv_entry_signal(event)

        self.assertEqual(result["status"], "PREPARATION_SENT")
        self.assertFalse(result["trade_created"])
        self.assertEqual(result["message_id"], 55)
        card.assert_called_once_with(event)
        scan.assert_not_called()
        set_status.assert_called_once()

    def test_confirmed_direction_gets_info_card_when_no_contract_qualifies(self):
        now = bot.now_new_york()
        event = {
            "event_id": "AAPL|1|CALL|1",
            "symbol": "AAPL",
            "direction": "CALL",
            "signal_type": "CALL",
            "event_kind": "ENTRY",
            "timeframe": "1",
            "bar_close_time_ms": int(now.timestamp() * 1000),
            "signal_price": 215.25,
        }
        with (
            patch.object(bot, "now_new_york", return_value=now),
            patch.object(bot, "market_is_open_now", return_value=True),
            patch.object(app, "LIVE_ENTRY_ALERTS_ENABLED", True),
            patch.object(app, "scan_symbol_options", return_value={
                "status": "WAIT", "reason": "no fresh option contract",
            }) as scan,
            patch.object(app, "telegram_send_tv_signal_card", return_value=66) as card,
            patch.object(app, "_set_tv_entry_event_status"),
        ):
            result = app.process_tv_entry_signal(event)

        scan.assert_called_once_with("AAPL", requested_direction="CALL")
        card.assert_called_once()
        self.assertEqual(result["signal_notification_message_id"], 66)

    def test_confirmed_direction_gets_info_card_when_contract_scan_fails(self):
        now = bot.now_new_york()
        event = {
            "event_id": "AAPL|1|PUT|2",
            "symbol": "AAPL",
            "direction": "PUT",
            "signal_type": "PUT",
            "event_kind": "ENTRY",
            "timeframe": "1",
            "bar_close_time_ms": int(now.timestamp() * 1000),
            "signal_price": 215.25,
        }
        with (
            patch.object(bot, "now_new_york", return_value=now),
            patch.object(bot, "market_is_open_now", return_value=True),
            patch.object(app, "LIVE_ENTRY_ALERTS_ENABLED", True),
            patch.object(
                app, "scan_symbol_options", side_effect=RuntimeError("provider unavailable")
            ),
            patch.object(app, "telegram_send_tv_signal_card", return_value=77) as card,
            patch.object(app, "_set_tv_entry_event_status"),
        ):
            result = app.process_tv_entry_signal(event)

        card.assert_called_once()
        self.assertEqual(result["status"], "CONTRACT_SCAN_ERROR")
        self.assertEqual(result["signal_notification_message_id"], 77)

    def test_worker_passes_direction_to_existing_contract_selector(self):
        bar_close_time_ms = int(bot.now_new_york().timestamp() * 1000)
        event = {
            "event_id": f"AAPL|1|{bar_close_time_ms}",
            "symbol": "AAPL",
            "direction": "PUT",
            "timeframe": "1",
            "bar_close_time_ms": bar_close_time_ms,
        }
        with app._TV_ENTRY_EVENT_CACHE_LOCK:
            app._TV_ENTRY_EVENT_CACHE[event["event_id"]] = {
                "status": "queued",
                "queued_monotonic": 0,
                "symbol": "AAPL",
                "direction": "PUT",
            }
        store = {
            "trades": {},
            "tv_entry_events": {
                event["event_id"]: {
                    **event,
                    "status": "queued",
                    "received_at": bot.now_new_york().isoformat(),
                }
            },
        }
        with (
            patch.object(bot, "AUTO_SCAN_ENABLED", False),
            patch.object(app, "scan_symbol_options", return_value={
                "status": "TRADE", "contract": "AAPL-PUT",
            }) as scan,
            patch.object(bot, "market_is_open_now", return_value=True),
            patch.object(bot, "_load_trade_store", side_effect=lambda: store),
            patch.object(bot, "_save_trade_store"),
        ):
            result = app.process_tv_entry_signal(event)

        scan.assert_called_once_with("AAPL", requested_direction="PUT")
        self.assertEqual(result["contract"], "AAPL-PUT")
        self.assertEqual(
            app._TV_ENTRY_EVENT_CACHE[event["event_id"]]["status"], "TRADE"
        )

    def test_disabled_auto_scanner_does_not_start_a_background_thread(self):
        with (
            patch.object(bot, "AUTO_SCAN_ENABLED", False),
            patch.object(app, "_start_thread_once") as start_thread,
        ):
            result = app.start_auto_scanner()

        self.assertIsNone(result)
        start_thread.assert_not_called()

    def test_worker_drops_signal_that_expired_in_queue(self):
        now = bot.now_new_york()
        bar_close_time_ms = int((now.timestamp() - 301) * 1000)
        event = {
            "event_id": f"AAPL|1|{bar_close_time_ms}",
            "symbol": "AAPL",
            "direction": "CALL",
            "timeframe": "1",
            "bar_close_time_ms": bar_close_time_ms,
        }
        store = {
            "trades": {},
            "tv_entry_events": {
                event["event_id"]: {**event, "status": "queued"},
            },
        }
        with (
            patch.object(bot, "now_new_york", return_value=now),
            patch.object(bot, "_load_trade_store", side_effect=lambda: store),
            patch.object(bot, "_save_trade_store"),
            patch.object(app, "scan_symbol_options") as scan,
        ):
            result = app.process_tv_entry_signal(event)

        self.assertEqual(result["status"], "EXPIRED")
        self.assertEqual(
            store["tv_entry_events"][event["event_id"]]["status"], "EXPIRED"
        )
        scan.assert_not_called()

    def test_restart_recovery_requeues_fresh_events_and_expires_stale_ones(self):
        now = bot.now_new_york()
        fresh_time = int((now.timestamp() - 30) * 1000)
        stale_time = int((now.timestamp() - 301) * 1000)
        fresh_id = f"AAPL|1|{fresh_time}"
        stale_id = f"TSLA|1|{stale_time}"
        state = {
            "trades": {},
            "tv_entry_events": {
                fresh_id: {
                    "event_id": fresh_id,
                    "symbol": "AAPL",
                    "direction": "CALL",
                    "timeframe": "1",
                    "bar_close_time_ms": fresh_time,
                    "status": "processing",
                    "received_at": now.isoformat(),
                },
                stale_id: {
                    "event_id": stale_id,
                    "symbol": "TSLA",
                    "direction": "PUT",
                    "timeframe": "1",
                    "bar_close_time_ms": stale_time,
                    "status": "queued",
                    "received_at": now.isoformat(),
                },
            },
        }

        with (
            patch.object(bot, "now_new_york", return_value=now),
            patch.object(bot, "_load_trade_store", side_effect=lambda: state),
            patch.object(bot, "_save_trade_store"),
        ):
            recovered_count = app.recover_tv_entry_queue()

        self.assertEqual(recovered_count, 1)
        recovered = app.TV_ENTRY_QUEUE.get_nowait()
        app.TV_ENTRY_QUEUE.task_done()
        self.assertEqual(recovered["event_id"], fresh_id)
        self.assertEqual(state["tv_entry_events"][fresh_id]["status"], "queued")
        self.assertEqual(state["tv_entry_events"][stale_id]["status"], "expired")

    def test_external_direction_is_recorded_as_its_own_confidence_evidence(self):
        confidence = app.calculate_trade_confidence(
            {}, -100, "disabled", "Fresh Bid/Ask Midpoint",
            external_direction_confirmed=True,
        )

        self.assertEqual(
            confidence["components"]["external_direction_signal"], 16.67
        )
        self.assertNotIn("direction_edge", confidence["components"])

    def test_attached_indicator_has_four_authenticated_dynamic_alert_types(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "GOLD_ORCA_TODAY_OPTION_TABLE.pine"
        ).read_text(encoding="utf-8")
        self.assertIn('"سر الربط — REEF_WEBHOOK_SECRET"', source)
        for signal_type, direction in (
            ("CALL", "CALL"),
            ("PUT", "PUT"),
            ("PREPARE_CALL", "CALL"),
            ("PREPARE_PUT", "PUT"),
        ):
            self.assertIn(
                f'alert(f_tv_signal_payload("{signal_type}", "{direction}")',
                source,
            )
        self.assertIn("bar_close_time_ms", source)
        self.assertIn("alert.freq_once_per_bar_close", source)


if __name__ == "__main__":
    unittest.main()
