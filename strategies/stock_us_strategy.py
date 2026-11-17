"""US Equity & Treasury ETF Strategy (26 bars/day 15m calibration) powered by nexus_quant_core."""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import (
    BreakoutFsmParams,
    FirFilterParams,
    MarketExecutionParams,
    MorphologyParams,
    SignalEvent,
)
from nexus_quant_core.indicators import (
    SessionCandleAggregator,
    calculate_atr,
    calculate_cascaded_fir_series,
    calculate_derivatives,
    calculate_rolling_vwap_series,
    calculate_sma_series,
)
from nexus_quant_core.math_utils import safe_float
from nexus_quant_core.risk_math import compute_structural_stop_price
from nexus_quant_core.signals import (
    evaluate_brk_entry,
    evaluate_btx_brk_exit,
    evaluate_btx_mfo_exit,
    evaluate_masb_signals,
    evaluate_mfo_entry,
    extract_bond_etf_features,
    extract_daily_morphology,
)
from strategies.us_futures_wave_filter import UsFuturesWaveFilter
from us_core.calendar import DEFAULT_US_CALENDAR, UsSessionCalendar
from us_core.clock import DEFAULT_CLOCK, ClockProtocol
from us_core.config import (
    US_BREAKOUT_PARAMS,
    US_EXECUTION_PARAMS,
    US_FIR_PARAMS,
    US_MORPHOLOGY_PARAMS,
)
from us_core.symbol import UsSymbol


class StockUsStrategy:
    """US cash-session 15m strategy (09:30~16:00 ET = 26 bars/day; 13:00 ET early close = 14 bars/day).

    Supported Entry Archetypes:
    - BRK:  3-state 15m Breakout FSM across VWAP(520) & MA1560 (60 US days)
    - MFO:  Bullish Fanout Momentum (VWAP520 > MA520 > MA1560 + positive 78-bar slopes)
    - MASB: Moving Average Squeeze Breakout on FIR/VWAP/MA cluster
    - CAP / DBL / DIP / SQZ / REV: Causal prior-day daily morphology + 15m FIR bullish turn
    - BMR:  US Treasury Bond ETF (TLT, IEF, SHY, AGG, BND) oversold cycle mean reversion

    Supported Exit Archetypes:
    - Structural Stop-Loss (ATR-bounded [0.5%, 5.0%])
    - BTX_BRK (0.88 peak drawdown ratio [-12%] over 520 bars or High < VWAP520)
    - BTX_MFO (0.88 peak drawdown ratio or underwater FIR velocity < -0.35% / divergence >= 0.40%)
    - BMR_TARGET (mean reversion to daily MA60 or RSI >= 62)
    """

    MAX_15M_HISTORY = 2200

    def __init__(
        self,
        *,
        calendar: Optional[UsSessionCalendar] = None,
        clock: Optional[ClockProtocol] = None,
        wave_filter: Optional[UsFuturesWaveFilter] = None,
        fir_params: FirFilterParams = US_FIR_PARAMS,
        breakout_params: BreakoutFsmParams = US_BREAKOUT_PARAMS,
        morph_params: MorphologyParams = US_MORPHOLOGY_PARAMS,
        exec_params: MarketExecutionParams = US_EXECUTION_PARAMS,
        enable_brk: bool = True,
        enable_mfo: bool = True,
        enable_masb: bool = True,
        enable_morphology: bool = True,
        enable_bond_bmr: bool = True,
    ) -> None:
        self.calendar = calendar if calendar is not None else DEFAULT_US_CALENDAR
        self.clock = clock if clock is not None else DEFAULT_CLOCK
        self.wave_filter = wave_filter
        self.fir_params = fir_params
        self.breakout_params = breakout_params
        self.morph_params = morph_params
        self.exec_params = exec_params
        self.enable_brk = bool(enable_brk)
        self.enable_mfo = bool(enable_mfo)
        self.enable_masb = bool(enable_masb)
        self.enable_morphology = bool(enable_morphology)
        self.enable_bond_bmr = bool(enable_bond_bmr)

        self.aggregator = SessionCandleAggregator(
            self.calendar,
            window_seconds=900,
            max_bars=self.MAX_15M_HISTORY,
        )
        self._bars_15m: Dict[str, Deque[Dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=self.MAX_15M_HISTORY)
        )
        self._daily_morph_by_sym: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._daily_ma60_by_sym: Dict[str, Dict[str, float]] = {}
        self._bond_features_by_sym: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._entry_bar_ts_by_sym: Dict[str, int] = {}

    def _normalize_bar_dict(self, symbol: str, bar: Any) -> Optional[Dict[str, Any]]:
        if isinstance(bar, dict):
            ts = int(safe_float(bar.get("timestamp", bar.get("ts", 0)), 0.0))
            op = safe_float(bar.get("open", bar.get("o", 0.0)), 0.0)
            hi = safe_float(bar.get("high", bar.get("h", op)), op)
            lo = safe_float(bar.get("low", bar.get("l", op)), op)
            cl = safe_float(bar.get("close", bar.get("c", 0.0)), 0.0)
            vol = max(0.0, safe_float(bar.get("volume", bar.get("v", 0.0)), 0.0))
            td = str(bar.get("trading_date") or "").strip()
        else:
            ts = int(safe_float(getattr(bar, "timestamp", getattr(bar, "ts", 0)), 0.0))
            op = safe_float(getattr(bar, "open", getattr(bar, "o", 0.0)), 0.0)
            hi = safe_float(getattr(bar, "high", getattr(bar, "h", op)), op)
            lo = safe_float(getattr(bar, "low", getattr(bar, "l", op)), op)
            cl = safe_float(getattr(bar, "close", getattr(bar, "c", 0.0)), 0.0)
            vol = max(0.0, safe_float(getattr(bar, "volume", getattr(bar, "v", 0.0)), 0.0))
            td = str(getattr(bar, "trading_date", "") or "").strip()

        if ts <= 0 or cl <= 0.0:
            return None
        if op <= 0.0:
            op = cl
        hi = max(hi, op, cl)
        lo = min(lo, op, cl) if lo > 0.0 else min(op, cl)
        if not td:
            td = self.calendar.get_trading_day(float(ts), symbol=symbol).isoformat()
        return {
            "symbol": symbol,
            "timestamp": ts,
            "trading_date": td,
            "open": op,
            "high": hi,
            "low": lo,
            "close": cl,
            "volume": vol,
        }

    def warmup_daily_bars(self, symbol: str, daily_bars: Sequence[Any]) -> None:
        sym_info = UsSymbol.parse(symbol)
        clean = sym_info.code
        ma60_map, morph_map = extract_daily_morphology(daily_bars, params=self.morph_params)
        self._daily_ma60_by_sym[clean] = ma60_map
        self._daily_morph_by_sym[clean] = morph_map
        if sym_info.is_bond:
            self._bond_features_by_sym[clean] = extract_bond_etf_features(
                daily_bars,
                ma_period=self.morph_params.ma_quarter,
                rsi_period=self.morph_params.atr_period,
                breakdown_ratio=0.96,
            )

    def warmup_15m_bars(self, symbol: str, bars_15m: Sequence[Any]) -> int:
        clean = UsSymbol.parse(symbol).code
        buf = self._bars_15m[clean]
        buf.clear()
        loaded = 0
        parsed_list = [b for b in (self._normalize_bar_dict(clean, item) for item in bars_15m) if b is not None]
        parsed_list.sort(key=lambda x: x["timestamp"])
        for b in parsed_list:
            if self.calendar.get_session_type(float(b["timestamp"]), symbol=clean) == "REGULAR":
                buf.append(b)
                loaded += 1
        return loaded

    def is_entry_window_open(self, bar_label_ts: float, symbol: str = "") -> bool:
        """Check US regular-session entry gate: 09:30~15:30 ET normally, shrinking to 09:30~12:30 ET on 13:00 early closes."""
        ts = float(bar_label_ts)
        if self.calendar.get_session_type(ts, symbol=symbol) != "REGULAR":
            return False
        dt_et = self.calendar.to_et_datetime(ts)
        cutoff_hm = self.calendar.get_last_entry_cutoff_hm(dt_et.date())
        bar_hm = (dt_et.hour, dt_et.minute)
        return (9, 30) <= bar_hm <= cutoff_hm

    def _get_prior_daily_record(
        self,
        records_by_date: Dict[str, Dict[str, Any]],
        current_trading_date: str,
    ) -> Optional[Dict[str, Any]]:
        """Strictly causal lookup returning the latest completed daily record with trading_date < current_trading_date."""
        if not records_by_date:
            return None
        prior_dates = [d for d in records_by_date.keys() if d < current_trading_date]
        if not prior_dates:
            return None
        return records_by_date[max(prior_dates)]

    def get_latest_prev_close(self, symbol: str, current_trading_date: str) -> Optional[float]:
        clean = UsSymbol.parse(symbol).code
        rec = self._get_prior_daily_record(self._daily_morph_by_sym.get(clean, {}), current_trading_date)
        return float(rec["close"]) if rec and "close" in rec else None

    def on_tick(
        self,
        symbol: str,
        price: float,
        timestamp: float,
        volume: float = 0.0,
        *,
        open_position: Optional[Dict[str, Any]] = None,
    ) -> Optional[SignalEvent]:
        """Ingest tick into SessionCandleAggregator; evaluate strategy when a 15m regular bar seals."""
        clean = UsSymbol.parse(symbol).code
        if self.calendar.get_session_type(timestamp, symbol=clean) != "REGULAR":
            return None
        sealed = self.aggregator.update_candle(clean, price, timestamp, volume)
        if sealed is None:
            return None
        return self.on_15m_bar_closed(sealed, open_position=open_position)

    def on_15m_bar_closed(
        self,
        bar: Any,
        *,
        open_position: Optional[Dict[str, Any]] = None,
    ) -> Optional[SignalEvent]:
        """Evaluate causal US exits or entries on a sealed 15m regular-session candle."""
        raw_sym = bar.get("symbol", "") if isinstance(bar, dict) else getattr(bar, "symbol", "")
        sym_info = UsSymbol.parse(raw_sym)
        clean = sym_info.code

        norm_bar = self._normalize_bar_dict(clean, bar)
        if norm_bar is None:
            return None
        if self.calendar.get_session_type(float(norm_bar["timestamp"]), symbol=clean) != "REGULAR":
            return None

        buf = self._bars_15m[clean]
        if buf and buf[-1]["timestamp"] == norm_bar["timestamp"]:
            buf[-1] = norm_bar
        elif not buf or norm_bar["timestamp"] > buf[-1]["timestamp"]:
            buf.append(norm_bar)
        else:
            return None

        bars_list = list(buf)
        idx = len(bars_list) - 1
        closes = [b["close"] for b in bars_list]
        highs = [b["high"] for b in bars_list]
        lows = [b["low"] for b in bars_list]
        c_close = closes[idx]
        c_high = highs[idx]
        c_low = lows[idx]
        bar_ts = float(norm_bar["timestamp"])
        td_str = norm_bar["trading_date"]

        fir_vals = calculate_cascaded_fir_series(closes, params=self.fir_params)
        vwap_vals = calculate_rolling_vwap_series(bars_list, lookback=self.breakout_params.vwap_window)
        ma520_vals = calculate_sma_series(closes, period=self.breakout_params.ma_mid_window)
        ma1560_vals = calculate_sma_series(closes, period=self.breakout_params.ma_long_window)
        atr_15m = calculate_atr(highs, lows, closes, period=self.morph_params.atr_period)

        # ======================================================================
        # 1. EXIT EVALUATION (Active across full regular session until close)
        # ======================================================================
        if open_position is not None:
            stop_px = safe_float(open_position.get("stop_loss_price", 0.0), 0.0)
            pos_entry_type = str(open_position.get("entry_type", "BRK") or "BRK").upper()
            max_high_since = safe_float(open_position.get("max_high_since_entry", c_high), c_high)

            # Resolve entry_idx in current deque if available
            entry_idx: Optional[int] = None
            saved_entry_ts = self._entry_bar_ts_by_sym.get(clean)
            if saved_entry_ts is not None:
                for k in range(idx, -1, -1):
                    if bars_list[k]["timestamp"] <= saved_entry_ts:
                        entry_idx = k
                        break

            if stop_px > 0.0 and c_low <= stop_px:
                exit_px = round(min(c_close, stop_px), 2)
                return SignalEvent(
                    symbol=clean,
                    side="SELL",
                    action="CLOSE",
                    price=exit_px,
                    stop_loss_price=stop_px,
                    entry_type=pos_entry_type,
                    entry_subtype="STOP_LOSS",
                    reason=f"STOP_LOSS (15m low ${c_low:.2f} <= structural stop ${stop_px:.2f})",
                    timestamp=bar_ts,
                )

            if pos_entry_type == "BMR" and sym_info.is_bond:
                prior_bond = self._get_prior_daily_record(self._bond_features_by_sym.get(clean, {}), td_str)
                if prior_bond is not None:
                    ma60_val = safe_float(prior_bond.get("ma60", 0.0), 0.0)
                    rsi_val = safe_float(prior_bond.get("rsi", 50.0), 50.0)
                    if (ma60_val > 0.0 and c_close >= ma60_val) or rsi_val >= 62.0:
                        return SignalEvent(
                            symbol=clean,
                            side="SELL",
                            action="CLOSE",
                            price=c_close,
                            stop_loss_price=stop_px,
                            entry_type="BMR",
                            entry_subtype="BMR_TARGET",
                            reason=f"BMR mean-reversion target reached (${c_close:.2f} vs MA60 ${ma60_val:.2f}, RSI {rsi_val:.1f})",
                            timestamp=bar_ts,
                        )

            if pos_entry_type == "BRK":
                should_exit, exit_reason = evaluate_btx_brk_exit(
                    highs,
                    closes,
                    vwap_vals,
                    idx=idx,
                    params=self.breakout_params,
                    entry_idx=entry_idx,
                )
                if should_exit:
                    return SignalEvent(
                        symbol=clean,
                        side="SELL",
                        action="CLOSE",
                        price=c_close,
                        stop_loss_price=stop_px,
                        entry_type=pos_entry_type,
                        entry_subtype="BTX_BRK",
                        reason=exit_reason,
                        timestamp=bar_ts,
                    )
            else:
                should_exit, exit_reason = evaluate_btx_mfo_exit(
                    highs,
                    closes,
                    vwap_vals,
                    idx=idx,
                    params=self.breakout_params,
                    fir_params=self.fir_params,
                    entry_idx=entry_idx,
                    max_high_since_entry=max(max_high_since, c_high),
                    fir_vals=fir_vals,
                )
                if should_exit:
                    return SignalEvent(
                        symbol=clean,
                        side="SELL",
                        action="CLOSE",
                        price=c_close,
                        stop_loss_price=stop_px,
                        entry_type=pos_entry_type,
                        entry_subtype="BTX_MFO",
                        reason=exit_reason,
                        timestamp=bar_ts,
                    )
            return None

        # ======================================================================
        # 2. ENTRY EVALUATION (Gated by 09:30~15:30 ET or 09:30~12:30 ET early close)
        # ======================================================================
        if not self.is_entry_window_open(bar_ts, symbol=clean):
            return None

        c_fir = fir_vals[idx]
        p_fir = fir_vals[idx - 1] if idx >= 1 else c_fir
        p2_fir = fir_vals[idx - 2] if idx >= 2 else p_fir
        fir_vel, fir_acc = calculate_derivatives(
            c_fir, p_fir, p2_fir, deadzone_eps=self.fir_params.deadzone_eps
        )
        c_vwap = vwap_vals[idx]
        c_ma520 = ma520_vals[idx]
        bias20_intraday = ((c_close - c_ma520) / c_ma520) if c_ma520 > 1e-9 else 0.0

        prior_morph = self._get_prior_daily_record(self._daily_morph_by_sym.get(clean, {}), td_str)
        daily_atr = safe_float(prior_morph.get("daily_atr", 0.0), 0.0) if prior_morph else 0.0
        eff_atr = max(atr_15m, daily_atr * 0.35) if daily_atr > 0.0 else atr_15m

        triggered: Optional[Tuple[str, str, str]] = None

        # (A) BMR: US Treasury Bond ETF Mean Reversion (TLT / IEF / SHY / AGG / BND)
        if self.enable_bond_bmr and sym_info.is_bond and idx >= 5:
            prior_bond = self._get_prior_daily_record(self._bond_features_by_sym.get(clean, {}), td_str)
            if prior_bond is not None and not bool(prior_bond.get("is_breakdown", False)):
                rsi_val = safe_float(prior_bond.get("rsi", 50.0), 50.0)
                bias60_val = safe_float(prior_bond.get("bias60", 0.0), 0.0)
                if (rsi_val <= 38.0 or bias60_val <= -3.0) and fir_vel > 0.0 and c_close >= c_fir:
                    triggered = (
                        "BMR",
                        "BMR_TREASURY",
                        f"US Treasury BMR oversold turn (RSI={rsi_val:.1f}, bias60={bias60_val:.2f}%, FIR_vel=+{fir_vel:.3f}%)",
                    )

        # (B) BRK: 26-bars/day 3-state Breakout FSM across VWAP(520) & MA(1560)
        if triggered is None and self.enable_brk:
            in_bias_band = self.breakout_params.bias20_min <= bias20_intraday <= self.breakout_params.bias20_max
            if in_bias_band and evaluate_brk_entry(
                fir_vals,
                vwap_vals,
                ma1560_vals,
                idx=idx,
                params=self.breakout_params,
            ):
                triggered = (
                    "BRK",
                    "BRK_MA1560",
                    f"US 15m BRK breakout above MA1560 (${ma1560_vals[idx]:.2f}) with VWAP520 (${c_vwap:.2f}) ignition",
                )

        # (C) MFO: Bullish Moving Average Fanout (VWAP520 > MA520 > MA1560)
        if triggered is None and self.enable_mfo:
            if evaluate_mfo_entry(
                fir_vals,
                vwap_vals,
                ma520_vals,
                ma1560_vals,
                idx=idx,
                params=self.breakout_params,
            ):
                triggered = (
                    "MFO",
                    "MFO_FANOUT",
                    f"US 15m MFO fanout entry (VWAP520 ${c_vwap:.2f} > MA520 ${c_ma520:.2f} > MA1560 ${ma1560_vals[idx]:.2f})",
                )

        # (D) MASB: Moving Average Squeeze Breakout
        if triggered is None and self.enable_masb and len(bars_list) >= max(self.morph_params.ma_month, 30):
            win_bars = bars_list[-min(len(bars_list), 600) :]
            masb_res = evaluate_masb_signals(
                win_bars,
                fir_params=self.fir_params,
                morph_params=self.morph_params,
                vwap_window=self.breakout_params.vwap_window,
            )
            if masb_res["buy_indices"] and masb_res["buy_indices"][-1] == (len(win_bars) - 1):
                sp = masb_res["spread_pct"][-1]
                triggered = (
                    "MASB",
                    "MASB_15M",
                    f"US 15m MASB squeeze breakout (cluster spread={sp:.3f}%, FIR_vel=+{fir_vel:.3f}%)",
                )

        # (E) Daily Morphology Setups (CAP / DBL / DIP / SQZ / REV) + 15m Intraday Confirmation
        if triggered is None and self.enable_morphology and prior_morph is not None and idx >= 5:
            intraday_turn = fir_vel > 0.0 and c_close > c_fir and (p_fir <= vwap_vals[idx - 1] and c_fir > c_vwap or fir_acc > 0.0)
            if intraday_turn:
                if bool(prior_morph.get("is_cap_day")):
                    triggered = ("CAP", "CAP_CLIMAX", "US Daily Capitulation Climax + 15m FIR reversal")
                elif bool(prior_morph.get("is_dbl_day")):
                    sub = "DBL_A" if bool(prior_morph.get("is_dbl_a")) else "DBL_B"
                    triggered = ("DBL", sub, f"US Daily Double-Bottom ({sub}) + 15m FIR reversal")
                elif bool(prior_morph.get("is_dip_day")):
                    sub = (
                        "DIP_A"
                        if prior_morph.get("is_dip_a")
                        else "DIP_B"
                        if prior_morph.get("is_dip_b")
                        else "DIP_C"
                        if prior_morph.get("is_dip_c")
                        else "DIP_D"
                    )
                    triggered = ("DIP", sub, f"US Daily Pullback Support ({sub}) + 15m FIR turn")
                elif bool(prior_morph.get("is_sqz_base_day")):
                    triggered = ("SQZ", "SQZ_DAILY", "US Daily 4-MA Squeeze Base + 15m breakout")
                elif bool(prior_morph.get("is_rev_day")) and c_fir > c_vwap and fir_acc > 0.005:
                    triggered = ("REV", "REV_OVERSOLD", "US Daily Oversold Reversal + 15m FIR acceleration")

        if triggered is None:
            return None

        entry_type, entry_subtype, reason = triggered

        # Macro CME Futures (ES=F / NQ=F) Wave Regime Gate
        if self.wave_filter is not None:
            allowed, macro_note = self.wave_filter.allows_long_entry(
                bar_ts,
                entry_type=entry_type,
                is_bond=sym_info.is_bond,
            )
            if not allowed:
                return None
            reason = f"{reason} | {macro_note}"

        stop_px = compute_structural_stop_price(
            entry_price=c_close,
            side="BUY",
            atr=eff_atr,
            bar_low=c_low,
            bar_high=c_high,
            exec_params=self.exec_params,
        )
        self._entry_bar_ts_by_sym[clean] = norm_bar["timestamp"]

        return SignalEvent(
            symbol=clean,
            side="BUY",
            action="OPEN",
            price=c_close,
            stop_loss_price=stop_px,
            entry_type=entry_type,
            entry_subtype=entry_subtype,
            reason=reason,
            timestamp=bar_ts,
            context={
                "atr": round(eff_atr, 4),
                "vwap520": round(c_vwap, 4),
                "ma520": round(c_ma520, 4),
                "ma1560": round(ma1560_vals[idx], 4),
                "entry_bar_idx": idx,
                "trading_date": td_str,
            },
        )
