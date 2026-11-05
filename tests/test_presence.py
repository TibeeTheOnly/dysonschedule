"""Debounce, stability window and fault detection."""

from __future__ import annotations

from datetime import timedelta

from conftest import make_rig

from dyson_pi.hardware.presence import Presence


def test_initial_state_needs_stability(rig_factory):
    r = rig_factory()
    r.clock.advance(1.0)
    assert r.svc.presence.state is Presence.UNKNOWN
    r.clock.advance(2.5)
    assert r.svc.presence.state is Presence.PRESENT


def test_short_bounces_are_ignored(rig):
    mon = rig.svc.presence
    for _ in range(20):  # 50 ms glitches, well under the 200 ms debounce
        rig.sensor.present = False
        rig.clock.advance(0.05)
        rig.sensor.present = True
        rig.clock.advance(0.3)
    assert mon.state is Presence.PRESENT
    assert not mon.flapping
    assert mon.pending is None


def test_change_must_be_stable_before_commit(rig):
    mon = rig.svc.presence
    rig.sensor.present = False
    rig.clock.advance(0.5)
    assert mon.state is Presence.PRESENT
    assert mon.pending is Presence.ABSENT  # debounced but not yet stable
    rig.clock.advance(1.0)
    rig.sensor.present = True  # robot wiggles back before stability elapsed
    rig.clock.advance(4.0)
    assert mon.state is Presence.PRESENT
    assert rig.svc.robot.runs() == []


def test_stable_change_commits_with_original_timestamp(rig):
    t_change = rig.clock.now()
    rig.sensor.present = False
    rig.clock.advance(10)
    assert rig.svc.presence.state is Presence.ABSENT
    since = rig.svc.robot.state_since
    assert since is not None and abs((since - t_change).total_seconds()) < 0.1


def test_debounce_ms_configurable(rig_factory):
    r = rig_factory(overrides={"sensor": {"debounce_ms": 1000, "stability_s": 0}})
    r.clock.advance(1.1)
    assert r.svc.presence.state is Presence.PRESENT
    r.sensor.present = False
    r.clock.advance(0.6)
    r.sensor.present = True
    r.clock.advance(2)
    assert r.svc.presence.state is Presence.PRESENT
    r.sensor.present = False
    r.clock.advance(1.1)
    assert r.svc.presence.state is Presence.ABSENT


def test_closed_means_present_inverted(rig_factory):
    r = rig_factory(overrides={"hardware": {"presence_closed_means_present": False}})
    r.sensor.present = True  # contact closed
    r.settle()
    assert r.svc.presence.state is Presence.ABSENT


def test_flapping_detected_and_clears(rig):
    mon = rig.svc.presence
    for _ in range(4):  # 8 debounced changes, each 0.5 s
        rig.sensor.present = False
        rig.clock.advance(0.5)
        rig.sensor.present = True
        rig.clock.advance(0.5)
    assert mon.flapping
    assert any("flapping" in f for f in rig.svc.status()["robot"]["sensor_faults"])
    rig.clock.advance(65)
    assert not mon.flapping


def test_stale_sensor_warning(rig):
    assert not rig.svc.robot.stale()
    rig.sensor.present = False
    rig.clock.advance(5)
    assert not rig.svc.robot.stale()
    # Jump wall time 73 h ahead (stale_after_h default is 72) without polling for days.
    rig.clock.set_wall(rig.clock.now() + timedelta(hours=73))
    assert rig.svc.robot.stale()
    assert any("No presence change" in f for f in rig.svc.status()["robot"]["sensor_faults"])


def test_boot_reconcile_closes_run_when_robot_returned_while_down(tmp_path):
    r1 = make_rig(tmp_path)
    r1.settle()
    r1.sensor.present = False
    r1.clock.advance(5)
    assert r1.svc.robot.open_run_id is not None
    r1.svc.db.close()

    r2 = make_rig(tmp_path)  # restart: robot came home while we were down
    r2.sensor.present = True
    r2.settle()
    runs = r2.svc.robot.runs()
    assert runs[0]["returned_at"] is not None
    assert runs[0]["approximate"] == 1
    assert r2.svc.robot.open_run_id is None
