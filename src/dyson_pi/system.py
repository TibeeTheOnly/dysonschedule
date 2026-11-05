"""Host information and systemd integration (all degrade gracefully off-Pi)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

_START_MONO = time.monotonic()
_FALLBACK_BOOT_ID = str(uuid.uuid4())


def cpu_temp_c() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000
    except (OSError, ValueError):
        return None


def uptime_s() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return time.monotonic() - _START_MONO


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return _FALLBACK_BOOT_ID


def primary_ip() -> str | None:
    """IP of the interface holding the default route (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed
            return s.getsockname()[0]
    except OSError:
        return None


def hostname() -> str:
    return socket.gethostname()


def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def ntp_synchronized() -> bool | None:
    """True/False from timedatectl; None if it can't be determined."""
    out = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if out is None or out == "":
        return None
    return out.lower() == "yes"


def wifi_ssid(interface: str = "wlan0") -> str | None:
    out = _run(["nmcli", "-t", "-g", "GENERAL.CONNECTION", "device", "show", interface])
    return out or None


def sd_notify(message: str) -> bool:
    """Minimal sd_notify(3) without dependencies."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr or not hasattr(socket, "AF_UNIX"):
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode())
        return True
    except OSError:
        return False


def watchdog_interval_s() -> float | None:
    usec = os.environ.get("WATCHDOG_USEC")
    try:
        return int(usec) / 1_000_000 if usec else None
    except ValueError:
        return None
