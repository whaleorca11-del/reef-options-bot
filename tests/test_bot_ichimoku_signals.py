import sys
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as app
import orca_whale_options_bot_FAST_60S_5S as bot


class BotIchimokuSignalTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 2, 10, 5, tzinfo=bot.NEW_YORK_TZ)

    def make_bars(self, last_bar_close=100.0, include_incomplete=False):
        bars = []
        trading_day = date(2026, 9, 21)
        while trading_day <= self.now.date():
            if trading_day.weekday() < 5:
                for market_minute in range(9 * 60 + 30, 16 * 60, 30):
                    hour, minute = divmod(market_minute, 60)
                    bar_start = datetime.combine(
                        trading_day, time(hour, minute), bot.NEW_YORK_TZ
                    )
                    bar_close = bar_start + timedelta(minutes=30)
                    if bar_close > self.now:
                        continue
                    close = 100.0
                    high = 101.0
                    low = 99.0
                    if bar_start.date() == self.now.date() and bar_start.hour == 9:
                        close = last_bar_close
                        high = max(101.0, close)
                        low = min(99.0, close)
                    bars.append({
                        "t": int(bar_start.timestamp() * 1000),
                        "h": high,
                        "l": low,
                        "c": close,
                    })

            trading_day += timedelta(days=1)

        if include_incomplete:
            incomplete_start = datetime(
                2026, 10, 2, 10, 0, tzinfo=bot.NEW_YORK_TZ
            )
            bars.append({
                "t": int(incomplete_start.timestamp() * 1000),
                "h": 103.0,
                "l": 99.0,
                "c": 103.0,
            })
        return bars

    def test_calculates_call_cross_from_latest_completed_bar(self):
        with patch.object(bot, "WATCHLIST", ["AAPL"]):
            signal = app._ichimoku_30m_signal_from_bars(
                "AAPL", self.make_bars(102.0), now=self.now
            )

        self.assertEqual(signal["direction"], "CALL")
        self.assertEqual(signal["signal_type"], "ICHIMOKU_CALL")
        self.assertEqual(signal["interval"], "30")
        self.assertEqual(signal["source"], "orca_internal_massive")
        self.assertEqual(signal["bar_time"], int(
            datetime(2026, 10, 2, 10, 0, tzinfo=bot.NEW_YORK_TZ).timestamp()
            * 1000
        ))

    def test_calculates_put_cross_from_latest_completed_bar(self):
        with patch.object(bot, "WATCHLIST", ["AAPL"]):
            signal = app._ichimoku_30m_signal_from_bars(
                "AAPL", self.make_bars(98.0), now=self.now
            )

        self.assertEqual(signal["direction"], "PUT")
        self.assertEqual(signal["signal_type"], "ICHIMOKU_PUT")

    def test_ignores_incomplete_bar_even_if_it_would_cross(self):
        with patch.object(bot, "WATCHLIST", ["AAPL"]):
            signal = app._ichimoku_30m_signal_from_bars(
                "AAPL",
                self.make_bars(100.0, include_incomplete=True),
                now=self.now,
            )

        self.assertIsNone(signal)

    def test_rejects_stale_completed_bars(self):
        stale_now = datetime(2026, 10, 2, 10, 11, tzinfo=bot.NEW_YORK_TZ)
        with patch.object(bot, "WATCHLIST", ["AAPL"]):
            with self.assertRaisesRegex(ValueError, "stale"):
                app._ichimoku_30m_signal_from_bars(
                    "AAPL", self.make_bars(102.0), now=stale_now
                )

    def test_poll_cannot_reactivate_the_removed_provider(self):
        store = {"trades": {}}
        with (
            patch.object(bot, "WATCHLIST", ["AAPL"]),
            patch.object(bot, "env", return_value="test-api-key"),
            patch.object(
                app,
                "_massive_stock_get",
                return_value={"status": "OK", "results": self.make_bars(102.0)},
            ) as get_bars,
            patch.object(bot, "_load_trade_store", side_effect=lambda: store),
            patch.object(bot, "_save_trade_store"),
        ):
            signal, ignored = app._poll_and_register_bot_ichimoku_signal(
                "AAPL", now=self.now
            )

        self.assertIsNone(signal)
        self.assertIn("permanently removed", ignored)
        self.assertNotIn("ichimoku_30m_signals", store)
        get_bars.assert_not_called()

    def test_removed_feed_is_rejected_before_fetching_bars(self):
        with (
            patch.object(bot, "WATCHLIST", ["AAPL"]),
            patch.object(bot, "env", return_value="test-api-key"),
            patch.object(
                app,
                "_massive_stock_get",
                return_value={"status": "DELAYED", "results": self.make_bars(102.0)},
            ),
        ):
            signal, reason = app._fetch_bot_ichimoku_30m_signal(
                "AAPL", now=self.now
            )

        self.assertIsNone(signal)
        self.assertIn("permanently removed", reason)

    def test_disabled_poll_does_not_read_any_provider_key(self):
        with (
            patch.object(bot, "WATCHLIST", ["AAPL"]),
            patch.object(bot, "env", side_effect=AssertionError("No key reads")),
            patch.object(app, "_massive_stock_get") as get_bars,
        ):
            signal, reason = app._fetch_bot_ichimoku_30m_signal(
                "AAPL", now=self.now
            )

        self.assertIsNone(signal)
        self.assertIn("permanently removed", reason)
        get_bars.assert_not_called()


if __name__ == "__main__":
    unittest.main()