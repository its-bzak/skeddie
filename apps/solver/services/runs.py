"""Scheduling runs: request a season's schedule, solve it later, record how it went.

A run row tracks one call of schedule_season():
queued -> running -> scheduled | precheck_failed | infeasible | timeout | error.
The database allows only one queued or running run per season, so two admins
cannot start competing solves.

These functions take the caller's session and never open their own, so the
caller decides where a run executes (a background thread in the API, inline
in tests). Runs live in the API process: a restart interrupts them, and
fail_interrupted_runs() marks what was left behind.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import ScheduleRun, Season
from services.persist import schedule_season

log = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running")
INTERRUPTED_MESSAGE = "Interrupted by a server restart; start a new run."


class RunAlreadyActive(Exception):
    def __init__(self, run_id: uuid.UUID):
        self.run_id = run_id
        super().__init__(f"The season already has a scheduling run in progress ({run_id})")


def create_run(db: Session, season: Season, time_limit_seconds: int) -> ScheduleRun:
    """Insert a queued run for the season. The caller commits."""
    run = ScheduleRun(
        organization_id=season.organization_id,
        season_id=season.id,
        status="queued",
        time_limit_seconds=time_limit_seconds,
    )
    try:
        with db.begin_nested():
            db.add(run)
            db.flush()
    except IntegrityError as exc:
        if getattr(exc.orig.diag, "constraint_name", None) != "schedule_runs_one_active_per_season":
            raise
        active_id = db.scalar(
            select(ScheduleRun.id).where(
                ScheduleRun.season_id == season.id, ScheduleRun.status.in_(ACTIVE_STATUSES)
            )
        )
        raise RunAlreadyActive(active_id) from None
    return run


def execute_run(db: Session, run_id: uuid.UUID) -> None:
    """Solve a queued run and record the outcome, committing as it goes.

    A successful schedule is committed; any other outcome is rolled back, which
    also restores the Draft games schedule_season removed. Unexpected errors
    are logged and recorded on the run rather than lost.
    """
    run = db.get(ScheduleRun, run_id)
    run.status = "running"
    run.started_at = datetime.now(timezone.utc)
    db.commit()
    season_id, time_limit = run.season_id, run.time_limit_seconds

    try:
        outcome = schedule_season(db, season_id, time_limit_seconds=time_limit)
    except Exception as exc:
        db.rollback()
        log.exception("Scheduling run %s failed", run_id)
        _finish(db, run_id, status="error", error=f"{type(exc).__name__}: {exc}")
        return

    if outcome.ok:
        db.commit()
    else:
        db.rollback()
    _finish(
        db,
        run_id,
        status=outcome.status,
        games_scheduled=outcome.games_scheduled,
        issues=outcome.issues,
        quality=outcome.quality.as_dict() if outcome.quality else None,
        solver_status=outcome.solver_status,
        solve_seconds=round(outcome.solve_seconds, 3),
    )


def _finish(db: Session, run_id: uuid.UUID, **fields) -> None:
    run = db.get(ScheduleRun, run_id)
    for name, value in fields.items():
        setattr(run, name, value)
    run.finished_at = datetime.now(timezone.utc)
    db.commit()


def fail_interrupted_runs(db: Session) -> int:
    """Mark runs left queued or running (by a restart) as errors. Returns how many."""
    result = db.execute(
        update(ScheduleRun)
        .where(ScheduleRun.status.in_(ACTIVE_STATUSES))
        .values(status="error", error=INTERRUPTED_MESSAGE, finished_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount
