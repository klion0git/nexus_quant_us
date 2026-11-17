"""CME Globex US Index Futures (ES=F / NQ=F) Elliott Wave macro regime filter with cash-open freeze."""
from __future__ import annotations

from typing import Any, Optional, Sequence, Tuple

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import CandleBar, MacroRegimeSnapshot, WaveRegimeParams
from nexus_quant_core.math_utils import safe_float
from nexus_quant_core.signals import ElliottWaveRegimeFSM
from us_core.calendar import DEFAULT_US_CALENDAR, UsSessionCalendar
from us_core.config import US_WAVE_REGIME_PARAMS
from us_core.symbol import UsSymbol


class UsFuturesWaveFilter:
    """Macro regime gate wrapping nexus_quant_core.ElliottWaveRegimeFSM for ES=F / NQ=F.

    Features:
    1. CME Globex 23-hour overnight continuity via UsSessionCalendar (zero false resets across
       17:00~18:00 ET daily maintenance break or Friday 17:00 -> Sunday 18:00 ET weekend).
    2. Cash-equity opening freeze window (09:15 ~ 09:45 ET on NYSE trading days): locks the
       confirmed pre-09:15 ET overnight Globex regime across the 09:30 ET NYSE opening auction
       to prevent opening-wick whipsaws.
    """

    FREEZE_START_HM: Tuple[int, int] = (9, 15)
    FREEZE_END_HM: Tuple[int, int] = (9, 45)

    def __init__(
        self,
        *,
        symbol: str = "ES=F",
        timeframe: str = "15m",
        params: WaveRegimeParams = US_WAVE_REGIME_PARAMS,
        calendar: Optional[UsSessionCalendar] = None,
        enabled: bool = True,
    ) -> None:
        sym_info = UsSymbol.parse(symbol)
        self.symbol = sym_info.code
        self.calendar = calendar if calendar is not None else DEFAULT_US_CALENDAR
        self.params = params
        self.enabled = bool(enabled)
        self.fsm = ElliottWaveRegimeFSM(
            params=self.params,
            calendar=self.calendar,
            symbol=self.symbol,
            timeframe=timeframe,
        )
        self._pre_freeze_regime: Optional[MacroRegimeSnapshot] = None

    def is_in_opening_freeze_window(self, utc_ts: float) -> bool:
        """Return True during 09:15:00 <= ET < 09:45:00 on NYSE trading days."""
        dt_et = self.calendar.to_et_datetime(float(utc_ts))
        if not self.calendar.is_trading_day(dt_et.date()):
            return False
        hm = (dt_et.hour, dt_et.minute)
        return self.FREEZE_START_HM <= hm < self.FREEZE_END_HM

    def on_macro_candle(self, bar: Any) -> MacroRegimeSnapshot:
        """Feed a sealed CME futures candle (CandleBar or dict) into the causal wave FSM."""
        if isinstance(bar, dict):
            ts = safe_float(bar.get("timestamp", bar.get("ts", 0.0)), 0.0)
            op = safe_float(bar.get("open", bar.get("o", 0.0)), 0.0)
            hi = safe_float(bar.get("high", bar.get("h", 0.0)), 0.0)
            lo = safe_float(bar.get("low", bar.get("l", 0.0)), 0.0)
            cl = safe_float(bar.get("close", bar.get("c", 0.0)), 0.0)
        else:
            ts = safe_float(getattr(bar, "timestamp", getattr(bar, "ts", 0.0)), 0.0)
            op = safe_float(getattr(bar, "open", getattr(bar, "o", 0.0)), 0.0)
            hi = safe_float(getattr(bar, "high", getattr(bar, "h", 0.0)), 0.0)
            lo = safe_float(getattr(bar, "low", getattr(bar, "l", 0.0)), 0.0)
            cl = safe_float(getattr(bar, "close", getattr(bar, "c", 0.0)), 0.0)

        in_freeze = self.is_in_opening_freeze_window(ts) if ts > 0.0 else False
        if not in_freeze and self.fsm.last_regime.updated_at_ts > 0.0:
            self._pre_freeze_regime = self.fsm.last_regime

        live_snap = self.fsm.process_candle(
            high=hi,
            low=lo,
            close=cl,
            timestamp=ts,
            open_price=op if op > 0.0 else None,
        )
        if not in_freeze:
            self._pre_freeze_regime = live_snap
        return self.get_effective_regime(ts)

    def warmup(self, bars: Sequence[Any]) -> MacroRegimeSnapshot:
        for b in sorted(
            bars,
            key=lambda x: safe_float(
                x.get("timestamp", x.get("ts", 0.0)) if isinstance(x, dict) else getattr(x, "timestamp", 0.0),
                0.0,
            ),
        ):
            self.on_macro_candle(b)
        return self.fsm.last_regime

    def get_effective_regime(self, utc_ts: Optional[float] = None) -> MacroRegimeSnapshot:
        """Return active regime snapshot, preserving pre-09:15 ET regime during 09:15~09:45 ET freeze."""
        if utc_ts is not None and utc_ts > 0.0 and self.is_in_opening_freeze_window(utc_ts):
            if self._pre_freeze_regime is not None and self._pre_freeze_regime.updated_at_ts > 0.0:
                return MacroRegimeSnapshot(
                    trend=self._pre_freeze_regime.trend,
                    wave_stage=self._pre_freeze_regime.wave_stage,
                    fib_level=self._pre_freeze_regime.fib_level,
                    description=f"[OPENING_FREEZE 09:15-09:45 ET] {self._pre_freeze_regime.description}",
                    updated_at_ts=self._pre_freeze_regime.updated_at_ts,
                )
        return self.fsm.last_regime

    def allows_long_entry(
        self,
        utc_ts: float,
        *,
        entry_type: str = "BRK",
        is_bond: bool = False,
    ) -> Tuple[bool, str]:
        """Evaluate whether US equity/ETF long entry is permitted under the current ES=F/NQ=F wave regime."""
        if not self.enabled:
            return True, "Macro wave filter disabled"
        if is_bond:
            return True, "US Treasury ETF duration hedge exempt from equity futures bearish filter"

        snap = self.get_effective_regime(utc_ts)
        if snap.trend == "BEARISH":
            # Deep capitulation climax (CAP) may still contrarian-trigger; trend/pullback entries are gated
            if str(entry_type).upper() != "CAP":
                return False, f"Macro filter blocked: {self.symbol} {snap.trend} ({snap.wave_stage})"
        return True, f"Macro filter OK: {self.symbol} {snap.trend} ({snap.wave_stage})"
