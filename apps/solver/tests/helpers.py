"""Builders for test data and an assertion for database rejections."""

import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from models import (
    Coach,
    Division,
    Event,
    EventOfficial,
    Field,
    Official,
    Organization,
    Season,
    Sport,
    Team,
    TeamCoach,
    TimeSlot,
    User,
    Venue,
)

NEW_YORK = ZoneInfo("America/New_York")


@contextmanager
def rejected(db: Session, sqlstate: str, constraint: str | None = None):
    """Assert the block (plus a final flush) is rejected by the database.

    sqlstate: 23P01 exclusion, 23503 foreign key, 23505 unique, 23514 check,
    P0001 raised by a trigger. The block runs in a savepoint, so the session
    stays usable afterwards.
    """
    with pytest.raises(DBAPIError) as info:
        with db.begin_nested():
            yield
            db.flush()
    error = info.value.orig
    assert error.sqlstate == sqlstate, f"expected SQLSTATE {sqlstate}, got {error.sqlstate}: {error}"
    if constraint is not None:
        assert error.diag.constraint_name == constraint, (
            f"expected constraint {constraint}, got {error.diag.constraint_name}: {error}"
        )


def local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NEW_YORK)


def _tag() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class League:
    org: Organization
    sport: Sport
    season: Season
    venue: Venue
    fields: list[Field]
    divisions: dict[str, Division] = field(default_factory=dict)
    teams: dict[str, Team] = field(default_factory=dict)
    coaches: dict[str, Coach] = field(default_factory=dict)
    officials: dict[str, Official] = field(default_factory=dict)


def make_league(
    db: Session,
    *,
    divisions: dict[str, list[str]] | None = None,
    games_per_team: int | None = 3,
    max_games_per_week: int | None = 2,
    n_fields: int = 2,
    start: date = date(2027, 4, 5),
    end: date = date(2027, 4, 25),
    timezone: str = "America/New_York",
    coach_for: dict[str, list[str]] | None = None,
    officials: int = 0,
    sport: Sport | None = None,
) -> League:
    """An organization with one season, a venue, fields, divisions and teams.

    divisions maps division name -> team names. coach_for maps coach name ->
    the team names they coach; by default every team gets its own coach.
    """
    tag = _tag()
    divisions = divisions or {"12U": ["A", "B", "C", "D"]}
    org = Organization(name=f"Test Org {tag}", status="active", timezone=timezone)
    sport = sport or Sport(name=f"Basketball {tag}")
    db.add_all([org, sport])
    db.flush()

    season = Season(organization_id=org.id, sport_id=sport.id, name="Season", start_date=start, end_date=end)
    venue = Venue(organization_id=org.id, name=f"Venue {tag}")
    db.add_all([season, venue])
    db.flush()

    fields = [
        Field(organization_id=org.id, venue_id=venue.id, sport_id=sport.id, name=f"Court {i + 1}")
        for i in range(n_fields)
    ]
    db.add_all(fields)
    db.flush()

    league = League(org=org, sport=sport, season=season, venue=venue, fields=fields)
    for div_name, team_names in divisions.items():
        division = Division(
            organization_id=org.id,
            season_id=season.id,
            name=div_name,
            games_per_team=games_per_team,
            max_games_per_week=max_games_per_week,
        )
        db.add(division)
        db.flush()
        league.divisions[div_name] = division
        for team_name in team_names:
            team = Team(organization_id=org.id, season_id=season.id, division_id=division.id, name=team_name)
            db.add(team)
            league.teams[team_name] = team
    db.flush()

    coach_for = coach_for if coach_for is not None else {f"Coach {t}": [t] for t in league.teams}
    for coach_name, team_names in coach_for.items():
        league.coaches[coach_name] = add_person(db, org, Coach, coach_name)
        for team_name in team_names:
            db.add(
                TeamCoach(
                    organization_id=org.id,
                    team_id=league.teams[team_name].id,
                    coach_id=league.coaches[coach_name].id,
                )
            )
    for i in range(officials):
        league.officials[f"Ref {i + 1}"] = add_person(db, org, Official, f"Ref {i + 1}")
    db.flush()
    return league


def add_person(db: Session, org: Organization, role, name: str):
    first, _, last = name.partition(" ")
    user = User(email=f"{_tag()}@test.example.com", first_name=first, last_name=last or "Test")
    db.add(user)
    db.flush()
    person = role(user_id=user.id, organization_id=org.id)
    db.add(person)
    db.flush()
    return person


def add_slot(
    db: Session,
    league: League,
    field_index: int,
    start: datetime,
    minutes: int = 120,
    division: Division | None = None,
) -> TimeSlot:
    slot = TimeSlot(
        organization_id=league.org.id,
        season_id=league.season.id,
        field_id=league.fields[field_index].id,
        division_id=division.id if division else None,
        start_time=start,
        end_time=start + timedelta(minutes=minutes),
    )
    db.add(slot)
    db.flush()
    return slot


def add_game(
    db: Session,
    league: League,
    home: str,
    away: str,
    slot: TimeSlot | None = None,
    status: str = "Draft",
) -> Event:
    home_team, away_team = league.teams[home], league.teams[away]
    event = Event(
        organization_id=league.org.id,
        season_id=league.season.id,
        division_id=home_team.division_id,
        home_team_id=home_team.id,
        away_team_id=away_team.id,
        status=status,
    )
    db.add(event)
    db.flush()
    if slot is not None:
        slot.event_id = event.id
        db.flush()
    return event


def assign_official(db: Session, league: League, event: Event, official: str) -> EventOfficial:
    link = EventOfficial(
        organization_id=league.org.id, event_id=event.id, official_id=league.officials[official].id
    )
    db.add(link)
    db.flush()
    return link
