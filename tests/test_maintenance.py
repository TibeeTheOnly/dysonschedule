from __future__ import annotations

from datetime import timedelta

from dyson_pi.config import ReminderConfig
from dyson_pi.logic.maintenance import FanHourMeter
from dyson_pi.logic.models import PressMode


def _reminder(r, rid):
    return next(x for x in r.svc.maintenance.reminders() if x["id"] == rid)


def test_actuation_reminder_due_and_serviced(rig_factory):
    r = rig_factory(overrides={
        "safety": {"cooldown_s": 0},
        "maintenance": {"reminders": [
            {"id": "seals", "title": "Seals", "counter": "cylinder_actuations", "every": 3},
        ]},
    })
    r.settle()
    r.svc.maintenance.ensure_reminders()
    for _ in range(3):
        r.svc.actuator.submit(PressMode.FORCE)
    rem = _reminder(r, "seals")
    assert rem["due"] and rem["used"] == 3
    assert r.svc.maintenance.due_count() == 1
    r.svc.maintenance.mark_serviced("seals", note="new o-ring")
    rem = _reminder(r, "seals")
    assert not rem["due"] and rem["used"] == 0 and rem["last_serviced_at"]
    assert r.svc.counters.get("cylinder_actuations") == 3  # global total untouched
    log = r.svc.maintenance.service_log()
    assert log[0]["reminder_id"] == "seals" and log[0]["note"] == "new o-ring"


def test_days_reminder(rig_factory):
    r = rig_factory(overrides={"maintenance": {"reminders": [
        {"id": "air", "title": "Air", "counter": "days", "every": 30},
    ]}})
    r.svc.maintenance.ensure_reminders()
    assert not _reminder(r, "air")["due"]
    r.clock.set_wall(r.clock.now() + timedelta(days=31))
    assert _reminder(r, "air")["due"]


def test_new_reminder_starts_from_current_value(rig):
    rig.svc.counters.incr("cylinder_actuations", 5000)
    rig.svc.maintenance.ensure_reminders()  # default reminders created now
    assert not _reminder(rig, "cylinder_seals")["due"]


def test_fan_hour_meter_counts_uptime_once_per_boot(rig):
    uptime = {"v": 3600.0}
    boot = {"v": "a"}
    m = FanHourMeter(rig.svc.kv, rig.svc.counters, lambda: uptime["v"], lambda: boot["v"])
    m.update()
    assert rig.svc.counters.get("fan_seconds") == 3600
    uptime["v"] = 5400
    m.update()
    m.update()  # idempotent
    assert rig.svc.counters.get("fan_seconds") == 5400
    boot["v"], uptime["v"] = "b", 600  # reboot
    m.update()
    assert rig.svc.counters.get("fan_seconds") == 6000
    assert rig.svc.maintenance.summary()["fan_hours"] == round(6000 / 3600, 1)


def test_summary_counts(rig):
    rig.sensor.leave_after_presses = 1
    rig.svc.actuator.submit(PressMode.SMART)
    s = rig.svc.maintenance.summary()
    assert s["successful_starts"] == 1
    assert s["cylinder_actuations"] == 1
    assert s["relay_cycles"] == 1
    assert s["last_successful_start"]


def test_reminder_config_validation():
    import pytest
    from dyson_pi.config import ConfigError, ConfigManager

    with pytest.raises(ConfigError):
        ConfigManager({"maintenance": {"reminders": [
            {"id": "a", "title": "x", "counter": "days", "every": 1},
            {"id": "a", "title": "y", "counter": "days", "every": 2},
        ]}})
    assert ReminderConfig(id="ok_1", title="t", counter="fan_hours", every=10).every == 10
