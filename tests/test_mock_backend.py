"""The mock backend + SimulatedRobot used for laptop development."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dyson_pi.app import HostInfo, build_services
from dyson_pi.clock import FakeClock
from dyson_pi.config import ConfigManager, HardwareConfig
from dyson_pi.db import Database
from dyson_pi.hardware.factory import create_pins
from dyson_pi.hardware.presence import Presence
from dyson_pi.logic.models import Outcome, PressMode
from dyson_pi.web.auth import Auth
from dyson_pi.web.server import create_app


@pytest.fixture
def sim(tmp_path: Path):
    clock = FakeClock()
    raw = {"hardware": {"backend": "mock"}, "paths": {"state_dir": str(tmp_path)},
           "scheduler": {"require_ntp_sync": False}}
    svc = build_services(ConfigManager(raw), clock=clock, force_mock=True,
                         host=HostInfo(ntp_synchronized=lambda: True), db=Database(tmp_path / "t.db"))
    assert svc.sim_robot is not None
    clock.add_tick_listener(svc.presence.poll)
    clock.add_tick_listener(svc.sim_robot.tick)
    clock.add_tick_listener(svc.relay.check_watchdog)
    svc.sim_robot.enabled = True
    clock.advance(4)
    return svc, clock


def test_factory_auto_selects_mock_off_pi():
    pins = create_pins(HardwareConfig(backend="auto"))
    assert pins.is_mock


def test_simulated_asleep_robot_needs_two_presses(sim):
    svc, _ = sim
    svc.sim_robot.awake = False
    a = svc.actuator.submit(PressMode.SMART)
    assert a.outcome is Outcome.SUCCESS and a.presses == 2


def test_simulated_awake_robot_needs_one_press(sim):
    svc, _ = sim
    svc.sim_robot.awake = True
    a = svc.actuator.submit(PressMode.SMART)
    assert a.outcome is Outcome.SUCCESS and a.presses == 1


def test_simulated_robot_returns_and_falls_asleep(sim):
    svc, clock = sim
    svc.sim_robot.awake = True
    svc.sim_robot.clean_duration_s = 60
    svc.actuator.submit(PressMode.SMART)
    assert svc.presence.state is Presence.ABSENT
    clock.advance(70)
    assert svc.presence.state is Presence.PRESENT
    assert svc.counters.get("cleaning_runs") == 1
    clock.advance(31 * 60)
    assert svc.sim_robot.awake is False


def test_sim_presence_toggle_via_api(sim):
    svc, clock = sim
    Auth(svc.db, svc.clock, lambda: svc.cfg).set_password("password123")
    with TestClient(create_app(svc, manage_lifecycle=False)) as c:
        c.post("/api/login", json={"password": "password123"})
        r = c.post("/api/sim/presence", json={"present": False}, headers={"X-Requested-With": "dyson-pi"})
        assert r.status_code == 200 and r.json()["docked"] is False
        clock.advance(4)
        assert svc.presence.state is Presence.ABSENT
        s = c.get("/api/status").json()
        assert s["simulator"]["docked"] is False and s["robot"]["state"] == "away"
