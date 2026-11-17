"""US Moving Average Squeeze Breakout (MASB) chart strategy powered by nexus_quant_core."""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import CALENDAR_TIMEFRAMES
from nexus_quant_core.indicators import (
    calculate_elliott_wave_overlays,
    calculate_rolling_poc_series,
    calculate_sma_series,
    calculate_volume_profile,
)
from nexus_quant_core.math_utils import safe_float, safe_int
from nexus_quant_core.signals import evaluate_masb_signals
from chart_hist.config import normalize_chart_hist_timeframe
from us_core.calendar import DEFAULT_US_CALENDAR
from us_core.config import (
    US_BREAKOUT_PARAMS,
    US_FIR_PARAMS,
    US_MORPHOLOGY_PARAMS,
    US_VOLUME_PROFILE_PARAMS,
    US_WAVE_OVERLAY_PARAMS,
)


def evaluate_masb_chart_strategy(
    candles: Sequence[Any],
    *,
    symbol: str,
    timeframe: str,
) -> Dict[str, Any]:
    norm_tf = normalize_chart_hist_timeframe(timeframe)
    if not candles:
        return {
            "strategy": "masb",
            "symbol": symbol,
            "timeframe": norm_tf,
            "overlays": {"fir": [], "vwap": [], "ma520": [], "ma1560": [], "poc": []},
            "markers": [],
            "elliott_waves": {"lines": [], "markers": [], "wave_count": 0},
            "volume_profile": {"poc": 0.0, "vah": 0.0, "val": 0.0, "total_volume": 0.0, "bins": []},
        }

    times = [
        safe_int(c.get("timestamp", c.get("time", 0)) if isinstance(c, dict) else getattr(c, "timestamp", 0), 0)
        for c in candles
    ]
    closes = [
        safe_float(c.get("close", 0.0) if isinstance(c, dict) else getattr(c, "close", 0.0), 0.0)
        for c in candles
    ]
    is_daily = norm_tf in CALENDAR_TIMEFRAMES
    vwap_win = US_BREAKOUT_PARAMS.vwap_window if norm_tf == "15m" else 20

    res = evaluate_masb_signals(
        candles,
        fir_params=US_FIR_PARAMS,
        morph_params=US_MORPHOLOGY_PARAMS,
        vwap_window=vwap_win,
    )
    ma520_s = calculate_sma_series(closes, period=US_BREAKOUT_PARAMS.ma_mid_window if norm_tf == "15m" else 20)
    ma1560_s = calculate_sma_series(closes, period=US_BREAKOUT_PARAMS.ma_long_window if norm_tf == "15m" else 60)

    markers: List[Dict[str, Any]] = [
        {
            "time": times[idx],
            "position": "belowBar",
            "color": "#F59E0B",
            "shape": "arrowUp",
            "text": "BUY MASB",
        }
        for idx in res.get("buy_indices", [])
        if 0 <= idx < len(times)
    ]

    ew = calculate_elliott_wave_overlays(candles, timeframe=norm_tf, params=US_WAVE_OVERLAY_PARAMS)
    vp = calculate_volume_profile(candles, params=US_VOLUME_PROFILE_PARAMS, is_daily_level=is_daily)
    poc_s = calculate_rolling_poc_series(
        candles,
        params=US_VOLUME_PROFILE_PARAMS,
        calendar=DEFAULT_US_CALENDAR,
        symbol=symbol,
        is_daily_level=is_daily,
    )

    fir_s = res.get("fir") or closes
    vwap_s = res.get("vwap") or closes

    return {
        "strategy": "masb",
        "symbol": symbol,
        "timeframe": norm_tf,
        "overlays": {
            "fir": [{"time": times[i], "value": fir_s[i]} for i in range(len(fir_s))],
            "vwap": [{"time": times[i], "value": vwap_s[i]} for i in range(len(vwap_s))],
            "ma520": [{"time": times[i], "value": ma520_s[i]} for i in range(len(ma520_s))],
            "ma1560": [{"time": times[i], "value": ma1560_s[i]} for i in range(len(ma1560_s))],
            "poc": poc_s,
        },
        "markers": markers,
        "elliott_waves": ew,
        "volume_profile": vp,
    }
