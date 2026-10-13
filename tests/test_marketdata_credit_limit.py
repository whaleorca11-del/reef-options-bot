import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as app
from marketdata_quota import MarketDataQuota


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class MarketDataCreditLimitTests(unittest.TestCase):
    def setUp(self):
        self.credit_limit_patcher = patch.object(
            app, "_MARKETDATA_QUOTA", MarketDataQuota()
        )
        self.credit_limit_patcher.start()

    def tearDown(self):
        self.credit_limit_patcher.stop()

    def test_account_credit_limit_latches_and_prevents_more_requests(self):
        response = FakeResponse(
            429,
            '{"s":"error","errmsg":"You have reached your API credit limit '
            'for your Market Data account."}',
        )
        with (
            patch.object(app.bot, "env", return_value="test-token"),
            patch.object(app.requests, "get", return_value=response) as request,
        ):
            with self.assertRaises(app.MarketDataAccountCreditLimitError):
                app._marketdata_get("/v1/options/chain/AAPL/")

            with self.assertRaises(app.MarketDataAccountCreditLimitError):
                app._marketdata_get("/v1/options/chain/MSFT/")

        request.assert_called_once()
        self.assertIn(
            "credit limit",
            app._marketdata_account_credit_limit_reason().lower(),
        )

    def test_generic_429_does_not_latch_account_credit_limit(self):
        response = FakeResponse(429, "Too many requests; retry later.")
        with (
            patch.object(app.bot, "env", return_value="test-token"),
            patch.object(app.requests, "get", return_value=response) as request,
        ):
            with self.assertRaisesRegex(RuntimeError, "MARKETDATA_HTTP_429"):
                app._marketdata_get("/v1/options/chain/AAPL/")

        request.assert_called_once()
        self.assertIsNone(app._marketdata_account_credit_limit_reason())

    def test_scan_stops_after_credit_limit_and_marks_remaining_symbols_skipped(self):
        symbols = ["AAPL", "MSFT", "NVDA"]
        reason = (
            "MARKETDATA_HTTP_429 /v1/options/chain/AAPL/: "
            "You have reached your API credit limit."
        )

        def hit_credit_limit(_symbol):
            app._latch_marketdata_account_credit_limit(reason)
            raise app.MarketDataAccountCreditLimitError(reason)

        with (
            patch.object(app, "DATA_PROVIDER", "marketdata"),
            patch.object(app.bot, "WATCHLIST", symbols),
            patch.object(app.bot, "AUTO_SCAN_ENABLED", True),
            patch.object(app, "scan_symbol_options", side_effect=hit_credit_limit) as scan,
            patch.object(app, "_is_within_auto_scan_window", return_value=True),
        ):
            first = app.scan_watchlist_once(
                force=True, opening_window_only=True
            )
            scan.assert_called_once_with("AAPL")

            second = app.scan_watchlist_once(
                force=True, opening_window_only=True
            )
            scan.assert_called_once()

        self.assertEqual(first["checked"], 1)
        self.assertEqual(first["skipped_symbols"], ["MSFT", "NVDA"])
        self.assertTrue(first["incomplete"])
        self.assertTrue(first["provider_blocked"])
        self.assertEqual(
            first["results"][0]["status"], "DATA PROVIDER BLOCKED"
        )
        self.assertEqual(second["checked"], 0)
        self.assertEqual(second["skipped_symbols"], symbols)
        self.assertTrue(second["provider_blocked"])

        counts = app._scanner_result_counts(second)
        self.assertEqual(counts["checked"], 0)
        self.assertEqual(counts["errors"], 1)
        self.assertEqual(counts["skipped"], 3)


if __name__ == "__main__":
    unittest.main()
