"""Tiny HTTP client for the local service API (stdlib only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class ServiceUnavailable(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        self.status, self.body = status, body
        detail = body.get("detail") if isinstance(body, dict) else body
        super().__init__(f"HTTP {status}: {detail}")


def read_token(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except PermissionError as exc:
        raise PermissionError(
            f"cannot read {path}: run with sudo (or add your user to the dyson-pi group)"
        ) from exc
    except OSError:
        return None


class Client:
    def __init__(self, base_url: str, token: str | None, timeout: float = 10.0) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None, timeout: float | None = None,
                ok_statuses: tuple[int, ...] = (409,)) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = raw.decode(errors="replace")
            if exc.code in ok_statuses:
                return parsed
            raise ApiError(exc.code, parsed) from None
        except (urllib.error.URLError, OSError) as exc:
            raise ServiceUnavailable(f"service not reachable at {self.base}: {exc}") from None
        return json.loads(raw) if raw else None

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, body, **kw)

    def alive(self) -> bool:
        try:
            self.request("GET", "/api/health", timeout=2, ok_statuses=(503,))
            return True
        except (ServiceUnavailable, ApiError):
            return False
