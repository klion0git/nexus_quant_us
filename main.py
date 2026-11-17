"""Async orchestration loop for Nexus US live/sandbox quantitative trading."""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from broker.paper_broker import UsPaperBroker
from feeds.base_feed import UsBrokerFeedAdapter, create_us_feed
from notification.discord_notifier import UsDiscordNotifier
from strategies.stock_us_strategy import StockUsStrategy
from strategies.us_futures_wave_filter import UsFuturesWaveFilter
from us_core.calendar import DEFAULT_US_CALENDAR, UsSessionCalendar
from us_core.clock import DEFAULT_CLOCK, ClockProtocol
from us_core.config import UsSettings, load_us_settings
from us_core.time_utils import format_et_iso

logger = logging.getLogger("nexus_quant_us.main")


class UsTradingEngine:
    """End-to-end US trading orchestrator (NYSE/NASDAQ equities + CME ES=F macro wave filter)."""

    def __init__(
        self,
        *,
        settings: Optional[UsSettings] = None,
        calendar: Optional[UsSessionCalendar] = None,
        clock: Optional[ClockProtocol] = None,
        feed: Optional[UsBrokerFeedAdapter] = None,
        broker: Optional[UsPaperBroker] = None,
        notifier: Optional[UsDiscordNotifier] = None,
    ) -> None:
        self.settings = settings if settings is not None else load_us_settings()
        self.calendar = calendar if calendar is not None else DEFAULT_US_CALENDAR
        self.clock = clock if clock is not None else DEFAULT_CLOCK

        self.broker = (
            broker
            if broker is not None
            else UsPaperBroker(
                db_path=self.settings.paper_db_path,
                initial_capital=self.settings.initial_capital,
                calendar=self.calendar,
                clock=self.clock,
                max_position_pct=self.settings.max_position_pct,
                risk_per_trade_pct=self.settings.risk_per_trade_pct,
                daily_loss_limit_pct=self.settings.daily_loss_limit_pct,
            )
        )
        self.feed = (
            feed
            if feed is not None
            else create_us_feed(
                self.settings.broker_provider,
                calendar=self.calendar,
                clock=self.clock,
                api_key=self.settings.alpaca_api_key,
                secret_key=self.settings.alpaca_secret_key,
                paper=self.settings.alpaca_paper,
                host=self.settings.ibkr_host,
                port=self.settings.ibkr_port,
                client_id=self.settings.ibkr_client_id,
                app_key=self.settings.schwab_app_key,
                app_secret=self.settings.schwab_app_secret,
            )
        )
        self.wave_filter = UsFuturesWaveFilter(
            symbol=self.settings.macro_symbol,
            timeframe="15m",
            calendar=self.calendar,
            enabled=self.settings.enable_macro_wave_filter,
        )
        self.strategy = StockUsStrategy(
            calendar=self.calendar,
            clock=self.clock,
            wave_filter=self.wave_filter,
            enable_brk=self.settings.enable_brk,
            enable_mfo=self.settings.enable_mfo,
            enable_masb=self.settings.enable_masb,
            enable_morphology=self.settings.enable_morphology,
            enable_bond_bmr=self.settings.enable_bond_bmr,
        )
        self.notifier = (
            notifier
            if notifier is not None
            else UsDiscordNotifier(self.settings.discord_webhook_url, clock=self.clock)
        )
        self._running = False

    async def warmup(self) -> Dict[str, int]:
        """Warm up CME macro wave filter and per-symbol daily/15m history buffers."""
        counts: Dict[str, int] = {}
        macro_bars = await self.feed.fetch_macro_futures_candles(self.settings.macro_symbol, days=15)
        self.wave_filter.warmup(macro_bars)
        counts[self.settings.macro_symbol] = len(macro_bars)

        for sym in self.settings.target_stocks:
            daily = await self.feed.fetch_daily_candles(sym, days=90)
            bars_15m = await self.feed.fetch_15m_candles(sym, days=70)
            self.strategy.warmup_daily_bars(sym, daily)
            loaded = self.strategy.warmup_15m_bars(sym, bars_15m)
            counts[sym] = loaded
        return counts

    async def step_once(self) -> List[Dict]:
        """Execute one tick-polling and signal-evaluation cycle across target US equities."""
        executions: List[Dict] = []
        ticks = await self.feed.poll_latest_ticks(self.settings.target_stocks)
        for tick in ticks:
            stop_report = self.broker.update_market_price(
                tick.symbol,
                tick.price,
                timestamp=tick.timestamp,
                auto_stop_out=True,
            )
            if stop_report is not None and stop_report.get("accepted"):
                executions.append(stop_report)
                await self.notifier.notify_trade(stop_report)
                continue

            open_pos = self.broker.get_position(tick.symbol)
            sig = self.strategy.on_tick(
                tick.symbol,
                tick.price,
                tick.timestamp,
                tick.volume,
                open_position=open_pos,
            )
            if sig is not None:
                td_str = self.calendar.get_trading_day(sig.timestamp, symbol=sig.symbol).isoformat()
                prev_c = self.strategy.get_latest_prev_close(sig.symbol, td_str)
                res = self.broker.execute_signal(sig, ref_prev_close=prev_c)
                if res.get("accepted"):
                    executions.append(res)
                    await self.notifier.notify_trade(res)
        return executions

    async def run(self, *, max_iterations: Optional[int] = None, poll_interval_seconds: float = 2.0) -> None:
        self._running = True
        await self.feed.start()
        try:
            now_et = format_et_iso(self.clock.now_epoch())
            logger.info("Starting Nexus US Engine at %s (provider=%s)", now_et, self.feed.provider_name)
            await self.warmup()
            iteration = 0
            while self._running:
                await self.step_once()
                iteration += 1
                if max_iterations is not None and iteration >= max_iterations:
                    break
                await asyncio.sleep(max(0.05, float(poll_interval_seconds)))
        finally:
            self._running = False
            await self.feed.stop()

    def stop(self) -> None:
        self._running = False


async def run_us_main(provider: Optional[str] = None, iterations: Optional[int] = None) -> None:
    settings = load_us_settings()
    feed = create_us_feed(provider or settings.broker_provider)
    engine = UsTradingEngine(settings=settings, feed=feed)
    await engine.run(max_iterations=iterations)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run_us_main())
