"""Who plays whom: one-on-one matchups for each division.

For a division of n teams that each play g games:

* every pair of teams meets g // (n - 1) times (full round robins), and
* the remaining r = g % (n - 1) games per team come from a circulant pattern
  (each team plays its r nearest neighbours in a fixed order), which gives
  every team exactly r extra games with no repeated pairing.

So every team plays exactly g games. That is impossible when n * g is odd
(every game uses two teams), and those divisions are rejected.

Home/away is assigned by walking an Euler circuit of the matchup graph, which
guarantees each team's home and away counts differ by at most one.
"""

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass

from services.problem import DivisionInfo, FixedGame


@dataclass(frozen=True)
class Game:
    index: int
    division_id: uuid.UUID
    home_team_id: uuid.UUID
    away_team_id: uuid.UUID


class MatchupError(ValueError):
    pass


def pairings(team_ids: list[uuid.UUID], games_per_team: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Unordered pairings in which every team appears exactly `games_per_team` times."""
    n = len(team_ids)
    if n < 2:
        raise MatchupError(f"needs at least 2 teams (has {n})")
    if games_per_team < 1:
        raise MatchupError("games_per_team must be at least 1")
    if (n * games_per_team) % 2:
        raise MatchupError(
            f"{n} teams x {games_per_team} games is odd, so every team cannot play the same number "
            "of games; change games_per_team or the number of teams"
        )

    teams = list(team_ids)
    full_cycles, remainder = divmod(games_per_team, n - 1)
    result: list[tuple[uuid.UUID, uuid.UUID]] = []

    for _ in range(full_cycles):
        result.extend((teams[i], teams[j]) for i in range(n) for j in range(i + 1, n))

    # Offsets 1..remainder//2 each add 2 games per team; for odd remainders
    # (only possible when n is even) the "opposite" pairing adds 1 more.
    for offset in range(1, remainder // 2 + 1):
        result.extend((teams[i], teams[(i + offset) % n]) for i in range(n))
    if remainder % 2:
        result.extend((teams[i], teams[i + n // 2]) for i in range(n // 2))

    return result


def orient_balanced(pairs: list[tuple[uuid.UUID, uuid.UUID]]) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Choose (home, away) for every pairing so each team's home/away counts differ by <= 1.

    Teams with an odd number of games are joined to a dummy vertex so every
    degree is even; an Euler circuit then enters and leaves each team equally
    often. Dropping the dummy edges shifts each team by at most one.
    """
    dummy = object()
    edges: list[tuple[object, object]] = list(pairs)
    degree: Counter = Counter()
    for a, b in pairs:
        degree[a] += 1
        degree[b] += 1
    for team in sorted((t for t, d in degree.items() if d % 2), key=str):
        edges.append((team, dummy))

    adjacency: dict[object, list[tuple[int, object]]] = defaultdict(list)
    for index, (a, b) in enumerate(edges):
        adjacency[a].append((index, b))
        adjacency[b].append((index, a))

    used = [False] * len(edges)
    oriented: list[tuple[object, object] | None] = [None] * len(edges)
    cursor: dict[object, int] = defaultdict(int)

    # Hierholzer: every closed trail is balanced at each vertex, so orienting
    # edges in the direction they are first walked keeps in == out.
    for start in sorted(adjacency, key=lambda v: (v is dummy, str(v))):
        stack = [start]
        while stack:
            vertex = stack[-1]
            edges_here = adjacency[vertex]
            while cursor[vertex] < len(edges_here) and used[edges_here[cursor[vertex]][0]]:
                cursor[vertex] += 1
            if cursor[vertex] == len(edges_here):
                stack.pop()
                continue
            index, other = edges_here[cursor[vertex]]
            used[index] = True
            oriented[index] = (vertex, other)
            stack.append(other)

    return [oriented[i] for i in range(len(pairs))]  # type: ignore[misc]


def division_games(
    division: DivisionInfo,
    fixed_games: list[FixedGame],
    start_index: int = 0,
) -> list[Game]:
    """Games still to schedule for a division, after removing Published ones.

    Raises MatchupError if the division cannot be scheduled as configured or
    its Published games do not fit the matchup plan.
    """
    if division.games_per_team is None:
        raise MatchupError("has no games_per_team set")

    planned = Counter(
        frozenset(pair) for pair in pairings(sorted(division.team_ids, key=str), division.games_per_team)
    )
    for game in fixed_games:
        key = frozenset((game.home_team_id, game.away_team_id))
        if planned[key] == 0:
            raise MatchupError(
                "has Published games that do not fit its matchup plan (an extra meeting between two "
                "teams); cancel or unpublish them before rescheduling"
            )
        planned[key] -= 1

    remaining: list[tuple[uuid.UUID, uuid.UUID]] = []
    for key in sorted(planned, key=lambda k: sorted(map(str, k))):
        a, b = sorted(key, key=str)
        remaining.extend([(a, b)] * planned[key])

    return [
        Game(start_index + i, division.id, home, away)
        for i, (home, away) in enumerate(orient_balanced(remaining))
    ]
