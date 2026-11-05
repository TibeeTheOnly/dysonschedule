"""Safety guards: cooldown, hard max on-time, watchdog, daily limit, disable, lock."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from dyson_pi.config import ConfigError, ConfigManager
from dyson_pi.logic.models import Outcome, PressMode


def test_cooldown_between_sequences(rig):
    a1 = rig.svc.actuator.submit(PressMode.FORCE)
    assert a1.outcome is Outcome.COMPLETED
    a2 = rig.svc.actuator.submit(PressMode.FORCE)
    assert a2.outcome is Outcome.BLOCKED_COOLDOWN
    assert rig.presses == 1
    rig.clock.advance(rig.svc.cfg.safety.cooldown_s + 0.1)
    a3 = rig.svc.actuator.submit(PressMode.FORCE)
    assert a3.outcome is Outcome.COMPLETED
    assert rig.presses == 2


def test_pulse_is_capped_at_hard_max(rig):
    res = rig.svc.relay.pulse(10.0)
    assert res.capped
    assert res.actual_s == rig.svc.cfg.safety.hard_max_on_s
    (t_on, _), (t_off, _) = rig.relay_pin.history[:2]
    assert t_off - t_on <= rig.svc.cfg.safety.hard_max_on_s + 0.03
    assert not rig.relay_pin.is_active


def test_watchdog_forces_relay_off(rig):
    events = []
    rig.svc.bus.subscribe(lambda e: events.append(e.kind))
    rig.svc.relay._energize()  # simulate a bug leaving the relay on
    assert rig.relay_pin.is_active
    rig.clock.advance(rig.svc.cfg.safety.hard_max_on_s + 0.5)
    assert not rig.relay_pin.is_active
    assert rig.svc.relay.watchdog_trips == 1
    assert "relay.watchdog" in events


def test_hard_max_cannot_be_exceeded_by_config():
    with pytest.raises(ConfigError):
        ConfigManager({"press": {"press_duration_s": 4.0}, "safety": {"hard_max_on_s": 3.0}})


def test_calibration_override_cannot_exceed_hard_max(rig):
    with pytest.raises(ConfigError):
        rig.svc.config.set_overrides({"press.press_duration_s": 3.5})
    assert rig.svc.cfg.press.press_duration_s == 1.0


def test_daily_limit(rig_factory):
    r = rig_factory(overrides={"safety": {"daily_press_limit": 3, "cooldown_s": 0}})
    r.settle()
    outcomes = [r.svc.actuator.submit(PressMode.FORCE).outcome for _ in range(5)]
    assert outcomes[:3] == [Outcome.COMPLETED] * 3
    assert outcomes[3:] == [Outcome.BLOCKED_DAILY_LIMIT] * 2
    assert r.presses == 3


def test_daily_limit_aborts_mid_sequence(rig_factory):
    r = rig_factory(overrides={"safety": {"daily_press_limit": 1}})
    r.settle()
    a = r.svc.actuator.submit(PressMode.DOUBLE)
    assert a.outcome is Outcome.ABORTED
    assert r.presses == 1


def test_daily_limit_resets_at_local_midnight(rig_factory):
    # 22:30 UTC = 23:30 Budapest (winter)
    r = rig_factory(overrides={"safety": {"daily_press_limit": 1, "cooldown_s": 0}},
                    start=datetime(2026, 1, 5, 22, 30, tzinfo=timezone.utc))
    r.settle()
    assert r.svc.actuator.submit(PressMode.FORCE).outcome is Outcome.COMPLETED
    assert r.svc.actuator.submit(PressMode.FORCE).outcome is Outcome.BLOCKED_DAILY_LIMIT
    r.clock.set_wall(r.clock.now() + timedelta(minutes=40))  # past local midnight
    assert r.svc.actuator.presses_today() == 0
    assert r.svc.actuator.submit(PressMode.FORCE).outcome is Outcome.COMPLETED


def test_disable_toggle_blocks_everything(rig):
    rig.svc.actuator.set_disabled(True)
    for mode in PressMode:
        a = rig.svc.actuator.submit(mode)
        assert a.outcome is Outcome.BLOCKED_DISABLED, mode
    assert rig.presses == 0
    rig.svc.actuator.set_disabled(False)
    assert rig.svc.actuator.submit(PressMode.FORCE).outcome is Outcome.COMPLETED


def test_disable_mid_sequence_stops_next_press(rig):
    rig.sensor.leave_after_presses = None

    def disable_after_first_press(active: bool) -> None:
        if not active and rig.relay_pin.activations == 1:
            rig.svc.actuator.set_disabled(True)

    rig.relay_pin.on_change(disable_after_first_press)
    a = rig.svc.actuator.submit(PressMode.SMART)
    assert a.outcome is Outcome.ABORTED
    assert rig.presses == 1


def test_disable_persists_across_restart(tmp_path):
    from conftest import make_rig

    r1 = make_rig(tmp_path)
    r1.svc.actuator.set_disabled(True)
    r1.svc.db.close()
    r2 = make_rig(tmp_path)
    assert r2.svc.actuator.disabled


def test_only_one_sequence_at_a_time(rig):
    blocked = []
    gate = threading.Event()

    def try_second(active: bool) -> None:
        if active and not gate.is_set():
            gate.set()
            blocked.append(rig.svc.actuator.submit(PressMode.FORCE))

    rig.relay_pin.on_change(try_second)
    first = rig.svc.actuator.submit(PressMode.FORCE)
    assert first.outcome is Outcome.COMPLETED
    assert blocked and blocked[0].outcome is Outcome.BLOCKED_BUSY
    assert rig.presses == 1


def test_relay_off_after_exception_in_sequence(rig, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(rig.svc.robot, "note_press", boom)
    a = rig.svc.actuator.submit(PressMode.FORCE)
    assert a.outcome is Outcome.ERROR
    assert not rig.relay_pin.is_active
    assert not rig.svc.actuator.busy  # lock released


def test_stop_forces_relay_off(rig):
    rig.svc.relay._energize()
    rig.svc.stop()
    assert not rig.relay_pin.is_active


def test_counters_track_cycles_and_actuations(rig):
    rig.svc.actuator.submit(PressMode.TEST)
    rig.clock.advance(6)
    rig.svc.actuator.submit(PressMode.FORCE)
    assert rig.svc.counters.get("relay_cycles") == 2
    assert rig.svc.counters.get("cylinder_actuations") == 1  # test click excluded
    assert rig.svc.actuator.presses_today() == 2  # but both count toward the daily limit


def test_config_rejects_departure_timeout_shorter_than_settle():
    with pytest.raises(ConfigError):
        ConfigManager({"press": {"departure_timeout_s": 3.0}, "sensor": {"stability_s": 3.0}})
