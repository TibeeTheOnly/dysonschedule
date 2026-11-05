"""Schedule persistence (SQLite)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from ..clock import Clock
from ..db import Database
from ..events import iso
from ..logic.models import PressMode
from .recurrence import normalize_days, parse_time

ALLOWED_MODES = {PressMode.SMART, PressMode.SINGLE, PressMode.DOUBLE}


@dataclass
class Schedule:
    id: int
    name: str
    days: list[int]
    time: str
    mode: PressMode
    enabled: bool
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mode"] = self.mode.value
        return d


def _validate(name: str, days: list[int | str], time_s: str, mode: str) -> tuple[str, list[int], str, PressMode]:
    name = (name or "").strip() or "Cleaning"
    if len(name) > 60:
        raise ValueError("name too long (max 60)")
    nd = normalize_days(days)
    t = parse_time(time_s)
    pm = PressMode(mode)
    if pm not in ALLOWED_MODES:
        raise ValueError("scheduled mode must be smart, single or double (force is never scheduled)")
    return name, nd, f"{t.hour:02d}:{t.minute:02d}", pm


class ScheduleStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    @staticmethod
    def _row(r: dict[str, Any]) -> Schedule:
        return Schedule(
            id=r["id"], name=r["name"], days=json.loads(r["days"]), time=r["time"],
            mode=PressMode(r["mode"]), enabled=bool(r["enabled"]), created_at=r["created_at"],
        )

    def list(self) -> list[Schedule]:
        return [self._row(r) for r in self.db.query("SELECT * FROM schedules ORDER BY time, id")]

    def get(self, sid: int) -> Schedule | None:
        r = self.db.query_one("SELECT * FROM schedules WHERE id=?", (sid,))
        return self._row(r) if r else None

    def create(self, name: str, days: list[int | str], time: str, mode: str = "smart", enabled: bool = True) -> Schedule:
        n, d, t, m = _validate(name, days, time, mode)
        cur = self.db.execute(
            "INSERT INTO schedules(name, days, time, mode, enabled, created_at) VALUES(?,?,?,?,?,?)",
            (n, json.dumps(d), t, m.value, int(enabled), iso(self.clock.now())),
        )
        s = self.get(int(cur.lastrowid or 0))
        assert s is not None
        return s

    def update(self, sid: int, **fields: Any) -> Schedule:
        cur = self.get(sid)
        if cur is None:
            raise KeyError(sid)
        n, d, t, m = _validate(
            fields.get("name", cur.name), fields.get("days", cur.days),
            fields.get("time", cur.time), fields.get("mode", cur.mode.value),
        )
        enabled = bool(fields.get("enabled", cur.enabled))
        self.db.execute(
            "UPDATE schedules SET name=?, days=?, time=?, mode=?, enabled=? WHERE id=?",
            (n, json.dumps(d), t, m.value, int(enabled), sid),
        )
        s = self.get(sid)
        assert s is not None
        return s

    def delete(self, sid: int) -> bool:
        return self.db.execute("DELETE FROM schedules WHERE id=?", (sid,)).rowcount > 0
