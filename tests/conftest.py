"""Test rig: the real service graph on a FakeClock with mock pins.

``ScriptedSensor`` models the robot from the tests' point of view: it watches
relay presses and opens the presence contact (robot leaves) after a scripted
number of presses, so the smart-start loop runs exactly like on hardware,
in milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from dyson_pi.app import HostInfo, Services, build_services
from dyson_pi.clock import FakeClock
from dyson_pi.config import ConfigManager
from dyson_pi.db import Database
from dyson_pi.hardware.factory import Pins
from dyson_pi.hardware.mock import MockOutput


@dataclass
class ScriptedSensor:
    """InputPin driven by a script.

    * ``present``: current robot presence (closed contact = present).
    * ``leave_after_presses``: robot leaves ``leave_delay_s`` after the Nth
      completed press (None = never leaves).
    * ``reads``: number of times the monitor polled it.
    Bounce is simulated by toggling ``present`` between ``clock.advance`` calls.
    """

    clock: FakeClock
    relay: MockOutput
    present: bool = True
    leave_after_presses: int | None = None
    leave_delay_s: float = 3.0
    presses_seen: int = 0
    reads: int = 0
    _leave_at: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.relay.on_change(self._on_relay)
        self.clock.add_tick_listener(self._tick)

    def _on_relay(self, active: bool) -> None:
        if active:
            return
        self.presses_seen += 1
        if self.leave_after_presses is not None and self.presses_seen == self.leave_after_presses:
            self._leave_at = self.clock.monotonic() + self.leave_delay_s

    def _tick(self) -> None:
        if self._leave_at is not None and self.clock.monotonic() >= self._leave_at:
            self._leave_at = None
            self.present = False

    def read_closed(self) -> bool:
        self.reads += 1
        return self.present

    def close(self) -> None:
        pass


@dataclass
class Rig:
    svc: Services
    clock: FakeClock
    relay_pin: MockOutput
    sensor: ScriptedSensor

    def settle(self, seconds: float = 4.0) -> None:
        """Let the presence monitor commit the initial state."""
        self.clock.advance(seconds)

    @property
    def presses(self) -> int:
        return self.relay_pin.activations


def make_rig(tmp_path: Path, overrides: dict[str, Any] | None = None,
             start: datetime | None = None, ntp: bool | None = True) -> Rig:
    raw: dict[str, Any] = {
        "hardware": {"backend": "mock"},
        "paths": {"state_dir": str(tmp_path), "log_dir": str(tmp_path / "logs")},
        "scheduler": {"require_ntp_sync": True},
    }
    for section, values in (overrides or {}).items():
        raw.setdefault(section, {}).update(values)
    clock = FakeClock(start=start or datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc))  # a Monday
    relay_pin = MockOutput()
    relay_pin.bind_clock(clock)
    sensor = ScriptedSensor(clock, relay_pin)
    host = HostInfo(
        cpu_temp_c=lambda: 48.5, uptime_s=lambda: 100.0, boot_id=lambda: "boot-1",
        primary_ip=lambda: "172.20.10.2", hostname=lambda: "dyson-pi",
        ntp_synchronized=lambda: ntp, wifi_ssid=lambda _i: "hotspot",
    )
    svc = build_services(
        ConfigManager(raw), clock=clock, pins=Pins(relay_pin, sensor, "mock"), host=host,
        db=Database(tmp_path / "test.db"),
    )
    clock.add_tick_listener(svc.presence.poll)
    clock.add_tick_listener(svc.relay.check_watchdog)
    return Rig(svc, clock, relay_pin, sensor)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    r = make_rig(tmp_path)
    r.settle()
    return r


@pytest.fixture
def rig_factory(tmp_path: Path):
    def factory(**kwargs: Any) -> Rig:
        return make_rig(tmp_path, **kwargs)

    return factory
