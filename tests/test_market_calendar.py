import unittest
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as app
import orca_whale_options_bot_FAST_60S_5S as bot


def ny_time(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=bot.NEW_YORK_TZ)


class MarketCalendarTests(unittest.TestCase):
    def test_regular_weekday_session(self):
        self.assertTrue(bot.market_is_open_now(ny_time(2026, 10, 5, 9, 30)))
        self.assertFalse(bot.market_is_open_now(ny_time(2026, 10, 5, 9, 29)))
        self.assertFalse(bot.market_is_open_now(ny_time(2026, 10, 5, 16, 0)))

    def test_weekends_and_2026_holidays_are_closed(self):
        for moment in (
            ny_time(2026, 10, 3, 11),  # Saturday
            ny_time(2026, 1, 19, 11),  # Martin Luther King Jr. Day
            ny_time(2026, 4, 3, 11),  # Good Friday
            ny_time(2026, 7, 3, 11),  # Independence Day observed
            ny_time(2026, 11, 26, 11),  # Thanksgiving
        ):
            with self.subTest(moment=moment):
                self.assertFalse(bot.market_is_open_now(moment))

    def test_2026_early_closes_at_1pm(self):
        for day in ((2026, 11, 27), (2026, 12, 24)):
            with self.subTest(day=day):
                self.assertTrue(bot.market_is_open_now(ny_time(*day, 12, 59)))
                self.assertFalse(bot.market_is_open_now(ny_time(*day, 13, 0)))
        self.assertTrue(bot.market_is_open_now(ny_time(2026, 7, 2, 15, 59)))

    def test_2026_calendar_matches_nyse_recurring_dates(self):
        expected_holidays = {
            (1, 1), (1, 19), (2, 16), (4, 3), (5, 25),
            (6, 19), (7, 3), (9, 7), (11, 26), (12, 25),
        }
        actual_holidays = {
            (day.month, day.day)
            for day in bot._market_holidays(2026)
            if day.year == 2026
        }
        self.assertEqual(actual_holidays, expected_holidays)

        expected_early_closes = {(11, 27), (12, 24)}
        actual_early_closes = {
            (day.month, day.day)
            for day in bot._market_early_closes(2026)
            if day.year == 2026
        }
        self.assertEqual(actual_early_closes, expected_early_closes)

    def test_independence_day_early_close_when_july_3_is_a_trading_day(self):
        self.assertTrue(bot.market_is_open_now(ny_time(2028, 7, 3, 12, 59)))
        self.assertFalse(bot.market_is_open_now(ny_time(2028, 7, 3, 13, 0)))

    def test_new_years_day_saturday_is_not_observed_by_nyse(self):
        self.assertTrue(bot.market_is_open_now(ny_time(2027, 12, 31, 10)))

    def test_new_years_day_sunday_is_observed_on_monday(self):
        self.assertFalse(bot.market_is_open_now(ny_time(2023, 1, 2, 10)))


class AutoScanCadenceTests(unittest.TestCase):
    def test_waits_fifteen_minutes_after_open_before_first_scan(self):
        for hour, minute, expected_seconds in (
            (9, 30, 900),
            (9, 33, 720),
            (9, 36, 540),
            (9, 39, 360),
            (9, 44, 60),
            (9, 45, 900),
        ):
            with self.subTest(time=(hour, minute)):
                self.assertEqual(
                    app._seconds_until_next_auto_scan(
                        ny_time(2026, 10, 2, hour, minute),
                        regular_interval_seconds=900,
                        opening_interval_seconds=900,
                    ),
                    expected_seconds,
                )

    def test_continues_every_fifteen_minutes_after_delayed_start(self):
        for hour, minute, expected_seconds in (
            (9, 45, 900),
            (9, 59, 60),
            (10, 0, 900),
            (10, 5, 600),
        ):
            with self.subTest(time=(hour, minute)):
                self.assertEqual(
                    app._seconds_until_next_auto_scan(
                        ny_time(2026, 10, 2, hour, minute),
                        regular_interval_seconds=900,
                        opening_interval_seconds=900,
                    ),
                    expected_seconds,
                )

    def test_closed_holiday_waits_until_next_open_session(self):
        now = ny_time(2026, 11, 26, 10)  # Thanksgiving
        next_open = ny_time(2026, 11, 27, 9, 30)
        self.assertEqual(
            app._seconds_until_next_market_open(now),
            (next_open - now).total_seconds(),
        )

    def test_early_close_waits_until_next_trading_day(self):
        now = ny_time(2026, 11, 27, 13)  # Early close after Thanksgiving
        next_open = ny_time(2026, 11, 30, 9, 30)
        self.assertEqual(
            app._seconds_until_next_market_open(now),
            (next_open - now).total_seconds(),
        )


if __name__ == "__main__":
    unittest.main()