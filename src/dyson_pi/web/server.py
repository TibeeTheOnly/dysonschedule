"""FastAPI application factory and systemd integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__, system
from ..app import Services
from .api import router
from .auth import SESSION_COOKIE, Auth

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


async def _systemd_watchdog(svc: Services) -> None:
    """Ping the systemd watchdog from the event loop, so a hung loop or a dead
    worker thread gets the service restarted."""
    interval = system.watchdog_interval_s()
    if not interval:
        return
    while True:
        await asyncio.sleep(interval / 3)
        threads_ok = svc.presence._thread is not None and svc.presence._thread.is_alive()
        threads_ok = threads_ok and svc.scheduler._thread is not None and svc.scheduler._thread.is_alive()
        if threads_ok:
            system.sd_notify("WATCHDOG=1")
        else:
            log.error("worker thread died; withholding watchdog ping so systemd restarts us")


def create_app(svc: Services, manage_lifecycle: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        wd: asyncio.Task[None] | None = None
        if manage_lifecycle:
            await asyncio.to_thread(svc.start)
            system.sd_notify("READY=1")
            wd = asyncio.create_task(_systemd_watchdog(svc))
        try:
            yield
        finally:
            if wd:
                wd.cancel()
            if manage_lifecycle:
                system.sd_notify("STOPPING=1")
                await asyncio.to_thread(svc.stop)

    app = FastAPI(title="Dyson Pi", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.svc = svc
    app.state.auth = Auth(svc.db, svc.clock, lambda: svc.cfg)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def _authed(request: Request) -> bool:
        return app.state.auth.session_valid(request.cookies.get(SESSION_COOKIE))

    @app.get("/", include_in_schema=False)
    def index(request: Request) -> Response:
        if not _authed(request):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/login", include_in_schema=False)
    def login_page() -> Response:
        return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-cache"})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    return app
