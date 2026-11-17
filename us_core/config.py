"""US market configuration, DST-safe America/New_York timezone, and nexus_quant_core parameter specs."""
from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from us_core._bootstrap import PROJECT_ROOT, ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import (
    BreakoutFsmParams,
    FirFilterParams,
    FrictionParams,
    MarketExecutionParams,
    MorphologyParams,
    VolumeProfileParams,
    WaveOverlayParams,
    WaveRegimeParams,
)
from nexus_quant_core.math_utils import safe_float, safe_int


class UsEasternFallbackTz(datetime.tzinfo):
    """Pure-stdlib US Eastern DST tzinfo (Energy Policy Act of 2007) used if system tzdata is missing.

    Rules:
    - EDT (UTC-4) begins 2nd Sunday in March at 02:00 local standard time (07:00 UTC).
    - EST (UTC-5) resumes 1st Sunday in November at 02:00 local daylight time (06:00 UTC).
    """

    key = "America/New_York"
    _EST_OFFSET = datetime.timedelta(hours=-5)
    _EDT_OFFSET = datetime.timedelta(hours=-4)
    _ZERO_DELTA = datetime.timedelta(0)
    _ONE_HOUR = datetime.timedelta(hours=1)

    @staticmethod
    def _dst_boundary_dates(year: int) -> Tuple[datetime.date, datetime.date]:
        march_1 = datetime.date(year, 3, 1)
        first_sun_mar = 1 + ((6 - march_1.weekday()) % 7)
        second_sun_mar = datetime.date(year, 3, first_sun_mar + 7)

        nov_1 = datetime.date(year, 11, 1)
        first_sun_nov = datetime.date(year, 11, 1 + ((6 - nov_1.weekday()) % 7))
        return second_sun_mar, first_sun_nov

    def _is_dst(self, dt: Optional[datetime.datetime]) -> bool:
        if dt is None:
            return False
        d = dt.date()
        dst_start, dst_end = self._dst_boundary_dates(dt.year)
        if d < dst_start or d > dst_end:
            return False
        if dst_start < d < dst_end:
            return True
        if d == dst_start:
            # 02:00 local springs forward to 03:00 EDT
            return (dt.hour, dt.minute, dt.second, dt.microsecond) >= (2, 0, 0, 0)
        # d == dst_end (1st Sunday in November)
        if dt.hour < 1:
            return True
        if dt.hour >= 2:
            return False
        # Ambiguous 01:00..01:59:59 hour: fold=0 is EDT (UTC-4), fold=1 is EST (UTC-5)
        return getattr(dt, "fold", 0) == 0

    def utcoffset(self, dt: Optional[datetime.datetime]) -> datetime.timedelta:
        return self._EDT_OFFSET if self._is_dst(dt) else self._EST_OFFSET

    def dst(self, dt: Optional[datetime.datetime]) -> datetime.timedelta:
        return self._ONE_HOUR if self._is_dst(dt) else self._ZERO_DELTA

    def tzname(self, dt: Optional[datetime.datetime]) -> str:
        return "EDT" if self._is_dst(dt) else "EST"

    def fromutc(self, dt: datetime.datetime) -> datetime.datetime:
        if dt.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")
        utc_naive = dt.replace(tzinfo=None)
        dst_start_d, dst_end_d = self._dst_boundary_dates(utc_naive.year)
        # 2nd Sunday in March 02:00 EST == 07:00 UTC; 1st Sunday in November 02:00 EDT == 06:00 UTC
        dst_start_utc = datetime.datetime(dst_start_d.year, 3, dst_start_d.day, 7, 0, 0)
        dst_end_utc = datetime.datetime(dst_end_d.year, 11, dst_end_d.day, 6, 0, 0)

        if dst_start_utc <= utc_naive < dst_end_utc:
            local = utc_naive + self._EDT_OFFSET
            return local.replace(tzinfo=self, fold=0)

        fold = 1 if (dst_end_utc <= utc_naive < (dst_end_utc + self._ONE_HOUR)) else 0
        local = utc_naive + self._EST_OFFSET
        return local.replace(tzinfo=self, fold=fold)

    def __repr__(self) -> str:
        return "UsEasternFallbackTz('America/New_York')"


def resolve_us_timezone() -> datetime.tzinfo:
    """Resolve ZoneInfo('America/New_York') with automatic pure-stdlib US DST fallback."""
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        return UsEasternFallbackTz()


TZ_US: datetime.tzinfo = resolve_us_timezone()
TZ_UTC: datetime.timezone = datetime.timezone.utc
DATA_DIR: Path = PROJECT_ROOT / "data"


def load_dotenv_file(dotenv_path: Optional[Path] = None, *, override: bool = False) -> Dict[str, str]:
    """Safely parse key=value pairs from .env without third-party dependencies."""
    target = dotenv_path if dotenv_path is not None else (PROJECT_ROOT / ".env")
    loaded: Dict[str, str] = {}
    if not target.exists() or not target.is_file():
        return loaded
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, val = line.split("=", 1)
        clean_key = key.strip()
        clean_val = val.strip()
        if len(clean_val) >= 2 and clean_val[0] == clean_val[-1] and clean_val[0] in ("'", '"'):
            clean_val = clean_val[1:-1]
        elif "#" in clean_val:
            clean_val = clean_val.split("#", 1)[0].strip()
        if not clean_key:
            continue
        loaded[clean_key] = clean_val
        if override or clean_key not in os.environ:
            os.environ[clean_key] = clean_val
    return loaded


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


# ==============================================================================
# Explicit US Parameter Instances for nexus_quant_core
# ==============================================================================
US_FIR_PARAMS = FirFilterParams(
    stage1_weights=(1.0, 2.0, 3.0),
    stage2_weights=(1.0, 2.0, 3.0),
    deadzone_eps=1e-6,
)

US_BREAKOUT_PARAMS = BreakoutFsmParams(
    bars_per_day_15m=26,
    vwap_window=520,
    ma_short_window=130,
    ma_mid_window=520,
    ma_long_window=1560,
    ignition_lookback=130,
    fsm_timeout_bars=130,
    slope_lookback=78,
    btx_high_lookback=520,
    btx_drawdown_ratio=0.88,
    btx_mfo_vel_threshold=-0.35,
    btx_mfo_max_divergence_pct=0.004,
    bias20_min=-0.04,
    bias20_max=0.45,
    min_submersion_bars=0,
    min_submersion_pct=0.0,
)

US_MORPHOLOGY_PARAMS = MorphologyParams(
    atr_period=14,
    ma_fast=5,
    ma_mid=10,
    ma_month=20,
    ma_quarter=60,
    vol_ma_period=20,
    cap_down_days=4,
    cap_bias20_max=-0.06,
    cap_vol_ratio_min=1.75,
    dbl_lookback_min=15,
    dbl_lookback_max=75,
    dbl_diff_tol=0.030,
    dbl_neck_min=0.10,
    dbl_vol_ratio_min=1.60,
    dbl_shadow_min=0.50,
    dbl_bias20_min=-0.14,
    dbl_bias20_max=-0.03,
    dip_bias60_abs_max=0.09,
    dip_a_bias20_max=-0.06,
    dip_b_bias20_max=-0.09,
    dip_c_pullback_max=-0.16,
    dip_d_pullback_max=-0.13,
    sqz_spread4_max=0.025,
    sqz_bias60_abs_max=0.025,
    sqz_vol_ratio_min=1.20,
    masb_squeeze_threshold_pct=0.35,
    masb_vel_min=0.012,
    masb_acc_min=0.006,
    masb_vol_mult=1.2,
)

US_WAVE_OVERLAY_PARAMS = WaveOverlayParams(
    window_by_timeframe={
        "15m": 5,
        "1h": 4,
        "4h": 4,
        "1d": 4,
        "1wk": 3,
        "1mo": 3,
        "3mo": 3,
    },
    threshold_bounds_by_timeframe={
        "15m": (0.008, 0.030),
        "1h": (0.015, 0.045),
        "4h": (0.015, 0.045),
        "1d": (0.022, 0.065),
        "1wk": (0.035, 0.095),
        "1mo": (0.050, 0.140),
        "3mo": (0.050, 0.140),
    },
    volatility_multiplier=2.2,
    wave4_overlap_tolerance=0.01,
)

US_WAVE_REGIME_PARAMS = WaveRegimeParams(
    swing_threshold_pct=0.40,
    fib_min_retracement=0.20,
    fib_max_retracement=0.65,
    default_bar_seconds=60.0,
    max_pivots=100,
    holiday_active_mult=1.2,
    gap_mult=1.8,
)

US_VOLUME_PROFILE_PARAMS = VolumeProfileParams(
    rolling_window=130,
    value_area_pct=0.70,
    intraday_tick_steps=(
        (500.0, 1.0),
        (100.0, 0.25),
        (20.0, 0.10),
        (1.0, 0.02),
        (0.0, 0.005),
    ),
    daily_tick_steps=(
        (500.0, 2.5),
        (100.0, 1.0),
        (20.0, 0.25),
        (1.0, 0.05),
        (0.0, 0.01),
    ),
    session_reset=True,
)

US_EXECUTION_PARAMS = MarketExecutionParams(
    currency="USD",
    lot_size=1,
    allow_odd_lot=True,
    price_decimals=2,
    daily_price_limit_pct=None,
    limit_up_buy_guard_ratio=None,
    min_stop_buffer_ratio=0.005,
    max_stop_risk_ratio=0.050,
    atr_stop_mult=1.8,
    loss_cooldown_seconds=900.0,
    signal_cooldown_seconds=180.0,
)

US_FRICTION_PARAMS = FrictionParams(
    buy_commission_rate=0.0,
    sell_commission_rate=0.0,
    sell_tax_rate=0.0,
    day_trade_tax_rate=0.0,
    etf_tax_rate=0.0,
    min_commission=0.0,
    per_share_fee=0.0,
    sec_taf_sell_rate=0.0000278,
    slippage_bps=2.0,
)


@dataclass(frozen=True)
class UsSettings:
    """Typed US runtime settings loaded from environment / .env."""

    broker_provider: str
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_paper: bool
    ibkr_host: str
    ibkr_port: int
    ibkr_client_id: int
    schwab_app_key: str
    schwab_app_secret: str
    target_stocks: Tuple[str, ...]
    macro_symbol: str
    initial_capital: float
    max_position_pct: float
    risk_per_trade_pct: float
    daily_loss_limit_pct: float
    chart_host: str
    chart_port: int
    discord_webhook_url: str
    paper_db_path: Path
    enable_brk: bool
    enable_mfo: bool
    enable_masb: bool
    enable_morphology: bool
    enable_bond_bmr: bool
    enable_macro_wave_filter: bool


def load_us_settings(dotenv_path: Optional[Path] = None) -> UsSettings:
    load_dotenv_file(dotenv_path)
    raw_targets = os.environ.get("US_TARGET_STOCKS", "NVDA,AAPL,MSFT,TSLA,META,SPY,QQQ,TLT")
    targets = tuple(s.strip().upper() for s in raw_targets.split(",") if s.strip())
    if not targets:
        targets = ("NVDA", "AAPL", "MSFT", "TSLA", "META", "SPY", "QQQ", "TLT")

    provider = os.environ.get("US_BROKER_PROVIDER", "sandbox").strip().lower() or "sandbox"
    return UsSettings(
        broker_provider=provider,
        alpaca_api_key=os.environ.get("ALPACA_API_KEY", "").strip(),
        alpaca_secret_key=os.environ.get("ALPACA_SECRET_KEY", "").strip(),
        alpaca_paper=_env_bool("ALPACA_PAPER", True),
        ibkr_host=os.environ.get("IBKR_HOST", "127.0.0.1").strip() or "127.0.0.1",
        ibkr_port=safe_int(os.environ.get("IBKR_PORT", "7497"), 7497),
        ibkr_client_id=safe_int(os.environ.get("IBKR_CLIENT_ID", "10"), 10),
        schwab_app_key=os.environ.get("SCHWAB_APP_KEY", "").strip(),
        schwab_app_secret=os.environ.get("SCHWAB_APP_SECRET", "").strip(),
        target_stocks=targets,
        macro_symbol=os.environ.get("US_MACRO_SYMBOL", "ES=F").strip().upper() or "ES=F",
        initial_capital=max(1000.0, safe_float(os.environ.get("US_INITIAL_CAPITAL", "100000"), 100000.0)),
        max_position_pct=max(0.01, min(1.0, safe_float(os.environ.get("US_MAX_POSITION_PCT", "0.20"), 0.20))),
        risk_per_trade_pct=max(0.001, min(0.10, safe_float(os.environ.get("US_RISK_PER_TRADE_PCT", "0.01"), 0.01))),
        daily_loss_limit_pct=max(0.005, min(0.20, safe_float(os.environ.get("US_DAILY_LOSS_LIMIT_PCT", "0.02"), 0.02))),
        chart_host=os.environ.get("US_CHART_HOST", "127.0.0.1").strip() or "127.0.0.1",
        chart_port=safe_int(os.environ.get("US_CHART_PORT", "8081"), 8081),
        discord_webhook_url=os.environ.get("DISCORD_WEBHOOK_URL", "").strip(),
        paper_db_path=DATA_DIR / "us_paper_trading.db",
        enable_brk=_env_bool("US_ENABLE_BRK", True),
        enable_mfo=_env_bool("US_ENABLE_MFO", True),
        enable_masb=_env_bool("US_ENABLE_MASB", True),
        enable_morphology=_env_bool("US_ENABLE_MORPHOLOGY", True),
        enable_bond_bmr=_env_bool("US_ENABLE_BOND_BMR", True),
        enable_macro_wave_filter=_env_bool("US_ENABLE_MACRO_WAVE_FILTER", True),
    )
