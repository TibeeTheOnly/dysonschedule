"""Event bus: every noteworthy thing is logged, persisted to the ``events``
table, and pushed to live subscribers (the SSE stream)."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from .clock import Clock
from .db import Database

Level = Literal["debug", "info", "warning", "error"]

log = logging.getLogger("dyson_pi.events")

_LOG_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


@dataclass
class Event:
    ts: str
    level: Level
    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self, db: Database | None, clock: Clock) -> None:
        self.db = db
        self.clock = clock
        self._subs: list[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)

        return unsubscribe

    def emit(
        self,
        kind: str,
        message: str,
        level: Level = "info",
        persist: bool = True,
        **data: Any,
    ) -> Event:
        ev = Event(ts=self.clock.now().isoformat(), level=level, kind=kind, message=message, data=data)
        log.log(_LOG_LEVELS[level], "%s: %s", kind, message, extra={"event_kind": kind, "event_data": data})
        if persist and self.db is not None:
            try:
                cur = self.db.execute(
                    "INSERT INTO events(ts, level, kind, message, data) VALUES(?,?,?,?,?)",
                    (ev.ts, level, kind, message, json.dumps(data, default=str) if data else None),
                )
                ev.id = cur.lastrowid
            except Exception:  # never let logging break the caller
                log.exception("failed to persist event")
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(ev)
            except Exception:
                log.exception("event subscriber failed")
        return ev

    def recent(self, limit: int = 100, before_id: int | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        sql = "SELECT * FROM events"
        clauses: list[str] = []
        params: list[Any] = []
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        if kind:
            clauses.append("kind LIKE ?")
            params.append(kind + "%")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, tuple(params))
        for r in rows:
            r["data"] = json.loads(r["data"]) if r["data"] else {}
        return rows

    def prune(self, retention_days: int) -> int:
        if self.db is None:
            return 0
        cutoff = (self.clock.now() - timedelta(days=retention_days)).isoformat()
        return self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None
