"""Deterministic US mock candle fetcher supporting all 7 canonical chart timeframes."""
from __future__ import annotations

import datetime
import hashlib
import math
import random
from typing import Any, Dict, List, Optional

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import CandleBar
from nexus_quant_core.indicators import SessionCandleAggregator
from chart_hist.config import normalize_chart_hist_timeframe
from feeds.base_feed import SandboxUsFeed
from us_core.calendar import DEFAULT_US_CALENDAR, UsSessionCalendar
from us_core.clock import DEFAULT_CLOCK, ClockProtocol
from us_core.config import TZ_UTC
from us_core.symbol import UsSymbol


def _seed_for(symbol: str, tf: str) -> int:
    digest = hashlib.sha256(f"us_mock:{symbol}:{tf}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


class MockUsChartFetcher:
    """Deterministic US historical bar generator for 15m, 1h, 4h, 1d, 1wk, 1mo, 3mo."""

    source_name = "mock"

    def __init__(
        self,
        *,
        calendar: Optional[UsSessionCalendar] = None,
        clock: Optional[ClockProtocol] = None,
        seed: int = 42,
    ) -> None:
        self.calendar = calendar if calendar is not None else DEFAULT_US_CALENDAR
        self.clock = clock if clock is not None else DEFAULT_CLOCK
        self.seed = int(seed)
        self._sandbox = SandboxUsFeed(calendar=self.calendar, clock=self.clock, seed=self.seed)
        self._agg = SessionCandleAggregator(self.calendar, window_seconds=900, max_bars=5000)

    async def fetch(self, symbol: str, timeframe: str, *, days: int = 75) -> List[CandleBar]:
        sym_info = UsSymbol.parse(symbol)
        clean = sym_info.code
        norm_tf = normalize_chart_hist_timeframe(timeframe)

        if norm_tf == "15m":
            eff_days = max(10, int(days))
            return await self._sandbox.fetch_15m_candles(clean, eff_days)

        if norm_tf in ("1h", "4h"):
            target_sec = 3600 if norm_tf == "1h" else 14400
            eff_days = max(20, int(days))
            bars_15m = await self._sandbox.fetch_15m_candles(clean, eff_days)
            raw_dicts = [
                {
                    "symbol": b.symbol,
                    "timestamp": b.timestamp,
                    "trading_date": b.trading_date,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in bars_15m
            ]
            resampled = self._agg.resample_bars(raw_dicts, target_window_seconds=target_sec, symbol=clean)
            return [
                CandleBar(
                    symbol=clean,
                    timeframe=norm_tf,
                    timestamp=int(r["timestamp"]),
                    trading_date=str(r["trading_date"]),
                    open=round(float(r["open"]), 2),
                    high=round(float(r["high"]), 2),
                    low=round(float(r["low"]), 2),
                    close=round(float(r["close"]), 2),
                    volume=round(float(r["volume"]), 2),
                )
                for r in resampled
            ]

        if norm_tf == "1d":
            eff_days = max(90, int(days))
            return await self._sandbox.fetch_daily_candles(clean, eff_days)

        # Calendar aggregations: 1wk, 1mo, 3mo aggregated from daily bars
        daily_count = 380 if norm_tf == "1wk" else (900 if norm_tf == "1mo" else 1800)
        daily_bars = await self._sandbox.fetch_daily_candles(clean, max(daily_count, int(days)))
        return self._aggregate_calendar_bars(clean, norm_tf, daily_bars)

    def _aggregate_calendar_bars(
        self,
        symbol: str,
        norm_tf: str,
        daily_bars: List[CandleBar],
    ) -> List[CandleBar]:
        buckets: Dict[str, Dict[str, Any]] = {}
        for b in daily_bars:
            d = datetime.date.fromisoformat(b.trading_date)
            if norm_tf == "1wk":
                anchor_d = d - datetime.timedelta(days=d.weekday())  # Monday of ISO week
            elif norm_tf == "1mo":
                anchor_d = datetime.date(d.year, d.month, 1)
            else:  # 3mo
                q_month = ((d.month - 1) // 3) * 3 + 1
                anchor_d = datetime.date(d.year, q_month, 1)

            key = anchor_d.isoformat()
            anchor_ts = int(datetime.datetime(anchor_d.year, anchor_d.month, anchor_d.day, 0, 0, 0, tzinfo=TZ_UTC).timestamp())
            if key not in buckets:
                buckets[key] = {
                    "timestamp": anchor_ts,
                    "trading_date": key,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
            else:
                acc = buckets[key]
                acc["high"] = max(acc["high"], b.high)
                acc["low"] = min(acc["low"], b.low)
                acc["close"] = b.close
                acc["volume"] += b.volume

        rng = random.Random(_seed_for(symbol, norm_tf))
        _ = rng  # deterministic hook
        return [
            CandleBar(
                symbol=symbol,
                timeframe=norm_tf,
                timestamp=int(buckets[k]["timestamp"]),
                trading_date=str(buckets[k]["trading_date"]),
                open=round(float(buckets[k]["open"]), 2),
                high=round(float(buckets[k]["high"]), 2),
                low=round(float(buckets[k]["low"]), 2),
                close=round(float(buckets[k]["close"]), 2),
                volume=round(float(buckets[k]["volume"]), 2),
            )
            for k in sorted(buckets.keys())
        ]
