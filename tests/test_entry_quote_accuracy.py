import sys
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as app


def quote_snapshot(bid, ask, updated=None):
    now = time.time() if updated is None else updated
    expiration = (app.bot.now_new_york().date() + timedelta(days=20)).isoformat()
    ticker = f"AAPL{expiration[2:4]}{expiration[5:7]}{expiration[8:10]}C00215000"
    return ticker, {
        "results": [{
            "details": {
                "ticker": ticker,
                "expiration_date": expiration,
                "strike_price": 215,
            },
            "last_quote": {
                "bid": bid,
                "ask": ask,
                "last_updated": now,
            },
            "last_trade": {"price": (bid + ask) / 2, "timestamp": now},
            "day": {"volume": 1000, "close": (bid + ask) / 2},
            "open_interest": 3000,
            "implied_volatility": 0.35,
            "greeks": {"delta": 0.5, "theta": -0.15},
        }],
    }


class EntryQuoteAccuracyTests(unittest.TestCase):
    def test_entry_refresh_uses_latest_quote_and_recalculates_score(self):
        ticker, snapshot = quote_snapshot(5.85, 5.95)
        candidate = {
            "ticker": ticker,
            "expiration": snapshot["results"][0]["details"]["expiration_date"],
            "strike": 215,
            "score": 90,
        }

        with (
            patch.object(app, "provider_contract_snapshot", return_value=snapshot),
            patch.object(app, "OPTION_ENTRY_MIN_PREMIUM", 2.5),
            patch.object(app, "OPTION_ENTRY_MAX_PREMIUM", 6.0),
        ):
            refreshed = app.refresh_contract_quote_for_entry(
                "AAPL", candidate, 215
            )

        self.assertEqual(refreshed["bid"], 5.85)
        self.assertEqual(refreshed["ask"], 5.95)
        self.assertAlmostEqual(refreshed["midpoint"], 5.90)
        self.assertTrue(refreshed["quote_fresh"])
        self.assertTrue(refreshed["selection_eligible"])
        self.assertEqual(refreshed["estimated_contract_cost_usd"], 595.0)

    def test_entry_refresh_rejects_ask_above_contract_budget(self):
        ticker, snapshot = quote_snapshot(5.90, 6.20)
        candidate = {
            "ticker": ticker,
            "expiration": snapshot["results"][0]["details"]["expiration_date"],
            "strike": 215,
            "score": 90,
        }

        with (
            patch.object(app, "provider_contract_snapshot", return_value=snapshot),
            patch.object(app, "OPTION_ENTRY_MIN_PREMIUM", 2.5),
            patch.object(app, "OPTION_ENTRY_MAX_PREMIUM", 6.0),
        ):
            with self.assertRaisesRegex(RuntimeError, "ask premium \\$6.20"):
                app.refresh_contract_quote_for_entry("AAPL", candidate, 215)

    def test_entry_refresh_rejects_stale_quote(self):
        ticker, snapshot = quote_snapshot(5.85, 5.95, time.time() - 60)
        candidate = {
            "ticker": ticker,
            "expiration": snapshot["results"][0]["details"]["expiration_date"],
            "strike": 215,
            "score": 90,
        }

        with patch.object(app, "provider_contract_snapshot", return_value=snapshot):
            with self.assertRaisesRegex(
                RuntimeError, "FRESH TWO-SIDED OPTION QUOTE REQUIRED"
            ):
                app.refresh_contract_quote_for_entry("AAPL", candidate, 215)

    def test_card_shows_quote_snapshot_not_fake_current_price_when_monitor_off(self):
        metrics = app._card_price_metrics({
            "entry_price": 5.975,
            "midpoint": 5.975,
            "bid": 5.90,
            "ask": 6.05,
            "current_price": 5.975,
        }, monitor_enabled=False)

        self.assertEqual(metrics[0][1:3], ("ENTRY MID", "$5.975"))
        self.assertEqual(metrics[1][1:3], ("ENTRY BID / ASK", "$5.90 / $6.05"))
        self.assertEqual(metrics[2][1:3], ("PRICE UPDATES", "OFF"))

    def test_card_shows_current_mark_only_after_a_monitor_update(self):
        metrics = app._card_price_metrics({
            "entry_price": 5.975,
            "midpoint": 5.975,
            "current_price": 6.20,
            "last_successful_option_price_update": "2026-10-06T10:00:00-04:00",
        }, monitor_enabled=True)

        self.assertEqual(metrics[1][1:3], ("CURRENT MARK", "$6.20"))
        self.assertEqual(metrics[2][1], "CHANGE")

    def test_card_badge_says_signal_until_price_tracking_is_confirmed(self):
        self.assertEqual(
            app._card_badge({"status": "active"}, monitor_enabled=False),
            "SIGNAL",
        )
        self.assertEqual(
            app._card_badge({
                "status": "active",
                "last_successful_option_price_update": "2026-10-06T10:00:00-04:00",
            }, monitor_enabled=True),
            "TRACKING",
        )


class MarketDataExpirationRangeTests(unittest.TestCase):
    def test_chain_requests_all_configured_expirations_and_caches_by_range(self):
        payload = {"optionSymbol": []}
        first_day = datetime(2026, 10, 6, 12)
        next_day = datetime(2026, 10, 7, 12)

        with (
            patch.dict(app._MASSIVE_CHAIN_CACHE, {}, clear=True),
            patch.object(
                app.bot,
                "now_new_york",
                side_effect=[first_day, first_day, next_day],
            ),
            patch.object(
                app, "_marketdata_get", return_value=payload
            ) as marketdata_get,
        ):
            app._marketdata_raw_chain("aapl")
            app._marketdata_raw_chain("AAPL")
            app._marketdata_raw_chain("AAPL")

        self.assertEqual(marketdata_get.call_count, 2)
        self.assertEqual(
            marketdata_get.call_args_list[0].args,
            ("/v1/options/chain/AAPL/",),
        )
        self.assertEqual(
            marketdata_get.call_args_list[0].kwargs["params"],
            {"from": "2026-10-16", "to": "2026-11-05"},
        )
        self.assertEqual(
            marketdata_get.call_args_list[1].kwargs["params"],
            {"from": "2026-10-17", "to": "2026-11-06"},
        )


if __name__ == "__main__":
    unittest.main()
