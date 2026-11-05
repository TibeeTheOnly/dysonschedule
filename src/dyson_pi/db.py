"""SQLite persistence: one shared connection, WAL mode, numbered migrations."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE kv (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE events (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      TEXT NOT NULL,
        level   TEXT NOT NULL,
        kind    TEXT NOT NULL,
        message TEXT NOT NULL,
        data    TEXT
    );
    CREATE INDEX events_ts ON events(ts);
    CREATE TABLE attempts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        mode        TEXT NOT NULL,
        trigger     TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        outcome     TEXT NOT NULL,
        presses     INTEGER NOT NULL DEFAULT 0,
        dry_run     INTEGER NOT NULL DEFAULT 0,
        message     TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        departed_at     TEXT,
        returned_at     TEXT,
        duration_s      REAL,
        trigger         TEXT NOT NULL DEFAULT 'external',
        approximate     INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE schedules (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        days       TEXT NOT NULL,
        time       TEXT NOT NULL,
        mode       TEXT NOT NULL DEFAULT 'smart',
        enabled    INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE counters (
        name  TEXT PRIMARY KEY,
        value REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE reminders (
        id               TEXT PRIMARY KEY,
        baseline         REAL NOT NULL DEFAULT 0,
        last_serviced_at TEXT
    );
    CREATE TABLE service_log (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        reminder_id   TEXT NOT NULL,
        ts            TEXT NOT NULL,
        counter_value REAL NOT NULL,
        note          TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE api_tokens (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        last_used  TEXT
    );
    """,
]


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if str(path) != ":memory:" else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=10.0
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if self.path is not None:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=10000")
        self._migrate()
        self._fix_ownership()

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for idx in range(version, len(MIGRATIONS)):
                self._conn.executescript("BEGIN;" + MIGRATIONS[idx] + f"; PRAGMA user_version={idx + 1}; COMMIT;")

    def _fix_ownership(self) -> None:
        """When a root CLI command opens the DB, keep files owned by the
        service user so the service can still write afterwards."""
        if self.path is None or not hasattr(os, "geteuid") or os.geteuid() != 0:
            return
        try:
            st = self.path.parent.stat()
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(self.path) + suffix)
                if p.exists():
                    os.chown(p, st.st_uid, st.st_gid)
        except OSError:
            pass

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def backup_to(self, dest: Path) -> None:
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
            finally:
                target.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class KV:
    """JSON key/value store on top of the ``kv`` table."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.query_one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM kv WHERE key=?", (key,))


class Counters:
    def __init__(self, db: Database) -> None:
        self.db = db

    def incr(self, name: str, by: float = 1) -> float:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO counters(name, value) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value=value+excluded.value",
                (name, by),
            )
            return conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()[0]

    def get(self, name: str) -> float:
        row = self.db.query_one("SELECT value FROM counters WHERE name=?", (name,))
        return row["value"] if row else 0.0

    def all(self) -> dict[str, float]:
        return {r["name"]: r["value"] for r in self.db.query("SELECT name, value FROM counters")}
