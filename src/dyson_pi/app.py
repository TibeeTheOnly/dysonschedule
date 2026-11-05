"""Composition root: builds every component from config and owns their
lifecycle. Tests build the same graph with a FakeClock and mock pins."""

from __future__ import annotations

import atexit
import logging
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, system
from .clock import Clock, SystemClock
from .config import AppConfig, ConfigManager
from .db import KV, Counters, Database
from .events import EventBus, iso
from .hardware.factory import Pins, create_pins
from .hardware.mock import MockInput, MockOutput, SimulatedRobot
from .hardware.presence import Presence, PresenceMonitor
from .hardware.relay import SafeRelay
from .logic.actuator import Actuator
from .logic.maintenance import FanHourMeter, Maintenance
from .logic.robot import RobotTracker
from .scheduler.runner import ScheduleRunner
from .scheduler.store import ScheduleStore

log = logging.getLogger(__name__)


@dataclass
class HostInfo:
    """System probes, swappable in tests."""

    cpu_temp_c: Callable[[], float | None] = system.cpu_temp_c
    uptime_s: Callable[[], float] = system.uptime_s
    boot_id: Callable[[], str] = system.boot_id
    primary_ip: Callable[[], str | None] = system.primary_ip
    hostname: Callable[[], str] = system.hostname
    ntp_synchronized: Callable[[], bool | None] = system.ntp_synchronized
    wifi_ssid: Callable[[str], str | None] = system.wifi_ssid


@dataclass
class Services:
    config: ConfigManager
    clock: Clock
    db: Database
    kv: KV
    counters: Counters
    bus: EventBus
    pins: Pins
    relay: SafeRelay
    presence: PresenceMonitor
    robot: RobotTracker
    maintenance: Maintenance
    fan_meter: FanHourMeter
    actuator: Actuator
    schedules: ScheduleStore
    scheduler: ScheduleRunner
    host: HostInfo
    sim_robot: SimulatedRobot | None = None
    started_at: str = ""
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: list[threading.Thread] = field(default_factory=list)
    _host_cache: dict[str, Any] = field(default_factory=dict)
    _running: bool = False

    @property
    def cfg(self) -> AppConfig:
        return self.config.current

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start background threads. Relay is forced OFF first."""
        if self._running:
            return
        self._running = True
        self.relay.force_off("service start")
        self.relay.start_supervisor()
        self.presence.start()
        self.scheduler.start()
        self.maintenance.ensure_reminders()
        self.fan_meter.update()
        self._stop.clear()
        self._spawn(self._housekeeping_loop, "housekeeping")
        if self.sim_robot is not None:
            self._spawn(self._sim_loop, "sim-robot")
        self.write_cli_token()
        self.write_relay_off_env()
        ip = self.host.primary_ip()
        self.bus.emit("service.started",
                      f"Service started (v{__version__}, backend={self.pins.backend}, IP {ip or 'unknown'})",
                      backend=self.pins.backend, ip=ip, hostname=self.host.hostname())
        atexit.register(self.relay.force_off, "atexit")

    def stop(self) -> None:
        if not self._running:
            self.relay.force_off("service stop")
            return
        self._running = False
        self.actuator.shutdown()
        self.relay.force_off("service stop")
        self._stop.set()
        self.scheduler.stop()
        self.presence.stop()
        for t in self._threads:
            t.join(timeout=2)
        self._threads.clear()
        try:
            self.fan_meter.update()
        except Exception:
            log.exception("fan meter flush failed")
        self.bus.emit("service.stopped", "Service stopping, relay forced OFF")
        self.relay.force_off("service stop")
        self.relay.stop_supervisor()
        self.pins.close()

    def _spawn(self, fn: Callable[[], None], name: str) -> None:
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _housekeeping_loop(self) -> None:
        last_prune = 0.0
        last_temp_log = 0.0
        while not self._stop.wait(60):
            try:
                self.fan_meter.update()
                now = self.clock.monotonic()
                if now - last_temp_log >= 900:
                    last_temp_log = now
                    t = self.host.cpu_temp_c()
                    if t is not None:
                        self.bus.emit("system.temp", f"CPU temperature {t:.1f} °C",
                                      level="warning" if t >= 75 else "info", cpu_temp_c=t)
                if now - last_prune >= 86400:
                    last_prune = now
                    self.bus.prune(self.cfg.logging.event_retention_days)
                    self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(self.clock.now()),))
            except Exception:
                log.exception("housekeeping failed")

    def _sim_loop(self) -> None:
        assert self.sim_robot is not None
        while not self._stop.wait(0.05):
            self.sim_robot.tick()

    def write_cli_token(self) -> None:
        """Local token for `dysonctl` (file is readable by root and the service user)."""
        from .web.auth import Auth

        path: Path = self.cfg.paths.cli_token_path
        try:
            token = secrets.token_urlsafe(32)
            Auth(self.db, self.clock, lambda: self.cfg).replace_named_token("cli-local", token)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(token + "\n", encoding="utf-8")
            try:
                path.chmod(0o640)
            except OSError:
                pass
        except Exception:
            log.exception("could not write CLI token to %s", path)

    def write_relay_off_env(self) -> None:
        """Tell deploy/relay-off.sh (systemd ExecStopPost) which pin/level means
        OFF for the config actually running. systemd sets RUNTIME_DIRECTORY."""
        rundir = os.environ.get("RUNTIME_DIRECTORY")
        if not rundir:
            return
        hw = self.cfg.hardware
        level = "dh" if hw.relay_active_low else "dl"
        try:
            Path(rundir, "relay-off.env").write_text(
                f"RELAY_PIN={hw.relay_pin}\nRELAY_OFF_LEVEL={level}\n", encoding="utf-8")
        except OSError:
            log.exception("could not write relay-off.env")

    # ------------------------------------------------------------------ status
    def _cached_host(self, key: str, fn: Callable[[], Any], ttl: float = 30.0) -> Any:
        now = self.clock.monotonic()
        hit = self._host_cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._host_cache[key] = (now, val)
        return val

    def status(self) -> dict[str, Any]:
        cfg = self.cfg
        now = self.clock.now()
        state = self.presence.state
        since = self.robot.state_since if state is not Presence.UNKNOWN else None
        faults: list[str] = []
        if self.presence.flapping:
            faults.append("Presence sensor is flapping (rapid on/off). Check wiring and the contact relay.")
        if self.robot.stale():
            faults.append(f"No presence change for over {cfg.sensor.stale_after_h:.0f} h. "
                          "If the robot has run since, check the sensor.")
        if self.presence.read_errors:
            faults.append(f"Presence sensor read errors: {self.presence.read_errors}")
        if self.relay.watchdog_trips:
            faults.append(f"Relay watchdog tripped {self.relay.watchdog_trips}× since start")
        current = self.actuator.current
        asleep = self.robot.predicted_asleep()
        return {
            "time": iso(now),
            "robot": {
                "state": {"present": "docked", "absent": "away", "unknown": "unknown"}[state.value],
                "presence": state.value,
                "since": iso(since),
                "seconds_in_state": (now - since).total_seconds() if since else None,
                "settling": self.presence.pending.value if self.presence.pending else None,
                "raw_present": self.presence.raw_present,
                "last_interaction": iso(self.robot.last_interaction),
                "predicted_asleep": asleep,
                "minutes_until_sleep": self.robot.minutes_until_sleep(),
                "sensor_faults": faults,
            },
            "relay": {"on": self.relay.is_on, "dry_run_pulse": self.relay.simulated_on, "backend": self.pins.backend},
            "actuator": {
                "disabled": self.actuator.disabled,
                "busy": self.actuator.busy,
                "current": current.to_dict() if current else None,
                "cooldown_remaining_s": round(self.actuator.cooldown_remaining(), 1),
                "presses_today": self.actuator.presses_today(),
                "daily_limit": cfg.safety.daily_press_limit,
                "dry_run": cfg.safety.dry_run,
            },
            "last_attempt": self.actuator.last_start_attempt(),
            "alert": self.actuator.alert,
            "schedules": {
                "waiting_for_ntp": self.scheduler.waiting_for_ntp,
                "upcoming": self.scheduler.upcoming(limit=5),
            },
            "maintenance": {**self.maintenance.summary(), "due": self.maintenance.due_count()},
            "system": {
                "version": __version__,
                "cpu_temp_c": self._cached_host("temp", self.host.cpu_temp_c, 5),
                "uptime_s": self.host.uptime_s(),
                "ip": self._cached_host("ip", self.host.primary_ip),
                "hostname": self._cached_host("hostname", self.host.hostname, 3600),
                "wifi": self._cached_host("ssid", lambda: self.host.wifi_ssid(cfg.network.interface)),
                "timezone": cfg.scheduler.timezone,
                "local_time": now.astimezone(cfg.tz).strftime("%Y-%m-%d %H:%M:%S"),
                "started_at": self.started_at,
            },
            "simulator": self.sim_robot.describe() if self.sim_robot is not None else None,
        }


def build_services(
    config: ConfigManager,
    clock: Clock | None = None,
    pins: Pins | None = None,
    force_mock: bool = False,
    host: HostInfo | None = None,
    db: Database | None = None,
) -> Services:
    clock = clock or SystemClock()
    cfg = config.current
    db = db or Database(cfg.paths.db_path)
    kv = KV(db)
    counters = Counters(db)
    config.attach_override_store(lambda: kv.get("config.overrides", {}), lambda v: kv.set("config.overrides", v))
    bus = EventBus(db, clock)
    pins = pins or create_pins(cfg.hardware, force_mock=force_mock)
    current = lambda: config.current  # noqa: E731

    relay = SafeRelay(
        pins.relay, clock,
        hard_max_on_s=lambda: config.current.safety.hard_max_on_s,
        on_fault=lambda msg: bus.emit("relay.watchdog", msg, level="error"),
        on_cycle=lambda: counters.incr("relay_cycles"),
    )
    relay.force_off()  # before anything else can happen
    presence = PresenceMonitor(pins.presence, clock, lambda: config.current.sensor,
                               cfg.hardware.presence_closed_means_present)
    robot = RobotTracker(db, kv, counters, bus, clock, current)
    presence.on_change(robot.handle_change)
    maintenance = Maintenance(db, kv, counters, bus, clock, current)
    host = host or HostInfo()
    fan_meter = FanHourMeter(kv, counters, host.uptime_s, host.boot_id)
    actuator = Actuator(relay, presence, robot, db, kv, counters, bus, clock, current)
    store = ScheduleStore(db, clock)
    runner = ScheduleRunner(store, actuator, bus, clock, current, host.ntp_synchronized)

    sim: SimulatedRobot | None = None
    if isinstance(pins.relay, MockOutput) and isinstance(pins.presence, MockInput):
        pins.relay.bind_clock(clock)
        sim = SimulatedRobot(clock, pins.relay, pins.presence,
                             closed_means_present=cfg.hardware.presence_closed_means_present,
                             sleep_after_s=cfg.press.sleep_timeout_min * 60)

    return Services(
        config=config, clock=clock, db=db, kv=kv, counters=counters, bus=bus, pins=pins, relay=relay,
        presence=presence, robot=robot, maintenance=maintenance, fan_meter=fan_meter, actuator=actuator,
        schedules=store, scheduler=runner, host=host, sim_robot=sim, started_at=iso(clock.now()) or "",
    )
