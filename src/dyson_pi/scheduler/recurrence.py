"""Next-occurrence calculation for "these weekdays at HH:MM local time".

DST rules (Europe/Budapest and friends):
* Spring forward (02:00-03:00 doesn't exist): a 02:30 schedule fires at 03:30
  local that day (the same absolute offset after the gap).
* Fall back (02:00-03:00 happens twice): fires once, at the first occurrence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_time(s: str) -> time:
    m = _TIME_RE.match(s.strip())
    if not m:
        raise ValueError(f"time must be HH:MM (24h), got {s!r}")
    return time(int(m.group(1)), int(m.group(2)))


def normalize_days(days: Iterable[int | str]) -> list[int]:
    out: set[int] = set()
    for d in days:
        if isinstance(d, str):
            key = d.strip().lower()[:3]
            if key not in DAY_NAMES:
                raise ValueError(f"unknown day {d!r}")
            out.add(DAY_NAMES.index(key))
        else:
            if not 0 <= int(d) <= 6:
                raise ValueError(f"day index must be 0 (Mon) .. 6 (Sun), got {d}")
            out.add(int(d))
    if not out:
        raise ValueError("at least one day is required")
    return sorted(out)


def _local_instant(d: date, t: time, tz: ZoneInfo) -> datetime:
    """Map a local wall time to an aware UTC instant, handling DST gaps/folds."""
    naive = datetime.combine(d, t)
    local = naive.replace(tzinfo=tz, fold=0)  # fold=0 -> first of an ambiguous pair
    utc = local.astimezone(timezone.utc)
    # For a nonexistent time, fold=0 uses the pre-transition offset, so the UTC
    # instant lands just after the gap: exactly the behavior documented above.
    return utc


def next_occurrence(after: datetime, days: Iterable[int], at: time, tz: ZoneInfo) -> datetime:
    """First occurrence strictly after ``after`` (aware). Returns aware UTC."""
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    day_set = set(days)
    if not day_set:
        raise ValueError("no days")
    start = after.astimezone(tz).date()
    for offset in range(0, 9):
        d = start + timedelta(days=offset)
        if d.weekday() not in day_set:
            continue
        inst = _local_instant(d, at, tz)
        if inst > after:
            return inst
    raise RuntimeError("no occurrence found within 9 days")  # unreachable with >=1 day


def describe_days(days: Iterable[int]) -> str:
    ds = sorted(set(days))
    if ds == list(range(7)):
        return "Every day"
    if ds == list(range(5)):
        return "Weekdays"
    if ds == [5, 6]:
        return "Weekends"
    return ", ".join(DAY_NAMES[d].capitalize() for d in ds)
