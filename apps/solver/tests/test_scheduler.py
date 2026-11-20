import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from itertools import pairwise

from sqlalchemy import select, text, update

from models import Event, Organization, Season, SlotRule, Team, TimeSlot, Unavailability
from services.eligibility import Eligibility
from services.matchups import Game
from services.persist import build_games, schedule_season
from services.precheck import precheck
from services.problem import DivisionInfo, FixedGame, Interval, Problem, SlotInfo, TeamInfo
from services.quality import ScheduleQuality, pacing_window, schedule_quality
from services.scheduler import diagnose_infeasible, round_windows, solve
from services.slot_carver import generate_time_slots
from services.validate import validate_schedule
from tests.helpers import NEW_YORK, make_league

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
        self.season_start: date | None = None
        self.playable_end: date | None = None
        self.makeup_weeks = 0

    def season(self, start: date, playable_end: date, makeup_weeks: int = 0):
        self.season_start, self.playable_end, self.makeup_weeks = start, playable_end, makeup_weeks
        return self

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
            season_start=self.season_start,
            playable_end=self.playable_end,
            makeup_weeks=self.makeup_weeks,
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


def test_games_are_paced_evenly_instead_of_front_loaded():
    # Six Mondays (2027-04-05 .. 05-10) with far more slots than needed; each
    # team plays 3 games over a 6-week window.
    mondays = [date(2027, 4, 5) + timedelta(weeks=w) for w in range(6)]
    builder = ProblemBuilder({"U10": ["A", "B", "C", "D"]}, games_per_team=3, cap=None)
    problem = builder.slots_at(mondays, [16, 18], fields=2).season(date(2027, 4, 5), date(2027, 5, 16)).build()
    games, placed, result = solved(problem)
    assert result.objective == 0

    # By each Sunday, every team has played floor..ceil of 3 x days_elapsed / 42
    for sunday in (date(2027, 4, 11), date(2027, 4, 18), date(2027, 4, 25), date(2027, 5, 2), date(2027, 5, 9)):
        elapsed = (sunday - date(2027, 4, 5)).days + 1
        low, high = 3 * elapsed // 42, -(-3 * elapsed // 42)
        for team in "ABCD":
            team_id = builder.id(team)
            played = sum(
                1 for g in games
                if team_id in (g.home_team_id, g.away_team_id)
                and placed[g.index].start.astimezone(NEW_YORK).date() <= sunday
            )
            assert low <= played <= high, (team, sunday, played)


def test_games_land_inside_their_round_window():
    mondays = [date(2027, 4, 5) + timedelta(weeks=w) for w in range(10)]
    builder = ProblemBuilder({"U12": ["A", "B", "C", "D", "E", "F"]}, games_per_team=5, cap=1)
    problem = builder.slots_at(mondays, [16, 18], fields=2).season(date(2027, 4, 5), date(2027, 6, 13)).build()
    games, placed, result = solved(problem)
    assert result.used_round_windows
    windows = round_windows(games, pacing_window(problem))
    for g in games:
        earliest, latest = windows[g.index]
        assert earliest <= placed[g.index].start.astimezone(NEW_YORK).date() <= latest


def test_round_windows_fall_back_when_a_team_is_out_for_its_whole_window():
    mondays = [date(2027, 4, 5) + timedelta(weeks=w) for w in range(9)]
    builder = ProblemBuilder({"U10": ["A", "B", "C", "D"]}, games_per_team=3, cap=None)
    builder.slots_at(mondays, [16, 18], fields=2).season(date(2027, 4, 5), date(2027, 6, 6))
    games, _ = build_games(builder.build())
    first_game_of_a = next(g for g in games if g.round == 0 and builder.id("A") in (g.home_team_id, g.away_team_id))
    earliest, latest = round_windows(games, pacing_window(builder.build()))[first_game_of_a.index]
    builder.block_team("A", ny(earliest, 0), ny(latest + timedelta(days=1), 0))

    _, _, result = solved(builder.build())
    assert not result.used_round_windows


def test_schedule_quality_counts_each_soft_goal():
    builder = ProblemBuilder({"U10": ["A", "B", "C", "D"]}, games_per_team=1, cap=None)
    problem = (
        builder.slots_at([date(2027, 4, 5), date(2027, 4, 6)], [16, 18], fields=1)
        .season(date(2027, 4, 5), date(2027, 5, 16))
        .build()
    )
    a, b, c, d = (builder.id(t) for t in "ABCD")
    division = builder.id("U10")
    monday_16, monday_18, tuesday_16, _ = problem.slots
    games = [Game(0, division, a, b), Game(1, division, a, c), Game(2, division, a, d)]
    assignment = {0: monday_16.id, 1: monday_18.id, 2: tuesday_16.id}

    # A plays all 3 games in the first two days of a six-week window: twice on
    # Monday, then Tuesday. Off pace by 2, 2, 1, 1 and 0 at the five Sundays.
    assert schedule_quality(problem, games, assignment, proven_optimal=False) == ScheduleQuality(
        off_pace_games=6, same_day_doubles=1, back_to_backs=1, proven_optimal=False
    )


def test_precheck_mentions_the_makeup_window_when_short_of_slots():
    problem = (
        ProblemBuilder({"U10": ["A", "B", "C", "D"]}, games_per_team=3)
        .slots_at([MONDAYS[0]], [16])
        .season(MONDAYS[0], MONDAYS[1], makeup_weeks=1)
        .build()
    )
    games, _ = build_games(problem)
    issues = precheck(problem, games, Eligibility.build(problem))
    assert any("open time slots" in i for i in issues)
    assert issues[-1].startswith("Slots in the season's last week are kept free as a makeup window")


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
    assert outcome.objective == 0  # no same-day, back-to-back or crowded weeks, and evenly paced
    assert outcome.quality == ScheduleQuality(0, 0, 0, proven_optimal=True)

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

    # Nothing on Memorial Day or in the June 24-30 makeup week, and the season
    # is not front-loaded: every team still plays in June.
    game_dates = {start.astimezone(NEW_YORK).date() for _, start, _ in rows}
    assert date(2027, 5, 31) not in game_dates
    assert max(game_dates) <= date(2027, 6, 23)
    for team_games in busy.values():
        assert max(start for start, _ in team_games).astimezone(NEW_YORK).month == 6

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


def test_blackouts_added_after_slot_generation_and_the_makeup_window_are_respected(db):
    league = make_league(db)  # 2027-04-05 to 2027-04-25, 4 teams x 3 games
    for day_of_week in (0, 2):
        db.add(SlotRule(organization_id=league.org.id, season_id=league.season.id, field_id=league.fields[0].id,
                        day_of_week=day_of_week, start_time=time(16), end_time=time(20), slot_duration_mins=120))
    db.flush()
    generate_time_slots(db, league.season.id)

    # Added after the slots exist: the slots stay, but the solver must not use them
    db.add(Unavailability(organization_id=league.org.id, target_type="organization", specific_date=date(2027, 4, 12)))
    league.season.makeup_weeks = 1  # keeps 2027-04-19 .. 04-25 free
    db.flush()

    outcome = schedule_season(db, league.season.id)
    assert outcome.ok, outcome.issues
    game_dates = {start.astimezone(NEW_YORK).date() for _, start, _ in scheduled_games(db, league.season.id)}
    assert game_dates == {date(2027, 4, 5), date(2027, 4, 7), date(2027, 4, 14)}


def test_season_without_slots_fails_precheck(db):
    easton = db.scalar(select(Season).join(Organization).where(Organization.name == "Easton Youth Athletics"))
    outcome = schedule_season(db, easton.id)
    assert outcome.status == "precheck_failed"
    assert any("open time slots" in issue for issue in outcome.issues)
