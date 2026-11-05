"""ScheduleRunner: firing, NTP gate, missed-run skipping, closed-loop rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dyson_pi.logic.models import Outcome

MON_0800_UTC = datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc)  # 09:00 Budapest


def run_for(r, seconds: float, step: float = 1.0) -> None:
    """Advance the clock, ticking the scheduler every ``step`` seconds."""
    t = 0.0
    while t < seconds:
        r.clock.advance(step)
        r.svc.scheduler.tick()
        t += step


def kinds(r) -> list[str]:
    return [e["kind"] for e in r.svc.bus.recent(500)]


@pytest.fixture
def srig(rig_factory):
    r = rig_factory(start=MON_0800_UTC)
    r.settle()
    return r


def test_schedule_fires_smart_start_at_time(srig):
    srig.sensor.leave_after_presses = 1
    srig.svc.schedules.create("Morning", ["mon"], "09:01")
    srig.svc.scheduler.rearm()
    run_for(srig, 40)
    assert srig.presses == 0
    run_for(srig, 60)
    assert srig.presses == 1
    att = srig.svc.actuator.attempts(1)[0]
    assert att["trigger"].startswith("schedule:")
    assert att["outcome"] == Outcome.SUCCESS.value


def test_next_run_reported(srig):
    s = srig.svc.schedules.create("Evening", ["mon", "tue"], "18:30")
    srig.svc.scheduler.rearm()
    up = srig.svc.scheduler.upcoming()
    assert up[0]["id"] == s.id
    assert up[0]["next_run"] == datetime(2026, 1, 5, 17, 30, tzinfo=timezone.utc).isoformat()


def test_disabled_schedule_does_not_fire(srig):
    s = srig.svc.schedules.create("Morning", ["mon"], "09:01")
    srig.svc.schedules.update(s.id, enabled=False)
    srig.svc.scheduler.rearm()
    run_for(srig, 120)
    assert srig.presses == 0
    assert srig.svc.scheduler.upcoming()[0]["next_run"] is None


def test_robot_not_present_skips_and_logs_reason(srig):
    srig.sensor.present = False
    srig.clock.advance(5)
    srig.svc.schedules.create("Morning", ["mon"], "09:01")
    srig.svc.scheduler.rearm()
    run_for(srig, 90)
    assert srig.presses == 0
    skipped = [e for e in srig.svc.bus.recent(100) if e["kind"] == "schedule.skipped"]
    assert skipped and "not at the dock" in skipped[0]["message"]


def test_waits_for_ntp_then_skips_missed_runs(rig_factory):
    r = rig_factory(start=MON_0800_UTC, ntp=False)
    r.settle()
    r.svc.schedules.create("Morning", ["mon"], "09:01")
    run_for(r, 5)
    assert r.svc.scheduler.waiting_for_ntp
    assert r.svc.scheduler.upcoming()[0]["next_run"] is not None  # provisional, for the UI
    run_for(r, 120)  # 09:01 passes while unsynced
    assert r.presses == 0
    r.svc.scheduler._ntp_synced = lambda: True
    run_for(r, 10)
    assert not r.svc.scheduler.waiting_for_ntp
    assert r.presses == 0  # missed run NOT fired late
    nxt = r.svc.scheduler.next_runs()
    assert list(nxt.values())[0] == datetime(2026, 1, 12, 8, 1, tzinfo=timezone.utc)
    assert "scheduler.ntp" in kinds(r)


def test_ntp_gate_can_be_disabled(rig_factory):
    r = rig_factory(start=MON_0800_UTC, ntp=False, overrides={"scheduler": {"require_ntp_sync": False}})
    r.settle()
    r.sensor.leave_after_presses = 1
    r.svc.schedules.create("Morning", ["mon"], "09:01")
    run_for(r, 100)
    assert r.presses == 1


def test_late_tick_beyond_grace_is_skipped(srig):
    srig.svc.schedules.create("Morning", ["mon"], "09:01")
    srig.svc.scheduler.rearm()
    srig.clock.advance(60 + 120)  # thread stalled: no ticks until 2 min after due time
    srig.svc.scheduler.tick()
    assert srig.presses == 0
    assert "schedule.missed" in kinds(srig)


def test_clock_jump_forward_does_not_fire_backlog(srig):
    srig.svc.schedules.create("Every day", list(range(7)), "09:30")
    srig.svc.scheduler.rearm()
    srig.clock.set_wall(srig.clock.now() + timedelta(days=3))
    srig.svc.scheduler.tick()
    assert srig.presses == 0


def test_store_validation(srig):
    st = srig.svc.schedules
    with pytest.raises(ValueError):
        st.create("x", [], "09:00")
    with pytest.raises(ValueError):
        st.create("x", ["mon"], "9am")
    with pytest.raises(ValueError):
        st.create("x", ["mon"], "09:00", mode="force")
    s = st.create("", ["sun", "sat"], "07:00", mode="double")
    assert s.name == "Cleaning" and s.days == [5, 6]
    s2 = st.update(s.id, time="08:15", days=[0])
    assert s2.time == "08:15" and s2.days == [0] and s2.mode.value == "double"
    assert st.delete(s.id) and st.get(s.id) is None


def test_schedules_persist(tmp_path):
    from conftest import make_rig

    r1 = make_rig(tmp_path, start=MON_0800_UTC)
    r1.svc.schedules.create("Morning", ["mon"], "09:01")
    r1.svc.db.close()
    r2 = make_rig(tmp_path, start=MON_0800_UTC)
    assert [s.name for s in r2.svc.schedules.list()] == ["Morning"]
