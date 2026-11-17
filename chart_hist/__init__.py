"""US Historical Chart Workstation package."""
from __future__ import annotations

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from chart_hist.config import (
    DB_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_SYMBOL,
    DEFAULT_TIMEFRAME,
    ORDERED_TIMEFRAMES,
    PRESET_SYMBOLS,
    SUPPORTED_TIMEFRAMES,
    normalize_chart_hist_timeframe,
)
from chart_hist.database import (
    delete_candles,
    get_available_symbols,
    get_candles,
    init_db,
    save_candles,
)
from chart_hist.server import create_app, start_server

__all__ = [
    "DB_PATH",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_SYMBOL",
    "DEFAULT_TIMEFRAME",
    "ORDERED_TIMEFRAMES",
    "PRESET_SYMBOLS",
    "SUPPORTED_TIMEFRAMES",
    "normalize_chart_hist_timeframe",
    "init_db",
    "save_candles",
    "get_candles",
    "delete_candles",
    "get_available_symbols",
    "create_app",
    "start_server",
]
