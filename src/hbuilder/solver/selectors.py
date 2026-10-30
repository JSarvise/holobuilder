"""Resolves `--require`/`--leader`/
`--exclude`/`--include-unowned` to a constrained candidate pool.

Pure — no I/O, no `argparse`, no `sys`, no `json`. This module receives
already-loaded card data (`cli.py`'s shared `load_inputs` owns every file
read) and produces a `CandidatePool` plus a `constraint` predicate that
plugs directly into `solver.exact.iter_candidates`/`solve_exact`'s existing
`constraint` parameter.

Every raised `SelectorError` follows the house `"{field}: {detail}"`
convention, naming the
offending flag and id, e.g. `"--require: unknown card id 'xyz'"`.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator

from hbuilder.domain.card import Card
from hbuilder.domain.team import MEMBER_COUNT


class SelectorError(ValueError):
    """Raised by `resolve_selectors` when `--require`/`--leader`/
    `--exclude` name an unknown id, a conflicting pair, or an unowned id
    without `--include-unowned`. The CLI maps this class to exit 5
    (`EXIT_INVALID_SELECTOR`)."""


@dataclass(frozen=True, slots=True)
class Selectors:
    """The parsed, not-yet-resolved selector flags. `require`/`exclude`/
    `leader` are already-deduplicated-by-the-CLI-or-not tuples
    (deduplication is `resolve_selectors`'s job, not the caller's).

    `leader` is a tuple even though at most one card can hold the leader
    slot: the flag is repeatable at the CLI, and collapsing it here rather
    than in argparse is what lets `--leader x --leader y` be *reported* as
    a conflict instead of silently resolving to the last occurrence."""

    require: tuple[str, ...]
    leader: tuple[str, ...]
    exclude: tuple[str, ...]
    include_unowned: bool


@dataclass(frozen=True, slots=True)
class CandidatePool:
    """The constrained, canonically-ordered (ascending `card.id`) result
    of applying `Selectors` to the loaded card collection.

    `locked_ids` is the union of `--require` and `--leader` — every id in
    it must appear in every yielded arrangement's combination.
    `leader_id` is `None` unless `--leader` was given, in which case every
    yielded arrangement's leader must be that card. `included_unowned_ids`
    is non-empty only when `--include-unowned` actually widened the final
    pool — it is computed from the pool that survives `--exclude`
    filtering, never from the wider set of ids merely named on the command
    line, so a `--exclude`'d unowned id (which never actually enters the
    pool) is never reported here as having widened anything."""

    cards: tuple[Card, ...]
    locked_ids: frozenset[str]
    leader_id: str | None
    included_unowned_ids: tuple[str, ...]


def _flagged_ids(
    require_ids: tuple[str, ...], leader_id: str | None, exclude_ids: tuple[str, ...]
) -> Iterator[tuple[str, str]]:
    """Yield `(flag, id)` pairs in a fixed, deterministic order — `--require`
    ids first (in the order given), then `--leader`, then `--exclude` ids
    (in the order given). Every check that must name "which flag" walks
    this same order, so the first violation reported is always the same
    one regardless of which check is running."""
    for card_id in require_ids:
        yield "--require", card_id
    if leader_id is not None:
        yield "--leader", leader_id
    for card_id in exclude_ids:
        yield "--exclude", card_id


def resolve_selectors(
    all_cards: Iterable[Card], owned_cards: Iterable[Card], sel: Selectors
) -> CandidatePool:
    """Apply `sel` to `all_cards`/`owned_cards`, raising `SelectorError` on
    the first violation, in this order:

    1. Deduplicate `require`/`exclude`/`leader` (repeating a flag with the
       same id is idempotent, not an error, so flags compose with shell
       loops). `--leader` additionally collapses to at most one id: two
       *different* ids cannot share the one leader slot, so that is a
       conflict naming both, never a last-one-wins.
    2. Every id named by `--require`/`--leader`/`--exclude` must exist in
       `all_cards`.
    3. An id named by both `--exclude` and either `--require` or
       `--leader` is a conflict, naming both flags.
    4. `--leader` and `--require` may name the same card (accepted, and
       counted once in `locked_ids`).
    5. An id named by any selector that is not in `owned_cards` is an
       invalid selector unless `sel.include_unowned` is set.
    6. Build the pool: start from `owned_cards`, add any
       `--include-unowned` cards named by a selector, remove every
       `--exclude` id, then canonicalise by ascending `card.id`.

    Fewer than `MEMBER_COUNT` cards surviving step 6 is **not** raised here —
    it is neither an input error nor a selector error but a valid, empty
    search space. The CLI is responsible for reporting that case as exit 6
    (`EXIT_INSUFFICIENT_POOL`); turning it into a `SelectorError` here
    would collapse two distinct exit codes into one.
    """
    all_by_id = {card.id: card for card in all_cards}
    owned_ids = {card.id for card in owned_cards}

    # Step 1: dedupe require/exclude/leader, preserving first-seen order.
    require_ids = tuple(dict.fromkeys(sel.require))
    exclude_ids = tuple(dict.fromkeys(sel.exclude))
    # Equal merges, different collides. Deduplicating FIRST is what keeps
    # `--leader x --leader x --leader y` a collision between x and y rather
    # than between x and itself.
    leader_ids = tuple(dict.fromkeys(sel.leader))
    if len(leader_ids) > 1:
        raise SelectorError(
            f"--leader: cards '{leader_ids[0]}' and '{leader_ids[1]}' cannot "
            f"both hold the leader slot"
        )
    leader_id = leader_ids[0] if leader_ids else None

    flagged = tuple(_flagged_ids(require_ids, leader_id, exclude_ids))

    # Step 2: every named id must exist in cards.json.
    for flag, card_id in flagged:
        if card_id not in all_by_id:
            raise SelectorError(f"{flag}: unknown card id '{card_id}'")

    # Step 3: --exclude may not name a card also named by --require/--leader.
    require_set = set(require_ids)
    for card_id in exclude_ids:
        if card_id in require_set:
            raise SelectorError(
                f"--exclude: card '{card_id}' is also named by --require"
            )
        if leader_id is not None and card_id == leader_id:
            raise SelectorError(
                f"--exclude: card '{card_id}' is also named by --leader"
            )

    # Step 4: locked_ids is the union of --require and --leader; a card
    # named by both is counted once.
    locked_ids = frozenset(
        require_set | ({leader_id} if leader_id is not None else set())
    )

    # Step 5: every named id must be owned, unless --include-unowned.
    if not sel.include_unowned:
        for flag, card_id in flagged:
            if card_id not in owned_ids:
                raise SelectorError(
                    f"{flag}: card '{card_id}' is not owned; pass "
                    f"--include-unowned to rank teams you cannot yet field"
                )

    # Step 6: build the pool.
    exclude_set = set(exclude_ids)
    if sel.include_unowned:
        admitted_unowned_ids = {
            card_id for _, card_id in flagged if card_id not in owned_ids
        }
    else:
        admitted_unowned_ids = set()

    pool_ids = (owned_ids | admitted_unowned_ids) - exclude_set
    cards = tuple(
        sorted((all_by_id[card_id] for card_id in pool_ids), key=lambda card: card.id)
    )
    included_unowned_ids = tuple(
        sorted(card_id for card_id in pool_ids if card_id not in owned_ids)
    )

    return CandidatePool(
        cards=cards,
        locked_ids=locked_ids,
        leader_id=leader_id,
        included_unowned_ids=included_unowned_ids,
    )


def constraint_from(
    pool: CandidatePool,
) -> Callable[[tuple, int], bool] | None:
    """The predicate `solver.exact.iter_candidates`/`solve_exact`'s
    existing `constraint` parameter accepts: `(combination, leader_index)
    -> bool`. An arrangement passes only when its combination contains
    every id in `pool.locked_ids`, and, when `pool.leader_id` is set, only
    when the card at `leader_index` is that card.

    Returns `None` when nothing is locked (`pool.locked_ids` is empty),
    so an unconstrained run pays no per-arrangement predicate call —
    `pool.locked_ids` is always non-empty whenever `pool.leader_id` is
    set (by construction in `resolve_selectors`), so this single check
    covers both the require-only and leader cases."""
    if not pool.locked_ids:
        return None

    locked_ids = pool.locked_ids
    leader_id = pool.leader_id

    def _constraint(members: tuple, leader) -> bool:
        # `--leader` constrains the OUTFIT SLOT, which is not one of the five,
        # so it is checked against the leader card itself rather than against
        # a position in the member tuple. A `--require`d card satisfies the
        # locked set from either role: as a member, or as the leader whose
        # outfit is applied.
        present = {card.id for card in members}
        if leader is not None:
            present.add(leader.id)
        if not locked_ids.issubset(present):
            return False
        if leader_id is not None and leader.id != leader_id:
            return False
        return True

    return _constraint


def set_constraint_from(pool: CandidatePool):
    """The SET-level half of `constraint_from`, for
    `solver.exact.iter_candidates`'s `set_constraint` parameter: given the five
    members, can this set still satisfy `pool.locked_ids` once a leader is
    chosen?

    This is the only predicate that can make a locked run genuinely cheaper.
    `constraint_from`'s predicate takes a (members, leader) pair, which the
    solver only has AFTER it has scored the whole set — so on its own it prunes
    the output while the enumeration still pays for every set, and the run
    costs what an unconstrained one does no matter what
    `project_arrangements` reported.

    Returns `None` when nothing is locked, so an unconstrained run pays no
    per-set call. Shares its rule with `iter_constrained_sets` below.
    """
    if not pool.locked_ids:
        return None

    locked_ids = pool.locked_ids
    leader_id = pool.leader_id

    def _set_constraint(members: tuple) -> bool:
        unmet = locked_ids - {card.id for card in members}
        if len(unmet) > 1:
            return False  # one outfit slot cannot cover two required cards
        if unmet and leader_id is not None and unmet != {leader_id}:
            return False  # the leader is pinned to a different card
        return True

    return _set_constraint


def iter_constrained_sets(pool: CandidatePool) -> Iterator[tuple]:
    """The pure member-set enumeration `constraint_from`'s set-level
    filtering is built from, exposed directly so tests read the same
    definition the predicate does rather than a second, parallel one. Yields
    every `MEMBER_COUNT`-card member combination from `pool.cards` that could
    still satisfy `pool.locked_ids` once a leader is chosen. Leader choice
    itself is not resolved here (that is `constraint_from`'s /
    `iter_candidates`'s job); this yields sets, not arrangements.

    **A locked id need not be among the members.** The leader slot sits
    outside the five, so `--require x` is satisfied
    either by x being a member or by x being the leader whose outfit is
    applied. A superset test against the member ids alone would wrongly
    reject every set that leaves a
    required card for the leader slot. There is exactly ONE leader slot, so
    at most one lock may be deferred to it, and if `--leader` pinned that
    slot the deferred lock has to be that same card."""
    leader_id = pool.leader_id
    for combo in itertools.combinations(pool.cards, MEMBER_COUNT):
        # Five distinct holomem, the same enumeration constraint
        # `iter_candidates` applies — a set that cannot be a team is not a
        # candidate set.
        if len({card.member for card in combo}) != MEMBER_COUNT:
            continue
        unmet = pool.locked_ids - {card.id for card in combo}
        if len(unmet) > 1:
            continue  # one outfit slot cannot cover two required cards
        if unmet and leader_id is not None and unmet != {leader_id}:
            continue  # the leader is pinned to a different card
        yield combo
