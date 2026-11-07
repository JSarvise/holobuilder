"""The board planner values a board with the SAME per-song score the rows report.

`tree_optimise.Oracle.score_team` is what the board MILP's node values and its Act tables are
made of (`_branch_values`, `board_plan_solver._act_tables`); `song_rank.score_team_on_chart` is
the number every search row and detail page reports. If the two differ, the MILP optimises a
different function than the one the plan is judged by: a singer-led team's plan can then lose to
another plan on the reported score although every MILP solved to optimality (measured on the
golden: Matsuri's board in the Okayu team on m0342, 2,173,833 against 2,174,105).

The cause this pins: the Oracle scored the song on the modelled stat pool, while the reported
score uses the deck screen's Overall Power (`overall_power_for`: the board's stat rates under one
ceiling with the singer's, and the Member Power-Up paid on that base).

Needs the account's gitignored dumps; skips without them.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path

import pytest

from hbuilder.errors import MissingInput

_REPO = Path(__file__).resolve().parents[1]
_TREE_OPTIMISE = _REPO / "tools" / "refresh" / "tree_optimise.py"
# the golden's singer-led case: leader + 5 members on m0342 (Matsuri, chr-00007, is a member)
SONG = "m0342"
TEAM = ["card-00016-4-cmmn-0000-00", "card-00007-3-nrml-0008-00", "card-03007-5-uniq-0039-00",
        "card-04015-5-uniq-0054-00", "card-06005-5-uniq-0061-00", "card-00031-5-uniq-0025-00"]


def _load_tree_optimise():
    spec = importlib.util.spec_from_file_location("tree_optimise", _TREE_OPTIMISE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tree_optimise"] = module
    spec.loader.exec_module(module)
    return module


try:
    _TREE = _load_tree_optimise()
    _ORACLE = _TREE.Oracle(pool_size=1, quiet=True, song=SONG, uptime_model="finite")
except (MissingInput, FileNotFoundError) as exc:  # gitignored account data missing
    _TREE = _ORACLE = None
    _WHY = str(exc)
else:
    _WHY = ""

pytestmark = pytest.mark.skipif(_ORACLE is None, reason=f"needs the account's dumps: {_WHY}")


def _reported(state) -> float:
    from hbuilder.domain.song_rank import score_team_on_chart
    from hbuilder.domain.team import Team

    by_id = _ORACLE._by_id
    team = Team(leader=by_id[TEAM[0]], members=tuple(by_id[c] for c in TEAM[1:]))
    cfg = dataclasses.replace(_ORACLE.base_cfg, board=_ORACLE.board_for(state))
    return score_team_on_chart(team, _ORACLE._chart, cfg).total_score


@pytest.mark.parametrize("matsuri", [None, ["S-001", "B-013"], ["S-001", "B-030", "Y-005"]])
def test_the_planner_scores_a_board_as_the_rows_report_it(matsuri):
    nodes = {} if matsuri is None else {"chr-00007": matsuri}
    state = _ORACLE.state_from(nodes)
    assert _ORACLE.score_team(TEAM[0], TEAM[1:], state) == pytest.approx(_reported(state), rel=1e-12)


def test_the_modelled_switch_scores_the_song_as_the_planner_did_before():
    from hbuilder.domain.song import score_song

    state = _ORACLE.state_from({})
    with _TREE.modelled_song_power():
        modelled = _ORACLE.score_team(TEAM[0], TEAM[1:], state)
    by_id = _ORACLE._by_id
    cfg = dataclasses.replace(_ORACLE.base_cfg, board=_ORACLE.board_for(state))
    old = score_song(tuple(by_id[c] for c in TEAM[1:]), (by_id[TEAM[0]],), _ORACLE._chart, cfg)
    assert modelled == old[0].total_score != _reported(state)


def test_in_board_values_are_the_nodes_marginals_inside_that_board():
    """`_branch_values(context=...)`: a node the board lacks is valued by adding it, one it has
    by removing it -- here on the leader's board (red nodes)."""
    graphs = _TREE._graphs(_ORACLE.skilltree)
    prices = _TREE._prices()
    leader_chr = "chr-" + TEAM[0].split("-")[1]
    context = {c: list(v["nodes"]) for c, v in _ORACLE.state_from({}).items() if c in graphs}
    values = _TREE._branch_values(_ORACLE, graphs, prices, TEAM[0], TEAM[1:], context=context)

    def score(nodes):
        return _ORACLE.score_team(TEAM[0], TEAM[1:], _ORACLE.state_from(
            {**context, leader_chr: sorted(nodes)}))
    full = score(set(context[leader_chr]) | {"S-001"})
    reds = sorted(n for n in values if n.startswith("R-"))[:3]
    assert reds
    for n in reds:
        have = set(context[leader_chr]) | {"S-001"}
        expect = full - score(have - {n}) if n in have else score(have | {n}) - full
        assert values[n] == expect
