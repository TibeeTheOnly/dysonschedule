"""REST API. Everything except /api/health and /api/login requires auth:
either the session cookie (dashboard) or ``Authorization: Bearer <token>``."""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..app import Services
from ..config import CALIBRATION_FIELDS, ConfigError
from ..events import Event
from ..logic.models import Outcome, PressMode
from .auth import SESSION_COOKIE, Auth

CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "dyson-pi"

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- dependencies
def svc_dep(request: Request) -> Services:
    return request.app.state.svc


def auth_dep(request: Request) -> Auth:
    return request.app.state.auth


def require_auth(request: Request) -> str:
    """Returns the principal kind: 'token' or 'session'."""
    auth: Auth = request.app.state.auth
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        if auth.token_valid(header[7:].strip()):
            return "token"
        raise HTTPException(401, "invalid API token")
    if auth.session_valid(request.cookies.get(SESSION_COOKIE)):
        # CSRF: browsers can't add custom headers cross-site without CORS preflight.
        if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise HTTPException(403, "missing X-Requested-With header")
        return "session"
    raise HTTPException(401, "not authenticated")


Svc = Annotated[Services, Depends(svc_dep)]
Principal = Annotated[str, Depends(require_auth)]


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------- public
@router.get("/health")
def health(svc: Svc) -> JSONResponse:
    checks: dict[str, Any] = {}
    try:
        svc.db.query_one("SELECT 1 AS ok")
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"error: {exc}"
    checks["relay_on"] = svc.relay.is_on
    checks["presence"] = svc.presence.state.value
    checks["sensor_flapping"] = svc.presence.flapping
    checks["actuator_disabled"] = svc.actuator.disabled
    checks["scheduler_waiting_for_ntp"] = svc.scheduler.waiting_for_ntp
    checks["backend"] = svc.pins.backend
    ok = checks["db"] == "ok"
    body = {"status": "ok" if ok else "error", "version": __version__,
            "uptime_s": svc.host.uptime_s(), "time": svc.clock.now().isoformat(), "checks": checks}
    return JSONResponse(body, status_code=200 if ok else 503)


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response, svc: Svc) -> dict[str, Any]:
    auth: Auth = request.app.state.auth
    if not auth.password_set:
        raise HTTPException(503, "No password set. Run: sudo dysonctl set-password")
    client = _client(request)
    if auth.throttled(client):
        raise HTTPException(429, "Too many failed attempts, try again in a few minutes")
    if not auth.check_password(body.password):
        auth.record_failure(client)
        svc.bus.emit("auth.failed", f"Failed login from {client}", level="warning")
        raise HTTPException(401, "wrong password")
    token = auth.create_session()
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", secure=svc.cfg.web.cookie_secure,
        max_age=svc.cfg.web.session_days * 86400, path="/",
    )
    svc.bus.emit("auth.login", f"Dashboard login from {client}")
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict[str, Any]:
    request.app.state.auth.delete_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


# ---------------------------------------------------------------- status
@router.get("/status")
def status(svc: Svc, _: Principal) -> dict[str, Any]:
    return svc.status()


@router.get("/events")
def events(svc: Svc, _: Principal, limit: int = Query(100, ge=1, le=1000),
           before: int | None = None, kind: str | None = None) -> list[dict[str, Any]]:
    return svc.bus.recent(limit, before, kind)


@router.get("/attempts")
def attempts(svc: Svc, _: Principal, limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    return svc.actuator.attempts(limit)


@router.get("/runs")
def runs(svc: Svc, _: Principal, limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    return svc.robot.runs(limit)


@router.get("/stream")
async def stream(request: Request, svc: Svc, _: Principal) -> StreamingResponse:
    """Server-Sent Events: ``status`` snapshots and ``event`` items."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=200)

    def on_event(ev: Event) -> None:
        def put() -> None:
            if not queue.full():
                queue.put_nowait(ev)
        loop.call_soon_threadsafe(put)

    unsubscribe = svc.bus.subscribe(on_event)

    def fmt(kind: str, data: Any) -> str:
        return f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"

    async def gen():
        try:
            yield "retry: 3000\n\n"
            yield fmt("status", await asyncio.to_thread(svc.status))
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    yield fmt("status", await asyncio.to_thread(svc.status))
                    continue
                if ev.level != "debug":
                    yield fmt("event", ev.to_dict())
                while not queue.empty():  # coalesce bursts into one status
                    extra = queue.get_nowait()
                    if extra.level != "debug":
                        yield fmt("event", extra.to_dict())
                yield fmt("status", await asyncio.to_thread(svc.status))
        finally:
            unsubscribe()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- actuation
def _attempt_response(attempt_dict: dict[str, Any], outcome: Outcome) -> JSONResponse:
    code = 409 if outcome.is_blocked else 202 if outcome is Outcome.RUNNING else 200
    return JSONResponse(attempt_dict, status_code=code)


@router.post("/press")
def press(
    svc: Svc,
    principal: Principal,
    mode: PressMode = PressMode.SMART,
    wait: bool = False,
    dry_run: bool = False,
) -> JSONResponse:
    """Run a press sequence. 200 = finished, 202 = running (wait=false),
    409 = blocked/refused (see ``outcome`` and ``message``)."""
    if mode is PressMode.TEST:
        raise HTTPException(400, "use /api/test/relay for relay test pulses")
    trigger = "api" if principal == "token" else "dashboard"
    a = svc.actuator.submit(mode, trigger=trigger, dry_run=dry_run or None, wait=wait)
    return _attempt_response(a.to_dict(), a.outcome)


@router.post("/test/relay")
def test_relay(svc: Svc, principal: Principal, pulse_s: float | None = Query(None, gt=0, le=1.0)) -> JSONResponse:
    a = svc.actuator.submit(PressMode.TEST, trigger="test", wait=True, pulse_s=pulse_s)
    return _attempt_response(a.to_dict(), a.outcome)


class DisableBody(BaseModel):
    disabled: bool


@router.post("/actuator/disable")
def set_disabled(body: DisableBody, svc: Svc, principal: Principal) -> dict[str, Any]:
    svc.actuator.set_disabled(body.disabled, by="API token" if principal == "token" else "dashboard")
    return {"disabled": svc.actuator.disabled}


@router.post("/alert/dismiss")
def dismiss_alert(svc: Svc, _: Principal) -> dict[str, Any]:
    svc.actuator.dismiss_alert()
    return {"ok": True}


# ---------------------------------------------------------------- schedules
class ScheduleBody(BaseModel):
    name: str = Field("Cleaning", max_length=60)
    days: list[int | str] = Field(min_length=1)
    time: str
    mode: Literal["smart", "single", "double"] = "smart"
    enabled: bool = True


class SchedulePatch(BaseModel):
    name: str | None = Field(None, max_length=60)
    days: list[int | str] | None = None
    time: str | None = None
    mode: Literal["smart", "single", "double"] | None = None
    enabled: bool | None = None


@router.get("/schedules")
def list_schedules(svc: Svc, _: Principal) -> dict[str, Any]:
    return {"waiting_for_ntp": svc.scheduler.waiting_for_ntp, "timezone": svc.cfg.scheduler.timezone,
            "schedules": svc.scheduler.upcoming(limit=0)}


@router.post("/schedules", status_code=201)
def create_schedule(body: ScheduleBody, svc: Svc, _: Principal) -> dict[str, Any]:
    try:
        s = svc.schedules.create(body.name, body.days, body.time, body.mode, body.enabled)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    svc.scheduler.rearm()
    svc.bus.emit("schedule.created", f"Schedule '{s.name}' created ({s.time})", schedule_id=s.id)
    return s.to_dict()


@router.patch("/schedules/{sid}")
def update_schedule(sid: int, body: SchedulePatch, svc: Svc, _: Principal) -> dict[str, Any]:
    try:
        s = svc.schedules.update(sid, **body.model_dump(exclude_none=True))
    except KeyError as exc:
        raise HTTPException(404, "schedule not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    svc.scheduler.rearm()
    svc.bus.emit("schedule.updated", f"Schedule '{s.name}' updated" + ("" if s.enabled else " (disabled)"),
                 schedule_id=s.id)
    return s.to_dict()


@router.delete("/schedules/{sid}")
def delete_schedule(sid: int, svc: Svc, _: Principal) -> dict[str, Any]:
    s = svc.schedules.get(sid)
    if s is None:
        raise HTTPException(404, "schedule not found")
    svc.schedules.delete(sid)
    svc.scheduler.rearm()
    svc.bus.emit("schedule.deleted", f"Schedule '{s.name}' deleted", schedule_id=sid)
    return {"ok": True}


# ---------------------------------------------------------------- maintenance
@router.get("/maintenance")
def maintenance(svc: Svc, _: Principal) -> dict[str, Any]:
    return {"counters": svc.maintenance.summary(), "reminders": svc.maintenance.reminders(),
            "service_log": svc.maintenance.service_log(20)}


class ServicedBody(BaseModel):
    note: str = Field("", max_length=200)


@router.post("/maintenance/{rid}/serviced")
def mark_serviced(rid: str, svc: Svc, _: Principal, body: ServicedBody | None = None) -> dict[str, Any]:
    try:
        return svc.maintenance.mark_serviced(rid, (body.note if body else ""))
    except KeyError as exc:
        raise HTTPException(404, "unknown reminder") from exc


# ---------------------------------------------------------------- calibration
def _calibration(svc: Services) -> dict[str, Any]:
    cur, file_cfg, ov = svc.cfg, svc.config.file_config, svc.config.overrides
    fields = {}
    for key in CALIBRATION_FIELDS:
        section, name = key.split(".")
        fields[key] = {
            "value": getattr(getattr(cur, section), name),
            "file_value": getattr(getattr(file_cfg, section), name),
            "overridden": key in ov,
        }
    return {"fields": fields, "hard_max_on_s": cur.safety.hard_max_on_s}


@router.get("/calibration")
def get_calibration(svc: Svc, _: Principal) -> dict[str, Any]:
    return _calibration(svc)


class CalibrationBody(BaseModel):
    values: dict[str, float | int | None]


@router.put("/calibration")
def put_calibration(body: CalibrationBody, svc: Svc, _: Principal) -> dict[str, Any]:
    try:
        svc.config.set_overrides(body.values)
    except ConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    svc.bus.emit("config.calibration", "Calibration updated",
                 values={k: v for k, v in body.values.items()})
    return _calibration(svc)


@router.delete("/calibration")
def reset_calibration(svc: Svc, _: Principal) -> dict[str, Any]:
    svc.config.reset_overrides()
    svc.bus.emit("config.calibration", "Calibration reset to config.yaml values")
    return _calibration(svc)


# ---------------------------------------------------------------- tokens / password
class TokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@router.get("/tokens")
def list_tokens(auth: Annotated[Auth, Depends(auth_dep)], _: Principal) -> list[dict[str, Any]]:
    return auth.list_tokens()


@router.post("/tokens", status_code=201)
def create_token(body: TokenBody, auth: Annotated[Auth, Depends(auth_dep)], svc: Svc,
                 _: Principal) -> dict[str, Any]:
    token = auth.create_token(body.name)
    svc.bus.emit("auth.token", f"API token '{body.name}' created")
    return {"name": body.name, "token": token}


@router.delete("/tokens/{tid}")
def revoke_token(tid: int, auth: Annotated[Auth, Depends(auth_dep)], svc: Svc, _: Principal) -> dict[str, Any]:
    if not auth.revoke_token(tid):
        raise HTTPException(404, "token not found")
    svc.bus.emit("auth.token", f"API token #{tid} revoked")
    return {"ok": True}


class PasswordBody(BaseModel):
    current: str
    new: str = Field(min_length=8, max_length=256)


@router.post("/password")
def change_password(body: PasswordBody, auth: Annotated[Auth, Depends(auth_dep)], svc: Svc,
                    principal: Principal) -> dict[str, Any]:
    if principal != "session":
        raise HTTPException(403, "password can only be changed from the dashboard")
    if not auth.check_password(body.current):
        raise HTTPException(401, "current password is wrong")
    auth.set_password(body.new)
    svc.bus.emit("auth.password", "Dashboard password changed")
    return {"ok": True, "relogin": True}


# ---------------------------------------------------------------- backup
@router.get("/backup")
def backup(svc: Svc, _: Principal) -> Response:
    buf = io.BytesIO()
    stamp = svc.clock.now().astimezone(svc.cfg.tz).strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "dyson.db"
        svc.db.backup_to(snap)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snap, "dyson.db")
            src = svc.config.source_path
            if src and src.exists():
                zf.write(src, "config.yaml")
            zf.writestr("calibration-overrides.json", json.dumps(svc.config.overrides, indent=2))
            zf.writestr("README.txt", f"dyson-pi backup {stamp} (v{__version__}).\n"
                        "Restore: stop the service, copy dyson.db to /var/lib/dyson-pi/ and config.yaml "
                        "to /etc/dyson-pi/, fix ownership (chown dyson-pi), start the service.\n")
    svc.bus.emit("backup.export", "Backup downloaded")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="dyson-pi-backup-{stamp}.zip"'})


# ---------------------------------------------------------------- simulator (mock backend only)
class SimPresence(BaseModel):
    present: bool


class SimAuto(BaseModel):
    enabled: bool
    clean_duration_s: float | None = Field(None, ge=5, le=7200)
    awake: bool | None = None


def _sim(svc: Services):
    if svc.sim_robot is None:
        raise HTTPException(404, "simulator only available with the mock backend")
    return svc.sim_robot


@router.post("/sim/presence")
def sim_presence(body: SimPresence, svc: Svc, _: Principal) -> dict[str, Any]:
    sim = _sim(svc)
    sim.enabled = False
    sim.docked = body.present
    sim._write_sensor()
    return sim.describe()


@router.post("/sim/auto")
def sim_auto(body: SimAuto, svc: Svc, _: Principal) -> dict[str, Any]:
    sim = _sim(svc)
    sim.enabled = body.enabled
    if body.clean_duration_s is not None:
        sim.clean_duration_s = body.clean_duration_s
    if body.awake is not None:
        sim.awake = body.awake
    return sim.describe()
