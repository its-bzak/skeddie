"""Which open slots each team, coach and game could use.

Shared by the precheck and the solver. The validator deliberately does not use
this module, so a bug here cannot hide itself.
"""

import uuid
from dataclasses import dataclass

from services.matchups import Game
from services.problem import Interval, Problem, SlotInfo


def _blocked(slots: list[SlotInfo], intervals: list[Interval]) -> set[uuid.UUID]:
    if not intervals:
        return set()
    return {s.id for s in slots if any(i.overlaps(s.start, s.end) for i in intervals)}


@dataclass
class Eligibility:
    team_blocked: dict[uuid.UUID, set[uuid.UUID]]  # team -> slot ids it cannot use
    coach_blocked: dict[uuid.UUID, set[uuid.UUID]]

    @classmethod
    def build(cls, problem: Problem) -> "Eligibility":
        return cls(
            team_blocked={t: _blocked(problem.slots, problem.team_blocked.get(t, [])) for t in problem.teams},
            coach_blocked={
                c: _blocked(problem.slots, intervals) for c, intervals in problem.coach_blocked.items()
            },
        )

    def blocked_for_team(self, problem: Problem, team_id: uuid.UUID) -> set[uuid.UUID]:
        """Slots unusable by the team itself or by any of its coaches."""
        blocked = set(self.team_blocked.get(team_id, ()))
        for coach_id in problem.teams[team_id].coach_ids:
            blocked |= self.coach_blocked.get(coach_id, set())
        return blocked

    def team_slots(self, problem: Problem, team_id: uuid.UUID) -> list[SlotInfo]:
        division_id = problem.teams[team_id].division_id
        blocked = self.blocked_for_team(problem, team_id)
        return [
            s
            for s in problem.slots
            if s.id not in blocked and (s.division_id is None or s.division_id == division_id)
        ]

    def game_slots(self, problem: Problem, game: Game) -> list[SlotInfo]:
        blocked = self.blocked_for_team(problem, game.home_team_id) | self.blocked_for_team(
            problem, game.away_team_id
        )
        return [
            s
            for s in problem.slots
            if s.id not in blocked and (s.division_id is None or s.division_id == game.division_id)
        ]
