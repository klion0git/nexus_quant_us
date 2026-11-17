"""Chart strategy registry for US chart_hist."""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from chart_hist.strategies.blank import evaluate_blank_strategy
from chart_hist.strategies.masb import evaluate_masb_chart_strategy
from chart_hist.strategies.stock_us_fir import evaluate_stock_us_fir_strategy

_STRATEGY_REGISTRY: Dict[str, Dict[str, Any]] = {
    "stock_us_fir": {
        "id": "stock_us_fir",
        "name": "US Equity FIR + MA520/MA1560 (BRK / MFO / BTX)",
        "fn": evaluate_stock_us_fir_strategy,
    },
    "masb": {
        "id": "masb",
        "name": "US Moving Average Squeeze Breakout (MASB)",
        "fn": evaluate_masb_chart_strategy,
    },
    "blank": {
        "id": "blank",
        "name": "Raw Candles + Elliott Waves + POC",
        "fn": evaluate_blank_strategy,
    },
}


def list_strategies() -> List[Dict[str, str]]:
    return [{"id": v["id"], "name": v["name"]} for v in _STRATEGY_REGISTRY.values()]


def get_strategy(strategy_id: str) -> Callable[..., Dict[str, Any]]:
    key = str(strategy_id or "stock_us_fir").strip().lower()
    entry = _STRATEGY_REGISTRY.get(key)
    if entry is None:
        raise ValueError(f"Unsupported US chart strategy: {strategy_id!r}; allowed: {list(_STRATEGY_REGISTRY.keys())}")
    return entry["fn"]


__all__ = ["list_strategies", "get_strategy"]
