"""`--require`/`--leader`/`--exclude`/
`--include-unowned` resolve to a constrained `CandidatePool`, with every
conflict named and every adjacency/empty/ordering edge pinned.

`--require` locks a card into every candidate team as leader or member;
`--leader` is a separate, stronger flag locking a card to the leader slot
specifically (`--require` is never overloaded to mean it).
`--require`/`--exclude` are repeatable flags, composed via
idempotent deduplication rather than comma-splitting. An id present in
`cards.json` but absent from the owned subset is an invalid selector
unless `--include-unowned` widens the pool explicitly.
"""

from __future__ import annotations

import itertools
import math

import pytest

from hbuilder.solver.exact import iter_candidates
from hbuilder.solver.selectors import (
    CandidatePool,
    SelectorError,
    Selectors,
    constraint_from,
    iter_constrained_sets,
    resolve_selectors,
)

from conftest import make_card, make_scoring_config


def _six_cards() -> list:
    return [make_card(f"card_{i:02d}") for i in range(6)]


def _sel(
    *,
    require: tuple[str, ...] = (),
    leader: str | tuple[str, ...] | None = None,
    exclude: tuple[str, ...] = (),
    include_unowned: bool = False,
) -> Selectors:
    # `--leader` is repeatable at the CLI (argparse `append`), so `Selectors`
    # carries a tuple. A bare string here is the one-occurrence case.
    if leader is None:
        leader_ids: tuple[str, ...] = ()
    elif isinstance(leader, str):
        leader_ids = (leader,)
    else:
        leader_ids = leader
    return Selectors(
        require=require,
        leader=leader_ids,
        exclude=exclude,
        include_unowned=include_unowned,
    )


def test_duplicate_leader_flags_naming_the_same_card_are_idempotent():
    """Equal merges, exactly as a repeated
    `--require`/`--exclude` occurrence idempotent."""
    cards = _six_cards()
    sel = _sel(leader=("card_00", "card_00"))

    pool = resolve_selectors(cards, cards, sel)

    assert pool.leader_id == "card_00"
    assert pool.locked_ids == frozenset({"card_00"})


def test_two_different_leader_flags_collide_naming_both_ids():
    """Different collides. Two cards cannot both hold the one
    leader slot, and argparse's last-one-wins would otherwise discard the first
    silently."""
    cards = _six_cards()
    sel = _sel(leader=("card_00", "card_01"))

    with pytest.raises(SelectorError) as excinfo:
        resolve_selectors(cards, cards, sel)

    message = str(excinfo.value)
    assert "--leader" in message
    assert "card_00" in message and "card_01" in message


def test_three_leader_flags_with_one_odd_one_out_still_collide():
    """Deduplication must not mask a collision hiding among repeats."""
    cards = _six_cards()
    sel = _sel(leader=("card_00", "card_00", "card_02"))

    with pytest.raises(SelectorError) as excinfo:
        resolve_selectors(cards, cards, sel)

    assert "card_00" in str(excinfo.value) and "card_02" in str(excinfo.value)


def test_zero_leader_flags_leaves_the_leader_slot_unconstrained():
    """SOLV-06 empty."""
    cards = _six_cards()

    pool = resolve_selectors(cards, cards, _sel())

    assert pool.leader_id is None


def test_duplicate_require_flags_are_idempotent_not_an_error():
    cards = _six_cards()
    sel = _sel(require=("card_00", "card_00"))

    pool = resolve_selectors(cards, cards, sel)

    assert pool.locked_ids == frozenset({"card_00"})


def test_require_and_exclude_naming_the_same_card_raises_naming_both_flags():
    cards = _six_cards()
    sel = _sel(require=("card_00",), exclude=("card_00",))

    with pytest.raises(SelectorError) as excinfo:
        resolve_selectors(cards, cards, sel)

    message = str(excinfo.value)
    assert "--require" in message
    assert "--exclude" in message
    assert "card_00" in message


def test_leader_and_exclude_naming_the_same_card_raises_naming_both_flags():
    cards = _six_cards()
    sel = _sel(leader="card_00", exclude=("card_00",))

    with pytest.raises(SelectorError) as excinfo:
        resolve_selectors(cards, cards, sel)

    message = str(excinfo.value)
    assert "--leader" in message
    assert "--exclude" in message


def test_leader_plus_require_on_the_same_card_is_accepted_and_counted_once():
    cards = _six_cards()
    sel = _sel(require=("card_00",), leader="card_00")

    pool = resolve_selectors(cards, cards, sel)

    assert pool.locked_ids == frozenset({"card_00"})
    assert pool.leader_id == "card_00"


def test_two_different_leader_and_require_cards_both_lock():
    cards = _six_cards()
    sel = _sel(require=("card_01",), leader="card_00")

    pool = resolve_selectors(cards, cards, sel)

    assert pool.locked_ids == frozenset({"card_00", "card_01"})
    assert pool.leader_id == "card_00"


def test_unknown_require_id_raises_naming_the_flag_and_id():
    cards = _six_cards()
    sel = _sel(require=("nonexistent_99",))

    with pytest.raises(SelectorError) as excinfo:
        resolve_selectors(cards, cards, sel)

    message = str(excinfo.value)
    assert "--require" in message
    assert "nonexistent_99" in message


def test_unknown_leader_id_raises_naming_the_flag_and_id():
    cards = _six_cards()
    sel = _sel(leader="nonexistent_99")

    with pytest.raises(SelectorError) as excinfo:
        resolve_selectors(cards, cards, sel)

    message = str(excinfo.value)
    assert "--leader" in message
    assert "nonexistent_99" in message


def test_unknown_exclude_id_raises_naming_the_flag_and_id():
    cards = _six_cards()
    sel = _sel(exclude=("nonexistent_99",))

    with pytest.raises(SelectorError) as excinfo:
        resolve_selectors(cards, cards, sel)

    message = str(excinfo.value)
    assert "--exclude" in message
    assert "nonexistent_99" in message


def test_unowned_id_is_rejected_without_include_unowned():
    all_cards = _six_cards() + [make_card("unowned_01")]
    owned = _six_cards()
    sel = _sel(require=("unowned_01",))

    with pytest.raises(SelectorError) as excinfo:
        resolve_selectors(all_cards, owned, sel)

    message = str(excinfo.value)
    assert "--require" in message
    assert "unowned_01" in message
    assert "--include-unowned" in message


def test_unowned_id_is_accepted_with_include_unowned_and_recorded():
    all_cards = _six_cards() + [make_card("unowned_01")]
    owned = _six_cards()
    sel = _sel(require=("unowned_01",), include_unowned=True)

    pool = resolve_selectors(all_cards, owned, sel)

    pool_ids = {card.id for card in pool.cards}
    assert "unowned_01" in pool_ids
    assert pool.included_unowned_ids == ("unowned_01",)


def test_excluded_unowned_id_never_widens_the_pool_even_with_include_unowned():
    # A --exclude'd unowned id is admitted (per resolve_selectors' generic
    # per-selector ownership gate) and then immediately removed by the
    # --exclude filtering step — so it must never appear in
    # included_unowned_ids, which only reports ids that actually survive
    # into the final pool.
    all_cards = _six_cards() + [make_card("unowned_01")]
    owned = _six_cards()
    sel = _sel(exclude=("unowned_01",), include_unowned=True)

    pool = resolve_selectors(all_cards, owned, sel)

    pool_ids = {card.id for card in pool.cards}
    assert "unowned_01" not in pool_ids
    assert pool.included_unowned_ids == ()


def test_no_require_leaves_the_pool_unconstrained():
    cards = _six_cards()
    sel = _sel()

    pool = resolve_selectors(cards, cards, sel)

    assert pool.locked_ids == frozenset()
    assert pool.leader_id is None
    assert {card.id for card in pool.cards} == {card.id for card in cards}


def test_require_naming_all_six_owned_cards_yields_six_sets_and_six_arrangements():
    """Six locks over a five-member team is satisfiable exactly because the
    leader sits outside the five: five cards are members and the sixth wears
    the outfit. Each of the six member-sets defers a different card to the
    leader slot, so each admits exactly one arrangement."""
    cards = _six_cards()
    sel = _sel(require=tuple(card.id for card in cards))
    cfg = make_scoring_config()

    pool = resolve_selectors(cards, cards, sel)

    sets = list(iter_constrained_sets(pool))
    assert len(sets) == 6

    arrangements = list(
        iter_candidates(pool.cards, cfg, constraint=constraint_from(pool))
    )
    assert len(arrangements) == 6


def test_leader_fixes_the_leader_so_each_set_yields_exactly_one_arrangement():
    cards = [make_card(f"card_{i:02d}") for i in range(7)]
    sel = _sel(leader="card_00")
    cfg = make_scoring_config()

    pool = resolve_selectors(cards, cards, sel)

    arrangements = list(
        iter_candidates(pool.cards, cfg, constraint=constraint_from(pool))
    )
    # Every 5 of the 7 may be the member set -- INCLUDING sets containing
    # card_00 itself, since the leader may also be a member.
    assert len(arrangements) == math.comb(7, 5)
    assert all(candidate.leader_id == "card_00" for candidate in arrangements)


def test_exclude_against_the_shipped_pool_leaves_five_candidates_that_still_solve():
    """Five cards are exactly enough: they are the five members, and the
    leader is drawn from that same pool."""
    cards = _six_cards()
    sel = _sel(exclude=(cards[0].id,))
    cfg = make_scoring_config()

    pool = resolve_selectors(cards, cards, sel)

    assert len(pool.cards) == 5
    arrangements = list(
        iter_candidates(pool.cards, cfg, constraint=constraint_from(pool))
    )
    assert len(arrangements) == 5, "one member set, five leader choices"


def test_no_exclude_leaves_the_pool_unconstrained():
    cards = _six_cards()
    sel = _sel()

    pool = resolve_selectors(cards, cards, sel)

    assert {card.id for card in pool.cards} == {card.id for card in cards}


def test_flag_order_does_not_change_the_resolved_pool_or_ranked_output():
    cards = [make_card(f"card_{i:02d}") for i in range(8)]
    cfg = make_scoring_config()

    sel_forward = _sel(require=("card_00", "card_01"))
    sel_backward = _sel(require=("card_01", "card_00"))

    pool_forward = resolve_selectors(cards, cards, sel_forward)
    pool_backward = resolve_selectors(cards, cards, sel_backward)

    assert pool_forward.locked_ids == pool_backward.locked_ids
    assert pool_forward.cards == pool_backward.cards

    result_forward = list(
        iter_candidates(pool_forward.cards, cfg, constraint=constraint_from(pool_forward))
    )
    result_backward = list(
        iter_candidates(pool_backward.cards, cfg, constraint=constraint_from(pool_backward))
    )
    assert result_forward == result_backward


def test_constraint_from_returns_none_when_nothing_is_locked():
    cards = _six_cards()
    sel = _sel()

    pool = resolve_selectors(cards, cards, sel)

    assert constraint_from(pool) is None


def test_iter_constrained_sets_defers_at_most_one_lock_to_the_leader_slot():
    """A locked card may be a member OR the leader, so a member set is
    still viable while it is missing at most one lock. Missing two is not:
    there is only one outfit slot."""
    cards = [make_card(f"card_{i:02d}") for i in range(8)]
    sel = _sel(require=("card_00", "card_01"))

    pool = resolve_selectors(cards, cards, sel)

    sets = list(iter_constrained_sets(pool))

    for combo in sets:
        unmet = {"card_00", "card_01"} - {card.id for card in combo}
        assert len(unmet) <= 1, (
            "a set missing BOTH locked cards can never be completed: the one "
            "leader slot cannot wear two outfits"
        )

    # Both locks as members: choose the remaining 3 from the other 6.
    # Exactly one lock deferred: 2 choices of which, times the remaining 4
    # members from the other 6.
    both_as_members = math.comb(6, 3)
    one_deferred = 2 * math.comb(6, 4)
    assert len(sets) == both_as_members + one_deferred


def test_iter_constrained_sets_will_not_defer_a_lock_the_leader_flag_already_pinned():
    """If --leader pins the outfit slot to card_00, a set missing card_01
    cannot defer card_01 to that slot — it is taken."""
    cards = [make_card(f"card_{i:02d}") for i in range(8)]
    sel = _sel(require=("card_01",), leader="card_00")

    pool = resolve_selectors(cards, cards, sel)

    for combo in iter_constrained_sets(pool):
        assert "card_01" in {card.id for card in combo}
