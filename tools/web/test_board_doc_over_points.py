"""`hbweb.boardplan.over_points`: the boards of a maxed account's complete plan that cost
more points than a max-level holomem holds. The complete board keeps every node the account
already holds (a maxed account resets nothing), so the board panel warns about these.

    python -m pytest tools/web/test_board_doc_over_points.py
"""
from __future__ import annotations

from hbweb.boardplan import over_points


def _doc(*costs):
    chars = []
    for i, cost in enumerate(costs):
        c = {"chr": f"chr-{i:05d}", "name": f"Holomem {i}"}
        if cost is not None:
            c["plan"] = {"point_cost": cost, "point_budget": 361}
        chars.append(c)
    return {"characters": chars}


def test_only_the_boards_above_the_budget_are_listed():
    doc = _doc(361, 362, 120, None, 400)
    assert over_points(doc, 361) == [
        {"chr": "chr-00001", "name": "Holomem 1", "points": 362},
        {"chr": "chr-00004", "name": "Holomem 4", "points": 400},
    ]


def test_a_board_at_the_budget_or_without_a_plan_is_not_a_warning():
    assert over_points(_doc(361, None, 0), 361) == []
    assert over_points({"characters": []}, 361) == []
    assert over_points({}, 361) == []


def test_a_board_without_a_name_is_listed_by_its_id():
    doc = {"characters": [{"chr": "chr-00007", "plan": {"point_cost": 380}}]}
    assert over_points(doc, 361) == [{"chr": "chr-00007", "name": "chr-00007", "points": 380}]
