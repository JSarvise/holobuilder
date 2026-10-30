"""`board_plan_solver._act_tables`: a member board's ACTIVE uptime as (cooldown count,
rate sum) levels, scored exactly on the row and column through its current level.

The staircase: 3 Act.CD nodes of 40 permil and 10 Act.Prob nodes of 20-60 make
~4 x 30 levels a board, and the score is not monotone in the cooldown count (on one
chart a member scores better at 2 than at 3), so the MILP picks ONE level per board
from this table instead of pricing nodes one by one.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for _p in (HERE, REPO / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import board_plan_solver as bps  # noqa: E402

KIND = {"B-013": "Act.CD", "B-020": "Act.CD", "B-031": "Act.CD",
        "B-007": "Act.Prob", "B-010": "Act.Prob", "B-012": "Act.Prob"}
VALUE = {"B-013": 40, "B-020": 40, "B-031": 40, "B-007": 60, "B-010": 20, "B-012": 30}
GRAPH = {"S-001": (), "B-001": (), **{n: () for n in KIND}}


def _sums(nodes):
    return (sum(1 for n in nodes if KIND.get(n) == "Act.CD"),
            sum(VALUE[n] for n in nodes if KIND.get(n) == "Act.Prob"))


def test_levels_cover_every_reachable_sum_and_are_exact_when_separable():
    calls = []

    def score(state):
        k, p = _sums(state["chr-1"])
        calls.append((k, p))
        return 1000.0 * (k == 2) - 7.0 * k * k + 3.0 * p     # non-monotone in k

    state = {"chr-1": {"S-001", "B-001", "B-013", "B-010"}, "chr-2": {"S-001"}}
    table = bps._act_tables(score, state, ["chr-1"], {"chr-1": GRAPH}, KIND, VALUE)["chr-1"]
    sums = {0, 20, 30, 50, 60, 80, 90, 110}
    assert set(table) == {(k, p) for k in range(4) for p in sums}
    for (k, p), v in table.items():
        assert v == 1000.0 * (k == 2) - 7.0 * k * k + 3.0 * p
    assert max(table, key=table.get) == (2, 110)
    # the row through the current rate sum and the column through the current count
    assert len(calls) <= 4 + len(sums)


def test_a_board_with_no_act_node_has_no_table():
    out = bps._act_tables(lambda s: 0.0, {"chr-1": {"S-001"}}, ["chr-1"],
                          {"chr-1": {"S-001": (), "B-001": ()}}, KIND, VALUE)
    assert out == {}
