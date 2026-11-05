"""SafeRelay: the only code allowed to energize the actuator relay.

Safety layers (independent of each other):
1. Every pulse is capped at ``hard_max_on_s`` regardless of the requested duration.
2. Pulses always turn the relay off in a ``finally`` block.
3. A supervisor (thread in production, clock tick in tests) forces the relay
   OFF if it has been on longer than ``hard_max_on_s`` + margin, whatever the
   reason (bug, hung thread, ...).
4. ``force_off()`` is called on start, stop, atexit and from signal handlers;
   systemd ``ExecStopPost`` latches the pin OFF with ``pinctrl`` after exit.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from ..clock import Clock
from .base import OutputPin

log = logging.getLogger(__name__)

WATCHDOG_MARGIN_S = 0.25


@dataclass
class PulseResult:
    requested_s: float
    actual_s: float
    capped: bool
    dry_run: bool


class SafeRelay:
    def __init__(
        self,
        pin: OutputPin,
        clock: Clock,
        hard_max_on_s: Callable[[], float],
        on_fault: Callable[[str], None] | None = None,
        on_cycle: Callable[[], None] | None = None,
    ) -> None:
        self._pin = pin
        self._clock = clock
        self._hard_max = hard_max_on_s
        self._on_fault = on_fault
        self._on_cycle = on_cycle
        self._lock = threading.RLock()
        self._on_since: float | None = None
        self._dry_on = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.watchdog_trips = 0

    # -- state -------------------------------------------------------------
    @property
    def is_on(self) -> bool:
        return self._on_since is not None

    @property
    def simulated_on(self) -> bool:
        """True during a dry-run pulse (relay NOT energized)."""
        return self._dry_on

    # -- low level (private: use pulse) ---------------------------------------
    def _energize(self) -> None:
        with self._lock:
            self._on_since = self._clock.monotonic()
            self._pin.set(True)
        if self._on_cycle:
            self._on_cycle()

    def _release(self) -> None:
        with self._lock:
            try:
                self._pin.set(False)
            finally:
                self._on_since = None

    # -- public --------------------------------------------------------------
    def pulse(self, duration_s: float, dry_run: bool = False) -> PulseResult:
        hard_max = self._hard_max()
        actual = max(0.0, min(duration_s, hard_max))
        capped = actual < duration_s
        if capped:
            log.warning("pulse of %.2fs capped to hard max %.2fs", duration_s, hard_max)
        if dry_run:
            self._dry_on = True
            try:
                self._clock.sleep(actual)
            finally:
                self._dry_on = False
            return PulseResult(duration_s, actual, capped, True)
        self._energize()
        try:
            self._clock.sleep(actual)
        finally:
            self._release()
        return PulseResult(duration_s, actual, capped, False)

    def force_off(self, reason: str = "") -> None:
        try:
            self._release()
        except Exception:
            log.exception("force_off failed (%s)", reason)
        else:
            if reason:
                log.info("relay forced OFF (%s)", reason)

    def check_watchdog(self) -> bool:
        """Force OFF if on too long. Returns True if it tripped."""
        since = self._on_since
        if since is None:
            return False
        limit = self._hard_max() + WATCHDOG_MARGIN_S
        if self._clock.monotonic() - since <= limit:
            return False
        self.force_off("watchdog")
        self.watchdog_trips += 1
        msg = f"Relay watchdog tripped: relay was on longer than {limit:.2f}s and was forced OFF"
        log.error(msg)
        if self._on_fault:
            self._on_fault(msg)
        return True

    # -- supervisor thread ---------------------------------------------------
    def start_supervisor(self, interval_s: float = 0.05) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(interval_s):
                try:
                    self.check_watchdog()
                except Exception:
                    log.exception("relay supervisor error")

        self._thread = threading.Thread(target=loop, name="relay-watchdog", daemon=True)
        self._thread.start()

    def stop_supervisor(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
