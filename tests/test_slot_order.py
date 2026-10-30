"""Deck slot order and count-limited targets.

A count-limited effect reaches the `count` matching members with the greatest
base value of the stat the effect raises (total parameter for an all-stats
effect), tie broken by slot order — NOT the first `count` in slot order. This
matches the server on every measured row. Slot order therefore does not change
recipients except on an exact tie (`member_order` is `None`). This is the
SHIPPED rule, `slot_order_rule="free"`.

The machine code `Vision.LiveCore!LiveSkillUtility::IsSatisfyEffectTarget*Condition`
(@ rva `0x5f03d70` / `0x5f03f50` / `0x5f04140`) counts matchers "at or before the
candidate in the member list" and returns `count <= targetCount` — first-`count`-
in-LIST-order — but the list it walks is PRE-SORTED, which is why the observed
recipients are the top `count` by the ranking value. The refuted slot-search
alternative is kept runnable as `slot_order_rule="best_order"`.

The tests below that exercise the slot-search machinery pin it explicitly via
`best_order(cfg)`; the rest assert the shipped rule.

**What this is worth, said plainly, because a test that pins a number should
also say how much to trust it.** The ordering swing over many charts is a
median 1–3 % of score, and the count-limited passive term is the smaller half
of that — a fraction of a percent on a real collection. The active-skill RNG is
worth ~20 % run to run (same song and team). **So slot order is far below the
observational noise of a single play and could never be confirmed by playing
once.** It is nonetheless fully deterministic in the model, identical for every
run of a given team, and therefore it moves rankings — which is exactly the kind
of term a ranking must not drop, and exactly the kind of term that must never be
described to the user as something they could go and verify.

Every pinned number below is derived by hand in its own docstring and asserted
against both rules, so a regression that silently swaps the rule fails rather
than passing at a plausible-looking value.
"""

from __future__ import annotations

import collections
import dataclasses
import itertools
import json
import random
import statistics

import pytest

from conftest import (
    FIXTURE_CARDS_PATH,
    make_card,
    make_scoring_config,
    make_skill,
    make_team,
)
from hbuilder.domain.card import EffectTarget, Stats, card_from_record
from hbuilder.domain.scoring import (
    SLOT_ORDER_RESOLVERS,
    _ordered_target_card_ids,
    score_card_set,
    score_fast,
    score_with_breakdown,
)
from hbuilder.domain.scoring_config import SLOT_ORDER_RULES
from hbuilder.domain.team import Team

def read_cards(path) -> tuple:
    """`card_from_record` over a cards.json document, deliberately NOT through
    `domain.loader.load_cards`.

    Not a style preference: these tests are about scoring arithmetic, not about
    load-time validation — which every other card in the suite already covers.
    Coupling a slot-order assertion to a loader signature would make this file
    fail for a reason that has nothing to do with slot order. The records are
    the shipped, already-validated ones."""
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    return tuple(card_from_record(record) for record in document["cards"])


def stat_passive(effect_type: str, target: object, magnitude: float = 50.0):
    """A PASSIVE `stat_up` slot — always on, gated only by its condition, so
    its rate is exactly `magnitude / 100` and nothing about activation
    probability or uptime enters any number in this module."""
    return make_skill(
        "PASSIVE",
        family="stat_up",
        effect_type=effect_type,
        magnitude=magnitude,
        target=target,
    )


def cute(count: int) -> EffectTarget:
    return EffectTarget(kind="type", value="Cute", count=count)


def free(cfg):
    """The SHIPPED rule (matches every measured server row): every effect keeps
    the recipients `targets.resolve_target_card_ids` ranks for it by the base
    value of the stat the effect raises (total parameter for an all-stats
    effect). Each effect ranks on its own stat, so two effects can reach
    different members, and `member_order` is `None` — slot order only breaks an
    exact tie. This is also the DEFAULT (`make_scoring_config`), so `free(cfg)`
    equals a bare `cfg`; kept as a named helper to contrast with the refuted
    alternative."""
    return dataclasses.replace(cfg, slot_order_rule="free")


def best_order(cfg):
    """The REFUTED alternative, kept runnable: assumes the player picks recipients
    via slot order (first `count` in slot order), so a set scores at the max over
    its 120 orderings. Server measurements refute it — recipients are the top
    `count` by the ranking value regardless of slot. Pinned explicitly where a
    test exercises the slot-search machinery."""
    return dataclasses.replace(cfg, slot_order_rule="best_order")


# ---------------------------------------------------------------------------
# 1. The machine-code rule itself
# ---------------------------------------------------------------------------


def test_first_count_matchers_in_slot_order_receive_a_count_limited_effect():
    """The rule, read off the machine code: walk the deck IN SLOT ORDER, count
    matchers up to and including the candidate, keep it while
    `count <= targetCount`.

    Five members, three of them Cute (A, B, C), `targetCount = 2`. In the order
    (C, D, A, E, B) the Cute members appear at ranks 1 (C), 2 (A), 3 (B), so C
    and A are kept and B is not. Reversing the deck reverses which two.
    """
    a = make_card("A", type_="Cute")
    b = make_card("B", type_="Cute")
    c = make_card("C", type_="Cute")
    d = make_card("D", type_="Cool")
    e = make_card("E", type_="Cool")

    assert _ordered_target_card_ids(
        cute(2), "SRC", (c, d, a, e, b), "apply_to_available"
    ) == frozenset({"C", "A"})
    assert _ordered_target_card_ids(
        cute(2), "SRC", (b, e, a, d, c), "apply_to_available"
    ) == frozenset({"B", "A"})
    # A non-matching member never receives it however early it sits.
    assert _ordered_target_card_ids(
        cute(2), "SRC", (d, e, a, b, c), "apply_to_available"
    ) == frozenset({"A", "B"})


def test_target_count_zero_self_and_team_wide_are_order_invariant():
    """The three shapes that carry no count are the three the game returns on
    before ever entering the counting loop, and they are why leader and outfit
    skills are order-invariant: all 175 `LiveLeaderSkill` effect refs resolve to
    `targetCount = 0`."""
    a = make_card("A", type_="Cute")
    b = make_card("B", type_="Cute")
    c = make_card("C", type_="Cool")
    for ordering in ((a, b, c), (c, b, a), (b, a, c)):
        # `count == 0` is the game's own "no limit" early return.
        assert _ordered_target_card_ids(
            EffectTarget(kind="type", value="Cute", count=0),
            "SRC",
            ordering,
            "apply_to_available",
        ) == frozenset({"A", "B"})
        # Structurally team-wide.
        assert _ordered_target_card_ids(
            None, "SRC", ordering, "apply_to_available"
        ) == frozenset({"A", "B", "C"})
        # Self is the source card, never resolved against the deck at all.
        assert _ordered_target_card_ids(
            EffectTarget(kind="self", value=None, count=None),
            "SRC",
            ordering,
            "apply_to_available",
        ) == frozenset({"SRC"})


def test_under_supply_stays_the_configured_rule_on_the_ordered_path():
    """The machine code contains **no under-supply check at all** — it counts
    matchers and compares, so every matcher passes when fewer of them exist than
    `targetCount`. That is `"apply_to_available"`, the shipped default, and it
    means the machine code **refutes `"do_not_apply"`**.

    Retiring `"do_not_apply"` is a separate decision with its own blast radius
    (`config.json`, the assumptions header). This test pins that the knob is
    still honoured, identically, on both paths."""
    a = make_card("A", type_="Cute")
    b = make_card("B", type_="Cool")
    ordering = (a, b)
    assert _ordered_target_card_ids(
        cute(3), "SRC", ordering, "apply_to_available"
    ) == frozenset({"A"})
    assert _ordered_target_card_ids(
        cute(3), "SRC", ordering, "do_not_apply"
    ) == frozenset()


# ---------------------------------------------------------------------------
# 2. The crux: separability fails, so this is a search and not a table lookup
# ---------------------------------------------------------------------------


def test_a_cards_contribution_is_not_a_function_of_the_card_and_its_own_slot():
    """**A separable `w[card][slot]` table does not exist for the count-limited
    passive term.** Precomputing a per-(card, slot) contribution and solving a
    5x5 linear assignment is sound for the SPECIAL cue term, where a card's
    window depends only on the card and the chart. It is false here, because `count <= targetCount`
    counts *matching members before the candidate* — a property of the whole
    prefix, not of the pair (card, slot).

    Concretely: A is Cute, B is Cute, the effect is `type=Cute, count=1`, and A
    sits in slot 3 in **both** decks below. Whether A receives the buff is
    decided entirely by where B went. `w[A][3]` is therefore not a number.

    (So there are two recipes: an assignment for SPECIAL and a search over the
    120 permutations for passives, and only the second applies here.)
    """
    a = make_card("A", type_="Cute")
    b = make_card("B", type_="Cute")
    c = make_card("C", type_="Cool")
    d = make_card("D", type_="Cool")
    e = make_card("E", type_="Cool")

    b_late = (c, d, a, b, e)  # A at slot 3, B at slot 4
    b_early = (b, d, a, c, e)  # A at slot 3, B at slot 1
    assert b_late[2] is a and b_early[2] is a

    assert _ordered_target_card_ids(
        cute(1), "SRC", b_late, "apply_to_available"
    ) == frozenset({"A"})
    assert _ordered_target_card_ids(
        cute(1), "SRC", b_early, "apply_to_available"
    ) == frozenset({"B"})


def test_two_count_limited_effects_cannot_each_have_their_own_best_order():
    """The behaviour change, with every number derived by hand.

    Members (all stats out of `Stats(performance, technique, sense)`):

        A  Cute  (300, 100, 100)      D  Cool  (100, 100, 100)
        B  Cute  (100, 300, 100)      E  Cool  (100, 100, 100)
        C  Cute  (100, 100, 100)

    D carries a PASSIVE `stat_up` raising **performance** by 50 %, targeting
    `type=Cute, count=1`. E carries the same but raising **technique**. Both are
    always-on, so each rate is exactly `50/100 = 0.5`, family weight 1.0,
    additive stacking. No score-pool or board term exists, so
    `total_score == effective_stat_sum`.

    **The shipped rule (`"free"`)** resolves the two effects independently, each
    on the stat it raises. The performance effect ranks the three Cute members by
    performance and takes the top 1 -> A. The technique effect ranks them by
    technique -> B:

        A: 300*1.5 + 100     + 100 = 650
        B: 100     + 300*1.5 + 100 = 650
        C, D, E:                     300 each
        total                      = 2200.0

    This is what the server does.

    **The slot-order alternative (`"best_order"`)** has both effects read ONE
    slot order; both have `count = 1`, so both land on whichever Cute member sits
    earliest:

        first Cute = A:  A = 300*1.5 + 100*1.5 + 100 = 700, B = 500  -> 2100
        first Cute = B:  B = 100*1.5 + 300*1.5 + 100 = 700, A = 500  -> 2100
        first Cute = C:  C = 100*1.5 + 100*1.5 + 100 = 400           -> 2000

    so it tops out at **2100.0**, 100.0 below what the server pays.
    """
    a = make_card("A", type_="Cute", stats=Stats(300, 100, 100))
    b = make_card("B", type_="Cute", stats=Stats(100, 300, 100))
    c = make_card("C", type_="Cute", stats=Stats(100, 100, 100))
    d = make_card(
        "D", type_="Cool", passive=stat_passive("performance", cute(1))
    )
    e = make_card("E", type_="Cool", passive=stat_passive("technique", cute(1)))
    leader = make_card("L", type_="Cool")
    team = make_team([leader, a, b, c, d, e])
    cfg = make_scoring_config()

    # Shipped rule (default `free`): each effect ranks by the stat it raises —
    # performance -> A (300), technique -> B (300).
    #   A: 300*1.5 + 100 + 100 = 650, B: 100 + 300*1.5 + 100 = 650, C/D/E: 300  -> 2200
    assert score_fast(team, cfg) == 2200.0
    assert score_fast(team, free(cfg)) == 2200.0
    # best_order collapses both onto one slot-earliest Cute and reaches 2100.
    assert score_fast(team, best_order(cfg)) == 2100.0


def test_the_chosen_slot_order_is_reported_and_the_attribution_names_it():
    """A tool that recommends a team without its slot order would hand the
    player the median case instead of the best one.

    Same team as above. Under `best_order`'s chosen order both effects reach
    exactly one Cute member — the SAME one, since both have `count = 1` — and
    the breakdown must say so: `target_card_ids` names the cards the scored
    number actually reached."""
    a = make_card("A", type_="Cute", stats=Stats(300, 100, 100))
    b = make_card("B", type_="Cute", stats=Stats(100, 300, 100))
    c = make_card("C", type_="Cute", stats=Stats(100, 100, 100))
    d = make_card("D", type_="Cool", passive=stat_passive("performance", cute(1)))
    e = make_card("E", type_="Cool", passive=stat_passive("technique", cute(1)))
    team = make_team([make_card("L", type_="Cool"), a, b, c, d, e])
    cfg = make_scoring_config()

    # The refuted alternative `best_order` still reports the chosen slot order, and
    # both count-1 effects reach the SAME first Cute in that order.
    bo = score_with_breakdown(team, best_order(cfg))
    assert bo.slot_order_rule == "best_order"
    assert bo.member_order is not None
    assert sorted(bo.member_order) == ["A", "B", "C", "D", "E"]
    first_cute = next(cid for cid in bo.member_order if cid in {"A", "B", "C"})
    bo_stat_rows = [row for row in bo.contributions if row.pool == "stat"]
    assert len(bo_stat_rows) == 2
    assert {row.target_card_ids for row in bo_stat_rows} == {(first_cute,)}

    # Shipped rule (default `free`): no order is chosen (`member_order is None`),
    # and each effect ranks by the stat it raises, so performance names A and
    # technique names B.
    default = score_with_breakdown(team, cfg)
    assert default.member_order is None
    assert default.slot_order_rule == "free"
    assert {
        row.target_card_ids for row in default.contributions if row.pool == "stat"
    } == {("A",), ("B",)}


# ---------------------------------------------------------------------------
# 3. The O(5) precondition: nothing binds -> both rules bit-identical
# ---------------------------------------------------------------------------


def test_when_no_count_limited_effect_binds_the_answer_is_bit_identical():
    """An effect can only move with slot order when its matcher count STRICTLY
    exceeds its own `count`. With matchers `<= count`, first-N-in-order and
    top-N-by-stat both select every matcher, so the two rules agree exactly —
    not approximately.

    Two Cute members against `count = 2`: nothing binds. The assertion is `==`
    on the raw floats, deliberately, not `pytest.approx`: the no-binding case
    short-circuits to the very same code path, so a tolerance here would hide a
    real divergence.
    """
    a = make_card("A", type_="Cute", stats=Stats(300, 100, 100))
    b = make_card("B", type_="Cute", stats=Stats(100, 300, 100))
    c = make_card("C", type_="Cool", stats=Stats(120, 130, 140))
    d = make_card("D", type_="Cool", passive=stat_passive("performance", cute(2)))
    e = make_card("E", type_="Cool", passive=stat_passive("technique", cute(2)))
    team = make_team([make_card("L", type_="Cool"), a, b, c, d, e])
    cfg = make_scoring_config()

    best = score_fast(team, cfg)
    assert best == score_fast(team, free(cfg))
    # Both Cute members receive both buffs; C/D/E receive neither.
    #   A: 300*1.5 + 100*1.5 + 100 = 700
    #   B: 100*1.5 + 300*1.5 + 100 = 700
    #   C: 120 + 130 + 140         = 390
    #   D, E:                        300 each
    assert best == 2390.0
    # No order was chosen, because every order scores the same — reporting one
    # would imply a recommendation the model never made.
    assert score_with_breakdown(team, cfg).member_order is None


def test_the_whole_suite_of_teams_without_targets_is_untouched():
    """The shipped fixture collection carries no count-limited target at all, so
    the default rule must be the exact identity on it: an order-aware path that
    silently shifted every existing result would make the whole suite
    untrustable."""
    cards = read_cards(FIXTURE_CARDS_PATH)
    cfg = make_scoring_config()
    seen = 0
    for combo in itertools.combinations(cards, 5):
        if len({card.member for card in combo}) != 5:
            continue
        for leader in cards:
            team = Team(leader=leader, members=combo)
            assert score_fast(team, cfg) == score_fast(team, free(cfg))
            seen += 1
    assert seen > 0


# ---------------------------------------------------------------------------
# 4. Structural invariants
# ---------------------------------------------------------------------------


def test_the_stat_pool_is_the_only_order_dependent_term():
    """Proving the negative, because it is what bounds the change: a Score UP, a
    Score Support and the board factor are all order-invariant here.

    - `_bucket_entry` reads no target at all for a `pool == "score"` skill.
    - `_score_up_pool` models the runtime literally — a support that reaches
      NOBODY never enters the effect collection, one that reaches anybody boosts
      the whole collection — and the first `count` matchers of a NON-EMPTY
      matcher set is non-empty under every ordering.
    - `_board_score_rate` sums over the member set.

    So a team whose only count-limited targets sit on score-pool and support
    skills scores identically under both rules, even though three members share
    the Cute type and the count would bind if the stat pool were involved.
    """
    score_up = make_skill(
        "ACTIVE",
        family="conditional_score_up",
        effect_type="score",
        duration=10.0,
        interval=30.0,
        probability_tier="High",
        magnitude=40.0,
        target=cute(1),
    )
    support = make_skill(
        "COSTUME",
        family="score_support",
        effect_type="score",
        magnitude=30.0,
        target=cute(1),
    )
    a = make_card("A", type_="Cute", stats=Stats(300, 100, 100), active=score_up)
    b = make_card("B", type_="Cute", stats=Stats(100, 300, 100), active=score_up)
    c = make_card("C", type_="Cute", stats=Stats(100, 100, 300))
    d = make_card("D", type_="Cool")
    e = make_card("E", type_="Cool")
    leader = make_card("L", type_="Cool", costume_skill=support)
    team = make_team([leader, a, b, c, d, e])
    cfg = make_scoring_config()

    assert score_fast(team, cfg) == score_fast(team, free(cfg))
    assert score_with_breakdown(team, cfg).member_order is None


def test_score_card_set_reproduces_score_fast_bit_identically_when_order_binds():
    """Batch/single parity, on teams where the count binds. The batch
    entry point hoists the ordering enumeration out of its per-leader tail
    (`orderings_cache`), which is precisely the kind of optimisation that can
    silently produce a different answer per leader — so the parity is asserted
    on `==`, over several leaders, on a team where the count genuinely binds."""
    a = make_card("A", type_="Cute", stats=Stats(300, 100, 100))
    b = make_card("B", type_="Cute", stats=Stats(100, 300, 100))
    c = make_card("C", type_="Cute", stats=Stats(100, 100, 300))
    d = make_card("D", type_="Cool", passive=stat_passive("performance", cute(1)))
    e = make_card("E", type_="Cool", passive=stat_passive("technique", cute(1)))
    leaders = (
        make_card("L", type_="Cool"),
        make_card(
            "M",
            type_="Cool",
            costume_skill=make_skill(
                "COSTUME", family="stat_up", effect_type="all_stats", magnitude=10.0
            ),
        ),
        a,
    )
    members = (a, b, c, d, e)
    cfg = make_scoring_config()

    for rule in SLOT_ORDER_RULES:
        scoped = dataclasses.replace(cfg, slot_order_rule=rule)
        batch = score_card_set(members, leaders, scoped)
        for leader, leader_score in zip(leaders, batch):
            assert leader_score.total_score == score_fast(
                Team(leader=leader, members=members), scoped
            )

    # The count genuinely binds under the shipped rule, not short-circuiting: each
    # count-1 effect reaches exactly one Cute, the one highest in the stat it
    # raises — performance -> A (300), technique -> B (300).
    bd = score_with_breakdown(Team(leader=leaders[0], members=members), cfg)
    stat_targets = {row.target_card_ids for row in bd.contributions if row.pool == "stat"}
    assert stat_targets == {("A",), ("B",)}


def test_every_declared_slot_order_rule_has_an_implementation():
    assert set(SLOT_ORDER_RESOLVERS) == set(SLOT_ORDER_RULES)
    assert SLOT_ORDER_RULES[0] == "free", (
        "the shipped default must be the MEASURED rule (top-count by the stat "
        "the effect raises), not the refuted slot-order predecessor"
    )


# ---------------------------------------------------------------------------
# 4b. The RANKING recipient tie-break follows DECK SLOT ORDER, not card_id
# ---------------------------------------------------------------------------
#
# On an EXACT tie of the ranking value among a count-limited target's matchers,
# the SERVER keeps deck SLOT ORDER (earlier slot wins). That value is the stat
# the effect raises, so the decks below tie on the BUFFED stat: the payout is then
# the same whoever is chosen and what the tie-break decides is which member the
# breakdown names. The Overall Power DISPLAY matches this (`_SLOT_ORDER_TIE_BREAK`,
# bit-exact over `bonus_extract_db.jsonl`), and the RANKING objective
# (`_free_stat_sum` -> `TopNByBuffedStat`) uses the same slot-order tie-break, so
# `total_score` agrees with the server on the rare tie decks. `TopNByBuffedStat`
# is a STABLE sort with no `card_id` secondary key, so ties keep the
# DETERMINISTIC input/slot order `Team` guarantees.


def _tie_deck():
    """A count-1 `type:Cute` performance buff whose two top matchers TIE on
    performance — the stat the buff raises — while card_id order AND total
    parameter both DISAGREE with slot order:

      card-zzz: performance 300, total 350  (slot-EARLIER -> the recipient)
      card-aaa: performance 300, total 550  (card_id-smaller AND higher total)

    So the two plausible wrong rules (card_id tie-break; rank by total) would
    both name card-aaa, and only slot order names card-zzz.
    """
    buff = make_skill(
        "PASSIVE",
        family="stat_up",
        effect_type="performance",
        magnitude=20.0,
        target=cute(1),
    )
    slot_win = make_card(  # earlier slot, LARGER id, lower total -> slot order picks it
        "card-zzz", type_="Cute", stats=Stats(performance=300, technique=50, sense=0)
    )
    id_win = make_card(  # later slot, SMALLER id, higher total -> both wrong rules pick it
        "card-aaa", type_="Cute", stats=Stats(performance=300, technique=250, sense=0)
    )
    emitter = make_card(  # carries the buff; too low a performance to be a recipient
        "card-emit", type_="Cute", stats=Stats(performance=10, technique=10, sense=10),
        passive=buff,
    )
    f1 = make_card("card-f1", type_="Happy", stats=Stats(performance=10, technique=10, sense=10))
    f2 = make_card("card-f2", type_="Happy", stats=Stats(performance=10, technique=10, sense=10))
    leader = make_card("card-lead", type_="Happy", stats=Stats(performance=0, technique=0, sense=0))
    # Members in SLOT ORDER: slot_win (card-zzz) sits before id_win (card-aaa).
    return make_team([leader, slot_win, id_win, emitter, f1, f2])


def test_ranking_recipient_tie_break_follows_slot_order_not_card_id():
    """The count-1 buff's recipient, on an exact tie of the buffed stat, is the
    SLOT-EARLIER member (card-zzz), not the card_id-smaller, higher-total one
    (card-aaa). base_stat_sum is 350 + 550 + 30 + 30 + 30 = 990 and the buff adds
    `0.20 * 300 = 60` whichever of the two receives it, so the sum is 1050.0
    either way — the recipient named in the breakdown is what tells them apart."""
    team = _tie_deck()
    cfg = make_scoring_config()

    bd = score_with_breakdown(team, cfg)

    assert bd.effective_stat_sum == pytest.approx(1050.0)
    # The scored recipient IS the slot-earlier member.
    stat_targets = {row.target_card_ids for row in bd.contributions if row.pool == "stat"}
    assert stat_targets == {("card-zzz",)}
    # score_fast (the hot loop) agrees with the breakdown's total_score bit-for-bit.
    assert bd.total_score == score_fast(team, cfg)
    # And the RANKING agrees with the DISPLAY on the recipient (both slot-order).
    assert bd.overall_power_stat_sum == pytest.approx(bd.effective_stat_sum)


def test_ranking_recipient_tie_break_is_deterministic_across_runs():
    """The stable-sort tie-break over the deterministic slot order must be
    reproducible — the same deck scores identically twice, and names the same
    recipient both times."""
    team = _tie_deck()
    cfg = make_scoring_config()

    first = score_fast(team, cfg)
    second = score_fast(team, cfg)
    assert first == second

    bd1 = score_with_breakdown(team, cfg)
    bd2 = score_with_breakdown(team, cfg)
    t1 = {row.target_card_ids for row in bd1.contributions if row.pool == "stat"}
    t2 = {row.target_card_ids for row in bd2.contributions if row.pool == "stat"}
    assert t1 == t2 == {("card-zzz",)}


def test_ranking_tie_break_slot_order_moves_with_the_deck_arrangement():
    """The recipient FOLLOWS the deck slot order: swapping the two tied matchers'
    slots swaps which one the buff lands on. With card-aaa placed EARLIER than
    card-zzz, the buff is now named on card-aaa. Same tied deck as `_tie_deck`
    (both performance 300), so the sum stays 1050.0."""
    buff = make_skill(
        "PASSIVE", family="stat_up", effect_type="performance", magnitude=20.0, target=cute(1)
    )
    zzz = make_card("card-zzz", type_="Cute", stats=Stats(performance=300, technique=50, sense=0))
    aaa = make_card("card-aaa", type_="Cute", stats=Stats(performance=300, technique=250, sense=0))
    emitter = make_card(
        "card-emit", type_="Cute", stats=Stats(performance=10, technique=10, sense=10), passive=buff
    )
    f1 = make_card("card-f1", type_="Happy", stats=Stats(performance=10, technique=10, sense=10))
    f2 = make_card("card-f2", type_="Happy", stats=Stats(performance=10, technique=10, sense=10))
    leader = make_card("card-lead", type_="Happy", stats=Stats(performance=0, technique=0, sense=0))
    # card-aaa now sits in the EARLIER slot.
    team = make_team([leader, aaa, zzz, emitter, f1, f2])
    cfg = make_scoring_config()

    bd = score_with_breakdown(team, cfg)
    assert bd.effective_stat_sum == pytest.approx(1050.0)
    stat_targets = {row.target_card_ids for row in bd.contributions if row.pool == "stat"}
    assert stat_targets == {("card-aaa",)}
    assert bd.total_score == score_fast(team, cfg)


def test_the_recipient_is_the_highest_buffed_stat_not_the_highest_total():
    """The rule itself, where it and the total-parameter rule disagree. The count-1
    performance buff has a UNIQUE highest-performance matcher (card-aaa, 300) and
    a different UNIQUE highest-total matcher (card-zzz, total 500, performance
    200). The server pays card-aaa (ranking by the buffed stat is bit-exact on
    every measured row, ranking by total on about three quarters). No tie, so
    slot order and card_id are both irrelevant."""
    buff = make_skill(
        "PASSIVE", family="stat_up", effect_type="performance", magnitude=20.0, target=cute(1)
    )
    # zzz: total 500 (unique max) but performance 200; aaa: total 350, performance 300.
    top = make_card("card-zzz", type_="Cute", stats=Stats(performance=200, technique=300, sense=0))
    mid = make_card("card-aaa", type_="Cute", stats=Stats(performance=300, technique=50, sense=0))
    emitter = make_card(
        "card-emit", type_="Cute", stats=Stats(performance=10, technique=10, sense=10), passive=buff
    )
    f1 = make_card("card-f1", type_="Happy", stats=Stats(performance=10, technique=10, sense=10))
    f2 = make_card("card-f2", type_="Happy", stats=Stats(performance=10, technique=10, sense=10))
    leader = make_card("card-lead", type_="Happy", stats=Stats(performance=0, technique=0, sense=0))
    team = make_team([leader, top, mid, emitter, f1, f2])
    cfg = make_scoring_config()

    bd = score_with_breakdown(team, cfg)
    # base = 500 + 350 + 30 + 30 + 30 = 940; buff on the top-performance aaa
    # (perf 300): 940 + 0.20 * 300 = 1000. The total-parameter rule would pay zzz: 980.
    assert bd.effective_stat_sum == pytest.approx(1000.0)
    stat_targets = {row.target_card_ids for row in bd.contributions if row.pool == "stat"}
    assert stat_targets == {("card-aaa",)}
    assert bd.total_score == score_fast(team, cfg)


# ---------------------------------------------------------------------------
# 5. The invariant, over a broad generated collection
# ---------------------------------------------------------------------------


def _generated_collection(rng: random.Random) -> tuple:
    """A synthetic 60-card collection shaped like the real one: three types,
    three generations, and a count-limited PASSIVE `stat_up` on most cards, at
    the same `type`/2, `type`/3 and `generation`/2 mix the shipped collection
    carries.

    Generated rather than read from disk on purpose. `tests/test_fixture_
    isolation.py` forbids a test reaching the user's authoritative, hand-edited
    collection, and it is right to: a test that samples it would change its
    verdict every time a card is transcribed. What is asserted below is an
    INVARIANT of the two rules, not a property of any particular collection,
    so it does not need the real one.
    """
    types = ("Cute", "Cool", "Sexy")
    generations = ("1期生", "2期生", "3期生")
    effect_types = ("performance", "technique", "sense", "all_stats")
    shapes = (("type", 2), ("type", 3), ("generation", 2), None, None)
    cards = []
    for index in range(60):
        card_type = types[index % 3]
        generation = generations[(index // 3) % 3]
        shape = shapes[index % len(shapes)]
        if shape is None:
            passive = None
        else:
            kind, count = shape
            passive = stat_passive(
                rng.choice(effect_types),
                EffectTarget(
                    kind=kind,
                    value=card_type if kind == "type" else generation,
                    count=count,
                ),
                magnitude=rng.choice((20.0, 35.0, 50.0)),
            )
        cards.append(
            make_card(
                f"{index:03d}",
                type_=card_type,
                generations=frozenset({generation}),
                stats=Stats(
                    rng.randrange(1000, 4000),
                    rng.randrange(1000, 4000),
                    rng.randrange(1000, 4000),
                ),
                passive=passive,
            )
        )
    return tuple(cards)


def test_the_shipped_rule_differs_from_the_refuted_predecessor_on_a_real_share():
    """The shipped rule (`free`) and the refuted alternative (`best_order`,
    slot-search) disagree on a non-trivial share of teams, so a regression that
    silently made the alternative the default cannot pass this file.

    Only the disagreement is asserted, not a fixed direction (e.g.
    "`best_order <= free`, always"): neither rule bounds the other on every
    team — they differ where the count binds.
    """
    rng = random.Random(11)
    cards = _generated_collection(rng)
    cfg = make_scoring_config()
    by_holomem: dict[str, list] = collections.defaultdict(list)
    for card in cards:
        by_holomem[card.member].append(card)
    holomems = sorted(by_holomem)

    differ = 0
    gaps: list[float] = []
    for _ in range(400):
        members = tuple(
            rng.choice(by_holomem[name]) for name in rng.sample(holomems, 5)
        )
        team = Team(leader=rng.choice(cards), members=members)
        shipped = score_fast(team, cfg)               # default: free
        predecessor = score_fast(team, best_order(cfg))
        if shipped != predecessor:
            differ += 1
            gaps.append(abs(shipped - predecessor) / shipped * 100.0)

    assert differ >= 20, (
        f"only {differ}/400 sampled teams distinguished the shipped rule from the "
        "refuted predecessor — either the generator stopped producing binding "
        "targets or the default has been reverted to the predecessor"
    )
    assert 0.0 < statistics.median(gaps) < 10.0
