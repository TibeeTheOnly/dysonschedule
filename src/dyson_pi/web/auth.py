"""Password, session and API-token handling.

* Password: stdlib scrypt (no compiled deps), stored in the DB kv table.
* Sessions: random token in an HttpOnly cookie; only its SHA-256 is stored,
  so sessions are revocable server-side.
* API tokens: random, shown once, stored as SHA-256; sent as
  ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from ..clock import Clock
from ..config import AppConfig
from ..db import KV, Database
from ..events import iso

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
SESSION_COOKIE = "dyson_session"


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, db: Database, clock: Clock, cfg: Callable[[], AppConfig]) -> None:
        self.db, self.kv, self.clock, self._cfg = db, KV(db), clock, cfg
        self._fail_lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    # -- password ---------------------------------------------------------------
    @property
    def password_set(self) -> bool:
        return bool(self.kv.get("auth.password_hash"))

    def set_password(self, password: str) -> None:
        self.kv.set("auth.password_hash", hash_password(password))
        self.db.execute("DELETE FROM sessions")  # log everyone out

    def check_password(self, password: str) -> bool:
        stored = self.kv.get("auth.password_hash")
        return bool(stored) and verify_password(password, stored)

    # -- login throttling ----------------------------------------------------------
    def throttled(self, client: str, max_failures: int = 5, window_s: float = 300) -> bool:
        now = self.clock.monotonic()
        with self._fail_lock:
            recent = [t for t in self._failures.get(client, []) if now - t < window_s]
            self._failures[client] = recent
            return len(recent) >= max_failures

    def record_failure(self, client: str) -> None:
        with self._fail_lock:
            self._failures.setdefault(client, []).append(self.clock.monotonic())

    # -- sessions -------------------------------------------------------------------
    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock.now()
        exp = now + timedelta(days=self._cfg().web.session_days)
        self.db.execute("INSERT INTO sessions(token_hash, created_at, expires_at) VALUES(?,?,?)",
                        (_sha(token), iso(now), iso(exp)))
        return token

    def session_valid(self, token: str | None) -> bool:
        if not token:
            return False
        row = self.db.query_one("SELECT expires_at FROM sessions WHERE token_hash=?", (_sha(token),))
        return bool(row) and row["expires_at"] > (iso(self.clock.now()) or "")

    def delete_session(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (_sha(token),))

    # -- API tokens --------------------------------------------------------------------
    def create_token(self, name: str) -> str:
        token = "dp_" + secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO api_tokens(name, token_hash, created_at) VALUES(?,?,?)",
                        (name.strip()[:60] or "token", _sha(token), iso(self.clock.now())))
        return token

    def replace_named_token(self, name: str, token: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM api_tokens WHERE name=?", (name,))
            conn.execute("INSERT INTO api_tokens(name, token_hash, created_at) VALUES(?,?,?)",
                         (name, _sha(token), iso(self.clock.now())))

    def token_valid(self, token: str | None) -> bool:
        if not token:
            return False
        row = self.db.query_one("SELECT id FROM api_tokens WHERE token_hash=?", (_sha(token),))
        if not row:
            return False
        self.db.execute("UPDATE api_tokens SET last_used=? WHERE id=?", (iso(self.clock.now()), row["id"]))
        return True

    def list_tokens(self) -> list[dict[str, Any]]:
        return self.db.query("SELECT id, name, created_at, last_used FROM api_tokens ORDER BY id")

    def revoke_token(self, token_id: int) -> bool:
        return self.db.execute("DELETE FROM api_tokens WHERE id=?", (token_id,)).rowcount > 0
