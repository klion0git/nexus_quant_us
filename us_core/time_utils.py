"""US Eastern (America/New_York) timestamp & formatting utilities."""
from __future__ import annotations

import datetime
from typing import Optional

from us_core.calendar import DEFAULT_US_CALENDAR, UsSessionCalendar
from us_core.clock import DEFAULT_CLOCK, ClockProtocol
from us_core.config import TZ_US, TZ_UTC


def utc_ts_to_et(utc_ts: float, tz: Optional[datetime.tzinfo] = None) -> datetime.datetime:
    """Convert UTC epoch seconds to timezone-aware America/New_York datetime."""
    target_tz = tz if tz is not None else TZ_US
    return datetime.datetime.fromtimestamp(float(utc_ts), tz=TZ_UTC).astimezone(target_tz)


def et_to_utc_ts(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    *,
    fold: int = 0,
    tz: Optional[datetime.tzinfo] = None,
) -> float:
    """Construct an America/New_York wall-clock datetime and return its UTC epoch seconds."""
    target_tz = tz if tz is not None else TZ_US
    dt_et = datetime.datetime(year, month, day, hour, minute, second, tzinfo=target_tz, fold=fold)
    return float(dt_et.timestamp())


def format_et_iso(utc_ts: float, tz: Optional[datetime.tzinfo] = None) -> str:
    """Format UTC epoch seconds as 'YYYY-MM-DD HH:MM:SS EDT/EST'."""
    dt_et = utc_ts_to_et(utc_ts, tz=tz)
    return dt_et.strftime("%Y-%m-%d %H:%M:%S %Z")


def trading_date_str(
    utc_ts: float,
    *,
    symbol: str = "",
    calendar: Optional[UsSessionCalendar] = None,
) -> str:
    """Return canonical YYYY-MM-DD trading_date in US market calendar."""
    cal = calendar if calendar is not None else DEFAULT_US_CALENDAR
    return cal.get_trading_day(utc_ts, symbol=symbol).isoformat()


def now_utc_ts(clock: Optional[ClockProtocol] = None) -> float:
    """Return current UTC epoch seconds via injected ClockProtocol."""
    clk = clock if clock is not None else DEFAULT_CLOCK
    return clk.now_epoch()
