"""MEMBERS FIRST, LEADER SECOND — the reusable, file-free core of the
members-first search.

The per-(song, leader) objective `S(T, C)` depends on the leader only through
(a) its outfit skill — a team-wide stat_up / score_support gated on the FIVE
members' type or generation counts, plus its scored rider — (b) its own
holomem's plan role (leader / leader+member) and singer context, and (c)
nothing else. So:

1. **Enumerate once, no leader.** Every legal member 5-set of the pool is
   scored ONE time under a BARE proxy leader (`bare_leader`: a real card with
   its outfit skill removed — same holomem, so the plan role and `sings`
   behave like a real leader of that holomem). The scores are kept in
   `exact_row.legal_rosters` order (lexicographic 5-combinations of pool
   indices with distinct holomems) — the caller owns the bytes.
2. **Per real leader, re-score only what can matter** (`candidate_tiers`):
   the top-K sets by bare score overall, the top-K among the sets that
   SATISFY the leader's outfit gate (a gated outfit can only win on a set
   that fires it), and the top-K among the sets containing the leader's own
   holomem (the leader+member role). Their union (≈ 2K … 3K sets) is scored
   on the exact leaf under the real leader; the best is the members-first
   answer for that leader.

Measured on song m0334 against the complete exact table (66 leaders, B&B): K=2,000
reproduces every certified row — 66/66, 0 loss, same rosters; K=100 misses
4 GATED outfits whose optimum is a gate-specific kit far down the bare ranking.
K is empirical; the answer per leader is HEURISTIC until a B&B row certifies it.

`tabulate` packs the per-set aggregates the tiers filter on into flat arrays
(5 bytes + 4 + 8 + 8 per set — ~210 MB for the 8,441,052 sets of the 66-card
5★ pool, built in ~8 s): type counts as nibbles, generation PRESENCE as a
bitmask (a count-gate on a generation re-reads the roster, since presence
cannot count), holomem set as a bitmask.

INCREMENTAL UPDATE (`extended_order` / `merge_scores`). A new
card lands in the pool; the old enumeration (pool P, scores in
`legal_rosters(P)` order) is EXTENDED to P' ⊇ P without re-scoring a single
old set: both orders are lexicographic over pool indices sorted by id, and
the old pool's order is the restriction of the new one's (a monotone index
map preserves lexicographic order, and legality depends only on the
holomems, which do not change), so the sets made only of old cards appear in
the new enumeration in exactly the old order — ONE pass over the new
`legal_rosters` consumes the old scores in sequence and sends ONLY the sets
that contain a new card to the scorer (≈ C(n-1, 4)-scale for one new card,
minutes, not the hour of a full enumeration). An old card that left the pool
(a representative change — `exhaustive_song_leader.build_pool` collapses
identical-feature cards to one representative) is handled the same way in
reverse: its old sets are skipped, not reused.

Stdlib only; no file I/O; nothing here writes anything. The scoring callback
(`batch_score`) is injected — the web server hands in
`parallel_refine.RefinePool.batch_score`, the same leaf the exhaustive route
and the branch-and-bound use.
"""

from __future__ import annotations

import array
import dataclasses
from typing import Callable, Iterable, Mapping, Sequence

from hbuilder.domain.card import Card
from hbuilder.domain.team import Team
from hbuilder.solver.exact_row import legal_rosters

__all__ = [
    "BARE_TAG",
    "Gate",
    "Merge",
    "Tables",
    "bare_leader",
    "candidate_indices",
    "candidate_tiers",
    "extended_order",
    "gate_of",
    "gate_satisfied",
    "merge_scores",
    "score_sets",
    "tabulate",
]

BARE_TAG = "bare"
MEMBER_COUNT = 5

Gate = tuple[str, str | None, int]


def bare_leader(card: Card) -> Card:
    """The proxy: the same card (same holomem, so the plan role and `sings`
    behave as for a real leader of that holomem) with NO outfit skill. The id's
    4th segment becomes `bare` so the proxy can never be mistaken for a
    catalogue card (`card-04012-5-uniq-0051-00` -> `card-04012-5-bare-0051-00`)."""
    parts = card.id.split("-")
    if len(parts) < 6:
        raise ValueError(f"card id {card.id!r} is not of the catalogue shape")
    new_id = "-".join(parts[:2] + ["5", BARE_TAG, parts[4], parts[5]])
    return dataclasses.replace(card, id=new_id, costume_skill=None,
                               costume_name=f"BARE ({card.costume_name})")


def is_bare(card: Card) -> bool:
    parts = card.id.split("-")
    return len(parts) > 3 and parts[3] == BARE_TAG


def gate_of(card: Card) -> Gate | None:
    """`(kind, target, count)` of the outfit's gate, or None if the card has
    no outfit skill or the outfit is ungated. `kind` is "type" or
    "generation" (anything else is reported as-is; `gate_satisfied` then
    treats it as always-on, i.e. the gate tier collapses into the overall
    tier — never as never-on)."""
    cs = card.costume_skill
    if cs is None:
        return None
    cond = cs.condition
    kind = getattr(cond, "kind", None)
    if kind in (None, "ungated"):
        return None
    target = getattr(cond, "target", None) or getattr(cond, "value", None)
    return (str(kind), target, int(getattr(cond, "count", 0) or 0))


# ---------------------------------------------------------------------------
# the per-set tables
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Tables:
    """Per-set aggregates of every legal 5-set of `pool`, in `legal_rosters`
    order (index `i` here = index `i` of the enumeration's scores).

    `flat[5i:5i+5]` — the 5 pool indices of set `i` (ascending);
    `tcount[i]` — the count of each type, 4 bits per type in `types` order;
    `gmask[i]` — bit `g` set when some member carries generation `gens[g]`;
    `hmask[i]` — bit `h` set when holomem `hols[h]` is a member."""

    n: int
    pool_ids: tuple[str, ...]
    types: tuple[str, ...]
    gens: tuple[str, ...]
    hols: tuple[str, ...]
    card_t: tuple[int, ...]         # per pool card: index into `types`
    card_gmask: tuple[int, ...]     # per pool card: generation bitmask
    card_h: tuple[int, ...]         # per pool card: index into `hols`
    flat: array.array
    tcount: array.array
    gmask: Sequence[int]
    hmask: Sequence[int]

    @property
    def t_idx(self) -> dict[str, int]:
        return {t: i for i, t in enumerate(self.types)}

    @property
    def g_idx(self) -> dict[str, int]:
        return {g: i for i, g in enumerate(self.gens)}

    @property
    def h_idx(self) -> dict[str, int]:
        return {h: i for i, h in enumerate(self.hols)}

    def roster(self, i: int) -> tuple[int, ...]:
        return tuple(self.flat[5 * i:5 * i + 5])

    def roster_ids(self, i: int) -> tuple[str, ...]:
        return tuple(self.pool_ids[j] for j in self.roster(i))


def tabulate(reps: Sequence[Card]) -> Tables:
    """Build the `Tables` of every legal member 5-set of `reps` (pool order =
    `reps` order, the enumeration's own). ~8 s / ~210 MB for 8.4 M sets."""
    types = tuple(sorted({c.type for c in reps}))
    gens = tuple(sorted({g for c in reps for g in c.generations}))
    hols = tuple(sorted({c.member for c in reps}))
    if len(types) > 8:
        raise ValueError(f"{len(types)} card types do not fit 4-bit counts in 32 bits")
    t_idx = {t: i for i, t in enumerate(types)}
    g_idx = {g: i for i, g in enumerate(gens)}
    h_idx = {h: i for i, h in enumerate(hols)}
    card_t = tuple(t_idx[c.type] for c in reps)
    card_gmask = tuple(sum(1 << g_idx[g] for g in c.generations) for c in reps)
    card_h = tuple(h_idx[c.member] for c in reps)
    hol = tuple(c.member for c in reps)
    flat = array.array("B")
    tcount = array.array("I")
    # bitmasks fit a machine word on every real pool; a wider pool degrades
    # to Python ints rather than failing
    gmask: Sequence[int] = array.array("Q") if len(gens) <= 64 else []
    hmask: Sequence[int] = array.array("Q") if len(hols) <= 64 else []
    shifts = tuple(4 * t for t in card_t)
    n = 0
    for idx in legal_rosters(hol, MEMBER_COUNT):
        flat.extend(idx)
        tc = gm = hm = 0
        for k in idx:
            tc += 1 << shifts[k]
            gm |= card_gmask[k]
            hm |= 1 << card_h[k]
        tcount.append(tc)
        gmask.append(gm)   # type: ignore[union-attr]
        hmask.append(hm)   # type: ignore[union-attr]
        n += 1
    return Tables(n=n, pool_ids=tuple(c.id for c in reps), types=types, gens=gens,
                  hols=hols, card_t=card_t, card_gmask=card_gmask, card_h=card_h,
                  flat=flat, tcount=tcount, gmask=gmask, hmask=hmask)


# ---------------------------------------------------------------------------
# gate semantics and the candidate tiers
# ---------------------------------------------------------------------------
def _satisfier(tables: Tables, gate: Gate | None) -> Callable[[int], bool] | None:
    """The per-set predicate of `gate` over `tables`, or None when there is no
    gate. A type gate counts members of that type; a generation gate counts
    MEMBERS carrying the generation (presence bit first, then the roster —
    an exact check). An unknown kind, or a target no pool card
    carries, is always-on (the gate tier equals the overall tier)."""
    if gate is None:
        return None
    kind, target, cnt = gate
    if kind == "type":
        t_idx = tables.t_idx
        if target in t_idx:
            shift, tcount = 4 * t_idx[target], tables.tcount
            return lambda i: ((tcount[i] >> shift) & 0xF) >= cnt
    elif kind == "generation":
        g_idx = tables.g_idx
        if target in g_idx:
            gi, gmask, flat, card_gmask = g_idx[target], tables.gmask, tables.flat, tables.card_gmask
            return lambda i: bool((gmask[i] >> gi) & 1) and sum(
                1 for k in flat[5 * i:5 * i + 5] if (card_gmask[k] >> gi) & 1) >= cnt
    return lambda i: True


def gate_satisfied(tables: Tables, gate: Gate | None, i: int) -> bool:
    """Does set `i` fire `gate`? (No gate -> True.)"""
    f = _satisfier(tables, gate)
    return True if f is None else f(i)


def candidate_tiers(order: Sequence[int], tables: Tables, leader: Card,
                    ks: Iterable[int]) -> dict[int, set[int]]:
    """For each K in `ks`, the set of enumeration indices a real `leader` must
    be re-scored on: the first K of `order` (indices sorted by bare score,
    descending), the first K of `order` that satisfy the leader's outfit gate
    (only when the outfit is gated), and the first K of `order` that contain
    the leader's own holomem. One pass over `order`, stopping once every tier
    is full. A leader whose holomem is not in the pool has an empty own tier."""
    ks = sorted({int(k) for k in ks})
    if not ks or ks[0] < 0:
        raise ValueError("ks must be non-negative")
    kmax = ks[-1]
    gate = gate_of(leader)
    satisfies = _satisfier(tables, gate)
    h_idx = tables.h_idx
    own_bit = 1 << h_idx[leader.member] if leader.member in h_idx else 0
    hmask = tables.hmask
    cand_by_k: dict[int, set[int]] = {k: set() for k in ks}
    seen_all = seen_gate = seen_own = 0
    own_possible = own_bit != 0
    for i in order:
        if seen_all < kmax:
            seen_all += 1
            for k in ks:
                if seen_all <= k:
                    cand_by_k[k].add(i)
        if satisfies is not None and seen_gate < kmax and satisfies(i):
            seen_gate += 1
            for k in ks:
                if seen_gate <= k:
                    cand_by_k[k].add(i)
        if own_possible and seen_own < kmax and (hmask[i] & own_bit):
            seen_own += 1
            for k in ks:
                if seen_own <= k:
                    cand_by_k[k].add(i)
        if (seen_all >= kmax and (satisfies is None or seen_gate >= kmax)
                and (not own_possible or seen_own >= kmax)):
            break
    return cand_by_k


def candidate_indices(order: Sequence[int], tables: Tables, leader: Card,
                      k: int) -> list[int]:
    """The union of the three K-tiers for `leader`, as a sorted list."""
    return sorted(candidate_tiers(order, tables, leader, (k,))[k])


# ---------------------------------------------------------------------------
# scoring helper
# ---------------------------------------------------------------------------
def score_sets(leader: Card, reps: Sequence[Card], rosters: Iterable[Sequence[int]],
               batch_score: Callable[[Sequence[Team]], Sequence[float]]) -> list[float]:
    """Score member sets (tuples of pool indices into `reps`) under `leader`
    with the injected `batch_score` (one float per team, in order) — used both
    for the bare enumeration (leader = the proxy) and the per-leader re-score."""
    teams = [Team(leader=leader, members=tuple(reps[k] for k in idx)) for idx in rosters]
    return list(batch_score(teams)) if teams else []


def best_of(score_of: Mapping[int, float], candidates: Iterable[int]) -> tuple[int, float]:
    """`(index, score)` of the best candidate; ties -> the smallest index
    (a stable rule, rather than whatever order a set iterates in)."""
    best_i, best = -1, float("-inf")
    for i in candidates:
        s = score_of[i]
        if s > best or (s == best and i < best_i):
            best_i, best = i, s
    if best_i < 0:
        raise ValueError("no candidates")
    return best_i, best


# ---------------------------------------------------------------------------
# incremental update: extend an enumeration to a larger pool
# ---------------------------------------------------------------------------
def extended_order(old_pool_ids: Sequence[str], new_pool_ids: Sequence[str]) -> tuple[int, ...]:
    """Per OLD pool card, its index in the NEW pool (-1 when the card left the
    pool). The old pool's order must be the restriction of the new one's —
    the present cards' new indices strictly increase — else ValueError: that
    is the property `merge_scores` relies on to reuse the old scores by
    POSITION (both enumerations are lexicographic over pool indices, so a
    monotone index map carries old-only sets to the new enumeration in the
    old order). Duplicate ids raise too."""
    old = tuple(old_pool_ids)
    new = tuple(new_pool_ids)
    if len(set(old)) != len(old) or len(set(new)) != len(new):
        raise ValueError("pool ids must be distinct")
    pos = {pid: i for i, pid in enumerate(new)}
    out = tuple(pos.get(pid, -1) for pid in old)
    last = -1
    for pid, i in zip(old, out):
        if i < 0:
            continue
        if i <= last:
            raise ValueError(f"old pool order is not a restriction of the new pool order "
                             f"(at {pid!r}: new index {i} after {last})")
        last = i
    return out


@dataclasses.dataclass(frozen=True)
class Merge:
    """The result of `merge_scores`: the NEW enumeration's scores (in
    `legal_rosters(new pool holomems, 5)` order) and its bookkeeping."""

    scores: array.array             # 'd', one per legal set of the new pool
    n_sets: int
    reused: int                     # old scores carried over by position
    sets_rescored: int              # sets containing a new card, scored now
    dropped: int                    # old scores skipped (a card left the pool)
    old_index: tuple[int, ...]      # `extended_order(old_pool_ids, new_pool_ids)`
    new_indices: tuple[int, ...]    # new-pool indices of the cards not in the old pool
    dropped_ids: tuple[str, ...]    # old ids absent from the new pool


def merge_scores(old_scores: Sequence[float], old_pool_ids: Sequence[str],
                 new_reps: Sequence[Card],
                 score_new_sets: Callable[[Sequence[tuple[int, ...]]], Sequence[float]], *,
                 old_holomems: Sequence[str] | None = None, chunk: int = 20000,
                 progress: Callable[[int, int], None] | None = None) -> Merge:
    """Extend the enumeration `old_scores` (over `old_pool_ids`, in
    `legal_rosters` order) to the pool `new_reps` (its order = the new
    enumeration's): every set made only of old cards takes its OLD score by
    position; every set that contains a new card is scored by
    `score_new_sets(rosters)` (tuples of NEW pool indices, called per `chunk`;
    returns one float per roster, in order). `old_holomems` (per old pool
    card) is needed only when some old card is absent from the new pool — its
    old sets are then skipped, never reused; without drops it is derived from
    `new_reps`. `progress(rescored_so_far, reused_so_far)` after each chunk.

    Raises ValueError when the old order is not a restriction of the new
    one (`extended_order`) or the old scores do not line up with the old
    pool's legal sets (a wrong file)."""
    old_ids = tuple(old_pool_ids)
    new_ids = tuple(c.id for c in new_reps)
    old_index = extended_order(old_ids, new_ids)
    n_new_pool = len(new_ids)
    present = {i for i in old_index if i >= 0}
    is_new = [i not in present for i in range(n_new_pool)]
    new_indices = tuple(i for i in range(n_new_pool) if is_new[i])
    dropped_ids = tuple(pid for pid, i in zip(old_ids, old_index) if i < 0)
    new_hol = tuple(c.member for c in new_reps)
    if chunk <= 0:
        raise ValueError("chunk must be positive")

    # the old scores that are reusable, in order: all of them when no old card
    # left the pool; else the old enumeration is walked to skip the sets that
    # contain a dropped card (their holomems are needed for the walk)
    if dropped_ids:
        if old_holomems is None:
            raise ValueError(f"{len(dropped_ids)} old card(s) left the pool "
                             f"({dropped_ids[:3]}...); pass old_holomems to skip their sets")
        old_hol = tuple(old_holomems)
        if len(old_hol) != len(old_ids):
            raise ValueError("old_holomems must have one entry per old pool card")
        dropped_pos = {k for k, i in enumerate(old_index) if i < 0}

        def reusable():
            j = -1
            for j, idx in enumerate(legal_rosters(old_hol, MEMBER_COUNT)):
                if j >= len(old_scores):
                    raise ValueError("old scores are shorter than the old pool's legal sets")
                if not any(k in dropped_pos for k in idx):
                    yield old_scores[j]
            if j + 1 != len(old_scores):
                raise ValueError(f"old scores hold {len(old_scores)} entries, the old pool "
                                 f"has {j + 1} legal sets")
        old_iter = reusable()
    else:
        old_iter = iter(old_scores)

    scores = array.array("d")
    pending: list[tuple[int, ...]] = []
    pending_pos: list[int] = []
    reused = rescored = 0

    def flush() -> None:
        nonlocal rescored
        if not pending:
            return
        got = score_new_sets(list(pending))
        if len(got) != len(pending):
            raise ValueError("score_new_sets returned a different number of scores")
        for p, s in zip(pending_pos, got):
            scores[p] = float(s)
        rescored += len(pending)
        pending.clear()
        pending_pos.clear()
        if progress is not None:
            progress(rescored, reused)

    for idx in legal_rosters(new_hol, MEMBER_COUNT):
        if is_new[idx[0]] or is_new[idx[1]] or is_new[idx[2]] or is_new[idx[3]] or is_new[idx[4]]:
            pending.append(idx)
            pending_pos.append(len(scores))
            scores.append(0.0)
            if len(pending) >= chunk:
                flush()
        else:
            try:
                scores.append(float(next(old_iter)))
            except StopIteration:
                raise ValueError("old scores ran out before the old-only sets did "
                                 "(the scores do not belong to this old pool)") from None
            reused += 1
    flush()
    leftover = sum(1 for _ in old_iter)
    if leftover:
        raise ValueError(f"{leftover} old score(s) were never placed (the scores do not "
                         f"belong to this old pool)")
    dropped = len(old_scores) - reused
    return Merge(scores=scores, n_sets=len(scores), reused=reused, sets_rescored=rescored,
                 dropped=dropped, old_index=old_index, new_indices=new_indices,
                 dropped_ids=dropped_ids)
