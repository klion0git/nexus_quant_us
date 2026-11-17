"""Fetcher registry for US chart_hist."""
from __future__ import annotations

from typing import Optional

from chart_hist.fetchers.mock_fetcher import MockUsChartFetcher
from chart_hist.fetchers.yfinance_fetcher import YFinanceUsChartFetcher
from us_core.calendar import UsSessionCalendar
from us_core.clock import ClockProtocol


def get_fetcher(
    source: str = "mock",
    *,
    calendar: Optional[UsSessionCalendar] = None,
    clock: Optional[ClockProtocol] = None,
):
    key = str(source or "mock").strip().lower()
    if key in ("mock", "sandbox"):
        return MockUsChartFetcher(calendar=calendar, clock=clock)
    if key in ("yfinance", "yf"):
        return YFinanceUsChartFetcher(calendar=calendar)
    raise ValueError(f"Unsupported US chart fetcher source: {source!r}; expected 'mock' or 'yfinance'")


__all__ = ["MockUsChartFetcher", "YFinanceUsChartFetcher", "get_fetcher"]
