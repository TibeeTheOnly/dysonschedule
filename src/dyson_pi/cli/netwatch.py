"""Connectivity watchdog, run every minute by dyson-pi-netwatch.timer (as root).

* "Up" = the NetworkManager hotspot connection is active AND the default
  gateway (the phone) answers a ping.
* Down for ``watchdog_down_minutes``: ``nmcli connection up <name>``.
* Still down another period later: toggle the Wi-Fi radio, then alternate.
* Down/restore/actions are logged to journald (stdout) and the event table.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..clock import SystemClock
from ..config import AppConfig
from ..db import Database
from ..events import EventBus

Runner = Callable[[list[str], float], tuple[int, str]]


def _run(cmd: list[str], timeout: float = 30.0) -> tuple[int, str]:
    if shutil.which(cmd[0]) is None:
        return 127, f"{cmd[0]} not found"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def connection_active(name: str, run: Runner = _run) -> bool:
    rc, out = run(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"], 10)
    return rc == 0 and name in out.splitlines()


def default_gateway(run: Runner = _run) -> str | None:
    rc, out = run(["ip", "-4", "route", "show", "default"], 5)
    m = re.search(r"default via (\S+)", out) if rc == 0 else None
    return m.group(1) if m else None


def gateway_ok(run: Runner = _run) -> bool:
    gw = default_gateway(run)
    if not gw:
        return False
    rc, _ = run(["ping", "-c", "2", "-W", "2", gw], 10)
    return rc == 0


class NetWatch:
    def __init__(self, cfg: AppConfig, state_path: Path, bus: EventBus | None,
                 run: Runner = _run, now: Callable[[], float] = time.time) -> None:
        self.cfg, self.state_path, self.bus, self.run, self.now = cfg, state_path, bus, run, now

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, st: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(st))

    def _log(self, kind: str, msg: str, level: str = "info") -> None:
        print(f"[netwatch] {msg}", flush=True)
        if self.bus is not None:
            self.bus.emit(kind, msg, level=level)  # type: ignore[arg-type]

    def check(self) -> str:
        name = self.cfg.network.connection_name
        st = self._load()
        up = connection_active(name, self.run) and gateway_ok(self.run)
        now = self.now()
        if up:
            if "down_since" in st:
                mins = (now - st["down_since"]) / 60
                ip = self._ip()
                self._log("network.restored",
                          f"Wi-Fi '{name}' back after {mins:.1f} min ({st.get('actions', 0)} reconnect action(s)), IP {ip}")
                self._save({})
            return "up"
        if "down_since" not in st:
            self._save({"down_since": now, "actions": 0, "last_action": now - 1e9})
            self._log("network.down", f"Wi-Fi '{name}' appears down", "warning")
            return "down"
        period = self.cfg.network.watchdog_down_minutes * 60
        if now - st["down_since"] < period or now - st.get("last_action", 0) < period:
            return "waiting"
        actions = int(st.get("actions", 0))
        if actions % 2 == 0:
            rc, out = self.run(["nmcli", "connection", "up", "id", name], 60)
            what = f"nmcli connection up '{name}' -> rc={rc} {out[:200]}"
        else:
            self.run(["nmcli", "radio", "wifi", "off"], 15)
            time.sleep(3)
            rc, out = self.run(["nmcli", "radio", "wifi", "on"], 15)
            what = f"Wi-Fi radio toggled -> rc={rc}"
        st.update(actions=actions + 1, last_action=now)
        self._save(st)
        self._log("network.reconnect", f"Network down {(now - st['down_since']) / 60:.0f} min, reconnect attempt "
                  f"{actions + 1}: {what}", "warning")
        return "action"

    def _ip(self) -> str:
        rc, out = self.run(["ip", "-4", "-o", "addr", "show", self.cfg.network.interface], 5)
        m = re.search(r"inet (\S+)/", out) if rc == 0 else None
        return m.group(1) if m else "unknown"


def run_netwatch(cfg: AppConfig) -> str:
    db = None
    try:
        db = Database(cfg.paths.db_path)
        bus: EventBus | None = EventBus(db, SystemClock())
    except Exception as exc:  # DB problems must not stop network recovery
        print(f"[netwatch] event log unavailable: {exc}")
        bus = None
    try:
        return NetWatch(cfg, cfg.paths.state_dir / "netwatch.json", bus).check()
    finally:
        if db is not None:
            db.close()
