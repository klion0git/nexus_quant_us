"""Tests for 26-bars/day US Equity Strategy (MA520/MA1560 BRK/MFO/BTX 0.88, BMR, early-close gate) & ES=F Wave Filter."""
from __future__ import annotations

import asyncio
import datetime
import tempfile
import unittest
from pathlib import Path
from typing import List

from main import UsTradingEngine
from nexus_quant_core.contracts import CandleBar
from strategies.stock_us_strategy import StockUsStrategy
from strategies.us_futures_wave_filter import UsFuturesWaveFilter
from us_core.calendar import UsSessionCalendar
from us_core.clock import SimulatedClock
from us_core.config import TZ_UTC, US_BREAKOUT_PARAMS, UsSettings
from us_core.time_utils import et_to_utc_ts


def _generate_regular_15m_timestamps(cal: UsSessionCalendar, end_date: datetime.date, total_bars: int) -> List[int]:
    """Generate exact regular-session 15m timestamps (26/day or 14/early-close) ending on end_date."""
    days: List[datetime.date] = []
    cur = end_date
    while len(days) * 14 < total_bars + 60:
        if cal.is_trading_day(cur):
            days.append(cur)
        cur -= datetime.timedelta(days=1)
    days.reverse()

    stamps: List[int] = []
    for d in days:
        close_h, close_m = cal.get_regular_close_hm(d)
        t = int(et_to_utc_ts(d.year, d.month, d.day, 9, 30, 0))
        end_t = int(et_to_utc_ts(d.year, d.month, d.day, close_h, close_m, 0))
        while t < end_t:
            stamps.append(t)
            t += 900
    return stamps[-total_bars:]


class TestUsStrategiesAndWaveFilter(unittest.TestCase):
    def setUp(self) -> None:
        self.cal = UsSessionCalendar()
        self.clock = SimulatedClock(initial_epoch=et_to_utc_ts(2026, 11, 16, 14, 0, 0))

    def test_us_breakout_params_26_bars_per_day_calibration(self) -> None:
        self.assertEqual(US_BREAKOUT_PARAMS.bars_per_day_15m, 26)
        self.assertEqual(US_BREAKOUT_PARAMS.vwap_window, 520)
        self.assertEqual(US_BREAKOUT_PARAMS.ma_short_window, 130)
        self.assertEqual(US_BREAKOUT_PARAMS.ma_mid_window, 520)
        self.assertEqual(US_BREAKOUT_PARAMS.ma_long_window, 1560)
        self.assertAlmostEqual(US_BREAKOUT_PARAMS.btx_drawdown_ratio, 0.88, places=4)

    def test_brk_entry_and_btx_brk_088_drawdown_exit(self) -> None:
        strat = StockUsStrategy(calendar=self.cal, clock=self.clock, enable_mfo=False, enable_masb=False, enable_morphology=False)
        # Build 1620 regular 15m bars (> 1560 MA_LONG):
        # First 1000 bars at $110, next 615 bars at $95 (so MA1560 ~$104 > VWAP520 ~$95),
        # then ignite FIR above VWAP520 ($95 -> $98), and finally cross FIR above MA1560 ($108)!
        stamps = _generate_regular_15m_timestamps(self.cal, datetime.date(2026, 11, 16), 1625)
        # Pick last bar inside 09:30~15:30 ET entry window
        while not strat.is_entry_window_open(float(stamps[-1]), symbol="NVDA"):
            stamps.pop()

        n = len(stamps)
        bars: List[CandleBar] = []
        for i, ts in enumerate(stamps):
            td = self.cal.get_trading_day(float(ts), symbol="NVDA").isoformat()
            if i < n - 620:
                px = 110.0
                vol = 100_000.0
            elif i < n - 8:
                px = 95.0
                vol = 500_000.0  # Heavy low-price volume keeps VWAP520 < MA1560 during breakout
            elif i < n - 2:
                px = 98.5        # Ignites FIR > VWAP520 while VWAP520 < MA1560
                vol = 20_000.0
            elif i == n - 2:
                px = 101.0       # Keeps FIR (~99.6) below MA1560 (~104.1)
                vol = 10_000.0
            else:
                px = 132.0       # Breakout bar pushes 2-stage FIR (~108.5) cleanly above MA1560 (~104.1)!
                vol = 10_000.0
            bars.append(
                CandleBar(
                    symbol="NVDA",
                    timeframe="15m",
                    timestamp=ts,
                    trading_date=td,
                    open=px,
                    high=px + 0.5,
                    low=px - 0.5,
                    close=px,
                    volume=vol,
                )
            )

        strat.warmup_15m_bars("NVDA", bars[:-1])
        sig = strat.on_15m_bar_closed(bars[-1], open_position=None)
        self.assertIsNotNone(sig, "Expected BRK entry signal on MA1560 breakout")
        assert sig is not None
        self.assertEqual(sig.entry_type, "BRK")
        self.assertEqual(sig.action, "OPEN")

        # Now test BTX_BRK exit at US 0.88 drawdown ratio (-12% from peak $200 -> $176.00)
        next_day_t1 = int(et_to_utc_ts(2026, 11, 17, 9, 45, 0))
        next_day_t2 = int(et_to_utc_ts(2026, 11, 17, 10, 0, 0))
        next_day_t3 = int(et_to_utc_ts(2026, 11, 17, 10, 15, 0))

        # Peak bar at $200
        peak_bar = CandleBar(
            symbol="NVDA",
            timeframe="15m",
            timestamp=next_day_t1,
            trading_date="2026-11-17",
            open=195.0,
            high=200.0,
            low=194.0,
            close=199.0,
            volume=50_000.0,
        )
        open_pos = {
            "symbol": "NVDA",
            "entry_type": "BRK",
            "stop_loss_price": 100.0,
            "max_high_since_entry": 200.0,
        }
        self.assertIsNone(strat.on_15m_bar_closed(peak_bar, open_position=open_pos))

        # 10.5% pullback ($179.0 > $200 * 0.88 = $176.0, and high > VWAP) -> MUST HOLD (unlike 0.90!)
        hold_bar = CandleBar(
            symbol="NVDA",
            timeframe="15m",
            timestamp=next_day_t2,
            trading_date="2026-11-17",
            open=180.0,
            high=182.0,
            low=178.0,
            close=179.0,
            volume=50_000.0,
        )
        self.assertIsNone(strat.on_15m_bar_closed(hold_bar, open_position=open_pos))

        # 13% pullback ($174.0 < $200 * 0.88 = $176.0) -> MUST EXIT via BTX_BRK!
        exit_bar = CandleBar(
            symbol="NVDA",
            timeframe="15m",
            timestamp=next_day_t3,
            trading_date="2026-11-17",
            open=177.0,
            high=178.0,
            low=173.5,
            close=174.0,
            volume=50_000.0,
        )
        exit_sig = strat.on_15m_bar_closed(exit_bar, open_position=open_pos)
        self.assertIsNotNone(exit_sig)
        assert exit_sig is not None
        self.assertEqual(exit_sig.action, "CLOSE")
        self.assertEqual(exit_sig.entry_subtype, "BTX_BRK")

    def test_mfo_fanout_entry_on_26_bars_per_day_windows(self) -> None:
        strat = StockUsStrategy(calendar=self.cal, clock=self.clock, enable_brk=False, enable_masb=False, enable_morphology=False)
        stamps = _generate_regular_15m_timestamps(self.cal, datetime.date(2026, 11, 16), 1720)
        while not strat.is_entry_window_open(float(stamps[-1]), symbol="AAPL"):
            stamps.pop()

        n = len(stamps)
        bars: List[CandleBar] = []
        for i, ts in enumerate(stamps):
            td = self.cal.get_trading_day(float(ts), symbol="AAPL").isoformat()
            # Steady uptrend creates VWAP520 > MA520 > MA1560 with positive 78-bar slopes
            #Note: VWAP520 over 520 bars with linear +0.05/bar sits ~8.6 below current base_px
            base_px = 150.0 + 0.05 * i
            if n - 6 <= i <= n - 2:
                # Pulls FIR just below VWAP520 (base_px - 8.6) while keeping VWAP520 > MA520 > MA1560
                px = base_px - 10.5
                vol = 5_000.0
            elif i == n - 1:
                # Surge crosses 2-stage FIR back above VWAP520!
                px = base_px + 6.0
                vol = 150_000.0
            else:
                px = base_px
                vol = 120_000.0
            bars.append(
                CandleBar(
                    symbol="AAPL",
                    timeframe="15m",
                    timestamp=ts,
                    trading_date=td,
                    open=round(px, 2),
                    high=round(px + 0.5, 2),
                    low=round(px - 0.5, 2),
                    close=round(px, 2),
                    volume=vol,
                )
            )

        strat.warmup_15m_bars("AAPL", bars[:-1])
        sig = strat.on_15m_bar_closed(bars[-1], open_position=None)
        self.assertIsNotNone(sig, "Expected MFO entry on bullish fanout VWAP520 > MA520 > MA1560")
        assert sig is not None
        self.assertEqual(sig.entry_type, "MFO")

    def test_treasury_etf_bmr_entry_and_early_close_entry_gate(self) -> None:
        strat = StockUsStrategy(calendar=self.cal, clock=self.clock)

        # Verify early-close entry window on Black Friday (2026-11-27, 13:00 ET close) and Xmas Eve (2026-12-24)
        for early_d in (datetime.date(2026, 11, 27), datetime.date(2026, 12, 24)):
            ts_1230 = et_to_utc_ts(early_d.year, early_d.month, early_d.day, 12, 30, 0)
            ts_1245 = et_to_utc_ts(early_d.year, early_d.month, early_d.day, 12, 45, 0)
            self.assertTrue(strat.is_entry_window_open(ts_1230, symbol="TLT"))
            self.assertFalse(strat.is_entry_window_open(ts_1245, symbol="TLT"))

        # Warm up oversold daily TLT bars (gentle downtrend without breaking < 0.96 of prior base low)
        daily_bars: List[CandleBar] = []
        d_cur = datetime.date(2026, 8, 3)
        idx = 0
        while d_cur <= datetime.date(2026, 11, 13):
            if self.cal.is_trading_day(d_cur):
                px = 100.0 - 0.08 * idx if idx < 55 else 95.6 - 0.12 * (idx - 55)
                ts_d = int(datetime.datetime(d_cur.year, d_cur.month, d_cur.day, 0, 0, 0, tzinfo=TZ_UTC).timestamp())
                daily_bars.append(
                    CandleBar(
                        symbol="TLT",
                        timeframe="1d",
                        timestamp=ts_d,
                        trading_date=d_cur.isoformat(),
                        open=round(px + 0.1, 2),
                        high=round(px + 0.2, 2),
                        low=round(px - 0.1, 2),
                        close=round(px, 2),
                        volume=8_000_000.0,
                    )
                )
                idx += 1
            d_cur += datetime.timedelta(days=1)

        strat.warmup_daily_bars("TLT", daily_bars)
        tlt_15m: List[CandleBar] = []
        prices = [93.2, 93.1, 93.0, 93.0, 93.1, 93.3, 93.6, 94.0]
        for k, px in enumerate(prices):
            ts_k = int(et_to_utc_ts(2026, 11, 16, 9, 30, 0)) + k * 900
            tlt_15m.append(
                CandleBar(
                    symbol="TLT",
                    timeframe="15m",
                    timestamp=ts_k,
                    trading_date="2026-11-16",
                    open=px - 0.05,
                    high=px + 0.10,
                    low=px - 0.10,
                    close=px,
                    volume=250_000.0,
                )
            )
        strat.warmup_15m_bars("TLT", tlt_15m[:-1])
        bmr_sig = strat.on_15m_bar_closed(tlt_15m[-1], open_position=None)
        self.assertIsNotNone(bmr_sig, "Expected BMR signal on oversold US Treasury ETF TLT")
        assert bmr_sig is not None
        self.assertEqual(bmr_sig.entry_type, "BMR")

    def test_es_futures_wave_filter_weekend_continuity_and_0915_0945_freeze(self) -> None:
        wf = UsFuturesWaveFilter(symbol="ES=F", timeframe="15m", calendar=self.cal)

        # Feed bullish Wave 1 -> Wave 2 (50% Fib) -> Wave 3 impulse ending on Friday's final 16:45 ET Globex bar
        fri_base = int(et_to_utc_ts(2026, 11, 13, 16, 0, 0))
        wave_legs = [
            (5900.0, 5905.0, 5895.0, 5902.0),  # 16:00 ET Valley 5895
            (5902.0, 5955.0, 5901.0, 5950.0),  # 16:15 ET Wave 1 Peak 5955 (+60 pts, >0.40%)
            (5950.0, 5951.0, 5925.0, 5927.0),  # 16:30 ET Wave 2 Valley 5925 (50% Fib retracement!)
            (5927.0, 5995.0, 5926.0, 5990.0),  # 16:45 ET Wave 3 Peak 5995 -> evaluates BULLISH IMPULSE_WAVE_3!
        ]
        for k, (o, h, l, c) in enumerate(wave_legs):
            ts = fri_base + k * 900
            wf.on_macro_candle(
                CandleBar(
                    symbol="ES=F",
                    timeframe="15m",
                    timestamp=ts,
                    trading_date="2026-11-13",
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=25_000.0,
                )
            )

        self.assertEqual(wf.fsm.last_regime.trend, "BULLISH")
        self.assertEqual(wf.fsm.last_regime.wave_stage, "IMPULSE_WAVE_3")

        # Across the 49-hour weekend close (Fri 16:45 ET -> Sun 18:00 ET Globex open): regime MUST NOT reset!
        sun_open_ts = int(et_to_utc_ts(2026, 11, 15, 18, 0, 0))
        snap_sun = wf.on_macro_candle(
            CandleBar(
                symbol="ES=F",
                timeframe="15m",
                timestamp=sun_open_ts,
                trading_date="2026-11-16",
                open=5988.0,
                high=5993.0,
                low=5985.0,
                close=5991.0,
                volume=12_000.0,
            )
        )
        self.assertEqual(snap_sun.trend, "BULLISH")
        self.assertEqual(snap_sun.wave_stage, "IMPULSE_WAVE_3")

        # Stream continuous overnight Globex 15m bars from Sun 18:15 ET through Mon 09:00 ET
        mon_0900 = int(et_to_utc_ts(2026, 11, 16, 9, 0, 0))
        cur_t = sun_open_ts + 900
        while cur_t <= mon_0900:
            wf.on_macro_candle(
                CandleBar(
                    symbol="ES=F",
                    timeframe="15m",
                    timestamp=cur_t,
                    trading_date="2026-11-16",
                    open=5990.0,
                    high=5994.0,
                    low=5988.0,
                    close=5992.0,
                    volume=8_000.0,
                )
            )
            cur_t += 900

        # During Monday 09:15~09:45 ET cash-open freeze window, a violent bearish crash wick at 09:30 ET
        # MUST NOT flip get_effective_regime(09:30 ET) away from the pre-09:15 BULLISH regime!
        mon_0915_freeze = int(et_to_utc_ts(2026, 11, 16, 9, 15, 0))
        wf.on_macro_candle(
            CandleBar(
                symbol="ES=F",
                timeframe="15m",
                timestamp=mon_0915_freeze,
                trading_date="2026-11-16",
                open=5991.0,
                high=5993.0,
                low=5989.0,
                close=5990.0,
                volume=15_000.0,
            )
        )

        mon_0930_freeze = int(et_to_utc_ts(2026, 11, 16, 9, 30, 0))
        self.assertTrue(wf.is_in_opening_freeze_window(mon_0930_freeze))
        frozen_snap = wf.on_macro_candle(
            CandleBar(
                symbol="ES=F",
                timeframe="15m",
                timestamp=mon_0930_freeze,
                trading_date="2026-11-16",
                open=5990.0,
                high=5991.0,
                low=5820.0,  # Huge opening-auction spike below prior valleys
                close=5825.0,
                volume=90_000.0,
            )
        )
        self.assertEqual(frozen_snap.trend, "BULLISH")
        self.assertIn("OPENING_FREEZE", frozen_snap.description)

        # At 09:45 ET freeze window lifts -> live bearish breakdown is now visible!
        mon_0945_unfrozen = int(et_to_utc_ts(2026, 11, 16, 9, 45, 0))
        self.assertFalse(wf.is_in_opening_freeze_window(mon_0945_unfrozen))
        live_after_freeze = wf.get_effective_regime(mon_0945_unfrozen)
        self.assertEqual(live_after_freeze.trend, "BEARISH")

    def test_us_trading_engine_sandbox_warmup_and_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = UsSettings(
                broker_provider="sandbox",
                alpaca_api_key="",
                alpaca_secret_key="",
                alpaca_paper=True,
                ibkr_host="127.0.0.1",
                ibkr_port=7497,
                ibkr_client_id=10,
                schwab_app_key="",
                schwab_app_secret="",
                target_stocks=("NVDA", "TLT"),
                macro_symbol="ES=F",
                initial_capital=100_000.0,
                max_position_pct=0.20,
                risk_per_trade_pct=0.01,
                daily_loss_limit_pct=0.02,
                chart_host="127.0.0.1",
                chart_port=8081,
                discord_webhook_url="",
                paper_db_path=Path(tmp) / "engine_paper.db",
                enable_brk=True,
                enable_mfo=True,
                enable_masb=True,
                enable_morphology=True,
                enable_bond_bmr=True,
                enable_macro_wave_filter=True,
            )
            engine = UsTradingEngine(settings=settings, calendar=self.cal, clock=self.clock)

            async def _run() -> dict:
                counts = await engine.warmup()
                await engine.step_once()
                return counts

            counts = asyncio.run(_run())
            self.assertGreater(counts["ES=F"], 100)
            self.assertGreater(counts["NVDA"], 1560)


if __name__ == "__main__":
    unittest.main()
