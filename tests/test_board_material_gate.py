"""With the account's materials, a node priced in an item the account holds none of is not
bought by any plan (R-064 costs Red Star Cubes, a material the game added after the eight
cubes and hexas). The gate is the player-level `req` every planner already reads."""

from __future__ import annotations

from hbuilder.domain.board_node_values import (
    UNREACHABLE_PLAYER_LEVEL,
    gate_unheld_materials,
    node_items_from_master,
)
from hbuilder.domain.skilltree import player_level_requirement

# field 1000 as the master export decodes it: a repeated `Consumption` per row
MASTER = [
    {
        "group_id": "R-064",
        "data": {
            "1000": [
                "\b\u0003\u0012\u0018item-skill_tree_leader-3\u0018\n",
                "\b\u0003\u0012\u0018item-skill_tree_leader-2\u0018d",
            ]
        },
    },
    {
        "group_id": "R-044",
        "data": {
            "1000": [
                "\b\u0003\u0012\u0018item-skill_tree_leader-1\u0018d",
                "\b\u0003\u0012\u0018item-skill_tree_leader-2\u0018\u0019",
            ]
        },
    },
    {"group_id": "S-001", "data": {}},
]
TREE = {
    "nodes": {
        "R-064": {"br": "R", "req": "cd-player_level-25"},
        "R-044": {"br": "R", "req": "cd-player_level-10"},
        "S-001": {"br": "S"},
    },
    "models": {"m": {}},
}
WALLET = {
    "item-skill_tree_leader-1": 2840,
    "item-skill_tree_leader-2": 997,
    "item-skill_tree_leader-3": 0,
}


def test_the_items_a_node_costs_are_read_from_its_price():
    items = node_items_from_master(MASTER)
    assert items["R-064"] == {"item-skill_tree_leader-3", "item-skill_tree_leader-2"}
    assert items["R-044"] == {"item-skill_tree_leader-1", "item-skill_tree_leader-2"}
    assert "S-001" not in items


def test_a_node_priced_in_an_unheld_item_is_gated_and_nothing_else_moves():
    gated, ids = gate_unheld_materials(TREE, MASTER, WALLET)
    assert ids == ("R-064",)
    assert player_level_requirement(gated["nodes"]["R-064"]["req"]) == UNREACHABLE_PLAYER_LEVEL
    assert gated["nodes"]["R-044"] == TREE["nodes"]["R-044"]
    assert gated["models"] is TREE["models"]
    assert TREE["nodes"]["R-064"]["req"] == "cd-player_level-25"  # the input is not written


def test_one_held_unit_or_an_unknown_item_gates_nothing():
    """Held at all: the materials re-plan decides whether it is enough. Not in the wallet:
    unknown is not zero."""
    assert gate_unheld_materials(TREE, MASTER, {**WALLET, "item-skill_tree_leader-3": 1})[1] == ()
    unknown = {k: v for k, v in WALLET.items() if k != "item-skill_tree_leader-3"}
    assert gate_unheld_materials(TREE, MASTER, unknown)[1] == ()
