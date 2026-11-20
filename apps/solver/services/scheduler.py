"""The scheduling engine: assign every game to one open time slot (OR-Tools CP-SAT).

Slots with the same start, end and division restriction are interchangeable
for every rule below (only the field differs), so the model works on "time
blocks" whose capacity is the number of such slots; concrete fields are handed
out afterwards. This removes the symmetry between identical courts, which
otherwise multiplies the search space by the number of courts.

Round windows: every game has a round (see matchups.py). The engine first
lets each game use only slots inside its round's share of the playable window,
widened by WINDOW_SLACK_DAYS on each side. That shrinks the search space
several-fold; measured, 100 teams went from ~70s to 20-40s and 192 teams from
poorly paced to near-perfect within 150s. Windows are a search shortcut, not
a rule: if the restricted model is infeasible or finds nothing in time, the
engine re-solves without them for the rest of the time limit.

Decision variables exist only where a game could really use a block: it is
allowed for the game's division and does not touch any unavailability or
existing game of either team or any of their coaches.

Hard constraints
  * every game gets exactly one slot; a block holds at most as many games as
    it has slots
  * no team and no coach is in two overlapping games. For each distinct slot
    start time, a team/coach may be in at most one of its games among the
    slots covering that moment. Two half-open intervals overlap exactly when
    one contains the other's start, so this is exact for any slot lengths.
  * per team, per Monday-Sunday week (organization timezone), at most
    max_games_per_week games, counting Published games already on the calendar

Soft goals (minimized): two games for a team on the same day, games on
back-to-back days, more games in one week than an even spread needs, and
falling off an even pace across the season (the band is defined in
quality.py). Only games outside the pace band cost anything, so a well-paced
schedule scores 0 and the solver can prove it optimal quickly. Each team's
games per day and per week are counted once and shared by the weekly cap and
every soft goal, which keeps the model small.
"""

import math
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from ortools.sat.python import cp_model

from services.eligibility import Eligibility
from services.matchups import Game
from services.problem import Problem, SlotInfo
from services.quality import pace_band, pace_checkpoints, pacing_window

SAME_DAY_WEIGHT = 10
BACK_TO_BACK_WEIGHT = 3
WEEK_CROWDING_WEIGHT = 5
PACE_WEIGHT = 2
WINDOW_SLACK_DAYS = 7

_STATUS_NAMES = {
    cp_model.OPTIMAL: "optimal",
    cp_model.FEASIBLE: "feasible",
    cp_model.INFEASIBLE: "infeasible",
    cp_model.UNKNOWN: "timeout",
    cp_model.MODEL_INVALID: "model_invalid",
}


@dataclass
class SolveResult:
    status: str  # optimal | feasible | infeasible | timeout | model_invalid
    assignment: dict[int, uuid.UUID] = field(default_factory=dict)  # game index -> slot id
    objective: float | None = None
    wall_seconds: float = 0.0
    # False when the engine fell back to the unrestricted model (or never used windows).
    used_round_windows: bool = False

    @property
    def found(self) -> bool:
        return self.status in ("optimal", "feasible")


def round_windows(games: list[Game], window: tuple[date, date]) -> dict[int, tuple[date, date]]:
    """The dates each game may use: its round's share of the window, plus slack on both sides."""
    first, last = window
    days = (last - first).days + 1
    slack = timedelta(days=WINDOW_SLACK_DAYS)
    windows = {}
    for game in games:
        start = first + timedelta(days=game.round * days // game.rounds) - slack
        end = first + timedelta(days=(game.round + 1) * days // game.rounds - 1) + slack
        windows[game.index] = (max(start, first), min(end, last))
    return windows


def _count(model: cp_model.CpModel, terms: list, upper: int) -> cp_model.LinearExprT:
    """One variable equal to sum(terms), or the term itself when there is only one."""
    if len(terms) == 1:
        return terms[0]
    count = model.new_int_var(0, upper, "")
    model.add(count == sum(terms))
    return count


def _add_pacing(
    model: cp_model.CpModel,
    window: tuple[date, date],
    day_count: dict[date, cp_model.LinearExprT],
    fixed_dates: dict[date, int],
    total_games: int,
) -> list[cp_model.LinearExprT]:
    """Penalty terms for one team falling outside the even-pace band at each checkpoint."""
    first, last = window
    penalties: list[cp_model.LinearExprT] = []
    played_so_far: cp_model.LinearExprT = 0
    period_start = first
    for checkpoint in pace_checkpoints(first, last):
        this_week = [count for day, count in day_count.items() if period_start <= day <= checkpoint]
        played = model.new_int_var(0, total_games, "")
        model.add(played == played_so_far + sum(this_week))
        played_so_far, period_start = played, checkpoint + timedelta(days=1)

        on_calendar = played + sum(n for day, n in fixed_dates.items() if day <= checkpoint)
        low, high = pace_band(total_games, first, checkpoint, last)
        off_pace = model.new_int_var(0, total_games, "")
        model.add(off_pace >= low - on_calendar)
        model.add(off_pace >= on_calendar - high)
        penalties.append(PACE_WEIGHT * off_pace)
    return penalties


def _add_no_overlap(model: cp_model.CpModel, entries: list[tuple[SlotInfo, cp_model.IntVar]]) -> None:
    """At most one of `entries` may be chosen at any instant (sweep over start times)."""
    if len(entries) < 2:
        return
    entries = sorted(entries, key=lambda e: e[0].start)
    active: list[tuple[SlotInfo, cp_model.IntVar]] = []
    i = 0
    for point in sorted({slot.start for slot, _ in entries}):
        while i < len(entries) and entries[i][0].start <= point:
            active.append(entries[i])
            i += 1
        active = [e for e in active if e[0].end > point]
        if len(active) > 1:
            model.add_at_most_one(var for _, var in active)


def solve(
    problem: Problem,
    games: list[Game],
    eligibility: Eligibility | None = None,
    *,
    time_limit_seconds: float = 30.0,
    workers: int = 8,
    seed: int = 0,
    deterministic: bool = False,
    use_round_windows: bool = True,
) -> SolveResult:
    """Solve within the time limit, trying round windows first.

    `deterministic=True` guarantees identical output for identical input (about 2x slower).
    """
    started = time.monotonic()
    if not games:
        return SolveResult("optimal", {}, 0.0, 0.0)

    eligibility = eligibility or Eligibility.build(problem)
    options = {"workers": workers, "seed": seed, "deterministic": deterministic}
    window = pacing_window(problem)

    remaining = time_limit_seconds
    if use_round_windows and window is not None:
        windows = round_windows(games, window)
        result = _solve_model(problem, games, eligibility, windows, remaining, **options)
        if result.found:
            result.used_round_windows = True
            result.wall_seconds = time.monotonic() - started
            return result
        remaining = time_limit_seconds - (time.monotonic() - started)
        if remaining <= 0:
            return SolveResult("timeout", wall_seconds=time.monotonic() - started)

    result = _solve_model(problem, games, eligibility, None, remaining, **options)
    result.wall_seconds = time.monotonic() - started
    return result


def _solve_model(
    problem: Problem,
    games: list[Game],
    eligibility: Eligibility,
    windows: dict[int, tuple[date, date]] | None,
    time_limit_seconds: float,
    *,
    workers: int,
    seed: int,
    deterministic: bool,
) -> SolveResult:
    model = cp_model.CpModel()

    # Group interchangeable slots into time blocks; a block's representative
    # slot carries its start/end. Fields are handed out in name order.
    blocks: dict[tuple, list[SlotInfo]] = defaultdict(list)
    for slot in problem.slots:
        blocks[(slot.start, slot.end, slot.division_id)].append(slot)
    for members in blocks.values():
        members.sort(key=lambda s: (s.field_name, str(s.id)))
    block_key = {s.id: key for key, members in blocks.items() for s in members}
    representative = {members[0].id: key for key, members in blocks.items()}
    slot_date = {s.id: problem.date_of(s.start) for s in problem.slots}

    x_by_game: dict[int, list[tuple[SlotInfo, cp_model.IntVar]]] = {}
    by_block: dict[tuple, list[cp_model.IntVar]] = defaultdict(list)
    for game in games:
        keys = list(dict.fromkeys(block_key[s.id] for s in eligibility.game_slots(problem, game)))
        if windows is not None:
            earliest, latest = windows[game.index]
            keys = [key for key in keys if earliest <= slot_date[blocks[key][0].id] <= latest]
        if not keys:
            return SolveResult("infeasible")
        entries = [(blocks[key][0], model.new_bool_var("")) for key in keys]
        x_by_game[game.index] = entries
        model.add_exactly_one(var for _, var in entries)
        for slot, var in entries:
            by_block[block_key[slot.id]].append(var)

    for key, lits in by_block.items():
        capacity = len(blocks[key])
        if len(lits) > capacity:
            if capacity == 1:
                model.add_at_most_one(lits)
            else:
                model.add(sum(lits) <= capacity)

    games_by_team: dict[uuid.UUID, list[Game]] = defaultdict(list)
    games_by_coach: dict[uuid.UUID, list[Game]] = defaultdict(list)
    for game in games:
        games_by_team[game.home_team_id].append(game)
        games_by_team[game.away_team_id].append(game)
        coaches = problem.teams[game.home_team_id].coach_ids | problem.teams[game.away_team_id].coach_ids
        for coach_id in coaches:
            games_by_coach[coach_id].append(game)

    # A coach of a single team has exactly that team's games, so its
    # constraints would be duplicates; only distinct game sets are added.
    seen_game_sets: set[frozenset[int]] = set()
    for resource_games in list(games_by_team.values()) + list(games_by_coach.values()):
        game_set = frozenset(g.index for g in resource_games)
        if game_set in seen_game_sets:
            continue
        seen_game_sets.add(game_set)
        _add_no_overlap(model, [entry for g in resource_games for entry in x_by_game[g.index]])

    fixed_week: Counter = Counter()
    fixed_date: Counter = Counter()
    for fixed in problem.fixed_games:
        for team_id in (fixed.home_team_id, fixed.away_team_id):
            fixed_week[(team_id, problem.week_of(fixed.start))] += 1
            fixed_date[(team_id, problem.date_of(fixed.start))] += 1

    window = pacing_window(problem)
    penalties: list[cp_model.LinearExprT] = []
    for team_id, team_games in games_by_team.items():
        division = problem.divisions[problem.teams[team_id].division_id]
        game_count = len(team_games)
        by_date: dict[date, list[cp_model.IntVar]] = defaultdict(list)
        for game in team_games:
            for slot, var in x_by_game[game.index]:
                by_date[slot_date[slot.id]].append(var)

        day_count = {day: _count(model, lits, game_count) for day, lits in by_date.items()}
        days_by_week: dict[tuple[int, int], list[date]] = defaultdict(list)
        for day in day_count:
            days_by_week[tuple(day.isocalendar())[:2]].append(day)
        week_count = {
            week: _count(model, [day_count[day] for day in days], game_count)
            for week, days in days_by_week.items()
        }
        team_fixed_dates = {day: n for (team, day), n in fixed_date.items() if team == team_id}

        # Weekly cap (hard)
        if division.max_games_per_week is not None:
            for week, count in week_count.items():
                model.add(count <= division.max_games_per_week - fixed_week[(team_id, week)])

        # Same day / back-to-back days (soft). Days with a Published game count
        # too, even when the team has no open slot that day.
        games_on = {
            day: day_count.get(day, 0) + team_fixed_dates.get(day, 0)
            for day in set(day_count) | set(team_fixed_dates)
        }
        for day, lits in by_date.items():
            on_day = len(lits) + team_fixed_dates.get(day, 0)
            if on_day >= 2:
                same = model.new_int_var(0, on_day, "")
                model.add(same >= games_on[day] - 1)
                penalties.append(SAME_DAY_WEIGHT * same)
            next_day = day + timedelta(days=1)
            if next_day in games_on:
                both = model.new_int_var(0, game_count * 2, "")
                model.add(both >= games_on[day] + games_on[next_day] - 1)
                penalties.append(BACK_TO_BACK_WEIGHT * both)

        # Even spread across the weeks that have usable slots (soft)
        target = math.ceil(game_count / max(1, len(week_count)))
        for week, count in week_count.items():
            possible = sum(len(by_date[day]) for day in days_by_week[week]) + fixed_week[(team_id, week)]
            if possible > target:
                crowd = model.new_int_var(0, game_count, "")
                model.add(crowd >= count + fixed_week[(team_id, week)] - target)
                penalties.append(WEEK_CROWDING_WEIGHT * crowd)

        # Even pace across the playable window (soft)
        if window is not None:
            total_games = game_count + sum(team_fixed_dates.values())
            penalties.extend(_add_pacing(model, window, day_count, team_fixed_dates, total_games))

    if penalties:
        model.minimize(sum(penalties))

    solver = cp_model.CpSolver()
    params = solver.parameters
    params.max_time_in_seconds = time_limit_seconds
    params.num_workers = workers
    params.random_seed = seed
    params.interleave_search = deterministic
    # Measured on the seed league (112 games, 1,040 slots): these presolve
    # passes cost ~18s and find nothing useful for this model shape (identical
    # teams, slot grids); without them the solve takes ~3s.
    params.symmetry_level = 0
    params.find_big_linear_overlap = False
    params.cp_model_probing_level = 0
    status = _STATUS_NAMES.get(solver.solve(model), "timeout")

    result = SolveResult(status)
    if result.found:
        result.objective = solver.objective_value if penalties else 0.0
        free = {key: list(members) for key, members in blocks.items()}
        for game in games:
            for slot, var in x_by_game[game.index]:
                if solver.boolean_value(var):
                    result.assignment[game.index] = free[representative[slot.id]].pop(0).id
                    break
    return result


def diagnose_infeasible(
    problem: Problem,
    games: list[Game],
    eligibility: Eligibility,
    *,
    time_limit_seconds: float = 10.0,
) -> list[str]:
    """Explain an infeasible season by solving each division on its own (without round windows)."""
    issues: list[str] = []
    division_ids = sorted({g.division_id for g in games}, key=lambda d: problem.divisions[d].name)
    for division_id in division_ids:
        subset = [g for g in games if g.division_id == division_id]
        result = solve(
            problem, subset, eligibility, time_limit_seconds=time_limit_seconds, use_round_windows=False
        )
        name = problem.divisions[division_id].name
        if result.status == "infeasible":
            issues.append(
                f"Division {name} cannot be scheduled even on its own: its weekly cap, team and coach "
                "availability and slot restrictions conflict."
            )
        elif not result.found:
            issues.append(f"Division {name} could not be checked on its own within {time_limit_seconds:.0f}s.")

    if not issues:
        divisions_by_coach: dict[uuid.UUID, set[str]] = defaultdict(set)
        for team in problem.teams.values():
            for coach_id in team.coach_ids:
                divisions_by_coach[coach_id].add(problem.divisions[team.division_id].name)
        shared = sorted(
            f"{problem.coach_names.get(c, str(c))} ({', '.join(sorted(d))})"
            for c, d in divisions_by_coach.items()
            if len(d) > 1
        )
        message = "Every division can be scheduled on its own, but not all together: they compete for the same slots"
        if shared:
            message += " and share coaches: " + "; ".join(shared)
        issues.append(message + ".")
    return issues
