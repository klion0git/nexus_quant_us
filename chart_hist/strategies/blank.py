"""Blank US chart strategy providing raw candles plus Elliott Wave & Volume Profile POC overlays."""
from __future__ import annotations

from typing import Any, Dict, Sequence

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import CALENDAR_TIMEFRAMES
from nexus_quant_core.indicators import (
    calculate_elliott_wave_overlays,
    calculate_rolling_poc_series,
    calculate_volume_profile,
)
from chart_hist.config import normalize_chart_hist_timeframe
from us_core.calendar import DEFAULT_US_CALENDAR
from us_core.config import US_VOLUME_PROFILE_PARAMS, US_WAVE_OVERLAY_PARAMS


def evaluate_blank_strategy(
    candles: Sequence[Any],
    *,
    symbol: str,
    timeframe: str,
) -> Dict[str, Any]:
    norm_tf = normalize_chart_hist_timeframe(timeframe)
    is_daily = norm_tf in CALENDAR_TIMEFRAMES

    ew = calculate_elliott_wave_overlays(
        candles,
        timeframe=norm_tf,
        params=US_WAVE_OVERLAY_PARAMS,
    )
    vp = calculate_volume_profile(
        candles,
        params=US_VOLUME_PROFILE_PARAMS,
        is_daily_level=is_daily,
    )
    poc_series = calculate_rolling_poc_series(
        candles,
        params=US_VOLUME_PROFILE_PARAMS,
        calendar=DEFAULT_US_CALENDAR,
        symbol=symbol,
        is_daily_level=is_daily,
    )

    return {
        "strategy": "blank",
        "symbol": symbol,
        "timeframe": norm_tf,
        "overlays": {
            "poc": poc_series,
        },
        "markers": [],
        "elliott_waves": ew,
        "volume_profile": vp,
    }
