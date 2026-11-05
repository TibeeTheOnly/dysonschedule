from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from dyson_pi.web.auth import Auth, hash_password, verify_password
from dyson_pi.web.server import create_app

PW = "correct-horse"
H = {"X-Requested-With": "dyson-pi"}


@pytest.fixture
def client(rig):
    app = create_app(rig.svc, manage_lifecycle=False)
    Auth(rig.svc.db, rig.svc.clock, lambda: rig.svc.cfg).set_password(PW)
    with TestClient(app) as c:
        c.rig = rig  # type: ignore[attr-defined]
        yield c


@pytest.fixture
def authed(client):
    r = client.post("/api/login", json={"password": PW})
    assert r.status_code == 200
    return client


@pytest.fixture
def token(rig):
    return Auth(rig.svc.db, rig.svc.clock, lambda: rig.svc.cfg).create_token("shortcuts")


def test_password_hashing():
    h = hash_password("s3cret-pass")
    assert h.startswith("scrypt$") and "s3cret" not in h
    assert verify_password("s3cret-pass", h)
    assert not verify_password("wrong-pass", h)
    with pytest.raises(ValueError):
        hash_password("short")


def test_health_is_public(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["checks"]["backend"] == "mock"


def test_requires_auth(client):
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/press?mode=smart").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_logout(client):
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    r = client.post("/api/login", json={"password": PW})
    assert r.status_code == 200 and "dyson_session" in r.cookies
    assert client.get("/api/status").status_code == 200
    assert client.get("/").status_code == 200
    client.post("/api/logout", headers=H)
    assert client.get("/api/status").status_code == 401


def test_login_throttled(client):
    for _ in range(5):
        client.post("/api/login", json={"password": "bad"})
    assert client.post("/api/login", json={"password": PW}).status_code == 429


def test_csrf_header_required_for_cookie_posts(authed):
    assert authed.post("/api/press?mode=force").status_code == 403
    assert authed.post("/api/press?mode=force&wait=true", headers=H).status_code == 200


def test_bearer_token(client, token):
    hdr = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/status", headers=hdr).status_code == 200
    assert client.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.post("/api/press?mode=smart&wait=true", headers=hdr)  # no CSRF header needed
    assert r.status_code == 200
    assert r.json()["trigger"] == "api"


def test_press_smart_success(authed):
    authed.rig.sensor.leave_after_presses = 1
    r = authed.post("/api/press?mode=smart&wait=true", headers=H)
    assert r.status_code == 200
    assert r.json()["outcome"] == "success"
    s = authed.get("/api/status").json()
    assert s["robot"]["state"] == "away"
    assert s["last_attempt"]["outcome"] == "success"


def test_press_refused_returns_409(authed):
    authed.rig.sensor.present = False
    authed.rig.clock.advance(5)
    r = authed.post("/api/press?mode=smart", headers=H)
    assert r.status_code == 409
    assert r.json()["outcome"] == "refused_not_present"


def test_press_async_returns_202(authed):
    r = authed.post("/api/press?mode=force", headers=H)
    assert r.status_code == 202 and r.json()["outcome"] == "running"


def test_disable_toggle(authed):
    assert authed.post("/api/actuator/disable", json={"disabled": True}, headers=H).json()["disabled"]
    r = authed.post("/api/press?mode=force", headers=H)
    assert r.status_code == 409 and r.json()["outcome"] == "blocked_disabled"
    assert authed.get("/api/status").json()["actuator"]["disabled"] is True
    authed.post("/api/actuator/disable", json={"disabled": False}, headers=H)


def test_dry_run_param(authed):
    r = authed.post("/api/press?mode=force&wait=true&dry_run=true", headers=H)
    assert r.json()["dry_run"] is True
    assert authed.rig.presses == 0


def test_schedule_crud(authed):
    r = authed.post("/api/schedules", json={"name": "Morning", "days": ["mon", "wed"], "time": "09:30"}, headers=H)
    assert r.status_code == 201
    sid = r.json()["id"]
    lst = authed.get("/api/schedules").json()
    assert lst["timezone"] == "Europe/Budapest"
    assert lst["schedules"][0]["next_run"]
    assert authed.patch(f"/api/schedules/{sid}", json={"enabled": False}, headers=H).json()["enabled"] is False
    assert authed.post("/api/schedules", json={"days": [], "time": "09:30"}, headers=H).status_code == 422
    assert authed.post("/api/schedules", json={"days": ["mon"], "time": "25:00"}, headers=H).status_code == 422
    assert authed.delete(f"/api/schedules/{sid}", headers=H).status_code == 200
    assert authed.delete(f"/api/schedules/{sid}", headers=H).status_code == 404


def test_calibration(authed):
    c = authed.get("/api/calibration").json()
    assert c["fields"]["press.press_duration_s"]["value"] == 1.0
    r = authed.put("/api/calibration", json={"values": {"press.press_duration_s": 0.7}}, headers=H)
    assert r.status_code == 200
    assert r.json()["fields"]["press.press_duration_s"]["overridden"]
    assert authed.rig.svc.cfg.press.press_duration_s == 0.7
    bad = authed.put("/api/calibration", json={"values": {"safety.hard_max_on_s": 9}}, headers=H)
    assert bad.status_code == 422
    too_long = authed.put("/api/calibration", json={"values": {"press.press_duration_s": 5}}, headers=H)
    assert too_long.status_code == 422
    authed.delete("/api/calibration", headers=H)
    assert authed.rig.svc.cfg.press.press_duration_s == 1.0


def test_maintenance_endpoints(authed):
    m = authed.get("/api/maintenance").json()
    ids = [r["id"] for r in m["reminders"]]
    assert "cylinder_seals" in ids
    r = authed.post("/api/maintenance/cylinder_seals/serviced", json={"note": "ok"}, headers=H)
    assert r.status_code == 200 and r.json()["last_serviced_at"]
    assert authed.post("/api/maintenance/nope/serviced", headers=H).status_code == 404


def test_test_relay(authed):
    r = authed.post("/api/test/relay", headers=H)
    assert r.status_code == 200 and r.json()["mode"] == "test"
    assert authed.rig.presses == 1


def test_tokens(authed):
    r = authed.post("/api/tokens", json={"name": "ha"}, headers=H)
    tok = r.json()["token"]
    assert tok.startswith("dp_")
    lst = authed.get("/api/tokens").json()
    tid = next(t["id"] for t in lst if t["name"] == "ha")
    assert authed.get("/api/status", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    authed.delete(f"/api/tokens/{tid}", headers=H)
    assert authed.get("/api/status", headers={"Authorization": f"Bearer {tok}", "Cookie": ""}).status_code == 401


def test_backup_zip(authed):
    r = authed.get("/api/backup")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert "dyson.db" in zf.namelist()


def test_events_and_history(authed):
    authed.rig.sensor.leave_after_presses = 1
    authed.post("/api/press?mode=smart&wait=true", headers=H)
    ev = authed.get("/api/events?limit=50").json()
    assert any(e["kind"] == "press.success" for e in ev)
    assert len(authed.get("/api/runs").json()) == 1
    assert authed.get("/api/attempts").json()[0]["outcome"] == "success"


def test_simulator_absent_without_mock_input(authed):
    # this rig uses a ScriptedSensor, so there is no SimulatedRobot
    assert authed.post("/api/sim/presence", json={"present": False}, headers=H).status_code == 404


def test_status_shape(authed):
    s = authed.get("/api/status").json()
    for key in ("robot", "relay", "actuator", "last_attempt", "schedules", "maintenance", "system"):
        assert key in s
    assert s["system"]["ip"] == "172.20.10.2"
    assert s["system"]["cpu_temp_c"] == 48.5
