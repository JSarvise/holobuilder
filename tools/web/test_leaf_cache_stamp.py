"""row_detail's cached leaf inputs are served only while the account files they were read
from are unchanged: a refresh rewrites the boards, and a detail opened right after it must
be computed on the new ones, not on inputs held from before it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "refresh"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_plan_solver as bps  # noqa: E402
import leaf_inputs  # noqa: E402
import row_detail  # noqa: E402


def test_leaf_inputs_rebuilt_when_account_files_change(monkeypatch):
    builds: list[int] = []
    stamp = [(1, 2, 3)]
    monkeypatch.setattr(row_detail, "_LEAF_CACHE", {})
    monkeypatch.setattr(bps, "_account_stamp", lambda: stamp[0])
    monkeypatch.setattr(leaf_inputs, "leaf_inputs", lambda *a, **k: builds.append(1) or {"n": len(builds)})

    first = row_detail._leaf_inputs_cached("account", "m0020", "EXPERT")
    again = row_detail._leaf_inputs_cached("account", "m0020", "EXPERT")
    assert again is first and len(builds) == 1  # same files: the held inputs

    stamp[0] = (1, 2, 4)  # a refresh rewrote board_state.json
    after = row_detail._leaf_inputs_cached("account", "m0020", "EXPERT")
    assert after is not first and after["n"] == 2
