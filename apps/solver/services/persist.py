"""Schedule a season end to end and write the result in one transaction.

    lock season -> drop its Draft games -> load problem -> matchups -> precheck
    -> solve -> validate -> insert Draft games and link them to their slots

The caller owns the transaction: commit when `outcome.ok`, otherwise roll back
(which also restores the Draft games that were removed). The database's
triggers and exclusion constraints check every row as it is written, so a bug
anywhere above turns into a rolled-back transaction, never a double booking.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from models import Event, Season, TimeSlot
from services.eligibility import Eligibility
from services.locks import lock_season
from services.matchups import Game, MatchupError, division_games
from services.precheck import precheck
from services.problem import Problem, load_problem
from services.scheduler import diagnose_infeasible, solve
from services.validate import validate_schedule


@dataclass
class ScheduleOutcome:
    status: str  # scheduled | precheck_failed | infeasible | timeout
    games_scheduled: int = 0
    issues: list[str] = field(default_factory=list)
    solve_seconds: float = 0.0
    solver_status: str | None = None
    objective: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "scheduled"


class ScheduleIntegrityError(RuntimeError):
    """The solver produced a schedule that fails validation. This is a bug; nothing is saved."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("Solver output failed validation:\n" + "\n".join(f"  - {v}" for v in violations))


def build_games(problem: Problem) -> tuple[list[Game], list[str]]:
    games: list[Game] = []
    issues: list[str] = []
    for division in sorted(problem.divisions.values(), key=lambda d: d.name):
        if not division.team_ids:
            continue
        fixed = [f for f in problem.fixed_games if f.division_id == division.id]
        try:
            games.extend(division_games(division, fixed, start_index=len(games)))
        except MatchupError as exc:
            issues.append(f"Division {division.name} {exc}.")
    return games, issues


def schedule_season(db: Session, season_id: uuid.UUID, *, time_limit_seconds: float = 30.0) -> ScheduleOutcome:
    if db.get(Season, season_id) is None:
        raise LookupError(f"Season {season_id} does not exist")

    lock_season(db, season_id)
    # Draft games are replaced wholesale. Their slots are freed (ON DELETE SET
    # NULL) and their bookings removed (ON DELETE CASCADE) by the database.
    db.execute(
        delete(Event).where(Event.season_id == season_id, Event.status == "Draft"),
        execution_options={"synchronize_session": False},
    )
    db.expire_all()

    problem = load_problem(db, season_id)
    games, issues = build_games(problem)
    eligibility = Eligibility.build(problem)
    if not issues:
        issues = precheck(problem, games, eligibility)
    if issues:
        return ScheduleOutcome("precheck_failed", issues=issues)

    result = solve(problem, games, eligibility, time_limit_seconds=time_limit_seconds)
    if result.status == "infeasible":
        return ScheduleOutcome(
            "infeasible",
            issues=diagnose_infeasible(problem, games, eligibility),
            solve_seconds=result.wall_seconds,
            solver_status=result.status,
        )
    if not result.found:
        return ScheduleOutcome(
            "timeout",
            issues=[f"No schedule was found within {time_limit_seconds:.0f} seconds; try a longer time limit."],
            solve_seconds=result.wall_seconds,
            solver_status=result.status,
        )

    violations = validate_schedule(problem, games, result.assignment)
    if violations:
        raise ScheduleIntegrityError(violations)

    season = db.get(Season, season_id)
    events = [
        Event(
            organization_id=season.organization_id,
            season_id=season_id,
            division_id=g.division_id,
            home_team_id=g.home_team_id,
            away_team_id=g.away_team_id,
            status="Draft",
        )
        for g in games
    ]
    db.add_all(events)
    db.flush()

    slots = {
        s.id: s for s in db.scalars(select(TimeSlot).where(TimeSlot.id.in_(list(result.assignment.values()))))
    }
    for game, event in zip(games, events, strict=True):
        slots[result.assignment[game.index]].event_id = event.id
    db.flush()

    return ScheduleOutcome(
        "scheduled",
        games_scheduled=len(games),
        solve_seconds=result.wall_seconds,
        solver_status=result.status,
        objective=result.objective,
    )
