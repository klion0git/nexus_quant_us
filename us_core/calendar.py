"""US NYSE & CME Globex dual-asset session calendar implementing SessionCalendarProtocol."""
from __future__ import annotations

import datetime
import math
from functools import lru_cache
from typing import Any, FrozenSet, Optional, Set, Tuple

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import SessionCalendarProtocol
from us_core.config import TZ_US, TZ_UTC
from us_core.symbol import is_us_futures_symbol


# Historical / special one-off NYSE full-day closures (hurricanes, national days of mourning)
_SPECIAL_NYSE_CLOSURES: FrozenSet[datetime.date] = frozenset(
    {
        datetime.date(2012, 10, 29),  # Hurricane Sandy
        datetime.date(2012, 10, 30),  # Hurricane Sandy
        datetime.date(2018, 12, 5),   # President George H.W. Bush Day of Mourning
        datetime.date(2025, 1, 9),    # President Jimmy Carter Day of Mourning
    }
)


def compute_easter_sunday(year: int) -> datetime.date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher) for Easter Sunday."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l_val = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_val) // 451
    month = (h + l_val - 7 * m + 114) // 31
    day = ((h + l_val - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """Return the n-th (1-based) occurrence of weekday (0=Mon..6=Sun) in (year, month)."""
    first = datetime.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return datetime.date(year, month, 1 + offset + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> datetime.date:
    """Return the last occurrence of weekday (0=Mon..6=Sun) in (year, month)."""
    if month == 12:
        next_first = datetime.date(year + 1, 1, 1)
    else:
        next_first = datetime.date(year, month + 1, 1)
    last_day = next_first - datetime.timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - datetime.timedelta(days=offset)


def _observed_fixed_holiday(year: int, month: int, day: int, *, observe_saturday_on_friday: bool = True) -> Optional[datetime.date]:
    actual = datetime.date(year, month, day)
    if actual.weekday() == 5:  # Saturday
        return actual - datetime.timedelta(days=1) if observe_saturday_on_friday else None
    if actual.weekday() == 6:  # Sunday
        return actual + datetime.timedelta(days=1)
    return actual


@lru_cache(maxsize=128)
def get_nyse_holidays_for_year(year: int) -> FrozenSet[datetime.date]:
    """Compute canonical NYSE holidays for a given Gregorian year."""
    holidays: Set[datetime.date] = set()

    # 1. New Year's Day (NYSE Rule 7.2: Sunday observed Monday Jan 2; Saturday NOT observed Dec 31)
    nyd = _observed_fixed_holiday(year, 1, 1, observe_saturday_on_friday=False)
    if nyd is not None:
        holidays.add(nyd)

    # 2. Martin Luther King, Jr. Day (3rd Monday in January, observed by NYSE since 1998)
    if year >= 1998:
        holidays.add(_nth_weekday_of_month(year, 1, 0, 3))

    # 3. Washington's Birthday / Presidents' Day (3rd Monday in February)
    holidays.add(_nth_weekday_of_month(year, 2, 0, 3))

    # 4. Good Friday (2 days before Easter Sunday)
    holidays.add(compute_easter_sunday(year) - datetime.timedelta(days=2))

    # 5. Memorial Day (Last Monday in May)
    holidays.add(_last_weekday_of_month(year, 5, 0))

    # 6. Juneteenth National Independence Day (June 19, observed by NYSE since 2022)
    if year >= 2022:
        juneteenth = _observed_fixed_holiday(year, 6, 19, observe_saturday_on_friday=True)
        if juneteenth is not None:
            holidays.add(juneteenth)

    # 7. Independence Day (July 4)
    july4 = _observed_fixed_holiday(year, 7, 4, observe_saturday_on_friday=True)
    if july4 is not None:
        holidays.add(july4)

    # 8. Labor Day (1st Monday in September)
    holidays.add(_nth_weekday_of_month(year, 9, 0, 1))

    # 9. Thanksgiving Day (4th Thursday in November)
    holidays.add(_nth_weekday_of_month(year, 11, 3, 4))

    # 10. Christmas Day (December 25)
    xmas = _observed_fixed_holiday(year, 12, 25, observe_saturday_on_friday=True)
    if xmas is not None:
        holidays.add(xmas)

    for sp in _SPECIAL_NYSE_CLOSURES:
        if sp.year == year:
            holidays.add(sp)

    return frozenset(holidays)


@lru_cache(maxsize=128)
def get_nyse_early_close_days_for_year(year: int) -> FrozenSet[datetime.date]:
    """Compute NYSE 13:00 ET early-close sessions for a given Gregorian year.

    Standard NYSE early closes (13:00 ET):
    1. Day after Thanksgiving (Black Friday — Friday after 4th Thursday in November).
    2. July 3rd when it is a trading weekday (i.e., Mon-Thu before Tue-Fri July 4th).
    3. Christmas Eve (December 24th) when it is a trading weekday.
    """
    holidays = get_nyse_holidays_for_year(year)
    early: Set[datetime.date] = set()

    # Black Friday
    thanksgiving = _nth_weekday_of_month(year, 11, 3, 4)
    black_friday = thanksgiving + datetime.timedelta(days=1)
    if black_friday.weekday() < 5 and black_friday not in holidays:
        early.add(black_friday)

    # July 3rd (when weekday and not observed July 4th holiday)
    july_3 = datetime.date(year, 7, 3)
    if july_3.weekday() < 5 and july_3 not in holidays:
        early.add(july_3)

    # December 24th (when weekday and not observed Christmas holiday)
    dec_24 = datetime.date(year, 12, 24)
    if dec_24.weekday() < 5 and dec_24 not in holidays:
        early.add(dec_24)

    return frozenset(early)


class UsSessionCalendar(SessionCalendarProtocol):
    """Dual-asset US market session calendar (NYSE Equities + CME Globex US Index Futures).

    - Timezone: America/New_York (handles EDT UTC-4 and EST UTC-5 automatically).
    - US Equities (is_futures=False):
        * Pre-market: 04:00 ~ 09:30 ET
        * Regular:    09:30 ~ 16:00 ET (26 bars of 15m: 09:30..15:45), or
                      09:30 ~ 13:00 ET (14 bars of 15m: 09:30..12:45) on NYSE early-close days
        * Post-market: 16:00 (or 13:00) ~ 20:00 ET (stays on the same America/New_York trading_date!)
    - US Index Futures (ES=F, NQ=F, etc., is_futures=True):
        * CME Globex 23-hour cycle: opens 18:00 ET (Sunday 18:00 ET -> Friday 17:00 ET)
        * Daily maintenance break: 17:00 ~ 18:00 ET (Mon-Thu)
        * Any session bar >= 18:00 ET rolls forward to the next business day (T+1).
    """

    def __init__(self, tz: Optional[datetime.tzinfo] = None) -> None:
        self._tz: datetime.tzinfo = tz if tz is not None else TZ_US

    @property
    def timezone(self) -> datetime.tzinfo:
        return self._tz

    def to_et_datetime(self, utc_ts: float) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(float(utc_ts), tz=TZ_UTC).astimezone(self._tz)

    def is_nyse_holiday(self, d: datetime.date) -> bool:
        return d in get_nyse_holidays_for_year(d.year)

    def is_early_close_day(self, d: datetime.date) -> bool:
        return d in get_nyse_early_close_days_for_year(d.year)

    def is_trading_day(self, d: datetime.date, symbol: str = "") -> bool:
        if d.weekday() >= 5:
            return False
        if self.is_nyse_holiday(d):
            return False
        return True

    def get_regular_close_hm(self, d: datetime.date) -> Tuple[int, int]:
        """Return (hour, minute) of NYSE regular close in ET: (13, 0) on early close, else (16, 0)."""
        return (13, 0) if self.is_early_close_day(d) else (16, 0)

    def get_last_entry_cutoff_hm(self, d: datetime.date) -> Tuple[int, int]:
        """Return (hour, minute) of last 15m entry bar label in ET: (12, 30) on early close, else (15, 30)."""
        return (12, 30) if self.is_early_close_day(d) else (15, 30)

    def expected_regular_15m_bars(self, d: datetime.date) -> int:
        if not self.is_trading_day(d):
            return 0
        return 14 if self.is_early_close_day(d) else 26

    def roll_business_day(self, d: datetime.date, step_days: int, symbol: str = "") -> datetime.date:
        cur = d
        step = int(step_days)
        if step == 0:
            while not self.is_trading_day(cur, symbol=symbol):
                cur += datetime.timedelta(days=1)
            return cur
        direction = 1 if step > 0 else -1
        remaining = abs(step)
        while remaining > 0:
            cur += datetime.timedelta(days=direction)
            if self.is_trading_day(cur, symbol=symbol):
                remaining -= 1
        return cur

    def _coerce_to_trading_date(self, ts_or_date: Any, symbol: str = "") -> datetime.date:
        if isinstance(ts_or_date, datetime.datetime):
            return self.get_trading_day(ts_or_date.timestamp(), symbol=symbol)
        if isinstance(ts_or_date, datetime.date):
            return ts_or_date
        if isinstance(ts_or_date, str):
            s = ts_or_date.strip()
            if len(s) >= 10 and s[4] == "-" and s[7] == "-":
                return datetime.date.fromisoformat(s[:10])
            return self.get_trading_day(float(s), symbol=symbol)
        return self.get_trading_day(float(ts_or_date), symbol=symbol)

    def trading_days_between(self, older_ts_or_date: Any, newer_ts_or_date: Any, symbol: str = "") -> int:
        d1 = self._coerce_to_trading_date(older_ts_or_date, symbol=symbol)
        d2 = self._coerce_to_trading_date(newer_ts_or_date, symbol=symbol)
        if d1 == d2:
            return 0
        sign = 1 if d2 > d1 else -1
        start, end = (d1, d2) if d2 > d1 else (d2, d1)
        count = 0
        cur = start
        while cur < end:
            cur += datetime.timedelta(days=1)
            if self.is_trading_day(cur, symbol=symbol):
                count += 1
        return sign * count

    def get_trading_day(self, utc_ts: float, symbol: str = "") -> datetime.date:
        dt_et = self.to_et_datetime(utc_ts)
        d = dt_et.date()
        if is_us_futures_symbol(symbol):
            # CME Globex session opens at 18:00 ET for next business day (T+1)
            if dt_et.hour >= 18:
                return self.roll_business_day(d, 1, symbol=symbol)
            # Friday >= 17:00 ET, Saturday, Sunday < 18:00 ET, or holiday overnight (< 18:00 ET) -> next business day
            if not self.is_trading_day(d, symbol=symbol) or (d.weekday() == 4 and dt_et.hour >= 17):
                return self.roll_business_day(d, 1, symbol=symbol)
            return d

        # US Equities / ETFs: strictly follow America/New_York calendar date (including 16:00~20:00 ET post-market!)
        return d

    def get_session_type(self, utc_ts: float, symbol: str = "") -> str:
        dt_et = self.to_et_datetime(utc_ts)
        d = dt_et.date()
        wd = d.weekday()
        minutes = dt_et.hour * 60 + dt_et.minute

        if is_us_futures_symbol(symbol):
            # Saturday is closed all day
            if wd == 5:
                return "CLOSED"
            # Sunday opens at 18:00 ET
            if wd == 6:
                return "GLOBEX" if minutes >= 18 * 60 else "CLOSED"
            # Friday closes at 17:00 ET for the weekend
            if wd == 4 and minutes >= 17 * 60:
                return "CLOSED"
            # Daily CME maintenance break 17:00 ~ 18:00 ET (Mon-Thu)
            if 17 * 60 <= minutes < 18 * 60:
                return "MAINTENANCE"
            # Full holiday check for RTH vs overnight electronic
            if self.is_trading_day(d):
                close_h, close_m = self.get_regular_close_hm(d)
                if (9 * 60 + 30) <= minutes < (close_h * 60 + close_m):
                    return "REGULAR"
            return "GLOBEX"

        # US Equities / ETFs
        if not self.is_trading_day(d):
            return "CLOSED"
        close_h, close_m = self.get_regular_close_hm(d)
        close_min = close_h * 60 + close_m
        if (4 * 60) <= minutes < (9 * 60 + 30):
            return "PRE_MARKET"
        if (9 * 60 + 30) <= minutes < close_min:
            return "REGULAR"
        if close_min <= minutes < (20 * 60):
            return "POST_MARKET"
        return "CLOSED"

    def _et_anchor_epoch(self, d: datetime.date, hour: int, minute: int = 0) -> int:
        dt_et = datetime.datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=self._tz)
        return int(dt_et.timestamp())

    def _resolve_session_window_bounds(self, utc_ts: float, symbol: str = "") -> Optional[Tuple[int, int]]:
        """Return (session_start_utc, session_end_utc) for active session containing utc_ts, or None."""
        dt_et = self.to_et_datetime(utc_ts)
        d = dt_et.date()
        wd = d.weekday()
        minutes = dt_et.hour * 60 + dt_et.minute

        if is_us_futures_symbol(symbol):
            if wd == 5:
                return None
            if wd == 6 and minutes < 18 * 60:
                return None
            if wd == 4 and minutes >= 17 * 60:
                return None
            if 17 * 60 <= minutes < 18 * 60:
                return None
            if minutes >= 18 * 60:
                start_date = d
                end_date = d + datetime.timedelta(days=1)
            else:
                start_date = d - datetime.timedelta(days=1)
                end_date = d
            return (self._et_anchor_epoch(start_date, 18, 0), self._et_anchor_epoch(end_date, 17, 0))

        if not self.is_trading_day(d):
            return None
        close_h, close_m = self.get_regular_close_hm(d)
        close_min = close_h * 60 + close_m
        if (4 * 60) <= minutes < (9 * 60 + 30):
            return (self._et_anchor_epoch(d, 4, 0), self._et_anchor_epoch(d, 9, 30))
        if (9 * 60 + 30) <= minutes < close_min:
            return (self._et_anchor_epoch(d, 9, 30), self._et_anchor_epoch(d, close_h, close_m))
        if close_min <= minutes < (20 * 60):
            return (self._et_anchor_epoch(d, close_h, close_m), self._et_anchor_epoch(d, 20, 0))
        return None

    def get_session_bar_label(self, utc_ts: float, window_seconds: int, symbol: str = "") -> int:
        win = max(1, int(window_seconds))
        ts_f = float(utc_ts)

        # Calendar-level windows (>= 1d) anchor at 00:00 UTC of the trading date
        if win >= 86400:
            td = self.get_trading_day(ts_f, symbol=symbol)
            return int(datetime.datetime(td.year, td.month, td.day, 0, 0, 0, tzinfo=TZ_UTC).timestamp())

        # Sub-30m windows (e.g. 1m=60, 5m=300, 15m=900) align naturally with both :00 and :30 ET boundaries
        if (1800 % win) == 0:
            return int(math.floor(ts_f / float(win))) * win

        # Higher intraday windows (e.g. 1h=3600, 4h=14400) align to the active session open (09:30 ET for equities, 18:00 ET for CME)
        bounds = self._resolve_session_window_bounds(ts_f, symbol=symbol)
        if bounds is not None:
            sess_start, _ = bounds
            elapsed = max(0, int(math.floor(ts_f)) - sess_start)
            return sess_start + (elapsed // win) * win

        return int(math.floor(ts_f / float(win))) * win

    def get_bar_close_ts(self, bar_label_ts: int, window_seconds: int, symbol: str = "") -> int:
        win = max(1, int(window_seconds))
        lbl = int(bar_label_ts)
        raw_close = lbl + win
        if win >= 86400:
            return raw_close
        bounds = self._resolve_session_window_bounds(float(lbl), symbol=symbol)
        if bounds is not None:
            _, sess_end = bounds
            return min(raw_close, sess_end)
        return raw_close

    def get_session_active_seconds(self, start_ts: float, end_ts: float, symbol: str = "") -> float:
        """Compute active market trading seconds in [start_ts, end_ts] excluding closures & maintenance."""
        s = float(start_ts)
        e = float(end_ts)
        if e <= s:
            return 0.0

        is_fut = is_us_futures_symbol(symbol)
        dt_start = self.to_et_datetime(s)
        dt_end = self.to_et_datetime(e)

        # Enumerate active intervals day-by-day across [dt_start.date() - 1, dt_end.date() + 1]
        cur_date = dt_start.date() - datetime.timedelta(days=1)
        last_date = dt_end.date() + datetime.timedelta(days=1)
        total_active = 0.0

        while cur_date <= last_date:
            wd = cur_date.weekday()
            if is_fut:
                # CME Globex overnight segment starting cur_date 18:00 ET -> (cur_date + 1d) 17:00 ET
                # Valid start days: Sunday (6), Monday (0), Tuesday (1), Wednesday (2), Thursday (3)
                if wd in (6, 0, 1, 2, 3):
                    seg_start = float(self._et_anchor_epoch(cur_date, 18, 0))
                    seg_end = float(self._et_anchor_epoch(cur_date + datetime.timedelta(days=1), 17, 0))
                    ov_s = max(s, seg_start)
                    ov_e = min(e, seg_end)
                    if ov_e > ov_s:
                        total_active += ov_e - ov_s
            else:
                if self.is_trading_day(cur_date):
                    close_h, close_m = self.get_regular_close_hm(cur_date)
                    seg_start = float(self._et_anchor_epoch(cur_date, 9, 30))
                    seg_end = float(self._et_anchor_epoch(cur_date, close_h, close_m))
                    ov_s = max(s, seg_start)
                    ov_e = min(e, seg_end)
                    if ov_e > ov_s:
                        total_active += ov_e - ov_s
            cur_date += datetime.timedelta(days=1)

        return round(total_active, 3)


DEFAULT_US_CALENDAR = UsSessionCalendar()
