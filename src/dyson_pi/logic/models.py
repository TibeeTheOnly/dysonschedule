"""Shared enums and records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class PressMode(StrEnum):
    SMART = "smart"      # closed loop using the presence sensor
    SINGLE = "single"    # one press (requires robot present)
    DOUBLE = "double"    # wake + start (requires robot present)
    FORCE = "force"      # one press, ignores the presence sensor
    TEST = "test"        # short relay click for hardware testing


class Outcome(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"                    # smart: robot confirmed leaving
    COMPLETED = "completed"                # open-loop modes: presses delivered
    FAILED = "failed"                      # smart: robot still docked after max presses
    REFUSED_NOT_PRESENT = "refused_not_present"
    BLOCKED_DISABLED = "blocked_disabled"
    BLOCKED_COOLDOWN = "blocked_cooldown"
    BLOCKED_BUSY = "blocked_busy"
    BLOCKED_DAILY_LIMIT = "blocked_daily_limit"
    ABORTED = "aborted"                    # stopped mid-sequence (disable/limit/shutdown)
    ERROR = "error"

    @property
    def is_blocked(self) -> bool:
        return self.value.startswith("blocked_") or self is Outcome.REFUSED_NOT_PRESENT


@dataclass
class Attempt:
    mode: PressMode
    trigger: str
    started_at: str
    dry_run: bool = False
    outcome: Outcome = Outcome.RUNNING
    presses: int = 0
    message: str = ""
    finished_at: str | None = None
    id: int | None = None
    log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mode"] = str(self.mode)
        d["outcome"] = str(self.outcome)
        return d
