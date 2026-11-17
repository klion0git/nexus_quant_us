"""US Discord Webhook notifier formatting USD executions, SEC/TAF friction, and ES=F wave regimes."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import aiohttp

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import MacroRegimeSnapshot
from nexus_quant_core.math_utils import safe_float
from us_core.clock import DEFAULT_CLOCK, ClockProtocol
from us_core.time_utils import format_et_iso

logger = logging.getLogger("nexus_quant_us.discord")


class UsDiscordNotifier:
    """Async Discord webhook client tailored for US equities (USD) & CME futures macro alerts."""

    def __init__(
        self,
        webhook_url: str = "",
        *,
        clock: Optional[ClockProtocol] = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.webhook_url = str(webhook_url or "").strip()
        self.clock = clock if clock is not None else DEFAULT_CLOCK
        self.timeout_seconds = float(timeout_seconds)
        self.sent_history: List[Dict[str, Any]] = []

    @property
    def is_configured(self) -> bool:
        return self.webhook_url.startswith("https://") or self.webhook_url.startswith("http://")

    def format_trade_embed(self, execution_report: Dict[str, Any]) -> Dict[str, Any]:
        action = str(execution_report.get("action", "OPEN")).upper()
        side = str(execution_report.get("side", "BUY")).upper()
        symbol = str(execution_report.get("symbol", "UNKNOWN")).upper()
        qty = int(execution_report.get("quantity", 0))
        px = safe_float(execution_report.get("price", 0.0), 0.0)
        stop_px = safe_float(execution_report.get("stop_loss_price", 0.0), 0.0)
        pnl = safe_float(execution_report.get("realized_pnl_usd", 0.0), 0.0)
        friction = execution_report.get("friction") or {}
        sec_fee = safe_float(friction.get("tax", 0.0), 0.0)
        total_fee = safe_float(friction.get("total", 0.0), 0.0)
        ts = safe_float(execution_report.get("timestamp", self.clock.now_epoch()), self.clock.now_epoch())
        et_str = format_et_iso(ts)

        if action == "OPEN":
            color = 0x10B981  # Emerald green
            title = f"[US OPEN] {side} {symbol} — {qty} sh @ ${px:,.2f} USD"
            desc = (
                f"**Notional**: `${px * qty:,.2f} USD`\n"
                f"**Structural Stop**: `${stop_px:,.2f} USD`\n"
                f"**Est. Friction**: `${total_fee:,.4f} USD`\n"
                f"**NY Time**: `{et_str}`"
            )
        else:
            color = 0x2563EB if pnl >= 0.0 else 0xEF4444
            pnl_sign = "+" if pnl >= 0.0 else "-"
            title = f"[US CLOSE] {side} {symbol} — {qty} sh @ ${px:,.2f} USD ({pnl_sign}${abs(pnl):,.2f})"
            desc = (
                f"**Realized PnL**: `{pnl_sign}${abs(pnl):,.2f} USD`\n"
                f"**SEC/TAF Fee**: `${sec_fee:,.4f} USD` | **Total Friction**: `${total_fee:,.4f} USD`\n"
                f"**NY Time**: `{et_str}`"
            )

        return {
            "username": "Nexus US Quant",
            "embeds": [
                {
                    "title": title,
                    "description": desc,
                    "color": color,
                }
            ],
        }

    def format_regime_embed(self, macro_symbol: str, regime: MacroRegimeSnapshot) -> Dict[str, Any]:
        ts = regime.updated_at_ts if regime.updated_at_ts > 0 else self.clock.now_epoch()
        et_str = format_et_iso(ts)
        color_map = {"BULLISH": 0x10B981, "BEARISH": 0xEF4444, "NEUTRAL": 0x6B7280}
        fib_str = f"{regime.fib_level * 100:.1f}%" if regime.fib_level is not None else "N/A"
        return {
            "username": "Nexus US Quant",
            "embeds": [
                {
                    "title": f"[CME MACRO] {macro_symbol} -> {regime.trend} ({regime.wave_stage})",
                    "description": (
                        f"**Description**: {regime.description}\n"
                        f"**Wave Fib Ratio**: `{fib_str}`\n"
                        f"**NY Time**: `{et_str}`"
                    ),
                    "color": color_map.get(regime.trend, 0x6B7280),
                }
            ],
        }

    async def send_payload(self, payload: Dict[str, Any]) -> bool:
        self.sent_history.append(payload)
        if not self.is_configured:
            return False
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.webhook_url, json=payload) as resp:
                    return 200 <= resp.status < 300
        except Exception as exc:
            logger.warning("Discord webhook delivery failed: %s", exc)
            return False

    async def notify_trade(self, execution_report: Dict[str, Any]) -> bool:
        return await self.send_payload(self.format_trade_embed(execution_report))

    async def notify_macro_regime(self, macro_symbol: str, regime: MacroRegimeSnapshot) -> bool:
        return await self.send_payload(self.format_regime_embed(macro_symbol, regime))
