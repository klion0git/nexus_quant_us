"""Tests for US Paper Broker (SQLite WAL), 1-share sizing, +18% earnings gap, SEC/TAF friction, and 2% circuit breaker."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from broker.paper_broker import UsPaperBroker
from notification.discord_notifier import UsDiscordNotifier
from nexus_quant_core.contracts import OrderInstruction, SignalEvent
from us_core.calendar import UsSessionCalendar
from us_core.clock import SimulatedClock
from us_core.config import US_FRICTION_PARAMS
from us_core.time_utils import et_to_utc_ts


class TestUsBrokerAndRisk(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_us_paper.db"
        self.start_ts = et_to_utc_ts(2026, 11, 16, 10, 0, 0)
        self.clock = SimulatedClock(initial_epoch=self.start_ts)
        self.cal = UsSessionCalendar()
        self.broker = UsPaperBroker(
            db_path=self.db_path,
            initial_capital=100_000.0,
            calendar=self.cal,
            clock=self.clock,
            max_position_pct=0.20,
            risk_per_trade_pct=0.01,
            daily_loss_limit_pct=0.02,
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_single_share_unit_sizing_for_high_priced_us_equities(self) -> None:
        # META at $590/sh with $20,000 max position cap -> max 33 shares (non-1000-lot exact integer shares!)
        sig = SignalEvent(
            symbol="META",
            side="BUY",
            action="OPEN",
            price=590.0,
            stop_loss_price=570.0,  # $20/sh risk -> $1,000 risk cap allows 50 sh; $20,000 cap allows 33 sh
            entry_type="BRK",
            entry_subtype="BRK_MA1560",
            reason="High-priced US equity 1-share unit sizing test",
            timestamp=self.clock.now_epoch(),
        )
        res = self.broker.execute_signal(sig, ref_prev_close=580.0)
        self.assertTrue(res["accepted"], res)
        self.assertEqual(res["quantity"], 33)
        self.assertGreater(res["quantity"], 0)
        self.assertLess(res["quantity"], 1000)

    def test_plus_18_percent_us_earnings_gap_is_allowed(self) -> None:
        # NVDA closes yesterday at $120.00 and gaps +18% to $141.60 on earnings: MUST NOT be blocked!
        prev_close = 120.00
        gap_open_px = round(prev_close * 1.18, 2)  # $141.60 (+18%)
        sig = SignalEvent(
            symbol="NVDA",
            side="BUY",
            action="OPEN",
            price=gap_open_px,
            stop_loss_price=137.00,
            entry_type="BRK",
            entry_subtype="BRK_EARNINGS",
            reason="+18% earnings gap breakout",
            timestamp=self.clock.now_epoch(),
        )
        res = self.broker.execute_signal(sig, ref_prev_close=prev_close)
        self.assertTrue(res["accepted"], f"+18% US gap must be allowed: {res}")
        self.assertGreaterEqual(res["quantity"], 1)

    def test_usd_zero_commission_and_sec_taf_sell_friction(self) -> None:
        open_order = OrderInstruction(
            symbol="AAPL",
            side="BUY",
            action="OPEN",
            quantity=50.0,
            price=200.0,
            stop_loss_price=194.0,
            reason="Open AAPL",
            timestamp=self.clock.now_epoch(),
        )
        open_res = self.broker.submit_order(open_order)
        self.assertTrue(open_res["accepted"])
        # BUY has $0.00 commission and $0.00 SEC/TAF tax
        self.assertEqual(open_res["friction"]["commission"], 0.0)
        self.assertEqual(open_res["friction"]["tax"], 0.0)
        self.assertGreater(open_res["friction"]["slippage"], 0.0)

        self.clock.advance(600.0)
        close_order = OrderInstruction(
            symbol="AAPL",
            side="SELL",
            action="CLOSE",
            quantity=50.0,
            price=210.0,
            stop_loss_price=194.0,
            reason="Close AAPL for profit",
            timestamp=self.clock.now_epoch(),
        )
        close_res = self.broker.submit_order(close_order)
        self.assertTrue(close_res["accepted"])
        expected_sec_taf = round(50.0 * 210.0 * US_FRICTION_PARAMS.sec_taf_sell_rate, 4)
        self.assertEqual(close_res["friction"]["commission"], 0.0)
        self.assertAlmostEqual(close_res["friction"]["tax"], expected_sec_taf, places=4)
        self.assertGreater(close_res["realized_pnl_usd"], 490.0)

        # Verify Discord embed formatting in USD
        notifier = UsDiscordNotifier(clock=self.clock)
        embed = notifier.format_trade_embed(close_res)
        self.assertIn("USD", embed["embeds"][0]["title"])
        self.assertIn("SEC/TAF", embed["embeds"][0]["description"])

    def test_loss_cooldown_and_daily_2pct_circuit_breaker(self) -> None:
        # Open max-sized position (20% of $100k = $20,000 -> 200 shares @ $99.50)
        res1 = self.broker.submit_order(
            OrderInstruction(
                symbol="TSLA",
                side="BUY",
                action="OPEN",
                quantity=200.0,
                price=99.50,
                stop_loss_price=94.60,
                reason="Test trade 1",
                timestamp=self.clock.now_epoch(),
            )
        )
        self.assertTrue(res1["accepted"], res1)

        # Lose ~$1,100 on TSLA -> triggers 900s loss cooldown on TSLA, not yet 2% ($2,000) circuit breaker
        self.clock.advance(200.0)
        close1 = self.broker.submit_order(
            OrderInstruction(
                symbol="TSLA",
                side="SELL",
                action="CLOSE",
                quantity=float(res1["quantity"]),
                price=94.00,
                stop_loss_price=94.60,
                reason="Stop loss 1",
                timestamp=self.clock.now_epoch(),
            )
        )
        self.assertTrue(close1["accepted"])
        self.assertLess(close1["realized_pnl_usd"], -1000.0)
        self.assertFalse(close1["circuit_breaker_tripped"])

        # Immediate re-entry on TSLA within 900s loss cooldown must be rejected
        self.clock.advance(200.0)
        retry_tsla = self.broker.submit_order(
            OrderInstruction(
                symbol="TSLA",
                side="BUY",
                action="OPEN",
                quantity=50.0,
                price=95.00,
                stop_loss_price=91.00,
                reason="Premature re-entry",
                timestamp=self.clock.now_epoch(),
            )
        )
        self.assertFalse(retry_tsla["accepted"])
        self.assertIn("Loss cooldown", retry_tsla["reason"])

        # Second losing trade on MSFT pushing cumulative daily loss past -$2,000 (-2% of $100,000)
        res2 = self.broker.submit_order(
            OrderInstruction(
                symbol="MSFT",
                side="BUY",
                action="OPEN",
                quantity=195.0,
                price=100.00,
                stop_loss_price=95.10,
                reason="Test trade 2",
                timestamp=self.clock.now_epoch(),
            )
        )
        self.assertTrue(res2["accepted"], res2)
        self.clock.advance(200.0)
        close2 = self.broker.submit_order(
            OrderInstruction(
                symbol="MSFT",
                side="SELL",
                action="CLOSE",
                quantity=float(res2["quantity"]),
                price=94.00,
                stop_loss_price=95.10,
                reason="Stop loss 2",
                timestamp=self.clock.now_epoch(),
            )
        )
        self.assertTrue(close2["accepted"])
        self.assertTrue(close2["circuit_breaker_tripped"])

        # Any new OPEN order on the same trading day is now blocked by the 2% daily loss circuit breaker
        self.clock.advance(1200.0)
        blocked_spy = self.broker.submit_order(
            OrderInstruction(
                symbol="SPY",
                side="BUY",
                action="OPEN",
                quantity=10.0,
                price=590.00,
                stop_loss_price=580.00,
                reason="Should be blocked by circuit breaker",
                timestamp=self.clock.now_epoch(),
            )
        )
        self.assertFalse(blocked_spy["accepted"])
        self.assertIn("circuit breaker", blocked_spy["reason"].lower())

        # Next NY trading day (2026-11-17 10:00 EST) resets daily circuit breaker automatically!
        self.clock.set_epoch(et_to_utc_ts(2026, 11, 17, 10, 0, 0))
        allowed_next_day = self.broker.submit_order(
            OrderInstruction(
                symbol="SPY",
                side="BUY",
                action="OPEN",
                quantity=10.0,
                price=590.00,
                stop_loss_price=580.00,
                reason="Next trading day reset",
                timestamp=self.clock.now_epoch(),
            )
        )
        self.assertTrue(allowed_next_day["accepted"], allowed_next_day)


if __name__ == "__main__":
    unittest.main()
