"""ScheduleRunner: fires due schedules through the Actuator.

* Waits for NTP sync before doing anything (the Pi has no RTC: until NTP syncs,
  the clock may be days off).
* Next-run times are computed from "now" when the runner (re)arms, so runs
  missed while the Pi was off, or before NTP synced, are skipped, never
  fired late.
* A run is fired only if we notice it within ``misfire_grace_s``; otherwise
  it is logged as missed and skipped.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..clock import Clock
from ..config import AppConfig
from ..events import EventBus, iso
from ..logic.actuator import Actuator
from ..logic.models import Outcome
from .recurrence import describe_days, next_occurrence, parse_time
from .store import Schedule, ScheduleStore

log = logging.getLogger(__name__)


class ScheduleRunner:
    def __init__(
        self,
        store: ScheduleStore,
        actuator: Actuator,
        bus: EventBus,
        clock: Clock,
        cfg: Callable[[], AppConfig],
        ntp_synced: Callable[[], bool | None],
    ) -> None:
        self.store, self.actuator, self.bus, self.clock, self._cfg = store, actuator, bus, clock, cfg
        self._ntp_synced = ntp_synced
        self._next: dict[int, datetime] = {}
        self._armed = False
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_ntp_check = -1e9
        self._ntp_ok = False

    # -- NTP gate -------------------------------------------------------------
    def time_trusted(self) -> bool:
        if not self._cfg().scheduler.require_ntp_sync:
            return True
        if self._ntp_ok:
            return True
        now = self.clock.monotonic()
        if now - self._last_ntp_check >= 5.0:
            self._last_ntp_check = now
            synced = self._ntp_synced()
            if synced:
                self._ntp_ok = True
                self.bus.emit("scheduler.ntp", f"System clock synchronized via NTP: {self.clock.now().isoformat()}")
        return self._ntp_ok

    @property
    def waiting_for_ntp(self) -> bool:
        return not self._armed and self._cfg().scheduler.require_ntp_sync and not self._ntp_ok

    # -- arming ------------------------------------------------------------------
    def _compute(self, s: Schedule, after: datetime) -> datetime:
        return next_occurrence(after, s.days, parse_time(s.time), self._cfg().tz)

    def rearm(self) -> None:
        """Recompute next runs from now (call after any schedule change)."""
        with self._lock:
            if not self.time_trusted():
                self._armed = False
                return
            now = self.clock.now()
            self._next = {s.id: self._compute(s, now) for s in self.store.list() if s.enabled}
            if not self._armed:
                self._armed = True
                self.bus.emit("scheduler.armed", f"Scheduler armed with {len(self._next)} active schedule(s)")

    def next_runs(self) -> dict[int, datetime]:
        with self._lock:
            if self._armed:
                return dict(self._next)
        # Not armed yet (waiting for NTP): show provisional times for the UI.
        now = self.clock.now()
        return {s.id: self._compute(s, now) for s in self.store.list() if s.enabled}

    def upcoming(self, limit: int = 10) -> list[dict[str, Any]]:
        nr = self.next_runs()
        out = []
        for s in self.store.list():
            d = s.to_dict()
            d["next_run"] = iso(nr.get(s.id)) if s.id in nr else None
            d["days_label"] = describe_days(s.days)
            out.append(d)
        out.sort(key=lambda x: (x["next_run"] is None, x["next_run"] or ""))
        return out[:limit] if limit else out

    # -- tick ------------------------------------------------------------------------
    def tick(self) -> None:
        with self._lock:
            if not self._armed:
                self.rearm()
                if not self._armed:
                    return
            now = self.clock.now()
            grace = self._cfg().scheduler.misfire_grace_s
            due: list[tuple[Schedule, datetime]] = []
            for sid, when in list(self._next.items()):
                if when > now:
                    continue
                s = self.store.get(sid)
                if s is None or not s.enabled:
                    self._next.pop(sid, None)
                    continue
                self._next[sid] = self._compute(s, now)
                lateness = (now - when).total_seconds()
                if lateness > grace:
                    self.bus.emit("schedule.missed",
                                  f"Schedule '{s.name}' ({when.astimezone(self._cfg().tz):%a %H:%M}) missed by "
                                  f"{lateness:.0f}s - skipped, not fired late", level="warning", schedule_id=sid)
                    continue
                due.append((s, when))
        for s, when in due:  # outside the lock: a smart start takes a while
            self._fire(s, when)

    def _fire(self, s: Schedule, when: datetime) -> None:
        self.bus.emit("schedule.fire", f"Running schedule '{s.name}' ({s.mode.value})", schedule_id=s.id)
        attempt = self.actuator.submit(s.mode, trigger=f"schedule:{s.id}", wait=True)
        if attempt.outcome.is_blocked:
            self.bus.emit("schedule.skipped", f"Schedule '{s.name}' skipped: {attempt.message}",
                          level="warning", schedule_id=s.id, outcome=attempt.outcome.value)
        elif attempt.outcome not in (Outcome.SUCCESS, Outcome.COMPLETED):
            self.bus.emit("schedule.result", f"Schedule '{s.name}' finished: {attempt.outcome.value}",
                          level="warning", schedule_id=s.id, outcome=attempt.outcome.value)

    # -- thread ----------------------------------------------------------------------
    def start(self, interval_s: float = 1.0) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(interval_s):
                try:
                    self.tick()
                except Exception:
                    log.exception("scheduler tick failed")

        self._thread = threading.Thread(target=loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
