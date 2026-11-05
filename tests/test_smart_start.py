"""Closed-loop smart start: the four required scenarios plus edge cases."""

from __future__ import annotations

from dyson_pi.hardware.presence import Presence
from dyson_pi.logic.models import Outcome, PressMode


def test_robot_leaves_after_one_press(rig):
    rig.sensor.leave_after_presses = 1
    a = rig.svc.actuator.submit(PressMode.SMART, trigger="test")
    assert a.outcome is Outcome.SUCCESS
    assert a.presses == 1
    assert rig.presses == 1
    assert rig.svc.presence.state is Presence.ABSENT
    assert rig.svc.counters.get("successful_starts") == 1
    assert rig.svc.kv.get("last_successful_start") is not None
    assert rig.svc.robot.last_interaction is not None
    assert rig.svc.actuator.alert is None


def test_robot_asleep_leaves_after_second_press(rig):
    rig.sensor.leave_after_presses = 2
    t0 = rig.clock.monotonic()
    a = rig.svc.actuator.submit(PressMode.SMART)
    assert a.outcome is Outcome.SUCCESS
    assert a.presses == 2
    assert rig.presses == 2
    # first wait must have run the full departure timeout before pressing again
    assert rig.clock.monotonic() - t0 >= rig.svc.cfg.press.departure_timeout_s
    assert any("probably asleep" in line for line in a.log)


def test_robot_never_leaves_fails_without_blind_retries(rig):
    rig.sensor.leave_after_presses = None
    a = rig.svc.actuator.submit(PressMode.SMART)
    assert a.outcome is Outcome.FAILED
    assert rig.presses == rig.svc.cfg.press.max_presses == 2
    assert rig.svc.counters.get("failed_starts") == 1
    alert = rig.svc.actuator.alert
    assert alert is not None and alert["attempt_id"] == a.id
    # nothing else happens afterwards
    rig.clock.advance(120)
    assert rig.presses == 2


def test_robot_not_present_refuses_to_press(rig):
    rig.sensor.present = False
    rig.clock.advance(5)
    assert rig.svc.presence.state is Presence.ABSENT
    for mode in (PressMode.SMART, PressMode.SINGLE, PressMode.DOUBLE):
        a = rig.svc.actuator.submit(mode)
        assert a.outcome is Outcome.REFUSED_NOT_PRESENT, mode
        assert "Force press" in a.message
    assert rig.presses == 0


def test_force_press_ignores_sensor(rig):
    rig.sensor.present = False
    rig.clock.advance(5)
    a = rig.svc.actuator.submit(PressMode.FORCE)
    assert a.outcome is Outcome.COMPLETED
    assert rig.presses == 1


def test_unknown_presence_at_startup_refuses(rig_factory):
    r = rig_factory()  # not settled yet
    assert r.svc.presence.state is Presence.UNKNOWN
    a = r.svc.actuator.submit(PressMode.SMART)
    assert a.outcome is Outcome.REFUSED_NOT_PRESENT
    assert "unknown" in a.message
    assert r.presses == 0


def test_max_presses_configurable(rig_factory):
    r = rig_factory(overrides={"press": {"max_presses": 3}})
    r.settle()
    r.sensor.leave_after_presses = 3
    a = r.svc.actuator.submit(PressMode.SMART)
    assert a.outcome is Outcome.SUCCESS
    assert a.presses == 3


def test_departure_starting_at_deadline_is_not_pressed_again(rig):
    """Robot starts leaving just before the timeout: wait for stability rather
    than pressing (and pausing) a robot that is already departing."""
    timeout = rig.svc.cfg.press.departure_timeout_s
    rig.sensor.leave_after_presses = 1
    rig.sensor.leave_delay_s = timeout - 1.0  # leaves 1s before deadline; stability is 3s
    a = rig.svc.actuator.submit(PressMode.SMART)
    assert a.outcome is Outcome.SUCCESS
    assert rig.presses == 1


def test_double_press_uses_inter_press_delay(rig):
    a = rig.svc.actuator.submit(PressMode.DOUBLE)
    assert a.outcome is Outcome.COMPLETED
    on_times = [t for t, active in rig.relay_pin.history if active]
    off_times = [t for t, active in rig.relay_pin.history if not active]
    assert len(on_times) == 2
    gap = on_times[1] - off_times[0]
    assert abs(gap - rig.svc.cfg.press.inter_press_delay_s) < 0.05


def test_press_duration_is_applied(rig):
    rig.svc.actuator.submit(PressMode.FORCE)
    (t_on, _), (t_off, _) = rig.relay_pin.history[:2]
    assert abs((t_off - t_on) - rig.svc.cfg.press.press_duration_s) < 0.03


def test_departure_and_return_record_cleaning_run(rig):
    rig.sensor.leave_after_presses = 1
    rig.svc.actuator.submit(PressMode.SMART, trigger="schedule:7")
    runs = rig.svc.robot.runs()
    assert len(runs) == 1 and runs[0]["returned_at"] is None
    assert runs[0]["trigger"] == "schedule:7"
    rig.clock.advance(600)  # cleaning
    rig.sensor.present = True
    rig.clock.advance(5)
    runs = rig.svc.robot.runs()
    assert runs[0]["returned_at"] is not None
    assert 590 < runs[0]["duration_s"] < 620
    assert rig.svc.counters.get("cleaning_runs") == 1


def test_return_updates_last_interaction(rig):
    rig.sensor.present = False
    rig.clock.advance(5)
    before = rig.svc.robot.last_interaction
    rig.clock.advance(3600)
    rig.sensor.present = True
    rig.clock.advance(5)
    after = rig.svc.robot.last_interaction
    assert after is not None and (before is None or after > before)
    assert rig.svc.robot.predicted_asleep() is False


def test_sleep_hint_is_secondary(rig):
    # Hint says asleep, but the robot leaves after one press: sensor wins.
    rig.sensor.present = True
    assert rig.svc.robot.predicted_asleep() in (None, True)
    rig.sensor.leave_after_presses = 1
    a = rig.svc.actuator.submit(PressMode.SMART)
    assert a.outcome is Outcome.SUCCESS and a.presses == 1


def test_dry_run_does_not_energize_relay(rig):
    rig.sensor.leave_after_presses = 1
    a = rig.svc.actuator.submit(PressMode.SMART, dry_run=True)
    assert rig.presses == 0
    assert a.dry_run
    assert a.outcome is Outcome.FAILED  # robot never pressed, so it never leaves
    assert rig.svc.counters.get("failed_starts") == 0  # dry runs don't count
    assert rig.svc.counters.get("cylinder_actuations") == 0
    assert any("dry run" in line for line in a.log)
