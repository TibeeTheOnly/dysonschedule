"""Actuator: every press sequence goes through here.

Guards (checked in this order, before anything is energized):
  disabled toggle -> busy lock -> cooldown -> daily limit -> robot presence

Smart start (closed loop):
  1. refuse if the robot is not (stably) present
  2. press
  3. wait up to departure_timeout_s for the sensor to confirm departure
  4. departed -> SUCCESS
  5. still present -> it was probably asleep; press again and wait again
  6. still present after max_presses -> FAILED + dashboard alert. No blind retries.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..clock import Clock
from ..config import AppConfig
from ..db import KV, Counters, Database
from ..events import EventBus, iso
from ..hardware.presence import Presence, PresenceMonitor
from ..hardware.relay import SafeRelay
from .models import Attempt, Outcome, PressMode
from .robot import RobotTracker

log = logging.getLogger(__name__)


class SequenceAborted(Exception):
    pass


class Actuator:
    def __init__(
        self,
        relay: SafeRelay,
        presence: PresenceMonitor,
        robot: RobotTracker,
        db: Database,
        kv: KV,
        counters: Counters,
        bus: EventBus,
        clock: Clock,
        cfg: Callable[[], AppConfig],
    ) -> None:
        self.relay, self.presence, self.robot = relay, presence, robot
        self.db, self.kv, self.counters, self.bus, self.clock, self._cfg = db, kv, counters, bus, clock, cfg
        self._lock = threading.Lock()
        self._last_sequence_end: float | None = None
        self._current: Attempt | None = None
        self._shutdown = threading.Event()

    # ------------------------------------------------------------------ state
    @property
    def disabled(self) -> bool:
        return bool(self.kv.get("actuator.disabled", False))

    def set_disabled(self, disabled: bool, by: str = "dashboard") -> None:
        self.kv.set("actuator.disabled", disabled)
        if disabled:
            self.bus.emit("actuator.disabled", f"Actuator DISABLED by {by}: all presses blocked", level="warning")
        else:
            self.bus.emit("actuator.enabled", f"Actuator re-enabled by {by}")

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def current(self) -> Attempt | None:
        return self._current

    def cooldown_remaining(self) -> float:
        if self._last_sequence_end is None:
            return 0.0
        return max(0.0, self._cfg().safety.cooldown_s - (self.clock.monotonic() - self._last_sequence_end))

    def _today(self) -> str:
        return self.clock.now().astimezone(self._cfg().tz).date().isoformat()

    def presses_today(self) -> int:
        rec = self.kv.get("actuator.daily") or {}
        return int(rec.get("count", 0)) if rec.get("date") == self._today() else 0

    def _count_press(self) -> None:
        today = self._today()
        rec = self.kv.get("actuator.daily") or {}
        count = int(rec.get("count", 0)) + 1 if rec.get("date") == today else 1
        self.kv.set("actuator.daily", {"date": today, "count": count})

    def last_attempt(self) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM attempts WHERE outcome != 'running' ORDER BY id DESC LIMIT 1")

    def last_start_attempt(self) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM attempts WHERE mode != 'test' AND outcome != 'running' ORDER BY id DESC LIMIT 1"
        )

    def attempts(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM attempts ORDER BY id DESC LIMIT ?", (limit,))

    @property
    def alert(self) -> dict[str, Any] | None:
        return self.kv.get("actuator.alert")

    def dismiss_alert(self) -> None:
        self.kv.delete("actuator.alert")

    def shutdown(self) -> None:
        """Ask a running sequence to stop before its next press."""
        self._shutdown.set()

    # ------------------------------------------------------------------ entry
    def submit(
        self,
        mode: PressMode | str,
        trigger: str = "manual",
        dry_run: bool | None = None,
        wait: bool = True,
        pulse_s: float | None = None,
    ) -> Attempt:
        """Run a press sequence. Guards are evaluated synchronously, so a
        blocked request returns immediately even with ``wait=False``."""
        mode = PressMode(mode)
        cfg = self._cfg()
        dry = cfg.safety.dry_run if dry_run is None else (dry_run or cfg.safety.dry_run)
        attempt = Attempt(mode=mode, trigger=trigger, started_at=iso(self.clock.now()) or "", dry_run=dry)

        if not self._lock.acquire(blocking=False):
            return self._finish(attempt, Outcome.BLOCKED_BUSY, "Another press sequence is already running")
        try:
            blocked = self._check_guards(attempt)
        except BaseException:
            self._lock.release()
            raise
        if blocked is not None:
            self._lock.release()
            return blocked

        self._current = attempt
        self._insert(attempt)
        self.bus.emit("press.started", f"{mode.value} sequence started ({trigger}){' [DRY RUN]' if dry else ''}",
                      attempt_id=attempt.id, mode=mode.value, trigger=trigger, dry_run=dry)
        if wait:
            self._run_and_release(attempt, pulse_s)
        else:
            threading.Thread(
                target=self._run_and_release, args=(attempt, pulse_s), name=f"press-{attempt.id}", daemon=True
            ).start()
        return attempt

    # ------------------------------------------------------------------ guards
    def _check_guards(self, attempt: Attempt) -> Attempt | None:
        cfg = self._cfg()
        if self.disabled:
            return self._finish(attempt, Outcome.BLOCKED_DISABLED, "Actuator is disabled (emergency toggle)")
        remaining = self.cooldown_remaining()
        if remaining > 0:
            return self._finish(attempt, Outcome.BLOCKED_COOLDOWN, f"Cooldown active: wait {remaining:.1f}s")
        if not attempt.dry_run and self.presses_today() >= cfg.safety.daily_press_limit:
            return self._finish(
                attempt, Outcome.BLOCKED_DAILY_LIMIT,
                f"Daily press limit reached ({cfg.safety.daily_press_limit}); resets at midnight",
            )
        if attempt.mode in (PressMode.SMART, PressMode.SINGLE, PressMode.DOUBLE):
            state = self.presence.state
            if state is not Presence.PRESENT:
                why = "robot is not at the dock (probably cleaning: a press could pause it)" \
                    if state is Presence.ABSENT else "robot presence is unknown (sensor not settled yet)"
                return self._finish(attempt, Outcome.REFUSED_NOT_PRESENT, f"Refused: {why}. Use Force press to override.")
        return None

    # ------------------------------------------------------------------ run
    def _run_and_release(self, attempt: Attempt, pulse_s: float | None) -> None:
        try:
            outcome, message = self._execute(attempt, pulse_s)
        except SequenceAborted as exc:
            outcome, message = Outcome.ABORTED, str(exc)
        except Exception as exc:
            log.exception("press sequence crashed")
            self.relay.force_off("sequence error")
            outcome, message = Outcome.ERROR, f"Internal error: {exc}"
        finally:
            self._last_sequence_end = self.clock.monotonic()
            self._current = None
        try:
            self._finish(attempt, outcome, message)
        finally:
            self._lock.release()

    def _execute(self, attempt: Attempt, pulse_s: float | None) -> tuple[Outcome, str]:
        cfg = self._cfg()
        p = cfg.press
        if attempt.mode is PressMode.TEST:
            self._press(attempt, pulse_s or cfg.safety.test_pulse_s, count_actuation=False)
            return Outcome.COMPLETED, "Relay test pulse delivered"
        if attempt.mode in (PressMode.SINGLE, PressMode.FORCE):
            self._press(attempt, p.press_duration_s)
            return Outcome.COMPLETED, "Single press delivered" + (" (sensor ignored)" if attempt.mode is PressMode.FORCE else "")
        if attempt.mode is PressMode.DOUBLE:
            self._press(attempt, p.press_duration_s)
            self._sleep(p.inter_press_delay_s)
            self._press(attempt, p.press_duration_s)
            return Outcome.COMPLETED, "Double press (wake + start) delivered"

        # SMART
        hint = self.robot.predicted_asleep()
        attempt.log.append(
            "sleep hint: " + ("probably asleep" if hint else "probably awake" if hint is False else "unknown")
        )
        for n in range(1, p.max_presses + 1):
            self._press(attempt, p.press_duration_s)
            if self._wait_for_departure(p.departure_timeout_s):
                return Outcome.SUCCESS, f"Robot left the dock after {n} press{'es' if n > 1 else ''}"
            note = "probably asleep, the first press only woke it" if n == 1 else "still docked"
            attempt.log.append(f"press {n}: robot still present after {p.departure_timeout_s:.0f}s ({note})")
            self.bus.emit("press.no_departure", f"Robot still docked after press {n}", level="info",
                          attempt_id=attempt.id, press=n)
        return Outcome.FAILED, (
            f"Robot still docked after {p.max_presses} presses. Not retrying - check the robot, "
            "cylinder alignment and air supply."
        )

    def _sleep(self, seconds: float) -> None:
        if self._shutdown.is_set():
            raise SequenceAborted("Service shutting down")
        self.clock.sleep(seconds)

    def _press(self, attempt: Attempt, duration_s: float, count_actuation: bool = True) -> None:
        cfg = self._cfg()
        # Re-check the hard stops before *every* press, including mid-sequence.
        if self._shutdown.is_set():
            raise SequenceAborted("Service shutting down")
        if self.disabled:
            raise SequenceAborted(f"Actuator disabled mid-sequence after {attempt.presses} press(es)")
        if not attempt.dry_run and self.presses_today() >= cfg.safety.daily_press_limit:
            raise SequenceAborted(f"Daily press limit reached mid-sequence after {attempt.presses} press(es)")

        # debug-level, not persisted: lets the live dashboard show the relay state
        self.bus.emit("relay.on", "relay on", level="debug", persist=False)
        try:
            result = self.relay.pulse(duration_s, dry_run=attempt.dry_run)
        finally:
            self.bus.emit("relay.off", "relay off", level="debug", persist=False)
        attempt.presses += 1
        self.db.execute("UPDATE attempts SET presses=? WHERE id=?", (attempt.presses, attempt.id))
        msg = f"press {attempt.presses}: {result.actual_s:.2f}s" + (" (capped)" if result.capped else "")
        attempt.log.append(msg + (" [dry run: relay NOT energized]" if result.dry_run else ""))
        if result.dry_run:
            self.bus.emit("press.dry_run", f"[DRY RUN] would press for {result.actual_s:.2f}s", attempt_id=attempt.id)
            return
        self._count_press()
        if count_actuation:
            self.counters.incr("cylinder_actuations")
            self.robot.note_press(attempt.trigger)
        self.bus.emit("press.pulse", f"Pressed for {result.actual_s:.2f}s", persist=False, attempt_id=attempt.id)

    def _wait_for_departure(self, timeout_s: float) -> bool:
        start = self.clock.monotonic()
        deadline = start + timeout_s
        grace_deadline = deadline + self.presence.settle_time_s()
        while True:
            if self.presence.state is Presence.ABSENT:
                return True
            now = self.clock.monotonic()
            if now >= deadline:
                # Robot just started leaving at the deadline: let the stability
                # window finish instead of pressing a robot that is departing.
                if self.presence.pending is Presence.ABSENT and now < grace_deadline:
                    pass
                else:
                    return False
            self._sleep(0.1)

    # ------------------------------------------------------------------ persistence
    def _insert(self, attempt: Attempt) -> None:
        cur = self.db.execute(
            "INSERT INTO attempts(mode, trigger, started_at, outcome, presses, dry_run, message) VALUES(?,?,?,?,?,?,?)",
            (attempt.mode.value, attempt.trigger, attempt.started_at, attempt.outcome.value,
             attempt.presses, int(attempt.dry_run), attempt.message),
        )
        attempt.id = cur.lastrowid

    def _finish(self, attempt: Attempt, outcome: Outcome, message: str) -> Attempt:
        attempt.outcome = outcome
        attempt.message = message
        attempt.finished_at = iso(self.clock.now())
        if attempt.id is None:
            self._insert(attempt)
        self.db.execute(
            "UPDATE attempts SET outcome=?, message=?, finished_at=?, presses=? WHERE id=?",
            (outcome.value, message, attempt.finished_at, attempt.presses, attempt.id),
        )
        level = "info"
        if outcome is Outcome.SUCCESS and not attempt.dry_run:
            self.counters.incr("successful_starts")
            self.kv.set("last_successful_start", attempt.finished_at)
            self.dismiss_alert()
        elif outcome is Outcome.FAILED:
            level = "error"
            if not attempt.dry_run:
                self.counters.incr("failed_starts")
            self.kv.set("actuator.alert", {"ts": attempt.finished_at, "message": message, "attempt_id": attempt.id})
        elif outcome in (Outcome.ERROR, Outcome.ABORTED):
            level = "error" if outcome is Outcome.ERROR else "warning"
        elif outcome.is_blocked:
            level = "warning"
        self.bus.emit(f"press.{outcome.value}", message, level=level, attempt_id=attempt.id,
                      mode=attempt.mode.value, trigger=attempt.trigger, presses=attempt.presses,
                      dry_run=attempt.dry_run, steps=attempt.log)
        return attempt

    def started_at(self) -> datetime | None:
        return datetime.fromisoformat(self._current.started_at) if self._current else None
