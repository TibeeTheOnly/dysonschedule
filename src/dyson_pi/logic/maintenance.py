"""Maintenance counters, reminders and fan-hour accounting."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..clock import Clock
from ..config import AppConfig, ReminderConfig
from ..db import KV, Counters, Database
from ..events import EventBus, iso

COUNTER_NAMES = (
    "cylinder_actuations",
    "relay_cycles",
    "successful_starts",
    "failed_starts",
    "cleaning_runs",
    "fan_seconds",
)


class Maintenance:
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

    # -- counter values -----------------------------------------------------------
    def counter_value(self, name: str) -> float:
        if name == "fan_hours":
            return self.counters.get("fan_seconds") / 3600
        if name == "days":
            return self.clock.now().timestamp() / 86400
        return self.counters.get(name)

    def summary(self) -> dict[str, Any]:
        c = self.counters.all()
        return {
            "cylinder_actuations": int(c.get("cylinder_actuations", 0)),
            "relay_cycles": int(c.get("relay_cycles", 0)),
            "successful_starts": int(c.get("successful_starts", 0)),
            "failed_starts": int(c.get("failed_starts", 0)),
            "cleaning_runs": int(c.get("cleaning_runs", 0)),
            "fan_hours": round(c.get("fan_seconds", 0) / 3600, 1),
            "last_successful_start": self.kv.get("last_successful_start"),
        }

    # -- reminders ------------------------------------------------------------------
    def _row(self, rid: str) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM reminders WHERE id=?", (rid,))

    def ensure_reminders(self) -> None:
        """Create baseline rows for newly configured reminders (baseline=now)."""
        for r in self._cfg().maintenance.reminders:
            if self._row(r.id) is None:
                self.db.execute(
                    "INSERT INTO reminders(id, baseline, last_serviced_at) VALUES(?,?,NULL)",
                    (r.id, self.counter_value(r.counter)),
                )

    def reminder_status(self, r: ReminderConfig) -> dict[str, Any]:
        row = self._row(r.id) or {"baseline": self.counter_value(r.counter), "last_serviced_at": None}
        used = self.counter_value(r.counter) - row["baseline"]
        unit = {"cylinder_actuations": "actuations", "relay_cycles": "cycles", "fan_hours": "h",
                "cleaning_runs": "runs", "days": "days"}[r.counter]
        return {
            "id": r.id,
            "title": r.title,
            "counter": r.counter,
            "unit": unit,
            "every": r.every,
            "used": round(used, 1),
            "remaining": round(r.every - used, 1),
            "progress": min(1.0, used / r.every) if r.every else 0,
            "due": used >= r.every,
            "last_serviced_at": row["last_serviced_at"],
        }

    def reminders(self) -> list[dict[str, Any]]:
        self.ensure_reminders()
        return [self.reminder_status(r) for r in self._cfg().maintenance.reminders]

    def due_count(self) -> int:
        return sum(1 for r in self.reminders() if r["due"])

    def mark_serviced(self, rid: str, note: str = "") -> dict[str, Any]:
        cfg = next((r for r in self._cfg().maintenance.reminders if r.id == rid), None)
        if cfg is None:
            raise KeyError(rid)
        value = self.counter_value(cfg.counter)
        now = iso(self.clock.now())
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO reminders(id, baseline, last_serviced_at) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET baseline=excluded.baseline, last_serviced_at=excluded.last_serviced_at",
                (rid, value, now),
            )
            conn.execute(
                "INSERT INTO service_log(reminder_id, ts, counter_value, note) VALUES(?,?,?,?)",
                (rid, now, value, note),
            )
        self.bus.emit("maintenance.serviced", f"Marked serviced: {cfg.title}", reminder=rid, note=note)
        return self.reminder_status(cfg)

    def service_log(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM service_log ORDER BY id DESC LIMIT ?", (limit,))


class FanHourMeter:
    """Accumulates Pi uptime into the ``fan_seconds`` counter.

    The fan is hard-wired to 5V, so it runs exactly while the Pi is powered.
    Uses the kernel boot id + /proc/uptime so time before the service started
    (and across service restarts) is counted exactly once per boot.
    """

    def __init__(
        self,
        kv: KV,
        counters: Counters,
        uptime_s: Callable[[], float],
        boot_id: Callable[[], str],
    ) -> None:
        self.kv, self.counters, self._uptime, self._boot_id = kv, counters, uptime_s, boot_id

    def update(self) -> float:
        boot = self._boot_id()
        up = self._uptime()
        state = self.kv.get("fan.meter") or {}
        if state.get("boot_id") == boot:
            delta = up - float(state.get("uptime", 0))
        else:
            delta = up  # new boot: count everything since power-on
        if delta > 0:
            self.counters.incr("fan_seconds", delta)
        self.kv.set("fan.meter", {"boot_id": boot, "uptime": up})
        return max(delta, 0.0)
