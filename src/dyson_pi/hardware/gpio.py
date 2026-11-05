"""Real GPIO backend: gpiozero on the lgpio pin factory (Raspberry Pi OS Bookworm)."""

from __future__ import annotations

import os
from typing import Any


def _factory() -> Any:
    # lgpio writes notification pipes into its working dir; point it somewhere
    # writable (systemd sets LG_WD=/run/dyson-pi as well).
    os.environ.setdefault("LG_WD", "/tmp")
    from gpiozero.pins.lgpio import LGPIOFactory  # imported lazily: not on laptops

    return LGPIOFactory()


class GpioOutput:
    def __init__(self, pin: int, active_low: bool, factory: Any | None = None) -> None:
        from gpiozero import DigitalOutputDevice

        self._dev = DigitalOutputDevice(
            pin,
            active_high=not active_low,
            initial_value=False,  # relay OFF from the very first instruction
            pin_factory=factory or _factory(),
        )

    def set(self, active: bool) -> None:
        if active:
            self._dev.on()
        else:
            self._dev.off()

    @property
    def is_active(self) -> bool:
        return bool(self._dev.value)

    def close(self) -> None:
        try:
            self._dev.off()
        finally:
            self._dev.close()


class GpioInput:
    def __init__(self, pin: int, factory: Any | None = None) -> None:
        from gpiozero import DigitalInputDevice

        # pull_up=True: an open contact reads HIGH; gpiozero reports
        # value=1 ("active") when the pin is pulled LOW by the closed contact.
        # Debouncing is done in software by PresenceMonitor, not here.
        self._dev = DigitalInputDevice(pin, pull_up=True, bounce_time=None, pin_factory=factory or _factory())

    def read_closed(self) -> bool:
        return bool(self._dev.value)

    def close(self) -> None:
        self._dev.close()


def create_gpio_pins(relay_pin: int, active_low: bool, presence_pin: int) -> tuple[GpioOutput, GpioInput]:
    factory = _factory()
    out = GpioOutput(relay_pin, active_low, factory)
    try:
        inp = GpioInput(presence_pin, factory)
    except Exception:
        out.close()
        raise
    return out, inp
