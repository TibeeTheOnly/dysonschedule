"""Service entry point: ``python -m dyson_pi [--config PATH] [--mock]``."""

from __future__ import annotations

import argparse
import faulthandler
import logging
import os
import signal
import sys
from pathlib import Path

from .config import ConfigError, ConfigManager, dev_defaults, load_raw, resolve_config_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dyson-pi", description="Dyson Vis Nav button-pusher service")
    p.add_argument("--config", help="config file (default /etc/dyson-pi/config.yaml or $DYSON_PI_CONFIG)")
    p.add_argument("--mock", action="store_true", help="force the mock GPIO backend (laptop development)")
    p.add_argument("--host", help="override web.host")
    p.add_argument("--port", type=int, help="override web.port")
    p.add_argument("--dev-password", help="(mock only) set this dashboard password if none is set")
    args = p.parse_args(argv)

    faulthandler.enable()
    path = resolve_config_path(args.config)
    try:
        if path.exists():
            raw = load_raw(path)
        elif args.mock:
            raw = dev_defaults(Path.cwd() / ".dev-state")
            path = None  # type: ignore[assignment]
        else:
            print(f"config not found: {path} (use --mock for development)", file=sys.stderr)
            return 2
        if args.mock:
            raw.setdefault("hardware", {})["backend"] = "mock"
        if args.host:
            raw.setdefault("web", {})["host"] = args.host
        if args.port:
            raw.setdefault("web", {})["port"] = args.port
        config = ConfigManager(raw, source_path=path)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    from .logging_setup import setup_logging

    cfg = config.current
    setup_logging(cfg.logging, cfg.paths.log_dir, under_systemd="INVOCATION_ID" in os.environ)
    log = logging.getLogger("dyson_pi")

    from .app import build_services

    svc = build_services(config, force_mock=args.mock)

    # Belt and braces: relay OFF on any signal, even before uvicorn installs its handlers.
    # (uvicorn swaps in its own handlers while serving and re-raises the signal
    # after a graceful shutdown, which lands here: exit cleanly.)
    def _sig(signum: int, _frame: object) -> None:
        svc.relay.force_off(f"signal {signum}")
        raise SystemExit(0)

    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, _sig)

    if args.dev_password and svc.pins.is_mock:
        from .web.auth import Auth

        auth = Auth(svc.db, svc.clock, lambda: svc.cfg)
        if not auth.password_set:
            auth.set_password(args.dev_password)
            log.warning("dev password set from --dev-password")

    import uvicorn

    from .web.server import create_app

    app = create_app(svc)
    log.info("starting web server on %s:%d", cfg.web.host, cfg.web.port)
    try:
        uvicorn.run(app, host=cfg.web.host, port=cfg.web.port, log_config=None, access_log=False,
                    proxy_headers=False, timeout_graceful_shutdown=5)
    finally:
        svc.relay.force_off("process exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
