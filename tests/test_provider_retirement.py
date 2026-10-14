"""Provider removal must hold even with stale production configuration."""
import unittest
from pathlib import Path
from unittest.mock import patch

import main as app
from provider_config import resolve_data_provider


class ProviderRetirementTests(unittest.TestCase):
    def setUp(self):
        enabled = patch.object(app, "LIVE_ENTRY_ALERTS_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def test_default_and_retired_configuration_select_marketdata(self):
        for value in (None, "", "massive", "MASSIVE", "polygon", "polygon.io"):
            with self.subTest(value=value):
                self.assertEqual(resolve_data_provider(value), "marketdata")

    def test_other_supported_providers_remain_explicit(self):
        self.assertEqual(resolve_data_provider("marketdata_live"), "marketdata")
        self.assertEqual(resolve_data_provider("tradier_live"), "tradier")
        self.assertEqual(resolve_data_provider("theta"), "thetadata")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            resolve_data_provider("unknown")

    def test_dispatchers_cannot_fall_back_to_retired_provider(self):
        with (
            patch.object(app, "DATA_PROVIDER", "massive"),
            patch.object(app.bot, "env", side_effect=AssertionError("No key reads")),
            patch.object(app.requests, "get") as get,
        ):
            for call in (
                lambda: app.massive_chain("AAPL", "CALL"),
                lambda: app.provider_contract_snapshot("AAPL", "AAPL261030C00345000"),
                lambda: app.fetch_underlying_price("AAPL", [], []),
                app._provider_token,
            ):
                with self.assertRaisesRegex(RuntimeError, "removed data provider"):
                    call()
            get.assert_not_called()

    def test_marketdata_dispatch_and_missing_quote_do_not_fall_back(self):
        with (
            patch.object(app, "DATA_PROVIDER", "marketdata"),
            patch.object(app, "_marketdata_chain", return_value=[]) as chain,
            patch.object(app, "_marketdata_contract_snapshot",
                         side_effect=RuntimeError("MarketData unavailable")),
            patch.object(app, "_massive_stock_get") as retired,
        ):
            self.assertEqual(app.massive_chain("AAPL", "CALL"), [])
            chain.assert_called_once_with("AAPL", "CALL")
            with self.assertRaisesRegex(RuntimeError, "MarketData unavailable"):
                app.provider_contract_snapshot("AAPL", "AAPL261030C00345000")
            retired.assert_not_called()

    def test_missing_marketdata_key_blocks_scan_without_network_or_sending(self):
        with (
            patch.object(app, "DATA_PROVIDER", "marketdata"),
            patch.object(app.bot, "AUTO_SCAN_ENABLED", True),
            patch.object(app.bot, "market_is_open_now", return_value=True),
            patch.object(app.bot, "WATCHLIST", ["AAPL", "NVDA"]),
            patch.object(app.bot, "env", return_value="") as env,
            patch.object(app, "scan_symbol_options") as scan,
            patch.object(app.requests, "get") as get,
            patch.object(app.bot, "telegram_send_card") as send,
        ):
            result = app.scan_watchlist_once()
            self.assertFalse(result["accepted"])
            self.assertEqual(result["provider_block_kind"], "missing_key")
            self.assertEqual(result["checked"], 0)
            self.assertEqual(result["skipped_symbols"], ["AAPL", "NVDA"])
            self.assertIn("MARKETDATA_API_KEY is missing", result["error"])
            env.assert_called_once_with("MARKETDATA_API_KEY")
            scan.assert_not_called()
            get.assert_not_called()
            send.assert_not_called()

    def test_retired_stock_fetch_never_makes_http_requests(self):
        with patch.object(app.requests, "get") as get:
            with self.assertRaisesRegex(RuntimeError, "permanently removed"):
                app._massive_stock_get("/v2/aggs", "unused")
            get.assert_not_called()

    def test_production_modules_have_no_retired_endpoint_or_key_lookup(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("main.py", "orca_whale_options_bot_FAST_60S_5S.py"):
            source = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("https://api.massive.com", source)
            self.assertNotIn("MASSIVE_API_KEY", source)


if __name__ == "__main__":
    unittest.main()
