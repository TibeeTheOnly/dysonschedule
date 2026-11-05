"""Minimal pin interfaces the rest of the app depends on."""

from __future__ import annotations

from typing import Protocol


class OutputPin(Protocol):
    """Relay driver. ``set(True)`` means *relay energized*; the backend
    handles active-low/active-high polarity."""

    def set(self, active: bool) -> None: ...

    @property
    def is_active(self) -> bool: ...

    def close(self) -> None: ...


class InputPin(Protocol):
    """Dry-contact input with pull-up. ``read_closed()`` is True when the
    contact is closed (GPIO reads LOW)."""

    def read_closed(self) -> bool: ...

    def close(self) -> None: ...
