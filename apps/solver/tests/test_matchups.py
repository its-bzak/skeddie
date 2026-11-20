import uuid
from collections import Counter
from datetime import datetime, timezone

import pytest

from services.matchups import MatchupError, assign_rounds, division_games, orient_balanced, pairings
from services.problem import DivisionInfo, FixedGame


def team_ids(n: int) -> list[uuid.UUID]:
    return sorted((uuid.uuid4() for _ in range(n)), key=str)


CASES = [(n, g) for n in range(2, 11) for g in range(1, 2 * (n - 1) + 2)]


@pytest.mark.parametrize("n,g", CASES)
def test_every_team_plays_exactly_games_per_team(n, g):
    teams = team_ids(n)
    if (n * g) % 2:
        with pytest.raises(MatchupError, match="odd"):
            pairings(teams, g)
        return

    pairs = pairings(teams, g)
    games_per_team = Counter(t for pair in pairs for t in pair)
    assert all(games_per_team[t] == g for t in teams)
    assert all(a != b for a, b in pairs)

    # Opponents are spread evenly: every pair meets floor or ceil(g / (n - 1)) times
    meetings = Counter(frozenset(p) for p in pairs)
    low, high = g // (n - 1), -(-g // (n - 1))
    all_pairs = {frozenset((a, b)) for i, a in enumerate(teams) for b in teams[i + 1 :]}
    assert all(low <= meetings[p] <= high for p in all_pairs)


@pytest.mark.parametrize("n,g", [c for c in CASES if (c[0] * c[1]) % 2 == 0])
def test_home_and_away_are_balanced(n, g):
    oriented = orient_balanced(pairings(team_ids(n), g))
    home = Counter(h for h, _ in oriented)
    away = Counter(a for _, a in oriented)
    for team in set(home) | set(away):
        assert abs(home[team] - away[team]) <= 1


@pytest.mark.parametrize("n,g", [c for c in CASES if (c[0] * c[1]) % 2 == 0])
def test_no_team_plays_twice_in_a_round(n, g):
    pairs = pairings(team_ids(n), g)
    rounds = assign_rounds(pairs)
    per_round = Counter()
    for (a, b), r in zip(pairs, rounds, strict=True):
        per_round[(a, r)] += 1
        per_round[(b, r)] += 1
    assert max(per_round.values()) == 1


@pytest.mark.parametrize("n", range(2, 11))
def test_full_round_robins_use_the_circle_methods_rounds(n):
    for cycles in (1, 2):
        games_per_team = cycles * (n - 1)
        if (n * games_per_team) % 2:
            continue
        rounds = assign_rounds(pairings(team_ids(n), games_per_team))
        assert max(rounds) + 1 == cycles * (n - 1 if n % 2 == 0 else n)


def test_division_games_carry_their_round_and_published_games_leave_theirs():
    teams = team_ids(4)
    division = DivisionInfo(uuid.uuid4(), "U10", 3, 1, tuple(teams))
    planned = division_games(division, [])
    assert {(g.rounds, g.round) for g in planned} == {(3, 0), (3, 1), (3, 2)}

    first = next(g for g in planned if g.round == 0)
    remaining = division_games(division, [_fixed(division, first.home_team_id, first.away_team_id)])
    assert Counter(g.round for g in remaining) == Counter({0: 1, 1: 2, 2: 2})


def test_seed_shape_is_a_single_round_robin():
    teams = team_ids(8)
    pairs = pairings(teams, 7)
    assert len(pairs) == 28
    assert len({frozenset(p) for p in pairs}) == 28  # everyone plays everyone once


def test_rejects_tiny_or_unconfigured_divisions():
    with pytest.raises(MatchupError, match="at least 2 teams"):
        pairings(team_ids(1), 3)
    division = DivisionInfo(uuid.uuid4(), "U10", None, 1, tuple(team_ids(4)))
    with pytest.raises(MatchupError, match="games_per_team"):
        division_games(division, [])


def _fixed(division: DivisionInfo, home: uuid.UUID, away: uuid.UUID) -> FixedGame:
    now = datetime(2027, 4, 5, 20, tzinfo=timezone.utc)
    return FixedGame(uuid.uuid4(), division.id, home, away, now, now)


def test_published_games_are_subtracted_from_the_plan():
    teams = team_ids(4)
    division = DivisionInfo(uuid.uuid4(), "U10", 3, 1, tuple(teams))
    fixed = [_fixed(division, teams[0], teams[1]), _fixed(division, teams[2], teams[3])]

    remaining = division_games(division, fixed)
    assert len(remaining) == 4
    played = {frozenset((g.home_team_id, g.away_team_id)) for g in remaining}
    assert frozenset((teams[0], teams[1])) not in played
    totals = Counter(t for g in remaining for t in (g.home_team_id, g.away_team_id))
    assert all(totals[t] == 2 for t in teams)


def test_published_games_outside_the_plan_are_rejected():
    teams = team_ids(4)
    division = DivisionInfo(uuid.uuid4(), "U10", 3, 1, tuple(teams))
    duplicate = [_fixed(division, teams[0], teams[1]), _fixed(division, teams[1], teams[0])]
    with pytest.raises(MatchupError, match="do not fit"):
        division_games(division, duplicate)
