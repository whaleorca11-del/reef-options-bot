import copy
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as app
import orca_whale_options_bot_FAST_60S_5S as bot


def option(ticker, score=80):
    return {
        "ticker": ticker,
        "expiration": "2026-10-22",
        "strike": 215,
        "score": score,
        "selection_eligible": True,
    }


def fresh_entry_contract(contract):
    return {
        **contract,
        "score": 80,
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
    }


class MultiContractTradeTests(unittest.TestCase):
    def setUp(self):
        enabled = patch.object(app, "LIVE_ENTRY_ALERTS_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def test_repeated_cross_after_disk_reload_never_resends_recorded_contract(self):
        from paper_entry_rules import crossing_signal
        ticker = "AAPL261022C00215000"
        for status in ("active", "closed", "expired", "recovery_required", "opening"):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as directory,
                patch.object(app, "trade_state_path", return_value=Path(directory) / "trades.json"),
                patch.object(bot, "_load_trade_store", side_effect=app._load_durable_trade_store),
                patch.object(bot, "_save_trade_store") as save,
                patch.object(app, "refresh_contract_quote_for_entry") as quote,
                patch.object(bot, "telegram_send_card") as send,
            ):
                stored = {"trades": {"AAPL": {
                    "symbol": "AAPL", "contract_ticker": ticker,
                    "direction": "CALL", "status": status, "message_id": 42,
                }}}
                app._save_durable_trade_store(stored)
                original = app.trade_state_path().read_bytes()
                # Breaks and re-crosses are real direction observations, but the
                # saved exact option ticker must not produce a second card.
                for previous, close, direction in ((99, 101, "CALL"),
                                                    (101, 99, "PUT"),
                                                    (99, 101, "CALL")):
                    result = crossing_signal(previous, close, {"ema9_5m": 100},
                                             {"ema9_5m": 100}, bar_closed=True)
                    self.assertEqual(result["direction"], direction)
                    if direction == "CALL":
                        duplicate = app.create_trade(
                            "AAPL", direction, close,
                            contract=option(f"O:{ticker}"),
                        )
                        self.assertTrue(duplicate["_ignored_duplicate"])
                        self.assertEqual(duplicate["message_id"], 42)
                self.assertEqual(app.trade_state_path().read_bytes(), original)
                send.assert_not_called()
                quote.assert_not_called()
                save.assert_not_called()

    def test_distinct_contract_coexists_and_same_contract_is_not_resent(self):
        old_trade = {
            "symbol": "AAPL",
            "contract_ticker": "AAPL261022C00215000",
            "direction": "CALL",
            "status": "active",
            "message_id": 10,
            "entry_price": 4.0,
        }
        original_old_trade = copy.deepcopy(old_trade)
        store = {"trades": {"AAPL": old_trade}, "pending_candidates": {}}
        contract = option("AAPL261022C00220000")

        with (
            patch.object(app, "_fetch_macd_1h_direction", side_effect=lambda *_a, **_k: {
                "symbol": "AAPL", "direction": "CALL", "signal_type": "MACD_1H_EMA200",
                "timeframe": "60", "ema_length": 200, "ema_average": "EMA",
                "price": 220, "ema200": 200,
                "bar_time_ms": (((int(bot.now_new_york().timestamp()) - 1800) // 3600) * 3600 - 1800) * 1000,
                "bar_close_time_ms": (((int(bot.now_new_york().timestamp()) - 1800) // 3600) * 3600 + 1800) * 1000,
                "histogram": 0.1,
            }),
            patch.object(bot, "_load_trade_store", side_effect=lambda: store),
            patch.object(bot, "_save_trade_store"),
            patch.object(bot, "telegram_send_card", return_value=22) as send_card,
            patch.object(app, "_paper_trade_record", return_value={"id": "paper-new"}),
            patch.object(app, "_gamma_state_fields", return_value={}),
            patch.object(
                app, "refresh_contract_quote_for_entry",
                side_effect=lambda _symbol, candidate, _stock: fresh_entry_contract(candidate),
            ),
            patch.object(app, "_conservative_assessment", return_value={
                "can_open": True,
                "trade_confidence": {"level": "HIGH", "score": 90, "components": {}},
                "gamma_direction_state": "confirmed",
            }),
        ):
            opened = app.create_trade(
                "AAPL", "CALL", 215, contract=contract,
                gamma_direction="NEUTRAL",
            )
            duplicate = app.create_trade(
                "AAPL", "CALL", 215,
                contract={**contract, "ticker": f"O:{contract['ticker']}"},
                gamma_direction="NEUTRAL",
            )

        new_key = app._trade_storage_key("AAPL", contract["ticker"])
        self.assertEqual(opened["status"], "active")
        self.assertEqual(opened["message_id"], 22)
        self.assertIn("AAPL", store["trades"])
        self.assertIn(new_key, store["trades"])
        self.assertEqual(store["trades"]["AAPL"], original_old_trade)
        self.assertEqual(store["trades"][new_key]["status"], "active")
        self.assertTrue(duplicate["_ignored_duplicate"])
        self.assertEqual(len(store["trades"]), 2)
        send_card.assert_called_once()

    def test_scanner_filters_used_contract_without_blocking_active_symbol(self):
        old_ticker = "AAPL261022C00215000"
        new_call = option("AAPL261022C00220000", score=90)
        put = option("AAPL261022P00215000", score=70)
        store = {
            "trades": {
                "AAPL": {
                    "symbol": "AAPL",
                    "contract_ticker": old_ticker,
                    "status": "active",
                    "message_id": 10,
                }
            },
            "pending_candidates": {},
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

        def stats(candidates):
            return {
                "best": candidates[0] if candidates else None,
                "volume": 100,
                "open_interest": 1000,
                "candidate_scores": [],
                "candidate_count": len(candidates),
                "eligible_candidate_count": len(candidates),
                "raw_count": len(candidates),
                "skipped_identity": 0,
            }

        with (
            patch.object(app, "_fetch_macd_1h_direction", return_value={"direction": "CALL"}),
            patch.object(app, "_conservative_assessment", return_value={
                "can_open": True, "wait_reasons": [], "wait_status": "TRADE",
                "trade_confidence": {"level": "HIGH", "score": 90, "components": {}},
                "gamma_direction_state": "confirmed", "entry_source": "Fresh Bid/Ask Midpoint",
            }),
            patch.object(bot, "_load_trade_store", side_effect=lambda: store),
            patch.object(bot, "_save_trade_store"),
            patch.object(app, "massive_chain", side_effect=[[], []]),
            patch.object(app, "fetch_underlying_price", return_value=(215, "test")),
            patch.object(bot, "calculate_gamma_flip", return_value=gamma_info),
            patch.object(
                bot, "_prepare_side",
                side_effect=[
                    ([option(old_ticker), new_call], "2026-10-22"),
                    ([put], "2026-10-22"),
                ],
            ),
            patch.object(bot, "_side_flow_stats", side_effect=stats),
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
                "contract_ticker": new_call["ticker"],
                "message_id": 22,
                "trade_confidence": "HIGH",
            }) as create_trade,
            patch.object(app, "ORCA_CONSERVATIVE_MODE", False),
        ):
            result = app.scan_symbol_options("AAPL")

        self.assertEqual(result["status"], "TRADE")
        self.assertEqual(result["contract"], new_call["ticker"])
        create_trade.assert_called_once()
        self.assertEqual(create_trade.call_args.kwargs["contract"], new_call)
        self.assertEqual(store["trades"]["AAPL"]["status"], "active")

    def test_monitor_updates_composite_entry_without_touching_legacy_trade(self):
        old_trade = {
            "symbol": "AAPL",
            "contract_ticker": "AAPL261022C00215000",
            "status": "active",
            "message_id": 10,
            "monitor_version": 1,
            "entry_price": 4.0,
        }
        new_trade = {
            "symbol": "AAPL",
            "trade_record_key": "AAPL::AAPL261022C00220000",
            "contract_ticker": "AAPL261022C00220000",
            "status": "active",
            "message_id": 22,
            "monitor_version": 1,
            "entry_price": 4.0,
            "current_price": 4.0,
            "highest_price": 4.0,
            "lowest_price": 4.0,
            "achieved": 0,
            "stop_loss": 2.5,
            "targets": [5, 6, 7, 8, 9],
            "paper_trade_id": "paper-new",
        }
        original_old_trade = copy.deepcopy(old_trade)
        store = {
            "trades": {
                "AAPL": old_trade,
                "AAPL::AAPL261022C00220000": new_trade,
            },
            "paper_trades": [{
                "id": "paper-new",
                "entry_timestamp": "2026-10-05T10:00:00-04:00",
                "time_to_first_target_seconds": None,
            }],
        }
        identity = (new_trade["contract_ticker"], 22, 1)

        with (
            patch.object(bot, "_load_trade_store", side_effect=lambda: store),
            patch.object(bot, "_save_trade_store"),
            patch.object(bot, "now_new_york") as now,
        ):
            now.return_value.isoformat.return_value = "2026-10-05T10:00:00-04:00"
            updated = app._apply_monitored_price(
                "AAPL::AAPL261022C00220000", "AAPL", identity,
                4.2, "fresh_quote", 1, 0.1, 0.1,
            )

        self.assertEqual(updated["current_price"], 4.2)
        self.assertEqual(store["trades"]["AAPL"], original_old_trade)
        self.assertEqual(
            store["trades"]["AAPL::AAPL261022C00220000"]["current_price"],
            4.2,
        )


if __name__ == "__main__":
    unittest.main()
