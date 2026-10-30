"""The board LANDING: the converter that fills `ScoringConfig.board`, and the
CLI flag that hands it to the solver.

`tests/test_board.py` already pins the board MODEL against its acceptance
number. This file pins the two things that connect that model to a real
answer:

1. `tools/recon/proxy/build_board.py` — the join from captured account state
   (`chr-NNNNN` -> group ids -> socket occupants) to what `load_board` reads
   (holomem name -> effect ids -> Connect multiplier). Without this join
   `ScoringConfig.board` is `None` and the Holomem Board — 29.4-48.7 % of
   Overall Power in the game's own `Live/GetDeck` numbers — scores as
   exactly zero.
2. `hbuilder.cli` `--board` / `--no-board`, and the requirement that a run
   SAYS which of the two states it was in. A solve that silently moved by
   30 % between two invocations would be worse than never landing the board.

The fixtures here are synthetic, not a real captured account: `dumps/` is
gitignored account data. The one test that reads the real dumps skips when they
are absent, which is the normal state of a fresh checkout.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hbuilder import cli
from hbuilder.domain import board as board_module
from hbuilder.domain.board import BoardError, board_from_mapping, load_board
from hbuilder.domain.collection import CardLevelCaps
from hbuilder.domain.scoring import score_card_set, score_fast

from conftest import make_card, make_scoring_config, make_team

_REPO = Path(__file__).resolve().parents[1]
_BUILD_BOARD = _REPO / "tools" / "recon" / "proxy" / "build_board.py"
_DUMPS = _REPO / "tools" / "recon" / "proxy" / "dumps"


def _load_build_board():
    """`build_board.py` is a recon script under `tools/`, not an installed
    package, so it is imported by path — the same way the rest of `tools/`
    is exercised."""
    spec = importlib.util.spec_from_file_location("build_board", _BUILD_BOARD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_board"] = module
    spec.loader.exec_module(module)
    return module


_BUILD = _load_build_board()


# --------------------------------------------------------------------------
# A synthetic account, small enough to check by hand.
#
# One tree model. `S-001` sits at (0, 0); the Connect shape covers (0, 1) and
# (0, 2), which is where `R-001` and `G-001` sit. `R-002` at (5, 5) is outside
# it. So exactly two of the three unlocked nodes must come back boosted.
# --------------------------------------------------------------------------
_SKILLTREE = {
    "members": [
        {"chr": "chr-00001", "name": "Alpha", "model": "tree-model-001"},
        {"chr": "chr-00002", "name": "Beta", "model": "tree-model-001"},
    ],
    "models": {
        "tree-model-001": {
            "S-001": [0, 0],
            "R-001": [0, 1],
            "G-001": [0, 2],
            "R-002": [5, 5],
            "G-008": [9, 9],
            "Y-001": [8, 8],
        }
    },
    "extents": {"test_area-001": [[0, 1], [0, 2]]},
    "connect": {
        "connect-test": {
            "area": "test_area-001",
            "rare": "rare_5",
            "levels": [{"v": 1400}, {"v": 2000}],
        }
    },
    "card_connect": {"card-boost": "connect-test"},
}

_LEADER_EFFECT = "skill_tree_effect-all_parameter_up-50-skill_tree_effect_target-all-set_live_leader"
_ACCOUNT_EFFECT = "skill_tree_effect-all_parameter_up-5-skill_tree_effect_target-all-always"
_MEMBER_EFFECT = (
    "skill_tree_effect-sense_up-100-skill_tree_effect_target-skill_tree_character-always"
)
_GROUPING_ALPHA = (
    "skill_tree_effect-all_parameter_up_for_character_grouping-50"
    "-skill_tree_effect_target-character_grouping-grp-gen_0-always"
)
_GROUPING_BETA = (
    "skill_tree_effect-all_parameter_up_for_character_grouping-50"
    "-skill_tree_effect_target-character_grouping-grp-gen_9-always"
)
_Y_DEFAULT = (
    "skill_tree_effect-live_score_bonus_add_permil_up_by_music_skill_tree_character"
    "_and_music_singer_type-5-skill_tree_effect_passive_trigger"
    "-music_skill_tree_character_and_music_singer_type-solo-always-solo"
)
_Y_VARIANT = _Y_DEFAULT + "-fuwamoko"


def _row(group: str, number: str, effect: str, chrs: list[str] | None = None) -> dict:
    """One `SkillTreeNode` row in the master cache's real shape: numbered
    protobuf fields inside the field bag, the row id beside it. The bag's key
    comes from `build_board.ROW_DATA_KEY` rather than a literal, so this
    fixture and the converter cannot disagree about the input shape."""
    fields: dict = {"1": [group], "2": [int(number)], "6": [effect]}
    if chrs is not None:
        fields["3"] = chrs
    return {"group_id": group, "number": number, _BUILD.ROW_DATA_KEY: fields}


_NODE_MASTER = [
    _row("R-001", "1", _LEADER_EFFECT),
    _row("R-002", "1", _MEMBER_EFFECT),
    _row("G-001", "1", _ACCOUNT_EFFECT),
    # The 54-row shape, in miniature: one row per character, each targeting
    # that character's OWN grouping. Selecting the wrong row pays the wrong
    # generation, silently.
    _row("G-008", "1", _GROUPING_ALPHA, ["chr-00001"]),
    _row("G-008", "2", _GROUPING_BETA, ["chr-00002"]),
    # The 2-row `Y-*` shape: a default plus a character-specific variant.
    _row("Y-001", "1", _Y_DEFAULT),
    _row("Y-001", "2", _Y_VARIANT, ["chr-00002"]),
    # A socket carries no field 6 at all — an unlock, not an effect.
    {
        "group_id": "S-001",
        "number": "1",
        _BUILD.ROW_DATA_KEY: {"1": ["S-001"], "2": [1]},
    },
]

_STATE = {
    "chr-00001": {
        "nodes": ["R-001", "G-001", "R-002", "G-008", "Y-001", "S-001"],
        "slots": {"S-001": "card-boost"},
    },
    "chr-00002": {"nodes": ["G-008", "Y-001"], "slots": {}},
}


@pytest.fixture(scope="module")
def build_board():
    return _BUILD


@pytest.fixture(scope="module")
def converted(build_board):
    document, report, problems = build_board.build(
        _STATE, _SKILLTREE, _NODE_MASTER, level=1
    )
    return document, report, problems


# --------------------------------------------------------------------------
# 1. The converter's output is what `load_board` reads.
# --------------------------------------------------------------------------


def test_converter_output_parses_with_board_from_mapping(converted):
    """The contract that matters: the converter emits the schema the SOLVER
    reads, not a schema of its own. A converter whose output `load_board`
    rejects fails at solve time instead of build time."""
    document, _report, problems = converted
    assert problems == []
    model = board_from_mapping(document, source="<test>")
    assert set(model.characters) == {"Alpha", "Beta"}
    assert [node.node_id for node in model.characters["Alpha"].nodes] == [
        "R-001",
        "G-001",
        "R-002",
        "G-008",
        "Y-001",
    ]


def test_converter_keys_by_holomem_name_not_by_chr_id(converted):
    """`CharacterBoard.holomem` joins to `Card.member`. Emitting `chr-00001`
    would parse cleanly and then reach zero cards — a silently-zero board,
    which is the one failure mode indistinguishable from a correct zero."""
    document, _report, _problems = converted
    assert "chr-00001" not in document["characters"]
    assert "Alpha" in document["characters"]


def test_sockets_are_unlocks_not_effects(converted):
    """`S-001` is in the unlocked list and has no field 6 in `SkillTreeNode`.
    It must be omitted, not reported unresolved — otherwise every real
    account produces false alarms."""
    document, _report, problems = converted
    nodes = {row["node"] for row in document["characters"]["Alpha"]}
    assert "S-001" not in nodes
    assert problems == []


def test_connect_multiplier_is_geometry_not_a_guess(converted):
    """The shape covers (0,1) and (0,2) from the socket at (0,0), so exactly
    `R-001` and `G-001` are boosted, at `1 + 1400/1000 = 2.4` — the same
    number `test_board.py` transcribes for Ina's real 5-star Connect."""
    document, _report, _problems = converted
    by_node = {row["node"]: row["connect_multiplier"] for row in document["characters"]["Alpha"]}
    assert by_node["R-001"] == 2.4
    assert by_node["G-001"] == 2.4
    assert by_node["R-002"] == 1.0
    # Beta has no filled socket, so nothing of hers is boosted.
    beta = {row["node"]: row["connect_multiplier"] for row in document["characters"]["Beta"]}
    assert set(beta.values()) == {1.0}


def test_connect_level_two_is_selectable_and_is_not_the_default(build_board):
    """Which level a placement sits at is not known from the captured state —
    bloom is not recorded. Level 1 is the default because it is what
    reproduces the +28.73 pp acceptance number; level 2 is reachable so the
    sensitivity can be measured rather than argued about."""
    level_two, _report, problems = build_board.build(
        _STATE, _SKILLTREE, _NODE_MASTER, level=2
    )
    assert problems == []
    assert level_two["connect_level"] == 2
    by_node = {row["node"]: row["connect_multiplier"] for row in level_two["characters"]["Alpha"]}
    assert by_node["R-001"] == 3.0  # 1 + 2000/1000


# --------------------------------------------------------------------------
# 1b. `--connect-level auto` — deriving the level from the collection's bloom.
#
# The rule comes from master `CardPotential` (all 15 rows): at bloom 5 a
# 4★/5★ gets `effect_type 5` (Connect bonus -> level 2) while a 3★ gets
# `effect_type 4` (stats) instead. `collection.connect_bonus_level` implements
# it; these tests are about the converter honouring it AND about it labelling
# WHICH ACCOUNT the answer describes.
#
# A real-shaped card id is required, because the rarity is read out of it.
# --------------------------------------------------------------------------
_R5_CARD = "card-04001-5-uniq-0042-00"
_SKILLTREE_REAL_ID = {
    **_SKILLTREE,
    "card_connect": {**_SKILLTREE["card_connect"], _R5_CARD: "connect-test"},
}
_STATE_REAL_ID = {
    **_STATE,
    "chr-00001": {**_STATE["chr-00001"], "slots": {"S-001": _R5_CARD}},
}


# `"max"` can only mean a bloom if something says what the maximum IS, and that
# is `card_caps.json`. Without it the resolver leaves the bloom unknown — see
# `test_a_maxed_default_without_card_caps_cannot_invent_a_bloom`.
_CAPS = {
    "5": CardLevelCaps(
        level_by_limit_break=(40, 50, 60, 70, 80), max_bloom=5, bloom_mult_permil=()
    )
}


def _auto(collection, caps=_CAPS):
    return _BUILD.build(
        _STATE_REAL_ID, _SKILLTREE_REAL_ID, _NODE_MASTER, level=None,
        collection=collection, caps_by_rarity=caps,
    )


def test_auto_reads_level_two_from_a_bloom_the_owner_typed():
    """A named bloom 5 on a 5★ is account truth, so the document says
    `actual` — the derived level and the trustworthy-provenance label are one
    decision, not two."""
    document, _report, problems = _auto(
        {"card_default": "unknown", "cards": {_R5_CARD: {"bloom": 5}}}
    )
    assert problems == []
    assert document["connect_level"] == "auto"
    assert document["basis"] == "actual"
    assert document["connect_basis"] == {_BUILD.BASIS_MANUAL: 1}
    by_node = {r["node"]: r["connect_multiplier"] for r in document["characters"]["Alpha"]}
    assert by_node["R-001"] == 3.0  # 1 + 2000/1000, the Bloom-5 row


def test_auto_reads_level_one_from_a_bloom_below_five():
    """A real case, confirmed in game: both socket cards
    are below bloom 5, so the base level is not an assumption here — it is
    what the collection says."""
    document, _report, problems = _auto(
        {"card_default": "unknown", "cards": {_R5_CARD: {"bloom": 4}}}
    )
    assert problems == []
    assert document["basis"] == "actual"
    assert document["connect_basis"] == {_BUILD.BASIS_MANUAL: 1}
    by_node = {r["node"]: r["connect_multiplier"] for r in document["characters"]["Alpha"]}
    assert by_node["R-001"] == 2.4  # 1 + 1400/1000, the base row


def test_auto_marks_a_maxed_default_as_an_upper_bound_and_says_so():
    """⚠ The error this whole mechanism exists to stop.

    `card_default: "max"` holds for STATS (identical score on every card) and
    is wrong for Connect: it puts both of Ina's rare_5 socket cards at level 2,
    which overshoots the +28.73 pp acceptance number by +16.31 %, while in game
    both are below bloom 5.

    So a maxed default may still be *used* — a maxed account is a legitimate
    thing to ask about — but the document must not pass itself off as this
    account. `basis` flips to `upper-bound` and the reason is raised as a
    problem, loudly, rather than being a field nobody reads."""
    document, _report, problems = _auto({"card_default": "max", "cards": {}})

    assert document["basis"] == "upper-bound"
    assert document["connect_basis"] == {_BUILD.BASIS_MAXED: 1}
    assert any("UPPER BOUND" in p for p in problems), problems

    # It really did take level 2 — the label is not a substitute for the maths.
    by_node = {r["node"]: r["connect_multiplier"] for r in document["characters"]["Alpha"]}
    assert by_node["R-001"] == 3.0


def test_auto_falls_to_the_base_level_when_nothing_says_anything():
    """`None` is "not told us", never "level 1" — but a number has to be
    written, so the base is used and the basis records that it was assumed.
    That keeps the two halves distinguishable, which is the `None != 0` rule
    the collection model holds everywhere else."""
    document, _report, problems = _auto({"card_default": "unknown", "cards": {}})
    assert document["basis"] == "actual"
    assert document["connect_basis"] == {_BUILD.BASIS_ASSUMED: 1}
    assert not any("UPPER BOUND" in p for p in problems)
    by_node = {r["node"]: r["connect_multiplier"] for r in document["characters"]["Alpha"]}
    assert by_node["R-001"] == 2.4


def test_a_maxed_default_without_card_caps_cannot_invent_a_bloom():
    """`"max"` is a word, not a number: it only names a bloom once
    `card_caps.json` says what the maximum is. With no caps the resolver
    leaves the bloom unknown, so the level falls to the assumed base rather
    than to a guessed 5 — the failure is toward the shipped reading, not
    toward the upper bound."""
    document, _report, problems = _auto({"card_default": "max", "cards": {}}, caps=None)
    assert document["basis"] == "actual"
    assert document["connect_basis"] == {_BUILD.BASIS_ASSUMED: 1}
    assert not any("UPPER BOUND" in p for p in problems)
    by_node = {r["node"]: r["connect_multiplier"] for r in document["characters"]["Alpha"]}
    assert by_node["R-001"] == 2.4


def test_forcing_a_level_is_recorded_as_forced_and_stays_actual(build_board):
    """The default path. A forced level never consults the collection, so it
    can never be tainted by a maxed default — which is why it remains the
    shipped default while `auto` is opt-in."""
    document, _report, _problems = build_board.build(
        _STATE_REAL_ID, _SKILLTREE_REAL_ID, _NODE_MASTER, level=1,
        collection={"card_default": "max", "cards": {}},
    )
    assert document["basis"] == "actual"
    assert document["connect_basis"] == {_BUILD.BASIS_FORCED: 1}
    by_node = {r["node"]: r["connect_multiplier"] for r in document["characters"]["Alpha"]}
    assert by_node["R-001"] == 2.4


def test_multi_row_group_resolves_to_the_row_for_that_character(converted):
    """`G-008`/`G-011`/`G-021` carry one row per character and each row
    targets that character's OWN grouping. Picking the first row rather than
    the matching one would pay the wrong generation on 53 of 54 characters,
    and would still parse."""
    document, _report, _problems = converted
    alpha = {row["node"]: row["effects"] for row in document["characters"]["Alpha"]}
    beta = {row["node"]: row["effects"] for row in document["characters"]["Beta"]}
    assert alpha["G-008"] == [_GROUPING_ALPHA]
    assert beta["G-008"] == [_GROUPING_BETA]


def test_y_group_falls_back_to_the_default_row_when_no_variant_names_the_character(
    converted,
):
    """The 12 two-row `Y-*` groups are a `-solo` / `-solo-fuwamoko` variant
    pair, not a level pair: the variant names its characters in field 3 and
    everyone else takes the default row."""
    document, _report, _problems = converted
    alpha = {row["node"]: row["effects"] for row in document["characters"]["Alpha"]}
    beta = {row["node"]: row["effects"] for row in document["characters"]["Beta"]}
    assert alpha["Y-001"] == [_Y_DEFAULT]
    assert beta["Y-001"] == [_Y_VARIANT]


# --------------------------------------------------------------------------
# 2. An unresolved node is LOUD, never silently dropped.
# --------------------------------------------------------------------------


def test_an_unknown_node_is_reported_not_dropped(build_board):
    state = {"chr-00001": {"nodes": ["R-001", "Z-999"], "slots": {}}}
    document, _report, problems = build_board.build(
        state, _SKILLTREE, _NODE_MASTER, level=1
    )
    assert any("Z-999" in problem for problem in problems)
    assert [row["node"] for row in document["characters"]["Alpha"]] == ["R-001"]


def test_a_placed_card_with_no_connect_effect_is_reported(build_board):
    state = {"chr-00001": {"nodes": ["R-001"], "slots": {"S-001": "card-unknown"}}}
    _document, _report, problems = build_board.build(
        state, _SKILLTREE, _NODE_MASTER, level=1
    )
    assert any("card-unknown" in problem and "UNRESOLVED" in problem for problem in problems)


def test_a_character_missing_from_the_member_table_is_reported(build_board):
    state = {"chr-99999": {"nodes": ["R-001"], "slots": {}}}
    document, _report, problems = build_board.build(
        state, _SKILLTREE, _NODE_MASTER, level=1
    )
    assert document["characters"] == {}
    assert any("chr-99999" in problem for problem in problems)


def test_a_group_whose_rows_all_name_other_characters_is_reported(build_board):
    """No default row and no row for this character: the honest answer is
    "unresolved", not "take row 1"."""
    node_master = [_row("G-008", "1", _GROUPING_ALPHA, ["chr-00001"])]
    skilltree = dict(_SKILLTREE)
    state = {"chr-00002": {"nodes": ["G-008"], "slots": {}}}
    document, _report, problems = build_board.build(
        state, skilltree, node_master, level=1
    )
    assert document["characters"]["Beta"] == []
    assert any("chr-00002" in problem for problem in problems)


# --------------------------------------------------------------------------
# 3. Role-conditionality.
# --------------------------------------------------------------------------


def _stat_totals(contribution, members):
    return {
        card.id: sum(contribution.flat_by_card.get(card.id, {}).values())
        for card in members
    }


def test_a_leader_scope_effect_pays_only_when_that_holomem_leads(converted):
    """The red branch is `only while this member is the unit Leader`, and
    then it reaches the WHOLE team. So the same board must be worth a large
    number in one arrangement and exactly zero in another that differs only
    in who holds the leader slot.

    This is the property the whole landing exists to deliver: without it a
    board is a per-holomem constant that cancels out of a ranking, and the
    ranking would not move."""
    document, _report, _problems = converted
    model = board_from_mapping(document, source="<test>")

    alpha = make_card("card-alpha", member="Alpha")
    beta = make_card("card-beta", member="Beta")
    others = [make_card(f"card-{i}") for i in range(4)]
    members = (beta, *others)

    with_alpha_leading = board_module.resolve(model, alpha, members)
    with_other_leading = board_module.resolve(model, others[0], members)

    # R-001 is `all_parameter_up 50` at x2.4 = 120 flat to each of the three
    # stats, on every one of the five members — but only under Alpha.
    leading = _stat_totals(with_alpha_leading, members)
    not_leading = _stat_totals(with_other_leading, members)
    assert all(leading[card.id] - not_leading[card.id] == pytest.approx(360.0) for card in members)
    assert with_alpha_leading.power_bonus(members) > with_other_leading.power_bonus(members)


def test_a_leader_scope_effect_pays_nothing_when_its_holomem_is_only_a_member(converted):
    """Alpha in the five members but NOT in the leader slot is worth zero red.
    The scope is the leader SLOT, not membership."""
    document, _report, _problems = converted
    model = board_from_mapping(document, source="<test>")
    alpha = make_card("card-alpha", member="Alpha")
    others = [make_card(f"card-{i}") for i in range(4)]
    members = (alpha, *others)
    contribution = board_module.resolve(model, others[0], members)
    # Only R-002 (member scope, Alpha's own card) and G-001 (account scope)
    # can pay here; R-001's 360 must not appear on anyone.
    assert contribution.flat_by_card[others[1].id] == {
        "performance": pytest.approx(5.0 * 2.4),
        "technique": pytest.approx(5.0 * 2.4),
        "sense": pytest.approx(5.0 * 2.4),
    }


def test_the_whole_team_score_moves_when_the_leader_changes(converted):
    """The same property, at the level a user sees: the team SCORE, through
    `score_fast`, with the board reached the way the CLI reaches it."""
    document, _report, _problems = converted
    model = board_from_mapping(document, source="<test>")
    cfg = make_scoring_config()
    cfg_with_board = dataclasses.replace(cfg, board=model)

    alpha = make_card("card-alpha", member="Alpha")
    beta = make_card("card-beta", member="Beta")
    others = [make_card(f"card-{i}") for i in range(4)]
    alpha_leads = make_team([alpha, beta, *others])
    beta_leads = make_team([beta, alpha, *others])

    # Without the board the two arrangements score alike (the cards are
    # stat-identical); with it, Alpha's red branch separates them.
    assert score_fast(alpha_leads, cfg) == pytest.approx(score_fast(beta_leads, cfg))
    assert score_fast(alpha_leads, cfg_with_board) > score_fast(
        beta_leads, cfg_with_board
    )


# --------------------------------------------------------------------------
# 3b. The hoist that makes the board affordable, and the invariance it rests on.
#
# `score_card_set` skips `board.resolve` for every leader whose own holomem
# owns no red node. That is only sound if the skipped answer is BIT-identical
# to the one it reuses -- not close, identical -- so these pin the claim
# directly, with a negative control so they cannot pass on an empty board.
# --------------------------------------------------------------------------


def test_leader_scoped_holomem_names_exactly_the_characters_with_a_red_node(converted):
    """Alpha holds `R-001` (`-target-all-set_live_leader`); Beta holds only
    `G-008`/`Y-001`. Under-inclusion here is a wrong score, so this pins the
    set itself rather than only its effect."""
    document, _report, _problems = converted
    model = board_from_mapping(document, source="<test>")
    assert model.leader_scoped_holomem == frozenset({"Alpha"})


def test_a_leader_without_a_red_node_resolves_bit_identically_to_no_leader(converted):
    """The invariance `score_card_set`'s hoist rests on, checked field for
    field with `==` (never `approx`): the skipped path must reproduce the
    reused one exactly, because a float that is merely close would make the
    solver's answer depend on which leader happened to warm the cache."""
    document, _report, _problems = converted
    model = board_from_mapping(document, source="<test>")

    beta = make_card("card-beta", member="Beta")
    others = [make_card(f"card-{i}") for i in range(4)]
    members = (beta, *others)

    baseline = board_module.resolve(model, None, members)
    for leader in (beta, *others):
        assert leader.member not in model.leader_scoped_holomem
        assert board_module.resolve(model, leader, members) == baseline


def test_a_leader_WITH_a_red_node_does_not_resolve_like_no_leader(converted):
    """The negative control. Without it the test above passes on any board
    whose red branch is empty -- including one this module failed to parse."""
    document, _report, _problems = converted
    model = board_from_mapping(document, source="<test>")

    alpha = make_card("card-alpha", member="Alpha")
    others = [make_card(f"card-{i}") for i in range(4)]
    members = (alpha, *others)

    assert alpha.member in model.leader_scoped_holomem
    assert board_module.resolve(model, alpha, members) != board_module.resolve(
        model, None, members
    )


def test_the_memoised_invariant_terms_reproduce_the_full_walk_exactly(converted):
    """`leader_invariant_*` replaces a walk over every effect on every board
    with a per-card memo. It must return the SAME float, not a close one, for
    every member set — so this checks it against `resolve` itself over a set of
    teams chosen to exercise each scope: Alpha's own `member` node, Beta's
    `grouping` row, and the `account` node that reaches everyone."""
    document, _report, _problems = converted
    model = board_from_mapping(document, source="<test>")

    alpha = make_card("card-alpha", member="Alpha", generations=frozenset({"grp-gen_0"}))
    beta = make_card("card-beta", member="Beta", generations=frozenset({"grp-gen_9"}))
    others = [make_card(f"card-{i}") for i in range(4)]

    for members in (
        (alpha, *others),
        (beta, *others),
        (alpha, beta, *others[:3]),
        tuple(others) + (alpha,),
    ):
        # Every leader the set admits, plus the no-leader case -- so the red
        # branch (Alpha) is cross-checked as well as the invariant one.
        for leader in (None, alpha, beta, *others):
            holomem = None if leader is None else leader.member
            expected = board_module.resolve(model, leader, members)
            assert model.power_bonus_for(members, holomem) == expected.power_bonus(
                members
            )
            assert model.score_bonus_rate() == expected.score_bonus_rate()

    # The memo must be a cache, not a mutation: asking twice gives the same
    # answer, and a card seen in one set must not leak into another.
    assert model.power_bonus_for((alpha, *others), None) == board_module.resolve(
        model, None, (alpha, *others)
    ).power_bonus((alpha, *others))


def test_the_invariant_memo_does_not_confuse_two_cards_sharing_an_id(converted):
    """The memo is keyed by `(id, member, generations)` — the only card
    attributes the walk reads. Keyed by id alone, the second card here would be
    served the first one's buckets, and the wrong number would be plausible."""
    document, _report, _problems = converted
    model = board_from_mapping(document, source="<test>")
    others = [make_card(f"card-{i}") for i in range(4)]

    as_alpha = make_card("card-x", member="Alpha", generations=frozenset({"grp-gen_0"}))
    as_beta = make_card("card-x", member="Beta", generations=frozenset({"grp-gen_9"}))

    first = model.power_bonus_for((as_alpha, *others), None)
    second = model.power_bonus_for((as_beta, *others), None)
    assert first == board_module.resolve(model, None, (as_alpha, *others)).power_bonus(
        (as_alpha, *others)
    )
    assert second == board_module.resolve(model, None, (as_beta, *others)).power_bonus(
        (as_beta, *others)
    )
    # Negative control: Alpha's own `member`-scope node makes these differ, so
    # an id-keyed memo would have been caught by the assertions above.
    assert first != second


def test_score_card_set_matches_score_fast_for_every_leader_with_a_board(converted):
    """Fast/attribution parity, with a board configured -- the gap the hoist
    could drive through. `test_batch_api_parity.py` covers every other scoring
    stage this way but never sets `cfg.board`, so without this the per-leader
    board term is the one branch of `score_card_set` no parity test reaches.

    Both leaders matter: Alpha exercises the per-holomem `resolve`, the other
    five exercise the reused invariant answer."""
    document, _report, _problems = converted
    model = board_from_mapping(document, source="<test>")
    cfg = dataclasses.replace(make_scoring_config(), board=model)

    alpha = make_card("card-alpha", member="Alpha")
    beta = make_card("card-beta", member="Beta")
    others = [make_card(f"card-{i}") for i in range(4)]
    members = (beta, *others)
    pool = (alpha, beta, *others)

    leader_scores = score_card_set(members, pool, cfg)
    assert len(leader_scores) == len(pool)
    for leader, scored in zip(pool, leader_scores):
        expected = score_fast(make_team([leader, *members]), cfg)
        assert scored.total_score == expected, (
            f"score_card_set and score_fast disagree for leader {leader.id!r} "
            f"with a board configured: {scored.total_score!r} != {expected!r}"
        )

    # And the hoist must not flatten the ranking it exists to preserve:
    # Alpha leading is still worth strictly more than anyone else leading.
    by_id = {s.leader_id: s.total_score for s in leader_scores}
    assert by_id[alpha.id] > max(v for k, v in by_id.items() if k != alpha.id)


# --------------------------------------------------------------------------
# 3c. The board is part of the scoring model, so it is part of `config_hash`.
#
# A throughput measured without a board does not describe a run with one, and
# with the board outside the hash bench/throughput.json could not tell a board
# run from a boardless one: `project_duration` would report the stale rate with
# full confidence instead of warning.
# --------------------------------------------------------------------------


def test_config_hash_separates_a_board_run_from_a_boardless_one(converted):
    """Two runs whose ONLY difference is `--board` must
    hash differently, or the staleness warning cannot fire between them."""
    document, _report, _problems = converted
    model = board_from_mapping(document, source="<test>")
    cfg = make_scoring_config()

    boardless = cli.config_hash(cli._scoring_config_document(cfg))
    boarded = cli.config_hash(
        cli._scoring_config_document(dataclasses.replace(cfg, board=model))
    )
    assert boardless != boarded


def test_two_different_boards_hash_differently(converted, build_board):
    """Not just present-vs-absent: a board that has GROWN — the player buying a
    node — is a different scoring model, and the rates measured against the
    old one no longer describe it."""
    document, _report, _problems = converted
    cfg = make_scoring_config()

    smaller_state = {
        "chr-00001": {"nodes": ["G-001"], "slots": {}},
        "chr-00002": {"nodes": ["G-008"], "slots": {}},
    }
    smaller_doc, _r, problems = build_board.build(
        smaller_state, _SKILLTREE, _NODE_MASTER, level=1
    )
    assert problems == []

    def hashed(doc):
        model = board_from_mapping(doc, source="<test>")
        return cli.config_hash(
            cli._scoring_config_document(dataclasses.replace(cfg, board=model))
        )

    assert hashed(document) != hashed(smaller_doc)


def test_the_fingerprint_is_stable_across_two_loads_of_the_same_board(converted):
    """It must be a content digest, not an object identity — otherwise every
    run would warn that its own benchmark is stale, and the warning would be
    ignored within a day."""
    document, _report, _problems = converted
    first = board_from_mapping(document, source="<test>")
    second = board_from_mapping(json.loads(json.dumps(document)), source="<test>")

    assert first.fingerprint() == second.fingerprint()


# --------------------------------------------------------------------------
# 4. An absent board file is a first-class state and changes nothing.
# --------------------------------------------------------------------------


def test_absent_board_file_leaves_the_score_bit_identical(tmp_path):
    """The shipped default. `dumps/board.json` does not exist in a fresh
    checkout, and a run there must produce the same number as a run without
    any board support — not approximately, bit for bit."""
    missing = tmp_path / "no-such-board.json"
    assert load_board(missing) is None

    basis = cli.load_board_basis(str(missing))
    assert basis.model is None
    cfg = make_scoring_config()
    assert cfg.board is None

    cards = [make_card(f"card-{i}") for i in range(6)]
    team = make_team(cards)
    before = score_fast(team, cfg)
    # Exactly what run_solve does: only replace when a model was loaded.
    cfg_after = cfg if basis.model is None else dataclasses.replace(cfg, board=basis.model)
    assert cfg_after is cfg
    assert score_fast(team, cfg_after) == before


def test_board_basis_says_which_of_the_two_states_the_run_was_in(tmp_path, converted):
    """A solve that silently changed by 30 % between two invocations is the
    worst outcome available here, so both states are announced and both are
    machine-readable."""
    document, _report, _problems = converted
    path = tmp_path / "board.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    loaded = cli.load_board_basis(str(path))
    assert loaded.payload() == {
        "loaded": True,
        "path": str(path),
        "characters": 2,
        "nodes": 7,
    }
    assert "loaded" in loaded.lines()[0]
    assert "SCORED" in loaded.lines()[0]

    absent = cli.load_board_basis(str(tmp_path / "nope.json"))
    assert absent.payload()["loaded"] is False
    assert "ZERO" in absent.lines()[0]

    disabled = cli.load_board_basis(None)
    assert disabled.payload() == {
        "loaded": False,
        "path": None,
        "characters": 0,
        "nodes": 0,
    }
    assert "--no-board" in disabled.lines()[0]


def test_a_board_file_that_exists_but_does_not_parse_is_an_error(tmp_path):
    """Absent is fine. Present-and-wrong is not: a board that scores as zero
    because it failed to parse is indistinguishable from one that is
    correctly zero, which is exactly what `BoardError` exists to prevent."""
    path = tmp_path / "board.json"
    path.write_text(json.dumps({"characters": {"Alpha": [{"node": "R-1", "effects": ["nope"]}]}}), encoding="utf-8")
    with pytest.raises(BoardError):
        cli.load_board_basis(str(path))


# --------------------------------------------------------------------------
# 4b. The one cap that reaches a ranking.
# --------------------------------------------------------------------------


def test_song_all_bucket_is_clamped_to_the_attested_100_permil():
    """The type-19 `all`-scope score bonus is capped at 100 permil (client
    constant `0x75cec38` = `0000c842`, displayed 10.0 %). It is the ONLY capped
    quantity `score_bonus_rate()` can return, so it is the only one clamped
    here. It binds only once a board's bonus sum passes the cap."""
    contribution = board_module.BoardContribution(
        flat_by_card={},
        rate_by_card={},
        skill_effect_up=0.0,
        conditional_skill_effect_up=0.0,
        activation_rate_up={},
        cooldown_shorten={},
        song_score_bonus={"all": 0.378, "solo": 0.9, "group": 0.9},
    )
    assert board_module.SONG_ALL_CAP_PERMIL == 100
    assert contribution.score_bonus_rate() == pytest.approx(0.1)


def test_the_clamp_is_inert_below_the_cap():
    """A typical captured board sums to well under 100 permil, so the
    clamp must not move any number below the cap."""
    contribution = board_module.BoardContribution(
        flat_by_card={},
        rate_by_card={},
        skill_effect_up=0.0,
        conditional_skill_effect_up=0.0,
        activation_rate_up={},
        cooldown_shorten={},
        song_score_bonus={"all": 0.0048},
    )
    assert contribution.score_bonus_rate() == pytest.approx(0.0048)


def test_solo_and_group_are_neither_clamped_nor_read():
    """Do NOT clamp `solo`/`group` and do not pool them across characters.
    Pooling is what `resolve()` does — pinned here as a known, reported
    limitation rather than left to be
    rediscovered, because doing it right needs a per-character
    `song_score_bonus`, which is a restructure and not a clamp."""
    contribution = board_module.BoardContribution(
        flat_by_card={},
        rate_by_card={},
        skill_effect_up=0.0,
        conditional_skill_effect_up=0.0,
        activation_rate_up={},
        cooldown_shorten={},
        song_score_bonus={"all": 0.0, "solo": 0.9, "group": 0.9},
    )
    assert contribution.score_bonus_rate() == 0.0
    assert contribution.song_score_bonus["solo"] == 0.9


def test_solve_parser_defaults_to_the_conventional_board_path():
    args = cli.build_parser().parse_args(["solve"])
    assert args.board == cli.DEFAULT_BOARD_PATH
    assert args.no_board is False
    assert cli.DEFAULT_BOARD_PATH.startswith("tools/recon/proxy/dumps/")


# --------------------------------------------------------------------------
# 5. Against the real captured state, when it is present.
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not (_DUMPS / "board_state.json").exists()
    or not (_DUMPS / "skilltree.json").exists()
    or not (_DUMPS / "master_SkillTreeNode.json").exists(),
    reason="captured account state is gitignored and absent in a fresh checkout",
)
def test_the_real_capture_converts_with_nothing_unresolved(build_board):
    """Verify the artefact, not the counter. Reads the real capture
    and asserts the converted board (a) leaves nothing unresolved and (b)
    reproduces, node for node and multiplier for multiplier, the Ina fixture
    `test_board.py` transcribed BY HAND from the game — an independent
    derivation landing on the same 27 rows."""
    state = json.loads((_DUMPS / "board_state.json").read_text(encoding="utf-8"))
    skilltree = json.loads((_DUMPS / "skilltree.json").read_text(encoding="utf-8"))
    node_master = json.loads(
        (_DUMPS / "master_SkillTreeNode.json").read_text(encoding="utf-8")
    )
    document, _report, problems = build_board.build(
        state, skilltree, node_master, level=1
    )
    assert problems == []
    board_from_mapping(document, source="<real capture>")

    # The hand-transcription cross-check runs on the FROZEN capture the human
    # actually transcribed from (`fixtures/…pre_respec.json`) — a live account
    # can respec Ina's whole red branch away, leaving the LIVE capture with no
    # transcribed row and the live comparison vacuous. The property (independent human and
    # machine derivations agreeing node for node) is about the conversion, not
    # about the account's current build, so freezing its input preserves it.
    state = json.loads(
        (Path(__file__).parent / "fixtures"
         / "board_state_2026-08-09_pre_respec.json").read_text(encoding="utf-8")
    )
    document, _report, problems = build_board.build(
        state, skilltree, node_master, level=1
    )
    assert problems == []

    from test_board import INA_RED_NODES

    expected = {node: (effect, mult) for node, effect, mult in INA_RED_NODES}
    actual = {
        row["node"]: (row["effects"][0], row["connect_multiplier"])
        for row in document["characters"]["Ninomae Ina'nis"]
    }
    # SUBSET, not equality. The property this test exists for is that the automated conversion
    # and an independent hand transcription agree node for node and multiplier for multiplier.
    # Equality would additionally demand that the account never progress: the fixture is a
    # snapshot of Ina's board on the day it was typed, and unlocking a later node (R-037/R-038)
    # would fail it while all 27 transcribed rows still match exactly. A node the human never saw
    # cannot be checked against a human reading, so the honest assertion is over the rows they
    # did transcribe.
    #
    # ⚠ And the account can move DOWNWARD too: a node can be RESET on purpose (e.g. `R-032`).
    # A node the account no longer owns is
    # not a node "the conversion lost" — it is not in the conversion's INPUT. So the comparison is
    # taken over the rows the hand transcription and the live `board_state.json` still share, and
    # the strong property is kept intact against everything the account does have: nothing
    # unlocked may go missing, and every shared row must agree exactly.
    unlocked = set(state.get("chr-04003", {}).get("nodes", ()))
    assert unlocked, "board_state.json carries no Ina node list -- the guard below would be vacuous"
    checkable = {node: pair for node, pair in expected.items() if node in unlocked}
    assert checkable, "no transcribed node is still unlocked -- the fixture has gone fully stale"
    missing = sorted(set(checkable) - set(actual))
    assert not missing, f"the conversion lost nodes the account still has unlocked: {missing}"
    for node, pair in checkable.items():
        assert actual[node] == pair, (
            f"{node}: converted {actual[node]} but the hand transcription says {pair}"
        )
