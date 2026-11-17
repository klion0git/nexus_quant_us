"""Central clock module for nexus_quant_us.

ARCHITECTURAL RULE: This is the ONLY module in nexus_quant_us permitted to call
stdlib wall/monotonic clock primitives. All other modules must accept an
injected ClockProtocol or use DEFAULT_CLOCK.
"""
from __future__ import annotations

import threading
import time

from us_core._bootstrap import ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from nexus_quant_core.contracts import ClockProtocol


class SystemClock(ClockProtocol):
    """Production UTC epoch & monotonic clock backed by Python stdlib time."""

    def now_epoch(self) -> float:
        return float(time.time())

    def now_monotonic(self) -> float:
        return float(time.monotonic())


class SimulatedClock(ClockProtocol):
    """Deterministic controllable clock for unit tests, replay, and sandbox runs."""

    def __init__(self, initial_epoch: float = 1763391000.0, initial_monotonic: float = 1000.0) -> None:
        self._lock = threading.RLock()
        self._epoch = float(initial_epoch)
        self._monotonic = float(initial_monotonic)

    def now_epoch(self) -> float:
        with self._lock:
            return self._epoch

    def now_monotonic(self) -> float:
        with self._lock:
            return self._monotonic

    def set_epoch(self, new_epoch: float) -> None:
        with self._lock:
            delta = float(new_epoch) - self._epoch
            self._epoch = float(new_epoch)
            if delta > 0.0:
                self._monotonic += delta

    def advance(self, seconds: float) -> float:
        step = float(seconds)
        with self._lock:
            self._epoch += step
            self._monotonic += max(0.0, step)
            return self._epoch


DEFAULT_CLOCK: ClockProtocol = SystemClock()

__all__ = ["ClockProtocol", "SystemClock", "SimulatedClock", "DEFAULT_CLOCK"]
