"""A score must not depend on how this process happens to hash strings.

Python gives every process its own string hashes (PYTHONHASHSEED), so a set of node ids is
walked in another order in each one. Anything summed or listed in that order moves with it:
a board's rates summed in another order end on another last bit, and one point of Overall
Power after the ceiling. The search runs in worker processes, each with its own seed, so
such a score would also depend on which worker computed it.

The check: the same developed boards and scores, computed in fresh interpreters started with
different hash seeds, are the same floats bit for bit. Partial boards (a budget that buys
some of a branch) are the sensitive ones: their rates are sums of unequal terms.

Uses the real dumps (skilltree + master + charts, gitignored) and SKIPS when they are absent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from hbweb import config

SEEDS = ("0", "1", "2")

# What each interpreter computes: two teams at two board budgets on a few charts, each
# chart on the board developed for its singer context -- the exact leaf of the search.
PROBE = r"""
import dataclasses, json, sys
sys.path[:0] = json.loads(sys.argv[1])
from hbweb import config
from hbuilder.inputs import apply_collection, load_inputs
from hbuilder.domain.board_plan import node_effect_ids_from_master
from hbuilder.domain.song import load_charts
from hbuilder.domain.team import Team
from hbuilder.solver import parallel_refine as pr

skilltree = config.read_json(config.SKILLTREE_PATH)
node_master = config.read_json(config.NODE_MASTER_PATH)
library = load_charts(str(config.CHARTS_PATH))
cards, owned, cfg, _ = load_inputs(config.CARDS_PATH, config.OWNED_PATH)
cards, basis = apply_collection(cards, None, owned_ids=[c.id for c in owned], maxed=True)
if basis.bonuses is not None:
    cfg = dataclasses.replace(cfg, account=basis.bonuses)
five = [c for c in cards if c.rarity.startswith("5")]
teams = []
for k in range(2):
    leader, members = five[k * 7], []
    for c in five[k * 7 + 1:]:
        if c.member != leader.member and c.member not in {m.member for m in members}:
            members.append(c)
        if len(members) == 5:
            break
    teams.append(Team(leader=leader, members=tuple(members)))
charts = tuple(library.get(m) for m in sorted(library.music_ids)[:4])
out = {}
for budget in (120, 240):
    ctx = pr.RefineContext(
        cards=tuple(cards), cfg=dataclasses.replace(cfg, board=None), charts=charts,
        skilltree=skilltree, node_master=node_master,
        node_effect_ids=node_effect_ids_from_master(node_master), connect_cards=None,
        bench_characters={"(no captured board)": []},
        name_by_chr={"chr-" + c.id.split("-")[1]: c.member for c in cards},
        flat_budget=budget, granted={}, board_per_chart=True, use_plan_cache=True,
        plan_cache_seed=(teams[0].leader.id, tuple(m.id for m in teams[0].members)))
    with pr.RefinePool(ctx, workers=1) as pool:
        cache = pr.new_plan_cache(ctx, pool._by_id)
        for k, team in enumerate(teams):
            ids = (team.leader.id, tuple(m.id for m in team.members))
            scores = pr.evaluate(ctx, pool._by_id, pool._tree_for, pool._budget_for, ids,
                                 plan_cache=cache, per_chart=True)
            counted = pr.evaluate(ctx, pool._by_id, pool._tree_for, pool._budget_for,
                                  (*ids, None, pr.COUNTS), plan_cache=cache, per_chart=True)
            board = pr.develop_team(ctx, team, pool._tree_for, pool._budget_for, charts[0],
                                    plan_cache=cache).board
            out[f"{budget}/{k}"] = {
                "scores": [float(s).hex() for s in scores],
                "counted": [float(s).hex() for s in counted],
                "uptime": {m.member: [x.hex() for x in board.uptime_for(m.member)]
                           for m in team.members},
                "power": float(board.power_bonus_for(tuple(team.members), team.leader.member)).hex(),
                "nodes": {m.member: [n.node_id for n in board.characters[m.member].nodes]
                          for m in team.members if m.member in board.characters},
            }
print(json.dumps(out, sort_keys=True))
"""


def _start(seed: str) -> subprocess.Popen:
    env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONUTF8="1")
    return subprocess.Popen([sys.executable, "-c", PROBE, json.dumps(sys.path)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _result(proc: subprocess.Popen) -> dict:
    out, err = proc.communicate(timeout=600)
    assert proc.returncode == 0, err[-2000:]
    return json.loads(out.strip().splitlines()[-1])


def test_the_leaf_gives_the_same_floats_whatever_the_hash_seed():
    if (not config.read_json(config.SKILLTREE_PATH) or not config.read_json(config.NODE_MASTER_PATH)
            or not config.CHARTS_PATH.exists()):
        pytest.skip("skilltree/master/charts dumps absent (gitignored)")
    started = [(seed, _start(seed)) for seed in SEEDS]        # side by side
    runs = {seed: _result(proc) for seed, proc in started}
    first = runs[SEEDS[0]]
    assert first and all(v["scores"] for v in first.values())
    for seed in SEEDS[1:]:
        for key, value in first.items():
            assert runs[seed][key] == value, f"hash seed {seed} against {SEEDS[0]}, {key}"
