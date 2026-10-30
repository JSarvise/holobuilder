"""The score-model -> board-plan bridge (`domain/board_plan.py`).

Pins the two things the valuation must get right: node costs read straight from
master field 9, and each branch reaching the correct cards (leader -> all
members while leading, member -> its own card, grouping -> skipped for now),
with multi-row nodes taken as the max variant, not summed."""

from __future__ import annotations

from hbuilder.domain.card import Stats
from hbuilder.domain.board_plan import node_costs_from_master, power_node_values

import pytest
from conftest import make_card

K = 2.03734
_STATS = Stats(performance=1000, technique=1000, sense=1000)

_LEADER = ("skill_tree_effect-all_parameter_up-100-skill_tree_effect_target"
           "-all-set_live_leader")
_MEMBER = ("skill_tree_effect-sense_up-50-skill_tree_effect_target"
           "-skill_tree_character-always")
_GROUPING = ("skill_tree_effect-all_parameter_up_for_character_grouping-225"
             "-skill_tree_effect_target-character_grouping-grp-gen_0-always")


def test_node_costs_read_field_9_and_ignore_the_rest():
    rows = [
        {"group_id": "S-001", "data": {"9": [0]}},
        {"group_id": "G-015", "data": {"9": [3], "6": ["whatever"]}},
        {"group_id": "R-001", "data": {"9": [5]}},
        {"group_id": "no-cost", "data": {"2": [1]}},  # no field 9 -> skipped
    ]
    costs = node_costs_from_master(rows)
    assert costs == {"S-001": 0, "G-015": 3, "R-001": 5}


def _values(node_effect_ids, *, is_leader, own, members):
    return power_node_values(
        node_effect_ids,
        is_leader=is_leader, own_card=own, member_cards=members,
        bonus_permil=0.0, overall_power=0.0, k_constant=K,
    )


def test_leader_node_reaches_all_members_only_while_leading():
    members = tuple(make_card(f"card-m{i}-5-uniq-0000-00", member=f"M{i}", stats=_STATS)
                    for i in range(5))
    v_leading = _values({"R-001": [_LEADER]}, is_leader=True, own=None, members=members)
    v_bench = _values({"R-001": [_LEADER]}, is_leader=False, own=None, members=members)
    # flat +100 to all 3 stats of all 5 members, times dUS/dPower (= K at bonus 0).
    assert v_leading["R-001"] == pytest.approx(100 * 3 * 5 * K)
    assert v_bench["R-001"] == 0.0  # not leading -> its red board does nothing


def test_member_node_reaches_only_its_own_card():
    own = make_card("card-self-5-uniq-0000-00", member="Self", stats=_STATS)
    members = (own,)
    v = _values({"B-001": [_MEMBER]}, is_leader=False, own=own, members=members)
    assert v["B-001"] == pytest.approx(50 * 1 * K)  # sense only, one card


def test_grouping_node_is_skipped_and_variants_take_the_max():
    members = (make_card("card-x-5-uniq-0000-00", member="X", stats=_STATS),)
    # grouping alone -> 0 (needs team grouping). A leader node with two variant
    # rows takes the larger, never the sum.
    big = _LEADER.replace("-100-", "-300-")
    v = _values(
        {"G-001": [_GROUPING], "R-001": [_LEADER, big]},
        is_leader=True, own=None, members=members,
    )
    assert v["G-001"] == 0.0
    assert v["R-001"] == pytest.approx(300 * 3 * 1 * K)  # max(100, 300) variant, one member


def test_optimize_team_boards_sums_distinct_characters_by_role():
    from hbuilder.domain.skilltree import SkillTree, TreeNode, ROOT_NODE
    from hbuilder.domain.board_plan import optimize_team_boards

    # ROOT - R-001 (leader stat, reaches all members) ; ROOT - B-001 (member stat)
    nodes = {
        ROOT_NODE: TreeNode(node_id=ROOT_NODE, branch="S", cost=0, player_level=None),
        "R-001": TreeNode(node_id="R-001", branch="R", cost=1, player_level=None),
        "B-001": TreeNode(node_id="B-001", branch="B", cost=1, player_level=None),
    }
    adjacency = {
        ROOT_NODE: frozenset({"R-001", "B-001"}),
        "R-001": frozenset({ROOT_NODE}),
        "B-001": frozenset({ROOT_NODE}),
    }
    tree = SkillTree(nodes=nodes, positions={}, adjacency=adjacency)
    effects = {"R-001": [_LEADER], "B-001": [_MEMBER]}

    leader = make_card("card-lead-5-uniq-0000-00", member="Lead", stats=_STATS)
    members = tuple(make_card(f"card-m{i}-5-uniq-0000-00", member=f"M{i}", stats=_STATS)
                    for i in range(5))
    plan = optimize_team_boards(
        leader, members, tree_for=lambda c: tree, budget_for=lambda c: 10,
        node_effect_ids=effects, overall_power=0.0, bonus_permil=0.0, k_constant=K,
    )
    # 6 distinct characters each get a plan; the total is positive.
    assert len(plan.per_character) == 6
    assert plan.total_gain > 0.0
    # the leader bought its red node (reaches all members -> high value);
    assert "R-001" in plan.per_character[leader.id].selected
    # a member bought its own blue node, not the (useless-to-it) red one.
    m0 = members[0]
    assert "B-001" in plan.per_character[m0.id].selected
    assert "R-001" not in plan.per_character[m0.id].selected


def test_leader_whose_holomem_is_also_a_member_gets_ONE_board_valued_as_leader():
    """The game has one board per CHARACTER. A leader card whose holomem also
    fields a different card as a member (legal: the leader is not a member) must
    get ONE plan, valued as the leader (red nodes reach the team because she
    leads) AND reaching her member card's blue nodes — not two plans where the
    member's overwrites the leader's name-keyed board and silently drops every
    red (no R-044/062/063, singer power 0)."""
    from hbuilder.domain.skilltree import SkillTree, TreeNode, ROOT_NODE
    from hbuilder.domain.board_plan import optimize_team_boards, board_model_from_plan

    nodes = {
        ROOT_NODE: TreeNode(node_id=ROOT_NODE, branch="S", cost=0, player_level=None),
        "R-001": TreeNode(node_id="R-001", branch="R", cost=1, player_level=None),
        "B-001": TreeNode(node_id="B-001", branch="B", cost=1, player_level=None),
    }
    adjacency = {
        ROOT_NODE: frozenset({"R-001", "B-001"}),
        "R-001": frozenset({ROOT_NODE}),
        "B-001": frozenset({ROOT_NODE}),
    }
    tree = SkillTree(nodes=nodes, positions={}, adjacency=adjacency)
    effects = {"R-001": [_LEADER], "B-001": [_MEMBER]}

    leader = make_card("card-04001-5-uniq-0042-00", member="Mori Calliope", stats=_STATS)
    calli_member = make_card("card-04001-4-cmmn-0000-00", member="Mori Calliope", stats=_STATS)
    others = tuple(make_card(f"card-m{i}-5-uniq-0000-00", member=f"M{i}", stats=_STATS)
                   for i in range(4))
    members = (calli_member, *others)
    plan = optimize_team_boards(
        leader, members, tree_for=lambda c: tree, budget_for=lambda c: 10,
        node_effect_ids=effects, overall_power=0.0, bonus_permil=0.0, k_constant=K,
    )
    # 5 distinct CHARACTERS, not 6 cards.
    assert len(plan.per_character) == 5
    calli = plan.per_character[leader.id]
    assert calli_member.id not in plan.per_character
    # valued as leader (red bought) AND as her member card (blue bought).
    assert "R-001" in calli.selected
    assert "B-001" in calli.selected
    # ...and the assembled name-keyed board carries THAT plan, not an overwrite.
    board = board_model_from_plan(
        plan, leader, members, {"nodes": {}, "members": [], "models": {}}, effects,
        overall_power=0.0, bonus_permil=0.0, k_constant=K, active_pool_rate=0.0)
    got = {n.node_id for n in board.characters["Mori Calliope"].nodes}
    assert got == {"R-001", "B-001"}


def test_point_budget_for_level_matches_the_grant_milestones():
    from hbuilder.domain.board_plan import point_budget_for_level
    # the game's grant milestones: exact at the knots.
    assert point_budget_for_level(10) == 26
    assert point_budget_for_level(30) == 161
    assert point_budget_for_level(50) == 361
    # interpolated between, and capped at the full 453-point tree above.
    assert 161 < point_budget_for_level(35) < 261
    assert point_budget_for_level(80) == 453
    assert point_budget_for_level(500) == 453  # never exceeds the tree


def test_board_points_from_snapshot_reads_the_point_list():
    from hbuilder.domain.board_plan import board_points_from_snapshot
    snap = {"board_points": [
        {"point_id": "skill_tree_point-chr-00001", "quantity": 26},
        {"point_id": "skill_tree_point-chr-00004", "quantity": 29},
        {"point_id": "not-a-point", "quantity": 5},
        {"point_id": "skill_tree_point-chr-00005"},  # no quantity -> 0 (proto3 elides zero)
    ]}
    assert board_points_from_snapshot(snap) == {"chr-00001": 26, "chr-00004": 29,
                                                "chr-00005": 0}


def test_board_points_from_snapshot_reads_an_elided_quantity_as_zero():
    """proto3 elides a zero field: a character whose points are ALL spent comes
    back as `{'point_id': 'skill_tree_point-chr-04001'}` with no `quantity`.
    Read that as 0 unspent, not as "unknown" — skipping it would make the
    develop budget her 0 points and the estimate propose selling her whole
    board."""
    from hbuilder.domain.board_plan import board_points_from_snapshot
    snap = {"board_points": [
        {"point_id": "skill_tree_point-chr-04001"},
        {"point_id": "skill_tree_point-chr-00001", "quantity": 26},
        {"point_id": "not-a-point"},
    ]}
    assert board_points_from_snapshot(snap) == {"chr-04001": 0, "chr-00001": 26}


_EFF_UP = ("skill_tree_effect-live_active_skill_effect_up_permil_up-40"
           "-skill_tree_effect_target-all-set_live_leader")
_ACT_UP = ("skill_tree_effect-live_active_skill_activation_probability_up_permil_up"
           "-60-skill_tree_effect_target-skill_tree_character-always")


def test_active_pool_channels_are_valued_off_the_pool():
    members = tuple(make_card(f"card-m{i}-5-uniq-0000-00", member=f"M{i}", stats=_STATS)
                    for i in range(5))
    own = members[0]
    v = power_node_values(
        {"R-002": [_EFF_UP], "B-007": [_ACT_UP]},
        is_leader=True, own_card=own, member_cards=members,
        bonus_permil=0.0, overall_power=1_000_000.0, k_constant=K,
        active_pool_rate=0.5,
    )
    # skill_effect_up multiplies the WHOLE pool: pool * V * power * K.
    assert v["R-002"] == pytest.approx(0.5 * 0.04 * 1_000_000 * K)
    # activation_rate raises one member's share: (pool / 5) * V * power * K.
    assert v["B-007"] == pytest.approx((0.5 / 5) * 0.06 * 1_000_000 * K)


# --- the Y (song-score-bonus) channel's UNIT -----------------------------

# Real node strings from `dumps/board.json`: the pooled `all` bucket at 1 permil
# and a `solo`-scoped tile at 20.
_SONG_ALL = ("skill_tree_effect-live_score_bonus_add_permil_up_by_music_"
             "skill_tree_character_and_music_singer_type-1-"
             "skill_tree_effect_passive_trigger-music_skill_tree_character_"
             "and_music_singer_type-all-always-all")
_SONG_SOLO = ("skill_tree_effect-live_score_bonus_add_permil_up_by_music_"
              "skill_tree_character_and_music_singer_type-20-"
              "skill_tree_effect_passive_trigger-music_skill_tree_character_"
              "and_music_singer_type-solo-always-solo")


def test_song_score_bonus_is_valued_per_UNIT_of_rate_not_per_permil():
    """`BoardEffect.value` is documented as a FRACTION for every non-stat
    kind — "so no caller divides by 1000 twice" (`board.py`). A whole unit of
    score-bonus rate is worth `Power * K`, so a 20-permil tile (value 0.020) is
    worth `0.020 * Power * K`, exactly as its two siblings on the same rate axis
    are valued (`skill_effect_up`, `activation_rate_up`).

    Scaling it by `Power * K / 1000` — the per-PERMIL constant — would divide
    the fraction by 1000 a second time and make yellow nodes worth ~1/1000 of
    their real value (about half a percent of team score on a real roster)."""
    members = tuple(make_card(f"card-m{i}-5-uniq-0000-00", member=f"M{i}", stats=_STATS)
                    for i in range(5))
    v = power_node_values(
        {"Y-001": [_SONG_ALL], "Y-002": [_SONG_SOLO]},
        is_leader=True, own_card=members[0], member_cards=members,
        bonus_permil=0.0, overall_power=1_000_000.0, k_constant=K,
        singer_weight=1.0,
    )
    assert v["Y-001"] == pytest.approx(0.001 * 1_000_000 * K)
    assert v["Y-002"] == pytest.approx(0.020 * 1_000_000 * K)


def test_song_score_bonus_shares_the_scale_of_its_sibling_rate_channels():
    """The same invariant, stated without any constant: a score-BONUS rate
    of `r` and a skill-effect-up of `r` over the whole pool must land on the
    SAME scale. A factor-of-1000 slip between them is invisible unless the two
    channels are compared."""
    members = tuple(make_card(f"card-m{i}-5-uniq-0000-00", member=f"M{i}", stats=_STATS)
                    for i in range(5))
    v = power_node_values(
        {"Y-002": [_SONG_SOLO], "R-002": [_EFF_UP]},
        is_leader=True, own_card=members[0], member_cards=members,
        bonus_permil=0.0, overall_power=1_000_000.0, k_constant=K,
        active_pool_rate=1.0, singer_weight=1.0,
    )
    # _EFF_UP is 40 permil over a full pool -> 0.040 * Power * K.
    # _SONG_SOLO is 20 permil -> 0.020 * Power * K. Same scale, ratio exactly 2.
    assert v["R-002"] == pytest.approx(2.0 * v["Y-002"])


def test_a_song_scoped_node_is_worth_nothing_when_the_leader_does_not_sing():
    """`singer_weight` gates the solo/group scopes; the pooled `all` bucket is
    every song's, so it is not gated. Pinned so a change to the unit scale
    cannot quietly take the gate with it."""
    members = tuple(make_card(f"card-m{i}-5-uniq-0000-00", member=f"M{i}", stats=_STATS)
                    for i in range(5))
    v = power_node_values(
        {"Y-001": [_SONG_ALL], "Y-002": [_SONG_SOLO]},
        is_leader=True, own_card=members[0], member_cards=members,
        bonus_permil=0.0, overall_power=1_000_000.0, k_constant=K,
        singer_weight=0.0,
    )
    assert v["Y-002"] == pytest.approx(0.0)
    assert v["Y-001"] == pytest.approx(0.001 * 1_000_000 * K)


def test_active_channels_zero_without_a_pool_or_active_member():
    members = tuple(make_card(f"card-m{i}-5-uniq-0000-00", member=f"M{i}", stats=_STATS)
                    for i in range(5))
    # no pool -> no active value at all.
    v0 = power_node_values(
        {"R-002": [_EFF_UP], "B-007": [_ACT_UP]},
        is_leader=True, own_card=members[0], member_cards=members,
        bonus_permil=0.0, overall_power=1_000_000.0, k_constant=K, active_pool_rate=0.0,
    )
    assert v0["R-002"] == 0.0 and v0["B-007"] == 0.0
    # a pure outfit-leader (own card is NOT one of the members) fields no active,
    # so its own activation node is worth nothing.
    outsider = make_card("card-lead-5-uniq-0000-00", member="Lead", stats=_STATS)
    v1 = power_node_values(
        {"B-007": [_ACT_UP]},
        is_leader=True, own_card=outsider, member_cards=members,
        bonus_permil=0.0, overall_power=1_000_000.0, k_constant=K, active_pool_rate=0.5,
    )
    assert v1["B-007"] == 0.0


def test_grouping_node_valued_from_the_chr_override():
    # 3 of 5 members share the "Myth" grouping; the node grants +v all-stats to
    # the members in THIS character's own grouping, read from the chr override.
    members = tuple(
        make_card(
            f"card-m{i}-5-uniq-0000-00", member=f"M{i}",
            generations=frozenset({"Myth"}) if i < 3 else frozenset({"Gen0"}),
            stats=_STATS,
        )
        for i in range(5)
    )
    skilltree_nodes = {
        "G-011": {"chr": {"chr-m0": {"v": 225, "tgt": "character_grouping-grp-myth-always"}}}
    }
    v = power_node_values(
        {"G-011": ["ignored-master-variant"]},
        is_leader=False, own_card=members[0], member_cards=members,
        bonus_permil=0.0, overall_power=0.0, k_constant=K,
        skilltree={"nodes": skilltree_nodes}, own_chr="chr-m0",
    )
    # +225 to each of 3 stats of the 3 Myth members, dUS/dPower = K at bonus 0.
    assert v["G-011"] == pytest.approx(225 * 3 * 3 * K)


def test_connect_multipliers_boost_the_covered_valuable_nodes():
    from hbuilder.domain.board_plan import _connect_multipliers
    skilltree = {
        "models": {"m": {"S-001": [0, 0], "R-001": [1, 0], "R-002": [0, 1]}},
        "extents": {"area-a": [[1, 0]]},          # covers the node one step to the right
        "connect": {"eff-a": {"area": "area-a", "levels": [{"v": 1500}, {"v": 2000}]}},
        "card_connect": {"card-x": "eff-a"},
    }
    # R-001 is covered and valuable, R-002 is neither.
    mult = _connect_multipliers(skilltree, "m", {"R-001": 100.0, "R-002": 5.0})
    assert mult["R-001"] == pytest.approx(3.0)   # 1 + 2000/1000 (bloom-5 level)
    assert "R-002" not in mult                   # outside every socket's shape


def test_effect_string_for_uses_the_grouping_override_else_the_master_effect():
    from hbuilder.domain.board_plan import _effect_string_for
    skilltree = {"nodes": {
        "G-011": {"chr": {"chr-04003": {"v": 225, "tgt": "character_grouping-grp-myth-always"}}},
        "R-001": {"chr": {}},
    }}
    effects = {"R-001": ["skill_tree_effect-all_parameter_up-50-skill_tree_effect_target-all-set_live_leader"]}
    # grouping node -> the character's exact override, rebuilt as an effect id.
    g = _effect_string_for(skilltree, "G-011", "chr-04003", effects)
    assert "grp-myth" in g and "-225-" in g
    # non-grouping node -> its single master effect.
    assert _effect_string_for(skilltree, "R-001", "chr-04003", effects) == effects["R-001"][0]
    # a node with neither -> None (nothing to socket or score).
    assert _effect_string_for(skilltree, "X-999", "chr-04003", effects) is None


# ---------------------------------------------------------------------------
# Real Connect assignment (`connect_cards`): the develop re-score places the
# account's cards (one card, one socket, team-wide) instead of the idealised
# best-shape-everywhere multiplier. `None` keeps the idealised model exactly.
# ---------------------------------------------------------------------------

# An account-scope stat effect (reaches the members from ANY board), so the
# leader-only Alpha's nodes carry value: a leader-only card's own-card (blue)
# effects reach nobody, and these tests pin the Connect
# MECHANICS, not the blue reach.
_B_EFFECT = ("skill_tree_effect-sense_up-50-skill_tree_effect_target"
             "-all-always")


def _connect_fixture():
    """Two one-branch boards. Character A's socket shape can reach TWO valued
    nodes, character B's only ONE, so a placement on A is worth double."""
    from hbuilder.domain.board_optimizer import BoardPlan
    from hbuilder.domain.board_plan import TeamBoardPlan

    leader = make_card("card-00001-5-uniq-0000-00", member="Alpha", stats=_STATS)
    member = make_card("card-00002-5-uniq-0000-00", member="Beta", stats=_STATS)
    skilltree = {
        "nodes": {"B-001": {"chr": {}}, "B-002": {"chr": {}}},
        "members": [{"chr": "chr-00001", "model": "mA"},
                    {"chr": "chr-00002", "model": "mB"}],
        "models": {"mA": {"S-001": [0, 0], "B-001": [1, 0], "B-002": [2, 0]},
                   "mB": {"S-001": [0, 0], "B-001": [1, 0]}},
        # idealised path needs these; the real path reads `connect_cards` instead.
        "extents": {"area-2": [[1, 0], [2, 0]]},
        "connect": {"eff-2": {"area": "area-2", "levels": [{"v": 1000}]}},
        "card_connect": {"card-shape": "eff-2"},
    }
    effects = {"B-001": [_B_EFFECT], "B-002": [_B_EFFECT]}
    plan = TeamBoardPlan(total_gain=0.0, per_character={
        leader.id: BoardPlan(selected=frozenset({"B-001", "B-002"}), spent=0, value=0.0),
        member.id: BoardPlan(selected=frozenset({"B-001"}), spent=0, value=0.0),
    })
    return leader, member, skilltree, effects, plan


def _model_from_plan(leader, member, skilltree, effects, plan, **kw):
    from hbuilder.domain.board_plan import board_model_from_plan
    return board_model_from_plan(
        plan, leader, (member,), skilltree, effects,
        overall_power=0.0, bonus_permil=0.0, k_constant=K, active_pool_rate=0.0,
        **kw)


def _mult(board, member_name, node_id):
    nodes = board.characters[member_name].nodes
    return next(n.connect_multiplier for n in nodes if n.node_id == node_id)


def test_real_connect_assigns_each_card_once_best_socket_first():
    leader, member, skilltree, effects, plan = _connect_fixture()
    placements: list = []
    board = _model_from_plan(
        leader, member, skilltree, effects, plan,
        connect_cards={"card-big": (((1, 0), (2, 0)), 1000),
                       "card-small": (((1, 0), (2, 0)), 400)},
        placements_out=placements,
    )
    # the x2.0 card takes the double-value board (Alpha), the x1.4 the other.
    by_chr = {p.chr: p for p in placements}
    assert by_chr["chr-00001"].card == "card-big"
    assert by_chr["chr-00002"].card == "card-small"
    assert _mult(board, "Alpha", "B-001") == pytest.approx(2.0)
    assert _mult(board, "Alpha", "B-002") == pytest.approx(2.0)
    assert _mult(board, "Beta", "B-001") == pytest.approx(1.4)
    # one card, one socket: each card id appears exactly once.
    assert len({p.card for p in placements}) == len(placements) == 2


def test_real_connect_never_reuses_a_card_across_the_team():
    leader, member, skilltree, effects, plan = _connect_fixture()
    placements: list = []
    board = _model_from_plan(
        leader, member, skilltree, effects, plan,
        connect_cards={"card-only": (((1, 0), (2, 0)), 1000)},
        placements_out=placements,
    )
    # a single owned card lands on the BEST board only; the other stays bare.
    assert [(p.chr, p.card) for p in placements] == [("chr-00001", "card-only")]
    assert _mult(board, "Beta", "B-001") == pytest.approx(1.0)


def test_real_connect_needs_the_socket_bought_except_the_free_root():
    from hbuilder.domain.board_optimizer import BoardPlan
    from hbuilder.domain.board_plan import TeamBoardPlan

    leader, member, skilltree, effects, _ = _connect_fixture()
    # Alpha's model gains a second socket whose branch was NOT bought.
    skilltree["models"]["mA"]["S-002"] = [0, 1]
    skilltree["models"]["mA"]["B-003"] = [1, 1]
    skilltree["nodes"]["B-003"] = {"chr": {}}
    effects["B-003"] = [_B_EFFECT]
    plan = TeamBoardPlan(total_gain=0.0, per_character={
        leader.id: BoardPlan(selected=frozenset({"B-001", "B-002"}), spent=0, value=0.0),
        member.id: BoardPlan(selected=frozenset({"B-001"}), spent=0, value=0.0),
    })
    placements: list = []
    _model_from_plan(
        leader, member, skilltree, effects, plan,
        connect_cards={"card-a": (((1, 0),), 1000), "card-b": (((1, 0),), 1000)},
        placements_out=placements,
    )
    # S-002 is not in the bought set -> no placement may use it.
    assert all(p.socket == "S-001" for p in placements)


def test_connect_cards_none_keeps_the_idealised_multipliers():
    leader, member, skilltree, effects, plan = _connect_fixture()
    placements: list = []
    board = _model_from_plan(leader, member, skilltree, effects, plan,
                             placements_out=placements)
    # idealised: best shape at max bloom on EVERY board, no placements reported.
    assert placements == []
    assert _mult(board, "Alpha", "B-001") == pytest.approx(2.0)
    assert _mult(board, "Beta", "B-001") == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Member swap-refine (`refine_members`): hill-climb the five member slots by
# the injected target metric, leader fixed. develop_fn/score_fn are stubs, so
# these pin the ORCHESTRATION: shortlist -> exact accept, stop, rounds, and
# the Team rules filtering invalid mutations.
# ---------------------------------------------------------------------------


def _refine_fixture(values):
    """A developed seed team plus stub develop/score wired to `values`
    (card id -> worth; a team scores the sum of its members' worths)."""
    from hbuilder.domain.board_plan import BoardOptimisedTeam, TeamBoardPlan
    from hbuilder.domain.team import Team

    leader = make_card("card-L-5-uniq-0000-00", member="Leader", stats=_STATS)
    members = tuple(make_card(f"card-m{i}-5-uniq-0000-00", member=f"M{i}", stats=_STATS)
                    for i in range(5))
    seed = BoardOptimisedTeam(
        team=Team(leader=leader, members=members), board_off_us=0.0,
        optimised_us=0.0, plan=TeamBoardPlan(total_gain=0.0, per_character={}))
    developed = []

    def develop_fn(team):
        developed.append(team)
        return BoardOptimisedTeam(team=team, board_off_us=0.0, optimised_us=0.0,
                                  plan=TeamBoardPlan(total_gain=0.0, per_character={}))

    def score_fn(team, board):
        return sum(values.get(c.id, 0.0) for c in team.members)

    return seed, develop_fn, score_fn, developed


def test_refine_members_accepts_the_best_improving_swap_and_logs_it():
    from hbuilder.domain.board_plan import refine_members

    values = {f"card-m{i}-5-uniq-0000-00": 10.0 for i in range(5)}
    good = make_card("card-good-5-uniq-0000-00", member="Good", stats=_STATS)
    better = make_card("card-better-5-uniq-0000-00", member="Better", stats=_STATS)
    values[good.id], values[better.id] = 15.0, 30.0
    seed, develop_fn, score_fn, developed = _refine_fixture(values)

    refined, score, swaps = refine_members(
        seed, candidates=[good, better], develop_fn=develop_fn,
        score_fn=score_fn, max_rounds=1)
    # one round -> ONE accepted swap, and it is the best one (better, not good).
    assert len(swaps) == 1
    assert swaps[0]["in"] == better.id
    assert swaps[0]["gain"] == pytest.approx(20.0)
    assert better.id in {c.id for c in refined.team.members}
    assert refined.team.leader.id == seed.team.leader.id
    assert score == pytest.approx(70.0)  # 4x10 + 30


def test_refine_members_batch_score_fn_gives_the_same_answer_and_is_used_for_the_shortlist():
    """`batch_score_fn(teams)` replaces the per-candidate develop+score of the
    EXACT stage (what a process pool parallelises); the accepted swap, its
    gain and the final score must be identical to the serial path, and only
    the accepted winner is developed in-process."""
    from hbuilder.domain.board_plan import refine_members

    values = {f"card-m{i}-5-uniq-0000-00": 10.0 for i in range(5)}
    good = make_card("card-good-5-uniq-0000-00", member="Good", stats=_STATS)
    better = make_card("card-better-5-uniq-0000-00", member="Better", stats=_STATS)
    values[good.id], values[better.id] = 15.0, 30.0
    seed, develop_fn, score_fn, developed = _refine_fixture(values)
    serial = refine_members(seed, candidates=[good, better], develop_fn=develop_fn,
                            score_fn=score_fn, max_rounds=1)
    seed2, develop_fn2, score_fn2, developed2 = _refine_fixture(values)
    batches = []

    def batch_score_fn(teams):
        batches.append(len(teams))
        return [score_fn2(t, None) for t in teams]

    batched = refine_members(seed2, candidates=[good, better], develop_fn=develop_fn2,
                             score_fn=score_fn2, batch_score_fn=batch_score_fn,
                             max_rounds=1)
    assert batched[1] == pytest.approx(serial[1])
    assert batched[2] == serial[2]
    assert {c.id for c in batched[0].team.members} == {c.id for c in serial[0].team.members}
    # one batch per round covering every slot's shortlist (5 slots x 2 candidates)
    assert batches == [10]
    # only the accepted winner was developed in-process
    assert len(developed2) == 1


def test_refine_members_returns_the_seed_when_nothing_improves():
    from hbuilder.domain.board_plan import refine_members

    values = {f"card-m{i}-5-uniq-0000-00": 10.0 for i in range(5)}
    worse = make_card("card-worse-5-uniq-0000-00", member="Worse", stats=_STATS)
    values[worse.id] = 1.0
    seed, develop_fn, score_fn, developed = _refine_fixture(values)

    refined, score, swaps = refine_members(
        seed, candidates=[worse], develop_fn=develop_fn, score_fn=score_fn)
    assert refined is seed and swaps == [] and score == pytest.approx(50.0)


def test_refine_members_skips_mutations_the_team_rules_reject():
    from hbuilder.domain.board_plan import refine_members

    values = {f"card-m{i}-5-uniq-0000-00": 10.0 for i in range(5)}
    # Same holomem as slot 0's card: replacing ANY OTHER slot with it duplicates
    # the member and must be skipped, not crash. Only slot 0 itself may take it.
    twin = make_card("card-twin-5-uniq-0000-00", member="M0", stats=_STATS)
    values[twin.id] = 99.0
    seed, develop_fn, score_fn, developed = _refine_fixture(values)

    refined, _score, swaps = refine_members(
        seed, candidates=[twin], develop_fn=develop_fn, score_fn=score_fn,
        max_rounds=1)
    # accepted exactly once, in slot 0 (the only legal spot).
    assert [s["slot"] for s in swaps] == [0]
    assert refined.team.members[0].id == twin.id


def test_refine_members_shortlist_bounds_the_develop_calls():
    from hbuilder.domain.board_plan import refine_members

    values = {f"card-m{i}-5-uniq-0000-00": 10.0 for i in range(5)}
    pool = []
    for i in range(20):
        c = make_card(f"card-c{i:02d}-5-uniq-0000-00", member=f"C{i}", stats=_STATS)
        values[c.id] = 20.0 + i          # all improve; ranked by the cheap stage
        pool.append(c)
    seed, develop_fn, score_fn, developed = _refine_fixture(values)

    refine_members(seed, candidates=pool, develop_fn=develop_fn,
                   score_fn=score_fn, shortlist=3, max_rounds=1)
    # 5 slots x top-3 shortlist -> at most 15 develops, never the full 100.
    assert len(developed) <= 15


def test_refine_members_prefilter_orders_the_shortlist_but_never_decides():
    from hbuilder.domain.board_plan import refine_members

    values = {f"card-m{i}-5-uniq-0000-00": 10.0 for i in range(5)}
    pool = []
    for i in range(10):
        c = make_card(f"card-p{i}-5-uniq-0000-00", member=f"P{i}", stats=_STATS)
        values[c.id] = 20.0 + i
        pool.append(c)
    seed, develop_fn, score_fn, developed = _refine_fixture(values)

    # The prefilter ranks candidates BACKWARDS: with shortlist=3 only the three
    # candidates the prefilter likes best (the WORST by the real metric) are
    # developed, and the accepted swap is the best of those by the REAL metric.
    def prefilter_fn(team, board):
        return -score_fn(team, board)

    refined, _score, swaps = refine_members(
        seed, candidates=pool, develop_fn=develop_fn, score_fn=score_fn,
        prefilter_fn=prefilter_fn, shortlist=3, max_rounds=1)
    assert len(swaps) == 1
    # prefilter's top-3 are p0/p1/p2 (real worths 20/21/22); the exact stage
    # accepts the best of THOSE — p2 — never p9, which the prefilter buried.
    assert swaps[0]["in"] == pool[2].id
    assert all(t.members not in () for t in developed)  # developed only shortlisted
    assert len(developed) <= 3 * 5


def test_bench_characters_ride_along_and_the_team_overrides_its_own():
    """`bench_characters` folds the account's CURRENT bench boards into the
    developed model (their GREENs are account-global), and a character the team
    develops takes the PLAN's board, never its stale bench entry."""
    leader, member, skilltree, effects, plan = _connect_fixture()
    bench = {
        # a pure bench holomem: rides along untouched.
        "Gamma": [{"node": "G-001",
                   "effects": ["skill_tree_effect-all_parameter_up-5-"
                               "skill_tree_effect_target-all-always"],
                   "connect_multiplier": 1.5}],
        # the team's own Alpha: must be REPLACED by the developed plan.
        "Alpha": [{"node": "G-099",
                   "effects": ["skill_tree_effect-all_parameter_up-5-"
                               "skill_tree_effect_target-all-always"],
                   "connect_multiplier": 1.0}],
    }
    board = _model_from_plan(leader, member, skilltree, effects, plan,
                             bench_characters=bench)
    assert "Gamma" in board.characters
    assert board.characters["Gamma"].nodes[0].connect_multiplier == 1.5
    alpha_nodes = {n.node_id for n in board.characters["Alpha"].nodes}
    assert alpha_nodes == {"B-001", "B-002"}   # the plan's, not the bench G-099
    assert "Beta" in board.characters


# ---------------------------------------------------------------------------
# The developed board must translate the chart's chr-id cast to holomem names,
# or every singer-conditional term (R-044/062/063 "+stat% to all when this
# holomem sings", R-002, the yellow solo/group song bonus) is silently zero on
# the develop path -- and worse, the song-scope `song_score_bonus_for` then
# SUPERSEDES the pooled bucket with 0, so a leader would score LOWER on their
# own song than on a stranger's.
# ---------------------------------------------------------------------------

_R044 = ("skill_tree_effect-technique_up_permil_up-100-skill_tree_effect_target-all"
         "-skill_tree_effect_passive_trigger-music_skill_tree_character-set_live_leader")


def _singer_fixture():
    from hbuilder.domain.board_optimizer import BoardPlan
    from hbuilder.domain.board_plan import TeamBoardPlan

    leader = make_card("card-00018-5-uniq-0000-00", member="Hoshimachi Suisei", stats=_STATS)
    member = make_card("card-00002-5-uniq-0000-00", member="Beta", stats=_STATS)
    skilltree = {"nodes": {"R-044": {"chr": {}}}, "members": [], "models": {}}
    effects = {"R-044": [_R044]}
    plan = TeamBoardPlan(total_gain=0.0, per_character={
        leader.id: BoardPlan(selected=frozenset({"R-044"}), spent=0, value=0.0),
    })
    return leader, member, skilltree, effects, plan


def test_developed_board_fires_singer_conditionals_for_the_teams_own_chr_ids():
    leader, member, skilltree, effects, plan = _singer_fixture()
    board = _model_from_plan(leader, member, skilltree, effects, plan)
    # the chart names the leader by chr-id, as charts.json does
    power = board.conditional_stat_power_for((member,), leader.member, ("chr-00018",))
    assert power > 0
    # ...and matches the same call by name (what fixtures always did)
    assert power == board.conditional_stat_power_for((member,), leader.member,
                                                     ("Hoshimachi Suisei",))


def test_developed_board_accepts_a_wider_name_by_chr_for_bench_singers():
    leader, member, skilltree, effects, plan = _singer_fixture()
    board = _model_from_plan(leader, member, skilltree, effects, plan,
                             name_by_chr={"chr-99999": "Somebody Else"})
    assert board.name_by_chr["chr-99999"] == "Somebody Else"
    # the team's own mapping is not lost under a caller-supplied one
    assert board.name_by_chr["chr-00018"] == "Hoshimachi Suisei"


# ---------------------------------------------------------------------------
# The plan is a fixed function of (character,
# role, budget, SINGER CONTEXT). Conditional reds (R-002/044/062/063) and the
# solo/group yellows are worth `singer_weight` of their value; a leader-only
# card's blues reach nobody; a bench character values only its greens.
# ---------------------------------------------------------------------------

_R002 = ("skill_tree_effect-live_active_skill_effect_up_permil_up-100"
         "-skill_tree_effect_target-all-skill_tree_effect_passive_trigger"
         "-music_skill_tree_character-set_live_leader")
_Y_SOLO = ("skill_tree_effect-live_score_bonus_add_permil_up_by_music_skill_tree_character"
           "_and_music_singer_type-5-skill_tree_effect_passive_trigger-music_skill_tree"
           "_character_and_music_singer_type-solo-always-solo")
_Y_GROUP = ("skill_tree_effect-live_score_bonus_add_permil_up_by_music_skill_tree_character"
            "_and_music_singer_type-5-skill_tree_effect_passive_trigger-music_skill_tree"
            "_character_and_music_singer_type-group-always-group")
_Y_ALL = ("skill_tree_effect-live_score_bonus_add_permil_up_by_music_skill_tree_character"
          "_and_music_singer_type-1-skill_tree_effect_passive_trigger-music_skill_tree"
          "_character_and_music_singer_type-all-always-all")


def _five_members():
    return tuple(make_card(f"card-m{i}-5-uniq-0000-00", member=f"M{i}", stats=_STATS)
                 for i in range(5))


def test_conditional_reds_are_worth_singer_weight_of_their_value():
    members = _five_members()
    leader = make_card("card-lead-5-uniq-0000-00", member="Lead", stats=_STATS)
    kw = dict(is_leader=True, own_card=leader, member_cards=members,
              bonus_permil=0.0, overall_power=1_000_000.0, k_constant=K,
              active_pool_rate=0.5)
    off = power_node_values({"R-044": [_R044], "R-002": [_R002]}, **kw)
    on = power_node_values({"R-044": [_R044], "R-002": [_R002]}, singer_weight=1.0, **kw)
    half = power_node_values({"R-044": [_R044], "R-002": [_R002]}, singer_weight=0.5, **kw)
    # song-agnostic (default): the music_skill_tree_character trigger never fires.
    assert off["R-044"] == 0.0 and off["R-002"] == 0.0
    # the leader sings: R-044 = +10% technique to all 5 members; R-002 = +10% pool.
    assert on["R-044"] == pytest.approx(5 * 100 * K)
    assert on["R-002"] == pytest.approx(0.5 * 0.10 * 1_000_000 * K)
    assert half["R-044"] == pytest.approx(on["R-044"] / 2)
    # an UNCONDITIONAL red is untouched by the weight.
    assert (power_node_values({"R-001": [_LEADER]}, **kw)["R-001"]
            == power_node_values({"R-001": [_LEADER]}, singer_weight=1.0, **kw)["R-001"] > 0)


def test_yellow_solo_group_follow_singer_weight_and_all_stays_pooled():
    """What this test is FOR is the gate — solo/group follow `singer_weight`,
    `all` never does — and those assertions are untouched.

    ⚠ The scale constant is `Power*K` per whole unit of score-bonus rate, the
    same one this node's two siblings on the rate axis use — not
    `Power*K/1000`, which would divide an already-fractional
    `BoardEffect.value` by 1000 a second time. See
    `test_song_score_bonus_is_valued_per_UNIT_of_rate_not_per_permil`."""
    members = _five_members()
    leader = make_card("card-lead-5-uniq-0000-00", member="Lead", stats=_STATS)
    effects = {"Y-001": [_Y_SOLO], "Y-002": [_Y_GROUP], "Y-003": [_Y_ALL]}
    kw = dict(is_leader=True, own_card=leader, member_cards=members,
              bonus_permil=0.0, overall_power=1_000_000.0, k_constant=K)
    off = power_node_values(effects, **kw)
    on = power_node_values(effects, singer_weight=1.0, **kw)
    per_unit = 1_000_000.0 * K
    assert off["Y-001"] == 0.0 and off["Y-002"] == 0.0
    assert on["Y-001"] == pytest.approx(0.005 * per_unit)
    assert on["Y-002"] == pytest.approx(0.005 * per_unit)
    # `all` is the pooled bucket every song-agnostic score counts: weight 1 always.
    assert off["Y-003"] == on["Y-003"] == pytest.approx(0.001 * per_unit)


def test_a_song_pays_only_its_own_scope_of_yellows():
    """The scorer pays ONE scope per song (`song_score_bonus_for(singers,
    scope=singer_type)`). Valuing both lets a singer's solo yellows take sockets on her
    GROUP song and, in the cap's room, zero her group ones (about -1.7 % on real teams).
    "" (not known) keeps both."""
    members = _five_members()
    leader = make_card("card-lead-5-uniq-0000-00", member="Lead", stats=_STATS)
    effects = {"Y-001": [_Y_SOLO], "Y-002": [_Y_GROUP], "Y-003": [_Y_ALL]}
    kw = dict(is_leader=True, own_card=leader, member_cards=members, bonus_permil=0.0,
              overall_power=1_000_000.0, k_constant=K, singer_weight=1.0)
    legacy = power_node_values(effects, **kw)
    group = power_node_values(effects, song_scope="group", **kw)
    solo = power_node_values(effects, song_scope="solo", **kw)
    assert group["Y-001"] == 0.0 and group["Y-002"] == legacy["Y-002"] > 0
    assert solo["Y-002"] == 0.0 and solo["Y-001"] == legacy["Y-001"] > 0
    assert power_node_values(effects, song_scope="all", **kw)["Y-001"] == 0.0


def test_a_song_cast_carries_its_scope_to_the_bench_yellows():
    from hbuilder.domain.board_plan import SongCast, bench_yellow_pays
    group = SongCast({"chr-00003"}, "group")
    assert group == frozenset({"chr-00003"})          # read as the cast everywhere
    assert bench_yellow_pays("group", "chr-00003", group)
    assert not bench_yellow_pays("solo", "chr-00003", group)
    assert not bench_yellow_pays("group", "chr-00009", group)
    assert bench_yellow_pays("solo", "chr-00003", frozenset({"chr-00003"}))  # scope unknown
    assert bench_yellow_pays("all", "chr-00003", SongCast({"chr-00003"}, "all"))
    import pickle
    back = pickle.loads(pickle.dumps(group))          # worker processes get it whole
    assert back == group and back.scope == "group"


def test_blue_of_a_leader_only_card_reaches_nobody_but_a_member_card_reaches_itself():
    members = _five_members()
    leader = make_card("card-lead-5-uniq-0000-00", member="Lead", stats=_STATS)
    v_lead = _values({"B-001": [_MEMBER]}, is_leader=True, own=leader, members=members)
    assert v_lead["B-001"] == 0.0
    v_mem = _values({"B-001": [_MEMBER]}, is_leader=False, own=members[0], members=members)
    assert v_mem["B-001"] == pytest.approx(50 * K)


def test_bench_node_values_keep_only_the_greens():
    from hbuilder.domain.board_plan import bench_node_values
    members = _five_members()
    _G_ALL = ("skill_tree_effect-sense_up-10-skill_tree_effect_target-all-always")
    v = bench_node_values(
        {"G-001": [_G_ALL], "R-001": [_LEADER], "B-001": [_MEMBER],
         "R-044": [_R044], "Y-003": [_Y_ALL]},
        member_cards=members, bonus_permil=0.0, overall_power=1_000_000.0, k_constant=K,
    )
    assert v["G-001"] == pytest.approx(5 * 10 * K)
    assert v["R-001"] == v["B-001"] == v["R-044"] == v["Y-003"] == 0.0


def test_optimize_team_boards_singer_weight_reaches_only_the_leader_plan():
    from hbuilder.domain.skilltree import SkillTree, TreeNode, ROOT_NODE
    from hbuilder.domain.board_plan import optimize_team_boards

    nodes = {
        ROOT_NODE: TreeNode(node_id=ROOT_NODE, branch="S", cost=0, player_level=None),
        "R-044": TreeNode(node_id="R-044", branch="R", cost=1, player_level=None),
    }
    adjacency = {ROOT_NODE: frozenset({"R-044"}), "R-044": frozenset({ROOT_NODE})}
    tree = SkillTree(nodes=nodes, positions={}, adjacency=adjacency)
    leader = make_card("card-lead-5-uniq-0000-00", member="Lead", stats=_STATS)
    members = _five_members()
    common = dict(tree_for=lambda c: tree, budget_for=lambda c: 10,
                  node_effect_ids={"R-044": [_R044]}, overall_power=0.0,
                  bonus_permil=0.0, k_constant=K)
    agnostic = optimize_team_boards(leader, members, **common)
    sung = optimize_team_boards(leader, members, singer_weight=1.0, **common)
    assert "R-044" not in agnostic.per_character[leader.id].selected
    assert "R-044" in sung.per_character[leader.id].selected
    # members never see the singer context (leader-scope node, not theirs).
    for m in members:
        assert "R-044" not in sung.per_character[m.id].selected


def test_socket_bench_puts_the_bench_root_socket_in_the_same_matching():
    """`socket_bench`: an off-team board's ROOT socket joins the team's Connect
    matching -- each card used once account-wide -- and the account's stale S-001 card stops
    counting on the nodes an S-001 shape reaches; nodes outside that reach keep theirs."""
    leader, member, skilltree, effects, plan = _connect_fixture()
    green = "skill_tree_effect-all_parameter_up-5-skill_tree_effect_target-all-always"
    skilltree = {**skilltree,
                 "nodes": {**skilltree["nodes"], "G-001": {"chr": {}}, "G-002": {"chr": {}}},
                 "members": [*skilltree["members"], {"chr": "chr-00003", "model": "mG"}],
                 "models": {**skilltree["models"],
                            "mG": {"S-001": [0, 0], "G-001": [1, 0], "G-002": [6, 6]}}}
    bench = {"Gamma": [{"node": "G-001", "effects": [green], "connect_multiplier": 1.5},
                       {"node": "G-002", "effects": [green], "connect_multiplier": 1.5}]}
    cards = {"card-a": (((1, 0), (2, 0)), 1000), "card-b": (((1, 0), (2, 0)), 1000),
             "card-c": (((1, 0), (2, 0)), 1000)}
    placements: list = []
    board = _model_from_plan(leader, member, skilltree, effects, plan, connect_cards=cards,
                             bench_characters=bench, name_by_chr={"chr-00003": "Gamma"},
                             placements_out=placements, socket_bench=True)
    assert len({p.card for p in placements}) == len(placements)          # once account-wide
    assert {p.chr for p in placements} <= {"chr-00001", "chr-00002", "chr-00003"}
    assert _mult(board, "Gamma", "G-001") in (pytest.approx(1.0), pytest.approx(2.0))
    assert _mult(board, "Gamma", "G-002") == pytest.approx(1.5)        # beyond the root's reach
    # off by default: the bench rides along as it is
    board = _model_from_plan(leader, member, skilltree, effects, plan, connect_cards=cards,
                             bench_characters=bench, name_by_chr={"chr-00003": "Gamma"})
    assert _mult(board, "Gamma", "G-001") == pytest.approx(1.5)


def _bench_fixture():
    """`_connect_fixture` plus an off-team board "Gamma" (chr-00003): its root S-001 can
    lift the green G-001; its S-002 sits on a blue branch (B-009) no green is near --
    as on the real layout, where no bench S-002..S-004 reaches a green."""
    leader, member, skilltree, effects, plan = _connect_fixture()
    green = "skill_tree_effect-all_parameter_up-5-skill_tree_effect_target-all-always"
    skilltree = {**skilltree,
                 "nodes": {**skilltree["nodes"], "G-001": {"chr": {}}, "B-009": {"chr": {}}},
                 "members": [*skilltree["members"], {"chr": "chr-00003", "model": "mG"}],
                 "models": {**skilltree["models"],
                            "mG": {"S-001": [0, 0], "G-001": [1, 0], "S-002": [9, 9], "B-009": [10, 9]}}}
    bench = {"Gamma": [{"node": "G-001", "effects": [green], "connect_multiplier": 2.0}]}
    cards = {"card-a": (((1, 0), (2, 0)), 1000), "card-b": (((1, 0), (2, 0)), 1000)}
    return leader, member, skilltree, effects, plan, bench, cards


def test_held_bench_cards_are_the_ones_lifting_a_bench_green():
    """A card on an off-team socket whose shape reaches a GREEN keeps lifting every team
    (`bench_characters` carries its multiplier), so it is held; one on a socket that
    reaches no green lifts nothing a team sees and stays free; the team's own boards are
    re-assigned, so their cards are free too."""
    from hbuilder.domain.board_plan import held_bench_cards
    _l, _m, skilltree, _e, _p, _b, cards = _bench_fixture()
    sockets = {"chr-00003": {"S-001": "card-a", "S-002": "card-b"}}
    assert held_bench_cards(sockets, {"chr-00001", "chr-00002"}, skilltree, cards) == {"card-a"}
    assert held_bench_cards(sockets, {"chr-00003"}, skilltree, cards) == frozenset()
    assert held_bench_cards(None, set(), skilltree, cards) == frozenset()


def test_team_matching_never_reuses_a_card_held_on_a_bench_green():
    """The team must not socket a card that ALSO stays on a bench root lifting its green --
    the card would count twice. With `bench_sockets` the held card is out of the team's pool; the card on
    the green-less bench socket may still move to the team."""
    leader, member, skilltree, effects, plan, bench, cards = _bench_fixture()
    sockets = {"chr-00003": {"S-001": "card-a", "S-002": "card-b"}}
    placements: list = []
    board = _model_from_plan(leader, member, skilltree, effects, plan, connect_cards=cards,
                             bench_characters=bench, name_by_chr={"chr-00003": "Gamma"},
                             placements_out=placements, bench_sockets=sockets)
    assert {p.card for p in placements} == {"card-b"}
    assert _mult(board, "Gamma", "G-001") == pytest.approx(2.0)        # the bench as it is
    placements = []
    _model_from_plan(leader, member, skilltree, effects, plan, connect_cards=cards,
                     bench_characters=bench, name_by_chr={"chr-00003": "Gamma"},
                     placements_out=placements)                          # no bench info: as before
    assert "card-a" in {p.card for p in placements}


def test_socket_bench_with_bench_sockets_reassigns_every_unlocked_bench_socket():
    """With `socket_bench` and `bench_sockets` every unlocked socket of an off-team board
    joins the matching (S-002 too) and the board's multipliers are re-derived from the new
    placements alone: each card once account-wide, nothing double counted."""
    leader, member, skilltree, effects, plan, bench, cards = _bench_fixture()
    sockets = {"chr-00003": {"S-001": "card-a", "S-002": None}}
    placements: list = []
    board = _model_from_plan(leader, member, skilltree, effects, plan, connect_cards=cards,
                             bench_characters=bench, name_by_chr={"chr-00003": "Gamma"},
                             placements_out=placements, socket_bench=True, bench_sockets=sockets)
    assert len({p.card for p in placements}) == len(placements) == 2
    on_gamma = {p.socket: p.card for p in placements if p.chr == "chr-00003"}
    assert set(on_gamma) <= {"S-001", "S-002"}
    assert _mult(board, "Gamma", "G-001") == pytest.approx(2.0 if "S-001" in on_gamma else 1.0)


_SOLO_YELLOW = ("skill_tree_effect-live_score_bonus_add_permil_up_by_music_skill_tree_character"
                "_and_music_singer_type-5-skill_tree_effect_passive_trigger-music_skill_tree"
                "_character_and_music_singer_type-solo-always-solo")


def _yellow_bench_fixture():
    """`_bench_fixture` with a SOLO yellow (Y-007) beside Gamma's S-002: a card there
    lifts nothing green, but Gamma's own song pays her yellows whoever is on the team."""
    leader, member, skilltree, effects, plan, bench, cards = _bench_fixture()
    yellow = {"chr": {"chr-00003": {"t": _SOLO_YELLOW, "tr": "solo"}}}
    skilltree = {**skilltree, "nodes": {**skilltree["nodes"], "Y-007": yellow},
                 "models": {**skilltree["models"],
                            "mG": {**skilltree["models"]["mG"], "Y-007": [11, 9]}}}
    return leader, member, skilltree, {**effects, "Y-007": [_SOLO_YELLOW]}, plan, bench, cards


def test_a_card_on_a_bench_singers_yellow_is_held_on_her_song():
    """The scorer sums every board's song yellows, so a card on a bench singer's yellow
    socket keeps lifting her solo with her off the team; the team must not socket it too
    (that would count it twice, about +2 % on such a solo). Held on a song Gamma sings,
    free on any other, and free when the yellow is not bought."""
    from hbuilder.domain.board_plan import held_bench_cards
    _l, _m, skilltree, _e, _p, _b, cards = _yellow_bench_fixture()
    sockets = {"chr-00003": {"S-001": "card-a", "S-002": "card-b"}}
    team = {"chr-00001", "chr-00002"}
    assert held_bench_cards(sockets, team, skilltree, cards,
                            cast=frozenset({"chr-00003"})) == {"card-a", "card-b"}
    assert held_bench_cards(sockets, team, skilltree, cards,
                            cast=frozenset({"chr-00009"})) == {"card-a"}
    assert held_bench_cards(sockets, team, skilltree, cards) == {"card-a"}    # cast unknown
    assert held_bench_cards(sockets, team, skilltree, cards, cast=frozenset({"chr-00003"}),
                            bought={"chr-00003": frozenset({"G-001"})}) == {"card-a"}


def test_bench_node_values_keep_the_yellows_the_song_pays():
    """`bench_node_values` with the song's cast: a bench singer's solo yellow keeps its
    value (at 0, `socket_bench` would take the card off her yellow socket, a loss of a few
    percent); a non-singer's stays 0, and without a cast only greens count."""
    from hbuilder.domain.board_plan import bench_node_values
    _l, member, skilltree, effects, _p, _b, _c = _yellow_bench_fixture()
    def value(cast):
        return bench_node_values(effects, member_cards=(member,), bonus_permil=0.0,
                                 overall_power=100_000.0, k_constant=K,
                                 skilltree={"nodes": skilltree["nodes"]}, own_chr="chr-00003",
                                 cast=cast).get("Y-007", 0.0)
    assert value(frozenset({"chr-00003"})) > 0
    assert value(frozenset({"chr-00009"})) == 0.0
    assert value(None) == 0.0


def test_a_yellow_lift_partly_over_the_cap_still_wins_when_its_capped_part_is_worth_more():
    """Scaling the yellow values down until the lift fits would drop a yellow card worth
    its capped part and leave room unused. Here one card lifts either the green (4)
    or a 5-permil yellow (10) with 2.5 permil of room: capped, the yellow is worth 5."""
    from hbuilder.domain.board_plan import _yellow_capped_assignment
    _l, _m, skilltree, effects, _p, _b, _c = _yellow_bench_fixture()
    positions = skilltree["models"]["mG"]
    per_char = {"chr-00003": (positions, frozenset({"G-001", "S-002", "B-009", "Y-007"}),
                              {"G-001": 4.0, "B-009": 0.0, "Y-007": 10.0})}
    cards = {"card-a": (((1, 0), (2, 0)), 1000)}
    room_left = [("chr-00003", {"node": "Y-007", "connect_multiplier": 18.5})]  # 100-92.5-5
    _m1, placed = _yellow_capped_assignment(per_char, cards, cast=frozenset({"chr-00003"}),
                                            skilltree=skilltree, node_effect_ids=effects,
                                            fixed_rows=room_left)
    assert [(p.socket, p.card) for p in placed] == [("S-002", "card-a")]


def test_a_holomem_who_leads_and_plays_a_member_card_is_one_board_in_the_matching():
    """Keyed per card, the member entry would replace the leader's in the matching and
    her reds would read 0 (worth up to ~4 %). One entry per holomem,
    valued as the leader WITH her member card's blues -- so a plan listing both cards
    places sockets exactly like a plan listing the leader alone."""
    from hbuilder.domain.board_optimizer import BoardPlan
    from hbuilder.domain.board_plan import TeamBoardPlan
    leader, member, skilltree, effects, plan = _connect_fixture()
    twin = make_card("card-00001-5-uniq-0001-00", member="Alpha", stats=_STATS)
    both = TeamBoardPlan(total_gain=0.0, per_character={
        **plan.per_character, twin.id: plan.per_character[leader.id]})
    from hbuilder.domain.board_plan import board_model_from_plan
    cards = {"card-a": (((1, 0), (2, 0)), 1000)}
    def placements(p):
        out: list = []
        board_model_from_plan(p, leader, (member, twin), skilltree, effects,
                              overall_power=100_000.0, bonus_permil=0.0, k_constant=K,
                              active_pool_rate=0.0, connect_cards=cards, placements_out=out)
        return sorted((x.chr, x.socket, x.card) for x in out)
    assert placements(both) == placements(plan)
    assert ("chr-00001", "S-001", "card-a") in placements(both)


def test_the_socket_matching_stops_paying_for_yellows_past_the_song_cap():
    """Past `SONG_ALL_CAP_PERMIL` a yellow adds nothing, so linear values that price
    every permil would send cards to bench singers already at the cap. With room under
    the cap the yellow socket is filled; with none it is not."""
    from hbuilder.domain.board_plan import _yellow_capped_assignment
    _l, _m, skilltree, effects, _p, _b, _c = _yellow_bench_fixture()
    positions = skilltree["models"]["mG"]
    per_char = {"chr-00003": (positions, frozenset({"S-002", "Y-007"}), {"Y-007": 10.0})}
    cards = {"card-a": (((2, 0),), 1000)}
    cast = frozenset({"chr-00003"})
    _m1, placed = _yellow_capped_assignment(per_char, cards, cast=cast, skilltree=skilltree,
                                            node_effect_ids=effects)
    assert [(p.socket, p.card) for p in placed] == [("S-002", "card-a")]
    full = [("chr-00003", {"node": "Y-007", "connect_multiplier": 20.0})]   # 5 permil x 20
    _m2, placed = _yellow_capped_assignment(per_char, cards, cast=cast, skilltree=skilltree,
                                            node_effect_ids=effects, fixed_rows=full)
    assert placed == []


def test_a_song_yellow_is_priced_at_the_unit_score_it_multiplies():
    # a yellow multiplies every note (`song.py`, `* (1 + board_score_rate)`), so a unit of
    # its rate is worth P * K * (1 + b), not the Score Bonus summand P * K: at P * K a
    # singing leader's board would buy reds over her solo yellows
    from types import SimpleNamespace

    from hbuilder.domain.board_plan import node_values_from_recipes

    def yellow(scope):
        return SimpleNamespace(kind="song_score_bonus", scope="all", song_scope=scope,
                               value=0.005, conditional=False, stat=None)

    card = make_card("card-00031-5-a-0000-00", stats=Stats(1, 1, 1))
    recipes = [("Y-solo", None, (yellow("solo"),)), ("Y-all", None, (yellow("all"),))]
    kw = dict(is_leader=True, own_card=card, member_cards=[card], bonus_permil=900.0,
              overall_power=200_000.0, k_constant=2.0, singer_weight=1.0)
    v = node_values_from_recipes(recipes, song_scope="solo", **kw)
    assert v["Y-solo"] == pytest.approx(0.005 * 200_000.0 * (1.0 + 0.9) * 2.0)
    # the pooled `all` bucket is superseded by the song's own scope: worth nothing here
    assert v["Y-all"] == 0.0
    # ... and pays on a song whose scope is not known
    assert node_values_from_recipes(recipes, song_scope="", **kw)["Y-all"] == pytest.approx(
        0.005 * 200_000.0 * (1.0 + 0.9) * 2.0)
