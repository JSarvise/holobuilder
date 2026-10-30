"""The Holomem Board model, against its acceptance test.

On one fully decomposed captured deck the board is **37,472 of 135,736
Overall Power (27.6 %)** and **18.9 of 164.9 Score Bonus points (11.5 %)**.
The acceptance test this module has to pass:

> any board model must land on **+28.73 pp of Overall Power** and **+12.0 pp
> of Score Bonus** for Ina's A-vs-C configuration.

A-vs-C is a controlled experiment: the same five members, the
same outfit, only the leader changed between Ina'nis (27 unlocked red nodes,
`chr-04003`) and Calliope (zero). Ina is the only character in the account
with any red node unlocked, so the delta isolates the whole red-node effect:

    Power:       37,472 - 14,200 = 23,272  on a base of 81,009  =  +28.73 pp
    Score Bonus: 17.8 % - 5.8 %                                 =  +12.0 pp

Every number below is computed by hand in its own docstring and asserted
against the no-board answer too, so a silent regression fails rather than
passes. The fixture is Ina's 27 real unlocked R nodes and their real Connect
multipliers, transcribed from `SkillTreeNode` and the captured board state —
not invented, and not tuned.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from hbuilder.domain import board
from hbuilder.domain.board import (
    BoardError,
    BoardModel,
    BoardNode,
    CharacterBoard,
    parse_effect_id,
)
from hbuilder.domain.card import Stats
from hbuilder.domain.scoring import score_card_set, score_fast

from conftest import make_card, make_scoring_config, make_team

# --------------------------------------------------------------------------
# Ina's 27 unlocked red nodes, verbatim.
#
# `(node group id, the master row's effect id, the Connect multiplier)`.
#
# The multipliers are DERIVED, not free: Ina's board is `tree-model-002`, her
# two Connect slots hold `card-04001-5-uniq-0042-00` (shape `center_area-001`,
# rare_5, base 140 % -> x2.4) and `card-03002-5-uniq-0034-00` (shape
# `general_purpose_area-001`, rare_5, base 55 % -> x1.55), and the shapes'
# coordinate extents cover exactly these 15 of her 27 unlocked red nodes —
# five at x2.4 and ten at x1.55. Nothing here was fitted; substituting the
# Bloom-5 level of either Connect effect overshoots the acceptance number (by
# +16.48 %), which is how the base level is known to be the right reading.
# `test_bloom_5_connect_overshoots_the_acceptance_number` below pins it.
BLOOM5_CONNECT_MULTIPLIER: dict[float, float] = {2.4: 2.9, 1.55: 2.05, 1.0: 1.0}
# --------------------------------------------------------------------------
INA_RED_NODES: tuple[tuple[str, str, float], ...] = (
    ("R-001", "skill_tree_effect-all_parameter_up-50-skill_tree_effect_target-all-set_live_leader", 2.4),
    ("R-002", "skill_tree_effect-live_active_skill_effect_up_permil_up-100-skill_tree_effect_target-all-skill_tree_effect_passive_trigger-music_skill_tree_character-set_live_leader", 2.4),
    ("R-003", "skill_tree_effect-sense_up-100-skill_tree_effect_target-all-set_live_leader", 2.4),
    ("R-004", "skill_tree_effect-performance_up-100-skill_tree_effect_target-all-set_live_leader", 2.4),
    ("R-005", "skill_tree_effect-technique_up-100-skill_tree_effect_target-all-set_live_leader", 2.4),
    ("R-006", "skill_tree_effect-all_parameter_up-50-skill_tree_effect_target-all-set_live_leader", 1.0),
    ("R-007", "skill_tree_effect-all_parameter_up-50-skill_tree_effect_target-all-set_live_leader", 1.55),
    ("R-008", "skill_tree_effect-all_parameter_up-50-skill_tree_effect_target-all-set_live_leader", 1.55),
    ("R-009", "skill_tree_effect-sense_up-100-skill_tree_effect_target-all-set_live_leader", 1.55),
    ("R-010", "skill_tree_effect-all_parameter_up-50-skill_tree_effect_target-all-set_live_leader", 1.55),
    ("R-019", "skill_tree_effect-performance_up-100-skill_tree_effect_target-all-set_live_leader", 1.55),
    ("R-020", "skill_tree_effect-all_parameter_up-50-skill_tree_effect_target-all-set_live_leader", 1.55),
    ("R-021", "skill_tree_effect-technique_up-100-skill_tree_effect_target-all-set_live_leader", 1.55),
    ("R-022", "skill_tree_effect-all_parameter_up-50-skill_tree_effect_target-all-set_live_leader", 1.55),
    ("R-032", "skill_tree_effect-live_active_skill_effect_up_permil_up-20-skill_tree_effect_target-all-set_live_leader", 1.55),
    ("R-033", "skill_tree_effect-all_parameter_up-50-skill_tree_effect_target-all-set_live_leader", 1.0),
    ("R-034", "skill_tree_effect-live_active_skill_effect_up_permil_up-40-skill_tree_effect_target-all-set_live_leader", 1.0),
    ("R-035", "skill_tree_effect-all_parameter_up-70-skill_tree_effect_target-all-set_live_leader", 1.0),
    ("R-036", "skill_tree_effect-sense_up-150-skill_tree_effect_target-all-set_live_leader", 1.0),
    ("R-049", "skill_tree_effect-live_active_skill_effect_up_permil_up-40-skill_tree_effect_target-all-set_live_leader", 1.55),
    ("R-050", "skill_tree_effect-all_parameter_up_permil_up-10-skill_tree_effect_target-all-set_live_leader", 1.0),
    ("R-051", "skill_tree_effect-live_active_skill_effect_up_permil_up-40-skill_tree_effect_target-all-set_live_leader", 1.0),
    ("R-052", "skill_tree_effect-technique_up_permil_up-30-skill_tree_effect_target-all-set_live_leader", 1.0),
    ("R-054", "skill_tree_effect-all_parameter_up_permil_up-10-skill_tree_effect_target-all-set_live_leader", 1.0),
    ("R-055", "skill_tree_effect-all_parameter_up_permil_up-30-skill_tree_effect_target-all-set_live_leader", 1.0),
    ("R-056", "skill_tree_effect-sense_up_permil_up-30-skill_tree_effect_target-all-set_live_leader", 1.0),
    ("R-060", "skill_tree_effect-all_parameter_up_permil_up-10-skill_tree_effect_target-all-set_live_leader", 1.0),
)

# The A-vs-C configuration's own numbers.
MEMBER_PARAMETER = 81009
BOARD_POWER_WITH_INA_LEADING = 37472
BOARD_POWER_WITHOUT = 14200
RED_POWER_DELTA = BOARD_POWER_WITH_INA_LEADING - BOARD_POWER_WITHOUT  # 23,272
RED_POWER_DELTA_PP = 28.73

BOARD_SCORE_BONUS_WITH_INA_LEADING = 0.178
BOARD_SCORE_BONUS_WITHOUT = 0.058
RED_SCORE_DELTA_PP = 12.0
# The Score Bonus the ACTIVE pool showed on that deck, and which the red
# `SkillEffectUpPermilUp` term lifts: Active Skill 64.4 % plus the non-red
# board's own 5.8 % (the B branch's activation-probability nodes, which the
# deck panel already attributes to the board line).
BASE_ACTIVE_SCORE_RATIO = 0.644 + BOARD_SCORE_BONUS_WITHOUT  # 0.702


def _ina_board() -> BoardModel:
    nodes = tuple(
        BoardNode(
            node_id=node_id,
            effects=(parse_effect_id(effect_id, branch=node_id[:1]),),
            connect_multiplier=multiplier,
        )
        for node_id, effect_id, multiplier in INA_RED_NODES
    )
    return BoardModel(characters={"Ina": CharacterBoard(holomem="Ina", nodes=nodes)})


def _members_summing_to(total: int):
    """Five members whose base stats sum to exactly `total`, split evenly
    across the three stats.

    ⚠ The per-stat split of the captured deck's 81,009 Member Parameter is
    NOT recorded — the panel gives one scalar. The split matters because two
    of the red rates are stat-specific
    (Sense +3 %, Technique +3 %) on top of All +6 %, so the prediction moves
    with it — LINEARLY, by 0.03 per point of Performance.
    `test_the_prediction_is_stable_across_every_plausible_stat_split` below
    bounds what the assumption is worth: ~+/- 0.5 % over any split from 30 % to
    40 % Performance, which on this base is ~240 points end to end.

    ⚠ That bound is LARGER than the 86-point gap this fixture shows against the
    game's 23,272, which is why that gap cannot arbitrate anything about the
    model. It is a stated assumption, not a fitted parameter."""
    assert total % 3 == 0, "the even split has to be exact for the pinned numbers"
    per_stat_total = total // 3
    per_card = per_stat_total // 5
    remainder = per_stat_total - per_card * 5
    cards = []
    for index in range(5):
        value = per_card + (remainder if index == 0 else 0)
        performance = technique = sense = value
        cards.append(
            dataclasses.replace(
                make_card(f"member_{index}", member=f"Holomem{index}"),
                stats=Stats(performance=performance, technique=technique, sense=sense),
            )
        )
    return tuple(cards)


def _members_with_performance_share(total: int, p_share: float):
    """Five members summing to `total`, with `p_share` of it in Performance and
    the remainder split evenly between Technique and Sense.

    The counterpart to `_members_summing_to`'s even split: same total, same
    everything else, one knob. Used to show that the unrecorded split — not the
    per-node `Ceiling` — is what moves the A-vs-C prediction."""
    performance_total = round(total * p_share)
    rest = total - performance_total
    technique_total = rest // 2
    sense_total = rest - technique_total

    def per_card(stat_total: int, index: int) -> int:
        base = stat_total // 5
        return base + (stat_total - base * 5 if index == 0 else 0)

    return tuple(
        dataclasses.replace(
            make_card(f"member_{index}", member=f"Holomem{index}"),
            stats=Stats(
                performance=per_card(performance_total, index),
                technique=per_card(technique_total, index),
                sense=per_card(sense_total, index),
            ),
        )
        for index in range(5)
    )


# ==========================================================================
# 1. The acceptance test
# ==========================================================================


def test_ina_red_nodes_land_on_the_measured_power_delta():
    """+28.73 pp of Overall Power, by hand.

    FLAT points, each node's value times its Connect multiplier:

      all_parameter_up : 50*2.4 + 50 + 50*1.55*5 + 50 + 70 = 677.5   -> +677.5 to EACH stat
      sense_up         : 100*2.4 + 100*1.55 + 150          = 545.0
      performance_up   : 100*2.4 + 100*1.55                = 395.0
      technique_up     : 100*2.4 + 100*1.55                = 395.0

      per member: P 1072.5   T 1072.5   S 1222.5   = 3367.5
      x 5 members (the leader contributes no stats)       = 16,837.5

    RATES (per-mille / 1000), all unboosted:

      all  6 %  ->  P +0.06  T +0.06  S +0.06
      tech 3 %  ->  T +0.03
      sense 3 % ->  S +0.03
      so P 0.06, T 0.09, S 0.09 of that stat's own base.

    On 81,009 split evenly (27,003 per stat):

      0.06*27,003 + 0.09*27,003 + 0.09*27,003 = 1,620.18 + 2,430.27 + 2,430.27
                                              = 6,483.0

    Total 16,837.5 + 6,483.0 = **23,318.22** against the game's 23,272 —
    **+0.370 %**, i.e. +28.82 pp against +28.73 pp.

⚠ THIS TEST CANNOT ARBITRATE THE `Ceiling`. Read this before touching either
number.

`BoardEffect.scaled` applies the game's per-node `Ceiling`
(`GetEffectValueWithConnectEffectsInternal` @`0x6811970`), which takes the total
23,318.22 -> 23,358.0, i.e. +0.199 % -> +0.370 % against the 23,272 above. That
looks like a step backwards. It is not evidence either way, for one reason:

    **THE INPUT IS SYNTHETIC.** `_members_summing_to` splits 81,009 EVENLY
    (27,003 per stat). The real deck's split is NOT RECORDED anywhere.

And the prediction is highly sensitive to that unrecorded input, LINEARLY -- not
through the `Ceiling`. The red nodes pay 6 % on Performance but 9 % on Technique
and Sense, so the prediction falls by 0.03 for every point of Performance moved
out of an even split:

    predicted  =  16,875  +  0.09*MP  -  0.03*P_total  +  ceil_residue

The `Ceiling`'s own contribution is +37.5 flat (split-INDEPENDENT, it is per node)
plus a rate-half residue of 2.3 .. 10.6 -- structurally bounded by 15,
one per (card, stat) cell. The split term moves the answer by ~120 across its
plausible range. It swamps the thing this test could be read as measuring.

This model, varying only the split:

    P-share  33.33 % (even, what this test uses)   23,358   +86 vs 23,272
    P-share  33.72 % (account-wide, 97 cards)      23,355   +83
    P-share  34.75 % (the wire deck below)         23,327   +55
    P-share  37.75 % (another owned five-card set) 23,253   -19

The account's own observed P-shares BRACKET 23,272. So the target is inside the
model's reachable band and refutes nothing.

A re-derived 23,272 would not refute the `Ceiling` either, unless the same
capture also carried that deck's per-card, per-stat block: it is the SPLIT that
is missing, not the target. 23,272 is `37,472 - 14,200`, two screen-read
INTEGERS, and 37,472 is confirmed EXACTLY on the wire -- so the panel prints
this integer exactly and 23,272 is exact. The 86 points are real; they are an
artefact of the input, not slop.

WHERE THE `Ceiling` IS ACTUALLY CHECKED -- wire-exact, all 15 cells, and it is
`test_board_wire.py`'s job, not this one: `deck_oracle.json` idx 5 vs idx 30 hold
the SAME five cards at the SAME Member Parameter 104,449, differing only in the
leader, and the model reproduces BOTH board lines EXACTLY (44,682 with Ina
leading, 17,687 without). That is this test's experiment, run against integers
instead of against a synthesised split.

⚠ idx 30 is a `SaveDeck` row and carries `leader: None`, which is the documented
trap -- `SaveDeck` OMITS the leader, so the label does not mean "no leader" and 3
of the 32 rows are affected. The pair is still valid, but NOT because of the
label: Ina (`chr-04003`) is the only character in the account with any red node
unlocked, so a row reading 17,687 rather than 44,682 cannot have had her leading.
The NUMBER disambiguates the missing label. Do not reuse a `leader: None` row
without an argument of that shape.


    The "nearly double" red-branch figure some reports show was never in the
    data: the report that produced `All 53.0 %` had added 470 FLAT stat points
    to 60 per-mille and divided the sum by 10.
    """
    model = _ina_board()
    members = _members_summing_to(MEMBER_PARAMETER)
    leader = make_card("leader_ina", member="Ina")

    contribution = board.resolve(model, leader, members)
    predicted = contribution.power_bonus(members)

    # The two halves, pinned separately so a regression says which one moved.
    flat_total = sum(
        value for card in members for value in contribution.flat_by_card[card.id].values()
    )
    assert flat_total == pytest.approx(16875.0, abs=1e-6)
    assert predicted - flat_total == pytest.approx(6483.0, abs=0.01)

    assert predicted == pytest.approx(23358.0, abs=0.01)

    # ... and against the game's own measurement.
    #
    # ⛔ NO TOLERANCE HERE, DELIBERATELY. A tolerance turns "wrong" into "fine"
    # and reports nothing. The gap is stated as the exact integer it is, so a
    # change in it fails loudly.
    #
    # 86 points, and the docstring names the cause: this test's stat split is
    # synthetic and the real one is unrecorded.
    assert predicted - RED_POWER_DELTA == pytest.approx(86.0, abs=0.01)
    # The same gap in the panel's own units: +28.83 pp against the screen's
    # +28.73. Stated, not absorbed, for the same reason.
    predicted_pp = 100.0 * predicted / MEMBER_PARAMETER
    assert predicted_pp - RED_POWER_DELTA_PP == pytest.approx(0.1038, abs=0.001)

    # Without a board the answer is 0.0.
    assert predicted != 0.0


def test_the_screen_target_cannot_arbitrate_the_ceiling():
    """The 86-point gap is the unrecorded stat split, not a model error.

    Varying ONLY the Performance share -- every other input identical -- walks the
    prediction across 23,272 well inside the shares this account actually exhibits.
    So `RED_POWER_DELTA` neither confirms nor refutes the per-node `Ceiling`, and
    the test above must not be read as if it did.

    The one input that IS pinned here is the flat half: it is per-node and
    therefore split-independent, which is why it gets an exact assertion.
    """
    model = _ina_board()
    leader = make_card("leader_ina", member="Ina")

    def predict(p_share: float) -> float:
        members = _members_with_performance_share(MEMBER_PARAMETER, p_share)
        return board.resolve(model, leader, members).power_bonus(members)

    # The account's own observed Performance shares, over
    # `state/collection.json`: 33.72 % across all 97 owned cards, 37.75 % on
    # another owned five-card set. They BRACKET the screen figure.
    assert predict(0.3372) > RED_POWER_DELTA
    assert predict(0.3775) < RED_POWER_DELTA

    # ... and the walk is monotone in the share, so there is a crossing between
    # them. 0.03 lost per point of Performance moved out of even -- linear, and
    # nothing to do with the `Ceiling`.
    walk = [predict(s / 1000.0) for s in range(330, 390, 10)]
    assert walk == sorted(walk, reverse=True)

    # The `Ceiling`'s own rate-half contribution is bounded by one per (card,
    # stat) cell -- 15 -- so it cannot be what moves this number by 86.
    spread = max(walk) - min(walk)
    assert spread > 100.0


def test_ina_red_nodes_land_on_the_measured_score_bonus_delta():
    """+12.0 pp of Score Bonus, by hand.

    `live_active_skill_effect_up_permil_up` is the game's own
    `SkillEffectUpPermilUp` — the addend inside
    `ActiveSkillEffectDataCollection::CalcScoreUpRatio`'s
    `(1 + SkillEffectUpPermilUp/1000 + B)` factor, which multiplies the ACTIVE
    pool. Ina's four UNCONDITIONAL red nodes carrying it:

      R-032  20 * 1.55 =  31
      R-049  40 * 1.55 =  62
      R-034  40 * 1.00 =  40
      R-051  40 * 1.00 =  40
                        ---
                        173 per-mille = 0.173

    R-002 carries 100 per-mille at x2.4 = 240, but it is gated by a
    `skill_tree_effect_passive_trigger-music_skill_tree_character` — it fires
    only on that holomem's OWN song, which a song-agnostic deck panel does not
    evaluate. It is carried on `conditional_skill_effect_up`, never summed in.

    The deck panel attributes to the board the uplift that term produces on
    the score beneath it: Active Skill 64.4 % plus the non-red board's own
    5.8 %, so

        0.173 * 0.702 = **0.121446** = +12.14 pp against the game's +12.0 pp,
        **+1.2 %**.

    Note this is 173, not the 240 that `skilltree.json`'s `Act.Power 24.0 %`
    reports, and not "exactly twice the observed 12.0". Both of those sum the
    conditional node in and neither applies a Connect multiplier; the agreement
    at 24.0 = 2 x 12.0 is a coincidence of two errors.
    """
    model = _ina_board()
    members = _members_summing_to(MEMBER_PARAMETER)
    leader = make_card("leader_ina", member="Ina")

    contribution = board.resolve(model, leader, members)

    assert contribution.skill_effect_up == pytest.approx(0.173, abs=1e-9)
    assert contribution.conditional_skill_effect_up == pytest.approx(0.240, abs=1e-9)
    # The naive sum, kept so its refutation is visible.
    naive = contribution.skill_effect_up + contribution.conditional_skill_effect_up
    assert naive == pytest.approx(0.413, abs=1e-9)

    predicted = contribution.deck_screen_score_bonus(BASE_ACTIVE_SCORE_RATIO)
    assert predicted == pytest.approx(0.121446, abs=1e-6)
    assert 100.0 * predicted == pytest.approx(RED_SCORE_DELTA_PP, abs=0.15)

    # Without a board the answer is 0.0.
    assert predicted != 0.0


# A singer-conditional STAT rate — the R-044/062/063 family, "+10% Technique to
# all WHEN [character] is a singer". Same shape as R-002's conditional skill term
# but a stat, not the score-up addend. Leader-scoped, so on top of the singer
# condition the holomem must also be the leader (both true on the leader's own
# song).
CONDITIONAL_STAT = (
    "skill_tree_effect-technique_up_permil_up-100"
    "-skill_tree_effect_target-all"
    "-skill_tree_effect_passive_trigger-music_skill_tree_character-set_live_leader"
)


def test_singer_conditional_stat_power_fires_only_when_the_holomem_sings():
    """R-044/062/063 pay `+stat% to all` only when their holomem is a credited
    singer — dropped by the song-agnostic `power_bonus_for`/`_terms` (conditional),
    added back by `conditional_stat_power_for` when the cast contains the holomem.

    Effect is Technique +10% (permil 100), leader-scoped. Five members at 1000
    Technique: `ceil(0.10 * 1000) = 100` each, `100 * 5 = 500` when Ina both leads
    and sings. Nothing on a songless screen, and nothing when Ina sings but is not
    the leader (the leader-scoped effect reaches no card then)."""
    node = BoardNode("R-044", (parse_effect_id(CONDITIONAL_STAT, branch="R"),))
    model = BoardModel(characters={"Ina": CharacterBoard("Ina", (node,))})
    members = tuple(
        dataclasses.replace(make_card(f"m{i}"), stats=Stats(1000, 1000, 1000))
        for i in range(5)
    )

    # song-agnostic power excludes it (conditional), so song-agnostic callers are unmoved
    assert model.power_bonus_for(members, "Ina") == 0.0
    # no cast -> nothing
    assert model.conditional_stat_power_for(members, "Ina", ()) == 0.0
    # Ina leads AND sings -> +10% Technique to each of the five
    assert model.conditional_stat_power_for(members, "Ina", ("Ina",)) == 500.0
    # Ina sings but does NOT lead -> a leader-scoped effect reaches nobody
    assert model.conditional_stat_power_for(members, None, ("Ina",)) == 0.0
    # a different singer, Ina silent -> the trigger is unmet
    assert model.conditional_stat_power_for(members, "Ina", ("Nene",)) == 0.0


def test_the_prediction_is_stable_across_every_plausible_stat_split():
    """How much the one unrecorded input is worth — bounded, and it is NOT small.

    The sweep is **244 points end to end** and it CONTAINS 23,272, which is
    precisely why the A-vs-C figure cannot arbitrate the model: a fixture that
    brackets the target does not confirm it.

    The captured panel gives Member Parameter as one scalar (81,009) with no
    per-stat split, and two of the three red rates are stat-specific. Closed form:
    with a Performance share `s` the rate half is
    `81,009 * (0.06*s + 0.09*(1-s)) = 81,009 * (0.09 - 0.03*s)` — LINEAR in `s`,
    losing 0.03 per point of Performance moved out of even.

    With the model's own flat half (16,875, per-node `Ceiling` applied,
    split-independent):

        s = 0.300 -> rate 6,570 -> 23,445   (+173 vs the game's 23,272)
        s = 1/3   -> rate 6,483 -> 23,358   ( +86, what the acceptance test shows)
        s = 0.350 -> rate 6,447 -> 23,322   ( +50)
        s = 0.400 -> rate 6,326 -> 23,201   ( -71)

    ⚠ Both halves are taken from the model. Mixing the PRE-`Ceiling` flat half
    `16,837.5` with the model's rate half would compare two different models.
    """
    model = _ina_board()
    leader = make_card("leader_ina", member="Ina")

    predictions = []
    for performance_share in (0.30, 1 / 3, 0.35, 0.40):
        members = _members_with_performance_share(MEMBER_PARAMETER, performance_share)
        predictions.append(board.resolve(model, leader, members).power_bonus(members))

    assert min(predictions) == pytest.approx(23201.0, abs=1.0)
    assert max(predictions) == pytest.approx(23445.0, abs=1.0)

    # The point of the test, stated rather than implied: the unrecorded input is
    # worth more than the gap anyone might try to read as a model error.
    spread = max(predictions) - min(predictions)
    assert spread == pytest.approx(244.0, abs=2.0)
    assert spread > abs(23358.0 - RED_POWER_DELTA)

    # And the band brackets the game's own figure, so no split is refuted by it.
    assert min(predictions) < RED_POWER_DELTA < max(predictions)


def test_the_53_percent_figure_is_flat_points_divided_by_ten():
    """The "nearly double" red-branch reading is a units bug, reproduced here so it cannot be
    re-derived by accident.

    `skilltree.json` reported Ina's 27 red nodes as
    `All 53.0 % / Sense 38.0 % / Act.Power 24.0 % / Tech 23.0 % / Perf 20.0 %`.
    Summed WITHOUT Connect multipliers, the raw master values are

        flat  : All 470   Sense 350   Tech 200   Perf 200
        permil: All  60   Sense  30   Tech  30   Act.Power 240

    and `(flat + permil) / 10` is `53.0 / 38.0 / 23.0 / 20.0`, with
    `240/10 = 24.0` for Act.Power — every one of the five, to the digit. A
    flat stat point was printed as a tenth of a percent.

    `SkillTreeNode` also has no per-node level for a level-1-vs-level-2
    hypothesis to be about: 137 of 152 group ids have exactly one row, the 12
    multi-row groups are all Y (a `-solo` vs `-solo-fuwamoko` song variant),
    and the remaining 3 are the 54-row `character_grouping` groups."""
    flat = {"all": 0.0, "sense": 0.0, "technique": 0.0, "performance": 0.0}
    permil = {"all": 0.0, "sense": 0.0, "technique": 0.0, "performance": 0.0,
              "act_power": 0.0}
    for node_id, effect_id, _multiplier in INA_RED_NODES:
        effect = parse_effect_id(effect_id, branch=node_id[:1])
        if effect.kind == "stat":
            key = "all" if effect.stat == "all" else effect.stat
            flat[key] += effect.value
        elif effect.kind == "rate":
            key = "all" if effect.stat == "all" else effect.stat
            permil[key] += effect.value * 1000
        elif effect.kind == "skill_effect_up":
            permil["act_power"] += effect.value * 1000

    assert flat == {"all": 470.0, "sense": 350.0, "technique": 200.0, "performance": 200.0}
    assert permil == {
        "all": 60.0,
        "sense": 30.0,
        "technique": 30.0,
        "performance": 0.0,
        "act_power": 240.0,
    }

    assert (flat["all"] + permil["all"]) / 10 == 53.0
    assert (flat["sense"] + permil["sense"]) / 10 == 38.0
    assert (flat["technique"] + permil["technique"]) / 10 == 23.0
    assert (flat["performance"] + permil["performance"]) / 10 == 20.0
    assert permil["act_power"] / 10 == 24.0


def test_the_red_branch_reaches_five_cards_and_not_six():
    """Independent corroboration of the five-member model.

    The R branch's effect id says `-skill_tree_effect_target-all-`, and "all"
    could mean the six cards on the team screen or the five that carry stats.
    Six overshoots the game's 23,272 by 20 %:

        16,837.5 * 6/5 = 20,205 flat, + 6,483.0 = 26,685.7 -> +32.94 pp

    against +28.73 pp measured. The five-card reading is the one that fits,
    which agrees with "the leader contributes its outfit skill and nothing
    else" from a completely different direction."""
    model = _ina_board()
    leader = make_card("leader_ina", member="Ina")
    members = _members_summing_to(MEMBER_PARAMETER)

    five = board.resolve(model, leader, members).power_bonus(members)
    six_flat = 16837.5 * 6 / 5
    six = six_flat + (five - 16837.5)

    assert five == pytest.approx(23358.0, abs=0.01)
    assert six == pytest.approx(26725.5, abs=0.01)
    assert abs(six - RED_POWER_DELTA) / RED_POWER_DELTA > 0.14


def test_a_leader_who_is_not_ina_gets_none_of_her_red_nodes():
    """A-vs-C's other half: with Calliope leading, Ina's red branch is worth
    exactly zero — every term, not just the power one. Ina is the only
    character in the captured account with any red node unlocked, so this IS
    configuration C."""
    model = _ina_board()
    members = _members_summing_to(MEMBER_PARAMETER)
    contribution = board.resolve(model, make_card("leader_calli", member="Calli"), members)

    assert contribution.power_bonus(members) == 0.0
    assert contribution.skill_effect_up == 0.0
    assert contribution.conditional_skill_effect_up == 0.0
    assert contribution.score_bonus_rate() == 0.0


# ==========================================================================
# 2. Green is a constant AND a discriminator
# ==========================================================================

# 179 of 182 G rows. Every card, always.
GREEN_CONSTANT = "skill_tree_effect-all_parameter_up-5-skill_tree_effect_target-all-always"
# G-008 / G-011 / G-021: one row per character, targeted at HER OWN grouping.
GREEN_MYTH = (
    "skill_tree_effect-all_parameter_up_for_character_grouping-150"
    "-skill_tree_effect_target-character_grouping-grp-myth-always"
)


def test_c09_green_all_always_nodes_are_a_constant_that_cancels_in_a_ranking():
    """`-skill_tree_effect_target-all-always` reaches all five
    members of ANY team, so two candidate teams get the identical addend and
    it cancels out of the ranking.

    `all_parameter_up-5` on five members is `5 * 3 stats * 5 cards = 75`
    points, whichever five cards they are."""
    node = BoardNode("G-001", (parse_effect_id(GREEN_CONSTANT, branch="G"),))
    model = BoardModel(characters={"Ina": CharacterBoard("Ina", (node,))})
    leader = make_card("leader", member="Leader")

    team_a = _members_summing_to(81009)
    team_b = tuple(
        dataclasses.replace(card, stats=Stats(1000, 1000, 1000)) for card in team_a
    )

    a = board.resolve(model, leader, team_a).power_bonus(team_a)
    b = board.resolve(model, leader, team_b).power_bonus(team_b)
    assert a == 75.0
    assert b == 75.0
    assert a == b  # the constant: identical for two completely different teams


def test_c09_green_character_grouping_nodes_are_a_per_team_discriminator():
    """Gen-scoped green nodes DISCRIMINATE between teams.

    `G-008`, `G-011` and `G-021` each carry 54 rows — one per character — and
    each row's target is `character_grouping-grp-<that character's group>`.
    The family is `all_parameter_up_for_character_grouping`, so the value
    reaches only the team's cards that share the grouping. It therefore
    DISCRIMINATES between teams, and treating green as one team-constant
    addend is the wrong shape for these three nodes.

    Three Myth members out of five: `150 * 3 stats * 3 cards = 1,350`.
    Zero Myth members: `0`. Same board, same team size, different number."""
    node = BoardNode("G-011", (parse_effect_id(GREEN_MYTH, branch="G"),))
    model = BoardModel(characters={"Ina": CharacterBoard("Ina", (node,))})
    leader = make_card("leader", member="Leader")

    def _team(myth_count: int):
        return tuple(
            dataclasses.replace(
                make_card(f"m{i}", member=f"Holomem{i}"),
                generations=frozenset({"grp-myth"} if i < myth_count else {"grp-gen_0"}),
                stats=Stats(1000, 1000, 1000),
            )
            for i in range(5)
        )

    three_myth = _team(3)
    zero_myth = _team(0)
    five_myth = _team(5)

    assert board.resolve(model, leader, three_myth).power_bonus(three_myth) == 1350.0
    assert board.resolve(model, leader, zero_myth).power_bonus(zero_myth) == 0.0
    assert board.resolve(model, leader, five_myth).power_bonus(five_myth) == 2250.0

    # The claim in one assertion: it is not a constant.
    assert (
        board.resolve(model, leader, three_myth).power_bonus(three_myth)
        != board.resolve(model, leader, zero_myth).power_bonus(zero_myth)
    )


def test_c09_grouping_matches_the_catalogue_display_name_not_only_the_token():
    """The effect id names the group as a TOKEN (`grp-myth`), but the catalogue
    ships `Card.generations` as the DISPLAY name (`Myth`). BOTH must reach the card
    or every gen-scoped green (G-008/011/021) scores 0 on real data and the
    family looks inert. Same +150 node
    as the token test above; a team whose Myth cards carry the display string still
    gets the identical 1,350 (150 x 3 stats x 3 cards)."""
    node = BoardNode("G-011", (parse_effect_id(GREEN_MYTH, branch="G"),))
    model = BoardModel(characters={"Ina": CharacterBoard("Ina", (node,))})
    leader = make_card("leader", member="Leader")
    display = tuple(
        dataclasses.replace(
            make_card(f"m{i}", member=f"Holomem{i}"),
            generations=frozenset({"Myth"}),  # the catalogue's display name, not grp-myth
            stats=Stats(1000, 1000, 1000),
        )
        for i in range(3)
    ) + tuple(
        dataclasses.replace(
            make_card(f"n{i}", member=f"Other{i}"),
            generations=frozenset({"holoX"}),
            stats=Stats(1000, 1000, 1000),
        )
        for i in range(2)
    )
    assert board.resolve(model, leader, display).power_bonus(display) == 1350.0


def test_c09_the_grouping_scope_is_read_from_the_effect_id_not_assumed():
    """The scope is parsed out of the id, so the two green shapes are told
    apart structurally rather than by node number."""
    constant = parse_effect_id(GREEN_CONSTANT, branch="G")
    grouped = parse_effect_id(GREEN_MYTH, branch="G")

    assert constant.scope == "account"
    assert constant.grouping is None
    assert grouped.scope == "grouping"
    assert grouped.grouping == "grp-myth"
    assert grouped.value == 150.0


# ==========================================================================
# 3. Branch scopes, Connect arithmetic, and the parser's refusal to guess
# ==========================================================================


def test_blue_nodes_reach_only_their_own_holomems_card():
    """`-skill_tree_effect_target-skill_tree_character-always`: that
    character's own card, and only while she is one of the five members.
    `all_parameter_up-50` on one member is `50 * 3 = 150`."""
    effect_id = (
        "skill_tree_effect-all_parameter_up-50"
        "-skill_tree_effect_target-skill_tree_character-always"
    )
    node = BoardNode("B-001", (parse_effect_id(effect_id, branch="B"),))
    model = BoardModel(characters={"Nene": CharacterBoard("Nene", (node,))})
    leader = make_card("leader", member="Leader")

    with_nene = tuple(
        dataclasses.replace(
            make_card(f"m{i}", member="Nene" if i == 0 else f"Holomem{i}"),
            stats=Stats(1000, 1000, 1000),
        )
        for i in range(5)
    )
    without_nene = tuple(
        dataclasses.replace(
            make_card(f"m{i}", member=f"Holomem{i}"), stats=Stats(1000, 1000, 1000)
        )
        for i in range(5)
    )

    assert board.resolve(model, leader, with_nene).power_bonus(with_nene) == 150.0
    assert board.resolve(model, leader, without_nene).power_bonus(without_nene) == 0.0


def test_a_leaders_blue_nodes_do_not_count_while_she_is_not_a_member():
    """Not known from the game, so the conservative default is implemented: blue is defined as
    "while she is in the unit", and the leader slot is not a member slot, so
    a leader who is not also among the five contributes no blue."""
    effect_id = (
        "skill_tree_effect-all_parameter_up-50"
        "-skill_tree_effect_target-skill_tree_character-always"
    )
    node = BoardNode("B-001", (parse_effect_id(effect_id, branch="B"),))
    model = BoardModel(characters={"Nene": CharacterBoard("Nene", (node,))})
    members = tuple(
        dataclasses.replace(
            make_card(f"m{i}", member=f"Holomem{i}"), stats=Stats(1000, 1000, 1000)
        )
        for i in range(5)
    )
    leader = make_card("leader_nene", member="Nene")
    assert board.resolve(model, leader, members).power_bonus(members) == 0.0


def test_connect_multiplies_the_number_whatever_its_unit():
    """Confirmed against the deck screen: Calliope 5★ at 140 % turned "+50 to All"
    into +120, "Sense +100" into +240 and "Score Support 10 %" into 24 % —
    x2.4 on all three. So one multiplication, no per-family table."""
    flat_all = "skill_tree_effect-all_parameter_up-50-skill_tree_effect_target-all-always"
    flat_sense = "skill_tree_effect-sense_up-100-skill_tree_effect_target-all-always"
    rate = (
        "skill_tree_effect-live_active_skill_effect_up_permil_up-100"
        "-skill_tree_effect_target-all-set_live_leader"
    )
    nodes = (
        BoardNode("G-001", (parse_effect_id(flat_all, branch="G"),), 2.4),
        BoardNode("G-002", (parse_effect_id(flat_sense, branch="G"),), 2.4),
        BoardNode("R-001", (parse_effect_id(rate, branch="R"),), 2.4),
    )
    model = BoardModel(characters={"Calli": CharacterBoard("Calli", nodes)})
    leader = make_card("leader_calli", member="Calli")
    members = (
        dataclasses.replace(make_card("m0", member="M0"), stats=Stats(1000, 1000, 1000)),
    )

    contribution = board.resolve(model, leader, members)
    flat = contribution.flat_by_card["m0"]
    assert flat["performance"] == 120.0  # 50 -> 120
    assert flat["technique"] == 120.0
    assert flat["sense"] == 120.0 + 240.0  # 50 -> 120 plus 100 -> 240
    assert contribution.skill_effect_up == pytest.approx(0.24)  # 10 % -> 24 %


def test_connect_multiplier_of_one_is_the_identity():
    effect = parse_effect_id(GREEN_CONSTANT, branch="G")
    assert effect.scaled(1.0) is effect
    assert effect.scaled(2.0).value == effect.value * 2


def test_bloom_5_connect_overshoots_the_acceptance_number():
    """Why the board ships Connect level 1, pinned as a number instead of a
    claim.

    Both of Ina's socket cards are `rare_5`, so master
    `SkillTreeConnectEffect` gives each of them a Bloom-5 row:
    `center_area-001-rare_5` 1400 -> 1900 permil (x2.4 -> x2.9) and
    `general_purpose_area-001-rare_5` 550 -> 1050 (x1.55 -> x2.05). Level 2 is
    exactly level 1 + 500 permil on all 20 effects in that table, so this is a
    lookup, not a fit.

    Substituting them overshoots the game's own measurement by **+16.48 %**,
    against +0.20 % for the base level. That gap is what licenses level 1 as
    the shipped reading — and, read the other way round, it is evidence about
    the captured account: its two socket cards are below Bloom 5. A
    collection that defaults every card to `max` therefore cannot be trusted
    to supply the Connect level, which is the trap
    `tools/recon/proxy/build_board.py` has to avoid when it derives the level
    from a collection instead of taking `--connect-level`."""
    members = _members_summing_to(MEMBER_PARAMETER)
    leader = make_card("leader_ina", member="Ina")

    def power_at(multiplier_of) -> float:
        nodes = tuple(
            BoardNode(
                node_id=node_id,
                effects=(parse_effect_id(effect_id, branch=node_id[:1]),),
                connect_multiplier=multiplier_of(multiplier),
            )
            for node_id, effect_id, multiplier in INA_RED_NODES
        )
        model = BoardModel(
            characters={"Ina": CharacterBoard(holomem="Ina", nodes=nodes)}
        )
        return board.resolve(model, leader, members).power_bonus(members)

    base = power_at(lambda m: m)
    bloom5 = power_at(lambda m: BLOOM5_CONNECT_MULTIPLIER[m])

    # The shipped reading, restating the acceptance test from this angle.
    assert base == pytest.approx(23358.0, abs=0.01)
    assert 100.0 * (base / RED_POWER_DELTA - 1) == pytest.approx(0.370, abs=0.01)

    # Bloom 5 on both slots.
    assert bloom5 == pytest.approx(27108.0, abs=0.01)
    assert 100.0 * (bloom5 / RED_POWER_DELTA - 1) == pytest.approx(16.48, abs=0.01)

    # The property that actually matters, stated so it survives a re-measure of
    # the two magnitudes: Bloom 5 is much further from the observation than
    # base, and on the high side. A regression that inverted the levels, or
    # that let a `card_default: "max"` collection push every socket to level 2,
    # fails here.
    assert bloom5 > base
    assert abs(bloom5 / RED_POWER_DELTA - 1) > 20 * abs(base / RED_POWER_DELTA - 1)


def test_yellow_song_scoped_bonuses_are_separated_by_song_scope():
    """Only the song-INDEPENDENT bucket is
    offered to `scoring.py` as `ScoreUpBonusRatioBySkillTree`. The solo and
    group buckets are carried, unscored, because they are a per-song
    question that a song-agnostic ranking cannot answer."""
    all_songs = (
        "skill_tree_effect-live_score_bonus_add_permil_up_by_music_skill_tree_character"
        "_and_music_singer_type-1-skill_tree_effect_passive_trigger-music_skill_tree"
        "_character_and_music_singer_type-all-always-all"
    )
    solo = (
        "skill_tree_effect-live_score_bonus_add_permil_up_by_music_skill_tree_character"
        "_and_music_singer_type-5-skill_tree_effect_passive_trigger-music_skill_tree"
        "_character_and_music_singer_type-solo-always-solo"
    )
    nodes = (
        BoardNode("Y-030", (parse_effect_id(all_songs, branch="Y"),)),
        BoardNode("Y-001", (parse_effect_id(solo, branch="Y"),)),
    )
    model = BoardModel(characters={"Ina": CharacterBoard("Ina", nodes)})
    members = _members_summing_to(3000)

    contribution = board.resolve(model, make_card("l", member="L"), members)
    assert contribution.song_score_bonus == {"all": 0.001, "solo": 0.005}
    assert contribution.score_bonus_rate() == 0.001  # the song-independent half only


def _solo_yellow(value: int) -> str:
    return (
        "skill_tree_effect-live_score_bonus_add_permil_up_by_music_skill_tree_character"
        f"_and_music_singer_type-{value}-skill_tree_effect_passive_trigger-music_skill_tree"
        "_character_and_music_singer_type-solo-always-solo"
    )


def test_duo_solo_song_sums_both_singers_solo_nodes_under_one_cap():
    """A FUWAMOCO-style 2-singer `solo` song (chr-04016 + chr-04017) aggregates
    BOTH members' `solo`-scope nodes under ONE cap, as the game's
    `CalculateCurrentEffectValue` does (aggregate over the song's own singers, filtered
    by `musicSingerType_`, single clamp). This pins sum-then-cap (H1), not
    cap-each-then-sum (H2): 60 + 70 = 130 permil clamps to 100, never 130."""
    model = BoardModel(
        characters={
            "Fuwawa": CharacterBoard(
                "Fuwawa", (BoardNode("Y-001", (parse_effect_id(_solo_yellow(60), branch="Y"),)),)
            ),
            "Mococo": CharacterBoard(
                "Mococo", (BoardNode("Y-001", (parse_effect_id(_solo_yellow(70), branch="Y"),)),)
            ),
        },
        name_by_chr={"chr-04016": "Fuwawa", "chr-04017": "Mococo"},
    )
    both = ("chr-04016", "chr-04017")
    # sum-then-cap: min(0.060 + 0.070, 0.100) == 0.100  (H2 would give 0.130)
    assert model.song_score_bonus_for(both, scope="solo") == pytest.approx(0.1)
    # A single FUWAMOCO singer under the cap passes through unclamped.
    assert model.song_score_bonus_for(("chr-04016",), scope="solo") == pytest.approx(0.06)


def test_song_score_bonus_by_singer_is_each_singers_share_before_the_cap():
    """The page's "On this song" line: whose yellows the bonus comes from, and
    what the cap wastes -- 60 + 70 permil per singer, uncapped, while the song counts 100."""
    model = BoardModel(
        characters={
            "Fuwawa": CharacterBoard(
                "Fuwawa", (BoardNode("Y-001", (parse_effect_id(_solo_yellow(60), branch="Y"),)),)
            ),
            "Mococo": CharacterBoard(
                "Mococo", (BoardNode("Y-001", (parse_effect_id(_solo_yellow(70), branch="Y"),)),)
            ),
        },
        name_by_chr={"chr-04016": "Fuwawa", "chr-04017": "Mococo"},
    )
    got = model.song_score_bonus_by_singer(("chr-04016", "chr-04017"), scope="solo")
    assert got == pytest.approx({"Fuwawa": 0.06, "Mococo": 0.07})
    assert model.song_score_bonus_for(("chr-04016", "chr-04017"), scope="solo") == pytest.approx(0.1)


def test_songchart_admits_a_two_singer_duo_solo_but_not_three():
    """`SongChart` accepts a duo `solo` (the real FUWAMOCO case) and still
    rejects 3+ singers as a GROUP mislabelled `solo`."""
    from hbuilder.domain.song import ChartNote, SongChart, SongError

    def _chart(singers):
        return SongChart(
            music_id="m0191", difficulty="expert", difficulty_level=1,
            live_score_coefficient_permil=1000,
            notes=(ChartNote(time=0.0, perfect_permil=1000),),
            special_cues=(0.0, 0.0, 0.0, 0.0, 0.0),
            singers=tuple(singers), singer_type="solo",
        )

    duo = _chart(("chr-04016", "chr-04017"))
    assert duo.singers == ("chr-04016", "chr-04017")
    _chart(("chr-04016",))  # a lone singer is still fine
    with pytest.raises(SongError, match="distinct singers"):
        _chart(("chr-04016", "chr-04017", "chr-00015"))


def test_reward_families_are_parsed_and_deliberately_worth_zero():
    """Holowork/minigame reward families reach neither power nor score. They
    are routed to `"ignored"` rather than raising, because they are real board
    effects — but they are recognised, not silently unmatched."""
    effect_id = (
        "skill_tree_effect-work_reward_quantity_up_permil_up-50"
        "-skill_tree_effect_target-skill_tree_character-work-item-item-card_level_up"
    )
    effect = parse_effect_id(effect_id, branch="Y")
    assert effect.kind == "ignored"
    assert effect.value == 0.0


def test_an_unknown_effect_family_raises_rather_than_scoring_zero():
    """"Verify artefacts, not counters": a board term that is silently zero is
    indistinguishable from a board term that is correctly zero, which is the
    exact shape of an unmodelled board."""
    with pytest.raises(BoardError, match="unrouted board effect family"):
        parse_effect_id(
            "skill_tree_effect-a_family_nobody_routed-50"
            "-skill_tree_effect_target-all-always",
            branch="G",
        )
    with pytest.raises(BoardError, match="unparseable"):
        parse_effect_id("not an effect id at all", branch="G")


def test_activation_and_cooldown_riders_are_kept_per_member():
    """The B branch is the ONLY supplier of the cooldown axis — zero rows in
    any skill-effect table carry a `cool_time` effect. Both riders are
    per-holomem, so they are kept keyed by
    card and never summed into a team-wide scalar."""
    prob = (
        "skill_tree_effect-live_active_skill_activation_probability_up_permil_up-60"
        "-skill_tree_effect_target-skill_tree_character-always"
    )
    cool = (
        "skill_tree_effect-live_active_skill_cool_time_shorten_permil_up-40"
        "-skill_tree_effect_target-skill_tree_character-always"
    )
    nodes = (
        BoardNode("B-016", (parse_effect_id(prob, branch="B"),)),
        BoardNode("B-017", (parse_effect_id(cool, branch="B"),)),
    )
    model = BoardModel(characters={"Nene": CharacterBoard("Nene", nodes)})
    members = tuple(
        dataclasses.replace(
            make_card(f"m{i}", member="Nene" if i == 2 else f"Holomem{i}"),
            stats=Stats(1000, 1000, 1000),
        )
        for i in range(5)
    )
    contribution = board.resolve(model, make_card("l", member="L"), members)

    assert contribution.activation_rate_up == {"m2": 0.06}
    assert contribution.cooldown_shorten == {"m2": 0.04}
    # ... and they contribute nothing to power, which is the point of keeping
    # them out of the stat block.
    assert contribution.power_bonus(members) == 0.0


# ==========================================================================
# 4. The scoring.py wiring
# ==========================================================================


def _team_with_ina_leading():
    members = tuple(
        dataclasses.replace(
            make_card(f"m{i}", member=f"Holomem{i}"), stats=Stats(100, 100, 100)
        )
        for i in range(5)
    )
    return make_team([make_card("leader_ina", member="Ina"), *members])


def test_scoring_is_unchanged_when_no_board_is_configured():
    """The shipped default. `cfg.board is None` and `cfg.board_score_bonus` is
    empty, so the board stage is exactly the identity."""
    team = _team_with_ina_leading()
    cfg = make_scoring_config()
    assert cfg.board is None

    # Base stat sum is 5 x 300 = 1500 with no skills on any card.
    assert score_fast(team, cfg) == 1500.0


def test_a_configured_board_adds_its_power_as_a_summand_not_a_rate():
    """`Overall Power = Member Parameter + ... + Holomem Board Bonus + ...`,
    as `Live/GetDeck` reports it, reconciling exactly on captured
    configurations. So it is added to the stat sum BEFORE the score factors
    multiply, never folded into a rate.

    One green `all_parameter_up-5` node reaches all five members: `5 * 3 * 5 =
    75`. The team's own stat sum is 1500, so the total goes 1500 -> 1575 —
    additive, and exactly 75, not 1500 x anything."""
    node = BoardNode("G-001", (parse_effect_id(GREEN_CONSTANT, branch="G"),))
    model = BoardModel(characters={"Ina": CharacterBoard("Ina", (node,))})
    team = _team_with_ina_leading()

    without = score_fast(team, make_scoring_config())
    with_board = score_fast(team, dataclasses.replace(make_scoring_config(), board=model))

    assert without == 1500.0
    assert with_board == 1575.0
    assert with_board - without == 75.0


def test_a_red_branch_makes_the_board_leader_dependent_in_both_drivers():
    """The red branch applies only while its holomem LEADS, so unlike
    `cfg.board_score_bonus` the board term is NOT leader-invariant. Both
    `score_fast` and `score_card_set` must agree on that, member for member
    and leader for leader — the driver parity property.

    One red `all_parameter_up-50` node: `50 * 3 * 5 = 750` when Ina leads,
    `0` when she does not."""
    red = "skill_tree_effect-all_parameter_up-50-skill_tree_effect_target-all-set_live_leader"
    node = BoardNode("R-001", (parse_effect_id(red, branch="R"),))
    model = BoardModel(characters={"Ina": CharacterBoard("Ina", (node,))})
    cfg = dataclasses.replace(make_scoring_config(), board=model)

    members = tuple(
        dataclasses.replace(
            make_card(f"m{i}", member=f"Holomem{i}"), stats=Stats(100, 100, 100)
        )
        for i in range(5)
    )
    ina = make_card("leader_ina", member="Ina")
    calli = make_card("leader_calli", member="Calli")

    ina_total = score_fast(make_team([ina, *members]), cfg)
    calli_total = score_fast(make_team([calli, *members]), cfg)
    assert ina_total == 2250.0  # 1500 + 750
    assert calli_total == 1500.0
    assert ina_total - calli_total == 750.0

    batch = {result.leader_id: result.total_score for result in score_card_set(
        members, (ina, calli), cfg
    )}
    assert batch["leader_ina"] == ina_total
    assert batch["leader_calli"] == calli_total


def test_a_configured_board_supersedes_the_board_score_bonus_dict():
    """Both hooks exist; the structural model wins when it is set, because a
    per-holomem float cannot express "the whole team, but only while she
    leads". Asserted with numbers so a silent both-applied regression fails:
    the dict alone would give 1500 x 1.10 = 1650."""
    node = BoardNode("G-001", (parse_effect_id(GREEN_CONSTANT, branch="G"),))
    model = BoardModel(characters={"Ina": CharacterBoard("Ina", (node,))})
    team = _team_with_ina_leading()

    dict_only = dataclasses.replace(
        make_scoring_config(), board_score_bonus={f"Holomem{i}": 0.02 for i in range(5)}
    )
    both = dataclasses.replace(dict_only, board=model)

    assert score_fast(team, dict_only) == pytest.approx(1650.0)
    assert score_fast(team, both) == pytest.approx(1575.0)


# ==========================================================================
# 5. Loading
# ==========================================================================


def test_an_absent_board_file_loads_as_none_not_as_an_empty_board():
    """`data/` ships no board — it is captured ACCOUNT state. `None` is the
    first-class answer, and it makes the scoring stage the exact identity."""
    assert board.load_board("__no_such_board_file__.json") is None


def test_a_board_file_round_trips_through_the_loader_to_the_same_numbers(tmp_path):
    """Ina's whole red branch, written out and read back, lands on the same
    23,318.22 the in-memory fixture does — so the file format is not a second,
    separately-drifting description of the board."""
    path = tmp_path / "board.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "characters": {
                    "Ina": [
                        {
                            "node": node_id,
                            "effects": [effect_id],
                            "connect_multiplier": multiplier,
                        }
                        for node_id, effect_id, multiplier in INA_RED_NODES
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    model = board.load_board(path)
    assert model is not None
    members = _members_summing_to(MEMBER_PARAMETER)
    leader = make_card("leader_ina", member="Ina")
    contribution = board.resolve(model, leader, members)
    assert contribution.power_bonus(members) == pytest.approx(23358.0, abs=0.01)
    assert contribution.skill_effect_up == pytest.approx(0.173, abs=1e-9)


def test_the_loader_refuses_a_connect_multiplier_below_one():
    """`1 + permil/1000` is never below 1. A value below it means someone
    wrote a raw per-mille or a fraction, and silently accepting it would
    SHRINK the board rather than raising."""
    with pytest.raises(board.BoardError, match="connect_multiplier"):
        board.board_from_mapping(
            {
                "characters": {
                    "Ina": [
                        {
                            "node": "G-001",
                            "effects": [GREEN_CONSTANT],
                            "connect_multiplier": 0.4,
                        }
                    ]
                }
            }
        )


def test_the_loader_refuses_a_board_without_a_characters_object():
    with pytest.raises(board.BoardError, match="characters"):
        board.board_from_mapping({"schema_version": "1"})
