"""SQLite dual-axis schema and data model for US historical candles."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS historical_candles (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL CHECK (timeframe IN ('15m', '1h', '4h', '1d', '1wk', '1mo', '3mo')),
    timestamp INTEGER NOT NULL,
    trading_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL,
    PRIMARY KEY (symbol, timeframe, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_us_candles_sym_tf_td
    ON historical_candles(symbol, timeframe, trading_date);

CREATE INDEX IF NOT EXISTS idx_us_candles_sym_tf_ts
    ON historical_candles(symbol, timeframe, timestamp);
"""


@dataclass(frozen=True)
class UsHistoricalCandle:
    symbol: str
    timeframe: str
    timestamp: int
    trading_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp,
            "time": self.timestamp,
            "trading_date": self.trading_date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }
