"""Yahoo Finance historical fetcher for all 7 canonical US chart timeframes."""
from __future__ import annotations

import asyncio
import datetime
from typing import Any, Dict, List, Optional

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import CandleBar
from nexus_quant_core.indicators import SessionCandleAggregator
from nexus_quant_core.math_utils import safe_float
from chart_hist.config import normalize_chart_hist_timeframe
from us_core.calendar import DEFAULT_US_CALENDAR, UsSessionCalendar
from us_core.config import TZ_UTC
from us_core.symbol import UsSymbol


class YFinanceUsChartFetcher:
    """Fetches US equities, ETFs, and CME futures across 15m, 1h, 4h, 1d, 1wk, 1mo, 3mo."""

    source_name = "yfinance"

    def __init__(self, *, calendar: Optional[UsSessionCalendar] = None) -> None:
        self.calendar = calendar if calendar is not None else DEFAULT_US_CALENDAR
        self._agg = SessionCandleAggregator(self.calendar, window_seconds=3600, max_bars=5000)

    def _fetch_sync(self, symbol: str, timeframe: str, days: int) -> List[CandleBar]:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError("yfinance package is not installed") from exc

        sym_info = UsSymbol.parse(symbol)
        clean = sym_info.code
        norm_tf = normalize_chart_hist_timeframe(timeframe)

        if norm_tf == "15m":
            yf_interval = "15m"
            period = f"{min(max(5, int(days)), 59)}d"
        elif norm_tf in ("1h", "4h"):
            yf_interval = "60m"
            period = f"{min(max(10, int(days)), 720)}d"
        elif norm_tf == "1d":
            yf_interval = "1d"
            period = f"{max(30, int(days))}d"
        elif norm_tf == "1wk":
            yf_interval = "1wk"
            period = "5y"
        elif norm_tf == "1mo":
            yf_interval = "1mo"
            period = "10y"
        else:  # 3mo
            yf_interval = "3mo"
            period = "max"

        ticker = yf.Ticker(sym_info.yf_ticker)
        df = ticker.history(period=period, interval=yf_interval, auto_adjust=False, prepost=sym_info.is_futures)
        if df is None or df.empty:
            return []

        raw_bars: List[Dict[str, Any]] = []
        for idx_ts, row in df.iterrows():
            if not hasattr(idx_ts, "timestamp"):
                continue
            ts_val = int(idx_ts.timestamp())
            cl = safe_float(row.get("Close"), 0.0)
            if cl <= 0.0:
                continue
            op = safe_float(row.get("Open"), cl)
            hi = max(op, cl, safe_float(row.get("High"), cl))
            lo = min(op, cl, safe_float(row.get("Low"), cl))
            vol = max(0.0, safe_float(row.get("Volume"), 0.0))

            if norm_tf in ("15m", "1h", "4h"):
                if not sym_info.is_futures and self.calendar.get_session_type(float(ts_val), symbol=clean) != "REGULAR":
                    continue
                td_str = self.calendar.get_trading_day(float(ts_val), symbol=clean).isoformat()
            else:
                dt_utc = datetime.datetime.fromtimestamp(float(ts_val), tz=TZ_UTC)
                d = dt_utc.date()
                td_str = d.isoformat()
                ts_val = int(datetime.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ_UTC).timestamp())

            raw_bars.append(
                {
                    "symbol": clean,
                    "timestamp": ts_val,
                    "trading_date": td_str,
                    "open": round(op, 4),
                    "high": round(hi, 4),
                    "low": round(lo, 4),
                    "close": round(cl, 4),
                    "volume": vol,
                }
            )

        if norm_tf == "4h":
            raw_bars = self._agg.resample_bars(raw_bars, target_window_seconds=14400, symbol=clean)

        return [
            CandleBar(
                symbol=clean,
                timeframe=norm_tf,
                timestamp=int(b["timestamp"]),
                trading_date=str(b["trading_date"]),
                open=round(float(b["open"]), 4),
                high=round(float(b["high"]), 4),
                low=round(float(b["low"]), 4),
                close=round(float(b["close"]), 4),
                volume=float(b["volume"]),
            )
            for b in raw_bars
        ]

    async def fetch(self, symbol: str, timeframe: str, *, days: int = 75) -> List[CandleBar]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_sync, symbol, timeframe, days)
