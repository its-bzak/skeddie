"""Fast, plain-language capacity checks run before the solver.

Each check is a necessary condition for a schedule to exist. Passing them all
does not guarantee one exists (the solver decides that), but failing any of
them explains exactly why there is none.
"""

import uuid
from collections import Counter, defaultdict

from services.eligibility import Eligibility
from services.matchups import Game
from services.problem import Problem


def precheck(problem: Problem, games: list[Game], eligibility: Eligibility) -> list[str]:
    issues: list[str] = []

    games_by_division: dict[uuid.UUID, list[Game]] = defaultdict(list)
    needed_by_team: Counter = Counter()
    for game in games:
        games_by_division[game.division_id].append(game)
        needed_by_team[game.home_team_id] += 1
        needed_by_team[game.away_team_id] += 1

    if len(games) > len(problem.slots):
        issues.append(f"The season needs {len(games)} games but has only {len(problem.slots)} open time slots.")

    for division_id, division_games in games_by_division.items():
        division = problem.divisions[division_id]
        usable = sum(1 for s in problem.slots if s.division_id is None or s.division_id == division_id)
        if len(division_games) > usable:
            issues.append(
                f"Division {division.name} needs {len(division_games)} games but only {usable} open "
                "slots are available to it."
            )

    fixed_per_week: Counter = Counter()
    for fixed in problem.fixed_games:
        week = problem.week_of(fixed.start)
        fixed_per_week[(fixed.home_team_id, week)] += 1
        fixed_per_week[(fixed.away_team_id, week)] += 1

    for team_id, needed in sorted(needed_by_team.items(), key=lambda kv: problem.teams[kv[0]].name):
        team = problem.teams[team_id]
        division = problem.divisions[team.division_id]
        slots = eligibility.team_slots(problem, team_id)
        if len(slots) < needed:
            issues.append(
                f"{team.name} needs {needed} games but only {len(slots)} open slots fit its own and its "
                "coaches' availability."
            )
            continue
        cap = division.max_games_per_week
        if cap is not None:
            weeks = {problem.week_of(s.start) for s in slots}
            capacity = sum(max(0, cap - fixed_per_week[(team_id, w)]) for w in weeks)
            if capacity < needed:
                issues.append(
                    f"{team.name} needs {needed} games but its usable slots fall in only {len(weeks)} "
                    f"weeks and Division {division.name} allows {cap} per week (at most {capacity})."
                )

    checked_pairs: set[frozenset[uuid.UUID]] = set()
    for game in games:
        pair = frozenset((game.home_team_id, game.away_team_id))
        if pair in checked_pairs:
            continue
        checked_pairs.add(pair)
        if not eligibility.game_slots(problem, game):
            home = problem.teams[game.home_team_id].name
            away = problem.teams[game.away_team_id].name
            issues.append(f"{home} vs {away}: there is no open slot when both teams and their coaches are free.")

    if issues and problem.makeup_weeks:
        weeks = "week" if problem.makeup_weeks == 1 else f"{problem.makeup_weeks} weeks"
        issues.append(
            f"Slots in the season's last {weeks} are kept free as a makeup window and were not counted; "
            "lowering the season's makeup_weeks makes them available."
        )
    return issues
