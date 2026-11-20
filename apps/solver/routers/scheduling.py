"""Scheduling API: generate slots, build a schedule, inspect it, adjust it, publish it.

Every route is scoped to the organization in the URL: a season or event that
belongs to another organization is reported as not found. Authentication is
not implemented yet.

Building a schedule is a background run (large leagues take minutes): POST
starts it and returns 202, then GET the run until its status is final. Runs
execute one at a time in this process, because the solver already uses every
core; a server restart interrupts them (see services/runs.py).

Manual changes (PATCH) are policy overrides: they may ignore unavailability or
weekly caps, but the database still makes double-booking impossible, and any
such conflict is returned as 409 naming who is double-booked.
"""

import logging
import re
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field as PydanticField, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, aliased

from database import SessionLocal, get_db
from models import Coach, Division, Event, Field, Official, ScheduleRun, Season, Team, TimeSlot, User
from services.persist import DEFAULT_TIME_LIMIT_SECONDS
from services.runs import RunAlreadyActive, create_run, execute_run
from services.slot_carver import SlotGenerationError, generate_time_slots

log = logging.getLogger(__name__)

router = APIRouter(prefix="/orgs/{org_id}", tags=["scheduling"])

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SlotGenerationOut(BaseModel):
    created: int
    already_present: int
    blacked_out: int
    skipped: list[str]


class ScheduleRunRequest(BaseModel):
    time_limit_seconds: int = PydanticField(default=DEFAULT_TIME_LIMIT_SECONDS, ge=1, le=900)


class ScheduleRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    season_id: uuid.UUID
    status: str
    time_limit_seconds: int
    games_scheduled: int
    solver_status: str | None
    solve_seconds: float | None
    issues: list[str]
    quality: dict | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TeamRef(BaseModel):
    id: uuid.UUID
    name: str


class SlotRef(BaseModel):
    id: uuid.UUID
    field_id: uuid.UUID
    field_name: str
    start_time: datetime
    end_time: datetime


class EventOut(BaseModel):
    id: uuid.UUID
    status: str
    division_id: uuid.UUID
    division_name: str
    home_team: TeamRef
    away_team: TeamRef
    slot: SlotRef | None


class EventPatch(BaseModel):
    time_slot_id: uuid.UUID | None = None
    status: Literal["Cancelled"] | None = None

    @model_validator(mode="after")
    def exactly_one_change(self) -> "EventPatch":
        if (self.time_slot_id is None) == (self.status is None):
            raise ValueError("Send either time_slot_id (to move the game) or status='Cancelled', not both")
        return self


class PublishOut(BaseModel):
    published: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _season_in_org(db: Session, org_id: uuid.UUID, season_id: uuid.UUID) -> Season:
    season = db.get(Season, season_id)
    if season is None or season.organization_id != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Season not found")
    return season


def _event_in_org(db: Session, org_id: uuid.UUID, event_id: uuid.UUID) -> Event:
    event = db.get(Event, event_id)
    if event is None or event.organization_id != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return event


def _person_name(db: Session, model, person_id: uuid.UUID) -> str:
    row = db.execute(
        select(User.first_name, User.last_name).join(model, model.user_id == User.id).where(model.id == person_id)
    ).first()
    return f"{row.first_name} {row.last_name}" if row else str(person_id)


def _conflict_message(db: Session, exc: DBAPIError) -> str:
    """Translate a database rejection into a sentence. Call after rolling back."""
    diag = getattr(exc.orig, "diag", None)
    constraint = getattr(diag, "constraint_name", None) or ""
    ids = UUID_RE.findall(getattr(diag, "message_detail", None) or "")
    first_id = uuid.UUID(ids[0]) if ids else None

    if constraint == "team_bookings_no_overlap" and first_id:
        team = db.get(Team, first_id)
        return f"{team.name if team else first_id} already has a game at that time."
    if constraint == "coach_bookings_no_overlap" and first_id:
        return f"Coach {_person_name(db, Coach, first_id)} already has a game at that time."
    if constraint == "official_bookings_no_overlap" and first_id:
        return f"Official {_person_name(db, Official, first_id)} already has a game at that time."
    if constraint == "time_slots_event_id_key":
        return "That game is already in another time slot."
    if constraint == "time_slots_event_season_fkey":
        return "That time slot belongs to a different season than the game."
    return f"The database rejected the change ({constraint or getattr(diag, 'message_primary', 'unknown error')})."


def _event_out(event: Event, slot: TimeSlot | None, field: Field | None, division: Division,
               home: Team, away: Team) -> EventOut:
    return EventOut(
        id=event.id,
        status=event.status,
        division_id=division.id,
        division_name=division.name,
        home_team=TeamRef(id=home.id, name=home.name),
        away_team=TeamRef(id=away.id, name=away.name),
        slot=(
            SlotRef(
                id=slot.id,
                field_id=slot.field_id,
                field_name=field.name,
                start_time=slot.start_time,
                end_time=slot.end_time,
            )
            if slot is not None
            else None
        ),
    )


def _load_events(db: Session, *conditions) -> list[EventOut]:
    home = aliased(Team)
    away = aliased(Team)
    rows = db.execute(
        select(Event, TimeSlot, Field, Division, home, away)
        .join(Division, Division.id == Event.division_id)
        .join(home, home.id == Event.home_team_id)
        .join(away, away.id == Event.away_team_id)
        .outerjoin(TimeSlot, TimeSlot.event_id == Event.id)
        .outerjoin(Field, Field.id == TimeSlot.field_id)
        .where(*conditions)
        .order_by(TimeSlot.start_time.nulls_last(), Field.name, Division.name)
    ).all()
    return [_event_out(*row) for row in rows]


# ---------------------------------------------------------------------------
# Background runs
# ---------------------------------------------------------------------------

RunSubmitter = Callable[[uuid.UUID], object]

_run_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="schedule-run")


def _run_in_background(run_id: uuid.UUID) -> None:
    try:
        with SessionLocal() as db:
            execute_run(db, run_id)
    except Exception:
        # execute_run records solver failures on the run itself; this only
        # fires if the run row could not be updated at all.
        log.exception("Scheduling run %s could not be recorded", run_id)


def submit_in_background(run_id: uuid.UUID) -> Future:
    return _run_executor.submit(_run_in_background, run_id)


def get_run_submitter() -> RunSubmitter:
    """How runs are executed; tests override this to run them inline."""
    return submit_in_background


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/seasons/{season_id}/slots/generate", response_model=SlotGenerationOut)
def generate_slots(org_id: uuid.UUID, season_id: uuid.UUID, db: Session = Depends(get_db)):
    _season_in_org(db, org_id, season_id)
    try:
        result = generate_time_slots(db, season_id)
    except SlotGenerationError as exc:
        db.rollback()
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": "Time slots were not generated", "problems": exc.problems},
        )
    db.commit()
    return SlotGenerationOut(
        created=result.created,
        already_present=result.already_present,
        blacked_out=result.blacked_out,
        skipped=result.skipped,
    )


@router.post(
    "/seasons/{season_id}/schedule-runs",
    response_model=ScheduleRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_schedule_run(
    org_id: uuid.UUID,
    season_id: uuid.UUID,
    body: ScheduleRunRequest | None = None,
    db: Session = Depends(get_db),
    submit: RunSubmitter = Depends(get_run_submitter),
):
    """Start replacing the season's Draft games with a new schedule. Published games are kept."""
    season = _season_in_org(db, org_id, season_id)
    body = body or ScheduleRunRequest()
    try:
        run = create_run(db, season, body.time_limit_seconds)
    except RunAlreadyActive as exc:
        db.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "This season already has a scheduling run in progress", "run_id": str(exc.run_id)},
        )
    db.commit()
    submit(run.id)
    db.refresh(run)
    return ScheduleRunOut.model_validate(run)


@router.get("/schedule-runs/{run_id}", response_model=ScheduleRunOut)
def get_schedule_run(org_id: uuid.UUID, run_id: uuid.UUID, db: Session = Depends(get_db)):
    run = db.get(ScheduleRun, run_id)
    if run is None or run.organization_id != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scheduling run not found")
    return ScheduleRunOut.model_validate(run)


@router.get("/seasons/{season_id}/schedule-runs", response_model=list[ScheduleRunOut])
def list_schedule_runs(org_id: uuid.UUID, season_id: uuid.UUID, db: Session = Depends(get_db)):
    """The season's 20 most recent runs, newest first."""
    _season_in_org(db, org_id, season_id)
    runs = db.scalars(
        select(ScheduleRun)
        .where(ScheduleRun.season_id == season_id)
        .order_by(ScheduleRun.created_at.desc(), ScheduleRun.id)
        .limit(20)
    ).all()
    return [ScheduleRunOut.model_validate(run) for run in runs]


@router.get("/seasons/{season_id}/events", response_model=list[EventOut])
def list_events(org_id: uuid.UUID, season_id: uuid.UUID, db: Session = Depends(get_db)):
    _season_in_org(db, org_id, season_id)
    return _load_events(db, Event.season_id == season_id)


@router.patch("/events/{event_id}", response_model=EventOut)
def change_event(org_id: uuid.UUID, event_id: uuid.UUID, body: EventPatch, db: Session = Depends(get_db)):
    """Move a game to another open slot, or cancel it (which frees its slot)."""
    event = _event_in_org(db, org_id, event_id)
    current = db.scalar(select(TimeSlot).where(TimeSlot.event_id == event.id))

    try:
        if body.status == "Cancelled":
            event.status = "Cancelled"
            if current is not None:
                current.event_id = None
            db.flush()
        else:
            if event.status == "Cancelled":
                raise HTTPException(status.HTTP_409_CONFLICT, "A cancelled game cannot be moved")
            target = db.get(TimeSlot, body.time_slot_id)
            if target is None or target.organization_id != org_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Time slot not found")
            if target.event_id is not None and target.event_id != event.id:
                raise HTTPException(status.HTTP_409_CONFLICT, "That time slot already holds a game")
            if current is not None and current.id != target.id:
                current.event_id = None
                db.flush()
            target.event_id = event.id
            db.flush()
    except DBAPIError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, _conflict_message(db, exc)) from None

    db.commit()
    return _load_events(db, Event.id == event_id)[0]


@router.post("/seasons/{season_id}/publish", response_model=PublishOut)
def publish_schedule(org_id: uuid.UUID, season_id: uuid.UUID, db: Session = Depends(get_db)):
    """Publish every scheduled Draft game in the season."""
    _season_in_org(db, org_id, season_id)
    scheduled = select(TimeSlot.event_id).where(TimeSlot.event_id.is_not(None))
    result = db.execute(
        update(Event)
        .where(Event.season_id == season_id, Event.status == "Draft", Event.id.in_(scheduled))
        .values(status="Published")
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return PublishOut(published=result.rowcount)
