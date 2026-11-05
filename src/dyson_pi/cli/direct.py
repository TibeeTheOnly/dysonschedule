"""--direct mode: drive the GPIO from the CLI process. Only allowed while the
service is stopped (a GPIO line can only be owned by one process)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from ..app import Services, build_services
from ..config import ConfigManager
from ..hardware.factory import create_pins


@contextmanager
def direct_services(config: ConfigManager, mock: bool = False) -> Iterator[Services]:
    svc = build_services(config, force_mock=mock)
    svc.relay.start_supervisor()
    svc.presence.start()
    try:
        yield svc
    finally:
        svc.actuator.shutdown()
        svc.relay.force_off("dysonctl exit")
        svc.presence.stop()
        svc.relay.stop_supervisor()
        svc.pins.close()
        svc.db.close()


def wait_settled(svc: Services, max_s: float | None = None) -> None:
    deadline = time.monotonic() + (max_s or svc.presence.settle_time_s() + 1.0)
    while time.monotonic() < deadline and svc.presence.state.value == "unknown":
        time.sleep(0.05)


def relay_off(config: ConfigManager, mock: bool = False) -> None:
    pins = create_pins(config.current.hardware, force_mock=mock)
    try:
        pins.relay.set(False)
    finally:
        pins.close()
