"""Converting an organization's local wall-clock times to UTC instants.

Slot rules and unavailabilities are stored as local times ("Mon 16:00") in the
organization's IANA timezone. Around daylight-saving changes a local time can
be missing (spring-forward gap) or happen twice (fall-back overlap), so every
conversion here says how it treats those cases.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

UTC = timezone.utc


@dataclass(frozen=True)
class Interval:
    """A half-open [start, end) span of UTC instants, matching tstzrange '[)'."""

    start: datetime
    end: datetime
    reason: str = ""

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end


def _instants(day: date, wall: time, tz: ZoneInfo) -> list[datetime]:
    """Every UTC instant that the local wall time on `day` can mean (0, 1 or 2)."""
    naive = datetime.combine(day, wall)
    found: set[datetime] = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        # A wall time inside a spring-forward gap does not round-trip.
        if aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == naive:
            found.add(aware.astimezone(UTC))
    return sorted(found)


def local_to_utc_exact(day: date, wall: time, tz: ZoneInfo) -> datetime | None:
    """The single UTC instant for a local time, or None if it is missing or ambiguous."""
    instants = _instants(day, wall, tz)
    return instants[0] if len(instants) == 1 else None


def local_span_to_utc(day: date, start: time, end: time, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """A local [start, end) window as UTC, widened to the safe side.

    Used for unavailability: when a boundary is ambiguous or falls inside a
    gap, the earliest reading of the start and the latest reading of the end
    are used. The window can only grow, never shrink.
    """
    return _readings(day, start, tz)[0], _readings(day, end, tz)[-1]


def local_day_to_utc(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """A whole local day, from midnight to the next midnight, as UTC (widened like local_span_to_utc)."""
    return _readings(day, time(0), tz)[0], _readings(day + timedelta(days=1), time(0), tz)[-1]


def _readings(day: date, wall: time, tz: ZoneInfo) -> list[datetime]:
    """Both fold readings of a wall time as UTC instants, sorted (may be equal)."""
    naive = datetime.combine(day, wall)
    return sorted(naive.replace(tzinfo=tz, fold=fold).astimezone(UTC) for fold in (0, 1))


def dates_on_weekday(first: date, last: date, weekday: int) -> Iterator[date]:
    """Dates in [first, last] whose weekday() (0 = Monday) equals `weekday`."""
    offset = (weekday - first.weekday()) % 7
    current = first + timedelta(days=offset)
    while current <= last:
        yield current
        current += timedelta(days=7)


def local_date(instant: datetime, tz: ZoneInfo) -> date:
    return instant.astimezone(tz).date()


def iso_week(instant: datetime, tz: ZoneInfo) -> tuple[int, int]:
    """(ISO year, ISO week) in the organization timezone; weeks run Monday-Sunday."""
    year, week, _ = instant.astimezone(tz).isocalendar()
    return year, week
