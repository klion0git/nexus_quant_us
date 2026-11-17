"""US Stock FIR + MA520/MA1560 Breakout/Fanout chart strategy powered by nexus_quant_core."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import CALENDAR_TIMEFRAMES
from nexus_quant_core.indicators import (
    calculate_cascaded_fir_series,
    calculate_elliott_wave_overlays,
    calculate_rolling_poc_series,
    calculate_rolling_vwap_series,
    calculate_sma_series,
    calculate_volume_profile,
)
from nexus_quant_core.math_utils import safe_float, safe_int
from nexus_quant_core.signals import (
    evaluate_brk_entry,
    evaluate_btx_brk_exit,
    evaluate_btx_mfo_exit,
    evaluate_mfo_entry,
)
from chart_hist.config import normalize_chart_hist_timeframe
from us_core.calendar import DEFAULT_US_CALENDAR
from us_core.config import (
    US_BREAKOUT_PARAMS,
    US_FIR_PARAMS,
    US_VOLUME_PROFILE_PARAMS,
    US_WAVE_OVERLAY_PARAMS,
)


def _extract_bar_tuple(c: Any):
    if isinstance(c, dict):
        ts = safe_int(c.get("timestamp", c.get("time", 0)), 0)
        hi = safe_float(c.get("high", 0.0), 0.0)
        lo = safe_float(c.get("low", 0.0), 0.0)
        cl = safe_float(c.get("close", 0.0), 0.0)
    else:
        ts = safe_int(getattr(c, "timestamp", getattr(c, "time", 0)), 0)
        hi = safe_float(getattr(c, "high", 0.0), 0.0)
        lo = safe_float(getattr(c, "low", 0.0), 0.0)
        cl = safe_float(getattr(c, "close", 0.0), 0.0)
    return ts, hi, lo, cl


def evaluate_stock_us_fir_strategy(
    candles: Sequence[Any],
    *,
    symbol: str,
    timeframe: str,
) -> Dict[str, Any]:
    norm_tf = normalize_chart_hist_timeframe(timeframe)
    if not candles:
        return {
            "strategy": "stock_us_fir",
            "symbol": symbol,
            "timeframe": norm_tf,
            "overlays": {"fir": [], "vwap": [], "ma520": [], "ma1560": [], "poc": []},
            "markers": [],
            "elliott_waves": {"lines": [], "markers": [], "wave_count": 0},
            "volume_profile": {"poc": 0.0, "vah": 0.0, "val": 0.0, "total_volume": 0.0, "bins": []},
        }

    tuples = [_extract_bar_tuple(c) for c in candles]
    times = [t[0] for t in tuples]
    highs = [t[1] for t in tuples]
    closes = [t[3] for t in tuples]
    is_daily = norm_tf in CALENDAR_TIMEFRAMES

    # Use US 26-bars/day 15m windows (520=20d, 1560=60d) on 15m; scale naturally on higher timeframes
    if norm_tf == "15m":
        vwap_win = US_BREAKOUT_PARAMS.vwap_window      # 520
        mid_win = US_BREAKOUT_PARAMS.ma_mid_window     # 520
        long_win = US_BREAKOUT_PARAMS.ma_long_window   # 1560
    elif norm_tf == "1h":
        vwap_win, mid_win, long_win = 140, 140, 420    # 7 regular bars/day * 20d / 60d
    elif norm_tf == "4h":
        vwap_win, mid_win, long_win = 40, 40, 120      # 2 regular bars/day * 20d / 60d
    else:
        vwap_win, mid_win, long_win = 20, 20, 60

    fir_s = calculate_cascaded_fir_series(closes, params=US_FIR_PARAMS)
    vwap_s = calculate_rolling_vwap_series(candles, lookback=vwap_win)
    ma_mid_s = calculate_sma_series(closes, period=mid_win)
    ma_long_s = calculate_sma_series(closes, period=long_win)

    markers: List[Dict[str, Any]] = []
    in_pos = False
    pos_type = ""
    entry_idx: Optional[int] = None
    peak_high = 0.0

    if norm_tf == "15m" and len(closes) > US_BREAKOUT_PARAMS.ma_long_window:
        for i in range(US_BREAKOUT_PARAMS.ma_long_window, len(closes)):
            ts = times[i]
            if not in_pos:
                if not DEFAULT_US_CALENDAR.is_trading_day(DEFAULT_US_CALENDAR.to_et_datetime(float(ts)).date()):
                    continue
                dt_et = DEFAULT_US_CALENDAR.to_et_datetime(float(ts))
                cutoff = DEFAULT_US_CALENDAR.get_last_entry_cutoff_hm(dt_et.date())
                if not ((9, 30) <= (dt_et.hour, dt_et.minute) <= cutoff):
                    continue

                if evaluate_brk_entry(fir_s, vwap_s, ma_long_s, idx=i, params=US_BREAKOUT_PARAMS):
                    in_pos = True
                    pos_type = "BRK"
                    entry_idx = i
                    peak_high = highs[i]
                    markers.append(
                        {
                            "time": ts,
                            "position": "belowBar",
                            "color": "#10B981",
                            "shape": "arrowUp",
                            "text": "BUY BRK",
                        }
                    )
                elif evaluate_mfo_entry(fir_s, vwap_s, ma_mid_s, ma_long_s, idx=i, params=US_BREAKOUT_PARAMS):
                    in_pos = True
                    pos_type = "MFO"
                    entry_idx = i
                    peak_high = highs[i]
                    markers.append(
                        {
                            "time": ts,
                            "position": "belowBar",
                            "color": "#06B6D4",
                            "shape": "arrowUp",
                            "text": "BUY MFO",
                        }
                    )
            else:
                peak_high = max(peak_high, highs[i])
                if pos_type == "BRK":
                    ex, _ = evaluate_btx_brk_exit(
                        highs, closes, vwap_s, idx=i, params=US_BREAKOUT_PARAMS, entry_idx=entry_idx
                    )
                    tag = "BTX_BRK"
                else:
                    ex, _ = evaluate_btx_mfo_exit(
                        highs,
                        closes,
                        vwap_s,
                        idx=i,
                        params=US_BREAKOUT_PARAMS,
                        fir_params=US_FIR_PARAMS,
                        entry_idx=entry_idx,
                        max_high_since_entry=peak_high,
                        fir_vals=fir_s,
                    )
                    tag = "BTX_MFO"
                if ex:
                    in_pos = False
                    markers.append(
                        {
                            "time": ts,
                            "position": "aboveBar",
                            "color": "#F43F5E",
                            "shape": "arrowDown",
                            "text": f"EXIT {tag}",
                        }
                    )

    ew = calculate_elliott_wave_overlays(candles, timeframe=norm_tf, params=US_WAVE_OVERLAY_PARAMS)
    vp = calculate_volume_profile(candles, params=US_VOLUME_PROFILE_PARAMS, is_daily_level=is_daily)
    poc_s = calculate_rolling_poc_series(
        candles,
        params=US_VOLUME_PROFILE_PARAMS,
        calendar=DEFAULT_US_CALENDAR,
        symbol=symbol,
        is_daily_level=is_daily,
    )

    return {
        "strategy": "stock_us_fir",
        "symbol": symbol,
        "timeframe": norm_tf,
        "overlays": {
            "fir": [{"time": times[i], "value": fir_s[i]} for i in range(len(times))],
            "vwap": [{"time": times[i], "value": vwap_s[i]} for i in range(len(times))],
            "ma520": [{"time": times[i], "value": ma_mid_s[i]} for i in range(len(times))],
            "ma1560": [{"time": times[i], "value": ma_long_s[i]} for i in range(len(times))],
            "poc": poc_s,
        },
        "markers": markers,
        "elliott_waves": ew,
        "volume_profile": vp,
    }
