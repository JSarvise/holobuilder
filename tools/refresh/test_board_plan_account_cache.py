"""The planner's per-process Oracles (`board_plan_solver._ACCOUNT`) when plans overlap.

The server plans several row details at once (a player opens one team while another's detail
is still computing), and a Highlight detail plans for no song and then for its pool. With one
Oracle held per process, the second plan evicted the first's, the first's candidate plans
could not be scored, and the page read that as "the wallet cannot fund a plan".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import board_plan_solver as bps  # noqa: E402
import tree_optimise as to  # noqa: E402
from test_board_plan_candidates import KW  # noqa: E402


class FakeOracle:
    built: list = []

    def __init__(self, *, song=None, pool_songs=None, **_kw):
        self.song, self.pool_songs = song, pool_songs
        FakeOracle.built.append(song)

    def state_from(self, state):
        return state

    def score_team(self, leader_id, member_ids, state):
        return 1000.0 + len(state["chr-1"])


@pytest.fixture
def fake_account(monkeypatch):
    FakeOracle.built = []
    monkeypatch.setattr(to, "Oracle", FakeOracle)
    monkeypatch.setattr(bps, "_account_stamp", lambda: "stamp")
    # (graphs, prices, owned, ...) as `_account_inputs` returns them: held[1] graphs, held[3] owned
    monkeypatch.setattr(bps, "_account_inputs", lambda oracle: ({"chr-1": {}}, {}, {"chr-1": [to.ROOT]}))
    monkeypatch.setattr(bps, "_ACCOUNT", {})
    monkeypatch.setattr(bps, "_BLOCKS", {})
    return FakeOracle


def test_a_plan_scores_its_candidates_after_another_plan_loaded_its_own_oracle(fake_account):
    # this plan's Oracle (no song), then another plan's (a pool), then this plan scores
    assert bps._held_account(1, None, "account", "finite", ()) is not None
    pool = (("m1", 0.5), ("m2", 0.5))
    assert bps._held_account(1, "m1", "account", "finite", pool) is not None
    board = {"chr-1": {"nodes": [to.ROOT, "n1"]}}
    assert bps._plan_score(dict(KW, song=None), board) == 1002.0
    assert fake_account.built == [None, "m1"]  # both held: nothing rebuilt


def test_an_evicted_oracle_is_held_again_rather_than_left_unscored(fake_account):
    for k in range(bps._ACCOUNT_HELD + 1):
        bps._held_account(1, f"m{k}", "account", "finite", ())
    assert len(bps._ACCOUNT) == bps._ACCOUNT_HELD
    assert all(key[1] != "m0" for key in bps._ACCOUNT)  # the oldest went
    board = {"chr-1": {"nodes": [to.ROOT]}}
    assert bps._plan_score(dict(KW, song="m0"), board) == 1001.0
    assert fake_account.built[-1] == "m0"


def test_a_refresh_drops_the_oracles_of_the_old_files(fake_account, monkeypatch):
    bps._held_account(1, None, "account", "finite", ())
    bps._BLOCKS["bench"] = object()
    monkeypatch.setattr(bps, "_account_stamp", lambda: "after a refresh")
    bps._held_account(1, None, "account", "finite", ())
    assert [key[4] for key in bps._ACCOUNT] == ["after a refresh"]
    assert bps._BLOCKS == {}


def test_a_solved_plan_that_cannot_be_scored_is_not_reported_as_the_wallet(monkeypatch):
    monkeypatch.setattr(
        bps, "solve_team_board", lambda **kw: {"chr-1": {"nodes": [kw["_valuation"]], "slots": {}}}
    )
    monkeypatch.setattr(bps, "_plan_score", lambda kw, board: None)
    with pytest.raises(RuntimeError, match="could not be scored"):
        bps._best_of_candidates(dict(KW))


def test_no_plan_at_all_is_still_none(monkeypatch):
    # the wallet funds no plan: every candidate None, the caller's "the wallet cannot fund"
    monkeypatch.setattr(bps, "solve_team_board", lambda **kw: None)
    assert bps._best_of_candidates(dict(KW)) is None
