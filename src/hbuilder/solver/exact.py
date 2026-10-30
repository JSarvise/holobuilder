"""Exhaustive (1 Leader + 5 Members) enumeration and bounded-heap
Top-N selection over `domain.scoring.score_fast`.

`solve_exact` selects its Top-N result with `heapq.nlargest` over a
generator, never by collecting every scored arrangement into a list or
tuple and then sorting. At the shipped 6-card `data/owned.json` this
distinction is invisible; at the full ~54-card collection it is the
difference between a few hundred bytes of retained state and
~155,000,000 materialised records.

This module imports nothing from `json`, `argparse`, `pathlib`, or `sys`
(no I/O in the compute layer), and it never resolves a gate or computes
a rate itself — the only domain call it makes is
`score_card_set(combo, cfg)`, called once per 6-card combination rather
than once per arrangement (the batch API). `solver/` -> `domain/`
is a one-way seam: the heuristic's local-swap loop builds on it too, so a
solver-local copy of gate resolution or EV arithmetic here would mean
divergent implementations.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator

from hbuilder.domain.card import Card
from hbuilder.domain.scoring import score_card_set
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.team import MEMBER_COUNT, TEAM_SIZE, Team

# The default Top-N size when a caller does not request another.
DEFAULT_TOP_N: int = 10

# Cadence (in evaluated arrangements) at which `iter_candidates` calls the
# caller-supplied `progress` callback, when one is supplied (the CLI's
# stderr progress reporter uses it).
PROGRESS_INTERVAL_TEAMS: int = 250_000

# A constraint predicate rejects an arrangement before it is scored.
# `(combination, leader_index) -> bool`; `True` keeps the arrangement,
# `False` skips it. Selector locks (`--require`/`--leader`/`--exclude`)
# are wired through it.
Constraint = Callable[[tuple, object], bool]
# A set-level predicate, applied to the five members BEFORE they are scored.
# Separate from `Constraint` because it is the only one that can make a locked
# run genuinely cheaper: `Constraint` sees a (members, leader) pair, which
# exists only after `score_card_set` has already run for that set.
SetConstraint = Callable[[tuple], bool]
Progress = Callable[[int], None]


@dataclass(frozen=True, slots=True)
class RankedTeam:
    """One scored (leader, members) arrangement. `member_ids` is always
    ascending by construction (see `iter_candidates`), which is what makes
    `_heap_key`'s tie-break well defined without a second sort."""

    score: float
    leader_id: str
    member_ids: tuple[str, ...]
    team: Team


def canonical_pool(cards: Iterable[Card]) -> tuple[Card, ...]:
    """Order `cards` by ascending `card.id`. This is the determinism
    anchor: enumeration order becomes a pure function of the id
    set, independent of the order ids happen to appear in `owned.json`.
    Kept as its own function so `solve_exact`'s own body stays free of any
    ordering call."""
    return tuple(sorted(cards, key=lambda card: card.id))


def _iter_member_sets(
    pool: tuple[Card, ...],
    locked_ids: frozenset[str],
    leader_id: str | None = None,
) -> Iterator[tuple[Card, ...]]:
    """Every member set that could still satisfy `locked_ids`, built by
    INJECTING the locked cards rather than filtering them out afterwards.

    `set_constraint` stops a locked run from *scoring* sets that cannot
    qualify, but the enumeration still walks all `C(N, 5)` of them to reject
    them — 46.5 M at a 91-card pool, which is 30 s of pure walking for
    a run the projection puts at a fraction of a second. Choosing the free
    slots from the unlocked pool and splicing the locks in removes that walk
    entirely.

    Two families, and they cannot overlap: either every lock is a member
    (family A), or exactly one is deferred to the outfit slot (family B) —
    there is only one such slot. A family-B set is missing exactly one lock,
    and the free slots are drawn from cards that are not locked at all, so no
    set is produced twice.

    `leader_id`, when given, is the card the outfit slot is PINNED to, and it
    collapses family B to a single deferral. A family-B set is missing exactly
    one lock, so its outfit slot IS that missing card — and when the slot is
    already pinned to a different one, no leader can satisfy the set, so every
    one of them is built only to be rejected. On a 91-card pool with
    `--leader` + `--require` x2 (`k=3` locks, two of the three deferrals
    inadmissible, `C(88,3)` sets each) that is **219,472 sets walked and
    rejected on one `constraint` call apiece, ~0.3 s of 23.7 s (1.4%)**.
    Optional and defaulted off, so a caller that pins no leader walks every
    set.

    Each set is yielded ascending by `card.id`, matching what
    `itertools.combinations` over a canonical pool gives, so `member_ids`
    stays ascending by construction and `_heap_key`'s tie-break is unchanged.
    """
    if not locked_ids:
        yield from itertools.combinations(pool, MEMBER_COUNT)
        return

    locked = [card for card in pool if card.id in locked_ids]
    unlocked = [card for card in pool if card.id not in locked_ids]
    by_id = lambda card: card.id

    if len(locked) <= MEMBER_COUNT:
        for rest in itertools.combinations(unlocked, MEMBER_COUNT - len(locked)):
            yield tuple(sorted((*locked, *rest), key=by_id))

    for skipped in range(len(locked)):
        if leader_id is not None and locked[skipped].id != leader_id:
            # The deferred lock is the only card that can fill this set's
            # outfit slot, and the slot is pinned to someone else. Not a
            # heuristic: `iter_candidates` narrows such a set's leader vector
            # to `(deferred_index,)` and `constraint` then rejects it for
            # `leader.id != leader_id`, unconditionally. Skipping it here
            # removes the build and the rejection, never an emitted result.
            continue
        kept = locked[:skipped] + locked[skipped + 1 :]
        if len(kept) > MEMBER_COUNT:
            continue
        for rest in itertools.combinations(unlocked, MEMBER_COUNT - len(kept)):
            yield tuple(sorted((*kept, *rest), key=by_id))


def iter_candidates(
    pool: tuple[Card, ...],
    cfg: ScoringConfig,
    progress: Progress | None = None,
    constraint: Constraint | None = None,
    set_constraint: SetConstraint | None = None,
    locked_ids: frozenset[str] = frozenset(),
    leader_id: str | None = None,
    member_sets: Iterable[tuple[Card, ...]] | None = None,
    leader_ids: frozenset[str] | None = None,
) -> Iterator[RankedTeam]:
    """A generator function: yields one `RankedTeam` per (6-card
    combination, leader choice) pair. Never `print`s and never touches
    `sys`.

    **`member_sets` replaces the enumeration, never the scoring.** When given,
    it is iterated instead of `_iter_member_sets` and everything after it — R1,
    `set_constraint`, the two leader narrowings, `constraint`, `score_card_set`,
    the tie-break-safe ascending `member_ids` — runs verbatim. `solver.parallel`
    passes one worker's share of the combination space through it; that is the
    whole of what makes the parallel search a *partition* of this function
    rather than a second copy of it, and it is why the two are bit-identical by
    construction instead of by two implementations staying in step. A caller
    supplying it owes the same contract `_iter_member_sets` honours: five cards
    per set, ascending by `card.id`.

    `score_card_set(combo, cfg)` is called exactly once per
    6-card combination — not once per leader arrangement — since gate
    resolution and 18 of 19 per-skill EVs are identical across all six
    leader-permutations of a set. Each of the six `LeaderScore` entries it
    returns is then paired with its own constructed `Team` so
    `RankedTeam.team` stays available for the Top-N re-score, without the
    solver ever resolving a gate or computing a rate itself.

    `pool` must already be canonicalised (ascending by `card.id`) —
    `itertools.combinations` then yields each 6-card combination with its
    elements already in ascending-id order, so `member_ids` (built by
    skipping the leader's index) is ascending by construction, with no
    separate sort.

    **The leader vector is narrowed BEFORE `score_card_set`, never after.**
    `score_card_set(members, leaders, cfg)` costs a set-invariant prefix plus
    one `combine()` per entry in `leaders`; handing it the whole pool and then
    dropping 90 of 91 results with `constraint` pays for every one of them.
    On a 91-card pool: 480 sets/s against the full leader vector, 5,856
    sets/s against a one-card one — 12.2x, thrown away on every `--leader`
    run without the narrowing. Two narrowings apply, in this order:

    1. A set that defers a lock to the outfit slot has exactly ONE admissible
       leader — the deferred card itself, since no other card can supply that
       lock. `locked_ids` alone determines this, so it holds whether or not a
       `constraint` was supplied. (Without it, a locked run with no
       `constraint` would emit arrangements that violate its own locks.)
    2. `constraint` then filters whatever survives, so a `--leader` pin
       collapses the vector to one card before any scoring happens.

    **`leader_id` is the optional third narrowing, and it is the only one that
    moves work out of the ENUMERATION rather than out of the scoring.** With
    the outfit slot pinned, exactly one of the `k` family-B deferrals is
    admissible; `locked_ids` alone cannot say which, so without this parameter
    `_iter_member_sets` builds all `k` and `constraint` rejects `k-1` of them
    one set at a time (see `_iter_member_sets` for the cost).
    It also collapses the family-A leader vector to the pinned
    card directly, instead of calling `constraint` once per pool card to
    discover the same thing. Defaults to `None` (no pin).
    """
    evaluated = 0
    index_by_id = {card.id: position for position, card in enumerate(pool)}
    if leader_ids is None:
        all_leader_indices = tuple(range(len(pool)))
    else:
        # `solver.dominance` narrows the OUTFIT SLOT only, never the member
        # enumeration: a card whose outfit is dominated still carries stats and
        # skills and is a fully eligible member. The narrowing is applied here,
        # to the default vector, and deliberately NOT to the deferred-lock or
        # pinned vectors below — those are forced choices that `locked_ids` and
        # `leader_id` already determined, and silently dropping one would make
        # a locked run answer a different question than it was asked.
        all_leader_indices = tuple(
            position for position, card in enumerate(pool) if card.id in leader_ids
        )
    pinned_leader_indices: tuple[int, ...] | None = None
    if leader_id is not None:
        pinned_index = index_by_id.get(leader_id)
        if pinned_index is None:
            # The pinned leader is not in this pool, so no arrangement can
            # satisfy the pin. `constraint` would reject every one of them;
            # this reaches the same empty answer without enumerating.
            return
        pinned_leader_indices = (pinned_index,)
    sets = (
        _iter_member_sets(pool, locked_ids, leader_id)
        if member_sets is None
        else member_sets
    )
    for members in sets:
        # R1 is an ENUMERATION constraint: a holomem
        # cannot repeat among the members, so two skins of one character are
        # not a team. Skipped before scoring, never filtered afterwards --
        # a post-filter over yielded candidates would waste the score and,
        # worse, let an invalid team occupy a Top-N slot.
        holomem = {card.member for card in members}
        if len(holomem) != MEMBER_COUNT:
            continue
        # The set-level lock check runs BEFORE scoring, which is the whole
        # point of it: `--require` is supposed to make the run smaller, and
        # `project_arrangements` reports it as `C(N-k, 5-k)` sets. Rejecting
        # only per-(set, leader) below would prune the OUTPUT while the
        # enumeration still scored every set — the projection and the real
        # cost would then disagree, and the flag would buy nothing.
        if set_constraint is not None and not set_constraint(members):
            continue

        leader_indices = (
            all_leader_indices
            if pinned_leader_indices is None
            else pinned_leader_indices
        )
        if locked_ids:
            unmet = locked_ids - {card.id for card in members}
            if len(unmet) > 1:
                # Two locks left for one outfit slot: unsatisfiable.
                continue
            if unmet:
                deferred_index = index_by_id.get(next(iter(unmet)))
                if deferred_index is None:
                    continue  # the lock is not in this pool at all
                leader_indices = (deferred_index,)

        if constraint is not None:
            leader_indices = tuple(
                position
                for position in leader_indices
                if constraint(members, pool[position])
            )
            if not leader_indices:
                continue

        if len(leader_indices) == len(pool):
            leader_cards = pool
        else:
            leader_cards = tuple(pool[position] for position in leader_indices)

        leader_scores = score_card_set(members, leader_cards, cfg)
        member_ids = tuple(card.id for card in members)
        for slot, leader in enumerate(leader_cards):
            team = Team(leader=leader, members=members)
            yield RankedTeam(
                score=leader_scores[slot].total_score,
                leader_id=leader.id,
                member_ids=member_ids,
                team=team,
            )
            evaluated += 1
            if progress is not None and evaluated % PROGRESS_INTERVAL_TEAMS == 0:
                progress(evaluated)


def _heap_key(
    candidate: RankedTeam, index_of: dict[str, int]
) -> tuple[float, int, tuple[int, ...]]:
    """The tie-break key expressed so `heapq.nlargest` maximising it
    reproduces the fixed ascending order
    `(-score, leader_id, sorted(member_ids))`. Because `canonical_pool`
    sorts by id, an id's pool index is order-isomorphic to the id string,
    so negated integer indices invert cleanly: maximising this key yields
    highest score first, then the lowest leader index (= lexicographically
    smallest `leader_id`), then the lowest member-index tuple (=
    lexicographically smallest member-id tuple)."""
    return (
        candidate.score,
        -index_of[candidate.leader_id],
        tuple(-index_of[member_id] for member_id in candidate.member_ids),
    )


def solve_exact(
    cards: Iterable[Card],
    cfg: ScoringConfig,
    *,
    top_n: int = DEFAULT_TOP_N,
    progress: Progress | None = None,
    constraint: Constraint | None = None,
    set_constraint: SetConstraint | None = None,
    locked_ids: frozenset[str] = frozenset(),
    leader_id: str | None = None,
    leader_ids: frozenset[str] | None = None,
) -> tuple[RankedTeam, ...]:
    """Canonicalise the pool, then select the Top-N arrangements with
    `heapq.nlargest` over a generator — the generator is consumed directly
    by `nlargest`, never collected into a list or tuple first, which is
    what keeps memory bounded at `top_n` rather than at the full
    arrangement count. If `cards` holds fewer than `TEAM_SIZE` cards,
    `itertools.combinations` yields nothing and this returns an empty
    tuple; it never raises."""
    pool = canonical_pool(cards)
    index_of = {card.id: position for position, card in enumerate(pool)}
    return tuple(
        heapq.nlargest(
            top_n,
            iter_candidates(
                pool,
                cfg,
                progress=progress,
                constraint=constraint,
                set_constraint=set_constraint,
                locked_ids=locked_ids,
                leader_id=leader_id,
                leader_ids=leader_ids,
            ),
            key=lambda candidate: _heap_key(candidate, index_of),
        )
    )
