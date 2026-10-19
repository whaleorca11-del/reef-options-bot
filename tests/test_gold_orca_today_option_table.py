import unittest
from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[1]
    / "GOLD_ORCA_TODAY_OPTION_TABLE.pine"
).read_text(encoding="utf-8")


class GoldOrcaTodayOptionTableTests(unittest.TestCase):
    def test_standalone_indicator_builds_today_option_symbols_without_manual_tickers(self):
        self.assertIn('indicator("GOLD ORCA — عقد اليوم المستقل"', SOURCE)
        self.assertIn('"O:" + syminfo.ticker', SOURCE)
        self.assertIn("optionTodayDateKey", SOURCE)
        self.assertIn("f_option_ticker", SOURCE)
        self.assertIn("request.security(", SOURCE)
        self.assertNotIn("callContract", SOURCE)
        self.assertNotIn("putContract", SOURCE)

    def test_option_display_requires_a_fresh_tradingview_bar(self):
        self.assertIn("symbol, timeframe.period, close", SOURCE)
        self.assertIn("gaps=barmerge.gaps_on", SOURCE)
        self.assertIn("لا يوجد سعر حديث متاح", SOURCE)
        self.assertIn('f_box(5, 2, hasOption ? todayOptionSymbol : "—"', SOURCE)
        self.assertIn("todayOptionPrice", SOURCE)

    def test_option_data_diagnostic_can_run_without_a_signal_or_weekend_quote(self):
        self.assertIn('input.bool(true, "إظهار فحص جلب بيانات العقد"', SOURCE)
        self.assertIn('input.string("CALL", "اتجاه عقد الاختبار"', SOURCE)
        self.assertIn("optionTestDaysAhead", SOURCE)
        self.assertIn("dayofmonth(timenow, OPTION_TZ) + optionTestDaysAhead", SOURCE)
        self.assertIn("f_option_test_data(optionTestSymbol)", SOURCE)
        self.assertIn("gaps=barmerge.gaps_off", SOURCE)
        self.assertIn("لا توجد شمعة متاحة", SOURCE)
        self.assertIn("خارج الجلسة قد تكون تاريخية", SOURCE)

    def test_stop_tracking_uses_explicit_blocks_for_ambiguous_bar_state(self):
        self.assertEqual(SOURCE.count("if ambiguousTouch"), 2)
        self.assertEqual(
            SOURCE.count('trackingText := "وقف وهدف بنفس الشمعة — الترتيب مجهول"'),
            2,
        )

    def test_indicator_sends_only_authenticated_tradingview_alerts_to_orca(self):
        self.assertIn("REEF_WEBHOOK_SECRET", SOURCE)
        self.assertIn("f_tv_signal_payload", SOURCE)
        self.assertIn('alert(f_tv_signal_payload("CALL", "CALL")', SOURCE)
        self.assertIn('alert(f_tv_signal_payload("PUT", "PUT")', SOURCE)
        self.assertIn('alert(f_tv_signal_payload("PREPARE_CALL", "CALL")', SOURCE)
        self.assertIn('alert(f_tv_signal_payload("PREPARE_PUT", "PUT")', SOURCE)
        self.assertNotIn("api.telegram.org", SOURCE)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", SOURCE)
        self.assertIn("position.top_right, 8, 22", SOURCE)


if __name__ == "__main__":
    unittest.main()
