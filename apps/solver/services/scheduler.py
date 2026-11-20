"""The scheduling engine: assign every game to one open time slot (OR-Tools CP-SAT).

Slots with the same start, end and division restriction are interchangeable
for every rule below (only the field differs), so the model works on "time
blocks" whose capacity is the number of such slots; concrete fields are handed
out afterwards. This removes the symmetry between identical courts, which
otherwise multiplies the search space by the number of courts.

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
falling off an even pace across the season. Pacing: at the end of each
Monday-Sunday week of the playable window, a team with G games should have
played between floor and ceil of G x days_elapsed / playable_days of them;
only games outside that band cost anything, so a well-paced schedule scores 0
and the solver can prove it optimal quickly.
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
from services.timeutil import dates_on_weekday

SAME_DAY_WEIGHT = 10
BACK_TO_BACK_WEIGHT = 3
WEEK_CROWDING_WEIGHT = 5
PACE_WEIGHT = 2
SUNDAY = 6

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

    @property
    def found(self) -> bool:
        return self.status in ("optimal", "feasible")


def _pacing_window(problem: Problem) -> tuple[date, date] | None:
    """The local dates games may fall on: the season start to the end of its playable window."""
    if not problem.slots:
        return None
    slot_dates = [problem.date_of(s.start) for s in problem.slots]
    first = problem.season_start or min(slot_dates)
    last = problem.playable_end or max(slot_dates)
    return (first, last) if first <= last else None


def _add_pacing(
    model: cp_model.CpModel,
    window: tuple[date, date],
    by_date: dict[date, list[cp_model.IntVar]],
    fixed_dates: dict[date, int],
    total_games: int,
) -> list[cp_model.LinearExprT]:
    """Penalty terms for one team falling outside the even-pace band at each week end."""
    first, last = window
    span = (last - first).days + 1
    week_ends = list(dates_on_weekday(first, last, SUNDAY))
    if not week_ends or week_ends[-1] != last:
        week_ends.append(last)

    penalties: list[cp_model.LinearExprT] = []
    played_so_far: cp_model.LinearExprT = 0
    period_start = first
    for week_end in week_ends:
        this_week = [var for day, lits in by_date.items() if period_start <= day <= week_end for var in lits]
        played = model.new_int_var(0, total_games, "")
        model.add(played == played_so_far + sum(this_week))
        played_so_far = played
        period_start = week_end + timedelta(days=1)

        # Every complete schedule has played all games by the last day.
        if week_end == last:
            continue
        on_calendar = played + sum(n for day, n in fixed_dates.items() if day <= week_end)
        elapsed = (week_end - first).days + 1
        low = total_games * elapsed // span
        high = -(-total_games * elapsed // span)
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
) -> SolveResult:
    """Solve; `deterministic=True` guarantees identical output for identical input (about 2x slower)."""
    started = time.monotonic()
    if not games:
        return SolveResult("optimal", {}, 0.0, 0.0)

    eligibility = eligibility or Eligibility.build(problem)
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

    slot_week = {s.id: problem.week_of(s.start) for s in problem.slots}
    slot_date = {s.id: problem.date_of(s.start) for s in problem.slots}

    x_by_game: dict[int, list[tuple[SlotInfo, cp_model.IntVar]]] = {}
    by_block: dict[tuple, list[cp_model.IntVar]] = defaultdict(list)
    for game in games:
        keys = list(dict.fromkeys(block_key[s.id] for s in eligibility.game_slots(problem, game)))
        if not keys:
            return SolveResult("infeasible", wall_seconds=time.monotonic() - started)
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

    pacing_window = _pacing_window(problem)
    penalties: list[cp_model.LinearExprT] = []
    for team_id, team_games in games_by_team.items():
        division = problem.divisions[problem.teams[team_id].division_id]
        by_week: dict[tuple[int, int], list[cp_model.IntVar]] = defaultdict(list)
        by_date: dict[date, list[cp_model.IntVar]] = defaultdict(list)
        for game in team_games:
            for slot, var in x_by_game[game.index]:
                by_week[slot_week[slot.id]].append(var)
                by_date[slot_date[slot.id]].append(var)

        # Weekly cap (hard)
        if division.max_games_per_week is not None:
            for week, lits in by_week.items():
                model.add(sum(lits) <= division.max_games_per_week - fixed_week[(team_id, week)])

        # Same day / back-to-back days (soft). Days with a Published game count
        # too, even when the team has no open slot that day.
        team_fixed_dates = {day: count for (team, day), count in fixed_date.items() if team == team_id}
        games_on = {
            day: sum(by_date.get(day, [])) + team_fixed_dates.get(day, 0)
            for day in set(by_date) | set(team_fixed_dates)
        }
        for day, lits in by_date.items():
            on_day = len(lits) + fixed_date[(team_id, day)]
            if on_day >= 2:
                same = model.new_int_var(0, on_day, "")
                model.add(same >= games_on[day] - 1)
                penalties.append(SAME_DAY_WEIGHT * same)
            next_day = day + timedelta(days=1)
            if next_day in games_on:
                both = model.new_int_var(0, len(team_games) * 2, "")
                model.add(both >= games_on[day] + games_on[next_day] - 1)
                penalties.append(BACK_TO_BACK_WEIGHT * both)

        # Even spread across the weeks that have usable slots (soft)
        target = math.ceil(len(team_games) / max(1, len(by_week)))
        for week, lits in by_week.items():
            if len(lits) + fixed_week[(team_id, week)] > target:
                crowd = model.new_int_var(0, len(team_games), "")
                model.add(crowd >= sum(lits) + fixed_week[(team_id, week)] - target)
                penalties.append(WEEK_CROWDING_WEIGHT * crowd)

        # Even pace across the playable window (soft)
        if pacing_window is not None:
            total_games = len(team_games) + sum(team_fixed_dates.values())
            penalties.extend(_add_pacing(model, pacing_window, by_date, team_fixed_dates, total_games))

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

    result = SolveResult(status, wall_seconds=time.monotonic() - started)
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
    """Explain an infeasible season by solving each division on its own."""
    issues: list[str] = []
    division_ids = sorted({g.division_id for g in games}, key=lambda d: problem.divisions[d].name)
    for division_id in division_ids:
        subset = [g for g in games if g.division_id == division_id]
        result = solve(problem, subset, eligibility, time_limit_seconds=time_limit_seconds)
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
