"""Tests for US session calendar, America/New_York EDT/EST DST transitions, early closes, and CME Globex."""
from __future__ import annotations

import datetime
import unittest
from zoneinfo import ZoneInfo

from nexus_quant_core.contracts import SessionCalendarProtocol
from us_core.calendar import (
    UsSessionCalendar,
    get_nyse_early_close_days_for_year,
    get_nyse_holidays_for_year,
)
from us_core.config import TZ_UTC, UsEasternFallbackTz
from us_core.symbol import UsSymbol, is_us_futures_symbol, normalize_us_symbol
from us_core.time_utils import et_to_utc_ts, utc_ts_to_et


class TestUsSymbolAndDstCalendar(unittest.TestCase):
    def setUp(self) -> None:
        self.cal = UsSessionCalendar()
        self.fallback_tz = UsEasternFallbackTz()
        self.zoneinfo_tz = ZoneInfo("America/New_York")

    def test_symbol_normalization_and_asset_flags(self) -> None:
        nvda = UsSymbol.parse("NASDAQ:NVDA")
        self.assertEqual(nvda.code, "NVDA")
        self.assertEqual(nvda.exchange, "NASDAQ")
        self.assertTrue(nvda.is_equity)
        self.assertFalse(nvda.is_futures)
        self.assertFalse(nvda.is_bond)
        self.assertFalse(nvda.is_etf)

        brk1 = UsSymbol.parse("NYSE:BRK.B")
        brk2 = UsSymbol.parse("BRK-B")
        self.assertEqual(brk1.code, "BRK-B")
        self.assertEqual(brk2.code, "BRK-B")
        self.assertEqual(normalize_us_symbol("nyse:brk.b"), "BRK-B")

        for fut_sym in ("ES=F", "NQ=F", "YM=F", "RTY=F", "MES=F", "MNQ=F", "/ES", "/NQ"):
            parsed = UsSymbol.parse(fut_sym)
            self.assertTrue(parsed.is_futures, fut_sym)
            self.assertTrue(is_us_futures_symbol(fut_sym))

        for bond_sym in ("TLT", "IEF", "SHY", "AGG", "BND"):
            parsed = UsSymbol.parse(bond_sym)
            self.assertTrue(parsed.is_bond, bond_sym)
            self.assertTrue(parsed.is_etf, bond_sym)
            self.assertFalse(parsed.is_futures, bond_sym)

        for etf_sym in ("SPY", "QQQ", "IWM"):
            parsed = UsSymbol.parse(etf_sym)
            self.assertTrue(parsed.is_etf, etf_sym)
            self.assertFalse(parsed.is_bond, etf_sym)

    def test_protocol_conformance(self) -> None:
        self.assertIsInstance(self.cal, SessionCalendarProtocol)

    def test_march_edt_vs_november_est_open_timestamps(self) -> None:
        # March 16, 2026 is in EDT (UTC-4): 09:30 ET == 13:30 UTC
        ts_mar_open = et_to_utc_ts(2026, 3, 16, 9, 30, 0)
        dt_utc_mar = datetime.datetime.fromtimestamp(ts_mar_open, tz=TZ_UTC)
        self.assertEqual((dt_utc_mar.hour, dt_utc_mar.minute), (13, 30))
        dt_et_mar = utc_ts_to_et(ts_mar_open)
        self.assertEqual(dt_et_mar.utcoffset(), datetime.timedelta(hours=-4))
        self.assertEqual(self.cal.get_session_type(ts_mar_open, symbol="NVDA"), "REGULAR")

        # November 16, 2026 is in EST (UTC-5): 09:30 ET == 14:30 UTC
        ts_nov_open = et_to_utc_ts(2026, 11, 16, 9, 30, 0)
        dt_utc_nov = datetime.datetime.fromtimestamp(ts_nov_open, tz=TZ_UTC)
        self.assertEqual((dt_utc_nov.hour, dt_utc_nov.minute), (14, 30))
        dt_et_nov = utc_ts_to_et(ts_nov_open)
        self.assertEqual(dt_et_nov.utcoffset(), datetime.timedelta(hours=-5))
        self.assertEqual(self.cal.get_session_type(ts_nov_open, symbol="NVDA"), "REGULAR")

    def test_fallback_tz_matches_zoneinfo_across_dst_boundaries(self) -> None:
        # Check Spring Forward (2026-03-08) and Fall Back (2026-11-01, including ambiguous 01:30 fold=0/1)
        sample_utc_points = [
            datetime.datetime(2026, 3, 8, 6, 30, tzinfo=TZ_UTC),   # 01:30 EST
            datetime.datetime(2026, 3, 8, 7, 30, tzinfo=TZ_UTC),   # 03:30 EDT
            datetime.datetime(2026, 7, 15, 13, 30, tzinfo=TZ_UTC), # 09:30 EDT
            datetime.datetime(2026, 11, 1, 5, 30, tzinfo=TZ_UTC),  # 01:30 EDT (fold=0)
            datetime.datetime(2026, 11, 1, 6, 30, tzinfo=TZ_UTC),  # 01:30 EST (fold=1)
            datetime.datetime(2026, 11, 1, 7, 30, tzinfo=TZ_UTC),  # 02:30 EST
            datetime.datetime(2026, 12, 15, 14, 30, tzinfo=TZ_UTC),# 09:30 EST
        ]
        for utc_dt in sample_utc_points:
            zi = utc_dt.astimezone(self.zoneinfo_tz)
            fb = utc_dt.astimezone(self.fallback_tz)
            self.assertEqual(zi.utcoffset(), fb.utcoffset(), f"Offset mismatch at {utc_dt}")
            self.assertEqual((zi.year, zi.month, zi.day, zi.hour, zi.minute), (fb.year, fb.month, fb.day, fb.hour, fb.minute))

    def test_post_market_1930_est_stays_on_same_ny_trading_date(self) -> None:
        # 2026-11-16 19:30 EST is 2026-11-17 00:30 UTC (next UTC calendar date!)
        ts_post = et_to_utc_ts(2026, 11, 16, 19, 30, 0)
        dt_utc = datetime.datetime.fromtimestamp(ts_post, tz=TZ_UTC)
        self.assertEqual(dt_utc.date(), datetime.date(2026, 11, 17))
        self.assertEqual((dt_utc.hour, dt_utc.minute), (0, 30))

        # Yet in America/New_York equity calendar it MUST still belong to 2026-11-16 POST_MARKET!
        self.assertEqual(self.cal.get_trading_day(ts_post, symbol="NVDA"), datetime.date(2026, 11, 16))
        self.assertEqual(self.cal.get_session_type(ts_post, symbol="NVDA"), "POST_MARKET")

    def test_nyse_holidays_and_early_close_days(self) -> None:
        hols_2026 = get_nyse_holidays_for_year(2026)
        # 2026 holidays: Jan 1, MLK Jan 19, Pres Feb 16, Good Friday Apr 3, Memorial May 25,
        # Juneteenth Jun 19, July 4 (Sat -> observed Fri Jul 3!), Labor Sep 7, Thanksgiving Nov 26, Xmas Dec 25
        self.assertIn(datetime.date(2026, 1, 1), hols_2026)
        self.assertIn(datetime.date(2026, 1, 19), hols_2026)
        self.assertIn(datetime.date(2026, 4, 3), hols_2026)
        self.assertIn(datetime.date(2026, 6, 19), hols_2026)
        self.assertIn(datetime.date(2026, 7, 3), hols_2026)  # July 4, 2026 is Saturday -> July 3 is full holiday!
        self.assertIn(datetime.date(2026, 11, 26), hols_2026)
        self.assertIn(datetime.date(2026, 12, 25), hols_2026)

        early_2026 = get_nyse_early_close_days_for_year(2026)
        # Black Friday Nov 27, 2026 & Christmas Eve Dec 24, 2026 are 13:00 ET early closes; Jul 3, 2026 is full holiday
        self.assertIn(datetime.date(2026, 11, 27), early_2026)
        self.assertIn(datetime.date(2026, 12, 24), early_2026)
        self.assertNotIn(datetime.date(2026, 7, 3), early_2026)

        # In 2025, July 4 was Friday -> July 3, 2025 (Thursday) WAS a 13:00 ET early close!
        early_2025 = get_nyse_early_close_days_for_year(2025)
        self.assertIn(datetime.date(2025, 7, 3), early_2025)

        # Bar counts & cutoff times
        self.assertEqual(self.cal.expected_regular_15m_bars(datetime.date(2026, 11, 16)), 26)
        self.assertEqual(self.cal.get_last_entry_cutoff_hm(datetime.date(2026, 11, 16)), (15, 30))
        self.assertEqual(self.cal.expected_regular_15m_bars(datetime.date(2026, 11, 27)), 14)
        self.assertEqual(self.cal.get_last_entry_cutoff_hm(datetime.date(2026, 11, 27)), (12, 30))
        self.assertEqual(self.cal.get_regular_close_hm(datetime.date(2026, 12, 24)), (13, 0))

        # At 13:15 ET on Black Friday, regular session is already over -> POST_MARKET
        ts_bf_1245 = et_to_utc_ts(2026, 11, 27, 12, 45, 0)
        ts_bf_1315 = et_to_utc_ts(2026, 11, 27, 13, 15, 0)
        self.assertEqual(self.cal.get_session_type(ts_bf_1245, symbol="NVDA"), "REGULAR")
        self.assertEqual(self.cal.get_session_type(ts_bf_1315, symbol="NVDA"), "POST_MARKET")

    def test_cme_globex_futures_sunday_open_and_maintenance_break(self) -> None:
        # Sunday Nov 15, 2026 at 17:30 EST -> CLOSED
        ts_sun_before = et_to_utc_ts(2026, 11, 15, 17, 30, 0)
        self.assertEqual(self.cal.get_session_type(ts_sun_before, symbol="ES=F"), "CLOSED")

        # Sunday Nov 15, 2026 at 18:00 EST -> GLOBEX open, rolls forward to Monday 2026-11-16 trading_date!
        ts_sun_open = et_to_utc_ts(2026, 11, 15, 18, 0, 0)
        self.assertEqual(self.cal.get_session_type(ts_sun_open, symbol="ES=F"), "GLOBEX")
        self.assertEqual(self.cal.get_trading_day(ts_sun_open, symbol="ES=F"), datetime.date(2026, 11, 16))

        # Monday Nov 16, 2026 at 17:30 EST -> Daily CME 17:00~18:00 ET MAINTENANCE break!
        ts_mon_maint = et_to_utc_ts(2026, 11, 16, 17, 30, 0)
        self.assertEqual(self.cal.get_session_type(ts_mon_maint, symbol="ES=F"), "MAINTENANCE")

        # Zero active seconds during 17:00~18:00 ET maintenance break
        ts_1700 = et_to_utc_ts(2026, 11, 16, 17, 0, 0)
        ts_1800 = et_to_utc_ts(2026, 11, 16, 18, 0, 0)
        self.assertEqual(self.cal.get_session_active_seconds(ts_1700, ts_1800, symbol="ES=F"), 0.0)

        # Zero active seconds across Friday 17:00 ET -> Sunday 18:00 ET weekend close
        ts_fri_1700 = et_to_utc_ts(2026, 11, 13, 17, 0, 0)
        self.assertEqual(self.cal.get_session_active_seconds(ts_fri_1700, ts_sun_open, symbol="ES=F"), 0.0)

        # Monday Nov 16, 2026 at 18:30 EST -> rolls forward to Tuesday 2026-11-17 trading_date!
        ts_mon_1830 = et_to_utc_ts(2026, 11, 16, 18, 30, 0)
        self.assertEqual(self.cal.get_trading_day(ts_mon_1830, symbol="ES=F"), datetime.date(2026, 11, 17))


if __name__ == "__main__":
    unittest.main()
