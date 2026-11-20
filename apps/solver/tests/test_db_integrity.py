"""The database itself makes double-booking and cross-tenant references impossible.

Every test here goes around the application and writes rows directly, the way
a buggy endpoint, a script or a pgAdmin session could.
"""

import threading
import time

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from models import CoachBooking, Event, Organization, Sport, TeamBooking, TeamCoach, TimeSlot, OfficialBooking
from tests.helpers import add_game, add_slot, assign_official, local, make_league, rejected


def count(db, model, **filters) -> int:
    query = select(func.count()).select_from(model)
    for column, value in filters.items():
        query = query.where(getattr(model, column) == value)
    return db.scalar(query)


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------


def test_field_cannot_hold_overlapping_slots(db):
    league = make_league(db)
    add_slot(db, league, 0, local(2027, 4, 5, 16))
    with rejected(db, "23P01", "time_slots_no_field_overlap"):
        add_slot(db, league, 0, local(2027, 4, 5, 17))
    add_slot(db, league, 0, local(2027, 4, 5, 18))  # back-to-back is fine
    add_slot(db, league, 1, local(2027, 4, 5, 17))  # another field is fine


def test_game_cannot_occupy_two_slots(db):
    league = make_league(db)
    slot = add_slot(db, league, 0, local(2027, 4, 5, 16))
    game = add_game(db, league, "A", "B", slot)
    other_slot = add_slot(db, league, 1, local(2027, 4, 6, 16))
    with rejected(db, "23505", "time_slots_event_id_key"):
        db.execute(update(TimeSlot).where(TimeSlot.id == other_slot.id).values(event_id=game.id))


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


def test_team_cannot_play_two_overlapping_games_on_different_fields(db):
    league = make_league(db)
    add_game(db, league, "A", "B", add_slot(db, league, 0, local(2027, 4, 5, 16)))
    with rejected(db, "23P01", "team_bookings_no_overlap"):
        add_game(db, league, "A", "C", add_slot(db, league, 1, local(2027, 4, 5, 17)))


def test_back_to_back_games_are_allowed(db):
    league = make_league(db)
    add_game(db, league, "A", "B", add_slot(db, league, 0, local(2027, 4, 5, 16)))
    add_game(db, league, "A", "C", add_slot(db, league, 1, local(2027, 4, 5, 18)))
    assert count(db, TeamBooking, team_id=league.teams["A"].id) == 2


def test_moving_a_slot_into_a_conflict_is_rejected(db):
    league = make_league(db)
    add_game(db, league, "A", "B", add_slot(db, league, 0, local(2027, 4, 5, 16)))
    later = add_slot(db, league, 1, local(2027, 4, 5, 19))
    add_game(db, league, "A", "C", later)
    with rejected(db, "23P01", "team_bookings_no_overlap"):
        later.start_time = local(2027, 4, 5, 17)
        later.end_time = local(2027, 4, 5, 19)


def test_changing_a_games_team_into_a_conflict_is_rejected(db):
    league = make_league(db)
    add_game(db, league, "A", "B", add_slot(db, league, 0, local(2027, 4, 5, 16)))
    game = add_game(db, league, "C", "D", add_slot(db, league, 1, local(2027, 4, 5, 16)))
    with rejected(db, "23P01", "team_bookings_no_overlap"):
        game.home_team_id = league.teams["A"].id


# ---------------------------------------------------------------------------
# Coaches
# ---------------------------------------------------------------------------


def test_shared_coach_cannot_be_in_two_divisions_at_once(db):
    league = make_league(
        db,
        divisions={"12U": ["1A", "1B"], "16U": ["3C", "3D"]},
        coach_for={"Coach Shared": ["1A", "3C"], "Coach 1B": ["1B"], "Coach 3D": ["3D"]},
    )
    add_game(db, league, "1A", "1B", add_slot(db, league, 0, local(2027, 4, 5, 16)))
    with rejected(db, "23P01", "coach_bookings_no_overlap"):
        add_game(db, league, "3C", "3D", add_slot(db, league, 1, local(2027, 4, 5, 16)))


def test_assigning_a_coach_who_would_be_double_booked_is_rejected(db):
    league = make_league(db)
    add_game(db, league, "A", "B", add_slot(db, league, 0, local(2027, 4, 5, 16)))
    add_game(db, league, "C", "D", add_slot(db, league, 1, local(2027, 4, 5, 16)))
    with rejected(db, "23P01", "coach_bookings_no_overlap"):
        db.add(
            TeamCoach(
                organization_id=league.org.id,
                team_id=league.teams["C"].id,
                coach_id=league.coaches["Coach A"].id,
            )
        )


def test_coach_of_both_teams_in_one_game_is_booked_once(db):
    league = make_league(db, coach_for={"Coach X": ["A", "B"], "Coach C": ["C"], "Coach D": ["D"]})
    game = add_game(db, league, "A", "B", add_slot(db, league, 0, local(2027, 4, 5, 16)))
    assert count(db, CoachBooking, event_id=game.id) == 1


# ---------------------------------------------------------------------------
# Officials
# ---------------------------------------------------------------------------


def test_official_cannot_work_two_overlapping_games(db):
    league = make_league(db, officials=1)
    first = add_game(db, league, "A", "B", add_slot(db, league, 0, local(2027, 4, 5, 16)))
    second = add_game(db, league, "C", "D", add_slot(db, league, 1, local(2027, 4, 5, 17)))
    assign_official(db, league, first, "Ref 1")
    with rejected(db, "23P01", "official_bookings_no_overlap"):
        assign_official(db, league, second, "Ref 1")


# ---------------------------------------------------------------------------
# Booking lifecycle
# ---------------------------------------------------------------------------


def test_cancelling_a_game_frees_its_teams_coaches_and_officials(db):
    league = make_league(db, officials=1)
    game = add_game(db, league, "A", "B", add_slot(db, league, 0, local(2027, 4, 5, 16)))
    assign_official(db, league, game, "Ref 1")
    assert count(db, TeamBooking, event_id=game.id) == 2
    assert count(db, CoachBooking, event_id=game.id) == 2
    assert count(db, OfficialBooking, event_id=game.id) == 1

    game.status = "Cancelled"
    db.flush()
    assert count(db, TeamBooking, event_id=game.id) == 0
    assert count(db, CoachBooking, event_id=game.id) == 0
    assert count(db, OfficialBooking, event_id=game.id) == 0
    # The teams are free again at that time
    add_game(db, league, "A", "C", add_slot(db, league, 1, local(2027, 4, 5, 16)))


def test_unlinking_a_slot_frees_the_bookings(db):
    league = make_league(db)
    slot = add_slot(db, league, 0, local(2027, 4, 5, 16))
    game = add_game(db, league, "A", "B", slot)
    slot.event_id = None
    db.flush()
    assert count(db, TeamBooking, event_id=game.id) == 0


def test_two_games_can_swap_slots_in_one_transaction(db):
    league = make_league(db)
    early = add_slot(db, league, 0, local(2027, 4, 5, 16))
    late = add_slot(db, league, 1, local(2027, 4, 5, 18))
    first = add_game(db, league, "A", "B", early)
    second = add_game(db, league, "A", "C", late)

    db.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    db.execute(update(TimeSlot).where(TimeSlot.id == early.id).values(event_id=second.id))
    db.execute(update(TimeSlot).where(TimeSlot.id == late.id).values(event_id=first.id))
    db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))  # runs the deferred checks now

    bookings = dict(
        db.execute(
            select(TeamBooking.event_id, TeamBooking.during).where(TeamBooking.team_id == league.teams["A"].id)
        ).all()
    )
    assert bookings[first.id].lower == local(2027, 4, 5, 18)
    assert bookings[second.id].lower == local(2027, 4, 5, 16)


def test_booking_tables_cannot_be_written_directly(db):
    league = make_league(db)
    game = add_game(db, league, "A", "B", add_slot(db, league, 0, local(2027, 4, 5, 16)))
    one_hour = text("tstzrange(now(), now() + interval '1 hour')")
    coach_id = league.coaches["Coach C"].id
    with rejected(db, "P0001"):
        db.execute(delete(TeamBooking).where(TeamBooking.event_id == game.id))
    with rejected(db, "P0001"):
        db.execute(insert(CoachBooking).values(event_id=game.id, coach_id=coach_id, during=one_hour))
    with rejected(db, "P0001"):
        db.execute(update(TeamBooking).values(during=one_hour))
    assert count(db, TeamBooking, event_id=game.id) == 2


def test_deleting_parents_with_scheduled_games_cascades_cleanly(db):
    league = make_league(db, officials=1)
    game = add_game(db, league, "A", "B", add_slot(db, league, 0, local(2027, 4, 5, 16)))
    assign_official(db, league, game, "Ref 1")
    add_game(db, league, "C", "D", add_slot(db, league, 1, local(2027, 4, 5, 16)))

    db.delete(league.coaches["Coach A"])
    db.flush()
    assert count(db, CoachBooking, event_id=game.id) == 1

    db.delete(league.officials["Ref 1"])
    db.flush()
    assert count(db, OfficialBooking, event_id=game.id) == 0

    db.execute(delete(Organization).where(Organization.id == league.org.id))
    assert count(db, Event, organization_id=league.org.id) == 0
    assert count(db, TeamBooking, event_id=game.id) == 0
    assert count(db, CoachBooking, event_id=game.id) == 0


# ---------------------------------------------------------------------------
# Cross-organization / cross-division references
# ---------------------------------------------------------------------------


def test_game_teams_must_be_in_the_games_division(db):
    league = make_league(db, divisions={"12U": ["A", "B"], "14U": ["C", "D"]})
    with rejected(db, "23503", "events_away_team_division_fkey"):
        db.add(
            Event(
                organization_id=league.org.id,
                season_id=league.season.id,
                division_id=league.divisions["12U"].id,
                home_team_id=league.teams["A"].id,
                away_team_id=league.teams["C"].id,
            )
        )


def test_rows_cannot_point_into_another_organization(db):
    mine = make_league(db)
    theirs = make_league(db)

    with rejected(db, "23503", "time_slots_field_org_fkey"):
        db.add(
            TimeSlot(
                organization_id=mine.org.id,
                season_id=mine.season.id,
                field_id=theirs.fields[0].id,
                start_time=local(2027, 4, 5, 16),
                end_time=local(2027, 4, 5, 18),
            )
        )
    with rejected(db, "23503", "team_coaches_coach_org_fkey"):
        db.add(
            TeamCoach(
                organization_id=mine.org.id,
                team_id=mine.teams["A"].id,
                coach_id=theirs.coaches["Coach A"].id,
            )
        )
    with rejected(db, "23503", "events_season_org_fkey"):
        db.add(
            Event(
                organization_id=mine.org.id,
                season_id=theirs.season.id,
                division_id=theirs.divisions["12U"].id,
                home_team_id=theirs.teams["A"].id,
                away_team_id=theirs.teams["B"].id,
            )
        )


def test_game_cannot_use_a_slot_from_another_season(db):
    mine = make_league(db)
    theirs = make_league(db)
    game = add_game(db, mine, "A", "B")
    slot = add_slot(db, theirs, 0, local(2027, 4, 5, 16))
    with rejected(db, "23503"):
        db.execute(update(TimeSlot).where(TimeSlot.id == slot.id).values(event_id=game.id))


def test_team_with_games_cannot_silently_change_division(db):
    league = make_league(db, divisions={"12U": ["A", "B"], "14U": ["C", "D"]})
    add_game(db, league, "A", "B")
    with rejected(db, "23503"):
        league.teams["A"].division_id = league.divisions["14U"].id


# ---------------------------------------------------------------------------
# Value checks
# ---------------------------------------------------------------------------


def test_organization_timezone_must_be_an_iana_name(db):
    league = make_league(db)
    for bad in ("EST", "UTC+5", "Mars/Olympus_Mons", ""):
        with rejected(db, "23514", "organizations_timezone_check"):
            league.org.timezone = bad
    for good in ("Europe/London", "UTC", "America/Los_Angeles"):
        league.org.timezone = good
        db.flush()


def test_season_and_division_values_are_checked(db):
    league = make_league(db)
    with rejected(db, "23514", "seasons_date_order_check"):
        league.season.end_date = league.season.start_date.replace(day=1)
    with rejected(db, "23514", "divisions_games_per_team_check"):
        league.divisions["12U"].games_per_team = 0
    with rejected(db, "23514", "divisions_max_games_per_week_check"):
        league.divisions["12U"].max_games_per_week = -1


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_transactions_cannot_double_book_a_team(database_ready):
    """Two sessions race to book the same team at the same time; exactly one wins."""
    from database import engine

    with Session(engine) as setup:
        league = make_league(setup, divisions={"12U": ["A", "B", "C"]})
        slot_one = add_slot(setup, league, 0, local(2027, 4, 5, 16))
        slot_two = add_slot(setup, league, 1, local(2027, 4, 5, 16))
        game_one = add_game(setup, league, "A", "B")
        game_two = add_game(setup, league, "A", "C")
        org_id, sport_id = league.org.id, league.sport.id
        ids = (slot_one.id, slot_two.id, game_one.id, game_two.id)
        setup.commit()

    slot_one_id, slot_two_id, game_one_id, game_two_id = ids
    link = text("UPDATE time_slots SET event_id = :event WHERE id = :slot")
    outcome: dict[str, str] = {}

    def second_session() -> None:
        try:
            with engine.begin() as second:
                second.execute(text("SET LOCAL lock_timeout = '20s'"))
                second.execute(link, {"event": game_two_id, "slot": slot_two_id})
            outcome["result"] = "committed"
        except DBAPIError as exc:
            outcome["result"] = exc.orig.sqlstate
        except Exception as exc:  # surface anything unexpected in the assertion below
            outcome["result"] = repr(exc)

    first = engine.connect()
    try:
        first_tx = first.begin()
        first.execute(link, {"event": game_one_id, "slot": slot_one_id})  # not committed yet

        racer = threading.Thread(target=second_session)
        racer.start()
        time.sleep(1.0)
        assert racer.is_alive(), f"the second session should wait for the first to finish: {outcome}"
        first_tx.commit()
        racer.join(timeout=30)

        assert outcome == {"result": "23P01"}
    finally:
        first.close()  # rolls back if the test failed before committing
        with Session(engine) as cleanup:
            cleanup.execute(delete(Organization).where(Organization.id == org_id))
            cleanup.execute(delete(Sport).where(Sport.id == sport_id))
            cleanup.commit()
