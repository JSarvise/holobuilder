"""The board's geometry and prices (`domain/skilltree.py`): 4-neighbour
adjacency, the master point costs, the account-level gate and the join of the
node table with one model's geometry.

The rules a chosen board must satisfy, and their tests, are in
`tests/support/board_rules.py` and `tests/test_board_rules.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support.board_rules import MASTER_ROW_DATA_KEY

from hbuilder.domain.skilltree import (
    TOTAL_BOARD_POINTS,
    neighbours,
    player_level_requirement,
    tree_from_mapping,
)

_DUMPS = Path(__file__).resolve().parents[1] / "tools" / "recon" / "proxy" / "dumps"


def test_adjacency_is_orthogonal_and_excludes_diagonals():
    """The alternative fails: under 8-neighbour the three outer sockets stop
    being cut vertices, which contradicts the observed boards. So the diagonal
    exclusion is measured, not stylistic."""
    adj = neighbours({"o": [0, 0], "right": [1, 0], "up": [0, 1], "diag": [1, 1]})
    assert adj["o"] == frozenset({"right", "up"})
    assert "diag" not in adj["o"]
    # ... and the relation is symmetric.
    assert "o" in adj["right"] and "o" in adj["up"]


def test_the_real_node_costs_sum_to_the_measured_board_total():
    """453 over the node groups. Also checked here: field 9 does not vary across a group's rows, so a node's price is
    character-independent even for the 15 multi-row groups."""
    path = _DUMPS / "master_SkillTreeNode.json"
    if not path.exists():
        pytest.skip("dumps/ is gitignored; master_SkillTreeNode.json unavailable")

    rows = json.loads(path.read_text(encoding="utf-8"))
    by_group: dict[str, set[int]] = {}
    for row in rows:
        data = row.get(MASTER_ROW_DATA_KEY) or {}
        group = (data.get("1") or [None])[0]
        by_group.setdefault(group, set()).add(int((data.get("9") or [0])[0]))

    varying = {g: c for g, c in by_group.items() if len(c) > 1}
    assert varying == {}, f"cost varies within a group: {varying}"
    assert sum(next(iter(c)) for c in by_group.values()) == TOTAL_BOARD_POINTS


def test_the_gate_is_parsed_and_absence_is_not_zero():
    assert player_level_requirement("cd-player_level-30") == 30
    assert player_level_requirement(None) is None
    assert player_level_requirement("") is None
    # Some nodes are genuinely ungated; that is not "gated at 0".
    assert player_level_requirement("cd-something-else") is None


def test_tree_from_mapping_joins_the_two_sources():
    """`skilltree.json` has the node table and the geometry; the COSTS are
    master data. The join is explicit so the seam stays visible."""
    built = tree_from_mapping(
        {
            "S-001": {"br": "S"},
            "X": {"br": "G", "req": "cd-player_level-15", "e": {"d": "Grants All Stats UP.", "icon": "ic"}},
        },
        {"S-001": [0, 0], "X": [1, 0]},
        {"S-001": 0, "X": 7},
    )
    assert built.nodes["X"].cost == 7
    assert built.nodes["X"].player_level == 15
    assert built.nodes["X"].description == "Grants All Stats UP."
    assert built.adjacency["S-001"] == frozenset({"X"})
