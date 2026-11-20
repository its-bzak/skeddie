import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from itertools import pairwise

from sqlalchemy import select, text, update

from models import Event, Organization, Season, Team, TimeSlot
from services.eligibility import Eligibility
from services.matchups import Game
from services.persist import build_games, schedule_season
from services.precheck import precheck
from services.problem import DivisionInfo, FixedGame, Interval, Problem, SlotInfo, TeamInfo
from services.scheduler import diagnose_infeasible, solve
from services.slot_carver import generate_time_slots
from services.validate import validate_schedule
from tests.helpers import NEW_YORK

MONDAYS = [date(2027, 4, 5), date(2027, 4, 12), date(2027, 4, 19)]


# ---------------------------------------------------------------------------
# Building problems by hand
# ---------------------------------------------------------------------------


class ProblemBuilder:
    def __init__(self, divisions: dict[str, list[str]], games_per_team: int = 3, cap: int | None = 2,
                 coaches: dict[str, list[str]] | None = None):
        self.ids: dict[str, uuid.UUID] = {}
        self.divisions: dict[uuid.UUID, DivisionInfo] = {}
        team_division: dict[str, uuid.UUID] = {}
        for div_name, team_names in divisions.items():
            div_id = self.id(div_name)
            self.divisions[div_id] = DivisionInfo(
                div_id, div_name, games_per_team, cap, tuple(self.id(t) for t in team_names)
            )
            for team_name in team_names:
                team_division[team_name] = div_id
        coaches = coaches if coaches is not None else {f"Coach {t}": [t] for t in team_division}
        coach_ids: dict[str, set[uuid.UUID]] = defaultdict(set)
        for coach_name, team_names in coaches.items():
            for team_name in team_names:
                coach_ids[team_name].add(self.id(coach_name))
        self.teams = {
            self.id(t): TeamInfo(self.id(t), t, d, frozenset(coach_ids[t])) for t, d in team_division.items()
        }
        self.coach_names = {self.id(c): c for c in coaches}
        self.slots: list[SlotInfo] = []
        self.team_blocked: dict[uuid.UUID, list[Interval]] = defaultdict(list)
        self.coach_blocked: dict[uuid.UUID, list[Interval]] = defaultdict(list)
        self.fixed: list[FixedGame] = []

    def id(self, name: str) -> uuid.UUID:
        return self.ids.setdefault(name, uuid.uuid5(uuid.NAMESPACE_DNS, name))

    def slots_at(self, days, hours, fields=2, minutes=120, division: str | None = None):
        for day in days:
            for hour in hours:
                start = datetime.combine(day, time(hour), tzinfo=NEW_YORK).astimezone(timezone.utc)
                for f in range(fields):
                    self.slots.append(
                        SlotInfo(uuid.uuid4(), self.id(f"Court {f + 1}"), f"Court {f + 1}", start,
                                 start + timedelta(minutes=minutes), self.id(division) if division else None)
                    )
        return self

    def block_team(self, team: str, start: datetime, end: datetime):
        self.team_blocked[self.id(team)].append(Interval(start, end, "test"))
        return self

    def block_coach(self, coach: str, start: datetime, end: datetime):
        self.coach_blocked[self.id(coach)].append(Interval(start, end, "test"))
        return self

    def build(self) -> Problem:
        return Problem(
            season_id=uuid.uuid4(),
            season_name="Test",
            timezone="America/New_York",
            divisions=self.divisions,
            teams=self.teams,
            slots=self.slots,
            team_blocked=dict(self.team_blocked),
            coach_blocked=dict(self.coach_blocked),
            coach_names=self.coach_names,
            fixed_games=self.fixed,
        )


def ny(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=NEW_YORK)


def solved(problem: Problem, **kwargs):
    games, issues = build_games(problem)
    assert issues == []
    eligibility = Eligibility.build(problem)
    assert precheck(problem, games, eligibility) == []
    result = solve(problem, games, eligibility, time_limit_seconds=20, **kwargs)
    assert result.found, result.status
    assert validate_schedule(problem, games, result.assignment) == []
    slots = {s.id: s for s in problem.slots}
    return games, {g.index: slots[result.assignment[g.index]] for g in games}, result


# ---------------------------------------------------------------------------
# Solver on hand-built problems
# ---------------------------------------------------------------------------


def test_round_robin_respects_weekly_cap():
    problem = ProblemBuilder({"U10": ["A", "B", "C", "D"]}, games_per_team=3, cap=1).slots_at(
        MONDAYS, [16, 18]
    ).build()
    games, placed, _ = solved(problem)
    weeks = Counter()
    for g in games:
        for team in (g.home_team_id, g.away_team_id):
            weeks[(team, placed[g.index].start.isocalendar()[1])] += 1
    assert max(weeks.values()) == 1


def test_shared_coach_games_never_overlap():
    builder = ProblemBuilder(
        {"12U": ["X1", "X2"], "16U": ["Y1", "Y2"]},
        games_per_team=1,
        coaches={"Shared": ["X1", "Y1"], "Coach X2": ["X2"], "Coach Y2": ["Y2"]},
    )
    problem = builder.slots_at([MONDAYS[0]], [16, 18], fields=2).build()
    games, placed, _ = solved(problem)
    starts = {problem.divisions[g.division_id].name: placed[g.index].start for g in games}
    assert starts["12U"] != starts["16U"]


def test_unavailability_of_team_and_coach_is_respected():
    builder = ProblemBuilder({"U10": ["A", "B", "C", "D"]}, games_per_team=3, cap=None)
    builder.slots_at(MONDAYS, [16, 18])
    builder.block_team("A", ny(MONDAYS[0], 0), ny(MONDAYS[0], 23))
    builder.block_coach("Coach B", ny(MONDAYS[1], 15), ny(MONDAYS[1], 17))
    problem = builder.build()
    games, placed, _ = solved(problem)
    for g in games:
        slot = placed[g.index]
        if builder.id("A") in (g.home_team_id, g.away_team_id):
            assert slot.start.astimezone(NEW_YORK).date() != MONDAYS[0]
        if builder.id("B") in (g.home_team_id, g.away_team_id):
            assert slot.start != ny(MONDAYS[1], 16).astimezone(timezone.utc)


def test_division_restricted_slots_are_only_used_by_that_division():
    builder = ProblemBuilder({"12U": ["A", "B"], "16U": ["C", "D"]}, games_per_team=1)
    builder.slots_at([MONDAYS[0]], [16], fields=1, division="16U")
    builder.slots_at([MONDAYS[0]], [18], fields=1)
    problem = builder.build()
    games, placed, _ = solved(problem)
    for g in games:
        if placed[g.index].division_id is not None:
            assert g.division_id == builder.id("16U")


def test_published_games_are_worked_around_and_count_toward_caps():
    builder = ProblemBuilder({"U10": ["A", "B", "C", "D"]}, games_per_team=3, cap=1)
    builder.slots_at(MONDAYS, [16, 18])
    start = ny(MONDAYS[0], 16).astimezone(timezone.utc)
    builder.fixed.append(
        FixedGame(uuid.uuid4(), builder.id("U10"), builder.id("A"), builder.id("B"), start, start + timedelta(hours=2))
    )
    builder.block_team("A", start, start + timedelta(hours=2))
    builder.block_team("B", start, start + timedelta(hours=2))
    problem = builder.build()
    games, placed, _ = solved(problem)
    assert len(games) == 5  # 6 games in the round robin, 1 already published
    for g in games:
        if {builder.id("A"), builder.id("B")} & {g.home_team_id, g.away_team_id}:
            assert placed[g.index].start.astimezone(NEW_YORK).date() != MONDAYS[0]  # cap 1 already used


def test_deterministic_mode_gives_identical_schedules():
    problem = ProblemBuilder({"U10": ["A", "B", "C", "D", "E", "F"]}, games_per_team=5, cap=2).slots_at(
        MONDAYS, [16, 18], fields=3
    ).build()
    _, first, _ = solved(problem, deterministic=True)
    _, second, _ = solved(problem, deterministic=True)
    assert {k: v.id for k, v in first.items()} == {k: v.id for k, v in second.items()}


# ---------------------------------------------------------------------------
# Explaining failures
# ---------------------------------------------------------------------------


def test_precheck_explains_capacity_problems():
    too_few = ProblemBuilder({"U10": ["A", "B", "C", "D"]}, games_per_team=3).slots_at([MONDAYS[0]], [16]).build()
    games, _ = build_games(too_few)
    issues = precheck(too_few, games, Eligibility.build(too_few))
    assert any("needs 6 games but has only 2 open time slots" in i for i in issues)

    capped = ProblemBuilder({"U10": ["A", "B", "C", "D"]}, games_per_team=3, cap=1).slots_at(
        [MONDAYS[0]], [12, 14, 16, 18], fields=2
    ).build()
    games, _ = build_games(capped)
    issues = precheck(capped, games, Eligibility.build(capped))
    assert any("allows 1 per week" in i for i in issues)

    odd = ProblemBuilder({"U10": ["A", "B", "C"]}, games_per_team=3).slots_at(MONDAYS, [16]).build()
    _, issues = build_games(odd)
    assert any("is odd" in i for i in issues)


def test_infeasible_combination_names_the_shared_coach():
    # Each division alone fits, but the shared coach cannot be at both games
    # and there is only one time with open courts.
    builder = ProblemBuilder(
        {"12U": ["X1", "X2"], "16U": ["Y1", "Y2"]},
        games_per_team=1,
        coaches={"Pat Shared": ["X1", "Y1"], "Coach X2": ["X2"], "Coach Y2": ["Y2"]},
    )
    problem = builder.slots_at([MONDAYS[0]], [16], fields=2).build()
    games, _ = build_games(problem)
    eligibility = Eligibility.build(problem)
    assert precheck(problem, games, eligibility) == []
    assert solve(problem, games, eligibility, time_limit_seconds=10).status == "infeasible"
    issues = diagnose_infeasible(problem, games, eligibility)
    assert len(issues) == 1 and "Pat Shared (12U, 16U)" in issues[0]


def test_validator_catches_every_kind_of_violation():
    builder = ProblemBuilder({"U10": ["A", "B", "C", "D"]}, games_per_team=1, cap=1)
    builder.slots_at([MONDAYS[0]], [16], fields=2)
    builder.block_team("D", ny(MONDAYS[0], 16), ny(MONDAYS[0], 17))
    problem = builder.build()
    a, b, c, d = (builder.id(t) for t in "ABCD")
    div = builder.id("U10")
    same_time = problem.slots[:2]

    games = [Game(0, div, a, b), Game(1, div, a, c), Game(2, div, d, c)]
    violations = validate_schedule(problem, games, {0: same_time[0].id, 1: same_time[1].id, 2: same_time[1].id})
    text_out = "\n".join(violations)
    assert "holds 2 games" in text_out
    assert "Team A is in games 0 and 1 at the same time" in text_out
    assert "blocked time for D" in text_out
    assert "plays 2 games in week" in text_out
    assert "Coach Coach C is in games" in text_out
    assert "A plays 2 games (expected 1)" in text_out


# ---------------------------------------------------------------------------
# The seed league, end to end through the database
# ---------------------------------------------------------------------------


def dans_season(db) -> Season:
    return db.scalar(select(Season).join(Organization).where(Organization.name == "Dan's Organization"))


def scheduled_games(db, season_id):
    return db.execute(
        select(Event, TimeSlot.start_time, TimeSlot.end_time)
        .join(TimeSlot, TimeSlot.event_id == Event.id)
        .where(Event.season_id == season_id)
    ).all()


def test_seed_league_schedules_with_every_rule_respected(db):
    season = dans_season(db)
    generate_time_slots(db, season.id)
    outcome = schedule_season(db, season.id)
    assert outcome.ok, outcome.issues
    assert outcome.games_scheduled == 112
    assert outcome.objective == 0  # no same-day, back-to-back or crowded weeks

    teams = {t.name: t.id for t in db.scalars(select(Team).where(Team.season_id == season.id))}
    rows = scheduled_games(db, season.id)
    assert len(rows) == 112

    games_per_team, home, away, per_week = Counter(), Counter(), Counter(), Counter()
    busy = defaultdict(list)
    for event, start, end in rows:
        home[event.home_team_id] += 1
        away[event.away_team_id] += 1
        for team_id in (event.home_team_id, event.away_team_id):
            games_per_team[team_id] += 1
            per_week[(team_id, start.astimezone(NEW_YORK).isocalendar()[:2])] += 1
            busy[team_id].append((start, end))

    assert set(games_per_team.values()) == {7}
    assert all(abs(home[t] - away[t]) <= 1 for t in teams.values())
    assert max(per_week.values()) <= 2

    # 1A and 3C share a coach: never at the same time, never Wednesday 16:00-18:00
    shared = sorted(busy[teams["Team 1A"]] + busy[teams["Team 3C"]])
    assert all(earlier[1] <= later[0] for earlier, later in pairwise(shared))
    for start, _ in shared:
        local_start = start.astimezone(NEW_YORK)
        assert not (local_start.weekday() == 2 and local_start.hour < 18)

    # 2B is blacked out on 2027-04-10
    assert all(start.astimezone(NEW_YORK).date() != date(2027, 4, 10) for start, _ in busy[teams["Team 2B"]])

    # Proof straight from the booking tables: no overlapping pair exists
    for table, column in (("team_bookings", "team_id"), ("coach_bookings", "coach_id")):
        overlaps = db.scalar(text(
            f"SELECT count(*) FROM {table} a JOIN {table} b "
            f"ON a.{column} = b.{column} AND a.event_id < b.event_id AND a.during && b.during"
        ))
        assert overlaps == 0, table

    easton = db.scalar(select(Organization).where(Organization.name == "Easton Youth Athletics"))
    assert db.scalar(select(Event.id).where(Event.organization_id == easton.id).limit(1)) is None


def test_rescheduling_keeps_published_games_and_replaces_drafts(db):
    season = dans_season(db)
    generate_time_slots(db, season.id)
    assert schedule_season(db, season.id).ok

    published = {event.id: start for event, start, _ in scheduled_games(db, season.id)[:20]}
    db.execute(update(Event).where(Event.id.in_(published)).values(status="Published"))
    db.flush()

    outcome = schedule_season(db, season.id)
    assert outcome.ok, outcome.issues
    assert outcome.games_scheduled == 92

    rows = scheduled_games(db, season.id)
    assert len(rows) == 112
    kept = {event.id: start for event, start, _ in rows if event.status == "Published"}
    assert kept == published
    totals = Counter(t for event, _, _ in rows for t in (event.home_team_id, event.away_team_id))
    assert set(totals.values()) == {7}


def test_season_without_slots_fails_precheck(db):
    easton = db.scalar(select(Season).join(Organization).where(Organization.name == "Easton Youth Athletics"))
    outcome = schedule_season(db, easton.id)
    assert outcome.status == "precheck_failed"
    assert any("open time slots" in issue for issue in outcome.issues)
