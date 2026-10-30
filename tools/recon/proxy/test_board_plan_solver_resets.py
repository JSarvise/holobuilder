"""The exact board planner honours a reset budget.

Resetting costs 1,000 Hologold per node (read from the game binary), so "how many nodes may
this plan take off the boards" is a real constraint, the same knob the community tool
holodori.best exposes as a reset limit. `max_resets=None` allows a free rebuild.
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

TEAM = ["card-04001-5-uniq-0042-00", "card-00023-5-uniq-0019-00", "card-04003-5-uniq-0044-00",
        "card-00031-5-uniq-0025-00", "card-03002-5-uniq-0034-00", "card-04001-5-uniq-0042-00"]


def _needs_data():
    pytest.importorskip("highspy")
    if not (HERE / "dumps" / "board_state.json").exists():
        pytest.skip("dumps/ not on this checkout")


@pytest.fixture(scope="module")
def solved():
    _needs_data()
    import board_plan_solver
    import tree_optimise as to

    owned = to._owned_nodes()
    out = {}
    for limit in (0, 5):
        out[limit] = board_plan_solver.solve_team_board(
            scenario="account", song="m0334", pool=1, for_team=TEAM,
            develop_bench=False, max_resets=limit)
    return owned, out


def test_a_zero_reset_budget_only_adds_nodes(solved):
    import board_reset

    owned, out = solved
    plan = out[0]
    assert plan
    cost = board_reset.reset_cost(owned, {c: v["nodes"] for c, v in plan.items()})
    assert cost["count"] == 0, cost["per_character"]


def test_a_small_reset_budget_is_never_exceeded(solved):
    import board_reset

    owned, out = solved
    plan = out[5]
    assert plan
    cost = board_reset.reset_cost(owned, {c: v["nodes"] for c, v in plan.items()})
    assert cost["count"] <= 5, cost["per_character"]


# A real account's team: Lui leading Nene, Botan, Moona, Zeta, Kaela; Kobo on the bench
BENCH_TEAM = ["card-00036-5-uniq-0078-00", "card-00031-5-uniq-0025-00", "card-00032-4-cmmn-0000-00",
              "card-03002-5-uniq-0034-00", "card-03007-5-uniq-0039-00", "card-03008-5-uniq-0095-00"]
KOBO = "chr-03009"


@pytest.mark.parametrize("gain", [0.0001, 0.0])
def test_a_bench_blue_branch_pays_for_the_team_before_the_team_resets(monkeypatch, gain):
    """A bench board's blues reach no team card, so they are material the plan may take
    back. Kobo holding his whole blue branch (B-009..B-031 bought) must not plan a WORSE
    team than Kobo with those 23 blues already reset: the refunds are the same cubes, only
    a reset away. If the Act-level re-solves froze every bench board, the plan would reset
    the team's nodes instead (on a real account: 568,152 vs 570,941 after resetting Kobo by
    hand)."""
    _needs_data()
    import board_plan_solver
    import tree_optimise as to

    prices = to._prices()
    owned_now, bal_now, unspent_now = to._owned_nodes(), to.balance_from_disk, to._character_points
    if KOBO not in owned_now:
        pytest.skip("Kobo's board is not on this account")
    branch = [f"B-{i:03d}" for i in range(9, 32)]
    back = [n for n in branch if n not in owned_now[KOBO]]
    off = [n for n in branch if n in owned_now[KOBO]]

    def state(holding: bool):
        """Kobo with the whole branch (`holding`) or with none of it, the same account."""
        add, drop = (back, []) if holding else ([], off)
        cubes = {}
        for n, sign in [(n, -1) for n in add] + [(n, +1) for n in drop]:
            for k, q in prices[n].items():
                if k != "points":
                    cubes[k] = cubes.get(k, 0) + sign * q
        pts = sum(prices[n]["points"] for n in add) - sum(prices[n]["points"] for n in drop)

        def owned():
            o = {c: list(v) for c, v in owned_now.items()}
            o[KOBO] = sorted((set(o[KOBO]) | set(add)) - set(drop))
            return o

        def balance(*_a, **_k):
            b, src = bal_now()
            return {k: v + cubes.get(k, 0) for k, v in b.items()}, src

        def unspent():
            u = dict(unspent_now())
            u[KOBO] = u.get(KOBO, 0) - pts
            return u
        return owned, balance, unspent

    scores = {}
    for holding in (True, False):
        owned, balance, unspent = state(holding)
        monkeypatch.setattr(to, "_owned_nodes", owned)
        monkeypatch.setattr(to, "balance_from_disk", balance)
        monkeypatch.setattr(to, "_character_points", unspent)
        plan = board_plan_solver.solve_team_board(
            scenario="account", song=None, pool=1, for_team=BENCH_TEAM,
            develop_bench=True, reset_min_gain=gain)
        assert plan
        oracle = to.Oracle(pool_size=1, song=None, charts_path=None, scenario="account",
                           uptime_model="finite")
        o = owned()
        full = {c: sorted(set((plan.get(c) or {}).get("nodes") or o.get(c) or []) | {to.ROOT})
                for c in o}
        scores[holding] = oracle.score_team(BENCH_TEAM[0], tuple(BENCH_TEAM[1:]),
                                            oracle.state_from(full))
    assert scores[True] >= scores[False] * (1 - 1e-6), scores
