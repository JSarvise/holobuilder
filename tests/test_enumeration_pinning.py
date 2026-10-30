"""The lock-injected enumeration must be an OPTIMISATION, not a different
search — and `project_arrangements` must count what it actually produces.

Two properties, pinned against an independent reference rather than against
each other:

1. **Identical arrangements.** `solver.exact.iter_candidates` builds locked
   runs by choosing the free slots from the unlocked pool and splicing the
   locks in, and narrows the leader vector before `score_card_set` rather than
   discarding results after it. Both are cost changes; either one getting the
   splice or the narrowing wrong silently changes the ANSWER. The reference
   here is the dumb definition — walk every `C(N,5)`, pair it with every leader
   in the pool, keep what satisfies the locks — written out inline so it cannot
   drift toward the implementation it checks.

2. **The projection matches the enumeration.** `estimate.project_arrangements`
   is a closed form for the same quantity; counting only the family where every
   lock is a member would under-count a real mode-B run by 29.7x. Comparing the formula against a real enumeration is the only check
   that catches that class of error; comparing it against itself never will.

Both use `make_card`'s default per-id holomem, so R1 (five distinct holomem)
never fires and the closed form is exactly reachable. R1's own effect on the
count is pool-data-dependent and deliberately outside the formula — see
`project_arrangements`.
"""

from __future__ import annotations

import itertools
import math

import pytest

from hbuilder.solver.estimate import project_arrangements
from hbuilder.solver.exact import _iter_member_sets, canonical_pool, iter_candidates
from hbuilder.solver.selectors import (
    Selectors,
    constraint_from,
    resolve_selectors,
    set_constraint_from,
)

from conftest import make_card, make_scoring_config

POOL_SIZE = 9
IDS = tuple(f"card_{i:02d}" for i in range(POOL_SIZE))


def _pool():
    return [make_card(card_id) for card_id in IDS]


def _resolved(require=(), leader=(), exclude=()):
    cards = _pool()
    return cards, resolve_selectors(
        cards,
        cards,
        Selectors(
            require=require, leader=leader, exclude=exclude, include_unowned=False
        ),
    )


def _reference(pool, locked_ids, leader_id, cfg):
    """The definition, walked exhaustively: every 5-set from the whole pool,
    against every leader in the whole pool, keeping the pairs whose union of
    ids covers every lock (a lock may be satisfied from either role) and whose
    leader is the pinned one when there is one."""
    from hbuilder.domain.scoring import score_card_set

    out = set()
    for members in itertools.combinations(pool, 5):
        if len({card.member for card in members}) != 5:
            continue
        scores = score_card_set(members, tuple(pool), cfg)
        for index, leader in enumerate(pool):
            present = {card.id for card in members} | {leader.id}
            if not locked_ids.issubset(present):
                continue
            if leader_id is not None and leader.id != leader_id:
                continue
            out.add(
                (
                    leader.id,
                    tuple(card.id for card in members),
                    scores[index].total_score,
                )
            )
    return out


def _enumerated(candidate_pool, cfg, *, pin_leader=False):
    """`pin_leader=False` is the path without `leader_id=`
    — the enumerator learns the pin only from `constraint`. `pin_leader=True`
    additionally hands it `leader_id`, which is the optimisation under test.
    Every assertion that the two agree is therefore against the unchanged
    code path, not against a re-derivation of it."""
    pool = canonical_pool(candidate_pool.cards)
    results = list(
        iter_candidates(
            pool,
            cfg,
            constraint=constraint_from(candidate_pool),
            set_constraint=set_constraint_from(candidate_pool),
            locked_ids=candidate_pool.locked_ids,
            leader_id=candidate_pool.leader_id if pin_leader else None,
        )
    )
    as_triples = [(r.leader_id, r.member_ids, r.score) for r in results]
    assert len(as_triples) == len(set(as_triples)), "an arrangement was yielded twice"
    return set(as_triples), results


# `--leader` is a tuple at this layer because the flag is repeatable.
LOCK_CASES = [
    pytest.param((), (), (), id="no-locks"),
    pytest.param(("card_03",), (), (), id="require-one"),
    pytest.param(("card_01", "card_04"), (), (), id="require-two"),
    pytest.param((), ("card_02",), (), id="leader-only"),
    pytest.param(("card_05",), ("card_02",), (), id="leader-plus-require-one"),
    pytest.param(
        ("card_05", "card_07"), ("card_02",), (), id="leader-plus-require-two"
    ),
    pytest.param(("card_00", "card_03", "card_06"), (), (), id="require-three"),
    pytest.param(("card_03",), ("card_03",), (), id="leader-is-also-required"),
    pytest.param(("card_01",), (), ("card_08",), id="require-plus-exclude"),
    pytest.param(
        ("card_00", "card_01", "card_02", "card_03", "card_04"),
        ("card_06",),
        (),
        id="six-locks-five-members-plus-outfit",
    ),
]


@pytest.mark.parametrize("require,leader,exclude", LOCK_CASES)
def test_lock_injection_yields_exactly_the_reference_arrangement_set(
    require, leader, exclude
):
    cfg = make_scoring_config()
    _cards, pool = _resolved(require, leader, exclude)

    enumerated, _ = _enumerated(pool, cfg)
    reference = _reference(
        canonical_pool(pool.cards), pool.locked_ids, pool.leader_id, cfg
    )

    assert enumerated == reference, (
        "the injected enumeration is not the same search as the definition — "
        f"only in injected: {sorted(enumerated - reference)[:3]}, "
        f"only in reference: {sorted(reference - enumerated)[:3]}"
    )


@pytest.mark.parametrize("require,leader,exclude", LOCK_CASES)
def test_pinning_the_leader_id_changes_the_walk_and_not_the_answer(
    require, leader, exclude
):
    """`leader_id=` is an ENUMERATION narrowing, so the only thing that proves
    it safe is the output — compared against the same call without it, and
    against the inline brute-force reference, on every lock shape including the
    ones with no leader at all (where it must be a no-op)."""
    cfg = make_scoring_config()
    _cards, pool = _resolved(require, leader, exclude)

    unpinned, _ = _enumerated(pool, cfg, pin_leader=False)
    pinned, _ = _enumerated(pool, cfg, pin_leader=True)
    reference = _reference(
        canonical_pool(pool.cards), pool.locked_ids, pool.leader_id, cfg
    )

    assert pinned == unpinned, (
        "leader_id= dropped or invented an arrangement relative to the path "
        f"that learns the pin only from `constraint` — only pinned: "
        f"{sorted(pinned - unpinned)[:3]}, only unpinned: "
        f"{sorted(unpinned - pinned)[:3]}"
    )
    assert pinned == reference


@pytest.mark.parametrize("require,leader,exclude", LOCK_CASES)
def test_the_sets_leader_id_stops_walking_are_exactly_the_unemittable_ones(
    require, leader, exclude
):
    """The narrowing's actual claim, stated as a set relation rather than as a
    saving: every member set `leader_id=` removes from the walk is one the
    UNCHANGED path built, rejected, and never emitted an arrangement for.

    A family-B set is missing exactly one lock, so its outfit slot is forced to
    be that missing card; when the slot is pinned elsewhere, `constraint`
    rejects every leader for it. Comparing counts would not catch dropping a
    set that did emit — comparing membership does."""
    cfg = make_scoring_config()
    _cards, pool = _resolved(require, leader, exclude)
    canonical = canonical_pool(pool.cards)

    walked_unpinned = [
        tuple(card.id for card in members)
        for members in _iter_member_sets(canonical, pool.locked_ids, None)
    ]
    walked_pinned = [
        tuple(card.id for card in members)
        for members in _iter_member_sets(canonical, pool.locked_ids, pool.leader_id)
    ]
    _, results = _enumerated(pool, cfg, pin_leader=False)
    emitted = {candidate.member_ids for candidate in results}

    dropped = set(walked_unpinned) - set(walked_pinned)
    assert dropped.isdisjoint(emitted), (
        f"leader_id= stopped walking {len(dropped & emitted)} set(s) that the "
        f"unchanged path actually emitted arrangements for: "
        f"{sorted(dropped & emitted)[:3]}"
    )
    assert emitted.issubset(set(walked_pinned)), "an emitted set is no longer walked"
    # The walk never gets LONGER, and it never repeats a set it did not repeat
    # before (family A and family B stay disjoint).
    assert len(walked_pinned) <= len(walked_unpinned)
    assert len(set(walked_pinned)) == len(walked_pinned)


def test_the_narrowing_is_worth_a_pinned_number_of_sets_not_merely_worth_something():
    """On a real 91-card pool at the mode-B shape (`--leader` plus
    two `--require`, k=3): 219,472 sets walked and rejected, ~0.3 s of 23.7 s
    (1.4%). The same closed form, `(k-1) * C(N-k, 6-k)`, on the 9-card pool
    here: 2 * C(6,3) = 40 of 60 family-B sets, leaving 20.

    Pinned as exact integers on both sides so a regression that quietly stops
    narrowing (40 -> 0) or over-narrows (40 -> 60) fails rather than passes."""
    _cards, pool = _resolved(require=("card_05", "card_07"), leader=("card_02",))
    canonical = canonical_pool(pool.cards)
    assert (len(pool.cards), len(pool.locked_ids)) == (9, 3)

    unpinned = list(_iter_member_sets(canonical, pool.locked_ids, None))
    pinned = list(_iter_member_sets(canonical, pool.locked_ids, pool.leader_id))

    # Family A is C(6,2) = 15 sets; family B is 3 deferrals * C(6,3) = 60.
    assert len(unpinned) == 15 + 60 == 75
    assert len(pinned) == 15 + 20 == 35
    assert len(unpinned) - len(pinned) == 40

    # And the same 219,472/23.7 s arithmetic at the real shape, from the same
    # closed form rather than from a second one.
    assert 2 * math.comb(88, 3) == 219_472


def test_leader_id_alone_honours_the_pin_with_no_constraint_supplied():
    """`locked_ids` alone cannot say which lock a family-B set may defer, so
    before this parameter existed the only thing that enforced a pin inside the
    enumeration was `constraint`. `leader_id=` must be sufficient on its own —
    otherwise it is a speed knob that can disagree with the answer."""
    pool = canonical_pool(_pool())
    locked = frozenset({"card_02", "card_05"})

    results = list(
        iter_candidates(
            pool, make_scoring_config(), locked_ids=locked, leader_id="card_02"
        )
    )

    assert results
    assert {candidate.leader_id for candidate in results} == {"card_02"}
    for candidate in results:
        present = set(candidate.member_ids) | {candidate.leader_id}
        assert locked.issubset(present)
    # card_05 can never be the deferred one, so it is a member of every set.
    assert all("card_05" in candidate.member_ids for candidate in results)


def test_a_leader_id_outside_the_pool_enumerates_nothing_rather_than_everything():
    """The pin cannot be satisfied, so the honest answer is no arrangements.
    Reachable only from a direct caller — `resolve_selectors` guarantees the
    pinned card is in the pool — but "narrow to a card that is not there" must
    not silently fall back to "do not narrow"."""
    pool = canonical_pool(_pool())

    results = list(
        iter_candidates(pool, make_scoring_config(), leader_id="card_99_absent")
    )

    assert results == []


@pytest.mark.parametrize("require,leader,exclude", LOCK_CASES)
def test_projection_matches_the_sets_and_arrangements_actually_enumerated(
    require, leader, exclude
):
    cfg = make_scoring_config()
    _cards, pool = _resolved(require, leader, exclude)

    _, results = _enumerated(pool, cfg)
    real_sets = len({r.member_ids for r in results})
    real_arrangements = len(results)

    projected_sets, projected_arrangements = project_arrangements(
        len(pool.cards), len(pool.locked_ids), pool.leader_id is not None
    )

    assert (projected_sets, int(projected_arrangements)) == (
        real_sets,
        real_arrangements,
    ), (
        "project_arrangements is the number --estimate prints and the "
        "confirmation gate decides on; it must equal what the solver does"
    )


def test_a_lock_may_be_satisfied_by_the_outfit_slot_rather_than_a_member():
    """Family B is the half the old projection missed entirely. `--require x`
    is satisfied by x being the LEADER, so sets that omit x are candidates —
    and with the leader pinned to x they are the majority of the run."""
    cfg = make_scoring_config()
    _cards, pool = _resolved(require=("card_03",), leader=("card_03",))

    _, results = _enumerated(pool, cfg)

    deferred = {r.member_ids for r in results if "card_03" not in r.member_ids}
    assert deferred, "no set deferred the lock to the outfit slot"
    assert len(deferred) == 56, "C(8,5) sets omit card_03 entirely"
    assert all(r.leader_id == "card_03" for r in results)


def test_a_deferred_lock_admits_exactly_one_leader_even_with_no_constraint():
    """`locked_ids` alone is enough to know it: a set missing a lock can only
    get it from the outfit slot, so that slot IS the missing card. Passing
    `locked_ids` without a `constraint` used to emit every leader for such a
    set — arrangements that violate the very locks they were enumerated for."""
    pool = canonical_pool(_pool())
    locked = frozenset({"card_02"})

    results = list(iter_candidates(pool, make_scoring_config(), locked_ids=locked))

    assert results
    for candidate in results:
        assert "card_02" in set(candidate.member_ids) | {candidate.leader_id}


def test_the_leader_vector_is_narrowed_before_scoring_not_after(monkeypatch):
    """`score_card_set` costs one `combine()` per candidate leader, so handing
    it the whole pool and dropping 8 of 9 results afterwards pays for all nine.
    Measured on the real 91-card pool that is 12.2x, thrown away on every
    `--leader` run. Counted here rather than timed, so it fails
    deterministically."""
    from hbuilder.solver import exact as exact_mod

    cfg = make_scoring_config()
    _cards, pool = _resolved(require=("card_05",), leader=("card_02",))
    canonical = canonical_pool(pool.cards)

    widths: list[int] = []
    real = exact_mod.score_card_set

    def counting(members, leaders, config):
        widths.append(len(leaders))
        return real(members, leaders, config)

    monkeypatch.setattr(exact_mod, "score_card_set", counting)

    list(
        iter_candidates(
            canonical,
            cfg,
            constraint=constraint_from(pool),
            set_constraint=set_constraint_from(pool),
            locked_ids=pool.locked_ids,
        )
    )

    assert widths, "nothing was scored"
    assert set(widths) == {1}, (
        f"score_card_set was handed leader vectors of widths {sorted(set(widths))} "
        "for a run whose leader is pinned to one card"
    )
