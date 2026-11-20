"""How good a finished schedule is, measured against the engine's soft goals.

Measured from the schedule itself rather than read from the solver's
objective, so the numbers mean the same thing however the schedule was found.
Published games already on the calendar count too.

The pacing band is defined here once and the solver builds its pacing goal
from the same helpers, so the report and the engine cannot disagree.
"""

import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from services.matchups import Game
from services.problem import Problem
from services.timeutil import dates_on_weekday

SUNDAY = 6


@dataclass(frozen=True)
class ScheduleQuality:
    # Games a team is ahead of or behind an even pace, summed over every
    # Sunday checkpoint of the playable window.
    off_pace_games: int
    # Extra games a team plays on a day it already plays.
    same_day_doubles: int
    # Pairs of consecutive days on which a team plays.
    back_to_backs: int
    # The solver proved no better schedule exists under the current rules.
    proven_optimal: bool

    def as_dict(self) -> dict:
        return asdict(self)


def pacing_window(problem: Problem) -> tuple[date, date] | None:
    """The local dates games may fall on: season start to the end of the playable window."""
    if not problem.slots:
        return None
    slot_dates = [problem.date_of(s.start) for s in problem.slots]
    first = problem.season_start or min(slot_dates)
    last = problem.playable_end or max(slot_dates)
    return (first, last) if first <= last else None


def pace_checkpoints(first: date, last: date) -> list[date]:
    """The Sundays at which pace is measured; by the last day every game has been played."""
    return [sunday for sunday in dates_on_weekday(first, last, SUNDAY) if sunday < last]


def pace_band(total_games: int, first: date, checkpoint: date, last: date) -> tuple[int, int]:
    """Fewest and most games a team should have played by `checkpoint` to be on pace."""
    span = (last - first).days + 1
    elapsed = (checkpoint - first).days + 1
    return total_games * elapsed // span, -(-total_games * elapsed // span)


def schedule_quality(
    problem: Problem,
    games: list[Game],
    assignment: dict[int, uuid.UUID],
    *,
    proven_optimal: bool,
) -> ScheduleQuality:
    slot_start = {s.id: s.start for s in problem.slots}
    dates_by_team: dict[uuid.UUID, list[date]] = defaultdict(list)
    for game in games:
        day = problem.date_of(slot_start[assignment[game.index]])
        dates_by_team[game.home_team_id].append(day)
        dates_by_team[game.away_team_id].append(day)
    for fixed in problem.fixed_games:
        day = problem.date_of(fixed.start)
        dates_by_team[fixed.home_team_id].append(day)
        dates_by_team[fixed.away_team_id].append(day)

    window = pacing_window(problem)
    checkpoints = pace_checkpoints(*window) if window else []
    off_pace = same_day = back_to_back = 0
    for team_dates in dates_by_team.values():
        per_day = Counter(team_dates)
        same_day += sum(count - 1 for count in per_day.values() if count > 1)
        back_to_back += sum(1 for day in per_day if day + timedelta(days=1) in per_day)
        for checkpoint in checkpoints:
            low, high = pace_band(len(team_dates), window[0], checkpoint, window[1])
            played = sum(1 for day in team_dates if day <= checkpoint)
            off_pace += max(0, low - played, played - high)

    return ScheduleQuality(off_pace, same_day, back_to_back, proven_optimal)
