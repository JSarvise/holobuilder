"""Leader dominance — the premise, the pruning, and the boundary of the claim.

Pruning the outfit slot by dominance gives a headline speed-up of **5.7x**.
This module pins three things that the headline number does not say:

1. **The premise.** Dominance is only sound because a leader reaches the score
   through its outfit and nothing else. That is a property of the *shipped
   scoring model*, not of the game, and it stops being true wherever
   leader-scope red board nodes are in play. So it is asserted directly: for a
   fixed member set, every leader sharing a `(signature, magnitude)` pair must
   produce the identical float.

2. **The correction.** 5.7x counts signature *classes*. On a real pool 2 of
   the 16 owned signatures have their maximum TIED across several cards, and collapsing
   such a class to one representative changes which leader the tool names.
   Keeping every tied card gives **4.79x**, and that is what this module
   asserts.

3. **The boundary.** The pruned Top-N is NOT the exhaustive Top-N and cannot
   be — a dominated leader still forms a valid, merely sub-optimal team, and the
   exhaustive Top-10 contains such teams (36 of 40 real sub-pools agree, 4 do
   not). The test below states that as a property rather than letting a future
   reader discover it as a bug.
"""

from __future__ import annotations

import itertools

import pytest

from hbuilder.domain.card import EffectTarget, GateCondition, SlotOverride, Stats
from hbuilder.domain.scoring import score_card_set
from hbuilder.domain.unknown import UNKNOWN
from hbuilder.solver.dominance import (
    dominant_leader_ids,
    dominant_leaders,
    dominated_by,
    outfit_magnitude,
    outfit_signature,
)
from hbuilder.solver.exact import canonical_pool, solve_exact

from conftest import make_card, make_scoring_config, make_skill


def outfit(magnitude, *, family="stat_up", effect_type="sense", condition=None,
           target=None, override=None):
    return make_skill(
        "COSTUME",
        family=family,
        effect_type=effect_type,
        magnitude=magnitude,
        condition=condition,
        target=target,
        override=override,
    )


def leader_card(card_id, costume, **kwargs):
    return make_card(card_id, costume_skill=costume, **kwargs)


# ---------------------------------------------------------------------------
# The signature: what groups, and what refuses to group
# ---------------------------------------------------------------------------


def test_two_outfits_differing_only_in_magnitude_share_a_signature():
    weak = leader_card("a", outfit(35.0))
    strong = leader_card("b", outfit(45.0))

    assert outfit_signature(weak) == outfit_signature(strong)
    assert outfit_magnitude(weak) < outfit_magnitude(strong)
    assert dominant_leader_ids([weak, strong]) == {"b"}


def test_the_same_magnitude_on_a_different_stat_is_not_comparable():
    """`+120 % sense` and `+120 % performance` raise different numbers on
    different cards. A soundness constraint: compare only within an identical
    signature."""
    sense = leader_card("a", outfit(120.0, effect_type="sense"))
    performance = leader_card("b", outfit(120.0, effect_type="performance"))

    assert outfit_signature(sense) != outfit_signature(performance)
    assert dominant_leader_ids([sense, performance]) == {"a", "b"}


def test_a_gated_outfit_never_groups_with_an_ungated_one():
    gate = GateCondition(kind="type", target="Cute", count=2)
    ungated = leader_card("a", outfit(50.0))
    gated = leader_card("b", outfit(50.0, condition=gate))

    assert outfit_signature(ungated) != outfit_signature(gated)
    assert dominant_leader_ids([ungated, gated]) == {"a", "b"}


def test_two_outfits_with_different_override_gates_do_not_group():
    """The override is a SECOND magnitude that moves with the card, so two
    outfits agreeing on the base are not ordered by the base alone. Keeping the
    override in the KEY rather than in the comparison prunes less, never
    wrongly."""
    gate_a = GateCondition(kind="type", target="Cute", count=2)
    gate_b = GateCondition(kind="type", target="Cool", count=2)
    one = leader_card("a", outfit(50.0, override=SlotOverride(magnitude=90.0, condition=gate_a)))
    two = leader_card("b", outfit(50.0, override=SlotOverride(magnitude=90.0, condition=gate_b)))

    assert outfit_signature(one) != outfit_signature(two)
    assert dominant_leader_ids([one, two]) == {"a", "b"}


def test_a_self_targeting_outfit_is_never_pruned():
    """Its recipient is the leader — a member on some sets and nobody on
    others — so its contribution is not a function of (signature, magnitude) at
    all and magnitude does not order it. All 167 real outfits target the whole
    team, so this case does not arise in the shipped catalogue; it is guarded
    rather than trusted to stay that way."""
    weak = leader_card("a", outfit(35.0, target=EffectTarget(kind="self", value=None, count=None)))
    strong = leader_card("b", outfit(45.0, target=EffectTarget(kind="self", value=None, count=None)))

    assert dominant_leader_ids([weak, strong]) == {"a", "b"}


@pytest.mark.parametrize(
    "field", ["magnitude", "condition", "target", "override"]
)
def test_an_unknown_field_makes_a_card_its_own_class(field):
    """Grouping on a field that was never populated is the absent-vs-zero merge
    the schema layer exists to refuse."""
    plain = leader_card("a", outfit(45.0))
    fields = {"magnitude": 45.0}
    fields[field] = UNKNOWN
    tainted = leader_card("b", outfit(**fields))

    assert outfit_signature(plain) != outfit_signature(tainted)
    assert dominant_leader_ids([plain, tainted]) == {"a", "b"}


def test_every_card_tied_at_its_class_maximum_survives():
    """The correction to the 5.7x figure. Collapsing a tied class to one
    representative would change which leader `_heap_key` names at rank 1."""
    first = leader_card("a", outfit(45.0))
    second = leader_card("b", outfit(45.0))
    dominated = leader_card("c", outfit(35.0))

    assert dominant_leader_ids([first, second, dominated]) == {"a", "b"}


def test_a_zero_weighted_outfit_family_leaves_its_class_whole():
    """A family the config prices at zero scores the same at every magnitude, so
    the class is one big tie rather than an ordering. `config.json` prices
    `activation_rate_up`, `life_recovery` and `judgment_enhancement` at 0.0
    and no outfit uses any of them, so the guard is inert — it exists so
    that changing a weight fails a test instead of naming the wrong leader."""
    weak = leader_card("a", outfit(35.0, family="life_recovery", effect_type="score"))
    strong = leader_card("b", outfit(45.0, family="life_recovery", effect_type="score"))
    cfg = make_scoring_config()

    assert dominant_leader_ids([weak, strong]) == {"b"}
    assert dominant_leader_ids([weak, strong], cfg) == {"a", "b"}


def test_dominated_by_names_what_it_discarded():
    """A pruning that cannot say what it discarded is one nobody can check."""
    first = leader_card("a", outfit(45.0))
    dominated = leader_card("c", outfit(35.0))
    other = leader_card("b", outfit(20.0, effect_type="performance"))

    report = dominated_by([first, dominated, other])

    assert report["a"] == ("c",)
    assert report["b"] == ()
    assert "c" not in report


# ---------------------------------------------------------------------------
# The premise: the leader reaches the score ONLY through its outfit
# ---------------------------------------------------------------------------

_TYPES = ("Cute", "Cool", "Passion")


def build_pool(size: int):
    """A pool in which several cards deliberately share an outfit class while
    differing in everything else — stats, type, generation, ACTIVE and PASSIVE
    skills. If the leader reached the score by any route other than its outfit,
    those differences would show up and the premise test would fail."""
    cards = []
    for index in range(size):
        cards.append(
            make_card(
                f"card_{index:02d}",
                type_=_TYPES[index % 3],
                generations=frozenset({f"gen{index % 4}"}),
                stats=Stats(
                    performance=900 + index * 31,
                    technique=850 + index * 47,
                    sense=800 + index * 53,
                ),
                active=make_skill(
                    "ACTIVE",
                    family="conditional_score_up",
                    effect_type="score",
                    duration=8.0,
                    interval=20.0,
                    probability_tier="Medium",
                    magnitude=40.0 + index,
                    condition=(
                        GateCondition(kind="type", target=_TYPES[index % 3], count=2)
                        if index % 2 == 0
                        else None
                    ),
                ),
                passive=make_skill(
                    "PASSIVE",
                    family="stat_up",
                    effect_type="all_stats" if index % 3 == 0 else "performance",
                    magnitude=12.0 + index,
                    target=EffectTarget(kind="type", value=_TYPES[index % 3], count=2),
                ),
                # Deliberately only three distinct outfits over the whole pool,
                # so classes are large and the premise is tested on real ties.
                # Three outfits over the whole pool, arranged so that one
                # class holds a genuine DOMINATED card (45 < 60 on sense) and
                # its maximum is TIED across several cards -- both of the
                # cases this module is about.
                costume_skill=outfit(
                    (60.0, 45.0, 80.0)[index % 3],
                    effect_type=("sense", "sense", "performance")[index % 3],
                ),
            )
        )
    return canonical_pool(cards)


def test_every_leader_in_a_class_scores_the_identical_float(monkeypatch):
    """THE premise. Asserted as exact float equality over every member set of a
    12-card pool, against every leader in every multi-card class.

    If this ever fails, dominance is void — not approximate, void — and the
    likeliest cause is the one named in `dominance.py`'s header: leader-scope
    red board nodes becoming a real term in `_board_score_rate`.
    """
    pool = build_pool(12)
    cfg = make_scoring_config()

    classes: dict[tuple, list] = {}
    for card in pool:
        classes.setdefault((outfit_signature(card), outfit_magnitude(card)), []).append(card)
    multi = [group for group in classes.values() if len(group) > 1]
    assert multi, "the fixture must produce classes with more than one card"

    checked = 0
    for members in itertools.combinations(pool, 5):
        for group in multi:
            scores = [
                score_card_set(members, (card,), cfg)[0].total_score for card in group
            ]
            assert len(set(scores)) == 1, (
                f"leaders {[c.id for c in group]} share a (signature, magnitude) "
                f"class but scored {scores} on members "
                f"{[c.id for c in members]} — dominance is unsound"
            )
            checked += 1
    assert checked > 1000


# ---------------------------------------------------------------------------
# The consequence, and its boundary
# ---------------------------------------------------------------------------


def test_the_pruned_search_finds_the_same_best_team(monkeypatch):
    """Claim A and B together: same best score, same best arrangement."""
    pool = build_pool(12)
    cfg = make_scoring_config()
    keep = dominant_leader_ids(pool, cfg)
    assert len(keep) < len(pool)

    full = solve_exact(pool, cfg, top_n=10)
    pruned = solve_exact(pool, cfg, top_n=10, leader_ids=keep)

    assert pruned[0].score == full[0].score
    assert (pruned[0].leader_id, pruned[0].member_ids) == (
        full[0].leader_id,
        full[0].member_ids,
    )


def test_the_pruned_top_n_is_not_the_exhaustive_top_n_and_that_is_by_design():
    """Claim C, stated as a property rather than left to be found as a bug.

    A dominated leader forms a valid but sub-optimal team, and the exhaustive
    Top-N contains such teams. Pruning removes them, so the pruned list is "the
    best N arrangements among surviving leaders". 36 of 40 real sub-pools give
    identical lists; this fixture is built so the difference is deterministic.
    """
    pool = build_pool(9)
    cfg = make_scoring_config()
    keep = dominant_leader_ids(pool, cfg)

    # A deep list, because the divergence lives in the TAIL. The real
    # divergences over 40 sub-pools were at rank 10 of 10 and none was
    # above it, so a Top-10 on a 9-card pool is not deep enough to demonstrate
    # the property at all: on this fixture, a dominated leader first
    # appears at rank 61 of 1,134 and holds only 5 of the top 200 slots.
    # That rarity is itself the reassuring half of the finding, and is why
    # claim A and claim B get their own test above.
    full = solve_exact(pool, cfg, top_n=200)
    pruned = solve_exact(pool, cfg, top_n=200, leader_ids=keep)

    assert full[0].score == pruned[0].score
    assert any(team.leader_id not in keep for team in full), (
        "the fixture no longer places a dominated leader in the exhaustive "
        "list, so this test no longer demonstrates anything"
    )
    assert all(team.leader_id in keep for team in pruned)
    assert full != pruned


def test_pruning_narrows_the_outfit_slot_and_never_the_member_pool():
    """A dominated card carries stats and skills that have nothing to do with
    its outfit, so it must stay a fully eligible member."""
    pool = build_pool(9)
    cfg = make_scoring_config()
    keep = dominant_leader_ids(pool, cfg)
    dominated = [card.id for card in pool if card.id not in keep]
    assert dominated

    pruned = solve_exact(pool, cfg, top_n=10, leader_ids=keep)

    assert any(
        member_id in dominated for team in pruned for member_id in team.member_ids
    ), "a dominated card was excluded from the member enumeration too"


def test_an_empty_pool_and_a_pool_of_one_do_not_raise():
    assert dominant_leaders([]) == ()
    single = leader_card("a", outfit(45.0))
    assert dominant_leaders([single]) == (single,)


def test_a_card_with_no_outfit_at_all_is_handled():
    """`data/cards.json` populates all 167, but the schema allows `None` and a
    `max()` over an empty magnitude set would raise rather than answer."""
    bare = make_card("a", costume_skill=None)
    other = make_card("b", costume_skill=None)
    dressed = leader_card("c", outfit(45.0))

    survivors = dominant_leader_ids([bare, other, dressed])

    assert "c" in survivors
    assert {"a", "b"} <= survivors


# ---------------------------------------------------------------------------
# 4. The board makes leaders non-interchangeable, and the pruning must know.
#
# The risk: if the board changes a leader's value, a dominance key that does
# not carry the board prunes wrongly.
# `outfit_signature(card, board_discriminates=True)` puts the holomem back in
# the key; these tests check that it is ENOUGH. Against a brute force over
# every leader of every member set, the pruned exact search returns the
# identical arrangement and the identical float on a real pool at N=14, 18
# and 20 with a real captured board.
#
# ⚠ TWO THINGS TO KNOW.
#
# 1. **Dominance is not on the shipped solve path at all.** `cli.py` never
#    passes `leader_ids`, and `solve_exact` considers every pool card as leader
#    unless a caller narrows it. The only production caller of
#    `dominant_leaders` is `bench/validate_search.py`. So a wrong key cannot
#    move any answer `hbuilder solve` gives — it can only corrupt that
#    harness's conclusions. Every test below therefore passes
#    `leader_ids=` explicitly; a call without it exercises no pruning and would
#    pass whatever the key did.
#
# 2. **4.79x is a BOARDLESS number.** On a real 91-card pool the
#    survivors are 19 of 91 (4.79x) with no board and **86 of 91 (1.06x)**
#    with the captured one. The board is doing what it should — a red branch
#    makes two identical outfits genuinely different leaders — and it costs
#    nearly all of the pruning. Quoting 4.79x for a board-aware run would be
#    a true statement about one configuration, reused without its caveat.
# ---------------------------------------------------------------------------


def _red_board(holomem: str):
    """A board whose ONLY node is leader-scope, on one holomem — the shape that
    makes two otherwise-identical leaders differ. That is the whole hazard: a
    key blind to it would collapse them and discard the better one."""
    from hbuilder.domain.board import BoardModel, board_from_mapping

    return board_from_mapping(
        {
            "schema_version": "1",
            "characters": {
                holomem: [
                    {
                        "node": "R-001",
                        "effects": [
                            "skill_tree_effect-all_parameter_up-500"
                            "-skill_tree_effect_target-all-set_live_leader"
                        ],
                    }
                ]
            },
        },
        source="<test>",
    )


def test_the_dominance_key_separates_two_identical_outfits_on_different_holomem():
    """The property directly. Same outfit, same magnitude, different holomem:
    interchangeable with no board, and NOT interchangeable with one, because
    only one of them brings a red branch to the leader slot."""
    alpha = leader_card("alpha", outfit(45.0), member="Alpha")
    beta = leader_card("beta", outfit(45.0), member="Beta")

    assert outfit_signature(alpha) == outfit_signature(beta)
    assert outfit_signature(alpha, board_discriminates=True) != outfit_signature(
        beta, board_discriminates=True
    )


def test_a_board_saves_a_leader_the_blind_key_would_have_discarded():
    """The end-to-end check, on a pool built so a board-blind key gets it WRONG.

    `weak` and `strong` share an outfit signature and `weak` has the smaller
    magnitude, so a key that does not carry the board prunes `weak` outright.
    `weak` is also the only holomem with a red branch, and the branch is worth
    far more than the outfit gap — so `weak` is the true optimal leader and a
    blind key discards it before it is ever scored.

    That is the classic shape of a signature omitting a rider, and it is why
    the fixture is built here rather than reusing `build_pool`: in that pool
    the red holomem survives the blind key by luck, so the test would pass
    either way and prove nothing.

    Members are enumerated from an id-SORTED pool because `power_bonus` and
    `_effective_stat_sum` accumulate one running float over `members` in order:
    the same five cards in a different order is a different last bit, and
    comparing those would be testing summation order rather than the search.
    """
    import dataclasses

    weak = leader_card("card_weak", outfit(10.0), member="Red")
    strong = leader_card("card_strong", outfit(90.0), member="Blue")
    filler = [leader_card(f"card_f{i}", outfit(20.0 + i), member=f"F{i}") for i in range(5)]
    pool = canonical_pool([weak, strong, *filler])

    cfg = make_scoring_config()
    cfg_board = dataclasses.replace(cfg, board=_red_board("Red"))

    # The hazard is real for THIS pool: blind, `weak` is pruned; board-aware,
    # it is kept. Without this pair the test could pass on a pool where the
    # key never mattered.
    assert "card_weak" not in dominant_leader_ids(pool, cfg)
    assert "card_weak" in dominant_leader_ids(pool, cfg_board)

    # `leader_ids=` is REQUIRED here. `solve_exact` does not prune by
    # dominance on its own — it considers every pool card as leader unless a
    # caller narrows it — so a call without this argument exercises no pruning
    # at all and would pass whatever the key did.
    pruned = solve_exact(
        pool, cfg_board, top_n=1, leader_ids=dominant_leader_ids(pool, cfg_board)
    )[0]

    best = None
    for members in itertools.combinations(pool, 5):
        if len({card.member for card in members}) != 5:
            continue
        for scored in score_card_set(members, pool, cfg_board):
            if best is None or scored.total_score > best[0]:
                best = (scored.total_score, scored.leader_id)

    assert best is not None
    # The red branch really does make the weaker outfit the better leader —
    # otherwise the pruning could drop it and still be right.
    assert best[1] == "card_weak"
    assert pruned.leader_id == best[1]
    assert pruned.score == best[0], (
        "leader dominance pruned the true optimum with a board configured — "
        "the signature key does not carry everything the board discriminates on"
    )


def test_the_pruned_and_brute_force_answers_agree_on_the_shipped_fixture_pool():
    """The same comparison on `build_pool`'s wider pool. It cannot fail for the
    reason above — there the red holomem survives either key — so it is kept as
    a breadth check on the search rather than as the hazard test."""
    import dataclasses

    pool = canonical_pool(build_pool(8))
    cfg_board = dataclasses.replace(
        make_scoring_config(), board=_red_board(pool[0].member)
    )

    pruned = solve_exact(
        pool, cfg_board, top_n=1, leader_ids=dominant_leader_ids(pool, cfg_board)
    )[0]

    best = None
    for members in itertools.combinations(pool, 5):
        if len({card.member for card in members}) != 5:
            continue
        for scored in score_card_set(members, pool, cfg_board):
            if best is None or scored.total_score > best[0]:
                best = (scored.total_score, scored.leader_id)

    assert best is not None
    assert pruned.leader_id == best[1]
    assert pruned.score == best[0]
