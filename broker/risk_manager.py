"""US equity & ETF risk manager enforcing 1-share sizing, 2% daily loss circuit breaker, and cooldowns."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import FrictionParams, MarketExecutionParams
from nexus_quant_core.math_utils import safe_float
from nexus_quant_core.risk_math import (
    compute_position_size,
    compute_structural_stop_price,
    compute_trade_friction,
    is_buy_blocked_by_limit_up,
    is_within_daily_price_limit,
)
from us_core.config import US_EXECUTION_PARAMS, US_FRICTION_PARAMS
from us_core.symbol import UsSymbol


@dataclass
class RiskCheckResult:
    allowed: bool
    reason: str
    quantity: int = 0
    stop_loss_price: float = 0.0


class UsRiskManager:
    """Risk gatekeeper for US paper/live trading in USD."""

    def __init__(
        self,
        *,
        exec_params: MarketExecutionParams = US_EXECUTION_PARAMS,
        friction_params: FrictionParams = US_FRICTION_PARAMS,
        max_position_pct: float = 0.20,
        risk_per_trade_pct: float = 0.01,
        daily_loss_limit_pct: float = 0.02,
    ) -> None:
        self.exec_params = exec_params
        self.friction_params = friction_params
        self.max_position_pct = max(0.01, min(1.0, float(max_position_pct)))
        self.risk_per_trade_pct = max(0.001, min(0.20, float(risk_per_trade_pct)))
        self.daily_loss_limit_pct = max(0.005, min(0.50, float(daily_loss_limit_pct)))
        self._last_loss_ts_by_symbol: Dict[str, float] = {}
        self._last_signal_ts_by_symbol: Dict[str, float] = {}

    def record_signal_timestamp(self, symbol: str, ts: float) -> None:
        clean = UsSymbol.parse(symbol).code
        self._last_signal_ts_by_symbol[clean] = float(ts)

    def record_loss_exit(self, symbol: str, ts: float) -> None:
        clean = UsSymbol.parse(symbol).code
        self._last_loss_ts_by_symbol[clean] = float(ts)

    def is_daily_circuit_breaker_tripped(
        self,
        *,
        starting_equity: float,
        daily_pnl_usd: float,
    ) -> bool:
        start_eq = safe_float(starting_equity, 0.0)
        if start_eq <= 0.0:
            return False
        max_loss_usd = start_eq * self.daily_loss_limit_pct
        return safe_float(daily_pnl_usd, 0.0) <= -(max_loss_usd - 1e-6)

    def compute_stop_loss(
        self,
        *,
        entry_price: float,
        side: str = "BUY",
        atr: float = 0.0,
        bar_low: Optional[float] = None,
        bar_high: Optional[float] = None,
    ) -> float:
        return compute_structural_stop_price(
            entry_price=entry_price,
            side=side,
            atr=atr,
            bar_low=bar_low,
            bar_high=bar_high,
            exec_params=self.exec_params,
        )

    def compute_shares(
        self,
        *,
        account_equity: float,
        available_cash: float,
        entry_price: float,
        stop_price: float,
    ) -> int:
        eq = max(0.0, safe_float(account_equity, 0.0))
        cash = max(0.0, safe_float(available_cash, 0.0))
        px = safe_float(entry_price, 0.0)
        if eq <= 0.0 or cash <= 0.0 or px <= 0.0:
            return 0
        # Reserve tiny buffer for slippage so cash never goes negative
        slippage_mult = 1.0 + (max(0.0, self.friction_params.slippage_bps) / 10000.0) + 0.0005
        capital_budget = min(eq * self.max_position_pct, cash / slippage_mult)
        max_risk_usd = eq * self.risk_per_trade_pct
        return compute_position_size(
            capital_budget=capital_budget,
            entry_price=px,
            stop_price=stop_price,
            max_risk_amount=max_risk_usd,
            exec_params=self.exec_params,
        )

    def estimate_friction(
        self,
        *,
        symbol: str,
        side: str,
        price: float,
        quantity: float,
        is_day_trade: bool = False,
    ) -> Dict[str, float]:
        sym_info = UsSymbol.parse(symbol)
        return compute_trade_friction(
            side=side,
            price=price,
            quantity=quantity,
            friction_params=self.friction_params,
            is_etf=sym_info.is_etf,
            is_day_trade=is_day_trade,
        )

    def evaluate_open_order(
        self,
        *,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss_price: float,
        timestamp: float,
        account_equity: float,
        available_cash: float,
        starting_daily_equity: float,
        daily_pnl_usd: float,
        circuit_breaker_already_tripped: bool = False,
        ref_prev_close: Optional[float] = None,
        requested_quantity: Optional[int] = None,
        atr: float = 0.0,
    ) -> RiskCheckResult:
        clean_sym = UsSymbol.parse(symbol).code
        px = safe_float(entry_price, 0.0)
        ts = safe_float(timestamp, 0.0)
        if px <= 0.0:
            return RiskCheckResult(False, f"Invalid entry price: {entry_price!r}")

        # 1. Daily 2% circuit breaker check
        if circuit_breaker_already_tripped or self.is_daily_circuit_breaker_tripped(
            starting_equity=starting_daily_equity,
            daily_pnl_usd=daily_pnl_usd,
        ):
            return RiskCheckResult(
                False,
                f"Daily 2% loss circuit breaker active (daily_pnl=${daily_pnl_usd:.2f}, start=${starting_daily_equity:.2f})",
            )

        # 2. US market price limit checks (US has NO ±10% daily price limit; earnings gaps are allowed)
        if not is_within_daily_price_limit(px, ref_prev_close, exec_params=self.exec_params):
            return RiskCheckResult(False, "Price outside market daily price limit")
        if str(side).upper() == "BUY" and is_buy_blocked_by_limit_up(px, ref_prev_close, exec_params=self.exec_params):
            return RiskCheckResult(False, "Buy blocked by limit-up guard")

        # 3. Loss cooldown check
        last_loss_ts = self._last_loss_ts_by_symbol.get(clean_sym)
        if last_loss_ts is not None and (ts - last_loss_ts) < self.exec_params.loss_cooldown_seconds:
            rem = int(self.exec_params.loss_cooldown_seconds - (ts - last_loss_ts))
            return RiskCheckResult(False, f"Loss cooldown active for {clean_sym} ({rem}s remaining)")

        # 4. Signal cooldown check
        last_sig_ts = self._last_signal_ts_by_symbol.get(clean_sym)
        if last_sig_ts is not None and (ts - last_sig_ts) < self.exec_params.signal_cooldown_seconds:
            rem = int(self.exec_params.signal_cooldown_seconds - (ts - last_sig_ts))
            return RiskCheckResult(False, f"Signal cooldown active for {clean_sym} ({rem}s remaining)")

        # 5. Structural stop price normalization
        eff_stop = safe_float(stop_loss_price, 0.0)
        if eff_stop <= 0.0 or (str(side).upper() == "BUY" and eff_stop >= px):
            eff_stop = self.compute_stop_loss(entry_price=px, side=side, atr=atr)

        # 6. 1-share unit sizing
        if requested_quantity is not None and int(requested_quantity) > 0:
            max_affordable = self.compute_shares(
                account_equity=account_equity,
                available_cash=available_cash,
                entry_price=px,
                stop_price=eff_stop,
            )
            qty = min(int(requested_quantity), max_affordable) if max_affordable > 0 else 0
        else:
            qty = self.compute_shares(
                account_equity=account_equity,
                available_cash=available_cash,
                entry_price=px,
                stop_price=eff_stop,
            )

        if qty < 1:
            return RiskCheckResult(False, f"Insufficient capital/risk budget for 1 share of {clean_sym} at ${px:.2f}")

        return RiskCheckResult(True, "OK", quantity=int(qty), stop_loss_price=round(eff_stop, self.exec_params.price_decimals))
