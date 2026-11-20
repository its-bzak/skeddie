import uuid
from datetime import date, datetime, time, timezone

import pytest
from sqlalchemy import func, select

from models import Field, Organization, Season, SlotRule, Sport, TimeSlot
from services.slot_carver import (
    RuleSpec,
    SlotGenerationError,
    carve_rule,
    find_overlapping_rules,
    generate_time_slots,
)
from tests.helpers import NEW_YORK, local, make_league

UTC = timezone.utc


def rule(day_of_week: int, start: time, end: time, minutes: int, field_id=None) -> RuleSpec:
    return RuleSpec(uuid.uuid4(), field_id or uuid.uuid4(), None, day_of_week, start, end, minutes)


# ---------------------------------------------------------------------------
# Pure carving
# ---------------------------------------------------------------------------


def test_weekday_rule_produces_full_slots_in_utc():
    result = carve_rule(rule(0, time(16), time(20), 120), date(2027, 4, 1), date(2027, 4, 14), NEW_YORK)
    assert [(s.start, s.end) for s in result.slots] == [
        (datetime(2027, 4, 5, 20, tzinfo=UTC), datetime(2027, 4, 5, 22, tzinfo=UTC)),
        (datetime(2027, 4, 5, 22, tzinfo=UTC), datetime(2027, 4, 6, 0, tzinfo=UTC)),
        (datetime(2027, 4, 12, 20, tzinfo=UTC), datetime(2027, 4, 12, 22, tzinfo=UTC)),
        (datetime(2027, 4, 12, 22, tzinfo=UTC), datetime(2027, 4, 13, 0, tzinfo=UTC)),
    ]
    assert all(s.start.astimezone(NEW_YORK).weekday() == 0 for s in result.slots)
    assert result.skipped == []


def test_partial_trailing_slot_is_dropped():
    result = carve_rule(rule(2, time(16), time(19), 120), date(2027, 4, 7), date(2027, 4, 7), NEW_YORK)
    assert len(result.slots) == 1


def test_season_boundaries_are_inclusive():
    result = carve_rule(rule(3, time(10), time(12), 120), date(2027, 4, 1), date(2027, 4, 1), NEW_YORK)
    assert len(result.slots) == 1  # 2027-04-01 is a Thursday


def test_spring_forward_skips_slots_touching_missing_times():
    # 2027-03-14: 02:00-03:00 does not exist in New York
    result = carve_rule(rule(6, time(1), time(4), 60), date(2027, 3, 14), date(2027, 3, 14), NEW_YORK)
    assert [(s.start, s.end) for s in result.slots] == [
        (datetime(2027, 3, 14, 7, tzinfo=UTC), datetime(2027, 3, 14, 8, tzinfo=UTC)),  # 03:00-04:00 EDT
    ]
    assert len(result.skipped) == 2


def test_fall_back_skips_slots_touching_repeated_times():
    # 2027-11-07: 01:00-02:00 happens twice in New York
    result = carve_rule(rule(6, time(0), time(3), 60), date(2027, 11, 7), date(2027, 11, 7), NEW_YORK)
    assert [(s.start, s.end) for s in result.slots] == [
        (datetime(2027, 11, 7, 7, tzinfo=UTC), datetime(2027, 11, 7, 8, tzinfo=UTC)),  # 02:00-03:00 EST
    ]
    assert len(result.skipped) == 2


def test_overlapping_rules_are_detected_but_adjacent_ones_are_not():
    field_id = uuid.uuid4()
    a = rule(0, time(16), time(18), 60, field_id)
    b = rule(0, time(18), time(20), 60, field_id)
    c = rule(0, time(17), time(19), 60, field_id)
    other_day = rule(1, time(16), time(20), 60, field_id)
    assert find_overlapping_rules([a, b, other_day]) == []
    overlaps = {frozenset((x.rule_id, y.rule_id)) for x, y in find_overlapping_rules([a, b, c])}
    assert overlaps == {frozenset((a.rule_id, c.rule_id)), frozenset((c.rule_id, b.rule_id))}


# ---------------------------------------------------------------------------
# Database generation
# ---------------------------------------------------------------------------


def add_rule(db, league, field_index, day_of_week, start, end, minutes=120, division=None):
    slot_rule = SlotRule(
        organization_id=league.org.id,
        season_id=league.season.id,
        field_id=league.fields[field_index].id,
        division_id=division.id if division else None,
        day_of_week=day_of_week,
        start_time=start,
        end_time=end,
        slot_duration_mins=minutes,
    )
    db.add(slot_rule)
    db.flush()
    return slot_rule


def slot_count(db, season_id) -> int:
    return db.scalar(select(func.count()).select_from(TimeSlot).where(TimeSlot.season_id == season_id))


def test_generation_is_idempotent(db):
    league = make_league(db)  # 2027-04-05 (Mon) to 2027-04-25 (Sun): 15 weekdays
    for dow in range(5):
        add_rule(db, league, 0, dow, time(16), time(20))
    division = league.divisions["12U"]
    add_rule(db, league, 1, 5, time(10), time(14), division=division)

    first = generate_time_slots(db, league.season.id)
    assert (first.created, first.already_present) == (15 * 2 + 3 * 2, 0)

    again = generate_time_slots(db, league.season.id)
    assert (again.created, again.already_present) == (0, 36)
    assert slot_count(db, league.season.id) == 36

    restricted = db.scalars(select(TimeSlot).where(TimeSlot.field_id == league.fields[1].id)).all()
    assert {s.division_id for s in restricted} == {division.id}


def test_rule_on_a_field_of_another_sport_is_rejected(db):
    league = make_league(db)
    soccer = Sport(name=f"Soccer {uuid.uuid4().hex[:6]}")
    db.add(soccer)
    db.flush()
    pitch = Field(organization_id=league.org.id, venue_id=league.venue.id, sport_id=soccer.id, name="Pitch")
    db.add(pitch)
    db.flush()
    league.fields.append(pitch)
    add_rule(db, league, 2, 0, time(16), time(18))

    with pytest.raises(SlotGenerationError, match="different sport"):
        generate_time_slots(db, league.season.id)
    assert slot_count(db, league.season.id) == 0


def test_overlapping_rules_stop_generation(db):
    league = make_league(db)
    add_rule(db, league, 0, 0, time(16), time(20))
    add_rule(db, league, 0, 0, time(19), time(21), minutes=60)
    with pytest.raises(SlotGenerationError, match="overlap"):
        generate_time_slots(db, league.season.id)
    assert slot_count(db, league.season.id) == 0


def test_conflict_with_another_seasons_slot_writes_nothing(db):
    league = make_league(db)
    other_season = Season(
        organization_id=league.org.id,
        sport_id=league.sport.id,
        name="Overlapping season",
        start_date=league.season.start_date,
        end_date=league.season.end_date,
    )
    db.add(other_season)
    db.flush()
    db.add(
        TimeSlot(
            organization_id=league.org.id,
            season_id=other_season.id,
            field_id=league.fields[0].id,
            start_time=local(2027, 4, 12, 17),
            end_time=local(2027, 4, 12, 18),
        )
    )
    db.flush()
    add_rule(db, league, 0, 0, time(16), time(20))

    with pytest.raises(SlotGenerationError) as info:
        generate_time_slots(db, league.season.id)
    assert any("from another season" in p for p in info.value.problems)
    assert slot_count(db, league.season.id) == 0


def test_seed_season_generates_1040_slots(db):
    season = db.scalar(
        select(Season).join(Organization).where(Organization.name == "Dan's Organization")
    )
    result = generate_time_slots(db, season.id)
    assert result.created == 1040  # 4 courts x 13 weeks x (5 weekdays x 2 + 2 weekend days x 5)
    assert result.skipped == []

    easton = db.scalar(select(Season).join(Organization).where(Organization.name == "Easton Youth Athletics"))
    assert generate_time_slots(db, easton.id).created == 0  # Easton has no slot rules
