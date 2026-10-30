"""`fill_boards` must NEVER spend more points than a character's budget.

`_spend` does not mutate — it returns fresh `dict(wallet)` / `dict(points)`
copies — so every purchase site in `fill_boards` must use its return value, or
the running budget is never decremented and `_affordable` keeps seeing the full
original budget. Overspend hardly shows on a maxed account (361 points is
about a whole tree) and bites at real, low character levels.

This unit checks the budget without the ~1-2 minute end-to-end beam: it drives
`fill_boards` alone, with a positive value on every node (so the greedy WANTS to
buy everywhere) and a deliberately small per-character point budget, then asserts
no returned board exceeds its budget. Reads only gitignored master data, so it
skips on a fresh checkout.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PROXY = Path(__file__).resolve().parent
_MODULE = _PROXY / "tree_optimise.py"
_DUMPS = _PROXY / "dumps"


def _load():
    spec = importlib.util.spec_from_file_location("tree_optimise", _MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tree_optimise"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    not (_DUMPS / "skilltree.json").exists()
    or not (_DUMPS / "master_SkillTreeNode.json").exists(),
    reason="dumps/ is gitignored account data",
)
def test_fill_never_exceeds_a_characters_point_budget():
    to = _load()
    prices = to._prices()
    graphs = {cid: g for cid, g in to._graphs(to._load(_DUMPS / "skilltree.json")).items() if g}
    assert len(graphs) >= 6, "need a few real character graphs to exercise the fill"

    # A tiny, uniform point budget on EVERY character: each board must stop at its cap.
    BUDGET = 8
    points = {cid: BUDGET for cid in graphs}
    # Materials are effectively unlimited, so ONLY the point budget can bind —
    # isolating the currency under test.
    wallet = {item: 10 ** 9 for item in to.CURRENCIES}
    # Every node is worth something, so the greedy value loop wants to buy as much
    # as it can afford on every board — precisely the pressure the budget must cap.
    values = {node: 1.0 for node in prices}

    cids = sorted(graphs)
    leader_chr, member_chrs = cids[0], cids[1:6]

    state, _wallet, out_points, bought = to.fill_boards(
        graphs, prices, {}, wallet, points, values,
        leader_chr, member_chrs, song_mode=False, score_fn=None,
    )

    assert bought > 0, "the fill bought nothing, so it never exercised the budget"

    over = []
    spent_any = 0
    for cid, nodes in state.items():
        cost = sum(prices.get(n, {}).get("points", 0) for n in nodes)
        spent_any = max(spent_any, cost)
        if cost > BUDGET:
            over.append((cid, cost))
    assert not over, (
        f"fill_boards overspent the {BUDGET}-point budget on {len(over)} board(s): "
        f"{over[:5]} -- the _spend return value is being discarded again"
    )
    # Not vacuous: at least one board actually developed toward the cap.
    assert spent_any > 0, "no board spent any points; the test proved nothing"

    # The returned points ledger must AGREE: no character left with negative
    # points (which _spend would produce only if it were actually applied).
    assert all(p >= 0 for p in out_points.values()), (
        "a character's point ledger went negative -- the budget accounting is wrong"
    )
