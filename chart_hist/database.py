"""SQLite WAL storage engine for US historical candles with strict 7-timeframe & dual-axis enforcement."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Sequence, Union

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import CandleBar
from nexus_quant_core.math_utils import safe_float, safe_int
from chart_hist.config import DB_PATH, normalize_chart_hist_timeframe
from chart_hist.models import SCHEMA_SQL, UsHistoricalCandle
from us_core.calendar import DEFAULT_US_CALENDAR, UsSessionCalendar
from us_core.clock import DEFAULT_CLOCK, ClockProtocol
from us_core.symbol import UsSymbol

_db_lock = threading.RLock()


def _resolve_db_path(db_path: Optional[Union[str, Path]] = None) -> Path:
    if db_path is not None and not isinstance(db_path, bool) and str(db_path).strip():
        return Path(db_path)
    return DB_PATH


@contextmanager
def db_session(db_path: Optional[Union[str, Path]] = None) -> Generator[sqlite3.Connection, None, None]:
    target = _resolve_db_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: Optional[Union[str, Path]] = None) -> Path:
    target = _resolve_db_path(db_path)
    with _db_lock, db_session(target) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    return target


def save_candles(
    candles: Union[CandleBar, UsHistoricalCandle, Dict[str, Any], Iterable[Any]],
    *,
    db_path: Optional[Union[str, Path]] = None,
    calendar: Optional[UsSessionCalendar] = None,
    clock: Optional[ClockProtocol] = None,
) -> int:
    """Persist US candles into SQLite WAL store; strictly validates 7-timeframe whitelist & trading_date NOT NULL."""
    if candles is None:
        return 0
    if isinstance(candles, (CandleBar, UsHistoricalCandle, dict)):
        items: Sequence[Any] = [candles]
    else:
        items = list(candles)
    if not items:
        return 0

    cal = calendar if calendar is not None else DEFAULT_US_CALENDAR
    clk = clock if clock is not None else DEFAULT_CLOCK
    now_ts = clk.now_epoch()
    init_db(db_path)

    rows_to_upsert = []
    for c in items:
        if c is None or isinstance(c, bool):
            continue
        if isinstance(c, dict):
            raw_sym = c.get("symbol", "")
            raw_tf = c.get("timeframe", "")
            raw_ts = c.get("timestamp", c.get("time", c.get("ts", 0)))
            raw_td = c.get("trading_date", "")
            raw_o = c.get("open", c.get("o"))
            raw_h = c.get("high", c.get("h"))
            raw_l = c.get("low", c.get("l"))
            raw_cl = c.get("close", c.get("c"))
            raw_v = c.get("volume", c.get("v", 0.0))
        else:
            raw_sym = getattr(c, "symbol", "")
            raw_tf = getattr(c, "timeframe", "")
            raw_ts = getattr(c, "timestamp", getattr(c, "time", getattr(c, "ts", 0)))
            raw_td = getattr(c, "trading_date", "")
            raw_o = getattr(c, "open", getattr(c, "o", None))
            raw_h = getattr(c, "high", getattr(c, "h", None))
            raw_l = getattr(c, "low", getattr(c, "l", None))
            raw_cl = getattr(c, "close", getattr(c, "c", None))
            raw_v = getattr(c, "volume", getattr(c, "v", 0.0))

        sym = UsSymbol.parse(raw_sym).code
        tf = normalize_chart_hist_timeframe(raw_tf)
        ts = safe_int(raw_ts, 0)
        cl = safe_float(raw_cl, 0.0)
        if ts <= 0 or cl <= 0.0:
            continue
        op = safe_float(raw_o, cl)
        if op <= 0.0:
            op = cl
        hi = max(op, cl, safe_float(raw_h, cl))
        lo_cand = safe_float(raw_l, cl)
        lo = min(op, cl, lo_cand if lo_cand > 0.0 else cl)
        vol = max(0.0, safe_float(raw_v, 0.0))

        td_str = str(raw_td or "").strip()
        if len(td_str) != 10 or td_str[4] != "-" or td_str[7] != "-":
            td_str = cal.get_trading_day(float(ts), symbol=sym).isoformat()

        rows_to_upsert.append((sym, tf, ts, td_str, op, hi, lo, cl, vol, now_ts))

    if not rows_to_upsert:
        return 0

    upsert_sql = """
    INSERT INTO historical_candles (
        symbol, timeframe, timestamp, trading_date, open, high, low, close, volume, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(symbol, timeframe, timestamp) DO UPDATE SET
        trading_date = excluded.trading_date,
        open = excluded.open,
        high = excluded.high,
        low = excluded.low,
        close = excluded.close,
        volume = excluded.volume,
        created_at = excluded.created_at;
    """

    with _db_lock, db_session(db_path) as conn:
        conn.executemany(upsert_sql, rows_to_upsert)
        conn.commit()
    return len(rows_to_upsert)


def get_candles(
    symbol: str,
    timeframe: str,
    *,
    start_timestamp: Optional[int] = None,
    end_timestamp: Optional[int] = None,
    limit: int = 2500,
    db_path: Optional[Union[str, Path]] = None,
) -> List[UsHistoricalCandle]:
    """Query sorted historical candles in ascending timestamp order (tail-sliced to limit)."""
    clean_sym = UsSymbol.parse(symbol).code
    norm_tf = normalize_chart_hist_timeframe(timeframe)
    eff_limit = max(1, min(20000, safe_int(limit, 2500)))
    init_db(db_path)

    clauses = ["symbol = ?", "timeframe = ?"]
    params: List[Any] = [clean_sym, norm_tf]
    if start_timestamp is not None:
        clauses.append("timestamp >= ?")
        params.append(int(start_timestamp))
    if end_timestamp is not None:
        clauses.append("timestamp <= ?")
        params.append(int(end_timestamp))
    params.append(eff_limit)

    where_sql = " AND ".join(clauses)
    query = f"""
    SELECT symbol, timeframe, timestamp, trading_date, open, high, low, close, volume, created_at
    FROM (
        SELECT * FROM historical_candles
        WHERE {where_sql}
        ORDER BY timestamp DESC
        LIMIT ?
    )
    ORDER BY timestamp ASC;
    """

    with db_session(db_path) as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        UsHistoricalCandle(
            symbol=str(r["symbol"]),
            timeframe=str(r["timeframe"]),
            timestamp=int(r["timestamp"]),
            trading_date=str(r["trading_date"]),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"]),
            created_at=float(r["created_at"]),
        )
        for r in rows
    ]


def delete_candles(
    *,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    confirm_all: bool = False,
    db_path: Optional[Union[str, Path]] = None,
) -> int:
    init_db(db_path)
    clauses: List[str] = []
    params: List[Any] = []
    if symbol:
        clauses.append("symbol = ?")
        params.append(UsSymbol.parse(symbol).code)
    if timeframe:
        clauses.append("timeframe = ?")
        params.append(normalize_chart_hist_timeframe(timeframe))

    if not clauses and not confirm_all:
        raise ValueError("Refusing to wipe all historical candles without confirm_all=True")

    sql = "DELETE FROM historical_candles"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    with _db_lock, db_session(db_path) as conn:
        cur = conn.execute(sql, params)
        deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        conn.commit()
    return deleted


def get_available_symbols(db_path: Optional[Union[str, Path]] = None) -> List[Dict[str, Any]]:
    init_db(db_path)
    sql = """
    SELECT
        symbol,
        timeframe,
        COUNT(*) AS bar_count,
        MIN(trading_date) AS min_date,
        MAX(trading_date) AS max_date
    FROM historical_candles
    GROUP BY symbol, timeframe
    ORDER BY symbol ASC, timeframe ASC;
    """
    with db_session(db_path) as conn:
        rows = conn.execute(sql).fetchall()

    grouped: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sym = str(r["symbol"])
        entry = grouped.setdefault(sym, {"symbol": sym, "timeframes": {}, "total_bars": 0})
        cnt = int(r["bar_count"])
        entry["timeframes"][str(r["timeframe"])] = {
            "count": cnt,
            "min_date": str(r["min_date"]),
            "max_date": str(r["max_date"]),
        }
        entry["total_bars"] += cnt
    return list(grouped.values())
