"""The recon board optimiser's purchase graph must use the GAME's adjacency.

A hex-distance-1 rule would link the two hex diagonals `(+1,-1)/(-1,+1)` the game
never uses (246 phantom edges over the four layouts), and `cheapest_paths` would
shortcut through them and UNDER-COST every plan: R-061 `[1,-14]` would be reached
from an already-owned R-055 `[0,-13]` for 4 points instead of the real 8 through
R-058 `[1,-13]`. The client's `SkillTreeMapController.SetAdjacentNode` walks a
four-entry `Directions` array; that is the SAME rule `hbuilder.domain.skilltree`
implements and `test_skilltree_selection.py` pins to the game's degree histogram,
so `tree_optimise.adjacency()` delegates to that one rule.

These tests pin the recon side END-TO-END: the delegation itself (hermetic), the
acid case, and the concrete cost, so the recon tool and the solver can never
silently diverge on connectivity.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PROXY = _REPO / "tools" / "recon" / "proxy"
_TREE_OPTIMISE = _PROXY / "tree_optimise.py"
_DUMPS = _PROXY / "dumps"


def _load_tree_optimise():
    """Imported by path -- a recon script under `tools/`, not an installed
    package. Same shape as `test_tree_optimise_balance_source.py`."""
    spec = importlib.util.spec_from_file_location("tree_optimise", _TREE_OPTIMISE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tree_optimise"] = module
    spec.loader.exec_module(module)
    return module


def _skilltree_or_skip() -> dict:
    path = _DUMPS / "skilltree.json"
    if not path.exists():
        pytest.skip("dumps/ is gitignored; skilltree.json unavailable here")
    return json.loads(path.read_text(encoding="utf-8"))


# ==========================================================================
# 1. Delegation and orthogonality -- hermetic, no dumps needed
# ==========================================================================


def test_adjacency_is_the_four_neighbour_rule_not_hex_distance():
    """The two hex diagonals `(1,-1)/(-1,1)` are exactly what a hex-distance
    rule gets wrong; `(1,1)/(-1,-1)` never appear in either rule. A right-of, above and
    hex-diagonal probe pins that only the orthogonal pair links."""
    to = _load_tree_optimise()
    # o at origin; RIGHT and UP are orthogonal, HEXDIAG is the (1,-1) the game
    # never links, FARAWAY touches nothing.
    model = {
        "o": [0, 0],
        "RIGHT": [1, 0],
        "UP": [0, 1],
        "HEXDIAG": [1, -1],
        "FARAWAY": [5, 5],
    }
    adj = to.adjacency(model)
    assert adj["o"] == {"RIGHT", "UP"}
    assert "HEXDIAG" not in adj["o"]
    assert adj["FARAWAY"] == set()
    # symmetric, and typed as sets (the recon contract, not domain's frozenset)
    assert "o" in adj["RIGHT"] and isinstance(adj["o"], set)


def test_adjacency_matches_the_domain_rule_exactly():
    """`adjacency()` must be the domain `neighbours()` rule verbatim -- the whole
    point is that recon and the solver share ONE rule."""
    to = _load_tree_optimise()
    from hbuilder.domain.skilltree import neighbours

    model = {"o": [0, 0], "r": [1, 0], "u": [0, 1], "d": [1, -1], "x": [2, 0]}
    got = to.adjacency(model)
    want = {node: set(links) for node, links in neighbours(model).items()}
    assert got == want


# ==========================================================================
# 2. The acid case, on the real geometry -- skips without dumps
# ==========================================================================


def test_r061_links_only_to_r058_never_the_r055_phantom():
    """Canonical phantom: R-061 is hex-distance-1 from BOTH R-055 and R-058, but
    the game links only R-058 (delta (0,1)); R-055 sits on the dropped diagonal
    (delta (-1,1))."""
    to = _load_tree_optimise()
    document = _skilltree_or_skip()
    for model_name, model in document["models"].items():
        if not {"R-061", "R-058", "R-055"} <= set(model):
            continue
        adj = to.adjacency(model)
        assert "R-058" in adj["R-061"], model_name
        assert "R-055" not in adj["R-061"], model_name


def test_no_edge_lies_on_a_hex_diagonal_across_every_model():
    """The whole phantom class -- every phantom edge is a `(+1,-1)/(-1,+1)`
    diagonal. Not one such edge may appear in any layout."""
    to = _load_tree_optimise()
    document = _skilltree_or_skip()
    banned = {(1, -1), (-1, 1)}
    for model_name, model in document["models"].items():
        adj = to.adjacency(model)
        for node, links in adj.items():
            x, y = model[node]
            for other in links:
                ox, oy = model[other]
                assert (ox - x, oy - y) not in banned, (model_name, node, other)


# ==========================================================================
# 3. The concrete cost -- the number a plan actually charges
# ==========================================================================


def test_cheapest_path_to_r061_costs_the_real_eight_points():
    """chr-04003 owns R-055: under a hex-distance rule `cheapest_paths(R-061)`
    would be `('R-061',)` at 4 points; under the game's rule it is
    `('R-058','R-061')` at 8, because R-061 is not adjacent to an owned node.

    Pinned to a frozen board capture rather than the live board, because the
    witness needs a board that owns R-055."""
    to = _load_tree_optimise()
    document = _skilltree_or_skip()
    board_path = (Path(__file__).parent / "fixtures"
                  / "board_state_2026-08-09_pre_respec.json")
    board = json.loads(board_path.read_text(encoding="utf-8"))
    if "chr-04003" not in board or not board["chr-04003"].get("nodes"):
        pytest.skip("account has no captured board for chr-04003")

    member_model = {m["chr"]: m["model"] for m in document["members"]}
    model = document["models"][member_model["chr-04003"]]
    assert {"R-061", "R-058", "R-055"} <= set(model)

    prices = to._prices()
    owned = set(board["chr-04003"]["nodes"])
    assert "R-055" in owned, "the phantom only bites because R-055 is owned"

    graph = to.adjacency(model)
    paths = to.cheapest_paths(owned, graph, prices, max_nodes=9)
    path = paths.get("R-061")
    assert path == ("R-058", "R-061"), path
    assert sum(prices[n]["points"] for n in path) == 8
