"""PresenceMonitor: turns the raw dry-contact input into a trustworthy
docked/away state.

Two stages:
1. *Debounce*: the raw reading must be unchanged for ``debounce_ms`` before
   the debounced value follows it (filters contact bounce).
2. *Stability*: the debounced value must hold for ``stability_s`` before it is
   *committed* as the robot state (filters charging-contact flicker while the
   robot docks or undocks).

It also detects *flapping* (too many debounced changes in a window), which is
reported as a sensor fault.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from ..clock import Clock
from ..config import SensorConfig
from .base import InputPin

log = logging.getLogger(__name__)


class Presence(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass
class PresenceChange:
    old: Presence
    new: Presence
    changed_at: datetime  # when the debounced signal actually changed


ChangeListener = Callable[[PresenceChange], None]


class PresenceMonitor:
    def __init__(
        self,
        pin: InputPin,
        clock: Clock,
        sensor_cfg: Callable[[], SensorConfig],
        closed_means_present: bool,
    ) -> None:
        self._pin = pin
        self._clock = clock
        self._cfg = sensor_cfg
        self._closed_means_present = closed_means_present
        self._listeners: list[ChangeListener] = []
        self._lock = threading.RLock()

        self._raw: bool | None = None
        self._raw_since = 0.0
        self._debounced: bool | None = None
        self._debounced_since = 0.0
        self._state = Presence.UNKNOWN
        self._state_since_mono = clock.monotonic()
        self._changes: deque[float] = deque()
        self._flapping = False
        self.read_errors = 0

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- listeners -----------------------------------------------------------
    def on_change(self, fn: ChangeListener) -> None:
        self._listeners.append(fn)

    # -- public state ----------------------------------------------------------
    @property
    def state(self) -> Presence:
        return self._state

    @property
    def raw_present(self) -> bool | None:
        return self._raw

    @property
    def pending(self) -> Presence | None:
        """Debounced value that is waiting for the stability period, if any."""
        if self._debounced is None:
            return None
        deb = Presence.PRESENT if self._debounced else Presence.ABSENT
        return deb if deb != self._state else None

    @property
    def flapping(self) -> bool:
        return self._flapping

    def settle_time_s(self) -> float:
        cfg = self._cfg()
        return cfg.debounce_ms / 1000 + cfg.stability_s

    # -- core ------------------------------------------------------------------
    def poll(self) -> None:
        try:
            closed = self._pin.read_closed()
        except Exception:
            self.read_errors += 1
            if self.read_errors in (1, 10, 100) or self.read_errors % 1000 == 0:
                log.exception("presence read failed (%d errors)", self.read_errors)
            return
        present = closed if self._closed_means_present else not closed
        now = self._clock.monotonic()
        cfg = self._cfg()
        change: PresenceChange | None = None

        with self._lock:
            if self._raw is None or present != self._raw:
                self._raw = present
                self._raw_since = now

            if self._raw != self._debounced and now - self._raw_since >= cfg.debounce_ms / 1000:
                if self._debounced is not None:
                    self._changes.append(now)
                self._debounced = self._raw
                self._debounced_since = self._raw_since

            # flapping detection
            while self._changes and now - self._changes[0] > cfg.flap_window_s:
                self._changes.popleft()
            flapping = len(self._changes) >= cfg.flap_max_changes
            if flapping != self._flapping:
                self._flapping = flapping
                log.warning("presence sensor flapping: %s", flapping)

            if self._debounced is not None:
                deb = Presence.PRESENT if self._debounced else Presence.ABSENT
                if deb != self._state and now - self._debounced_since >= cfg.stability_s:
                    old = self._state
                    self._state = deb
                    self._state_since_mono = self._debounced_since
                    changed_at = self._clock.now() - timedelta(seconds=now - self._debounced_since)
                    change = PresenceChange(old, deb, changed_at)

        if change is not None:
            for fn in list(self._listeners):
                try:
                    fn(change)
                except Exception:
                    log.exception("presence listener failed")

    # -- thread ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                self.poll()
                self._stop.wait(self._cfg().poll_interval_ms / 1000)

        self._thread = threading.Thread(target=loop, name="presence-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
