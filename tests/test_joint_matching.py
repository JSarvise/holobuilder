"""`joint_matching.JointMatching`: the dynamic Hungarian matching of socket rows to card groups.

Solved once, it must hold the maximum weight (as `board_plan._max_weight_matching` and a brute
force find it), within the groups' capacities; re-solved with some rows changed, the maximum of
the changed weights, whatever the rows changed (a row emptied, a row given weights it did not
have, several at once), and the original left as it was."""

from __future__ import annotations

import itertools
import random

import pytest

from hbuilder.domain.board_plan import _max_weight_matching
from hbuilder.domain.joint_matching import JointMatching


def _random(rng: random.Random, n: int, m: int, density: float = 0.5):
    weights = [
        [round(rng.random() * 10, 3) if rng.random() < density else 0.0 for _ in range(m)] for _ in range(n)
    ]
    caps = [rng.randint(1, 3) for _ in range(m)]
    return weights, caps


def _best(weights, caps) -> float:
    match = _max_weight_matching(weights, caps)
    return sum(weights[i][j] for i, j in match.items())


def _brute(weights, caps) -> float:
    n, m = len(weights), len(caps)
    best = 0.0
    for choice in itertools.product(range(-1, m), repeat=n):
        if any(sum(1 for c in choice if c == j) > caps[j] for j in range(m)):
            continue
        best = max(best, sum(weights[i][c] for i, c in enumerate(choice) if c >= 0))
    return best


def _check(jm: JointMatching, weights, caps) -> None:
    assignment = jm.assignment()
    for j in range(len(caps)):
        assert sum(1 for c in assignment.values() if c == j) <= caps[j]
    assert all(weights[i][j] > 0 for i, j in assignment.items())
    assert jm.value() == pytest.approx(_best(weights, caps), rel=1e-12, abs=1e-12)


def test_a_solved_matching_holds_the_maximum_weight():
    rng = random.Random(20261114)
    for _ in range(40):
        weights, caps = _random(rng, rng.randint(1, 30), rng.randint(1, 8))
        _check(JointMatching(weights, caps), weights, caps)


def test_tiny_instances_agree_with_a_brute_force():
    rng = random.Random(7)
    for _ in range(60):
        weights, caps = _random(rng, rng.randint(1, 5), rng.randint(1, 3), density=0.7)
        assert JointMatching(weights, caps).value() == pytest.approx(_brute(weights, caps), abs=1e-9)


def test_rows_changed_are_re_solved_to_the_maximum_and_the_original_is_kept():
    rng = random.Random(42)
    for _ in range(60):
        n, m = rng.randint(2, 40), rng.randint(1, 8)
        weights, caps = _random(rng, n, m)
        base = JointMatching(weights, caps)
        before = (list(base.col_of_row), list(base.u), list(base.v))
        rows = rng.sample(range(n), rng.randint(1, min(n, 8)))
        changes = {}
        for r in rows:
            kind = rng.random()
            if kind < 0.25:
                changes[r] = [0.0] * m  # a socket the team's board does not have
            else:
                changes[r] = [round(rng.random() * 15, 3) if rng.random() < 0.6 else 0.0 for _ in range(m)]
        changed = [changes.get(i, row) for i, row in enumerate(weights)]
        _check(base.solve(changes), changed, caps)
        assert (base.col_of_row, base.u, base.v) == before
        # and chained: a second team from the same bench state
        more = {r: [round(rng.random() * 5, 3) for _ in range(m)] for r in rng.sample(range(n), 2)}
        _check(base.solve(more), [more.get(i, row) for i, row in enumerate(weights)], caps)


def test_a_re_solve_is_deterministic():
    rng = random.Random(3)
    weights, caps = _random(rng, 30, 6)
    base = JointMatching(weights, caps)
    changes = {4: [1.0] * 6, 17: [0.0] * 6, 9: [5.0, 0, 0, 2.0, 0, 1.0]}
    a, b = base.solve(changes), base.solve(dict(reversed(list(changes.items()))))
    assert a.col_of_row == b.col_of_row and a.u == b.u and a.v == b.v


def test_only_socket_rows_can_change():
    base = JointMatching([[1.0, 2.0]], [1, 1])
    with pytest.raises(ValueError):
        base.solve({1: [1.0, 1.0]})
