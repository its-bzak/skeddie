"""The scheduling problem as plain data, independent of the database.

`load_problem` reads one season from Postgres; everything downstream (matchups,
precheck, solver, validator) works on these dataclasses only, so it can be
unit-tested without a database.

All instants are timezone-aware UTC datetimes. Intervals are half-open
[start, end), matching tstzrange '[)' in the database, so back-to-back games
(16:00-18:00 then 18:00-20:00) do not overlap.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from models import (
    Coach,
    CoachBooking,
    Division,
    Event,
    Field,
    Organization,
    Season,
    Team,
    TeamBooking,
    TeamCoach,
    TimeSlot,
    Unavailability,
    User,
)
from services.timeutil import dates_on_weekday, iso_week, local_date, local_span_to_utc


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    reason: str = ""

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end


@dataclass(frozen=True)
class SlotInfo:
    id: uuid.UUID
    field_id: uuid.UUID
    field_name: str
    start: datetime
    end: datetime
    division_id: uuid.UUID | None  # None = open to every division


@dataclass(frozen=True)
class TeamInfo:
    id: uuid.UUID
    name: str
    division_id: uuid.UUID
    coach_ids: frozenset[uuid.UUID]


@dataclass(frozen=True)
class DivisionInfo:
    id: uuid.UUID
    name: str
    games_per_team: int | None
    max_games_per_week: int | None  # None = no weekly cap
    team_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class FixedGame:
    """A Published game already on the calendar; the solver works around it."""

    event_id: uuid.UUID
    division_id: uuid.UUID
    home_team_id: uuid.UUID
    away_team_id: uuid.UUID
    start: datetime
    end: datetime


@dataclass
class Problem:
    season_id: uuid.UUID
    season_name: str
    timezone: str
    divisions: dict[uuid.UUID, DivisionInfo]
    teams: dict[uuid.UUID, TeamInfo]
    slots: list[SlotInfo]  # open slots on fields of the season's sport
    # Times a team / coach cannot play: unavailability plus games already booked
    # outside this run (Published games, other seasons).
    team_blocked: dict[uuid.UUID, list[Interval]] = field(default_factory=dict)
    coach_blocked: dict[uuid.UUID, list[Interval]] = field(default_factory=dict)
    coach_names: dict[uuid.UUID, str] = field(default_factory=dict)
    fixed_games: list[FixedGame] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def week_of(self, instant: datetime) -> tuple[int, int]:
        return iso_week(instant, self.tz)

    def date_of(self, instant: datetime) -> date:
        return local_date(instant, self.tz)


def load_problem(db: Session, season_id: uuid.UUID) -> Problem:
    """Read everything the solver needs for one season.

    Call this after the season's Draft events have been removed, so that every
    remaining booking is something the new schedule must work around.
    """
    season = db.get(Season, season_id)
    if season is None:
        raise LookupError(f"Season {season_id} does not exist")
    org = db.get(Organization, season.organization_id)
    tz = ZoneInfo(org.timezone)

    divisions_rows = db.scalars(
        select(Division).where(Division.season_id == season_id).order_by(Division.name)
    ).all()
    team_rows = db.scalars(select(Team).where(Team.season_id == season_id).order_by(Team.name)).all()
    coach_links = db.execute(
        select(TeamCoach.team_id, TeamCoach.coach_id).join(Team, Team.id == TeamCoach.team_id).where(
            Team.season_id == season_id
        )
    ).all()

    coaches_by_team: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for team_id, coach_id in coach_links:
        coaches_by_team[team_id].add(coach_id)

    teams = {
        t.id: TeamInfo(t.id, t.name, t.division_id, frozenset(coaches_by_team.get(t.id, ())))
        for t in team_rows
    }
    team_ids_by_division: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for t in team_rows:
        team_ids_by_division[t.division_id].append(t.id)
    divisions = {
        d.id: DivisionInfo(
            d.id, d.name, d.games_per_team, d.max_games_per_week, tuple(team_ids_by_division.get(d.id, ()))
        )
        for d in divisions_rows
    }
    coach_ids = {c for cs in coaches_by_team.values() for c in cs}

    coach_names: dict[uuid.UUID, str] = {}
    if coach_ids:
        for coach_id, first, last in db.execute(
            select(Coach.id, User.first_name, User.last_name)
            .join(User, User.id == Coach.user_id)
            .where(Coach.id.in_(coach_ids))
        ):
            coach_names[coach_id] = f"{first} {last}"

    slot_rows = db.execute(
        select(TimeSlot, Field.name)
        .join(Field, Field.id == TimeSlot.field_id)
        .where(
            TimeSlot.season_id == season_id,
            TimeSlot.event_id.is_(None),
            Field.sport_id == season.sport_id,
        )
        .order_by(TimeSlot.start_time, Field.name)
    ).all()
    slots = [
        SlotInfo(s.id, s.field_id, field_name, s.start_time, s.end_time, s.division_id)
        for s, field_name in slot_rows
    ]

    team_blocked: dict[uuid.UUID, list[Interval]] = defaultdict(list)
    coach_blocked: dict[uuid.UUID, list[Interval]] = defaultdict(list)

    # Unavailability (local times in the organization timezone)
    targets = []
    if teams:
        targets.append((Unavailability.target_type == "team") & Unavailability.target_id.in_(list(teams)))
    if coach_ids:
        targets.append((Unavailability.target_type == "coach") & Unavailability.target_id.in_(list(coach_ids)))
    unavailability = []
    if targets:
        unavailability = db.scalars(
            select(Unavailability).where(
                Unavailability.organization_id == season.organization_id, or_(*targets)
            )
        ).all()
    for u in unavailability:
        bucket = team_blocked if u.target_type == "team" else coach_blocked
        reason = u.reason or "unavailable"
        days = (
            [u.specific_date]
            if u.specific_date is not None
            else list(dates_on_weekday(season.start_date, season.end_date, u.day_of_week))
        )
        for day in days:
            start, end = local_span_to_utc(day, u.start_time, u.end_time, tz)
            bucket[u.target_id].append(Interval(start, end, reason))

    # Games already booked that this run must not move
    if teams:
        team_bookings = db.execute(
            select(TeamBooking.team_id, TeamBooking.during).where(TeamBooking.team_id.in_(list(teams)))
        )
        for team_id, during in team_bookings:
            team_blocked[team_id].append(Interval(during.lower, during.upper, "already has a game"))
    if coach_ids:
        coach_bookings = db.execute(
            select(CoachBooking.coach_id, CoachBooking.during).where(CoachBooking.coach_id.in_(list(coach_ids)))
        )
        for coach_id, during in coach_bookings:
            coach_blocked[coach_id].append(Interval(during.lower, during.upper, "already has a game"))

    fixed_rows = db.execute(
        select(Event, TimeSlot.start_time, TimeSlot.end_time)
        .join(TimeSlot, TimeSlot.event_id == Event.id)
        .where(Event.season_id == season_id, Event.status == "Published")
    ).all()
    fixed_games = [
        FixedGame(e.id, e.division_id, e.home_team_id, e.away_team_id, start, end)
        for e, start, end in fixed_rows
    ]

    return Problem(
        season_id=season_id,
        season_name=season.name,
        timezone=org.timezone,
        divisions=divisions,
        teams=teams,
        slots=slots,
        team_blocked=dict(team_blocked),
        coach_blocked=dict(coach_blocked),
        coach_names=coach_names,
        fixed_games=fixed_games,
    )
