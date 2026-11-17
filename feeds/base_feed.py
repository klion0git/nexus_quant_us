"""US market data feed adapters implementing BrokerFeedProtocol.

Includes:
- UsBrokerFeedAdapter (abstract base class)
- SandboxUsFeed (deterministic US equities + CME Globex ES=F/NQ=F futures generator)
- YFinanceUsFeed (live/delayed Yahoo Finance US equities & CME futures adapter)
- IbkrUsFeedStub / AlpacaUsFeedStub / SchwabUsFeedStub (documented broker plug-in skeletons)
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import math
import random
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import BrokerFeedProtocol, CandleBar, MarketTick
from nexus_quant_core.math_utils import safe_float
from us_core.calendar import DEFAULT_US_CALENDAR, UsSessionCalendar
from us_core.clock import DEFAULT_CLOCK, ClockProtocol
from us_core.config import TZ_UTC
from us_core.symbol import UsSymbol


_DEFAULT_BASE_PRICES: Dict[str, float] = {
    "NVDA": 138.0,
    "AAPL": 228.0,
    "MSFT": 425.0,
    "TSLA": 320.0,
    "META": 590.0,
    "SPY": 595.0,
    "QQQ": 510.0,
    "TLT": 92.5,
    "IEF": 94.0,
    "SHY": 82.0,
    "BRK-B": 465.0,
    "ES=F": 5980.0,
    "NQ=F": 21150.0,
    "YM=F": 43900.0,
    "RTY=F": 2350.0,
}


def _symbol_seed(symbol: str, base_seed: int = 42) -> int:
    digest = hashlib.sha256(f"{symbol}:{base_seed}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


class UsBrokerFeedAdapter(BrokerFeedProtocol, ABC):
    """Base adapter for US market data feeds (NYSE/NASDAQ equities & CME Globex futures)."""

    provider_name: str = "base"

    def __init__(
        self,
        *,
        calendar: Optional[UsSessionCalendar] = None,
        clock: Optional[ClockProtocol] = None,
    ) -> None:
        self.calendar = calendar if calendar is not None else DEFAULT_US_CALENDAR
        self.clock = clock if clock is not None else DEFAULT_CLOCK
        self.running: bool = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    @abstractmethod
    async def fetch_daily_candles(self, symbol: str, days: int) -> List[CandleBar]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_15m_candles(self, symbol: str, days: int) -> List[CandleBar]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_macro_futures_candles(self, symbol: str, days: int) -> List[CandleBar]:
        raise NotImplementedError

    async def poll_latest_ticks(self, symbols: Sequence[str]) -> List[MarketTick]:
        """Poll latest market ticks for target US symbols."""
        return []


class SandboxUsFeed(UsBrokerFeedAdapter):
    """Deterministic zero-network US sandbox feed for equities, Treasury ETFs, and CME futures."""

    provider_name = "sandbox"

    def __init__(
        self,
        *,
        calendar: Optional[UsSessionCalendar] = None,
        clock: Optional[ClockProtocol] = None,
        seed: int = 42,
    ) -> None:
        super().__init__(calendar=calendar, clock=clock)
        self.seed = int(seed)
        self._tick_step: int = 0
        self._last_prices: Dict[str, float] = {}

    def _base_price(self, clean_sym: str) -> float:
        if clean_sym in _DEFAULT_BASE_PRICES:
            return _DEFAULT_BASE_PRICES[clean_sym]
        rng = random.Random(_symbol_seed(clean_sym, self.seed))
        return round(rng.uniform(45.0, 350.0), 2)

    def _collect_trading_days(self, end_date: datetime.date, count: int, symbol: str) -> List[datetime.date]:
        days_out: List[datetime.date] = []
        cur = end_date
        while len(days_out) < max(1, count):
            if self.calendar.is_trading_day(cur, symbol=symbol):
                days_out.append(cur)
            cur -= datetime.timedelta(days=1)
        days_out.reverse()
        return days_out

    async def fetch_daily_candles(self, symbol: str, days: int) -> List[CandleBar]:
        sym_info = UsSymbol.parse(symbol)
        clean = sym_info.code
        end_td = self.calendar.get_trading_day(self.clock.now_epoch(), symbol=clean)
        trading_days = self._collect_trading_days(end_td, max(5, int(days)), clean)

        rng = random.Random(_symbol_seed(clean, self.seed + 101))
        px = self._base_price(clean) * 0.90
        vol_base = 45_000_000.0 if not sym_info.is_futures else 1_200_000.0
        bars: List[CandleBar] = []

        for idx, d in enumerate(trading_days):
            wave = math.sin(idx / 9.0) * 0.008 + 0.0012
            ret = wave + rng.gauss(0.0, 0.011 if not sym_info.is_bond else 0.005)
            open_px = round(max(1.0, px * (1.0 + rng.gauss(0.0, 0.003))), 2)
            close_px = round(max(1.0, open_px * (1.0 + ret)), 2)
            span = max(0.05, open_px * abs(rng.gauss(0.010, 0.004)))
            high_px = round(max(open_px, close_px) + span * 0.6, 2)
            low_px = round(max(0.5, min(open_px, close_px) - span * 0.6), 2)
            vol = round(vol_base * rng.uniform(0.70, 1.55))
            ts_utc = int(datetime.datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ_UTC).timestamp())
            bars.append(
                CandleBar(
                    symbol=clean,
                    timeframe="1d",
                    timestamp=ts_utc,
                    trading_date=d.isoformat(),
                    open=open_px,
                    high=high_px,
                    low=low_px,
                    close=close_px,
                    volume=float(vol),
                )
            )
            px = close_px

        self._last_prices[clean] = px
        return bars

    async def fetch_15m_candles(self, symbol: str, days: int) -> List[CandleBar]:
        sym_info = UsSymbol.parse(symbol)
        if sym_info.is_futures:
            return await self.fetch_macro_futures_candles(symbol, days)

        clean = sym_info.code
        end_td = self.calendar.get_trading_day(self.clock.now_epoch(), symbol=clean)
        trading_days = self._collect_trading_days(end_td, max(2, int(days)), clean)
        rng = random.Random(_symbol_seed(clean, self.seed + 202))
        px = self._base_price(clean) * 0.92
        bars: List[CandleBar] = []

        for day_idx, d in enumerate(trading_days):
            close_h, close_m = self.calendar.get_regular_close_hm(d)
            start_dt = datetime.datetime(d.year, d.month, d.day, 9, 30, 0, tzinfo=self.calendar.timezone)
            end_dt = datetime.datetime(d.year, d.month, d.day, close_h, close_m, 0, tzinfo=self.calendar.timezone)
            cur_ts = int(start_dt.timestamp())
            end_ts = int(end_dt.timestamp())
            bar_in_day = 0

            while cur_ts < end_ts:
                drift = 0.00025 + 0.0006 * math.sin((day_idx * 26 + bar_in_day) / 35.0)
                noise = rng.gauss(0.0, 0.0022 if not sym_info.is_bond else 0.0010)
                open_px = round(max(1.0, px), 2)
                close_px = round(max(1.0, open_px * (1.0 + drift + noise)), 2)
                wick = max(0.02, open_px * rng.uniform(0.001, 0.0035))
                high_px = round(max(open_px, close_px) + wick, 2)
                low_px = round(max(0.5, min(open_px, close_px) - wick), 2)
                # U-shaped US intraday volume curve (higher at 09:30 open & 15:45 close)
                u_mult = 1.8 if bar_in_day in (0, 1, 24, 25) else 1.0
                vol = round(1_250_000.0 * u_mult * rng.uniform(0.75, 1.35))
                bars.append(
                    CandleBar(
                        symbol=clean,
                        timeframe="15m",
                        timestamp=cur_ts,
                        trading_date=d.isoformat(),
                        open=open_px,
                        high=high_px,
                        low=low_px,
                        close=close_px,
                        volume=float(vol),
                    )
                )
                px = close_px
                cur_ts += 900
                bar_in_day += 1

        self._last_prices[clean] = px
        return bars

    async def fetch_macro_futures_candles(self, symbol: str, days: int) -> List[CandleBar]:
        """Generate CME Globex 23-hour 15m bars (18:00 ET -> 17:00 ET, skipping 17:00~18:00 ET break)."""
        sym_info = UsSymbol.parse(symbol)
        clean = sym_info.code if sym_info.is_futures else "ES=F"
        end_td = self.calendar.get_trading_day(self.clock.now_epoch(), symbol=clean)
        trading_days = self._collect_trading_days(end_td, max(2, int(days)), clean)
        rng = random.Random(_symbol_seed(clean, self.seed + 303))
        px = self._base_price(clean)
        bars: List[CandleBar] = []

        for d_idx, td in enumerate(trading_days):
            # CME session for trading_date `td` starts on the prior calendar evening at 18:00 ET
            # (for Monday, starts Sunday 18:00 ET; for Tue-Fri, starts prior evening 18:00 ET)
            prev_cal = td - datetime.timedelta(days=1)
            while prev_cal.weekday() == 5:  # Skip Saturday so Monday's Globex session starts Sunday 18:00 ET
                prev_cal -= datetime.timedelta(days=1)
            sess_open_dt = datetime.datetime(
                prev_cal.year, prev_cal.month, prev_cal.day, 18, 0, 0, tzinfo=self.calendar.timezone
            )
            sess_close_dt = datetime.datetime(
                td.year, td.month, td.day, 17, 0, 0, tzinfo=self.calendar.timezone
            )
            cur_ts = int(sess_open_dt.timestamp())
            end_ts = int(sess_close_dt.timestamp())
            step_idx = 0

            while cur_ts < end_ts:
                sess_type = self.calendar.get_session_type(float(cur_ts), symbol=clean)
                if sess_type in ("GLOBEX", "REGULAR"):
                    wave = 0.0003 * math.sin((d_idx * 92 + step_idx) / 18.0) + 0.00008
                    open_px = round(px, 2)
                    close_px = round(max(10.0, open_px * (1.0 + wave + rng.gauss(0.0, 0.0009))), 2)
                    wick = max(0.25, open_px * 0.0008)
                    high_px = round(max(open_px, close_px) + wick, 2)
                    low_px = round(min(open_px, close_px) - wick, 2)
                    vol = round((38_000.0 if sess_type == "REGULAR" else 8_500.0) * rng.uniform(0.8, 1.3))
                    mapped_td = self.calendar.get_trading_day(float(cur_ts), symbol=clean).isoformat()
                    bars.append(
                        CandleBar(
                            symbol=clean,
                            timeframe="15m",
                            timestamp=cur_ts,
                            trading_date=mapped_td,
                            open=open_px,
                            high=high_px,
                            low=low_px,
                            close=close_px,
                            volume=float(vol),
                        )
                    )
                    px = close_px
                cur_ts += 900
                step_idx += 1

        self._last_prices[clean] = px
        return bars

    async def poll_latest_ticks(self, symbols: Sequence[str]) -> List[MarketTick]:
        self._tick_step += 1
        now_ts = self.clock.now_epoch()
        ticks: List[MarketTick] = []
        for raw_sym in symbols:
            clean = UsSymbol.parse(raw_sym).code
            rng = random.Random(_symbol_seed(clean, self.seed + self._tick_step))
            prev = self._last_prices.get(clean, self._base_price(clean))
            next_px = round(max(0.5, prev * (1.0 + rng.gauss(0.0001, 0.0012))), 2)
            self._last_prices[clean] = next_px
            spread = 0.02 if next_px < 200 else 0.05
            ticks.append(
                MarketTick(
                    symbol=clean,
                    price=next_px,
                    volume=float(rng.randint(100, 3500)),
                    timestamp=now_ts,
                    bid=round(next_px - spread / 2.0, 2),
                    ask=round(next_px + spread / 2.0, 2),
                    source="sandbox",
                )
            )
        return ticks


class YFinanceUsFeed(UsBrokerFeedAdapter):
    """Yahoo Finance market feed adapter for US equities, ETFs, and CME index futures."""

    provider_name = "yfinance"

    def _download_sync(self, symbol: str, interval: str, days: int) -> List[CandleBar]:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError("yfinance package is required for YFinanceUsFeed") from exc

        sym_info = UsSymbol.parse(symbol)
        clean = sym_info.code
        eff_days = max(1, int(days))
        if interval == "15m":
            eff_days = min(eff_days, 59)
        period_str = f"{eff_days}d"

        ticker = yf.Ticker(sym_info.yf_ticker)
        df = ticker.history(period=period_str, interval=interval, auto_adjust=False, prepost=sym_info.is_futures)
        if df is None or df.empty:
            return []

        norm_tf = "1d" if interval == "1d" else "15m"
        out: List[CandleBar] = []
        for idx_ts, row in df.iterrows():
            if hasattr(idx_ts, "timestamp"):
                ts_val = int(idx_ts.timestamp())
            else:
                continue
            cl = safe_float(row.get("Close"), 0.0)
            if cl <= 0.0:
                continue
            op = safe_float(row.get("Open"), cl)
            hi = max(op, cl, safe_float(row.get("High"), cl))
            lo = min(op, cl, safe_float(row.get("Low"), cl))
            vol = max(0.0, safe_float(row.get("Volume"), 0.0))

            if norm_tf == "1d":
                dt_et = self.calendar.to_et_datetime(float(ts_val))
                td_str = dt_et.date().isoformat()
                ts_val = int(
                    datetime.datetime(dt_et.year, dt_et.month, dt_et.day, 0, 0, 0, tzinfo=TZ_UTC).timestamp()
                )
            else:
                if not sym_info.is_futures and self.calendar.get_session_type(float(ts_val), symbol=clean) != "REGULAR":
                    continue
                td_str = self.calendar.get_trading_day(float(ts_val), symbol=clean).isoformat()

            out.append(
                CandleBar(
                    symbol=clean,
                    timeframe=norm_tf,
                    timestamp=ts_val,
                    trading_date=td_str,
                    open=round(op, 4),
                    high=round(hi, 4),
                    low=round(lo, 4),
                    close=round(cl, 4),
                    volume=vol,
                )
            )
        return out

    async def fetch_daily_candles(self, symbol: str, days: int) -> List[CandleBar]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_sync, symbol, "1d", days)

    async def fetch_15m_candles(self, symbol: str, days: int) -> List[CandleBar]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_sync, symbol, "15m", days)

    async def fetch_macro_futures_candles(self, symbol: str, days: int) -> List[CandleBar]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_sync, symbol, "15m", days)


class IbkrUsFeedStub(UsBrokerFeedAdapter):
    """Plug-in skeleton for Interactive Brokers TWS / IB Gateway (US SMART equities & CME Globex futures).

    Integration guide:
    - Connect via ib_insync / ibapi to (self.host, self.port, clientId=self.client_id).
    - Map US equities with Stock(symbol, 'SMART', 'USD') and CME futures with ContFuture('ES', 'CME').
    - Request historical bars with useRTH=True for equities and useRTH=False for CME Globex ES/NQ.
    """

    provider_name = "ibkr"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 10,
        calendar: Optional[UsSessionCalendar] = None,
        clock: Optional[ClockProtocol] = None,
    ) -> None:
        super().__init__(calendar=calendar, clock=clock)
        self.host = host
        self.port = int(port)
        self.client_id = int(client_id)

    async def fetch_daily_candles(self, symbol: str, days: int) -> List[CandleBar]:
        raise NotImplementedError(
            f"IbkrUsFeedStub ({self.host}:{self.port}) is a plug-in skeleton; use US_BROKER_PROVIDER=sandbox or yfinance."
        )

    async def fetch_15m_candles(self, symbol: str, days: int) -> List[CandleBar]:
        raise NotImplementedError("IbkrUsFeedStub is a plug-in skeleton; connect TWS/IB Gateway client to activate.")

    async def fetch_macro_futures_candles(self, symbol: str, days: int) -> List[CandleBar]:
        raise NotImplementedError("IbkrUsFeedStub CME Globex feed requires active IB Gateway session.")


class AlpacaUsFeedStub(UsBrokerFeedAdapter):
    """Plug-in skeleton for Alpaca Markets v2 REST & WebSocket API (IEX / SIP US equities).

    Integration guide:
    - Authenticate with APCA-API-KEY-ID and APCA-API-SECRET-KEY against data.alpaca.markets/v2.
    - Subscribe to wss://stream.data.alpaca.markets/v2/iex (or /sip) for real-time trades & bars.
    """

    provider_name = "alpaca"

    def __init__(
        self,
        *,
        api_key: str = "",
        secret_key: str = "",
        paper: bool = True,
        calendar: Optional[UsSessionCalendar] = None,
        clock: Optional[ClockProtocol] = None,
    ) -> None:
        super().__init__(calendar=calendar, clock=clock)
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = bool(paper)

    async def fetch_daily_candles(self, symbol: str, days: int) -> List[CandleBar]:
        raise NotImplementedError("AlpacaUsFeedStub is a plug-in skeleton; configure ALPACA_API_KEY and REST client.")

    async def fetch_15m_candles(self, symbol: str, days: int) -> List[CandleBar]:
        raise NotImplementedError("AlpacaUsFeedStub is a plug-in skeleton; configure ALPACA_API_KEY and REST client.")

    async def fetch_macro_futures_candles(self, symbol: str, days: int) -> List[CandleBar]:
        raise NotImplementedError("AlpacaUsFeedStub requires CME futures bridge or YFinanceUsFeed fallback.")


class SchwabUsFeedStub(UsBrokerFeedAdapter):
    """Plug-in skeleton for Charles Schwab Trader API (OAuth2 REST & Streamer for US equities & /ES futures)."""

    provider_name = "schwab"

    def __init__(
        self,
        *,
        app_key: str = "",
        app_secret: str = "",
        calendar: Optional[UsSessionCalendar] = None,
        clock: Optional[ClockProtocol] = None,
    ) -> None:
        super().__init__(calendar=calendar, clock=clock)
        self.app_key = app_key
        self.app_secret = app_secret

    async def fetch_daily_candles(self, symbol: str, days: int) -> List[CandleBar]:
        raise NotImplementedError("SchwabUsFeedStub is a plug-in skeleton; configure SCHWAB_APP_KEY OAuth2 token.")

    async def fetch_15m_candles(self, symbol: str, days: int) -> List[CandleBar]:
        raise NotImplementedError("SchwabUsFeedStub is a plug-in skeleton; configure SCHWAB_APP_KEY OAuth2 token.")

    async def fetch_macro_futures_candles(self, symbol: str, days: int) -> List[CandleBar]:
        raise NotImplementedError("SchwabUsFeedStub LEVELONE_FUTURES streamer requires active OAuth2 token.")


def create_us_feed(
    provider: str = "sandbox",
    *,
    calendar: Optional[UsSessionCalendar] = None,
    clock: Optional[ClockProtocol] = None,
    **kwargs: Any,
) -> UsBrokerFeedAdapter:
    """Factory creating the requested US market feed adapter."""
    key = str(provider or "sandbox").strip().lower()
    if key == "sandbox":
        return SandboxUsFeed(calendar=calendar, clock=clock, seed=int(kwargs.get("seed", 42)))
    if key == "yfinance":
        return YFinanceUsFeed(calendar=calendar, clock=clock)
    if key == "ibkr":
        return IbkrUsFeedStub(
            host=str(kwargs.get("host", "127.0.0.1")),
            port=int(kwargs.get("port", 7497)),
            client_id=int(kwargs.get("client_id", 10)),
            calendar=calendar,
            clock=clock,
        )
    if key == "alpaca":
        return AlpacaUsFeedStub(
            api_key=str(kwargs.get("api_key", "")),
            secret_key=str(kwargs.get("secret_key", "")),
            paper=bool(kwargs.get("paper", True)),
            calendar=calendar,
            clock=clock,
        )
    if key == "schwab":
        return SchwabUsFeedStub(
            app_key=str(kwargs.get("app_key", "")),
            app_secret=str(kwargs.get("app_secret", "")),
            calendar=calendar,
            clock=clock,
        )
    raise ValueError(f"Unsupported US broker feed provider: {provider!r}; expected sandbox|yfinance|ibkr|alpaca|schwab")
