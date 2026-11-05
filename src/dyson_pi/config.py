"""Configuration: YAML file validated by pydantic, plus a small set of
calibration overrides stored in the database (so the dashboard never rewrites
your commented ``config.yaml``)."""

from __future__ import annotations

import copy
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path("/etc/dyson-pi/config.yaml")


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HardwareConfig(_Base):
    backend: Literal["auto", "gpio", "mock"] = "auto"
    relay_pin: int = Field(17, ge=0, le=27, description="BCM number of the relay IN pin")
    relay_active_low: bool = True
    presence_pin: int = Field(27, ge=0, le=27)
    presence_closed_means_present: bool = True

    @model_validator(mode="after")
    def _distinct_pins(self) -> "HardwareConfig":
        if self.relay_pin == self.presence_pin:
            raise ValueError("relay_pin and presence_pin must differ")
        return self


class PressConfig(_Base):
    press_duration_s: float = Field(1.0, gt=0.05, le=5.0)
    inter_press_delay_s: float = Field(2.5, ge=0.2, le=30.0)
    departure_timeout_s: float = Field(20.0, ge=2.0, le=300.0)
    max_presses: int = Field(2, ge=1, le=4)
    sleep_timeout_min: float = Field(30.0, ge=1.0, le=24 * 60)


class SensorConfig(_Base):
    poll_interval_ms: int = Field(20, ge=5, le=500)
    debounce_ms: int = Field(200, ge=0, le=5000)
    stability_s: float = Field(3.0, ge=0.0, le=120.0)
    flap_max_changes: int = Field(6, ge=2, le=100)
    flap_window_s: float = Field(60.0, ge=5.0, le=3600.0)
    stale_after_h: float = Field(72.0, ge=1.0, le=24 * 60)


class SafetyConfig(_Base):
    hard_max_on_s: float = Field(3.0, gt=0.1, le=10.0)
    cooldown_s: float = Field(5.0, ge=0.0, le=3600.0)
    daily_press_limit: int = Field(20, ge=1, le=1000)
    dry_run: bool = False
    test_pulse_s: float = Field(0.15, gt=0.01, le=1.0)


class SchedulerConfig(_Base):
    timezone: str = "Europe/Budapest"
    require_ntp_sync: bool = True
    misfire_grace_s: float = Field(60.0, ge=1.0, le=3600.0)

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v


class WebConfig(_Base):
    host: str = "0.0.0.0"
    port: int = Field(80, ge=1, le=65535)
    session_days: int = Field(30, ge=1, le=365)
    cookie_secure: bool = False


class NetworkConfig(_Base):
    connection_name: str = "dyson-hotspot"
    interface: str = "wlan0"
    watchdog_down_minutes: float = Field(3.0, ge=1.0, le=120.0)


class PathsConfig(_Base):
    state_dir: Path = Path("/var/lib/dyson-pi")
    log_dir: Path = Path("/var/log/dyson-pi")

    @property
    def db_path(self) -> Path:
        return self.state_dir / "dyson.db"

    @property
    def cli_token_path(self) -> Path:
        return self.state_dir / "cli.token"


class LoggingConfig(_Base):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file_max_mb: float = Field(5.0, gt=0, le=100)
    file_backups: int = Field(5, ge=0, le=50)
    event_retention_days: int = Field(180, ge=1, le=3650)


class ReminderConfig(_Base):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str
    counter: Literal["cylinder_actuations", "relay_cycles", "fan_hours", "cleaning_runs", "days"]
    every: float = Field(gt=0)


def _default_reminders() -> list[ReminderConfig]:
    return [
        ReminderConfig(id="cylinder_seals", title="Inspect cylinder and seals",
                       counter="cylinder_actuations", every=2000),
        ReminderConfig(id="air_supply", title="Check air supply, hoses and fittings",
                       counter="days", every=30),
        ReminderConfig(id="fan_clean", title="Clean the cooling fan",
                       counter="fan_hours", every=1000),
    ]


class MaintenanceConfig(_Base):
    reminders: list[ReminderConfig] = Field(default_factory=_default_reminders)

    @field_validator("reminders")
    @classmethod
    def _unique_ids(cls, v: list[ReminderConfig]) -> list[ReminderConfig]:
        ids = [r.id for r in v]
        if len(ids) != len(set(ids)):
            raise ValueError("maintenance reminder ids must be unique")
        return v


class AppConfig(_Base):
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)
    press: PressConfig = Field(default_factory=PressConfig)
    sensor: SensorConfig = Field(default_factory=SensorConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)

    @model_validator(mode="after")
    def _cross_checks(self) -> "AppConfig":
        if self.press.press_duration_s > self.safety.hard_max_on_s:
            raise ValueError(
                f"press.press_duration_s ({self.press.press_duration_s}) must not exceed "
                f"safety.hard_max_on_s ({self.safety.hard_max_on_s})"
            )
        settle = self.sensor.debounce_ms / 1000 + self.sensor.stability_s
        if self.press.departure_timeout_s <= settle + 1.0:
            raise ValueError(
                f"press.departure_timeout_s ({self.press.departure_timeout_s}) must be more than "
                f"1 s longer than sensor debounce + stability ({settle:.1f} s), otherwise a "
                "departure can never be confirmed in time"
            )
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.scheduler.timezone)


# Fields the dashboard calibration page may override at runtime.
CALIBRATION_FIELDS: dict[str, type] = {
    "press.press_duration_s": float,
    "press.inter_press_delay_s": float,
    "press.departure_timeout_s": float,
    "press.max_presses": int,
    "sensor.debounce_ms": int,
    "sensor.stability_s": float,
}


class ConfigError(Exception):
    pass


def load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _apply_dotted(data: dict[str, Any], dotted: str, value: Any) -> None:
    section, key = dotted.split(".", 1)
    data.setdefault(section, {})
    data[section][key] = value


def build_config(raw: dict[str, Any], overrides: dict[str, Any] | None = None) -> AppConfig:
    merged = copy.deepcopy(raw)
    for dotted, value in (overrides or {}).items():
        if dotted in CALIBRATION_FIELDS:
            _apply_dotted(merged, dotted, value)
    try:
        return AppConfig.model_validate(merged)
    except Exception as exc:  # pydantic.ValidationError
        raise ConfigError(str(exc)) from exc


class ConfigManager:
    """Holds the effective config (file + calibration overrides).

    Components keep a reference to the manager and read ``.current`` on every
    use, so calibration changes apply immediately without a restart.
    """

    def __init__(
        self,
        raw: dict[str, Any],
        source_path: Path | None = None,
        load_overrides: Callable[[], dict[str, Any]] | None = None,
        save_overrides: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._raw = raw
        self.source_path = source_path
        self._load_overrides = load_overrides
        self._save_overrides = save_overrides
        self._lock = threading.Lock()
        self._overrides: dict[str, Any] = {}
        self._current = build_config(raw)

    def attach_override_store(
        self,
        load: Callable[[], dict[str, Any]],
        save: Callable[[dict[str, Any]], None],
    ) -> None:
        """Connect the DB-backed override layer (after the DB is open)."""
        self._load_overrides, self._save_overrides = load, save
        stored = {k: v for k, v in (load() or {}).items() if k in CALIBRATION_FIELDS}
        try:
            self._current = build_config(self._raw, stored)
            self._overrides = stored
        except ConfigError:
            # Stored overrides no longer valid with the current file: drop them.
            self._overrides = {}
            save({})

    @property
    def current(self) -> AppConfig:
        return self._current

    @property
    def file_config(self) -> AppConfig:
        return build_config(self._raw)

    @property
    def overrides(self) -> dict[str, Any]:
        return dict(self._overrides)

    def set_overrides(self, updates: dict[str, Any]) -> AppConfig:
        """Merge ``updates`` (dotted keys; ``None`` removes) and validate."""
        with self._lock:
            new = dict(self._overrides)
            for key, value in updates.items():
                if key not in CALIBRATION_FIELDS:
                    raise ConfigError(f"{key} is not a calibration field")
                if value is None:
                    new.pop(key, None)
                else:
                    new[key] = CALIBRATION_FIELDS[key](value)
            cfg = build_config(self._raw, new)
            self._overrides = new
            self._current = cfg
            if self._save_overrides:
                self._save_overrides(new)
            return cfg

    def reset_overrides(self) -> AppConfig:
        return self.set_overrides({k: None for k in list(self._overrides)})


def dev_defaults(state_dir: Path) -> dict[str, Any]:
    """Config used for laptop development when no config file exists."""
    return {
        "hardware": {"backend": "mock"},
        "scheduler": {"require_ntp_sync": False},
        "web": {"host": "127.0.0.1", "port": 8080},
        "paths": {"state_dir": str(state_dir), "log_dir": str(state_dir / "logs")},
    }


def resolve_config_path(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("DYSON_PI_CONFIG")
    return Path(env) if env else DEFAULT_CONFIG_PATH
