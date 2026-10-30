"""Slot order on a concrete song.

Deck slot order binds on ONE mechanism here:

* which of the chart's five `#mmm0B` cues fires each member's SPECIAL (from the
  client). This binds on *every* team.

The other half — which members a count-limited passive reaches — is **not
slot-dependent**: recipients are the `count` matching members with the greatest
TOTAL parameter, effect-independent and regardless of slot (the shipped `free` rule).
So there is **no stat-vs-cue tradeoff**:
a member can be pushed to the cue-optimal slot without losing a count-limited stat
buff it earns by total. `score_song_detailed` still walks the 120 orderings for the
SPECIAL cue, but the stat sum is order-invariant. These tests pin that the cue
optimum is reached at the full stat optimum, at no cost.
"""

from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.card import EffectTarget, Stats
from hbuilder.domain.scoring import _effective_stat_sum
from hbuilder.domain.song import ChartNote, SongChart, score_song_detailed

from conftest import make_card, make_scoring_config, make_skill

CERTAIN_TIERS = {"Low": 1.0, "Medium": 1.0, "High": 1.0}

BIG = Stats(performance=900, technique=900, sense=900)
SMALL = Stats(performance=10, technique=10, sense=10)

# Two notes: one before anything is live, one at 55 s where the ACTIVE's second
# window and the fifth SPECIAL cue both sit. Level 5 pins the music coefficient
# at exactly 1.0.
CHART = SongChart(
    music_id="m0001",
    difficulty="easy",
    difficulty_level=5,
    live_score_coefficient_permil=4,
    notes=(ChartNote(time=1.0, perfect_permil=1000), ChartNote(time=55.0, perfect_permil=1000)),
    special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
)


def _cfg():
    return make_scoring_config(probability_tiers=CERTAIN_TIERS)


def _members():
    """Five Cute members. `card-A` carries the huge SPECIAL *and* the biggest
    stats, which is what puts the two objectives in conflict: the passive wants
    it in the first two slots, the cue wants it in the fifth."""
    return (
        make_card(
            "card-A",
            stats=BIG,
            special=make_skill(
                "SPECIAL",
                family="conditional_score_up",
                effect_type="score",
                duration=10.0,
                magnitude=1000.0,
            ),
        ),
        make_card("card-B", stats=BIG),
        make_card(
            "card-C",
            stats=SMALL,
            passive=make_skill(
                "PASSIVE",
                family="stat_up",
                effect_type="all_stats",
                magnitude=100.0,
                target=EffectTarget(kind="type", value="Cute", count=2),
            ),
        ),
        make_card("card-D", stats=SMALL),
        make_card(
            "card-E",
            stats=SMALL,
            active=make_skill(
                "ACTIVE",
                family="conditional_score_up",
                effect_type="score",
                duration=10.0,
                interval=50.0,
                probability_tier="High",
                magnitude=50.0,
            ),
        ),
    )


def test_the_cue_optimum_is_reached_at_the_full_stat_optimum_no_tradeoff():
    """All five members are Cute and the passive's `count` is 2. Under the shipped
    rule the recipients are the two highest-TOTAL Cute — A and B (2700 each) — no
    matter where they sit, so the stat sum is 10890 in every ordering.

    **Stat sum.** Base `900x3 + 900x3 + 10x3*3 = 2700 + 2700 + 30 + 30 + 30 = 5490`;
    A and B doubled -> `5490 + 2700 + 2700 = 10890`, slot-invariant.

    **Score arithmetic.** `card-E`'s ACTIVE window [50, 60) covers the note at 55 s;
    `card-A`'s SPECIAL is +10.00 for 10 s, and the only cue reaching 55 s is the
    fifth (at 50 s), so the walk still wants A in slot 5. With
    `constant = power x 2.300 / 2.0 = 1.15 x power` and deck_power 10890:
        note 1  = 1.15 x 10890                 = 12523.5 -> 12524
        note 55 = 12523.5 x (1 + (1+10.0) x 0.50) = 12523.5 x 6.5 = 81402.75 -> 81403
        total                                                    = 93927

    Both optima are reached at once — there is no tradeoff, because slot order does
    not move the recipients."""
    members, leaders = _members(), (make_card("leader"),)
    best = score_song_detailed(members, leaders, CHART, _cfg())[0]

    # Under the shipped rule the count-2 recipients are the two highest-TOTAL Cute
    # (A and B) REGARDLESS of slot, so pushing A to slot 5 for the fifth cue costs
    # no stat sum — both optima are reached at once (the tradeoff the old slot rule
    # implied is gone). deck_power stays at the stat optimum 10890; the cue still
    # wants A in slot 5.
    assert best.member_order[4] == "card-A"
    assert best.deck_power == pytest.approx(10890.0)
    assert best.total_score == 93927


def test_the_per_song_stat_sum_equals_the_order_invariant_optimum():
    """The counterpart, computed through `scoring`'s own `_effective_stat_sum`:
    the stat sum for this member set is 10890 and is order-INVARIANT (recipients
    rank by total, so `_effective_stat_sum` chooses no order), and the per-song
    answer reports exactly that same 10890 while still claiming the cue bonus — no
    stat is sacrificed for the cue. If a regression reinstated slot-dependent
    recipients, the per-song deck_power would drop below the stat optimum and this
    test would catch it."""
    members = _members()
    cfg = _cfg()
    stat_entries = [
        (
            1.0,
            ("performance", "technique", "sense"),
            frozenset({"card-A", "card-B"}),
            EffectTarget(kind="type", value="Cute", count=2),
            "card-C",
        )
    ]
    stat_optimum, order = _effective_stat_sum(members, stat_entries, cfg)
    assert stat_optimum == pytest.approx(10890.0)
    # Order-invariant under the shipped rule: recipients rank by total, so no slot
    # order is chosen for the stat sum.
    assert order is None
    best = score_song_detailed(members, (make_card("leader"),), CHART, cfg)[0]
    # The per-song answer pays NO stat cost for the cue — both optima coincide.
    assert best.deck_power == pytest.approx(stat_optimum)


def test_when_no_count_limit_binds_the_stat_sum_is_order_invariant():
    """The O(5) precondition, on this path too. With the
    passive's `count` raised to 5 the matchers do not outnumber it, so every
    ordering selects all five members and the stat sum cannot move — the joint
    maximum collapses to the cue assignment alone, at no cost.

    Base 5490, every member doubled -> 10980, in all 120 orderings."""
    members = list(_members())
    members[2] = make_card(
        "card-C",
        stats=SMALL,
        passive=make_skill(
            "PASSIVE",
            family="stat_up",
            effect_type="all_stats",
            magnitude=100.0,
            target=EffectTarget(kind="type", value="Cute", count=5),
        ),
    )
    best = score_song_detailed(
        tuple(members), (make_card("leader"),), CHART, _cfg()
    )[0]
    assert best.deck_power == pytest.approx(10980.0)
    # Still pays the cue, because that half never stopped binding.
    assert best.member_order[4] == "card-A"


def test_the_retired_free_rule_leaves_the_cue_search_intact():
    """`cfg.slot_order_rule = "free"` retires the ordered *passive* rule (it is
    an upper bound on `best_order`, kept runnable under test). It must not also
    retire the SPECIAL cue search: the cue binding is a different attested
    mechanism and `"free"` says nothing about it.

    Under `"free"` every effect keeps the recipients `resolve_target_card_ids`
    picked for it in isolation — A and B, the top two by stat — so the stat sum
    is the unreachable 10890, and the cue is still landed."""
    cfg = dataclasses.replace(_cfg(), slot_order_rule="free")
    best = score_song_detailed(_members(), (make_card("leader"),), CHART, cfg)[0]
    assert best.deck_power == pytest.approx(10890.0)
    assert best.member_order[4] == "card-A"
