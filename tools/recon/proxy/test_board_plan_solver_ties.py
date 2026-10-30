"""The exact board planner does not spend or reset on a tie.

Scenario (m0342, Lui leading as an outfit with no Lui card among the members): a
plan could buy B-001..B-008 + S-003 on Lui (a blue reaches only her own member
cards: none) and reset her yellows and reds (worth 0 on a song she does not sing)
while her points and those cubes are left over, so nothing is gained and each
reset costs 1,000 Hologold plus the node on every other song. Two guards prevent
it: the socket-first fill counts blue as paying only where it reaches a member
(`_socket_branch_pays`), and the MILP's objective has a term that breaks a
zero-value tie (`TIE_BREAK`).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for _p in (HERE, REPO / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

LEADER = "card-00036-5-uniq-0029-00"   # Takane Lui, the leader slot only
MEMBERS = ["card-00031-5-uniq-0025-00", "card-03001-5-uniq-0033-00", "card-00034-5-uniq-0027-00",
           "card-03003-5-uniq-0035-00", "card-03002-5-uniq-0034-00"]


@pytest.fixture(scope="module")
def plan():
    pytest.importorskip("highspy")
    if not (HERE / "dumps" / "board_state.json").exists():
        pytest.skip("dumps/ not on this checkout")
    import board_plan_solver

    solved = board_plan_solver.solve_team_board(
        scenario="account", song="m0342", pool=1, for_team=[LEADER, *MEMBERS],
        develop_bench=True)
    assert solved
    return solved


def test_an_outfit_leader_keeps_her_board_and_buys_no_blue(plan):
    import tree_optimise as to

    owned = set(to._owned_nodes().get("chr-00036") or ())
    lui = set(plan["chr-00036"]["nodes"])
    assert not {n for n in lui - owned if n[0] == "B"}, "a blue on a non-member pays nothing"
    assert "S-003" not in lui - owned, "S-003's shape holds only blues"
    assert owned <= lui, f"reset for nothing: {sorted(owned - lui)}"


def test_no_grouping_is_planned_past_its_900_cap(plan):
    """Risu, Moona and Iofi (ID Gen 1) each holding the whole G-008/011/021 chain
    is 1,350, of which the game counts 900 -- points better spent elsewhere (e.g.
    on Lv40 Iofi's blues). The grouping greens are one capped variable per
    grouping, bench boards included (Gen 5's Lamy/Botan chains)."""
    import json

    import tree_optimise as to

    nodes = json.loads((HERE / "dumps" / "skilltree.json").read_text(encoding="utf-8"))["nodes"]
    state = json.loads((HERE / "dumps" / "board_state.json").read_text(encoding="utf-8"))
    state.update(plan)
    total: dict = {}
    for cid, entry in state.items():
        for n in to.PER_CHARACTER_GREEN & set(entry.get("nodes") or ()):
            row = (nodes[n].get("chr") or {}).get(cid)
            if row:
                total[row["tgt"]] = total.get(row["tgt"], 0) + row["v"]
    over = {g: v for g, v in total.items() if v > 900}
    assert not over, over
    assert total.get("character_grouping-grp-indonesia-gen_1-always") == 900


def test_a_reset_that_must_earn_its_keep_happens_less(plan):
    """`reset_min_gain`: each owned node taken off must earn 0.2 % of the team's
    score (on this team: 64 resets when free, 24 at 0.2 %, score -2.1 %)."""
    import board_plan_solver
    import tree_optimise as to

    owned = to._owned_nodes()

    def resets(p):
        return sum(len(set(owned.get(c) or ()) - set(v["nodes"]) - {to.ROOT}) for c, v in p.items())
    priced = board_plan_solver.solve_team_board(
        scenario="account", song="m0342", pool=1, for_team=[LEADER, *MEMBERS],
        develop_bench=True, reset_min_gain=0.002)
    assert priced
    assert 0 < resets(priced) < resets(plan)


def test_no_refund_charges_only_new_buys_against_the_inventory():
    """`no_refund`: a reset returns nothing, so the wallet is the inventory -- and a
    node KEPT costs nothing again (it is not charged as if re-bought)."""
    import board_plan_solver
    import tree_optimise as to

    owned, prices = to._owned_nodes(), to._prices()
    inventory, _src = to.balance_from_disk()
    plan = board_plan_solver.solve_team_board(
        scenario="account", song="m0342", pool=1, for_team=[LEADER, *MEMBERS],
        develop_bench=True, no_refund=True)
    assert plan
    spent = {cur: 0 for cur in to.CURRENCIES}
    for c, v in plan.items():
        for n in set(v["nodes"]) - set(owned.get(c) or ()):
            for cur in to.CURRENCIES:
                spent[cur] += prices.get(n, {}).get(cur, 0)
    assert all(spent[cur] <= int(inventory.get(cur, 0)) for cur in spent), spent
    lui = set(plan["chr-00036"]["nodes"])
    assert set(owned.get("chr-00036") or ()) <= lui


def test_no_yellow_is_bought_past_the_song_cap(plan):
    """m0342 is a GAMERS group song: 4 singers at 40 permil a full board, of which
    the game counts 100 (`SONG_ALL_CAP_PERMIL`). Valued per node, a plan would buy
    Fubuki's and Okayu's missing yellows (23 points) to reach 160; capped, a new
    yellow is bought only while the sum is under 100 (the owned 110 are kept)."""
    import tree_optimise as to
    from board_plan_solver import SONG_YELLOW_CAP, _YELLOW_PERMIL

    import json
    nodes = json.loads((HERE / "dumps" / "skilltree.json").read_text(encoding="utf-8"))["nodes"]
    owned = to._owned_nodes()
    singers = ["chr-00006", "chr-00014", "chr-00016", "chr-00017"]

    def permil(cid, ns):
        out = 0
        for n in ns:
            if n[0] != "Y":
                continue
            eff = (nodes[n].get("chr") or {}).get(cid) or nodes[n].get("e") or {}
            m = _YELLOW_PERMIL.search(eff.get("t") or "")
            if n[0] == "Y" and m and eff.get("tr") == "group":
                out += int(m.group(1))
        return out
    kept = sum(permil(c, set(owned.get(c) or ()) & set(plan[c]["nodes"])) for c in singers)
    new = sum(permil(c, set(plan[c]["nodes"]) - set(owned.get(c) or ())) for c in singers)
    assert new == 0 or kept + new <= SONG_YELLOW_CAP, (kept, new)
