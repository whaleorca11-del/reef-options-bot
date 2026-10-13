import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import main as app
from marketdata_quota import MarketDataQuota, next_daily_reset


def epoch(value):
    return datetime.fromisoformat(value).timestamp()


class Response:
    def __init__(self, status=200, headers=None, payload=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = "You've reached your API credit limit." if status == 429 else ""
        self.payload = payload or {"s": "ok"}

    def json(self):
        return self.payload


class QuotaWindowTests(unittest.TestCase):
    def test_documented_daily_reset_before_and_after_open(self):
        self.assertEqual(next_daily_reset(epoch("2026-10-13T13:29:59+00:00")),
                         epoch("2026-10-13T13:30:00+00:00"))
        self.assertEqual(next_daily_reset(epoch("2026-10-13T13:30:00+00:00")),
                         epoch("2026-10-14T13:30:00+00:00"))

    def test_reset_tracks_new_york_daylight_saving_changes(self):
        self.assertEqual(next_daily_reset(epoch("2026-10-31T13:31:00+00:00")),
                         epoch("2026-11-01T14:30:00+00:00"))
        self.assertEqual(next_daily_reset(epoch("2026-03-07T14:31:00+00:00")),
                         epoch("2026-03-08T13:30:00+00:00"))

    def test_provider_reset_header_is_authoritative_and_releases_at_boundary(self):
        clock = [epoch("2026-10-13T15:00:00+00:00")]
        quota = MarketDataQuota(clock=lambda: clock[0])
        reset = clock[0] + 60
        quota.block("credit limit", {"X-Api-Ratelimit-Reset": str(reset)})
        clock[0] = reset - 1
        self.assertEqual(quota.blocked_reason(), "credit limit")
        clock[0] = reset
        self.assertIsNone(quota.blocked_reason())
        self.assertEqual(quota.snapshot()["last_retry_released_at"], reset)

    def test_missing_invalid_and_expired_headers_use_daily_reset(self):
        now = epoch("2026-10-13T15:00:00+00:00")
        for header in ({}, {"x-api-ratelimit-reset": "NaN"},
                       {"x-api-ratelimit-reset": "invalid"},
                       {"x-api-ratelimit-reset": "1e99"},
                       {"x-api-ratelimit-reset": str(now - 1)}):
            with self.subTest(header=header):
                quota = MarketDataQuota(clock=lambda: now)
                quota.block("credit limit", header)
                self.assertEqual(quota.snapshot()["reset_at_epoch"], next_daily_reset(now))
                self.assertIsNotNone(quota.blocked_reason())

    def test_success_with_negative_remaining_pauses_next_request(self):
        quota = MarketDataQuota(clock=lambda: epoch("2026-10-13T15:00:00+00:00"))
        quota.observe({"X-API-RATELIMIT-LIMIT": "100000",
                       "X-API-RATELIMIT-REMAINING": "-664",
                       "X-API-RATELIMIT-CONSUMED": "1000"})
        snapshot = quota.snapshot()
        self.assertTrue(snapshot["blocked"])
        self.assertEqual(snapshot["remaining"], -664)
        self.assertEqual(snapshot["credits_consumed_since_start"], 1000)

    def test_old_remaining_is_not_reported_after_window_reset(self):
        clock = [epoch("2026-10-13T15:00:00+00:00")]
        quota = MarketDataQuota(clock=lambda: clock[0])
        quota.observe({"x-api-ratelimit-remaining": "500",
                       "x-api-ratelimit-reset": str(clock[0] + 60)})
        clock[0] += 60
        self.assertIsNone(quota.snapshot()["remaining"])

    def test_pause_survives_restart_without_persisting_error_or_credentials(self):
        clock = [epoch("2026-10-13T15:00:00+00:00")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quota.json"
            first = MarketDataQuota(clock=lambda: clock[0])
            first.configure_storage(path)
            first.block("credit limit with sensitive provider error",
                        {"x-api-ratelimit-reset": str(clock[0] + 60)})
            self.assertNotIn("sensitive", path.read_text())
            second = MarketDataQuota(clock=lambda: clock[0])
            second.configure_storage(path)
            self.assertIsNotNone(second.blocked_reason())
            clock[0] += 60
            self.assertIsNone(second.blocked_reason())
            self.assertFalse(json.loads(path.read_text())["blocked"])
            third = MarketDataQuota(clock=lambda: clock[0])
            third.configure_storage(path)
            self.assertIsNone(third.blocked_reason())

    def test_corrupt_pause_is_reported_and_not_overwritten(self):
        now = epoch("2026-10-13T15:00:00+00:00")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quota.json"
            path.write_text("invalid")
            quota = MarketDataQuota(clock=lambda: now)
            quota.configure_storage(path)
            self.assertIsNotNone(quota.snapshot()["storage_error"])
            self.assertIsNotNone(quota.blocked_reason())
            self.assertEqual(path.read_text(), "invalid")


class QuotaRequestTests(unittest.TestCase):
    def setUp(self):
        self.clock = [epoch("2026-10-13T15:00:00+00:00")]
        self.quota = MarketDataQuota(clock=lambda: self.clock[0])
        self.patcher = patch.object(app, "_MARKETDATA_QUOTA", self.quota)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_retry_without_restart_after_reset_and_relatch_if_still_exhausted(self):
        reset = self.clock[0] + 60
        response = Response(429, {"x-api-ratelimit-reset": str(reset)})
        with (
            patch.object(app.bot, "env", return_value="test"),
            patch.object(app.requests, "get", return_value=response) as get,
        ):
            with self.assertRaises(app.MarketDataAccountCreditLimitError):
                app._marketdata_get("/v1/options/chain/AAPL/")
            with self.assertRaises(app.MarketDataAccountCreditLimitError):
                app._marketdata_get("/v1/options/chain/AAPL/")
            self.assertEqual(get.call_count, 1)
            self.clock[0] = reset
            with self.assertRaises(app.MarketDataAccountCreditLimitError):
                app._marketdata_get("/v1/options/chain/AAPL/")
            self.assertEqual(get.call_count, 2)
            with self.assertRaises(app.MarketDataAccountCreditLimitError):
                app._marketdata_get("/v1/options/chain/AAPL/")
            self.assertEqual(get.call_count, 2)

    def test_successful_request_after_reset_and_actual_consumption_counters(self):
        self.quota.block("credit limit", {"x-api-ratelimit-reset": str(self.clock[0] + 60)})
        self.clock[0] += 60
        response = Response(headers={"x-api-ratelimit-consumed": "25",
                                     "x-api-ratelimit-remaining": "99975"})
        with (
            patch.object(app.bot, "env", return_value="test"),
            patch.object(app.requests, "get", return_value=response) as get,
        ):
            self.assertEqual(app._marketdata_get("/v1/options/chain/AAPL/"), {"s": "ok"})
            get.assert_called_once()
        snapshot = self.quota.snapshot()
        self.assertFalse(snapshot["blocked"])
        self.assertEqual(snapshot["requests_since_start"], 1)
        self.assertEqual(snapshot["credits_consumed_since_start"], 25)

    def test_exhausted_success_keeps_current_payload_but_blocks_further_http(self):
        response = Response(headers={"x-api-ratelimit-remaining": "0"})
        with (
            patch.object(app.bot, "env", return_value="test"),
            patch.object(app.requests, "get", return_value=response) as get,
        ):
            self.assertEqual(app._marketdata_get("/v1/options/chain/AAPL/"), {"s": "ok"})
            with self.assertRaises(app.MarketDataAccountCreditLimitError):
                app._marketdata_get("/v1/options/quotes/AAPL/")
            get.assert_called_once()

    def test_entire_scanner_resumes_after_reset_without_restart(self):
        self.quota.block("credit limit", {"x-api-ratelimit-reset": str(self.clock[0] + 60)})
        with (
            patch.object(app, "DATA_PROVIDER", "marketdata"),
            patch.object(app.bot, "env", return_value="test"),
            patch.object(app.bot, "AUTO_SCAN_ENABLED", True),
            patch.object(app.bot, "market_is_open_now", return_value=True),
            patch.object(app.bot, "WATCHLIST", ["AAPL"]),
            patch.object(app, "scan_symbol_options",
                         return_value={"accepted": True, "ignored": "no eligible contract"}) as scan,
            patch.object(app.bot.time, "sleep"),
        ):
            self.assertTrue(app.scan_watchlist_once()["provider_blocked"])
            scan.assert_not_called()
            self.clock[0] += 60
            result = app.scan_watchlist_once()
            self.assertFalse(result["provider_blocked"])
            self.assertEqual(result["checked"], 1)
            scan.assert_called_once_with("AAPL")

    def test_cached_response_is_counted_using_provider_cost_without_requesting_cached_mode(self):
        response = Response(203, {"x-api-ratelimit-consumed": "1"})
        with (
            patch.object(app.bot, "env", return_value="test"),
            patch.object(app.requests, "get", return_value=response) as get,
        ):
            app._marketdata_get("/v1/options/chain/AAPL/")
            self.assertNotIn("mode", get.call_args.kwargs["params"])
        self.assertEqual(self.quota.snapshot()["credits_consumed_since_start"], 1)

    def test_last_credits_used_by_success_stop_remaining_symbols_without_errors(self):
        def scan_symbol(symbol):
            self.quota.observe({"x-api-ratelimit-remaining": "0"})
            return {"symbol": symbol, "accepted": True, "ignored": "no eligible contract"}

        with (
            patch.object(app, "DATA_PROVIDER", "marketdata"),
            patch.object(app.bot, "env", return_value="test"),
            patch.object(app.bot, "WATCHLIST", ["AAPL", "AMD", "MSFT"]),
            patch.object(app, "scan_symbol_options", side_effect=scan_symbol) as scan,
            patch.object(app.bot.time, "sleep"),
        ):
            result = app.scan_watchlist_once(force=True)
        scan.assert_called_once_with("AAPL")
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["skipped_symbols"], ["AMD", "MSFT"])
        self.assertTrue(result["provider_blocked"])
        self.assertEqual(app._scanner_result_counts(result)["errors"], 1)

    def test_concurrent_chain_misses_purchase_once_and_preserve_full_universe(self):
        records = [{"details": {"contract_type": "call", "strike_price": 100}},
                   {"details": {"contract_type": "put", "strike_price": 200}}]
        start = threading.Barrier(2)

        def fetch():
            start.wait(timeout=2)
            return app._marketdata_raw_chain("AAPL")

        with (
            patch.object(app, "_MASSIVE_CHAIN_CACHE", {}),
            patch.object(app, "_marketdata_get", return_value={}) as get,
            patch.object(app, "_marketdata_records", return_value=records),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(lambda _: fetch(), range(2)))
        self.assertEqual(results, [records, records])
        get.assert_called_once()
        self.assertEqual(self.quota.snapshot()["chain_cache_hits_since_start"], 1)
        self.assertNotIn("strikeLimit", get.call_args.kwargs["params"])

    def test_expired_chain_is_refetched_without_extending_ttl(self):
        with (
            patch.object(app, "_MASSIVE_CHAIN_CACHE", {}),
            patch.object(app, "_marketdata_get", return_value={}) as get,
            patch.object(app, "_marketdata_records", return_value=[]),
            patch.object(app.time, "monotonic",
                         side_effect=[0, 0, app.MARKETDATA_CHAIN_CACHE_TTL_SECONDS + 1,
                                      app.MARKETDATA_CHAIN_CACHE_TTL_SECONDS + 1]),
        ):
            app._marketdata_raw_chain("AAPL")
            app._marketdata_raw_chain("AAPL")
        self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
