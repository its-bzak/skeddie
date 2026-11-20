"""Turning unavailability rows into concrete UTC intervals.

Shared by the slot carver (which skips blacked-out slots) and the problem
loader (team and coach availability, field blackouts), so both read a row the
same way. Rows are local times in the organization timezone; a row with no
start/end time covers the whole day.
"""

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from models import Field, Unavailability
from services.timeutil import Interval, dates_on_weekday, local_day_to_utc, local_span_to_utc


def _days_covered(row: Unavailability, first_day: date, last_day: date) -> list[date]:
    if row.day_of_week is not None:
        return list(dates_on_weekday(first_day, last_day, row.day_of_week))
    start = max(row.specific_date, first_day)
    end = min(row.end_date or row.specific_date, last_day)
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def unavailability_intervals(row: Unavailability, first_day: date, last_day: date, tz: ZoneInfo) -> list[Interval]:
    """Every window a row covers between first_day and last_day (inclusive), in UTC.

    Weekly rows repeat on their day_of_week; dated rows cover specific_date
    through end_date. Windows are widened to the safe side around DST changes.
    """
    reason = row.reason or "unavailable"
    intervals = []
    for day in _days_covered(row, first_day, last_day):
        if row.start_time is None:
            start, end = local_day_to_utc(day, tz)
        else:
            start, end = local_span_to_utc(day, row.start_time, row.end_time, tz)
        intervals.append(Interval(start, end, reason))
    return intervals


def load_field_blackouts(
    db: Session,
    organization_id: uuid.UUID,
    fields: list[Field],
    first_day: date,
    last_day: date,
    tz: ZoneInfo,
) -> dict[uuid.UUID, list[Interval]]:
    """Blackout windows per field: its own rows, its venue's rows and organization-wide rows."""
    blackouts: dict[uuid.UUID, list[Interval]] = {f.id: [] for f in fields}
    if not fields:
        return blackouts

    venue_ids = {f.venue_id for f in fields if f.venue_id is not None}
    targets = [Unavailability.target_type == "organization", Unavailability.field_id.in_(list(blackouts))]
    if venue_ids:
        targets.append(Unavailability.venue_id.in_(list(venue_ids)))
    rows = db.scalars(
        select(Unavailability).where(Unavailability.organization_id == organization_id, or_(*targets))
    ).all()

    for row in rows:
        intervals = unavailability_intervals(row, first_day, last_day, tz)
        for f in fields:
            applies = (
                row.target_type == "organization"
                or row.field_id == f.id
                or (row.venue_id is not None and row.venue_id == f.venue_id)
            )
            if applies:
                blackouts[f.id].extend(intervals)
    return blackouts


def overlaps_any(intervals: list[Interval], start: datetime, end: datetime) -> bool:
    return any(interval.overlaps(start, end) for interval in intervals)
