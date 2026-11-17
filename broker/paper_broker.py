"""SQLite WAL US paper broker in USD with 1-share execution, SEC/TAF sell friction, and daily risk tracking."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Union

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import OrderInstruction, SignalEvent
from nexus_quant_core.math_utils import safe_float, safe_int
from broker.risk_manager import UsRiskManager
from us_core.calendar import DEFAULT_US_CALENDAR, UsSessionCalendar
from us_core.clock import DEFAULT_CLOCK, ClockProtocol
from us_core.config import DATA_DIR, US_EXECUTION_PARAMS, US_FRICTION_PARAMS
from us_core.symbol import UsSymbol


PAPER_BROKER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    currency TEXT NOT NULL DEFAULT 'USD',
    initial_capital REAL NOT NULL,
    cash REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0.0,
    total_fees REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    avg_price REAL NOT NULL,
    stop_loss_price REAL NOT NULL,
    max_high_since_entry REAL NOT NULL,
    entry_type TEXT NOT NULL DEFAULT '',
    entry_subtype TEXT NOT NULL DEFAULT '',
    entry_bar_idx INTEGER,
    opened_at REAL NOT NULL,
    opened_trading_date TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    requested_price REAL NOT NULL,
    fill_price REAL NOT NULL,
    notional_usd REAL NOT NULL,
    commission_usd REAL NOT NULL,
    sec_taf_fee_usd REAL NOT NULL,
    slippage_usd REAL NOT NULL,
    total_friction_usd REAL NOT NULL,
    realized_pnl_usd REAL NOT NULL DEFAULT 0.0,
    entry_type TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    trading_date TEXT NOT NULL,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_risk_state (
    trading_date TEXT PRIMARY KEY,
    starting_equity REAL NOT NULL,
    realized_pnl_today REAL NOT NULL DEFAULT 0.0,
    circuit_breaker_tripped INTEGER NOT NULL DEFAULT 0,
    last_loss_ts REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_us_trades_symbol_ts ON trade_history(symbol, timestamp);
CREATE INDEX IF NOT EXISTS idx_us_trades_trading_date ON trade_history(trading_date);
"""


class UsPaperBroker:
    """Thread-safe SQLite WAL paper broker for US equities & ETFs in USD."""

    DEFAULT_ACCOUNT_ID = "US_MAIN"

    def __init__(
        self,
        *,
        db_path: Optional[Union[str, Path]] = None,
        initial_capital: float = 100000.0,
        calendar: Optional[UsSessionCalendar] = None,
        clock: Optional[ClockProtocol] = None,
        risk_manager: Optional[UsRiskManager] = None,
        max_position_pct: float = 0.20,
        risk_per_trade_pct: float = 0.01,
        daily_loss_limit_pct: float = 0.02,
    ) -> None:
        self.db_path: Path = Path(db_path) if db_path is not None else (DATA_DIR / "us_paper_trading.db")
        self.initial_capital = max(100.0, float(initial_capital))
        self.calendar = calendar if calendar is not None else DEFAULT_US_CALENDAR
        self.clock = clock if clock is not None else DEFAULT_CLOCK
        self.risk_manager = (
            risk_manager
            if risk_manager is not None
            else UsRiskManager(
                exec_params=US_EXECUTION_PARAMS,
                friction_params=US_FRICTION_PARAMS,
                max_position_pct=max_position_pct,
                risk_per_trade_pct=risk_per_trade_pct,
                daily_loss_limit_pct=daily_loss_limit_pct,
            )
        )
        self._lock = threading.RLock()
        self._mark_prices: Dict[str, float] = {}
        self._init_db()

    @contextmanager
    def _session(self) -> Generator[sqlite3.Connection, None, None]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        now_ts = self.clock.now_epoch()
        with self._lock, self._session() as conn:
            conn.executescript(PAPER_BROKER_SCHEMA_SQL)
            row = conn.execute(
                "SELECT account_id FROM accounts WHERE account_id = ?",
                (self.DEFAULT_ACCOUNT_ID,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO accounts (account_id, currency, initial_capital, cash, realized_pnl, total_fees, updated_at)
                    VALUES (?, 'USD', ?, ?, 0.0, 0.0, ?)
                    """,
                    (self.DEFAULT_ACCOUNT_ID, self.initial_capital, self.initial_capital, now_ts),
                )
            # Rehydrate last loss timestamps for cooldown continuity
            loss_rows = conn.execute(
                """
                SELECT symbol, MAX(timestamp) AS last_ts
                FROM trade_history
                WHERE action = 'CLOSE' AND realized_pnl_usd < 0
                GROUP BY symbol
                """
            ).fetchall()
            for r in loss_rows:
                self.risk_manager.record_loss_exit(str(r["symbol"]), float(r["last_ts"]))
            conn.commit()

    def _compute_equity_locked(
        self,
        conn: sqlite3.Connection,
        mark_prices: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        acc = conn.execute(
            "SELECT initial_capital, cash, realized_pnl, total_fees FROM accounts WHERE account_id = ?",
            (self.DEFAULT_ACCOUNT_ID,),
        ).fetchone()
        cash = float(acc["cash"])
        init_cap = float(acc["initial_capital"])
        realized = float(acc["realized_pnl"])
        total_fees = float(acc["total_fees"])

        pos_rows = conn.execute("SELECT symbol, side, quantity, avg_price FROM positions").fetchall()
        market_val = 0.0
        unrealized = 0.0
        merged_marks = dict(self._mark_prices)
        if mark_prices:
            for k, v in mark_prices.items():
                merged_marks[UsSymbol.parse(k).code] = float(v)

        for p in pos_rows:
            sym = str(p["symbol"])
            qty = int(p["quantity"])
            avg_px = float(p["avg_price"])
            mark_px = merged_marks.get(sym, avg_px)
            market_val += mark_px * qty
            if str(p["side"]).upper() == "BUY":
                unrealized += (mark_px - avg_px) * qty
            else:
                unrealized += (avg_px - mark_px) * qty

        equity = cash + market_val
        return {
            "initial_capital": round(init_cap, 2),
            "cash": round(cash, 2),
            "market_value": round(market_val, 2),
            "equity": round(equity, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_fees": round(total_fees, 4),
        }

    def _ensure_daily_risk_row_locked(
        self,
        conn: sqlite3.Connection,
        trading_date: str,
        current_equity: float,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM daily_risk_state WHERE trading_date = ?",
            (trading_date,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO daily_risk_state (trading_date, starting_equity, realized_pnl_today, circuit_breaker_tripped, last_loss_ts)
                VALUES (?, ?, 0.0, 0, 0.0)
                """,
                (trading_date, current_equity),
            )
            row = conn.execute(
                "SELECT * FROM daily_risk_state WHERE trading_date = ?",
                (trading_date,),
            ).fetchone()
        return row

    def get_account_summary(self, mark_prices: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        now_ts = self.clock.now_epoch()
        td_str = self.calendar.get_trading_day(now_ts).isoformat()
        with self._lock, self._session() as conn:
            eq_info = self._compute_equity_locked(conn, mark_prices=mark_prices)
            d_row = self._ensure_daily_risk_row_locked(conn, td_str, eq_info["equity"])
            conn.commit()
            daily_pnl = float(d_row["realized_pnl_today"]) + eq_info["unrealized_pnl"]
            tripped = bool(d_row["circuit_breaker_tripped"]) or self.risk_manager.is_daily_circuit_breaker_tripped(
                starting_equity=float(d_row["starting_equity"]),
                daily_pnl_usd=daily_pnl,
            )
            return {
                "account_id": self.DEFAULT_ACCOUNT_ID,
                "currency": "USD",
                "trading_date": td_str,
                **eq_info,
                "starting_daily_equity": round(float(d_row["starting_equity"]), 2),
                "realized_pnl_today": round(float(d_row["realized_pnl_today"]), 2),
                "daily_total_pnl": round(daily_pnl, 2),
                "circuit_breaker_tripped": tripped,
            }

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        clean = UsSymbol.parse(symbol).code
        with self._lock, self._session() as conn:
            row = conn.execute("SELECT * FROM positions WHERE symbol = ?", (clean,)).fetchone()
            return dict(row) if row is not None else None

    def get_all_positions(self) -> List[Dict[str, Any]]:
        with self._lock, self._session() as conn:
            rows = conn.execute("SELECT * FROM positions ORDER BY symbol ASC").fetchall()
            return [dict(r) for r in rows]

    def get_trade_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        lim = max(1, safe_int(limit, 100))
        with self._lock, self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM trade_history ORDER BY id DESC LIMIT ?",
                (lim,),
            ).fetchall()
            return [dict(r) for r in rows]

    def execute_signal(
        self,
        signal: SignalEvent,
        *,
        ref_prev_close: Optional[float] = None,
        requested_quantity: Optional[int] = None,
    ) -> Dict[str, Any]:
        order = OrderInstruction(
            symbol=signal.symbol,
            side=signal.side,
            action=signal.action,
            quantity=float(requested_quantity or 0),
            price=signal.price,
            stop_loss_price=signal.stop_loss_price,
            reason=signal.reason,
            timestamp=signal.timestamp,
            context={
                **dict(signal.context or {}),
                "entry_type": signal.entry_type,
                "entry_subtype": signal.entry_subtype,
            },
        )
        return self.submit_order(order, ref_prev_close=ref_prev_close)

    def submit_order(
        self,
        order: OrderInstruction,
        *,
        ref_prev_close: Optional[float] = None,
    ) -> Dict[str, Any]:
        sym_info = UsSymbol.parse(order.symbol)
        clean_sym = sym_info.code
        action = str(order.action or "OPEN").strip().upper()
        side = str(order.side or "BUY").strip().upper()
        ts = float(order.timestamp) if order.timestamp > 0 else self.clock.now_epoch()
        td_str = self.calendar.get_trading_day(ts, symbol=clean_sym).isoformat()
        req_px = safe_float(order.price, 0.0)
        ctx = dict(order.context or {})

        with self._lock, self._session() as conn:
            self._mark_prices[clean_sym] = req_px
            eq_info = self._compute_equity_locked(conn)
            d_row = self._ensure_daily_risk_row_locked(conn, td_str, eq_info["equity"])
            existing = conn.execute("SELECT * FROM positions WHERE symbol = ?", (clean_sym,)).fetchone()

            if action == "OPEN":
                if existing is not None:
                    return {
                        "accepted": False,
                        "reason": f"Position already open for {clean_sym}",
                        "symbol": clean_sym,
                    }

                daily_pnl = float(d_row["realized_pnl_today"]) + eq_info["unrealized_pnl"]
                tripped = bool(d_row["circuit_breaker_tripped"]) or self.risk_manager.is_daily_circuit_breaker_tripped(
                    starting_equity=float(d_row["starting_equity"]),
                    daily_pnl_usd=daily_pnl,
                )
                if tripped and not bool(d_row["circuit_breaker_tripped"]):
                    conn.execute(
                        "UPDATE daily_risk_state SET circuit_breaker_tripped = 1 WHERE trading_date = ?",
                        (td_str,),
                    )
                    conn.commit()

                req_qty = int(order.quantity) if order.quantity >= 1.0 else None
                chk = self.risk_manager.evaluate_open_order(
                    symbol=clean_sym,
                    side=side,
                    entry_price=req_px,
                    stop_loss_price=order.stop_loss_price,
                    timestamp=ts,
                    account_equity=eq_info["equity"],
                    available_cash=eq_info["cash"],
                    starting_daily_equity=float(d_row["starting_equity"]),
                    daily_pnl_usd=daily_pnl,
                    circuit_breaker_already_tripped=tripped,
                    ref_prev_close=ref_prev_close,
                    requested_quantity=req_qty,
                    atr=safe_float(ctx.get("atr", 0.0), 0.0),
                )
                if not chk.allowed:
                    return {
                        "accepted": False,
                        "reason": chk.reason,
                        "symbol": clean_sym,
                    }

                qty = chk.quantity
                friction = self.risk_manager.estimate_friction(
                    symbol=clean_sym,
                    side="BUY",
                    price=req_px,
                    quantity=qty,
                    is_day_trade=False,
                )
                total_cost = (req_px * qty) + friction["total"]
                new_cash = eq_info["cash"] - total_cost
                fill_px = round(req_px * (1.0 + US_FRICTION_PARAMS.slippage_bps / 10000.0), 4)
                entry_type = str(ctx.get("entry_type", "") or "")
                entry_subtype = str(ctx.get("entry_subtype", "") or "")
                entry_bar_idx = ctx.get("entry_bar_idx")

                conn.execute(
                    """
                    UPDATE accounts
                    SET cash = ?, total_fees = total_fees + ?, updated_at = ?
                    WHERE account_id = ?
                    """,
                    (new_cash, friction["total"], ts, self.DEFAULT_ACCOUNT_ID),
                )
                conn.execute(
                    """
                    INSERT INTO positions (
                        symbol, side, quantity, avg_price, stop_loss_price, max_high_since_entry,
                        entry_type, entry_subtype, entry_bar_idx, opened_at, opened_trading_date, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_sym,
                        "BUY",
                        qty,
                        req_px,
                        chk.stop_loss_price,
                        req_px,
                        entry_type,
                        entry_subtype,
                        int(entry_bar_idx) if entry_bar_idx is not None else None,
                        ts,
                        td_str,
                        ts,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO trade_history (
                        symbol, side, action, quantity, requested_price, fill_price, notional_usd,
                        commission_usd, sec_taf_fee_usd, slippage_usd, total_friction_usd,
                        realized_pnl_usd, entry_type, reason, trading_date, timestamp
                    ) VALUES (?, 'BUY', 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?)
                    """,
                    (
                        clean_sym,
                        qty,
                        req_px,
                        fill_px,
                        friction["notional"],
                        friction["commission"],
                        friction["tax"],
                        friction["slippage"],
                        friction["total"],
                        entry_type,
                        order.reason,
                        td_str,
                        ts,
                    ),
                )
                self.risk_manager.record_signal_timestamp(clean_sym, ts)
                conn.commit()
                return {
                    "accepted": True,
                    "action": "OPEN",
                    "symbol": clean_sym,
                    "side": "BUY",
                    "quantity": qty,
                    "price": req_px,
                    "fill_price": fill_px,
                    "stop_loss_price": chk.stop_loss_price,
                    "friction": friction,
                    "trading_date": td_str,
                }

            # CLOSE order
            if existing is None:
                return {
                    "accepted": False,
                    "reason": f"No open position to close for {clean_sym}",
                    "symbol": clean_sym,
                }

            pos_qty = int(existing["quantity"])
            close_qty = pos_qty if order.quantity <= 0 else min(pos_qty, int(order.quantity))
            avg_px = float(existing["avg_price"])
            is_day_trade = str(existing["opened_trading_date"]) == td_str

            open_friction = self.risk_manager.estimate_friction(
                symbol=clean_sym,
                side="BUY",
                price=avg_px,
                quantity=close_qty,
                is_day_trade=is_day_trade,
            )
            sell_friction = self.risk_manager.estimate_friction(
                symbol=clean_sym,
                side="SELL",
                price=req_px,
                quantity=close_qty,
                is_day_trade=is_day_trade,
            )
            gross_proceeds = req_px * close_qty
            net_proceeds = gross_proceeds - sell_friction["total"]
            gross_pnl = (req_px - avg_px) * close_qty
            realized_pnl = round(gross_pnl - open_friction["total"] - sell_friction["total"], 4)
            fill_px = round(req_px * (1.0 - US_FRICTION_PARAMS.slippage_bps / 10000.0), 4)

            conn.execute(
                """
                UPDATE accounts
                SET cash = cash + ?, realized_pnl = realized_pnl + ?, total_fees = total_fees + ?, updated_at = ?
                WHERE account_id = ?
                """,
                (net_proceeds, realized_pnl, sell_friction["total"], ts, self.DEFAULT_ACCOUNT_ID),
            )

            remaining_qty = pos_qty - close_qty
            if remaining_qty <= 0:
                conn.execute("DELETE FROM positions WHERE symbol = ?", (clean_sym,))
            else:
                conn.execute(
                    "UPDATE positions SET quantity = ?, updated_at = ? WHERE symbol = ?",
                    (remaining_qty, ts, clean_sym),
                )

            new_daily_realized = float(d_row["realized_pnl_today"]) + realized_pnl
            trip_now = int(
                bool(d_row["circuit_breaker_tripped"])
                or self.risk_manager.is_daily_circuit_breaker_tripped(
                    starting_equity=float(d_row["starting_equity"]),
                    daily_pnl_usd=new_daily_realized,
                )
            )
            last_loss_val = ts if realized_pnl < 0.0 else float(d_row["last_loss_ts"])
            conn.execute(
                """
                UPDATE daily_risk_state
                SET realized_pnl_today = ?, circuit_breaker_tripped = ?, last_loss_ts = ?
                WHERE trading_date = ?
                """,
                (new_daily_realized, trip_now, last_loss_val, td_str),
            )
            if realized_pnl < 0.0:
                self.risk_manager.record_loss_exit(clean_sym, ts)
            self.risk_manager.record_signal_timestamp(clean_sym, ts)

            conn.execute(
                """
                INSERT INTO trade_history (
                    symbol, side, action, quantity, requested_price, fill_price, notional_usd,
                    commission_usd, sec_taf_fee_usd, slippage_usd, total_friction_usd,
                    realized_pnl_usd, entry_type, reason, trading_date, timestamp
                ) VALUES (?, 'SELL', 'CLOSE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_sym,
                    close_qty,
                    req_px,
                    fill_px,
                    sell_friction["notional"],
                    sell_friction["commission"],
                    sell_friction["tax"],
                    sell_friction["slippage"],
                    sell_friction["total"],
                    realized_pnl,
                    str(existing["entry_type"]),
                    order.reason,
                    td_str,
                    ts,
                ),
            )
            conn.commit()
            return {
                "accepted": True,
                "action": "CLOSE",
                "symbol": clean_sym,
                "side": "SELL",
                "quantity": close_qty,
                "price": req_px,
                "fill_price": fill_px,
                "realized_pnl_usd": realized_pnl,
                "friction": sell_friction,
                "circuit_breaker_tripped": bool(trip_now),
                "trading_date": td_str,
            }

    def update_market_price(
        self,
        symbol: str,
        price: float,
        *,
        high: Optional[float] = None,
        low: Optional[float] = None,
        timestamp: Optional[float] = None,
        auto_stop_out: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Update mark price & peak high for open position; trigger structural stop-loss exit if breached."""
        clean_sym = UsSymbol.parse(symbol).code
        px = safe_float(price, 0.0)
        if px <= 0.0:
            return None
        ts = float(timestamp) if (timestamp is not None and timestamp > 0) else self.clock.now_epoch()
        eff_high = max(px, safe_float(high, px))
        eff_low = min(px, safe_float(low, px)) if (low is not None and safe_float(low, 0.0) > 0) else px

        with self._lock:
            self._mark_prices[clean_sym] = px
            pos = self.get_position(clean_sym)
            if pos is None:
                return None

            new_peak = max(float(pos["max_high_since_entry"]), eff_high)
            if new_peak > float(pos["max_high_since_entry"]):
                with self._session() as conn:
                    conn.execute(
                        "UPDATE positions SET max_high_since_entry = ?, updated_at = ? WHERE symbol = ?",
                        (new_peak, ts, clean_sym),
                    )
                    conn.commit()

            stop_px = float(pos["stop_loss_price"])
            if auto_stop_out and stop_px > 0.0 and eff_low <= stop_px:
                exit_px = min(px, stop_px)
                return self.submit_order(
                    OrderInstruction(
                        symbol=clean_sym,
                        side="SELL",
                        action="CLOSE",
                        quantity=float(pos["quantity"]),
                        price=exit_px,
                        stop_loss_price=stop_px,
                        reason=f"STOP_LOSS (low ${eff_low:.2f} <= stop ${stop_px:.2f})",
                        timestamp=ts,
                    )
                )
        return None
