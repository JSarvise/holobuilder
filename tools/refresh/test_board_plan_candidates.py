"""The board planner's three candidate plans: the modelled valuation (the plan as it was before
the planner scored songs on the Overall Power) with the whole Act search, then the bare-board
values and the values measured inside that first plan, each with the member boards held at the
first plan's Act levels; the scorer picks, and a tie keeps the earliest."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_plan_solver as bps  # noqa: E402
import tree_optimise as to  # noqa: E402

KW = dict(scenario="account", song="m1", pool=1, for_team=["L"] + [f"M{i}" for i in range(5)],
          char_level="current", resources="inventory", develop_bench=True, include_sockets=False,
          uptime_model="finite", max_resets=None, branches="GBRY", targets=None, no_refund=False,
          all_invited=False, reset_min_gain=0.0, act_search=True, song_pool=None,
          should_stop=None, statuses=None)
LEVELS = {"chr-2": (1, 30)}


def _fake(monkeypatch, scores: dict, seen: list):
    def solve(**kw):
        name = kw["_valuation"]
        seen.append((name, to._VALUATION.__dict__.get("modelled", False), kw.get("_context"),
                     kw.get("_levels")))
        if kw.get("_levels_out") is not None:
            kw["_levels_out"].update(LEVELS)
        return {"chr-1": {"nodes": [name], "slots": {}}}
    monkeypatch.setattr(bps, "solve_team_board", solve)
    monkeypatch.setattr(bps, "_plan_score", lambda kw, board: scores[board["chr-1"]["nodes"][0]])


def test_the_scorer_picks_and_both_others_start_from_the_modelled_plan(monkeypatch):
    seen: list = []
    _fake(monkeypatch, {"modelled": 100.0, "bare": 99.0, "in_board": 101.0}, seen)
    board = bps._best_of_candidates(dict(KW))
    assert board["chr-1"]["nodes"] == ["in_board"]
    assert [s[0] for s in seen] == ["modelled", "bare", "in_board"]
    assert seen[0][1] is True and seen[1][1] is False            # only (1) on the old valuation
    assert seen[0][3] is None                                     # (1) runs the whole Act search
    assert seen[1][3] == LEVELS and seen[2][3] == LEVELS          # (2), (3) at (1)'s Act levels
    assert seen[2][2] == {"chr-1": ["modelled"]}                  # (3) valued inside (1)


def test_off_a_song_the_in_board_candidate_runs_its_own_act_search(monkeypatch):
    seen: list = []
    _fake(monkeypatch, {"modelled": 100.0, "bare": 99.0, "in_board": 101.0}, seen)
    bps._best_of_candidates(dict(KW, song=None))
    # no bare candidate: off a song its valuation is the modelled one
    assert [s[0] for s in seen] == ["modelled", "in_board"]
    assert seen[-1][3] is None and seen[-1][2] == {"chr-1": ["modelled"]}


def test_a_candidate_equal_to_an_earlier_plan_is_not_judged_again(monkeypatch):
    def solve(**kw):
        if kw.get("_levels_out") is not None:
            kw["_levels_out"].update(LEVELS)
        same = kw["_valuation"] in ("modelled", "in_board")
        return {"chr-1": {"nodes": ["x" if same else kw["_valuation"]], "slots": {}}}
    monkeypatch.setattr(bps, "solve_team_board", solve)
    judged: list = []
    board = bps._best_of_candidates(dict(KW), judge=lambda b: judged.append(b) or 1.0)
    assert len(judged) == 2 and board["chr-1"]["nodes"] == ["x"]


def test_in_parallel_the_same_candidates_are_solved_and_judged_in_order(monkeypatch):
    for parallel in (False, True):
        seen: list = []
        _fake(monkeypatch, {"modelled": 100.0, "bare": 101.0, "in_board": 101.0}, seen)
        judged: list = []
        board = bps._best_of_candidates(
            dict(KW), judge=lambda b: judged.append(b["chr-1"]["nodes"][0]) or 101.0,
            parallel=parallel)
        assert sorted(s[0] for s in seen) == ["bare", "in_board", "modelled"]
        assert judged == ["modelled", "bare", "in_board"]
        assert board["chr-1"]["nodes"] == ["modelled"]           # a tie keeps the earliest


def test_first_alone_then_each_candidate_on_its_own_are_the_same_calls(monkeypatch):
    seen: list = []
    _fake(monkeypatch, {"modelled": 100.0, "bare": 99.0, "in_board": 101.0}, seen)
    levels: dict = {}
    first = bps._best_of_candidates(dict(KW), first_only=True, levels_out=levels)
    assert [s[0] for s in seen] == ["modelled"] and levels == LEVELS
    seen.clear()
    bps._best_of_candidates(dict(KW))
    whole_call = seen[1:]                                         # (2) and (3) inside one plan
    seen.clear()
    for name in bps.other_candidates(KW["song"]):
        bps.solve_candidate(name, first, levels, **{k: v for k, v in KW.items()
                                                    if k != "include_sockets"})
    assert seen == whole_call
    assert bps.other_candidates(None) == ("in_board",)


def test_a_tie_keeps_the_modelled_plan_so_nothing_scores_below_it(monkeypatch):
    _fake(monkeypatch, {"modelled": 100.0, "bare": 100.0, "in_board": 100.0}, [])
    assert bps._best_of_candidates(dict(KW))["chr-1"]["nodes"] == ["modelled"]
    _fake(monkeypatch, {"modelled": 100.0, "bare": 90.0, "in_board": 95.0}, [])
    assert bps._best_of_candidates(dict(KW))["chr-1"]["nodes"] == ["modelled"]


def test_the_modelled_switch_is_restored_after_use():
    assert not getattr(to._VALUATION, "modelled", False)
    with to.modelled_song_power():
        assert to._VALUATION.modelled is True
    assert to._VALUATION.modelled is False


def test_a_judge_rates_the_candidates_instead_of_the_oracle(monkeypatch):
    seen: list = []
    _fake(monkeypatch, {"modelled": 100.0, "bare": 99.0, "in_board": 101.0}, seen)
    judged = {"modelled": 5.0, "bare": 7.0, "in_board": 6.0}
    board = bps._best_of_candidates(dict(KW), judge=lambda b: judged[b["chr-1"]["nodes"][0]])
    assert board["chr-1"]["nodes"] == ["bare"]


# the golden's singer-led event team on m0342 (needs the account's gitignored dumps)
TEAM = ["card-00016-4-cmmn-0000-00", "card-00007-3-nrml-0008-00", "card-03007-5-uniq-0039-00",
        "card-04015-5-uniq-0054-00", "card-06005-5-uniq-0061-00", "card-00031-5-uniq-0025-00"]


def test_a_held_act_level_is_one_milp_at_that_level():
    kw = dict(KW, song="m0342", for_team=TEAM)
    levels: dict = {}
    with to.modelled_song_power():
        first = bps.solve_team_board(**kw, _valuation="modelled", _levels_out=levels)
    if first is None:
        pytest.skip("the account's dumps are not here")
    assert levels
    stages: dict = {}
    again = bps.solve_team_board(**kw, _valuation="bare", _levels=levels, _stages=stages)
    assert again is not None and stages["milps"] == 1         # one MILP, no Act search
    nodes = next(iter(bps._ACCOUNT.values()))[0].skilltree["nodes"]
    kind = {n: (m.get("e") or {}).get("t") for n, m in nodes.items()}
    value = {n: int((m.get("e") or {}).get("v") or 0) for n, m in nodes.items()}
    for chr_id, (cds, rate) in levels.items():
        have = set(again.get(chr_id, {}).get("nodes") or ())
        # the fill may add Act nodes on leftover points; never fewer than the held level
        assert sum(1 for n in have if kind.get(n) == "Act.CD") >= cds
        assert sum(value[n] for n in have if kind.get(n) == "Act.Prob") >= rate
