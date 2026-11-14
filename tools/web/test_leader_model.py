"""The joint search's leader model (`joint_search.LeaderModel`) is the same function as a
plain-loop reference model, bit for bit.

The model chooses which leaders a set is scored under, so it steers the whole search: a fit
that differs in the last bit can flip a near-tie, score other teams and serve another
answer. `ReferenceLeaderModel` below is the plain-loop model verbatim; the numpy model must
give the same coefficients (`==`) and the same leader order on every set.
"""

from __future__ import annotations

import itertools
import math
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
for extra in (HERE, HERE / "kernel"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import joint_search  # noqa: E402
from two_stage import _fires  # noqa: E402


class ReferenceLeaderModel:
    """The same model written as plain loops, the reference `joint_search.LeaderModel` matches."""

    def __init__(self, cards, leaders, gate_of, lean_of=None) -> None:
        self.cards = list(cards)
        self.leaders = list(leaders)
        self.gates = [gate_of(l) if gate_of is not None else None for l in self.leaders]
        self.a = [0.0] * len(self.leaders)
        self.b = [0.0] * len(self.leaders)
        self.c = [0.0] * len(self.leaders)
        self.d = [0.0] * len(self.leaders)
        self.lean = [lean_of(l) if lean_of is not None else None for l in self.leaders]
        self._fired: dict = {}

    def features(self, li, s):
        key = (li, s)
        fired = self._fired.get(key)
        if fired is None:
            gate = self.gates[li]
            fired = 1.0 if gate is not None and _fires(gate, self.cards, s) else 0.0
            self._fired[key] = fired
        holds = 1.0 if any(self.cards[i].member == self.leaders[li].member for i in s) else 0.0
        return fired, holds

    def leans(self, li, s):
        weights = self.lean[li]
        if weights is None:
            return 0.0
        if self.gates[li] is not None and not self.features(li, s)[0]:
            return 0.0
        lean = 0.0
        for i in s:
            lean += weights[i]
        return lean

    def predict(self, li, s):
        fired, holds = self.features(li, s)
        return self.a[li] + self.b[li] * fired + self.c[li] * holds + self.d[li] * self.leans(li, s)

    def fit(self, scored, sweeps=6, lam=2.0):
        by_set: dict = {}
        for (li, s), v in scored.items():
            if v > 0:
                by_set.setdefault(s, []).append((li, math.log(v)))
        by_set = {s: rows for s, rows in by_set.items() if len(rows) >= 2}
        if not by_set:
            return
        theta = {s: sum(y for _l, y in rows) / len(rows) for s, rows in by_set.items()}
        n = len(self.leaders)
        for _ in range(sweeps):
            acc = [[0.0] * 9 for _ in range(n)]
            rhs = [[0.0] * 3 for _ in range(n)]
            # the lean's sums: x, fired * x, holds * x, x * x and x * residual
            more = [[0.0] * 5 for _ in range(n)]
            for s, rows in by_set.items():
                for li, y in rows:
                    f, h = self.features(li, s)
                    x = (1.0, f, h)
                    r = y - theta[s]
                    A, B = acc[li], rhs[li]
                    for i in range(3):
                        B[i] += x[i] * r
                        for j in range(3):
                            A[3 * i + j] += x[i] * x[j]
                    lean = self.leans(li, s)
                    for k, term in enumerate((lean, f * lean, h * lean, lean * lean, lean * r)):
                        more[li][k] += term
            prior_b = joint_search._mean([self.b[li] for li in range(n) if self.gates[li] is not None])
            prior_c = joint_search._mean(self.c)
            prior_d = joint_search._mean([self.d[li] for li in range(n) if self.lean[li] is not None])
            for li in range(n):
                A, B = acc[li], rhs[li]
                if A[0] == 0:
                    continue
                if self.lean[li] is not None:
                    n3, n13, n23, n33, b3 = more[li]
                    ridge = joint_search.LEAN_RIDGE
                    sol = joint_search._solve(
                        [
                            [A[0], A[1], A[2], n3, B[0]],
                            [
                                A[1],
                                A[4] + lam,
                                A[5],
                                n13,
                                B[1] + lam * (prior_b if self.gates[li] is not None else 0.0),
                            ],
                            [A[2], A[5], A[8] + lam, n23, B[2] + lam * prior_c],
                            [n3, n13, n23, n33 + ridge, b3 + ridge * prior_d],
                        ]
                    )
                    if sol is not None:
                        self.a[li], self.b[li], self.c[li], self.d[li] = sol
                    continue
                A = A[:]
                B = B[:]
                A[4] += lam
                B[1] += lam * (prior_b if self.gates[li] is not None else 0.0)
                A[8] += lam
                B[2] += lam * prior_c
                sol = joint_search._solve3(A, B)
                if sol is not None:
                    self.a[li], self.b[li], self.c[li] = sol
            for s, rows in by_set.items():
                theta[s] = sum(y - self.predict(li, s) for li, y in rows) / len(rows)
        mean_a = joint_search._mean(self.a)
        self.a = [v - mean_a for v in self.a]

    def ranked(self, s):
        return sorted(range(len(self.leaders)), key=lambda li: -self.predict(li, s))


TYPES = ["Cute", "Cool", "Pure"]
GENERATIONS = ["Myth", "3rd", "holoX", "ReGLOSS", "ID1"]


def _world(seed: int, count: int = 40, holomems: int = 24):
    rng = random.Random(seed)
    cards = [
        SimpleNamespace(
            id=f"card-{i:03d}",
            member=f"Chr{i % holomems}",
            type=rng.choice(TYPES),
            generations=frozenset(rng.sample(GENERATIONS, rng.choice([1, 1, 2]))),
            gate=rng.choice(
                [None, None, ("type", rng.choice(TYPES), 3), ("generation", rng.choice(GENERATIONS), 2)]
            ),
        )
        for i in range(count)
    ]
    legal = [s for s in itertools.combinations(range(count), 5) if len({cards[i].member for i in s}) == 5]
    return rng, cards, legal


def _lean_of(cards, seed: int):
    """Per-card weights for two leaders in three (one of three weight lists each), None for
    the rest: outfits that favour a stat beside outfits that favour none."""
    rng = random.Random(1000 + seed)
    lists = [tuple(rng.uniform(-0.05, 0.05) for _ in cards) for _ in range(3)]
    choice = {id(c): rng.choice([0, 1, 2, None]) for c in cards}

    def lean_of(leader):
        k = choice[id(leader)]
        return None if k is None else lists[k]

    return lean_of


@pytest.mark.parametrize("leaning", [False, True], ids=["no lean", "lean"])
@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_the_numpy_model_is_the_plain_loop_model_bit_for_bit(seed, leaning):
    rng, cards, legal = _world(seed)
    gate_of = lambda c: c.gate  # noqa: E731
    lean_of = _lean_of(cards, seed) if leaning else None
    new = joint_search.LeaderModel(cards, cards, gate_of, lean_of=lean_of)
    ref = ReferenceLeaderModel(cards, cards, gate_of, lean_of=lean_of)
    scored: dict = {}
    for _round in range(5):
        # a round of scores: random sets under random leaders, a few under many leaders
        for s in rng.sample(legal, 120):
            for li in rng.sample(range(len(cards)), rng.choice([1, 2, 3, 8])):
                scored[(li, s)] = rng.uniform(1.0e6, 5.0e6) * (1.25 if rng.random() < 0.3 else 1.0)
        new.fit(scored)
        ref.fit(scored)
        assert new.a == ref.a and new.b == ref.b and new.c == ref.c and new.d == ref.d
        assert any(new.d) == leaning
        for s in rng.sample(legal, 60):
            assert new.ranked(s) == ref.ranked(s)
            for li in range(len(cards)):
                assert new.predict(li, s) == ref.predict(li, s)


def test_a_set_mean_is_cpythons_compensated_sum():
    """`_grouped_pysum` returns `sum(group)` exactly, including where a left fold would round
    differently (the compensated sum of CPython 3.12+)."""
    import numpy as np

    rng = random.Random(7)
    groups = [
        [
            rng.choice([1e16, 1.0, -1e16, 0.1, -0.3, 3.7e-5, rng.uniform(-20, 20)])
            for _ in range(rng.randint(1, 40))
        ]
        for _ in range(300)
    ]
    values = np.array([x for g in groups for x in g])
    lens = np.array([len(g) for g in groups])
    starts = np.concatenate(([0], np.cumsum(lens)[:-1]))
    got = joint_search._grouped_pysum(values, starts, lens)
    assert [float(x) for x in got] == [sum(g) for g in groups]


STATS = ("performance", "technique", "sense")


def _stat_cards(seed: int, count: int = 36):
    """Cards strongest in one of three stats; each card's outfit raises its own strongest stat
    by 80 % of the set's share of it, a third of them behind an easy type gate."""
    rng = random.Random(seed)
    cards = []
    for i in range(count):
        strong = STATS[i % 3]
        stats = {s: rng.uniform(3000, 4000) + (3000 if s == strong else 0) for s in STATS}
        cards.append(
            SimpleNamespace(
                id=f"card-{i:03d}",
                member=f"Chr{i}",
                type=rng.choice(TYPES),
                generations=frozenset(),
                strength=rng.uniform(0.9, 1.1),
                stats=stats,
                strong=strong,
                gate=("type", rng.choice(TYPES), 2) if i % 3 == rng.randrange(3) else None,
            )
        )

    def share(card, stat):
        return card.stats[stat] / sum(card.stats.values())

    def exact(li, s):
        value = 100_000.0
        for i in s:
            value *= cards[i].strength
        leader = cards[li]
        if leader.gate is None or _fires(leader.gate, cards, s):
            total = sum(sum(cards[i].stats.values()) for i in s)
            value *= 1.0 + 0.8 * sum(cards[i].stats[leader.strong] for i in s) / total
        return value

    def lean_of(leader):
        return tuple(0.45 * (share(c, leader.strong) - 1 / 3) for c in cards)

    return rng, cards, exact, lean_of


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_set_is_ranked_first_under_a_leader_whose_outfit_raises_its_strongest_stat(seed):
    # every outfit raises one stat, so a set's best leader is one that raises the stat its
    # members are strong in. Fitted on random sets under random leaders, the model ranks such a
    # leader first on a set of each stat; without the lean it ranks the leaders the same on every
    # set (but for gates), so it cannot be right for all three stats
    rng, cards, exact, lean_of = _stat_cards(seed)
    legal = list(itertools.combinations(range(len(cards)), 5))
    model = joint_search.LeaderModel(cards, cards, lambda c: c.gate, lean_of=lean_of)
    scored = {}
    for s in rng.sample(legal, 400):
        for li in rng.sample(range(len(cards)), 3):
            scored[(li, s)] = exact(li, s)
    model.fit(scored)
    for stat in STATS:
        mine = [i for i, c in enumerate(cards) if c.strong == stat]
        for _ in range(20):
            s = tuple(sorted(rng.sample(mine, 5)))
            ranked = model.ranked(s)
            assert cards[ranked[0]].strong == stat
            assert exact(ranked[0], s) == max(exact(li, s) for li in range(len(cards)))
            assert model.predict(ranked[0], s) == max(model.predict(li, s) for li in range(len(cards)))
