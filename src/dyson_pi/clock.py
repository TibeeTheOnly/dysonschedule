"""Injectable time source.

Everything that waits or timestamps goes through a :class:`Clock` so tests can
run multi-minute press sequences instantly with :class:`FakeClock`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current wall-clock time (timezone-aware, UTC)."""

    def monotonic(self) -> float:
        """Monotonic seconds, for measuring intervals."""

    def sleep(self, seconds: float) -> None:
        """Block for ``seconds``."""


class SystemClock:
    """Real time."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


TickListener = Callable[[], None]


class FakeClock:
    """Deterministic clock for tests and simulations.

    ``sleep``/``advance`` move time forward in small steps and call every
    registered tick listener after each step. That is how the presence monitor
    and relay watchdog "run" during a test without threads.
    """

    def __init__(
        self,
        start: datetime | None = None,
        step_s: float = 0.02,
    ) -> None:
        self._start_wall = start or datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
        self._mono = 1000.0
        self._mono_origin = self._mono
        self.step_s = step_s
        self._listeners: list[TickListener] = []
        self._lock = threading.RLock()

    def now(self) -> datetime:
        return self._start_wall + timedelta(seconds=self._mono - self._mono_origin)

    def monotonic(self) -> float:
        return self._mono

    def add_tick_listener(self, fn: TickListener) -> None:
        self._listeners.append(fn)

    def advance(self, seconds: float) -> None:
        with self._lock:
            remaining = seconds
            while remaining > 1e-9:
                dt = min(self.step_s, remaining)
                self._mono += dt
                remaining -= dt
                for fn in list(self._listeners):
                    fn()

    def sleep(self, seconds: float) -> None:
        self.advance(max(0.0, seconds))

    def set_wall(self, when: datetime) -> None:
        """Jump wall time without advancing monotonic time (like an NTP step)."""
        self._start_wall = when - timedelta(seconds=self._mono - self._mono_origin)
