"""dysonctl: command-line control and hardware tests.

By default commands go through the running service's local API, so they obey
the same lock, cooldown and limits as the dashboard. ``--direct`` drives the
GPIO from this process and is only allowed while the service is stopped.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
from pathlib import Path
from typing import Any

from ..config import ConfigError, ConfigManager, dev_defaults, load_raw, resolve_config_path
from .client import ApiError, Client, ServiceUnavailable, read_token


# ------------------------------------------------------------------ helpers
def _config(args: argparse.Namespace) -> ConfigManager:
    path = resolve_config_path(args.config)
    if path.exists():
        raw = load_raw(path)
    elif args.mock:
        raw, path = dev_defaults(Path.cwd() / ".dev-state"), None  # type: ignore[assignment]
    else:
        raise ConfigError(f"config not found: {path} (pass --config, or --mock for development)")
    if args.mock:
        raw.setdefault("hardware", {})["backend"] = "mock"
    return ConfigManager(raw, source_path=path)


def _client(args: argparse.Namespace, cfg: ConfigManager) -> Client:
    c = cfg.current
    url = args.url or f"http://127.0.0.1:{c.web.port}"
    token = args.token or read_token(c.paths.cli_token_path)
    return Client(url, token)


def _print_attempt(a: dict[str, Any]) -> int:
    print(f"{a['mode']}: {a['outcome'].upper()} - {a.get('message', '')} ({a.get('presses', 0)} press(es))")
    for line in a.get("log", []) or []:
        print(f"   · {line}")
    return 0 if a["outcome"] in ("success", "completed", "running") else 1


def _ensure_service_stopped(args: argparse.Namespace, cfg: ConfigManager) -> None:
    if _client(args, cfg).alive():
        sys.exit("The service is running and owns the GPIO pins. Drop --direct to go through it, "
                 "or stop it first: sudo systemctl stop dyson-pi")


# ------------------------------------------------------------------ commands
def cmd_status(args: argparse.Namespace, cfg: ConfigManager) -> int:
    s = _client(args, cfg).get("/api/status")
    if args.json:
        print(json.dumps(s, indent=2))
        return 0
    r, a = s["robot"], s["actuator"]
    print(f"Robot:     {r['state']}" + (f" (settling -> {r['settling']})" if r["settling"] else ""))
    if r["predicted_asleep"] is not None:
        print(f"Sleep:     {'probably asleep' if r['predicted_asleep'] else 'probably awake'} (hint only)")
    print(f"Relay:     {'ON' if s['relay']['on'] else 'off'}  backend={s['relay']['backend']}")
    print(f"Actuator:  {'DISABLED' if a['disabled'] else 'enabled'}, presses today {a['presses_today']}/{a['daily_limit']}"
          + (", busy" if a["busy"] else ""))
    la = s.get("last_attempt")
    if la:
        print(f"Last:      {la['mode']} {la['outcome']} at {la['started_at']}: {la['message']}")
    for f in r["sensor_faults"]:
        print(f"FAULT:     {f}")
    if s.get("alert"):
        print(f"ALERT:     {s['alert']['message']}")
    sysi = s["system"]
    temp = f"{sysi['cpu_temp_c']:.1f}°C" if sysi["cpu_temp_c"] is not None else "n/a"
    print(f"System:    {sysi['hostname']} {sysi['ip']} CPU {temp}  {sysi['local_time']}")
    for x in s["schedules"]["upcoming"]:
        if x["enabled"]:
            print(f"Schedule:  {x['name']} {x['days_label']} {x['time']} ({x['mode']}) next {x['next_run']}")
    return 0


def _press(args: argparse.Namespace, cfg: ConfigManager, mode: str) -> int:
    if args.direct:
        _ensure_service_stopped(args, cfg)
        from .direct import direct_services, wait_settled

        with direct_services(cfg, mock=args.mock) as svc:
            wait_settled(svc)
            print(f"Robot presence: {svc.presence.state.value}")
            a = svc.actuator.submit(mode, trigger="cli-direct", dry_run=args.dry_run or None, wait=True)
            return _print_attempt(a.to_dict())
    q = f"/api/press?mode={mode}&wait=true" + ("&dry_run=true" if args.dry_run else "")
    print(f"Running {mode} sequence via the service (this can take up to ~1 min)...")
    return _print_attempt(_client(args, cfg).post(q, timeout=300))


def cmd_press(args: argparse.Namespace, cfg: ConfigManager) -> int:
    return _press(args, cfg, args.mode)


def cmd_test_relay(args: argparse.Namespace, cfg: ConfigManager) -> int:
    if args.direct:
        _ensure_service_stopped(args, cfg)
    print("Clicking the relay (the cylinder will move)...")
    if args.direct:
        from .direct import direct_services

        with direct_services(cfg, mock=args.mock) as svc:
            a = svc.actuator.submit("test", trigger="cli-direct", wait=True, pulse_s=args.pulse)
            return _print_attempt(a.to_dict())
    q = "/api/test/relay" + (f"?pulse_s={args.pulse}" if args.pulse else "")
    return _print_attempt(_client(args, cfg).post(q))


def cmd_test_sensor(args: argparse.Namespace, cfg: ConfigManager) -> int:
    print(f"Watching the presence sensor for {args.seconds:.0f} s: move the robot on/off the dock.")
    print("time   raw       settling  robot")
    end = time.monotonic() + args.seconds
    last = None

    def show(raw: Any, pending: Any, state: str) -> None:
        nonlocal last
        row = (raw, pending, state)
        if row != last:
            t = args.seconds - max(0.0, end - time.monotonic())
            rawt = "-" if raw is None else ("present" if raw else "absent")
            print(f"{t:5.1f}  {rawt:<9} {pending or '-':<9} {state}", flush=True)
            last = row

    if args.direct:
        _ensure_service_stopped(args, cfg)
        from .direct import direct_services

        with direct_services(cfg, mock=args.mock) as svc:
            while time.monotonic() < end:
                p = svc.presence
                show(p.raw_present, p.pending.value if p.pending else None, p.state.value)
                time.sleep(0.05)
        return 0
    client = _client(args, cfg)
    while time.monotonic() < end:
        r = client.get("/api/status")["robot"]
        show(r["raw_present"], r["settling"], r["state"])
        time.sleep(0.25)
    return 0


def cmd_test_press(args: argparse.Namespace, cfg: ConfigManager) -> int:
    return _press(args, cfg, args.mode)


def cmd_disable(args: argparse.Namespace, cfg: ConfigManager) -> int:
    want = args.cmd == "disable"
    r = _client(args, cfg).post("/api/actuator/disable", {"disabled": want})
    print("Actuator", "DISABLED" if r["disabled"] else "enabled")
    return 0


def _auth_direct(cfg: ConfigManager):  # type: ignore[no-untyped-def]
    from ..clock import SystemClock
    from ..db import Database
    from ..web.auth import Auth

    return Auth(Database(cfg.current.paths.db_path), SystemClock(), lambda: cfg.current)


def cmd_set_password(args: argparse.Namespace, cfg: ConfigManager) -> int:
    if args.if_unset and _auth_direct(cfg).password_set:
        print("Dashboard password already set (unchanged).")
        return 0
    if args.stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass("New dashboard password (8+ chars): ")
        if pw != getpass.getpass("Repeat: "):
            print("Passwords do not match", file=sys.stderr)
            return 1
    try:
        _auth_direct(cfg).set_password(pw)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("Password set. Existing dashboard sessions were logged out.")
    return 0


def cmd_token(args: argparse.Namespace, cfg: ConfigManager) -> int:
    auth = _auth_direct(cfg)
    if args.action == "create":
        if not args.name:
            sys.exit("usage: dysonctl token create NAME")
        tok = auth.create_token(args.name)
        print(f"Token '{args.name}' (shown once):\n{tok}\n\nUse: Authorization: Bearer {tok}")
    elif args.action == "list":
        for t in auth.list_tokens():
            print(f"#{t['id']:<3} {t['name']:<20} created {t['created_at']}  last used {t['last_used'] or 'never'}")
    elif args.action == "revoke":
        if not args.name or not args.name.isdigit():
            sys.exit("usage: dysonctl token revoke ID")
        print("revoked" if auth.revoke_token(int(args.name)) else "no such token")
    return 0


def cmd_relay_off(args: argparse.Namespace, cfg: ConfigManager) -> int:
    client = _client(args, cfg)
    if client.alive():
        client.post("/api/actuator/disable", {"disabled": True})
        print("Service is running: actuator DISABLED via API (relay is forced off by the service).")
        return 0
    from .direct import relay_off

    relay_off(cfg, mock=args.mock)
    print("Relay driven OFF.")
    return 0


def cmd_config(args: argparse.Namespace, cfg: ConfigManager) -> int:
    c = cfg.current
    if args.action == "check":
        print(f"OK: {cfg.source_path or '(dev defaults)'} is valid")
        return 0
    if args.action == "get":
        obj: Any = c
        for part in (args.key or "").split("."):
            obj = getattr(obj, part)
        print(str(obj).lower() if isinstance(obj, bool) else obj)
        return 0
    if args.action == "show":
        print(json.dumps(c.model_dump(mode="json"), indent=2))
        return 0
    return 1


def cmd_net_watchdog(args: argparse.Namespace, cfg: ConfigManager) -> int:
    from .netwatch import run_netwatch

    print(run_netwatch(cfg.current))
    return 0


def cmd_health(args: argparse.Namespace, cfg: ConfigManager) -> int:
    h = _client(args, cfg).request("GET", "/api/health", ok_statuses=(503,))
    print(json.dumps(h, indent=2))
    return 0 if h.get("status") == "ok" else 1


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dysonctl", description="Control and test the Dyson Pi button pusher")
    p.add_argument("--config", help="config file (default /etc/dyson-pi/config.yaml)")
    p.add_argument("--url", help="service URL (default http://127.0.0.1:<web.port>)")
    p.add_argument("--token", help="API token (default: read the local CLI token)")
    p.add_argument("--mock", action="store_true", help="use the mock backend (development)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="show robot/relay/schedule status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    def direct_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--direct", action="store_true", help="drive GPIO directly (service must be stopped)")

    s = sub.add_parser("press", help="run a press sequence")
    s.add_argument("--mode", choices=["smart", "single", "double", "force"], default="smart")
    s.add_argument("--dry-run", action="store_true", help="log only, never energize the relay")
    direct_flags(s)
    s.set_defaults(fn=cmd_press)

    t = sub.add_parser("test", help="hardware tests").add_subparsers(dest="test", required=True)
    s = t.add_parser("relay", help="click the relay with a short pulse")
    s.add_argument("--pulse", type=float, help="pulse length in seconds (default safety.test_pulse_s)")
    direct_flags(s)
    s.set_defaults(fn=cmd_test_relay)
    s = t.add_parser("sensor", help="show the live presence sensor state")
    s.add_argument("--seconds", type=float, default=30.0)
    direct_flags(s)
    s.set_defaults(fn=cmd_test_sensor)
    s = t.add_parser("press", help="run a full press sequence")
    s.add_argument("--mode", choices=["smart", "single", "double", "force"], default="smart")
    s.add_argument("--dry-run", action="store_true")
    direct_flags(s)
    s.set_defaults(fn=cmd_test_press)

    for name in ("disable", "enable"):
        sub.add_parser(name, help=f"{name} the actuator (emergency toggle)").set_defaults(fn=cmd_disable)

    s = sub.add_parser("set-password", help="set the dashboard password")
    s.add_argument("--stdin", action="store_true", help="read the password from stdin")
    s.add_argument("--if-unset", action="store_true", help="do nothing if a password is already set")
    s.set_defaults(fn=cmd_set_password)

    s = sub.add_parser("token", help="manage API tokens")
    s.add_argument("action", choices=["create", "list", "revoke"])
    s.add_argument("name", nargs="?", help="token name (create) or id (revoke)")
    s.set_defaults(fn=cmd_token)

    sub.add_parser("relay-off", help="emergency: force the relay off").set_defaults(fn=cmd_relay_off)

    s = sub.add_parser("config", help="validate or query the config")
    s.add_argument("action", choices=["check", "get", "show"])
    s.add_argument("key", nargs="?", help="dotted key for 'get', e.g. hardware.relay_pin")
    s.set_defaults(fn=cmd_config)

    sub.add_parser("net-watchdog", help="(timer) check Wi-Fi and reconnect if down").set_defaults(fn=cmd_net_watchdog)
    sub.add_parser("health", help="print /api/health").set_defaults(fn=cmd_health)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = _config(args)
        return int(args.fn(args, cfg) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except ServiceUnavailable as exc:
        print(f"{exc}\nIs it running?  sudo systemctl status dyson-pi   (or use --direct)", file=sys.stderr)
        return 3
    except ApiError as exc:
        print(exc, file=sys.stderr)
        return 4
    except PermissionError as exc:
        print(exc, file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
