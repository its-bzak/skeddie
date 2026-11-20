from datetime import datetime, time, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from models import Event, Organization, ScheduleRun, Season, SlotRule, Sport
from routers.scheduling import submit_in_background
from services import runs
from services.runs import INTERRUPTED_MESSAGE, create_run, execute_run, fail_interrupted_runs
from services.slot_carver import generate_time_slots
from tests.helpers import make_league, rejected


def season_of(db, org_name: str) -> Season:
    return db.scalar(select(Season).join(Organization).where(Organization.name == org_name))


def run_row(league_or_season, **columns) -> ScheduleRun:
    season = getattr(league_or_season, "season", league_or_season)
    columns.setdefault("time_limit_seconds", 60)
    return ScheduleRun(organization_id=season.organization_id, season_id=season.id, **columns)


# ---------------------------------------------------------------------------
# Database guarantees
# ---------------------------------------------------------------------------


def test_a_season_can_have_only_one_active_run(db):
    league = make_league(db)
    db.add(run_row(league, status="queued"))
    db.flush()
    with rejected(db, "23505", "schedule_runs_one_active_per_season"):
        db.add(run_row(league, status="running"))

    finished = run_row(league, status="scheduled", finished_at=datetime.now(timezone.utc))
    db.add(finished)
    db.flush()


def test_run_values_are_checked(db):
    league = make_league(db)
    other = make_league(db)
    with rejected(db, "23514", "schedule_runs_status_check"):
        db.add(run_row(league, status="done", finished_at=datetime.now(timezone.utc)))
    with rejected(db, "23514", "schedule_runs_time_limit_check"):
        db.add(run_row(league, status="queued", time_limit_seconds=901))
    with rejected(db, "23514", "schedule_runs_finished_check"):
        db.add(run_row(league, status="scheduled"))
    with rejected(db, "23503", "schedule_runs_season_org_fkey"):
        db.add(ScheduleRun(organization_id=other.org.id, season_id=league.season.id, time_limit_seconds=60))


# ---------------------------------------------------------------------------
# Service behaviour
# ---------------------------------------------------------------------------


def test_create_run_reports_the_run_already_in_progress(db):
    season = season_of(db, "Dan's Organization")
    active = create_run(db, season, 60)
    with pytest.raises(runs.RunAlreadyActive) as info:
        create_run(db, season, 60)
    assert info.value.run_id == active.id


def test_an_unexpected_failure_is_recorded_on_the_run(db, monkeypatch):
    season = season_of(db, "Dan's Organization")
    run = create_run(db, season, 60)
    db.commit()

    def explode(*args, **kwargs):
        raise RuntimeError("solver exploded")

    monkeypatch.setattr(runs, "schedule_season", explode)
    execute_run(db, run.id)

    db.refresh(run)
    assert (run.status, run.error) == ("error", "RuntimeError: solver exploded")
    assert run.started_at is not None and run.finished_at is not None


def test_runs_left_active_by_a_restart_are_marked_as_errors(db):
    dans = season_of(db, "Dan's Organization")
    easton = season_of(db, "Easton Youth Athletics")
    queued = create_run(db, dans, 60)
    running = create_run(db, easton, 60)
    running.status, running.started_at = "running", datetime.now(timezone.utc)
    db.commit()

    assert fail_interrupted_runs(db) == 2
    for run in (queued, running):
        db.refresh(run)
        assert (run.status, run.error) == ("error", INTERRUPTED_MESSAGE)
        assert run.finished_at is not None


# ---------------------------------------------------------------------------
# The real background path: its own thread and its own session
# ---------------------------------------------------------------------------


def test_background_runs_commit_through_their_own_session(database_ready):
    from database import engine

    with Session(engine) as setup:
        league = make_league(setup)
        for day_of_week in (0, 2):
            setup.add(SlotRule(organization_id=league.org.id, season_id=league.season.id,
                               field_id=league.fields[0].id, day_of_week=day_of_week,
                               start_time=time(16), end_time=time(20), slot_duration_mins=120))
        setup.flush()
        generate_time_slots(setup, league.season.id)
        run = create_run(setup, league.season, 60)
        setup.commit()
        org_id, sport_id, season_id, run_id = league.org.id, league.sport.id, league.season.id, run.id

    try:
        submit_in_background(run_id).result(timeout=120)
        with Session(engine) as check:
            finished = check.get(ScheduleRun, run_id)
            assert finished.status == "scheduled", (finished.status, finished.issues, finished.error)
            assert finished.games_scheduled == 6
            assert check.scalars(select(Event).where(Event.season_id == season_id)).all()
    finally:
        with Session(engine) as cleanup:
            cleanup.execute(delete(Organization).where(Organization.id == org_id))
            cleanup.execute(delete(Sport).where(Sport.id == sport_id))
            cleanup.commit()
