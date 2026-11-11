"""The account's player level gates the board nodes every production planner may buy: a node
whose `cd-player_level-N` unlock is above it is never in a plan (the search's plan cache, the
plain develop, the exact MILP), and an unknown level gates nothing."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

from support.builders import make_scoring_config
from test_fast_leaf import EFFECTS, SKILLTREE, TREE, _team

from hbuilder.domain.board_plan import PlanCache, cached_team_plan
from hbuilder.domain.board_refine import optimize_and_rerank
from hbuilder.domain.skilltree import ROOT_NODE, SkillTree
from hbuilder.solver import parallel_refine as pr

REFRESH = Path(__file__).resolve().parents[1] / "tools" / "refresh"
TEAM = _team(1, [2, 3, 4, 5, 6])
# every scoring node unlocks at player level 30; the socket spine is ungated
LOCKED = frozenset(n for n in TREE.nodes if n != ROOT_NODE and not n.startswith("S-"))
GATED = SkillTree(
    nodes={
        n: dataclasses.replace(node, player_level=30 if n in LOCKED else None)
        for n, node in TREE.nodes.items()
    },
    positions=TREE.positions,
    adjacency=TREE.adjacency,
)


def _planned_nodes(level):
    cfg = make_scoring_config()
    rows = optimize_and_rerank(
        [TEAM],
        cfg,
        tree_for=lambda c: GATED,
        budget_for=lambda c: 453,
        node_effect_ids=EFFECTS,
        skilltree=SKILLTREE,
        player_level=level,
    )
    plain = {x.node_id for b in rows[0].board.characters.values() for x in b.nodes}
    plan = cached_team_plan(
        TEAM.leader,
        TEAM.members,
        cache=PlanCache.from_team(TEAM, cfg, player_level=level),
        tree_for=lambda c: GATED,
        budget_for=lambda c: 453,
        node_effect_ids=EFFECTS,
        skilltree=SKILLTREE,
    )
    cached = {n for board in plan.per_character.values() for n in board.selected}
    return plain, cached


def test_a_player_below_the_unlock_level_gets_no_locked_node():
    plain, cached = _planned_nodes(10)
    assert not plain & LOCKED and not cached & LOCKED
    plain, cached = _planned_nodes(31)  # the unlock reached: the plan buys them again
    assert plain & LOCKED and cached & LOCKED
    unknown, _ = _planned_nodes(None)  # an unknown level gates nothing
    assert unknown == plain


def test_the_leaf_takes_the_level_from_the_account(monkeypatch):
    account = make_scoring_config()
    got = {}
    monkeypatch.setattr(
        PlanCache, "from_team", classmethod(lambda cls, team, cfg, **kw: got.update(kw) or object())
    )
    for level in (12, None):
        cfg = dataclasses.replace(account, account=SimpleNamespace(level=level))
        ctx = SimpleNamespace(
            plan_cache_seed=(TEAM.leader.id, tuple(m.id for m in TEAM.members)), cfg=cfg, plan_store=None
        )
        pr.new_plan_cache(ctx, {c.id: c for c in (TEAM.leader, *TEAM.members)})
        assert got["player_level"] == level == pr.player_level(cfg)


def test_the_exact_planner_drops_the_locked_nodes():
    if str(REFRESH) not in sys.path:
        sys.path.insert(0, str(REFRESH))
    import board_plan_solver as bps

    skilltree = {
        "nodes": {"R-001": {"req": "cd-player_level-30"}, "R-002": {"req": "cd-player_level-10"}, "S-001": {}}
    }

    def locked(scenario, player):
        oracle = SimpleNamespace(scenario=scenario, collection={"player": player}, skilltree=skilltree)
        return bps._locked_nodes(SimpleNamespace(oracle=oracle))

    assert locked("account", {"level": 20}) == {"R-001"}
    assert locked("account", {"level": 31}) == set()
    assert locked("account", {}) == set()  # level unknown
    assert locked("maxed", {"level": 5}) == set()  # a maxed scenario: the top level
