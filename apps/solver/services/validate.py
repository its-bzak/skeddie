"""Independent checker for a finished schedule.

Re-derives every hard rule directly from the Problem with straightforward
interval comparisons. It shares no code with the solver or the eligibility
module, so a bug there cannot also hide here. Every solution is validated
before it is written to the database.
"""

import uuid
from collections import Counter, defaultdict
from datetime import datetime

from services.matchups import Game
from services.problem import Problem


def _overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def validate_schedule(problem: Problem, games: list[Game], assignment: dict[int, uuid.UUID]) -> list[str]:
    violations: list[str] = []
    slots = {s.id: s for s in problem.slots}

    def team_name(team_id: uuid.UUID) -> str:
        return problem.teams[team_id].name if team_id in problem.teams else str(team_id)

    def coach_name(coach_id: uuid.UUID) -> str:
        return problem.coach_names.get(coach_id, str(coach_id))

    # Every game placed exactly once, in a real open slot, one game per slot
    game_indexes = {g.index for g in games}
    for g in games:
        if g.index not in assignment:
            matchup = f"{team_name(g.home_team_id)} vs {team_name(g.away_team_id)}"
            violations.append(f"Game {g.index} ({matchup}) has no slot")
    for index in set(assignment) - game_indexes:
        violations.append(f"Assignment for unknown game {index}")
    for slot_id, count in Counter(assignment.values()).items():
        if count > 1:
            violations.append(f"Slot {slot_id} holds {count} games")

    placed: list[tuple[Game, datetime, datetime]] = []
    for g in games:
        slot_id = assignment.get(g.index)
        if slot_id is None:
            continue
        slot = slots.get(slot_id)
        if slot is None:
            violations.append(f"Game {g.index} uses slot {slot_id}, which is not an open slot of this season")
            continue
        if g.home_team_id == g.away_team_id:
            violations.append(f"Game {g.index} has the same team on both sides")
        for team_id in (g.home_team_id, g.away_team_id):
            if team_id not in problem.teams or problem.teams[team_id].division_id != g.division_id:
                violations.append(f"Game {g.index}: {team_name(team_id)} is not in the game's division")
        if slot.division_id is not None and slot.division_id != g.division_id:
            violations.append(f"Game {g.index} uses a slot reserved for another division")
        placed.append((g, slot.start, slot.end))

    # No team or coach in two overlapping games
    busy: dict[tuple[str, uuid.UUID], list[tuple[datetime, datetime, int]]] = defaultdict(list)
    for g, start, end in placed:
        for team_id in (g.home_team_id, g.away_team_id):
            busy[("team", team_id)].append((start, end, g.index))
        coaches = set()
        for team_id in (g.home_team_id, g.away_team_id):
            if team_id in problem.teams:
                coaches |= problem.teams[team_id].coach_ids
        for coach_id in coaches:
            busy[("coach", coach_id)].append((start, end, g.index))

    for (kind, resource_id), intervals in busy.items():
        for i in range(len(intervals)):
            for j in range(i + 1, len(intervals)):
                a, b = intervals[i], intervals[j]
                if _overlap(a[0], a[1], b[0], b[1]):
                    label = team_name(resource_id) if kind == "team" else coach_name(resource_id)
                    violations.append(f"{kind.title()} {label} is in games {a[2]} and {b[2]} at the same time")

    # Unavailability and games already on the calendar
    for g, start, end in placed:
        checks = []
        for team_id in (g.home_team_id, g.away_team_id):
            checks.append((team_name(team_id), problem.team_blocked.get(team_id, [])))
            if team_id in problem.teams:
                for coach_id in problem.teams[team_id].coach_ids:
                    label = f"coach {coach_name(coach_id)}"
                    checks.append((label, problem.coach_blocked.get(coach_id, [])))
        for label, intervals in checks:
            for interval in intervals:
                if _overlap(start, end, interval.start, interval.end):
                    violations.append(f"Game {g.index} overlaps a blocked time for {label} ({interval.reason})")

    # Weekly cap and total games per team, counting Published games
    per_week: Counter = Counter()
    per_team: Counter = Counter()
    for g, start, _ in placed:
        for team_id in (g.home_team_id, g.away_team_id):
            per_week[(team_id, problem.week_of(start))] += 1
            per_team[team_id] += 1
    for fixed in problem.fixed_games:
        for team_id in (fixed.home_team_id, fixed.away_team_id):
            per_week[(team_id, problem.week_of(fixed.start))] += 1
            per_team[team_id] += 1

    for (team_id, week), count in per_week.items():
        if team_id not in problem.teams:
            continue
        cap = problem.divisions[problem.teams[team_id].division_id].max_games_per_week
        if cap is not None and count > cap:
            violations.append(f"{team_name(team_id)} plays {count} games in week {week[0]}-W{week[1]:02d} (cap {cap})")

    scheduled_divisions = {g.division_id for g in games}
    for team in problem.teams.values():
        if team.division_id not in scheduled_divisions:
            continue
        expected = problem.divisions[team.division_id].games_per_team
        if expected is not None and per_team[team.id] != expected:
            violations.append(f"{team.name} plays {per_team[team.id]} games (expected {expected})")

    return violations
