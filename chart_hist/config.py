"""Configuration for US Historical Chart Workstation (chart_hist)."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import VALID_CHART_TIMEFRAMES, TimeframeNormalizer

CHART_HIST_DIR: Path = Path(__file__).resolve().parent
DB_PATH: Path = CHART_HIST_DIR / "data" / "us_historical_candles.db"

DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8081
DEFAULT_SYMBOL: str = "NVDA"
DEFAULT_TIMEFRAME: str = "15m"

ORDERED_TIMEFRAMES: List[str] = ["15m", "1h", "4h", "1d", "1wk", "1mo", "3mo"]

SUPPORTED_TIMEFRAMES: List[Dict[str, Any]] = [
    {"code": "15m", "label": "15m", "description": "15-Minute (26 bars/day US Regular)"},
    {"code": "1h", "label": "1h", "description": "1-Hour Session-Aligned"},
    {"code": "4h", "label": "4h", "description": "4-Hour Session-Aligned"},
    {"code": "1d", "label": "1d", "description": "Daily (NYSE / CME Trade Date)"},
    {"code": "1wk", "label": "1wk", "description": "Weekly"},
    {"code": "1mo", "label": "1mo", "description": "Monthly (1M -> 1mo)"},
    {"code": "3mo", "label": "3mo", "description": "Quarterly (3M -> 3mo)"},
]

PRESET_SYMBOLS: List[Dict[str, str]] = [
    {"symbol": "NVDA", "name": "NVIDIA Corp.", "category": "US Equity", "exchange": "NASDAQ"},
    {"symbol": "AAPL", "name": "Apple Inc.", "category": "US Equity", "exchange": "NASDAQ"},
    {"symbol": "MSFT", "name": "Microsoft Corp.", "category": "US Equity", "exchange": "NASDAQ"},
    {"symbol": "TSLA", "name": "Tesla Inc.", "category": "US Equity", "exchange": "NASDAQ"},
    {"symbol": "META", "name": "Meta Platforms Inc.", "category": "US Equity", "exchange": "NASDAQ"},
    {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "category": "US Equity ETF", "exchange": "ARCA"},
    {"symbol": "QQQ", "name": "Invesco QQQ Trust", "category": "US Equity ETF", "exchange": "NASDAQ"},
    {"symbol": "TLT", "name": "iShares 20+ Year Treasury Bond ETF", "category": "US Treasury ETF", "exchange": "NASDAQ"},
    {"symbol": "ES=F", "name": "E-mini S&P 500 Futures (Globex)", "category": "CME Index Futures", "exchange": "CME"},
    {"symbol": "NQ=F", "name": "E-mini Nasdaq-100 Futures (Globex)", "category": "CME Index Futures", "exchange": "CME"},
]


def normalize_chart_hist_timeframe(tf: Any) -> str:
    """Strictly canonicalize into the 7 US chart timeframes; raises ValueError on '1m' or invalid inputs."""
    return TimeframeNormalizer.normalize_chart_timeframe(tf)


__all__ = [
    "CHART_HIST_DIR",
    "DB_PATH",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_SYMBOL",
    "DEFAULT_TIMEFRAME",
    "ORDERED_TIMEFRAMES",
    "SUPPORTED_TIMEFRAMES",
    "PRESET_SYMBOLS",
    "VALID_CHART_TIMEFRAMES",
    "normalize_chart_hist_timeframe",
]
