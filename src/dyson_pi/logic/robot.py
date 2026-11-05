"""RobotTracker: persistent robot state, last interaction, sleep prediction
and cleaning-run history, fed by PresenceMonitor changes."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..clock import Clock
from ..config import AppConfig
from ..db import KV, Counters, Database
from ..events import EventBus, iso, parse_iso
from ..hardware.presence import Presence, PresenceChange

log = logging.getLogger(__name__)

# A departure this soon after one of our presses is attributed to that press.
ATTRIBUTION_WINDOW_S = 90.0


class RobotTracker:
    def __init__(
        self,
        db: Database,
        kv: KV,
        counters: Counters,
        bus: EventBus,
        clock: Clock,
        cfg: Callable[[], AppConfig],
    ) -> None:
        self.db, self.kv, self.counters, self.bus, self.clock, self._cfg = db, kv, counters, bus, clock, cfg
        self._lock = threading.RLock()
        self._last_press: tuple[datetime, str] | None = None
        self._boot_reconciled = False

    # -- persisted values -------------------------------------------------------
    @property
    def last_interaction(self) -> datetime | None:
        return parse_iso(self.kv.get("robot.last_interaction"))

    def touch(self, when: datetime | None = None, reason: str = "") -> None:
        when = when or self.clock.now()
        cur = self.last_interaction
        if cur is None or when > cur:
            self.kv.set("robot.last_interaction", iso(when))

    @property
    def persisted_state(self) -> Presence:
        return Presence(self.kv.get("robot.state", Presence.UNKNOWN.value))

    @property
    def state_since(self) -> datetime | None:
        return parse_iso(self.kv.get("robot.state_since"))

    @property
    def open_run_id(self) -> int | None:
        return self.kv.get("robot.open_run")

    # -- sleep hint ---------------------------------------------------------------
    def predicted_asleep(self) -> bool | None:
        """Secondary hint only; the sensor decides. None = unknown."""
        last = self.last_interaction
        if last is None:
            return None
        timeout = timedelta(minutes=self._cfg().press.sleep_timeout_min)
        return self.clock.now() - last > timeout

    def minutes_until_sleep(self) -> float | None:
        last = self.last_interaction
        if last is None:
            return None
        remaining = timedelta(minutes=self._cfg().press.sleep_timeout_min) - (self.clock.now() - last)
        return max(0.0, remaining.total_seconds() / 60)

    def stale(self) -> bool:
        since = self.state_since
        if since is None:
            return False
        return self.clock.now() - since > timedelta(hours=self._cfg().sensor.stale_after_h)

    # -- hooks ------------------------------------------------------------------------
    def note_press(self, trigger: str) -> None:
        now = self.clock.now()
        with self._lock:
            self._last_press = (now, trigger)
        self.touch(now, "press")

    def handle_change(self, change: PresenceChange) -> None:
        with self._lock:
            if change.old is Presence.UNKNOWN:
                self._reconcile_boot(change)
                return
            self._set_state(change.new, change.changed_at)
            self.touch(change.changed_at, "presence")
            if change.new is Presence.ABSENT:
                self._open_run(change.changed_at, self._attribution(change.changed_at), approximate=False)
                self.bus.emit("robot.departed", "Robot left the dock", trigger=self._attribution(change.changed_at))
            else:
                self._close_run(change.changed_at, approximate=False)

    # -- internals ----------------------------------------------------------------------
    def _attribution(self, when: datetime) -> str:
        if self._last_press and (when - self._last_press[0]).total_seconds() <= ATTRIBUTION_WINDOW_S:
            return self._last_press[1]
        return "external"

    def _set_state(self, state: Presence, since: datetime) -> None:
        self.kv.set("robot.state", state.value)
        self.kv.set("robot.state_since", iso(since))

    def _reconcile_boot(self, change: PresenceChange) -> None:
        """First committed reading after (re)start: compare with persisted state."""
        prev = self.persisted_state
        if prev == change.new and self.state_since is not None:
            self.bus.emit("robot.state", f"Robot state on startup: {change.new.value} (unchanged)", persist=False)
            return
        self._set_state(change.new, change.changed_at)
        if prev is Presence.ABSENT and change.new is Presence.PRESENT:
            self._close_run(change.changed_at, approximate=True)
        elif prev is Presence.PRESENT and change.new is Presence.ABSENT:
            self._open_run(change.changed_at, "external", approximate=True)
            self.bus.emit("robot.departed", "Robot is away (left while the service was not running)")
        else:
            self.bus.emit("robot.state", f"Robot state on startup: {change.new.value}")

    def _open_run(self, departed: datetime, trigger: str, approximate: bool) -> None:
        cur = self.db.execute(
            "INSERT INTO runs(departed_at, trigger, approximate) VALUES(?,?,?)",
            (iso(departed), trigger, int(approximate)),
        )
        self.kv.set("robot.open_run", cur.lastrowid)

    def _close_run(self, returned: datetime, approximate: bool) -> None:
        run_id = self.open_run_id
        duration: float | None = None
        if run_id is not None:
            row = self.db.query_one("SELECT departed_at, approximate FROM runs WHERE id=?", (run_id,))
            if row and row["departed_at"]:
                duration = (returned - datetime.fromisoformat(row["departed_at"])).total_seconds()
            self.db.execute(
                "UPDATE runs SET returned_at=?, duration_s=?, approximate=MAX(approximate, ?) WHERE id=?",
                (iso(returned), duration, int(approximate), run_id),
            )
            self.kv.delete("robot.open_run")
        else:
            self.db.execute(
                "INSERT INTO runs(departed_at, returned_at, trigger, approximate) VALUES(NULL, ?, 'external', 1)",
                (iso(returned),),
            )
        self.counters.incr("cleaning_runs")
        if duration is None:
            mins = ""
        elif duration < 90:
            mins = f" after {duration:.0f} s"
        else:
            mins = f" after {duration / 60:.0f} min"
        note = " (returned while the service was not running)" if approximate else ""
        self.bus.emit("robot.returned", f"Robot returned to the dock{mins}{note}", duration_s=duration)

    # -- queries ---------------------------------------------------------------------------
    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
