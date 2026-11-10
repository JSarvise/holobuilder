"""The joint search's leader model (`joint_search.LeaderModel`) is the same function as the
plain-loop model it replaced, bit for bit.

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
    """The plain-loop model (the code `joint_search.LeaderModel` replaced), kept verbatim."""

    def __init__(self, cards, leaders, gate_of) -> None:
        self.cards = list(cards)
        self.leaders = list(leaders)
        self.gates = [gate_of(l) if gate_of is not None else None for l in self.leaders]
        self.a = [0.0] * len(self.leaders)
        self.b = [0.0] * len(self.leaders)
        self.c = [0.0] * len(self.leaders)
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

    def predict(self, li, s):
        fired, holds = self.features(li, s)
        return self.a[li] + self.b[li] * fired + self.c[li] * holds

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
            prior_b = joint_search._mean([self.b[li] for li in range(n) if self.gates[li] is not None])
            prior_c = joint_search._mean(self.c)
            for li in range(n):
                A, B = acc[li], rhs[li]
                if A[0] == 0:
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
    cards = [SimpleNamespace(
        id=f"card-{i:03d}", member=f"Chr{i % holomems}", type=rng.choice(TYPES),
        generations=frozenset(rng.sample(GENERATIONS, rng.choice([1, 1, 2]))),
        gate=rng.choice([None, None, ("type", rng.choice(TYPES), 3),
                         ("generation", rng.choice(GENERATIONS), 2)]))
        for i in range(count)]
    legal = [s for s in itertools.combinations(range(count), 5)
             if len({cards[i].member for i in s}) == 5]
    return rng, cards, legal


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_the_numpy_model_is_the_plain_loop_model_bit_for_bit(seed):
    rng, cards, legal = _world(seed)
    gate_of = lambda c: c.gate  # noqa: E731
    new = joint_search.LeaderModel(cards, cards, gate_of)
    ref = ReferenceLeaderModel(cards, cards, gate_of)
    scored: dict = {}
    for _round in range(5):
        # a round of scores: random sets under random leaders, a few under many leaders
        for s in rng.sample(legal, 120):
            for li in rng.sample(range(len(cards)), rng.choice([1, 2, 3, 8])):
                scored[(li, s)] = rng.uniform(1.0e6, 5.0e6) * (1.25 if rng.random() < 0.3 else 1.0)
        new.fit(scored)
        ref.fit(scored)
        assert new.a == ref.a and new.b == ref.b and new.c == ref.c
        for s in rng.sample(legal, 60):
            assert new.ranked(s) == ref.ranked(s)
            for li in range(len(cards)):
                assert new.predict(li, s) == ref.predict(li, s)


def test_a_set_mean_is_cpythons_compensated_sum():
    """`_grouped_pysum` returns `sum(group)` exactly, including where a left fold would round
    differently (the compensated sum of CPython 3.12+)."""
    import numpy as np

    rng = random.Random(7)
    groups = [[rng.choice([1e16, 1.0, -1e16, 0.1, -0.3, 3.7e-5, rng.uniform(-20, 20)])
               for _ in range(rng.randint(1, 40))] for _ in range(300)]
    values = np.array([x for g in groups for x in g])
    lens = np.array([len(g) for g in groups])
    starts = np.concatenate(([0], np.cumsum(lens)[:-1]))
    got = joint_search._grouped_pysum(values, starts, lens)
    assert [float(x) for x in got] == [sum(g) for g in groups]
