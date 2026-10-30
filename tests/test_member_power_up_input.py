"""`power_up_permyriad` is hand-typed, and a 25x typo can pass silently.

A `power_up_permyriad: 951` where the true value is **39** makes the Member
Power-Up Bonus read 15,410 where the game reads 623 on a 164,449 base (0.38 %).

⚠ **A range check alone does NOT catch it, and this is the point.** The
roster-reachable maximum over a 169-card catalogue, every card owned and
maxed, is **974 permyriad**. 951 is INSIDE it. The declared cap is 5,000,
wider still. So the range check here catches the order-of-magnitude class of
typo and is worth having, and the thing that actually catches 951 is the
second half: the value is DERIVABLE from the account's own cards, so the tool
computes it and says when the file disagrees.
"""

from __future__ import annotations

import pytest

from hbuilder.domain.collection import (
    MEMBER_POWER_UP_CAP_PERMYRIAD,
    CardState,
    CollectionError,
    StatBasis,
    account_bonuses_from_mapping,
    derived_member_power_up_permyriad,
)

# A two-group ladder in the real shape: cumulative permyriad AT a level, zero
# for the early levels, topping out at the per-card maximum (4/5/8 for
# 3*/4*/5* in master data).
LADDER_5 = {lvl: (0 if lvl < 40 else (lvl - 38) // 6) for lvl in range(1, 81)}
LADDER_4 = {lvl: (0 if lvl < 30 else (lvl - 28) // 9) for lvl in range(1, 71)}


def _basis() -> StatBasis:
    return StatBasis(
        level_mult={},
        bloom_permil={},
        cards={
            "five-a": ("grp-card-level-rarity_5-01", "pot-5", ()),
            "five-b": ("grp-card-level-rarity_5-01", "pot-5", ()),
            "four-a": ("grp-card-level-rarity_4-01", "pot-4", ()),
        },
        power_up_permyriad={
            "grp-card-level-rarity_5-01": LADDER_5,
            "grp-card-level-rarity_4-01": LADDER_4,
        },
    )


def _state(card_id: str, level: int | None, source: str = "manual") -> CardState:
    return CardState(
        card_id=card_id,
        level=level,
        bloom=0,
        limit_break=None,
        level_cap=None,
        max_bloom=None,
        source=source,
    )


# --------------------------------------------------------------------------
# The range check
# --------------------------------------------------------------------------


def test_a_power_up_permyriad_past_the_declared_cap_is_refused():
    """Refused, not clamped — the same call `player.level` already makes. A
    silently clamped 50000 would score as the maximum the mechanic allows on
    what is plainly a typo."""
    with pytest.raises(CollectionError):
        account_bonuses_from_mapping(
            {"player": {"power_up_permyriad": MEMBER_POWER_UP_CAP_PERMYRIAD + 1}}
        )


def test_a_power_up_permyriad_at_the_cap_is_accepted():
    bonuses = account_bonuses_from_mapping(
        {"player": {"power_up_permyriad": MEMBER_POWER_UP_CAP_PERMYRIAD}}
    )
    assert bonuses.power_up_permyriad == MEMBER_POWER_UP_CAP_PERMYRIAD


def test_the_range_check_does_not_catch_the_real_typo():
    """Recorded deliberately. 951 is a legal value; only the derivation below
    knows it is wrong for THIS account. A test that pretended otherwise would
    be false reassurance."""
    assert account_bonuses_from_mapping(
        {"player": {"power_up_permyriad": 951}}
    ).power_up_permyriad == 951


# --------------------------------------------------------------------------
# The derivation that does catch it
# --------------------------------------------------------------------------


def test_the_value_is_summed_over_owned_cards_at_their_own_levels():
    states = {"five-a": _state("five-a", 80), "four-a": _state("four-a", 70)}
    derived = derived_member_power_up_permyriad(
        _basis(), states, ("five-a", "four-a")
    )
    assert derived.permyriad == LADDER_5[80] + LADDER_4[70]
    assert derived.counted == 2
    assert derived.skipped == ()
    assert derived.exact
    # with nothing skipped the two bounds meet
    assert derived.upper == derived.permyriad


def test_an_unowned_card_contributes_nothing():
    """`P` is an account-wide sum over cards the player HAS. A catalogue card
    they do not own has bought them no power-up."""
    states = {"five-a": _state("five-a", 80), "five-b": _state("five-b", 80)}
    derived = derived_member_power_up_permyriad(_basis(), states, ("five-a",))
    assert derived.permyriad == LADDER_5[80]
    assert derived.counted == 1


def test_a_defaulted_level_is_SKIPPED_not_believed():
    """⚠ The trap: owned cards with no `state/collection.json` entry, under a
    `card_default` of `"max"`, have a level that RESOLVES (it is not `None`) to
    the cap and would feed the ladder's top rung — two such cards can turn a
    true 38 into 51. Provenance is what matters, not whether a number came out:
    only a level the player actually recorded may enter an account total."""
    states = {
        "five-a": _state("five-a", 80),
        "five-b": _state("five-b", 80, source="max"),
    }
    derived = derived_member_power_up_permyriad(
        _basis(), states, ("five-a", "five-b")
    )
    assert derived.permyriad == LADDER_5[80]
    assert derived.counted == 1
    assert derived.skipped == ("five-b",)
    assert not derived.exact


def test_a_card_with_no_level_at_all_is_skipped_too():
    states = {"five-a": _state("five-a", 80), "five-b": _state("five-b", None)}
    derived = derived_member_power_up_permyriad(
        _basis(), states, ("five-a", "five-b")
    )
    assert derived.skipped == ("five-b",)


def test_the_upper_bound_credits_every_skipped_card_its_best_case():
    """A skipped card has not vanished — it can contribute at most its ladder's
    top rung. Carrying that as an UPPER bound is what lets a partial derivation
    still refute a wrong value, instead of shrugging."""
    states = {
        "five-a": _state("five-a", 80),
        "five-b": _state("five-b", 80, source="max"),
    }
    derived = derived_member_power_up_permyriad(
        _basis(), states, ("five-a", "five-b")
    )
    assert derived.upper == LADDER_5[80] + LADDER_5[80]


def test_it_is_absent_rather_than_zero_without_a_curve_export():
    """No ladder means the tool does not know, which is not the same as
    "the account has earned none" — the distinction `AccountBonuses` is built
    on."""
    assert derived_member_power_up_permyriad(None, {}, ()) is None
    bare = StatBasis(level_mult={}, bloom_permil={}, cards={})
    assert derived_member_power_up_permyriad(bare, {}, ("five-a",)) is None


def test_an_exact_derivation_contradicts_a_different_typed_value():
    """The whole point: the tool can say "your file says 951, your cards say
    39" instead of scoring the 951."""
    states = {"five-a": _state("five-a", 80)}
    derived = derived_member_power_up_permyriad(_basis(), states, ("five-a",))
    assert derived.disagrees_with(951)
    assert not derived.disagrees_with(derived.permyriad)
    assert not derived.disagrees_with(None)


# --------------------------------------------------------------------------
# "auto": derived every run, so it can never go stale again
# --------------------------------------------------------------------------


def test_auto_reads_as_unset_until_a_derivation_fills_it():
    """`"auto"` is not a number and must not be coerced into one. It parses to
    `None` — the honest "not answered yet" — plus a flag saying the answer is to
    be computed rather than read."""
    bonuses = account_bonuses_from_mapping({"player": {"power_up_permyriad": "auto"}})
    assert bonuses.power_up_permyriad is None
    assert bonuses.power_up_permyriad_is_auto


def test_an_explicit_number_is_not_auto():
    bonuses = account_bonuses_from_mapping({"player": {"power_up_permyriad": 39}})
    assert bonuses.power_up_permyriad == 39
    assert not bonuses.power_up_permyriad_is_auto


def test_an_absent_field_is_not_auto_either():
    """Deliberately NOT auto. Absent has always meant "unset, do not score this
    term", and silently starting to score it would change every existing answer
    on a document nobody edited. `"auto"` is opt-in and says so on the page."""
    bonuses = account_bonuses_from_mapping({"player": {}})
    assert bonuses.power_up_permyriad is None
    assert not bonuses.power_up_permyriad_is_auto


def test_a_word_that_is_not_auto_is_still_refused():
    with pytest.raises(CollectionError):
        account_bonuses_from_mapping({"player": {"power_up_permyriad": "lots"}})


def test_a_partial_derivation_still_refutes_a_value_above_its_upper_bound():
    """The realistic case. Two cards are unrecorded, so the total
    is only bounded — but 951 is far outside `[lower, upper]` and the bound is
    enough to prove it wrong. Refusing to warn here because the derivation is
    partial would let the 25x typo survive the very check written for it."""
    states = {
        "five-a": _state("five-a", 80),
        "five-b": _state("five-b", 80, source="max"),
    }
    derived = derived_member_power_up_permyriad(
        _basis(), states, ("five-a", "five-b")
    )
    assert not derived.exact
    assert derived.disagrees_with(951)
    # ...and stays silent anywhere inside the bound, where it cannot know
    assert not derived.disagrees_with(derived.permyriad)
    assert not derived.disagrees_with(derived.upper)
