import tempfile
import unittest
import copy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main as app


class MacdCardDeliveryTests(unittest.TestCase):
    def test_new_layout_preserves_trade_data_and_all_five_targets_share_one_row(self):
        state = {
            "symbol": "NVDA", "direction": "PUT", "status": "active",
            "stock_price": 200, "expiration": "2026-10-30",
            "contract_ticker": "NVDA261030P00200000", "strike": 200,
            "entry_price": 4.5, "midpoint": 4.5, "bid": 4.45, "ask": 4.55,
            "targets": [5.4, 6.525, 7.65, 9, 11.25], "stop_loss": 2.475,
            "score": 90, "created_at": app.bot.now_new_york().isoformat(),
        }
        original = copy.deepcopy(state)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(app.bot, "__file__", str(Path(tmp) / "template.py")),
            patch.object(app.bot, "fetch_company_logo", return_value=None),
            patch.object(app.bot.ImageDraw.ImageDraw, "text", autospec=True) as text,
        ):
            output = app.render_trade_card(state)
            with app.bot.Image.open(output) as image:
                self.assertEqual(image.size, (1080, 1620))
            calls = {call.args[2]: call for call in text.call_args_list}
            self.assertIn("PUT ↘", calls)
            self.assertIn("UNAVAILABLE", calls)
            self.assertIn("N/A", calls)
            self.assertEqual(len({
                calls[f"${target:.2f}"].args[1][1] for target in state["targets"]
            }), 1)
            self.assertEqual(calls["PUT ↘"].kwargs["fill"], "#FF596D")
            contract_calls = [
                call for call in text.call_args_list
                if call.args[2] == state["contract_ticker"]
            ]
            self.assertEqual(len(contract_calls), 1)
            self.assertLess(contract_calls[0].args[1][1], calls["STRIKE"].args[1][1])
            self.assertTrue(contract_calls[0].kwargs["font"].size > 40)
            self.assertNotIn("CONTRACT", calls)
            self.assertNotIn(app.bot.COMPANY_NAMES.get("NVDA", "NVDA"), calls)
            self.assertEqual(state, original)

    def test_monitoring_states_do_not_invent_live_prices(self):
        base = {
            "symbol": "AAPL", "direction": "CALL", "status": "active",
            "stock_price": 200, "expiration": "2026-10-30",
            "contract_ticker": "AAPL261030C00200000", "strike": 200,
            "entry_price": 4.5, "targets": [5.4, 6.525, 7.65, 9, 11.25],
            "stop_loss": 2.475, "score": 90,
        }
        for enabled, updated, expected in (
            (False, False, "PRICE UPDATES OFF"),
            (True, False, "PRICE UPDATES WAITING"),
            (True, True, "PRICE UPDATES ON"),
        ):
            state = dict(base)
            if updated:
                state.update(current_price=5, achieved=2,
                             last_successful_option_price_update=app.bot.now_new_york().isoformat())
            with (
                self.subTest(enabled=enabled, updated=updated),
                tempfile.TemporaryDirectory() as tmp,
                patch.object(app, "AUTO_MONITOR_ENABLED", enabled),
                patch.object(app.bot, "__file__", str(Path(tmp) / "template.py")),
                patch.object(app.bot.ImageDraw.ImageDraw, "text", autospec=True) as text,
            ):
                app.render_trade_card(state)
                labels = [call.args[2] for call in text.call_args_list]
                self.assertIn(expected, labels)
                if updated:
                    self.assertIn("Targets 2/5", labels)
                    self.assertTrue(any(label.startswith("Mark $5.00") for label in labels))
                else:
                    self.assertIn("Targets not tracked", labels)
                    self.assertFalse(any(label.startswith("Mark ") for label in labels))

    def test_card_labels_hourly_gate_and_preserves_historical_five_minute_cards(self):
        now = app.bot.now_new_york()
        state = {
            "symbol": "AAPL", "direction": "CALL", "status": "test",
            "stock_price": 200, "expiration": (now.date() + timedelta(days=10)).isoformat(),
            "contract_ticker": "AAPL261023C00200000", "strike": 200,
            "entry_price": 4.5, "midpoint": 4.5, "bid": 4.45, "ask": 4.55,
            "targets": [5.4, 6.525, 7.65, 9, 11.25], "stop_loss": 2.475,
            "score": 90, "created_at": now.isoformat(),
        }
        for signal_type, direction, expected in (
            (app.macd_direction.SIGNAL_TYPE, "CALL", "120/180/40 | 1h | GREEN | ABOVE EMA200"),
            (app.macd_direction.SIGNAL_TYPE, "PUT", "120/180/40 | 1h | RED | BELOW EMA200"),
            ("MACD_5M", "CALL", "120/180/40 | 5m | GREEN"),
        ):
            with self.subTest(signal_type=signal_type, direction=direction), tempfile.TemporaryDirectory() as tmp:
                with (
                    patch.object(app.bot, "__file__", str(Path(tmp) / "template.py")),
                    patch.object(app.bot, "fetch_company_logo", return_value=None),
                    patch.object(app.bot.ImageDraw.ImageDraw, "text", autospec=True) as text,
                ):
                    image = app.render_trade_card({
                        **state, "entry_signal_type": signal_type, "direction": direction,
                    })
                    self.assertTrue(image.exists())
                    labels = [call.args[2] for call in text.call_args_list]
                    self.assertIn(expected, labels)
                    self.assertIn("TEST", labels)

    def test_test_caption_is_explicit_and_sending_never_writes_trade_state(self):
        for status in ("test", "active"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                photo = Path(tmp) / "test.png"
                photo.write_bytes(b"mock photo, not uploaded")
                response = SimpleNamespace(ok=True, json=lambda: {
                    "ok": True, "result": {"message_id": 123},
                })
                with (
                    patch.object(app.bot, "env", return_value="unit-test-only"),
                    patch.object(app, "_render_unique_card", return_value=photo),
                    patch.object(app.requests, "post", return_value=response) as post,
                    patch.object(app.bot, "_load_trade_store") as load,
                    patch.object(app.bot, "_save_trade_store") as save,
                ):
                    self.assertEqual(app.telegram_send_card({"status": status}), 123)
                    data = post.call_args.kwargs["data"]
                    if status == "test":
                        self.assertIn("ليست توصية", data["caption"])
                        self.assertIn("افتراضية", data["caption"])
                    else:
                        self.assertNotIn("caption", data)
                    load.assert_not_called()
                    save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
