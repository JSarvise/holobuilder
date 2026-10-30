"""ONE search over (leader, five members) — the leader chosen inside the search.

`two_stage.search_song` finds the best team of a song with a member search under
a bare proxy, a triage of every leader on ~500 sets and a full search for up to
12 leaders: e.g. 179k exact evaluations, 50 s, on a 114-card account. The
community tool holodori.best answers the same question with ~3,700: it picks a
leader FOR each member set with a cheap proxy and evaluates only that pair.

This does the same with two LEARNED models and no hand-written proxy, so a new
card or song needs nothing:

* the SET screen (`kernel/surrogate.py`, unchanged) learns `log V(S)`, the value
  of a member set S under its best leader found so far, and ranks every legal
  set in C;
* the LEADER model learns, per leader L, `log score(L, S) = theta_S + a_L +
  b_L * fires(gate_L, S) + c_L * holds(L, S)` (theta_S a per-set effect, so only
  the leaders' RELATIVE order is learned). On m0342 this form alone picks the
  true best leader of 82 % of sets,
  losing 0.24 % at p90 when wrong; its error is too large to rank TEAMS, which
  is why it only chooses which leaders a set is exact-scored with.

Rounds: the screen proposes `step` unseen sets; each is exact-scored under its
`k` best leaders by the model (plus the `always_k` best of the `always` leaders,
the song's singers, by the same model);
both models refit on every exact score. Stops when the best `stable_top` TEAMS
have not changed for `stable_rounds` rounds. A POLISH then exact-scores the best
`polish` sets under EVERY leader, so the reported top sets carry their true best
leader, and `neighbours` (one-card swaps of the best teams, under the same
leader) hedge against what the screen cannot see.

Every score returned is the exact leaf's; the models only choose what to score.
No certificate: "best found", with how much was scored in `stats`.
"""
from __future__ import annotations

import math
import random
import time
from typing import Callable, Sequence

import two_stage
from two_stage import _fires
import surrogate

# added to a required card's screen weight (log units) after each fit: far above
# any fitted weight, so every set holding all required cards outranks every other
REQUIRE_BIAS = 1_000.0
# a gated leader whose repaired team comes this close to the best is climbed from too
REPAIR_MARGIN = 0.03
REPAIR_CLIMB = 3

# (leader index, ascending member index 5-tuple)
Pair = tuple[int, tuple[int, ...]]
ScorePairs = Callable[[list[Pair]], Sequence[float]]


class LeaderModel:
    """Per-leader `a_L + b_L * fired + c_L * holds` with a per-set effect,
    fitted by alternating least squares. `b`, `c` are shrunk towards the mean
    of the leaders that share their gate kind, so a leader with few gate-firing
    samples borrows from the others instead of guessing."""

    def __init__(self, cards, leaders, gate_of) -> None:
        self.cards = list(cards)
        self.leaders = list(leaders)
        self.gates = [gate_of(l) if gate_of is not None else None for l in self.leaders]
        self.a = [0.0] * len(self.leaders)
        self.b = [0.0] * len(self.leaders)
        self.c = [0.0] * len(self.leaders)
        self._fired: dict[tuple[int, tuple[int, ...]], float] = {}

    def features(self, li: int, s: tuple[int, ...]) -> tuple[float, float]:
        key = (li, s)
        fired = self._fired.get(key)
        if fired is None:
            gate = self.gates[li]
            fired = 1.0 if gate is not None and _fires(gate, self.cards, s) else 0.0
            self._fired[key] = fired
        holds = 1.0 if any(self.cards[i].member == self.leaders[li].member for i in s) else 0.0
        return fired, holds

    def predict(self, li: int, s: tuple[int, ...]) -> float:
        fired, holds = self.features(li, s)
        return self.a[li] + self.b[li] * fired + self.c[li] * holds

    def fit(self, scored: dict[Pair, float], sweeps: int = 6, lam: float = 2.0) -> None:
        by_set: dict[tuple[int, ...], list[tuple[int, float]]] = {}
        for (li, s), v in scored.items():
            if v > 0:
                by_set.setdefault(s, []).append((li, math.log(v)))
        # only sets seen under 2+ leaders say anything about relative leaders
        by_set = {s: rows for s, rows in by_set.items() if len(rows) >= 2}
        if not by_set:
            return
        theta = {s: sum(y for _l, y in rows) / len(rows) for s, rows in by_set.items()}
        n = len(self.leaders)
        for _ in range(sweeps):
            # leader parameters given theta: 3x3 normal equations per leader, ridge on b, c
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
            prior_b = _mean([self.b[li] for li in range(n) if self.gates[li] is not None])
            prior_c = _mean(self.c)
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
                sol = _solve3(A, B)
                if sol is not None:
                    self.a[li], self.b[li], self.c[li] = sol
            # theta given the leaders
            for s, rows in by_set.items():
                theta[s] = sum(y - self.predict(li, s) for li, y in rows) / len(rows)
        # identifiability: centre a on its mean (only differences matter)
        mean_a = _mean(self.a)
        self.a = [v - mean_a for v in self.a]

    def ranked(self, s: tuple[int, ...]) -> list[int]:
        return sorted(range(len(self.leaders)), key=lambda li: -self.predict(li, s))


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


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
    gate_of: Callable[[object], tuple | None] | None = None,
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

    Returns `{"ranking": [(score, leader index, set)] best first (every scored
    ALLOWED pair), "rounds": [...], "stats": {...}}`."""
    cards = list(cards)
    leaders = list(leaders)
    holomems = [c.member for c in cards]
    rng = random.Random(seed)
    screen = surrogate.Screen(cards)
    model = LeaderModel(cards, leaders, gate_of)
    scored: dict[Pair, float] = {}
    best_of_set: dict[tuple[int, ...], float] = {}
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
        return time.perf_counter() - t0

    def top(n: int) -> list[tuple[float, int, tuple[int, ...]]]:
        return sorted(((v, li, s) for (li, s), v in scored.items() if ok(li, s)),
                      key=lambda r: (-r[0], r[1], r[2]))[:n]

    def leaders_for(s: tuple[int, ...]) -> list[int]:
        ranked = model.ranked(s)
        # the `always` leaders (the song's singers) the model ranks best FOR THIS SET,
        # `always_k` of them, on top of the model's own `k`: every one of them on every
        # set costs ~4.9x the search on a song with 19 singer cards (e.g. m0525:
        # 65,796 pairs / 193.6 s against 13,597 / 39.6 s, the same top 20) while a solo
        # singer has 1-3 cards; dropping them altogether can lose a singer-led best team
        sing = [li for li in ranked if li in always_set][:always_k]
        return list(dict.fromkeys(ranked[:k] + sing))

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
    record = {"round": 0, "pairs": len(scored), "sets": len(best_of_set),
              "best": first_best[0][0] if first_best else None,
              "exact_seconds": exact_seconds, "fit_seconds": fit_seconds, "scan_seconds": 0.0}
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
        sets = [s for s, v in best_of_set.items() if v > 0]
        screen.fit(sets, [math.log(best_of_set[s]) for s in sets])
        for i in require:
            screen._card[i] += REQUIRE_BIAS
        model.fit(scored)
        fit_seconds = time.perf_counter() - t0
        t0 = time.perf_counter()
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
        head, tail = proposed[:step], proposed[step:]
        if diverse and tail:
            anchors = [set(s) for s in dict.fromkeys(s for _v, _li, s in top(3 * diverse_anchors))][:diverse_anchors]
            far = [s for s in tail if all(len(a & set(s)) <= diverse_overlap for a in anchors)]
            head += far[: max(1, step * diverse // 100)]
        proposed = head
        exact_seconds = take([(li, s) for s in proposed for li in leaders_for(s)
                              if allowed is None or allowed(li, s)])
        if neighbours:
            exact_seconds += _take_neighbours(top(neighbours), holomems, take, scored, ok)
        now = [(li, s) for _v, li, s in top(stable_top)]
        changed = now != leaders_top
        leaders_top = now
        unchanged = 0 if changed else unchanged + 1
        record = {"round": number, "pairs": len(scored), "sets": len(best_of_set),
                  "best": (top(1) or [(None,)])[0][0], "exact_seconds": exact_seconds,
                  "fit_seconds": fit_seconds, "scan_seconds": scan_seconds, "top_changed": changed}
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
    # GATE REPAIR: a gated leader (an outfit that needs 2+ Gen 3, 3+ Pure ...) is worth
    # little on a set that misses its gate, so the model learns it as weak and never
    # tries it where it would win, and no one-card swap reaches it (the gate needs two
    # cards at once). Each of the best sets is also scored under every leader whose
    # gate it misses, with its weakest members (the screen's card weights) swapped for
    # the strongest cards that satisfy the gate. Without this a gated leader can lead only
    # a handful of scored teams, all well below what she reaches once her gate is met --
    # which can be the true #1
    weight = [float(screen._card[i]) for i in range(len(cards))]      # zeros before any fit
    repaired_sets: set = set()
    repaired_best: dict = {}

    def repair_gates(sets) -> float:
        # one repair per (set, leader); then, for the leaders whose repair came within
        # REPAIR_MARGIN of the best team, every choice of WHICH members make way on the
        # best three sets (the screen's weights picked the wrong one for Noel: Anya out,
        # Koyori kept, 3,849,225 -- one swap from her 3,926,903)
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
        best = (top(1) or [(0.0,)])[0][0]
        lead = {li for _v, li, _s in top(neighbours)}
        near = sorted((r for li, r in repaired_best.items()
                       if li not in lead and r[0] >= (1.0 - REPAIR_MARGIN) * best), reverse=True)
        out = []
        for _v, li, s0 in near[:REPAIR_CLIMB]:
            mine = [(v, l, s) for (l, s), v in scored.items() if l == li and ok(l, s)]
            out.append(max(mine))
        return out

    polish_seconds += repair_gates(best_sets[:gate_repair])
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
    if neighbours:
        before = None
        for climbed in range(1, climb_rounds + 1):
            starts = top(neighbours) + contenders()
            now = [(li, s) for _v, li, s in starts]
            if now == before:
                break
            before = now
            polish_seconds += _take_neighbours(starts, holomems, take, scored, ok)
            polish_seconds += polish_new()
            polish_seconds += repair_gates(list(dict.fromkeys(s for _v, _li, s in top(polish)))[:gate_repair])
    return {
        "ranking": top(len(scored)),
        "rounds": rounds,
        "stats": {
            "stop": stop, "exact_evals": len(scored), "sets": len(best_of_set),
            "legal_sets": legal, "seconds": time.perf_counter() - started,
            "polish_seconds": polish_seconds, "k": k, "step": step,
            "initial_sets": initial_sets, "polish": polish, "neighbours": neighbours,
            "climb_rounds": climbed,
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
        return sorted(((v, li, s) for (li, s), v in scored.items() if ok(li, s)),
                      key=lambda r: (-r[0], r[1], r[2]))[:n]

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
    return {"ranking": top(len(scored)),
            "stats": {"evals": len(scored), "rounds": done, "seconds": time.perf_counter() - started}}


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
            rest = members[:position] + members[position + 1:]
            names = {holomems[i] for i in rest}
            for card in range(count):
                if card in members or holomems[card] in names:
                    continue
                p = (li, tuple(sorted(rest + (card,))))
                if p not in scored and ok(*p):
                    pairs.append(p)
    return take(pairs)


def search_song(cards: Sequence, leaders: Sequence, score: Callable, *, always: Sequence = (),
                gate_of=None, seed: int = 20261016, per_leader: int = 100,
                **options) -> dict:
    """`joint_search.search` with `two_stage.search_song`'s interface and result
    shape, so a caller written for that (`event_mode.search_event`) switches
    engines without other change. `score(leader, sets)` -> exact values (an
    objective-wrapped scorer works: `two_stage.objective_scorer`); `always` are
    leader OBJECTS. Each row is one leader's best team found; its `ranking` is
    that leader's best `per_leader` (value, set). Leaders that never appear in
    an evaluated pair are left out (`stats["leaders"]` counts the rows)."""
    leaders = list(leaders)
    position = {id(l): k for k, l in enumerate(leaders)}

    def score_pairs(pairs):
        out = [0.0] * len(pairs)
        by_leader: dict[int, list[int]] = {}
        for k, (li, _s) in enumerate(pairs):
            by_leader.setdefault(li, []).append(k)
        for li, ks in by_leader.items():
            values = score(leaders[li], [pairs[k][1] for k in ks])
            for k, v in zip(ks, values):
                out[k] = float(v)
        return out

    found = search(cards, leaders, score_pairs, gate_of=gate_of, seed=seed,
                   always=[position[id(l)] for l in always if id(l) in position], **options)
    rows: dict[int, dict] = {}
    for value, li, s in found["ranking"]:
        row = rows.get(li)
        if row is None:
            rows[li] = {"position": li, "leader": leaders[li], "status": "searched", "best": value,
                        "members": s, "ranking": [(value, s)]}
        elif len(row["ranking"]) < per_leader:
            row["ranking"].append((value, s))
    out = sorted(rows.values(), key=lambda r: (-r["best"], r["position"]))
    stats = dict(found["stats"])
    stats.update(leaders=len(out), searched=len(out), not_reached=0)
    return {"rows": out, "base": {}, "stats": stats}
