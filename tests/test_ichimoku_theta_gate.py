import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as app
import orca_whale_options_bot_FAST_60S_5S as bot


def raw_contract(theta=-0.25):
    return {
        "details": {
            "ticker": "AAPL261022C00215000",
            "expiration_date": "2026-10-22",
            "strike_price": 215,
        },
        "day": {"volume": 100, "close": 5},
        "greeks": {"delta": 0.5, "theta": theta},
        "open_interest": 1000,
        "implied_volatility": 0.5,
    }


class ThetaDeltaGateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 2, 10, 0, tzinfo=bot.NEW_YORK_TZ)

    def test_theta_equal_to_half_delta_is_eligible(self):
        with patch.object(bot, "now_new_york", return_value=self.now):
            eligible, _ = app._prepare_side([raw_contract(-0.25)], 215)

        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0]["theta_delta_ratio"], 0.5)

    def test_theta_above_half_delta_is_rejected(self):
        with patch.object(bot, "now_new_york", return_value=self.now):
            eligible, expiration = app._prepare_side([raw_contract(-0.251)], 215)

        self.assertEqual(eligible, [])
        self.assertIsNone(expiration)
        self.assertIn(
            "|Theta| 0.2510 exceeds 50% of |Delta| 0.5000",
            eligible.all_candidates[0]["rejection_reasons"],
        )

    def test_missing_theta_is_rejected(self):
        with patch.object(bot, "now_new_york", return_value=self.now):
            eligible, _ = app._prepare_side([raw_contract(None)], 215)

        self.assertEqual(eligible, [])
        self.assertIn(
            "Theta unavailable",
            eligible.all_candidates[0]["rejection_reasons"],
        )


class ThirtyMinuteIchimokuGateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 2, 10, 0, tzinfo=bot.NEW_YORK_TZ)
        self.payload = {
            "ticker": "NASDAQ:AAPL",
            "direction": "CALL",
            "signal_type": "ICHIMOKU_CALL",
            "interval": "30",
            "price": 215.5,
            "bar_time": int(self.now.timestamp() * 1000),
        }

    def test_accepts_fresh_30_minute_ichimoku_event(self):
        with patch.object(bot, "WATCHLIST", ["AAPL"]):
            signal = app._ichimoku_30m_signal_from_payload(
                self.payload, now=self.now
            )

        self.assertEqual(signal["symbol"], "AAPL")
        self.assertEqual(signal["direction"], "CALL")
        self.assertEqual(signal["timeframe"], "30")

    def test_rejects_non_30_minute_or_mismatched_signal(self):
        with patch.object(bot, "WATCHLIST", ["AAPL"]):
            for changes in (
                {"interval": "60"},
                {"signal_type": "ICHIMOKU_PUT"},
            ):
                payload = {**self.payload, **changes}
                with self.subTest(changes=changes):
                    with self.assertRaises(ValueError):
                        app._ichimoku_30m_signal_from_payload(
                            payload, now=self.now
                        )

    def test_rejects_stale_signal(self):
        payload = dict(self.payload)
        payload["bar_time"] = int((self.now.timestamp() - 1801) * 1000)
        with patch.object(bot, "WATCHLIST", ["AAPL"]):
            with self.assertRaisesRegex(ValueError, "stale"):
                app._ichimoku_30m_signal_from_payload(
                    payload, now=self.now
                )

    def test_rejects_bot_signal_older_than_bar_freshness_limit(self):
        payload = {
            **self.payload,
            "source": "orca_internal_massive",
            "bar_time": int((self.now.timestamp() - 601) * 1000),
        }
        with patch.object(bot, "WATCHLIST", ["AAPL"]):
            with self.assertRaisesRegex(ValueError, "stale"):
                app._ichimoku_30m_signal_from_payload(
                    payload, now=self.now
                )

    def test_only_fresh_bot_native_signals_can_be_used_for_entry(self):
        store = {
            "trades": {},
            "ichimoku_30m_signals": {
                "AAPL": {
                    "source": "orca_internal_massive",
                    "consumed": False,
                    "bar_time_ms": int(
                        (self.now.timestamp() - 601) * 1000
                    ),
                }
            },
        }
        with patch.object(bot, "_load_trade_store", return_value=store):
            self.assertIsNone(
                app._active_ichimoku_30m_signal("AAPL", now=self.now)
            )

        store["ichimoku_30m_signals"]["AAPL"]["bar_time_ms"] = int(
            (self.now.timestamp() - 300) * 1000
        )
        with patch.object(bot, "_load_trade_store", return_value=store):
            active = app._active_ichimoku_30m_signal(
                "AAPL", now=self.now
            )
        self.assertIsNotNone(active)

        store["ichimoku_30m_signals"]["AAPL"]["source"] = "external_webhook"
        with patch.object(bot, "_load_trade_store", return_value=store):
            self.assertIsNone(
                app._active_ichimoku_30m_signal("AAPL", now=self.now)
            )

    def test_consumed_event_cannot_be_replayed(self):
        store = {"trades": {}}
        with (
            patch.object(bot, "WATCHLIST", ["AAPL"]),
            patch.object(bot, "_load_trade_store", side_effect=lambda: store),
            patch.object(bot, "_save_trade_store"),
            patch.object(bot, "now_new_york", return_value=self.now),
        ):
            signal, ignored = app._register_ichimoku_30m_signal(
                self.payload, now=self.now
            )
            self.assertIsNone(ignored)
            self.assertTrue(
                app._consume_ichimoku_30m_signal(
                    "AAPL", signal["event_id"]
                )
            )
            replay, ignored = app._register_ichimoku_30m_signal(
                self.payload, now=self.now
            )

        self.assertIsNone(replay)
        self.assertEqual(ignored, "duplicate 30-minute Ichimoku event")

    def test_blocks_later_same_direction_breakout_after_contract_was_sent(self):
        store = {"trades": {}}
        first_payload = {
            **self.payload,
            "bar_time": int((self.now.timestamp() - 1200) * 1000),
        }
        later_payload = {
            **self.payload,
            "bar_time": int((self.now.timestamp() - 600) * 1000),
        }
        with (
            patch.object(bot, "WATCHLIST", ["AAPL"]),
            patch.object(bot, "_load_trade_store", side_effect=lambda: store),
            patch.object(bot, "_save_trade_store"),
            patch.object(bot, "now_new_york", return_value=self.now),
        ):
            signal, ignored = app._register_ichimoku_30m_signal(
                first_payload, now=self.now
            )
            self.assertIsNone(ignored)
            app._consume_ichimoku_30m_signal("AAPL", signal["event_id"])

            repeated, ignored = app._register_ichimoku_30m_signal(
                later_payload, now=self.now
            )

        self.assertIsNone(repeated)
        self.assertEqual(
            ignored, "same-direction Ichimoku contract already sent"
        )

    def test_opposite_direction_can_signal_after_last_contract(self):
        store = {"trades": {}}
        first_payload = {
            **self.payload,
            "direction": "PUT",
            "signal_type": "ICHIMOKU_PUT",
            "bar_time": int((self.now.timestamp() - 1200) * 1000),
        }
        opposite_payload = {
            **self.payload,
            "direction": "CALL",
            "signal_type": "ICHIMOKU_CALL",
            "bar_time": int((self.now.timestamp() - 600) * 1000),
        }
        with (
            patch.object(bot, "WATCHLIST", ["AAPL"]),
            patch.object(bot, "_load_trade_store", side_effect=lambda: store),
            patch.object(bot, "_save_trade_store"),
            patch.object(bot, "now_new_york", return_value=self.now),
        ):
            signal, ignored = app._register_ichimoku_30m_signal(
                first_payload, now=self.now
            )
            self.assertIsNone(ignored)
            app._consume_ichimoku_30m_signal("AAPL", signal["event_id"])

            opposite, ignored = app._register_ichimoku_30m_signal(
                opposite_payload, now=self.now
            )

        self.assertIsNone(ignored)
        self.assertEqual(opposite["direction"], "CALL")

    def test_scanner_qualifies_orca_direction_without_ichimoku_signal(self):
        call = {
            "ticker": "AAPL261022C00215000",
            "expiration": "2026-10-22",
            "strike": 215,
            "score": 80,
        }
        put = {
            "ticker": "AAPL261022P00215000",
            "expiration": "2026-10-22",
            "strike": 215,
            "score": 70,
        }
        gamma_info = {
            "flip": 100,
            "modeled_net_gex": 10,
            "gex_sign": "positive",
            "regime": "positive",
            "status": "OK",
            "gamma_distance_percent": 0,
            "gamma_completeness_percent": 100,
            "gamma_confidence": "HIGH",
        }

        with (
            patch.object(app, "_fetch_macd_1h_direction", return_value={"direction": "CALL"}),
            patch.object(app, "_conservative_assessment", return_value={
                "can_open": True, "wait_reasons": [], "wait_status": "TRADE",
                "trade_confidence": {"level": "HIGH", "score": 90, "components": {}},
                "gamma_direction_state": "confirmed", "entry_source": "Fresh Bid/Ask Midpoint",
            }),
            patch.object(bot, "_load_trade_store", return_value={"trades": {}}),
            patch.object(
                app, "_active_ichimoku_30m_signal",
                side_effect=AssertionError("Ichimoku must not gate entry"),
            ),
            patch.object(app, "massive_chain", side_effect=[[], []]),
            patch.object(app, "fetch_underlying_price", return_value=(100, "test")),
            patch.object(bot, "calculate_gamma_flip", return_value=gamma_info),
            patch.object(
                bot, "_prepare_side",
                side_effect=[([call], "2026-10-22"), ([put], "2026-10-22")],
            ),
            patch.object(
                bot, "_side_flow_stats",
                side_effect=[{"best": call}, {"best": put}],
            ),
            patch.object(bot, "_direction_strength", side_effect=[5, 2]),
            patch.object(
                app, "_side_diagnostic",
                side_effect=lambda direction, *_args, **_kwargs: {
                    "accepted": True,
                    "edge": 3 if direction == "CALL" else 1,
                },
            ),
            patch.object(app, "_gamma_state_fields", return_value={"gamma_data_quality": {}}),
            patch.object(app, "_clear_pending_candidate"),
            patch.object(app, "create_trade", return_value={
                "contract_ticker": call["ticker"],
                "message_id": 12,
                "trade_confidence": "HIGH",
            }) as create_trade,
            patch.object(app, "ORCA_CONSERVATIVE_MODE", False),
        ):
            result = app.scan_symbol_options("AAPL")

        self.assertEqual(result["status"], "TRADE")
        self.assertEqual(result["direction"], "CALL")
        self.assertEqual(result["entry_signal_mode"], "ORCA_SCORE_GAMMA")
        create_trade.assert_called_once()

    def test_create_trade_does_not_require_ichimoku_signal(self):
        store = {"trades": {}, "pending_candidates": {}}
        contract = {
            "ticker": "AAPL261022C00215000",
            "expiration": "2026-10-22",
            "strike": 215,
            "score": 80,
        }

        with (
            patch.object(bot, "now_new_york", return_value=self.now),
            patch.object(app, "_fetch_macd_1h_direction", return_value={
                "symbol": "AAPL", "direction": "CALL", "signal_type": "MACD_1H_EMA200",
                "timeframe": "60", "ema_length": 200, "ema_average": "EMA",
                "price": 215, "ema200": 200,
                "bar_time_ms": (((int(self.now.timestamp()) - 1800) // 3600) * 3600 - 1800) * 1000,
                "bar_close_time_ms": (((int(self.now.timestamp()) - 1800) // 3600) * 3600 + 1800) * 1000,
                "histogram": 0.1,
            }),
            patch.object(bot, "_load_trade_store", side_effect=lambda: store),
            patch.object(bot, "_save_trade_store"),
            patch.object(
                app, "_active_ichimoku_30m_signal",
                side_effect=AssertionError("Ichimoku must not gate entry"),
            ),
            patch.object(bot, "telegram_send_card", return_value=12),
            patch.object(app, "_paper_trade_record", return_value={"symbol": "AAPL"}),
            patch.object(app, "_gamma_state_fields", return_value={}),
            patch.object(
                app, "refresh_contract_quote_for_entry",
                return_value={
                    **contract,
                    "bid": 4.45,
                    "ask": 4.55,
                    "midpoint": 4.5,
                    "spread_percent": 2.22,
                    "quote_fresh": True,
                    "quote_age_seconds": 1,
                    "quote_timestamp": None,
                    "delta": 0.5,
                    "theta": -0.1,
                    "iv": 0.3,
                    "volume": 1000,
                    "open_interest": 3000,
                    "dte": 16,
                },
            ),
            patch.object(app, "_conservative_assessment", return_value={
                "can_open": True,
                "trade_confidence": {"level": "HIGH", "score": 90, "components": {}},
                "gamma_direction_state": "confirmed",
            }),
        ):
            trade = app.create_trade(
                "AAPL", "CALL", 215, contract=contract,
                gamma_direction="NEUTRAL",
            )

        self.assertEqual(trade["status"], "active")
        self.assertEqual(trade["entry_signal_type"], "ORCA_SCORE_GAMMA")
        self.assertEqual(trade["message_id"], 12)


if __name__ == "__main__":
    unittest.main()