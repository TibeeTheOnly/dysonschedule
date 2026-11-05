"""Backend selection."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import HardwareConfig
from .base import InputPin, OutputPin
from .mock import MockInput, MockOutput

log = logging.getLogger(__name__)


def is_raspberry_pi() -> bool:
    try:
        return "raspberry pi" in Path("/proc/device-tree/model").read_text(errors="ignore").lower()
    except OSError:
        return False


@dataclass
class Pins:
    relay: OutputPin
    presence: InputPin
    backend: str  # "gpio" or "mock"

    @property
    def is_mock(self) -> bool:
        return self.backend == "mock"

    def close(self) -> None:
        for p in (self.relay, self.presence):
            try:
                p.close()
            except Exception:
                log.exception("closing pin failed")


def create_pins(hw: HardwareConfig, force_mock: bool = False) -> Pins:
    backend = hw.backend
    if force_mock:
        backend = "mock"
    elif backend == "auto":
        backend = "gpio" if is_raspberry_pi() else "mock"

    if backend == "mock":
        log.warning("Using MOCK GPIO backend - no real hardware will be driven")
        return Pins(MockOutput(), MockInput(closed=hw.presence_closed_means_present), "mock")

    # On a real Pi a GPIO failure is fatal: silently falling back to mock would
    # make the dashboard lie about the robot.
    from .gpio import create_gpio_pins

    relay, presence = create_gpio_pins(hw.relay_pin, hw.relay_active_low, hw.presence_pin)
    log.info("GPIO backend ready: relay=GPIO%d (active_%s) presence=GPIO%d",
             hw.relay_pin, "low" if hw.relay_active_low else "high", hw.presence_pin)
    return Pins(relay, presence, "gpio")
