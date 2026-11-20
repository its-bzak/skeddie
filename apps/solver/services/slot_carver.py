"""Turn weekly SlotRules into concrete TimeSlots for a season.

A rule such as "Court 1, Mondays 16:00-20:00, 120-minute slots" becomes one
TimeSlot per full slot on every matching date of the season, converted from the
organization's timezone to UTC.

Generation is all-or-nothing and safe to re-run: slots that already exist are
left alone, and any conflict is reported before anything is written.

Slots that overlap a field, venue or organization blackout are not created.
Slots in the season's makeup window are created; the solver keeps them free so
they stay available for rainout makeups.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Field, Organization, Season, SlotRule, TimeSlot
from services.locks import lock_season
from services.timeutil import dates_on_weekday, local_to_utc_exact
from services.unavailability import load_field_blackouts, overlaps_any

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_REPORTED_PROBLEMS = 25


@dataclass(frozen=True)
class RuleSpec:
    rule_id: uuid.UUID
    field_id: uuid.UUID
    division_id: uuid.UUID | None
    day_of_week: int
    start_time: time
    end_time: time
    slot_duration_mins: int


@dataclass(frozen=True)
class CarvedSlot:
    rule_id: uuid.UUID
    field_id: uuid.UUID
    division_id: uuid.UUID | None
    start: datetime  # UTC
    end: datetime  # UTC


@dataclass
class CarveResult:
    slots: list[CarvedSlot] = field(default_factory=list)
    # Human-readable notes for slots dropped because a boundary falls in a
    # daylight-saving gap or overlap.
    skipped: list[str] = field(default_factory=list)


@dataclass
class SlotGenerationResult:
    created: int
    already_present: int
    blacked_out: int
    skipped: list[str]


class SlotGenerationError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Time slots were not generated:\n" + "\n".join(f"  - {p}" for p in problems))


def carve_rule(rule: RuleSpec, first_day: date, last_day: date, tz: ZoneInfo) -> CarveResult:
    """All full-length slots a rule produces between two dates (inclusive)."""
    result = CarveResult()
    duration = timedelta(minutes=rule.slot_duration_mins)

    for day in dates_on_weekday(first_day, last_day, rule.day_of_week):
        cursor = datetime.combine(day, rule.start_time)
        limit = datetime.combine(day, rule.end_time)
        while cursor + duration <= limit:
            slot_end = cursor + duration
            start_utc = local_to_utc_exact(day, cursor.time(), tz)
            end_utc = local_to_utc_exact(day, slot_end.time(), tz)
            if start_utc is None or end_utc is None:
                result.skipped.append(
                    f"{day.isoformat()} {cursor:%H:%M}-{slot_end:%H:%M}: "
                    "skipped because the clocks change during it"
                )
            else:
                result.slots.append(
                    CarvedSlot(rule.rule_id, rule.field_id, rule.division_id, start_utc, end_utc)
                )
            cursor = slot_end

    return result


def find_overlapping_rules(rules: list[RuleSpec]) -> list[tuple[RuleSpec, RuleSpec]]:
    """Pairs of rules on the same field and weekday whose hours overlap."""
    by_key: dict[tuple[uuid.UUID, int], list[RuleSpec]] = defaultdict(list)
    for rule in rules:
        by_key[(rule.field_id, rule.day_of_week)].append(rule)

    overlaps = []
    for group in by_key.values():
        group.sort(key=lambda r: (r.start_time, r.end_time))
        for i, earlier in enumerate(group):
            for later in group[i + 1 :]:
                if later.start_time >= earlier.end_time:
                    break
                overlaps.append((earlier, later))
    return overlaps


def generate_time_slots(db: Session, season_id: uuid.UUID) -> SlotGenerationResult:
    """Create the season's TimeSlots from its SlotRules. The caller commits.

    Raises SlotGenerationError (and writes nothing) if a rule is on a field of
    the wrong sport, two rules overlap, or a new slot would overlap a slot that
    already exists on the same field (for example from another season).
    """
    season = db.get(Season, season_id)
    if season is None:
        raise LookupError(f"Season {season_id} does not exist")

    lock_season(db, season_id)
    org = db.get(Organization, season.organization_id)
    tz = ZoneInfo(org.timezone)

    rule_rows = db.execute(
        select(SlotRule, Field).join(Field, Field.id == SlotRule.field_id).where(SlotRule.season_id == season_id)
    ).all()
    fields = {f.id: f for _, f in rule_rows}
    rules = [
        RuleSpec(
            r.id, r.field_id, r.division_id, r.day_of_week, r.start_time, r.end_time, r.slot_duration_mins
        )
        for r, _ in rule_rows
    ]

    def describe(rule: RuleSpec) -> str:
        return (
            f"{fields[rule.field_id].name} {DAY_NAMES[rule.day_of_week]} "
            f"{rule.start_time:%H:%M}-{rule.end_time:%H:%M}"
        )

    problems: list[str] = []
    for rule in rules:
        if fields[rule.field_id].sport_id != season.sport_id:
            problems.append(f"Rule {describe(rule)} is on a field for a different sport than the season")
    for earlier, later in find_overlapping_rules(rules):
        problems.append(f"Rules {describe(earlier)} and {describe(later)} overlap")
    if problems:
        raise SlotGenerationError(problems)

    blackouts = load_field_blackouts(
        db, season.organization_id, list(fields.values()), season.start_date, season.end_date, tz
    )
    carved: list[CarvedSlot] = []
    skipped: list[str] = []
    blacked_out = 0
    for rule in rules:
        result = carve_rule(rule, season.start_date, season.end_date, tz)
        for slot in result.slots:
            if overlaps_any(blackouts[slot.field_id], slot.start, slot.end):
                blacked_out += 1
            else:
                carved.append(slot)
        skipped.extend(f"{describe(rule)} on {note}" for note in result.skipped)

    if not carved:
        return SlotGenerationResult(created=0, already_present=0, blacked_out=blacked_out, skipped=skipped)

    window_start = min(s.start for s in carved)
    window_end = max(s.end for s in carved)
    existing = db.scalars(
        select(TimeSlot).where(
            TimeSlot.field_id.in_(fields.keys()),
            TimeSlot.start_time < window_end,
            TimeSlot.end_time > window_start,
        )
    ).all()
    existing_by_field: dict[uuid.UUID, list[TimeSlot]] = defaultdict(list)
    exact: set[tuple[uuid.UUID, datetime, datetime]] = set()
    for slot in existing:
        existing_by_field[slot.field_id].append(slot)
        if slot.season_id == season_id:
            exact.add((slot.field_id, slot.start_time, slot.end_time))

    to_create: list[CarvedSlot] = []
    already_present = 0
    for slot in carved:
        if (slot.field_id, slot.start, slot.end) in exact:
            already_present += 1
            continue
        clash = next(
            (e for e in existing_by_field[slot.field_id] if e.start_time < slot.end and slot.start < e.end_time),
            None,
        )
        if clash is not None:
            local = slot.start.astimezone(tz)
            problems.append(
                f"{fields[slot.field_id].name} {local:%Y-%m-%d %H:%M}: overlaps an existing slot "
                f"({clash.start_time.astimezone(tz):%H:%M}-{clash.end_time.astimezone(tz):%H:%M})"
                + ("" if clash.season_id == season_id else " from another season")
            )
        else:
            to_create.append(slot)

    if problems:
        extra = len(problems) - MAX_REPORTED_PROBLEMS
        reported = problems[:MAX_REPORTED_PROBLEMS]
        if extra > 0:
            reported.append(f"...and {extra} more")
        raise SlotGenerationError(reported)

    db.add_all(
        TimeSlot(
            organization_id=season.organization_id,
            season_id=season_id,
            field_id=s.field_id,
            division_id=s.division_id,
            start_time=s.start,
            end_time=s.end,
        )
        for s in to_create
    )
    db.flush()
    return SlotGenerationResult(
        created=len(to_create), already_present=already_present, blacked_out=blacked_out, skipped=skipped
    )
