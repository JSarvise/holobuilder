"""Pricing a board plan's resets: 1,000 Hologold per node taken off a board.

From the client: the reset dialog multiplies the number of
nodes in the reset list by `Setting.skillTreeResetConsumptionGoldQuantity` (1000 in
`main_Setting.json`). The free root S-001 is never in a reset list.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_reset as br  # noqa: E402


def test_a_plan_equal_to_the_current_board_resets_nothing():
    now = {"chr-1": ["S-001", "G-001", "G-002"]}
    cost = br.reset_cost(now, {"chr-1": ["S-001", "G-001", "G-002"]}, gold_per_node=1000)
    assert cost["count"] == 0
    assert cost["gold"] == 0
    assert cost["per_character"] == {}


def test_dropping_a_three_node_branch_costs_three_thousand():
    now = {"chr-1": ["S-001", "R-001", "R-002", "R-003", "G-001"]}
    plan = {"chr-1": ["S-001", "G-001", "G-002"]}
    cost = br.reset_cost(now, plan, gold_per_node=1000)
    assert cost["count"] == 3
    assert cost["gold"] == 3000
    assert cost["per_character"] == {"chr-1": ["R-001", "R-002", "R-003"]}


def test_adding_nodes_is_free_and_the_root_is_never_reset():
    now = {"chr-1": ["S-001"], "chr-2": ["S-001", "B-001"]}
    plan = {"chr-1": ["G-001"], "chr-2": ["S-001", "B-001", "B-002"]}
    cost = br.reset_cost(now, plan, gold_per_node=1000)
    assert cost["count"] == 0


def test_a_board_the_plan_does_not_mention_keeps_what_it_has():
    # The plan only lists the boards it changed; an unlisted board is not reset.
    now = {"chr-1": ["S-001", "G-001"], "chr-2": ["S-001", "B-001"]}
    cost = br.reset_cost(now, {"chr-1": ["S-001", "G-001"]}, gold_per_node=1000)
    assert cost["count"] == 0


def test_the_price_comes_from_the_game_setting_when_it_is_on_disk():
    price = br.gold_per_node()
    assert price == 1000
