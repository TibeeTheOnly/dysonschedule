from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from dyson_pi.scheduler.recurrence import describe_days, next_occurrence, normalize_days, parse_time

BUD = ZoneInfo("Europe/Budapest")


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def test_next_same_day_later():
    # Mon 2026-01-05 08:00 UTC = 09:00 local; schedule Mon 10:00 local
    nxt = next_occurrence(utc(2026, 1, 5, 8, 0), [0], time(10, 0), BUD)
    assert nxt == utc(2026, 1, 5, 9, 0)


def test_next_skips_to_following_week():
    nxt = next_occurrence(utc(2026, 1, 5, 10, 0), [0], time(10, 0), BUD)  # 11:00 local, past
    assert nxt == utc(2026, 1, 12, 9, 0)


def test_strictly_after():
    at = utc(2026, 1, 5, 9, 0)
    assert next_occurrence(at, [0], time(10, 0), BUD) == utc(2026, 1, 12, 9, 0)


def test_multiple_days():
    nxt = next_occurrence(utc(2026, 1, 5, 10, 0), [0, 2, 4], time(10, 0), BUD)
    assert nxt == utc(2026, 1, 7, 9, 0)  # Wednesday


def test_summer_offset():
    nxt = next_occurrence(utc(2026, 7, 6, 0, 0), [0], time(10, 0), BUD)
    assert nxt == utc(2026, 7, 6, 8, 0)  # CEST = UTC+2


def test_spring_forward_gap_shifts_after_gap():
    # 2026-03-29 (Sun): 02:00 -> 03:00 local. 02:30 doesn't exist.
    nxt = next_occurrence(utc(2026, 3, 28, 12, 0), [6], time(2, 30), BUD)
    assert nxt.astimezone(BUD).hour == 3 and nxt.astimezone(BUD).minute == 30
    assert nxt.astimezone(BUD).date().isoformat() == "2026-03-29"


def test_fall_back_fires_once():
    # 2026-10-25 (Sun): 03:00 -> 02:00 local. 02:30 happens twice.
    first = next_occurrence(utc(2026, 10, 24, 12, 0), [6], time(2, 30), BUD)
    assert first == utc(2026, 10, 25, 0, 30)  # first 02:30 (CEST, UTC+2)
    second = next_occurrence(first, [6], time(2, 30), BUD)
    assert second.astimezone(BUD).date().isoformat() == "2026-11-01"  # not 01:30 UTC the same day


def test_every_day_across_a_week():
    t = utc(2026, 1, 5, 0, 0)
    seen = []
    for _ in range(7):
        t = next_occurrence(t, range(7), time(9, 15), BUD)
        seen.append(t)
    assert all((b - a) == timedelta(days=1) for a, b in zip(seen, seen[1:]))


def test_parse_and_normalize():
    assert parse_time("07:05") == time(7, 5)
    for bad in ("7:05", "24:00", "12:60", "noon"):
        with pytest.raises(ValueError):
            parse_time(bad)
    assert normalize_days(["mon", "Wednesday", 4, 0]) == [0, 2, 4]
    with pytest.raises(ValueError):
        normalize_days([])
    with pytest.raises(ValueError):
        normalize_days([7])


def test_describe_days():
    assert describe_days(range(7)) == "Every day"
    assert describe_days([0, 1, 2, 3, 4]) == "Weekdays"
    assert describe_days([5, 6]) == "Weekends"
    assert describe_days([0, 3]) == "Mon, Thu"


def test_naive_rejected():
    with pytest.raises(ValueError):
        next_occurrence(datetime(2026, 1, 1), [0], time(1, 0), BUD)
