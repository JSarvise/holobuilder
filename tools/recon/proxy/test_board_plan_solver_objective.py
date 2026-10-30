"""The exact board planner must plan for the score the search and the page use.

`config.json` sets `active_uptime: steady` (the deck-screen model); the search
and the page score in-run (`finite`). Left on the file's value, the MILP planned
boards for a different objective and its plans scored up to 12.7 % below the
search's greedy plans on the in-run score."""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for _p in (HERE, REPO / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def test_the_solver_defaults_to_the_in_run_model():
    pytest.importorskip("highspy")
    import board_plan_solver

    default = inspect.signature(board_plan_solver.solve_team_board).parameters["uptime_model"].default
    assert default == "finite"


def test_the_oracle_takes_the_uptime_model_and_otherwise_keeps_the_file():
    if not (REPO / "tools/recon/proxy/dumps/skilltree.json").exists():
        pytest.skip("dumps/ not on this checkout")
    import tree_optimise as to

    configured = json.loads((REPO / "config.json").read_text(encoding="utf-8"))["model"]["active_uptime"]
    assert to.Oracle(pool_size=1, scenario="account", quiet=True).base_cfg.active_uptime_model == configured
    for model in ("finite", "steady"):
        oracle = to.Oracle(pool_size=1, scenario="account", quiet=True, uptime_model=model)
        assert oracle.base_cfg.active_uptime_model == model
