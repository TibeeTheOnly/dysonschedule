"""Mock backend for laptop development and tests.

``MockInput`` can be toggled by hand (dashboard Simulator panel), or driven by
``SimulatedRobot``, a toy model of the Vis Nav: it falls asleep after
inactivity, needs a wake press when asleep, leaves the dock a few seconds after
a start press and comes back after a configurable cleaning time.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from ..clock import Clock


class MockOutput:
    def __init__(self) -> None:
        self._active = False
        self.history: list[tuple[float, bool]] = []
        self._listeners: list[Callable[[bool], None]] = []
        self._clock: Clock | None = None

    def bind_clock(self, clock: Clock) -> None:
        self._clock = clock

    def on_change(self, fn: Callable[[bool], None]) -> None:
        self._listeners.append(fn)

    def set(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        self.history.append((self._clock.monotonic() if self._clock else 0.0, active))
        for fn in list(self._listeners):
            fn(active)

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def activations(self) -> int:
        return sum(1 for _, a in self.history if a)

    def close(self) -> None:
        self.set(False)


class MockInput:
    def __init__(self, closed: bool = True) -> None:
        self.closed = closed
        self._lock = threading.Lock()

    def set_closed(self, closed: bool) -> None:
        with self._lock:
            self.closed = closed

    def read_closed(self) -> bool:
        with self._lock:
            return self.closed

    def close(self) -> None:
        pass


@dataclass
class SimulatedRobot:
    """Toy Vis Nav reacting to presses on a MockOutput."""

    clock: Clock
    relay: MockOutput
    sensor: MockInput
    closed_means_present: bool = True
    sleep_after_s: float = 30 * 60
    depart_delay_s: float = 4.0
    clean_duration_s: float = 120.0
    min_press_s: float = 0.3
    enabled: bool = False
    docked: bool = True
    awake: bool = False
    _last_activity: float = field(default=0.0, init=False)
    _press_started: float | None = field(default=None, init=False)
    _depart_at: float | None = field(default=None, init=False)
    _return_at: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._last_activity = self.clock.monotonic()
        self.relay.on_change(self._on_relay)
        self._write_sensor()

    def _write_sensor(self) -> None:
        self.sensor.set_closed(self.docked if self.closed_means_present else not self.docked)

    def _on_relay(self, active: bool) -> None:
        now = self.clock.monotonic()
        if active:
            self._press_started = now
            return
        started, self._press_started = self._press_started, None
        if not self.enabled or started is None or now - started < self.min_press_s:
            return
        self._last_activity = now
        if not self.docked:
            return  # a press while cleaning would pause it; the app must never do this
        if not self.awake:
            self.awake = True  # first press only wakes
        elif self._depart_at is None:
            self._depart_at = now + self.depart_delay_s

    def tick(self) -> None:
        if not self.enabled:
            return
        now = self.clock.monotonic()
        if self.awake and self.docked and self._depart_at is None and now - self._last_activity > self.sleep_after_s:
            self.awake = False
        if self._depart_at is not None and now >= self._depart_at:
            self._depart_at = None
            self.docked = False
            self._return_at = now + self.clean_duration_s
            self._write_sensor()
        if self._return_at is not None and now >= self._return_at:
            self._return_at = None
            self.docked = True
            self._last_activity = now
            self._write_sensor()

    def describe(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "docked": self.docked,
            "awake": self.awake,
            "clean_duration_s": self.clean_duration_s,
        }
