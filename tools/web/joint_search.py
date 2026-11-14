"""ONE search over (leader, five members) — the leader chosen inside the search.

A member search under a bare proxy plus a triage of every leader (`two_stage.search_song`)
needs ~179k exact evaluations (50 s) for one song on a 114-card account; the community tool
holodori.best answers the same question with ~3,700 by picking a leader FOR each member set
with a cheap proxy and evaluating only that pair. This does the same with two LEARNED models
and no hand-written proxy, so a new card or song needs nothing:

* the SET screen (`kernel/surrogate.py`) learns `log V(S)`, the value of a member set S
  under its best leader found so far, and ranks every legal set in C;
* the LEADER model learns, per leader L, `log score(L, S) = theta_S + a_L +
  b_L * fires(gate_L, S) + c_L * holds(L, S) + d_L * leans(L, S)` (theta_S a per-set
  effect, so only the leaders' RELATIVE order is learned; `leans` is how far the set's
  members lean towards the stat the leader's outfit raises, see `LeaderModel`). It is too
  coarse to rank TEAMS, so it only chooses which leaders a set is exact-scored with.

Rounds: the screen proposes `step` unseen sets; each is exact-scored under its `k` best
leaders by the model (plus the `always_k` best of the `always` leaders, the song's singers);
both models refit on every exact score. Stops when the best `stable_top` TEAMS have not
changed for `stable_rounds` rounds. A POLISH then exact-scores the best `polish` sets under
EVERY leader, so the reported top sets carry their true best leader, and `neighbours`
(one-card swaps of the best teams, under the same leader) hedge against what the screen
cannot see.

Every score returned is the exact leaf's; the models only choose what to score.
No certificate: "best found", with how much was scored in `stats`.
"""

from __future__ import annotations

import heapq
import itertools
import math
import random
import time
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np
import surrogate
import two_stage
from two_stage import _fires

# added to a required card's screen weight (log units) after each fit: far above
# any fitted weight, so every set holding all required cards outranks every other
REQUIRE_BIAS = 1_000.0
# a gated leader whose repaired team comes this close to the best is climbed from too
REPAIR_MARGIN = 0.03
REPAIR_CLIMB = 3
# LEADER REGIONS (when the caller passes `tilt_of`, in place of the gate repair above). A
# leader's best sets lean towards the members strong in the stat its outfit raises, and hold
# the cards its gate needs; the set screen is fitted on the values the leaders scored so far
# give each set, so a region no well-scored leader shares stays where the screen never looks.
# A region = the outfit's stat tilt and its gate, when the gate is HARD (the round-0 random
# sets fire it less often than this); a soft gate fires on most strong sets anyway
REGION_HARD_GATE = 0.25
# each region's REGION_SEEDS best sets by the screen plus its tilt, its gate forced (a beam
# REGION_BEAM wide), are exact-scored under the region's leader the model ranks best on them
REGION_SEEDS = 4
REGION_BEAM = 8
# every region whose best team comes this close to the best team is climbed from, with no
# limit on how many: a seed sits up to ~2.5 % below what its region's climb reaches
# (measured on a maxed account, every EXPERT chart and levels 25-30)
REGION_MARGIN = 0.025
# ... with two-card moves too: the best this many one-swaps of each climbed region's team,
# paired (two members that pay only together, e.g. Fubuki and Matsuri)
REGION_PAIR_MOVES = 10

# LEADER CLASSES (when the caller passes `tilt_of`). The leaders whose outfits lean the same
# way (one class per stat an outfit raises, one for the outfits that favour no stat) value
# the member sets alike, and each class has its own best sets. The search follows the best
# teams found, so a class whose best set is not yet among them would be left where it stood:
# its region is then reached only by chance, and a search with the leader pinned to one of
# its cards finds a better team than the unpinned one. So every class is searched as a search
# pinned to it would be:
# * the set screen is fitted on each set's value NET of its leaders' effect (the leader
#   model's per-set term), so the scores under every leader teach it the same thing, and it
#   learns each card's class as a kind;
# * a card is worth more to one class than to another beyond what the tilt says (a member
#   whose own skill raises performance pays most under an outfit that raises performance
#   again), so each class has its OWN card weights on top: a ridge fit (CLASS_RIDGE) of what
#   the screen leaves unexplained of the scores under that class's leaders;
# * every round proposes the same number of unseen sets for EACH class (the round's `step`
#   and its `diverse` share, split between the classes), the screen's best once the class's
#   tilt (at its mean lean coefficient) and its own card weights are added, each exact-scored
#   under the leaders the model ranks best for it;
# * the best CLASS_NEIGHBOURS teams of every class are climbed in every round and its best in
#   the final climb (their one-card swaps under their leader, its set under every leader of
#   the class), however far below the best team the class stands;
# * the rounds go on until no class within CLASS_MARGIN of the best team has changed its best
#   for `stable_rounds` rounds.
CLASS_RIDGE = 4.0
CLASS_SWEEPS = 4
CLASS_NEIGHBOURS = 2
CLASS_MARGIN = 0.03

# (leader index, ascending member index 5-tuple)
Pair = tuple[int, tuple[int, ...]]
ScorePairs = Callable[[list[Pair]], Sequence[float]]


# The ridge on a leader's lean coefficient `d`, in the lean's own units: a set's lean moves
# ~0.05 between random sets, so its squares over the ~20 sets a leader is first scored on add
# up to ~0.04, and a ridge of that size pulls a leader's own estimate about a third of the way
# to the mean of the leaning leaders
LEAN_RIDGE = 0.02


class LeaderModel:
    """Per-leader `a_L + b_L * fired + c_L * holds + d_L * leans` with a per-set effect,
    fitted by alternating least squares. `b`, `c` are shrunk towards the mean
    of the leaders that share their gate kind, so a leader with few gate-firing
    samples borrows from the others instead of guessing.

    `leans(L, S)`: an outfit that raises ONE stat is worth more on the sets strong in that
    stat (+80 % performance adds ~10 % more to a performance set than to a technique one),
    so the best leader of a set depends on what its members are made of. `lean_of(leader)`
    gives the per-card weights of that (pool order; None for an outfit that favours no
    stat); a set's lean is their sum over its members while the outfit applies (no gate, or
    the gate fires). `d` is shrunk towards the mean of the leaning leaders (`LEAN_RIDGE`).
    Without it the model ranks the leaders the same on every set but for gates, and the
    sets of the stat its best-ranked leaders do not raise are never scored under a leader
    that suits them: the set screen then learns them as weak and their region is not
    searched.

    The fit and the ranking run on numpy arrays with the arithmetic of the plain
    loops they replace, in the same order, so every coefficient is the same float:
    per-leader sums are `bincount`s (an in-order left fold, as a loop's `+=`), and a
    set's mean is CPython's own `sum()` over its rows (`_grouped_pysum`). The leader
    choice steers which teams the search scores, so a last-bit difference here would
    change the answer."""

    def __init__(
        self,
        cards: Sequence,
        leaders: Sequence,
        gate_of: Callable[[Any], tuple | None] | None,
        lean_of: Callable[[Any], Sequence[float] | None] | None = None,
    ) -> None:
        self.cards = list(cards)
        self.leaders = list(leaders)
        self.gates = [gate_of(l) if gate_of is not None else None for l in self.leaders]
        self.a = [0.0] * len(self.leaders)
        self.b = [0.0] * len(self.leaders)
        self.c = [0.0] * len(self.leaders)
        self.d = [0.0] * len(self.leaders)
        self._fired: dict[tuple[int, tuple[int, ...]], float] = {}
        leans = [lean_of(l) if lean_of is not None else None for l in self.leaders]
        self.lean: list[tuple[float, ...] | None] = [
            tuple(float(v) for v in t) if t is not None else None for t in leans
        ]
        self._leans = any(t is not None for t in self.lean)
        self._lean = np.array(
            [t if t is not None else [0.0] * len(self.cards) for t in self.lean], dtype=float
        ).reshape(len(self.leaders), len(self.cards))
        # the distinct gates, and each leader's (-1: none): a set's gates fire once each
        self._gate_list = list(dict.fromkeys(g for g in self.gates if g is not None))
        index = {g: k for k, g in enumerate(self._gate_list)}
        self._gate_of = np.array([index[g] if g is not None else -1 for g in self.gates], dtype=np.intp)
        names = {
            m: k
            for k, m in enumerate(
                dict.fromkeys([c.member for c in self.cards] + [l.member for l in self.leaders])
            )
        }
        self._card_name = [names[c.member] for c in self.cards]
        self._leader_name = np.array([names[l.member] for l in self.leaders], dtype=np.intp)
        self._names = len(names)
        self._arrays()

    def _arrays(self) -> None:
        self._an = np.array(self.a, dtype=float)
        self._bn = np.array(self.b, dtype=float)
        self._cn = np.array(self.c, dtype=float)
        self._dn = np.array(self.d, dtype=float)

    def features(self, li: int, s: tuple[int, ...]) -> tuple[float, float]:
        key = (li, s)
        fired = self._fired.get(key)
        if fired is None:
            gate = self.gates[li]
            fired = 1.0 if gate is not None and _fires(gate, self.cards, s) else 0.0
            self._fired[key] = fired
        holds = 1.0 if any(self.cards[i].member == self.leaders[li].member for i in s) else 0.0
        return fired, holds

    def leans(self, li: int, s: tuple[int, ...]) -> float:
        """How far `s` leans towards leader `li`'s outfit: the leader's per-card weights
        added over the members in `s`'s order, 0.0 for a leader with none or whose gate
        `s` does not fire."""
        weights = self.lean[li]
        if weights is None:
            return 0.0
        if self.gates[li] is not None and not self.features(li, s)[0]:
            return 0.0
        lean = 0.0
        for i in s:
            lean += weights[i]
        return lean

    def predict(self, li: int, s: tuple[int, ...]) -> float:
        fired, holds = self.features(li, s)
        if not self._leans:
            return self.a[li] + self.b[li] * fired + self.c[li] * holds
        return self.a[li] + self.b[li] * fired + self.c[li] * holds + self.d[li] * self.leans(li, s)

    def fit(self, scored: dict[Pair, float], sweeps: int = 6, lam: float = 2.0) -> None:
        by_set: dict[tuple[int, ...], list[tuple[int, float]]] = {}
        for (li, s), v in scored.items():
            if v > 0:
                by_set.setdefault(s, []).append((li, math.log(v)))
        # only sets seen under 2+ leaders say anything about relative leaders
        by_set = {s: rows for s, rows in by_set.items() if len(rows) >= 2}
        if not by_set:
            return
        rows_li, rows_y, rows_f, rows_h, rows_x, lens = [], [], [], [], [], []
        for s, rows in by_set.items():
            lens.append(len(rows))
            for li, y in rows:
                f, h = self.features(li, s)
                rows_li.append(li)
                rows_y.append(y)
                rows_f.append(f)
                rows_h.append(h)
                rows_x.append(self.leans(li, s) if self._leans else 0.0)
        li_ = np.array(rows_li, dtype=np.intp)
        y = np.array(rows_y, dtype=float)  # type: ignore[assignment]
        f = np.array(rows_f, dtype=float)  # type: ignore[assignment]
        h = np.array(rows_h, dtype=float)  # type: ignore[assignment]
        x = np.array(rows_x, dtype=float)
        lens_ = np.array(lens, dtype=np.intp)
        starts = np.concatenate(([0], np.cumsum(lens_)[:-1])).astype(np.intp)
        group = np.repeat(np.arange(len(lens_)), lens_)
        theta = _grouped_pysum(y, starts, lens_) / lens_
        n = len(self.leaders)
        # the normal equations' left sides count rows, the same every sweep (x is 0 or 1)
        cnt = np.bincount(li_, minlength=n).astype(float)
        sf = np.bincount(li_, weights=f, minlength=n)
        sh = np.bincount(li_, weights=h, minlength=n)
        sfh = np.bincount(li_, weights=f * h, minlength=n)
        sx = np.bincount(li_, weights=x, minlength=n)
        sfx = np.bincount(li_, weights=f * x, minlength=n)
        shx = np.bincount(li_, weights=h * x, minlength=n)
        sxx = np.bincount(li_, weights=x * x, minlength=n)
        gated = [g is not None for g in self.gates]
        leaning = [t is not None for t in self.lean]
        for _ in range(sweeps):
            # leader parameters given theta: 3x3 normal equations per leader, ridge on b, c
            # (4x4 with the lean, ridge on d too, for a leader whose outfit leans)
            r = y - theta[group]
            b0 = np.bincount(li_, weights=r, minlength=n)
            b1 = np.bincount(li_, weights=f * r, minlength=n)
            b2 = np.bincount(li_, weights=h * r, minlength=n)
            b3 = np.bincount(li_, weights=x * r, minlength=n)
            prior_b = _mean([self.b[li] for li in range(n) if gated[li]])
            prior_c = _mean(self.c)
            prior_d = _mean([self.d[li] for li in range(n) if leaning[li]])
            for li in range(n):
                if cnt[li] == 0:
                    continue
                n0, n1, n2, n12 = float(cnt[li]), float(sf[li]), float(sh[li]), float(sfh[li])
                if leaning[li]:
                    n3, n13, n23, n33 = float(sx[li]), float(sfx[li]), float(shx[li]), float(sxx[li])
                    sol4 = _solve(
                        [
                            [n0, n1, n2, n3, float(b0[li])],
                            [n1, n1 + lam, n12, n13, float(b1[li]) + lam * (prior_b if gated[li] else 0.0)],
                            [n2, n12, n2 + lam, n23, float(b2[li]) + lam * prior_c],
                            [n3, n13, n23, n33 + LEAN_RIDGE, float(b3[li]) + LEAN_RIDGE * prior_d],
                        ]
                    )
                    if sol4 is not None:
                        self.a[li], self.b[li], self.c[li], self.d[li] = sol4
                    continue
                A = [n0, n1, n2, n1, n1, n12, n2, n12, n2]
                B = [float(b0[li]), float(b1[li]), float(b2[li])]
                A[4] += lam
                B[1] += lam * (prior_b if gated[li] else 0.0)
                A[8] += lam
                B[2] += lam * prior_c
                sol = _solve3(A, B)
                if sol is not None:
                    self.a[li], self.b[li], self.c[li] = sol
            # theta given the leaders
            self._arrays()
            pred = self._an[li_] + self._bn[li_] * f + self._cn[li_] * h
            if self._leans:
                pred = pred + self._dn[li_] * x
            theta = _grouped_pysum(y - pred, starts, lens_) / lens_
        # identifiability: centre a on its mean (only differences matter)
        mean_a = _mean(self.a)
        self.a = [v - mean_a for v in self.a]
        self._arrays()

    def ranked(self, s: tuple[int, ...]) -> list[int]:
        # one entry per distinct gate, then a 0.0 that a leader with no gate (-1) reads
        fires = np.array([1.0 if _fires(g, self.cards, s) else 0.0 for g in self._gate_list] + [0.0])
        f = fires[self._gate_of]
        present = np.zeros(self._names, dtype=float)
        present[[self._card_name[i] for i in s]] = 1.0
        h = present[self._leader_name]
        pred = self._an + self._bn * f + self._cn * h
        if self._leans:
            # each leader's weights added over the members in the set's order (`leans`),
            # kept where the outfit applies: no gate, or its gate fires
            lean = np.zeros(len(self.leaders), dtype=float)
            for i in s:
                lean = lean + self._lean[:, i]
            lean = np.where((self._gate_of < 0) | (f > 0), lean, 0.0)
            pred = pred + self._dn * lean
        return np.argsort(-pred, kind="stable").tolist()


def _grouped_pysum(values, starts, lens):
    """CPython's `sum()` of each group of `values` (group k = `values[starts[k]:starts[k] +
    lens[k]]`, every group non-empty), vectorised across the groups: the start `0 + x0`,
    then Neumaier's compensated steps in order, the compensation added at the end when it
    is finite and non-zero (bltinmodule.c, 3.12+). The same float as `sum(group)`."""
    total = values[starts] + 0.0
    comp = np.zeros_like(total)
    for k in range(1, int(lens.max()) if len(lens) else 0):
        live = np.nonzero(lens > k)[0]
        x = values[starts[live] + k]
        t0 = total[live]
        t = t0 + x
        comp[live] += np.where(np.abs(t0) >= np.abs(x), (t0 - t) + x, (x - t) + t0)
        total[live] = t
    return np.where((comp != 0) & np.isfinite(comp), total + comp, total)


def _rank_key(row):
    return (-row[0], row[1], row[2])


def _best(scored: dict, ok, n: int) -> list[tuple[float, int, tuple[int, ...]]]:
    """The best `n` allowed pairs of `scored` as (value, leader index, set), best first, ties
    by leader then set: `sorted(...)[:n]`, which `heapq.nsmallest` is documented to equal,
    without sorting everything a search scored each time it asks for its best few."""
    rows = ((v, li, s) for (li, s), v in scored.items() if ok(li, s))
    if n >= len(scored):
        return sorted(rows, key=_rank_key)
    return heapq.nsmallest(n, rows, key=_rank_key)


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _solve(rows):
    """The solution of a small linear system given as its augmented rows `[A | B]`, by
    Gauss-Jordan elimination with partial pivoting (as `_solve3`); None when singular."""
    m = [list(row) for row in rows]
    size = len(m)
    for c in range(size):
        p = max(range(c, size), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(size):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [a - f * b for a, b in zip(m[r], m[c])]
    return [m[i][size] / m[i][i] for i in range(size)]


def _solve3(A, B):
    m = [[A[0], A[1], A[2], B[0]], [A[3], A[4], A[5], B[1]], [A[6], A[7], A[8], B[2]]]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(3):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [a - f * b for a, b in zip(m[r], m[c])]
    return [m[i][3] / m[i][i] for i in range(3)]


def _gate_sets(cards, gate, count: int, rng: random.Random, holomems) -> list[tuple[int, ...]]:
    """Up to `count` random legal sets that fire `gate` — so a gated leader's
    gate term is learned even when random sets almost never fire it."""
    kind, target, need = gate
    if kind == "type":
        match = [i for i, c in enumerate(cards) if c.type == target]
    elif kind == "generation":
        match = [i for i, c in enumerate(cards) if target in c.generations]
    else:
        return []
    out: list[tuple[int, ...]]
    out, seen = [], set()
    for _ in range(count * 30):
        if len(out) >= count:
            break
        if len({holomems[i] for i in match}) < need:
            break
        picked: list[int] = []
        names: set[str] = set()
        for i in rng.sample(match, len(match)):
            if holomems[i] not in names:
                picked.append(i)
                names.add(holomems[i])
            if len(picked) == need:
                break
        rest = [i for i in range(len(cards)) if holomems[i] not in names]
        rng.shuffle(rest)
        for i in rest:
            if len(picked) == 5:
                break
            if holomems[i] not in names:
                picked.append(i)
                names.add(holomems[i])
        s = tuple(sorted(picked))
        if len(s) == 5 and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def search(
    cards: Sequence,
    leaders: Sequence,
    score_pairs: ScorePairs,
    *,
    gate_of: Callable[[Any], tuple | None] | None = None,
    always: Sequence[int] = (),
    always_k: int = 2,
    k: int = 2,
    initial_sets: int = 800,
    leader_samples: int = 10,
    gate_samples: int = 6,
    step: int = 600,
    max_rounds: int = 15,
    stable_top: int = 10,
    stable_rounds: int = 2,
    polish: int = 20,
    neighbours: int = 5,
    climb_rounds: int = 6,
    gate_repair: int = 10,
    diverse: int = 20,
    diverse_pool: int = 4,
    diverse_overlap: int = 2,
    diverse_anchors: int = 3,
    seeds: Sequence[Pair] = (),
    require: Sequence[int] = (),
    allowed: Callable[[int, tuple[int, ...]], bool] | None = None,
    seed: int = 20261016,
    deadline: float | None = None,
    on_round: Callable[[dict], None] | None = None,
    tilt_of: Callable[[object], Sequence[float] | None] | None = None,
) -> dict:
    """Best (leader, set) teams. `score_pairs([(leader index, set), ...])` ->
    exact scores, same order. `always`: leader indices of which the model's best
    `always_k` are scored with every proposed set (the song's singers). `seeds`: pairs to score first (the last
    answer, when inputs moved).

    Filters: `require` (pool indices every reported set must contain) pushes
    the screen onto those sets — a large bias on their card weights after each
    fit, so the scan ranks the sets holding all of them first, in the model's
    order — and seeds the random sample with them; `allowed(leader index, set)`
    rejects a pair from the answer (e.g. the leader's holomem among the
    members). Pairs outside the filters may still be scored while learning;
    only allowed ones are reported, ranked and used for the stop rule. To pin
    the leader, pass only the allowed leaders.

    `tilt_of(leader)`: the per-card log weights (pool order) its outfit adds to
    a set's value, or None. With it the leader model ranks each set's leaders by
    how the set leans too (`LeaderModel`), and every leader REGION is seeded and
    the ones near the best climbed (see REGION_*), in place of the gate repair.

    Returns `{"ranking": [(score, leader index, set)] best first (every scored
    ALLOWED pair), "rounds": [...], "stats": {...}}`."""
    cards = list(cards)
    leaders = list(leaders)
    holomems = [c.member for c in cards]
    rng = random.Random(seed)
    # the leader classes: a leader's class is its tilt (None: an outfit that favours no stat),
    # a card's is the class whose tilt weighs it most
    class_of: list = []
    groups: list[int | None] | None = None
    if tilt_of is not None:
        for leader in leaders:
            tilt = tilt_of(leader)
            class_of.append(tuple(tilt) if tilt is not None else None)
        tilts = list(dict.fromkeys(c for c in class_of if c is not None))
        if tilts:
            groups = [max(range(len(tilts)), key=lambda k: (tilts[k][i], -k)) for i in range(len(cards))]
    class_members: dict = {}
    for li, key in enumerate(class_of):
        class_members.setdefault(key, []).append(li)
    screen = surrogate.Screen(cards, groups=groups)
    model = LeaderModel(cards, leaders, gate_of, lean_of=tilt_of)
    scored: dict[Pair, float] = {}
    best_of_set: dict[tuple[int, ...], float] = {}
    # each scored set's leaders, and the sets each class has scored (class tracks)
    leaders_of_set: dict[tuple[int, ...], list[int]] = {}
    seen_of_class: dict = {key: set() for key in class_members}
    started = time.perf_counter()
    rounds: list[dict] = []
    always = list(dict.fromkeys(always))
    always_set = set(always)
    require = tuple(sorted(set(require)))
    if len({holomems[i] for i in require}) != len(require) or len(require) > 5:
        raise ValueError("the required cards must be at most five different holomems")

    def ok(li: int, s: tuple[int, ...]) -> bool:
        if require and not all(i in s for i in require):
            return False
        return allowed is None or allowed(li, s)

    def take(pairs: list[Pair]) -> float:
        t0 = time.perf_counter()
        # grouped by leader: a worker that keeps its leader between teams is ~1.9x
        # faster (e.g. 2,309 -> 4,319 teams/s on 16 workers)
        fresh = sorted(dict.fromkeys(p for p in pairs if p not in scored), key=lambda p: p[0])
        if fresh:
            values = list(score_pairs(fresh))
            if len(values) != len(fresh):
                raise ValueError(f"the scorer returned {len(values)} scores for {len(fresh)} pairs")
            for p, v in zip(fresh, values):
                v = float(v)
                if not math.isfinite(v):
                    raise ValueError(f"the scorer returned {v!r} for {p}")
                scored[p] = v
                if v > best_of_set.get(p[1], float("-inf")):
                    best_of_set[p[1]] = v
                if class_of:
                    leaders_of_set.setdefault(p[1], []).append(p[0])
                    seen_of_class[class_of[p[0]]].add(p[1])
        return time.perf_counter() - t0

    def top(n: int) -> list[tuple[float, int, tuple[int, ...]]]:
        return _best(scored, ok, n)

    def leaders_for(s: tuple[int, ...]) -> list[int]:
        ranked = model.ranked(s)
        # the `always` leaders (the song's singers) the model ranks best FOR THIS SET,
        # `always_k` of them, on top of the model's own `k`: every one of them on every
        # set costs ~4.9x the search on a song with 19 singer cards (e.g. m0525:
        # 65,796 pairs / 193.6 s against 13,597 / 39.6 s, the same top 20) while a solo
        # singer has 1-3 cards; dropping them altogether can lose a singer-led best team
        sing = [li for li in ranked if li in always_set][:always_k]
        return list(dict.fromkeys(ranked[:k] + sing))

    def class_best(count: int = 1) -> list[tuple[float, int, tuple[int, ...]]]:
        """The best `count` allowed teams of each leader class, best first (see LEADER
        CLASSES)."""
        own: dict = {key: [] for key in class_members}
        for (li, s), v in scored.items():
            rows = own[class_of[li]]
            row = (-v, li, s)
            if (len(rows) < count or row < rows[-1]) and ok(li, s):
                rows.append(row)
                rows.sort()
                del rows[count:]
        return [(-nv, li, s) for nv, li, s in sorted(row for rows in own.values() for row in rows)]

    def net_values() -> tuple[list, list]:
        """Every scored set and its log value net of its leaders' effect: the mean, over the
        leaders it was scored under, of `log score - (the leader model's leader terms)`."""
        sets, values = [], []
        for s, lis in leaders_of_set.items():
            net = [math.log(scored[(li, s)]) - model.predict(li, s) for li in lis if scored[(li, s)] > 0]
            if net:
                sets.append(s)
                values.append(sum(net) / len(net))
        return sets, values

    def class_weights() -> dict:
        """Each class's own card weights: a ridge fit, by coordinate descent in card order,
        of `log score - leader terms - the screen's prediction` over the pairs scored under
        the class's leaders."""
        sets = list(leaders_of_set)
        row = {s: k for k, s in enumerate(sets)}
        predicted = screen.predict_many(sets)
        out: dict = {}
        for key, members in class_members.items():
            mine = set(members)
            rows_s, rows_r = [], []
            for s, lis in leaders_of_set.items():
                for li in lis:
                    if li in mine and scored[(li, s)] > 0:
                        rows_s.append(s)
                        rows_r.append(math.log(scored[(li, s)]) - model.predict(li, s) - predicted[row[s]])
            weights = np.zeros(len(cards))
            if rows_s:
                res = np.array(rows_r, dtype=float)
                holds = np.array(rows_s, dtype=np.intp)
                where = [np.nonzero((holds == i).any(axis=1))[0] for i in range(len(cards))]
                for _ in range(CLASS_SWEEPS):
                    for i, at in enumerate(where):
                        if len(at):
                            new = (float(res[at].sum()) + len(at) * weights[i]) / (len(at) + CLASS_RIDGE)
                            res[at] -= new - weights[i]
                            weights[i] = new
            out[key] = weights
        return out

    def class_proposals() -> tuple[list[tuple[int, ...]], int]:
        """The round's unseen sets, as many for each class, the classes in turn so a set two
        classes propose is listed once; and how many legal sets there are."""
        out: dict = {}
        legal_sets = 0
        own = class_weights()
        each = max(1, step * (100 + diverse) // (100 * len(class_members)))
        for key, members in class_members.items():
            bias = [float(w) for w in own[key]]
            if key is not None:
                lean = _mean([model.d[li] for li in members])
                bias = [b + lean * w for b, w in zip(bias, key)]
            got, _pred, legal_sets = screen.best(each, seen_of_class[key], card_bias=bias)
            out.update(dict.fromkeys(got))
        return list(out), legal_sets

    def take_class_climb(teams) -> float:
        """One step of each class's climb: its best team's one-card swaps under its leader,
        and its set under every leader of the class."""
        if not teams:
            return 0.0
        spent = take([(lj, s) for _v, li, s in teams for lj in class_members[class_of[li]] if ok(lj, s)])
        return spent + _take_neighbours(teams, holomems, take, scored, ok)

    # --- round 0: a random sample, every leader seen on `leader_samples` sets
    random_sets = two_stage.random_legal_sets(holomems, initial_sets, rng)
    if require:
        # half the sample holds the required cards, so the screen sees that region
        random_sets = random_sets[: initial_sets // 2] + _sets_with(require, holomems, initial_sets // 2, rng)
    first_sets = random_sets
    pairs: list[Pair] = list(seeds)
    for li in range(len(leaders)):
        for s in rng.sample(random_sets, min(leader_samples, len(random_sets))):
            pairs.append((li, s))
        gate = model.gates[li]
        if gate is not None:
            for s in _gate_sets(cards, gate, gate_samples, rng, holomems):
                pairs.append((li, s))
                # a gate set is also seen by a few other leaders, for theta
                for other in rng.sample(range(len(leaders)), min(2, len(leaders))):
                    pairs.append((other, s))
    # every random set under at least two leaders (theta needs 2+)
    for s in first_sets:
        for li in rng.sample(range(len(leaders)), min(2, len(leaders))):
            pairs.append((li, s))
    exact_seconds = take(pairs)
    t0 = time.perf_counter()
    model.fit(scored)
    fit_seconds = time.perf_counter() - t0
    # round 0's sets, now under their predicted best leaders too
    exact_seconds += take([(li, s) for s in list(best_of_set) for li in leaders_for(s)])
    first_best = top(1)
    record = {
        "round": 0,
        "pairs": len(scored),
        "sets": len(best_of_set),
        "best": first_best[0][0] if first_best else None,
        "exact_seconds": exact_seconds,
        "fit_seconds": fit_seconds,
        "scan_seconds": 0.0,
    }
    rounds.append(record)
    if on_round:
        on_round(record)
    leaders_top = [(li, s) for _v, li, s in top(stable_top)]
    unchanged = 0
    stop = "max_rounds"
    legal = None
    for number in range(1, max_rounds + 1):
        if deadline is not None and time.perf_counter() >= deadline:
            stop = "deadline"
            break
        t0 = time.perf_counter()
        if class_of:
            model.fit(scored)
            screen.fit(*net_values())
        else:
            sets = [s for s, v in best_of_set.items() if v > 0]
            screen.fit(sets, [math.log(best_of_set[s]) for s in sets])
        for i in require:
            screen._card[i] += REQUIRE_BIAS
        if not class_of:
            model.fit(scored)
        fit_seconds = time.perf_counter() - t0
        t0 = time.perf_counter()
        if class_of:
            proposed, legal = class_proposals()
        else:
            proposed, _pred, legal = screen.best(step * (1 + diverse_pool), best_of_set.keys())
        scan_seconds = time.perf_counter() - t0
        if not proposed:
            stop = "exhausted"
            break
        # DIVERSITY: the screen's next best sets cluster around the best found so far,
        # and a search that settles in one region never sees another four members
        # away (seeds can settle on two near-equal sets four members apart, each never
        # scoring the other). So part of each round goes to the best-predicted sets
        # sharing at most `diverse_overlap` members with each of the current best sets
        # (a cheap build-around net).
        head, tail = (proposed, []) if class_of else (proposed[:step], proposed[step:])
        if diverse and tail:
            anchors = [set(s) for s in dict.fromkeys(s for _v, _li, s in top(3 * diverse_anchors))][
                :diverse_anchors
            ]
            far = [s for s in tail if all(len(a & set(s)) <= diverse_overlap for a in anchors)]
            head += far[: max(1, step * diverse // 100)]
        proposed = head
        exact_seconds = take(
            [(li, s) for s in proposed for li in leaders_for(s) if allowed is None or allowed(li, s)]
        )
        if neighbours:
            exact_seconds += _take_neighbours(top(neighbours), holomems, take, scored, ok)
            if class_of:
                exact_seconds += take_class_climb(class_best(CLASS_NEIGHBOURS))
        now = [(li, s) for _v, li, s in top(stable_top)]
        if class_of:
            # ... and the best team of every class near the best: a class still improving
            # keeps the rounds going
            floor = (1.0 - CLASS_MARGIN) * (top(1) or [(0.0,)])[0][0]  # type: ignore[list-item]
            now += [(li, s) for v, li, s in class_best() if v >= floor]
        changed = now != leaders_top
        leaders_top = now
        unchanged = 0 if changed else unchanged + 1
        record = {
            "round": number,
            "pairs": len(scored),
            "sets": len(best_of_set),
            "best": (top(1) or [(None,)])[0][0],  # type: ignore[list-item]
            "exact_seconds": exact_seconds,
            "fit_seconds": fit_seconds,
            "scan_seconds": scan_seconds,
            "top_changed": changed,
        }
        rounds.append(record)
        if on_round:
            on_round(record)
        if unchanged >= stable_rounds:
            stop = "stable"
            break
    # --- polish: the best sets under EVERY leader
    t0 = time.perf_counter()
    best_sets = list(dict.fromkeys(s for _v, _li, s in top(10 * polish)))[:polish]
    polish_seconds = take([(li, s) for s in best_sets for li in range(len(leaders)) if ok(li, s)])
    # GATE REPAIR: a gated leader (an outfit that needs 2+ Gen 3, 3+ Pure ...) is worth little
    # on a set that misses its gate, so the model learns it as weak and never tries it where
    # it would win, and no one-card swap reaches it (the gate needs two cards at once). Each
    # of the best sets is also scored under every leader whose gate it misses, its weakest
    # members (the screen's card weights) swapped for the strongest cards that satisfy the
    # gate: a gated leader's best team, once her gate is met, can be the true #1
    weight = [float(screen._card[i]) for i in range(len(cards))]  # zeros before any fit
    repaired_sets: set = set()
    repaired_best: dict = {}

    def repair_gates(sets) -> float:
        # one repair per (set, leader); then, for the leaders whose repair came within
        # REPAIR_MARGIN of the best team, every choice of WHICH members make way on the
        # best three sets (the screen's weights can pick the wrong one, one swap short)
        pairs, fresh = [], [s for s in sets if s not in repaired_sets]
        for s in fresh:
            repaired_sets.add(s)
            for li, gate in enumerate(model.gates):
                if gate is None or _fires(gate, cards, s):
                    continue
                fixed = _repair(s, gate, cards, holomems, weight)
                if fixed is not None and ok(li, fixed):
                    pairs.append((li, fixed))
        spent = take(pairs)
        for li, fs in pairs:
            v = scored.get((li, fs))
            if v is not None and v > repaired_best.get(li, (float("-inf"),))[0]:
                repaired_best[li] = (v, li, fs)
        return spent

    def contenders() -> list:
        """The best repaired team of each gated leader within REPAIR_MARGIN of the
        best team (at most REPAIR_CLIMB of them): the climb climbs from these too,
        under their own leader, so a repair one or two swaps off (the wrong member
        left, another card of the same holomem) still reaches the team."""
        best = (top(1) or [(0.0,)])[0][0]  # type: ignore[list-item]
        lead = {li for _v, li, _s in top(neighbours)}
        near = sorted(
            (r for li, r in repaired_best.items() if li not in lead and r[0] >= (1.0 - REPAIR_MARGIN) * best),
            reverse=True,
        )
        out = []
        for _v, li, s0 in near[:REPAIR_CLIMB]:
            mine = [(v, l, s) for (l, s), v in scored.items() if l == li and ok(l, s)]
            out.append(max(mine))
        return out

    # REGIONS: every leader's (tilt, hard gate) seeded from the screen and exact-scored; the
    # regions whose best team is within REGION_MARGIN of the best are climbed below
    region_of: list = []
    seeded = 0
    if tilt_of is not None:
        rate: dict = {}
        for gate in model.gates:
            if gate is not None and gate not in rate:
                rate[gate] = _mean(1.0 if _fires(gate, cards, s) else 0.0 for s in first_sets)
        regions: dict = {}
        for li, leader in enumerate(leaders):
            gate = model.gates[li]
            tilt = tilt_of(leader)
            key = (
                gate if gate is not None and rate[gate] < REGION_HARD_GATE else None,
                tuple(tilt) if tilt is not None else None,
            )
            regions.setdefault(key, []).append(li)
            region_of.append(key)
        seeds = []
        for (gate, tilt), members in regions.items():
            if gate is None and tilt is None:
                continue  # the screen's own region: the search above is its seed
            for s in _region_seed_sets(
                screen, cards, holomems, gate, tilt, require, REGION_SEEDS, REGION_BEAM
            ):
                li = max(members, key=lambda m: (model.predict(m, s), -m))
                if ok(li, s):
                    seeds.append((li, s))
        seeded = len(seeds)
        polish_seconds += take(seeds)
    else:
        polish_seconds += repair_gates(best_sets[:gate_repair])

    def region_contenders() -> list:
        """The best team of each region within REGION_MARGIN of the best team, the regions
        of the best `neighbours` teams' leaders left out (the climb starts from those)."""
        if not region_of:
            return []
        best = (top(1) or [(0.0,)])[0][0]  # type: ignore[list-item]
        lead = {region_of[li] for _v, li, _s in top(neighbours)}
        own: dict = {}
        for (li, s), v in scored.items():
            key = region_of[li]
            if key not in lead and ok(li, s) and (v, -li, s) > own.get(key, (float("-inf"),)):
                own[key] = (v, -li, s)
        return sorted(
            ((v, -nli, s) for v, nli, s in own.values() if v >= (1.0 - REGION_MARGIN) * best),
            key=lambda r: (-r[0], r[1], r[2]),
        )

    # then a hill climb: the one-card swaps of the best teams, again, until the
    # best ones stop changing. A single pass reaches teams ONE swap away; the
    # best team can be two or three swaps from anything the screen proposed.
    # A swap keeps the leader, so a set the climb reaches is seen under ONE leader
    # only; each round, the best sets not yet polished are scored under every
    # leader too: otherwise a set reached under one leader can score higher under
    # another that is never tried
    polished = set(best_sets)

    def polish_new() -> float:
        fresh = [s for s in dict.fromkeys(s for _v, _li, s in top(polish)) if s not in polished]
        polished.update(fresh)
        return take([(li, s) for s in fresh for li in range(len(leaders)) if ok(li, s)])

    climbed = 0
    region_climbs = 0
    if neighbours:
        before = None
        for climbed in range(1, climb_rounds + 1):
            regional = region_contenders()
            classes = class_best() if class_of else []
            starts = top(neighbours) + (regional if region_of else contenders()) + classes
            now = [(li, s) for _v, li, s in starts]
            if now == before:
                break
            before = now
            region_climbs = max(region_climbs, len(regional))
            polish_seconds += _take_neighbours(starts, holomems, take, scored, ok)
            polish_seconds += take_class_climb(classes)
            paired = list(dict.fromkeys(regional + classes))
            if paired:
                polish_seconds += _take_paired_swaps(paired, REGION_PAIR_MOVES, holomems, take, scored, ok)
            polish_seconds += polish_new()
            if not region_of:
                polish_seconds += repair_gates(
                    list(dict.fromkeys(s for _v, _li, s in top(polish)))[:gate_repair]
                )
    return {
        "ranking": top(len(scored)),
        "rounds": rounds,
        "stats": {
            "stop": stop,
            "exact_evals": len(scored),
            "sets": len(best_of_set),
            "legal_sets": legal,
            "seconds": time.perf_counter() - started,
            "polish_seconds": polish_seconds,
            "k": k,
            "step": step,
            "initial_sets": initial_sets,
            "polish": polish,
            "neighbours": neighbours,
            "climb_rounds": climbed,
            **(
                {
                    "regions": len(set(region_of)),
                    "region_seeds": seeded,
                    "region_climbs": region_climbs,
                    "classes": len(class_members),
                }
                if region_of
                else {}
            ),
        },
    }


def climb(
    cards: Sequence,
    leaders: Sequence,
    score_pairs: ScorePairs,
    start: Sequence[Pair],
    *,
    neighbours: int = 5,
    polish: int = 20,
    rounds: int = 8,
    require: Sequence[int] = (),
    allowed: Callable[[int, tuple[int, ...]], bool] | None = None,
    cancelled: Callable[[], bool] | None = None,
    pair_moves: int = 0,
) -> dict:
    """A hill climb on ANOTHER objective, from `start` (pairs another search found):
    the start pairs are scored, the best `polish` sets under every leader, then
    rounds of the one-card swaps of the best `neighbours` teams (same leader) plus
    every leader for a set that enters the best `polish`, until the best teams stop
    changing. The In general search runs it on a wider chart sample than the one it
    searched on: the narrow sample finds the region, this settles the team in it.
    Returns `{"ranking": [(score, leader index, set)] best first (every scored
    allowed pair), "stats": {...}}`; every score is `score_pairs`'."""
    holomems = [c.member for c in cards]
    require = tuple(sorted(set(require)))
    scored: dict[Pair, float] = {}

    def ok(li: int, s: tuple[int, ...]) -> bool:
        if require and not all(i in s for i in require):
            return False
        return allowed is None or allowed(li, s)

    def take(pairs) -> float:
        if cancelled is not None and cancelled():
            raise InterruptedError("cancelled")
        t0 = time.perf_counter()
        fresh = sorted(dict.fromkeys(p for p in pairs if p not in scored), key=lambda p: p[0])
        if fresh:
            for p, v in zip(fresh, score_pairs(fresh)):
                v = float(v)
                if not math.isfinite(v):
                    raise ValueError(f"the scorer returned {v!r} for {p}")
                scored[p] = v
        return time.perf_counter() - t0

    def top(n: int):
        return _best(scored, ok, n)

    started = time.perf_counter()
    take([p for p in start if ok(*p)])
    polished: set = set()

    def polish_new() -> None:
        fresh = [s for s in dict.fromkeys(s for _v, _li, s in top(polish)) if s not in polished]
        polished.update(fresh)
        take([(li, s) for s in fresh for li in range(len(leaders)) if ok(li, s)])

    polish_new()
    before, done = None, 0
    for done in range(1, rounds + 1):
        now = [(li, s) for _v, li, s in top(neighbours)]
        if now == before:
            break
        before = now
        _take_neighbours(top(neighbours), holomems, take, scored, ok)
        if pair_moves:
            _take_paired_swaps(top(1), pair_moves, holomems, take, scored, ok)
        polish_new()
    return {
        "ranking": top(len(scored)),
        "stats": {"evals": len(scored), "rounds": done, "seconds": time.perf_counter() - started},
    }


def _region_seed_sets(screen, cards, holomems, gate, tilt, require, count: int, beam: int):
    """The `count` best legal sets of one leader region by the screen's prediction plus the
    sum of `tilt` over the members: a beam `beam` wide, grown one card at a time from the
    required cards plus the best ways to fire `gate` (kind, target, need), so every set it
    returns fires the gate. Deterministic: ties go to the lower set."""
    n = len(cards)
    extra = list(tilt) if tilt is not None else [0.0] * n

    def value(s):
        return screen.predict(s) + sum(extra[i] for i in s)

    base = tuple(sorted(require))
    starts = [base]
    if gate is not None:
        kind, target, need = gate
        if kind == "type":
            match = [i for i in range(n) if cards[i].type == target]
        elif kind == "generation":
            match = [i for i in range(n) if target in cards[i].generations]
        else:
            return []
        short = need - sum(1 for i in base if i in match)
        if short > 0:
            names = {holomems[i] for i in base}
            pool = [i for i in match if i not in base and holomems[i] not in names]
            ways = [
                tuple(sorted(base + c))
                for c in itertools.combinations(pool, short)
                if len({holomems[i] for i in c}) == short
            ]
            if not ways or len(base) + short > 5:
                return []
            starts = sorted(ways, key=lambda s: (-value(s), s))[:beam]
    frontier = starts
    while frontier and len(frontier[0]) < 5:
        grown: dict = {}
        for s in frontier:
            names = {holomems[i] for i in s}
            for c in range(n):
                if c not in s and holomems[c] not in names:
                    t = tuple(sorted(s + (c,)))
                    if t not in grown:
                        grown[t] = value(t)
        frontier = sorted(grown, key=lambda s: (-grown[s], s))[:beam]
    return frontier[:count]


def _repair(s, gate, cards, holomems, weight):
    """`s` made to fire `gate` (kind, target, need): its weakest non-matching members
    (lowest `weight`) swapped for the strongest matching cards of holomems not in the
    set. None when the pool cannot fire the gate."""
    kind, target, need = gate
    if kind == "type":
        match = lambda i: cards[i].type == target  # noqa: E731
    elif kind == "generation":
        match = lambda i: target in cards[i].generations  # noqa: E731
    else:
        return None
    short = need - sum(1 for i in s if match(i))
    if short <= 0:
        return None
    out = sorted((i for i in s if not match(i)), key=lambda i: (weight[i], i))
    if len(out) < short:
        return None
    keep = [i for i in s if i not in out[:short]]
    names = {holomems[i] for i in keep}
    added = []
    for i in sorted((i for i in range(len(cards)) if match(i)), key=lambda i: (-weight[i], i)):
        if holomems[i] not in names:
            added.append(i)
            names.add(holomems[i])
            if len(added) == short:
                return tuple(sorted(keep + added))
    return None


def _sets_with(require, holomems, count, rng) -> list[tuple[int, ...]]:
    """`count` random legal sets holding every card in `require`."""
    names = {holomems[i] for i in require}
    rest = [i for i in range(len(holomems)) if holomems[i] not in names]
    out: list[tuple[int, ...]]
    out, seen = [], set()
    for _ in range(count * 20):
        if len(out) >= count:
            break
        picked, used = list(require), set(names)
        for i in rng.sample(rest, len(rest)):
            if len(picked) == 5:
                break
            if holomems[i] not in used:
                picked.append(i)
                used.add(holomems[i])
        s = tuple(sorted(picked))
        if len(s) == 5 and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _take_paired_swaps(best, moves, holomems, take, scored, ok) -> float:
    """Two-card moves on each of the `best` teams, cheaply: its `moves` best one-card
    swaps already scored (better OR worse than it), combined in pairs. A one-swap
    climb only takes a step that improves on its own, and some better teams need two
    swaps neither of which pays alone. Every legal pair of the best one-swaps is
    `moves * (moves - 1) / 2` teams at most, against ~70,000 for every two-card move,
    so this needs no screen."""
    pairs = []
    for _v, li, members in best:
        base = set(members)
        singles = []
        for (lj, s), v in scored.items():
            if lj != li:
                continue
            out, into = base - set(s), set(s) - base
            if len(out) == 1 and len(into) == 1:
                singles.append((v, next(iter(out)), next(iter(into))))
        singles.sort(key=lambda m: (-m[0], m[1], m[2]))
        singles = singles[:moves]
        for a in range(len(singles)):
            for b in range(a + 1, len(singles)):
                _va, out_a, in_a = singles[a]
                _vb, out_b, in_b = singles[b]
                if out_a == out_b or in_a == in_b:
                    continue
                s = tuple(sorted((base - {out_a, out_b}) | {in_a, in_b}))
                if len({holomems[i] for i in s}) != 5:
                    continue
                p = (li, s)
                if p not in scored and ok(*p):
                    pairs.append(p)
    return take(pairs)


def _take_neighbours(best, holomems, take, scored, ok) -> float:
    """Every legal one-card swap of each of the `best` teams, under the same
    leader — the hedge that does not go through either model."""
    count = len(holomems)
    pairs = []
    for _v, li, members in best:
        for position in range(5):
            rest = members[:position] + members[position + 1 :]
            names = {holomems[i] for i in rest}
            for card in range(count):
                if card in members or holomems[card] in names:
                    continue
                p = (li, tuple(sorted(rest + (card,))))
                if p not in scored and ok(*p):
                    pairs.append(p)
    return take(pairs)


def search_song(
    cards: Sequence,
    leaders: Sequence,
    score: Callable,
    *,
    always: Sequence = (),
    gate_of: Callable[[Any], tuple | None] | None = None,
    seed: int = 20261016,
    per_leader: int = 100,
    **options: Any,
) -> dict:
    """`joint_search.search` with `two_stage.search_song`'s interface and result
    shape, so a caller written for that (`event_mode.search_event`) switches
    engines without other change. `score(leader, sets)` -> exact values (an
    objective-wrapped scorer works: `two_stage.objective_scorer`); `always` are
    leader OBJECTS. Each row is one leader's best team found; its `ranking` is
    that leader's best `per_leader` (value, set). Leaders that never appear in
    an evaluated pair are left out (`stats["leaders"]` counts the rows)."""
    leaders = list(leaders)
    position = {id(l): k for k, l in enumerate(leaders)}
    # `score.many([(leader, sets), ...])` -> one value list per group, when the scorer
    # offers it: every leader's sets of a round in ONE call (one batch for the workers)
    many = getattr(score, "many", None)

    def score_pairs(pairs):
        out = [0.0] * len(pairs)
        by_leader: dict[int, list[int]] = {}
        for k, (li, _s) in enumerate(pairs):
            by_leader.setdefault(li, []).append(k)
        if many is not None:
            groups = list(by_leader.items())
            values_by_group = many([(leaders[li], [pairs[k][1] for k in ks]) for li, ks in groups])
            for (_li, ks), values in zip(groups, values_by_group):
                for k, v in zip(ks, values):
                    out[k] = float(v)
            return out
        for li, ks in by_leader.items():
            values = score(leaders[li], [pairs[k][1] for k in ks])
            for k, v in zip(ks, values):
                out[k] = float(v)
        return out

    found = search(
        cards,
        leaders,
        score_pairs,
        gate_of=gate_of,
        seed=seed,
        always=[position[id(l)] for l in always if id(l) in position],
        **options,
    )
    rows: dict[int, dict] = {}
    for value, li, s in found["ranking"]:
        row = rows.get(li)
        if row is None:
            rows[li] = {
                "position": li,
                "leader": leaders[li],
                "status": "searched",
                "best": value,
                "members": s,
                "ranking": [(value, s)],
            }
        elif len(row["ranking"]) < per_leader:
            row["ranking"].append((value, s))
    out = sorted(rows.values(), key=lambda r: (-r["best"], r["position"]))
    stats = dict(found["stats"])
    stats.update(leaders=len(out), searched=len(out), not_reached=0)
    return {"rows": out, "base": {}, "stats": stats}
