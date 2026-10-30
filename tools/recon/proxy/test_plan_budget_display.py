"""`_annotate_plan_budget` costs each board against the LEVEL's point budget.

Showing the CURRENT board's spend against the REAL level's points
(`spent_on_owned / granted`) regardless of the char_level toggle contradicts a
MAX-level plan: a level-50 holomem has 361 board points, not the ~80 its real level
grants, so a plan correctly costed at 361 would read as an impossible "84 to buy /
36 / 83 pt". `_annotate_plan_budget` sets, per planned character, the developed
board's own `point_cost` (the honest numerator) and the `point_budget` for the
chosen level: 361 at max, the real earned points at current (e.g. "248/361").

Reads only the gitignored master price/curve data, so it skips on a fresh checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROXY = Path(__file__).resolve().parent
_DUMPS = _PROXY / "dumps"


@pytest.mark.skipif(
    not (_DUMPS / "skilltree.json").exists()
    or not (_DUMPS / "master_SkillTreeNode.json").exists(),
    reason="dumps/ is gitignored account data",
)
def test_budget_is_361_at_max_and_real_at_current():
    sys.path.insert(0, str(_PROXY))
    import precompute_optimisations as pc
    import tree_optimise as to

    prices = to._prices()
    # A node that actually costs points, so point_cost is a real sum, not zero.
    node = next(n for n, p in prices.items() if p.get("points"))
    node_pts = prices[node]["points"]

    def _doc(granted):
        return {"characters": [{
            "chr": "chr-00012", "name": "Test",
            "points": {"granted": granted, "spent_on_owned": 36, "unspent": 47},
            "plan": {"optimised_nodes": [node], "to_buy": [node],
                     "to_sell": [], "slots": {}},
        }]}

    # MAX: the budget is the level-50 grant (361), NOT the real level's 83.
    at_max = _doc(83)
    pc._annotate_plan_budget(at_max, char_level="max")
    plan_max = at_max["characters"][0]["plan"]
    assert plan_max["point_budget"] == 361, (
        "a max-level plan must be costed against 361, not the real level's points"
    )
    assert plan_max["point_cost"] == node_pts, "point_cost is the board's own points"

    # CURRENT: the real earned points (granted).
    at_now = _doc(83)
    pc._annotate_plan_budget(at_now, char_level="current")
    assert at_now["characters"][0]["plan"]["point_budget"] == 83

    # A character with no plan is left untouched (no crash, no fields invented).
    bare = {"characters": [{"chr": "chr-00001", "points": {"granted": 40}}]}
    pc._annotate_plan_budget(bare, char_level="max")
    assert "plan" not in bare["characters"][0]
