"""A `MissingInput` that reaches the top of a command-line script prints its one-line message
and exits with code 1, without a traceback; any other uncaught error keeps its traceback."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def _run(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(REPO), env=env)


def test_an_uncaught_missing_input_prints_one_line_and_exits_1():
    got = _run("from hbuilder import paths\n"
               "from hbuilder.errors import MissingInput\n"
               "raise MissingInput('missing dumps/board_state.json -- run refresh.cmd')\n")
    assert got.returncode == 1
    assert got.stderr.strip() == "missing dumps/board_state.json -- run refresh.cmd"
    assert "Traceback" not in got.stderr


def test_any_other_uncaught_error_keeps_its_traceback():
    got = _run("from hbuilder import paths\nraise ValueError('boom')\n")
    assert got.returncode == 1
    assert "Traceback" in got.stderr and "ValueError: boom" in got.stderr


def test_build_board_reports_a_self_contradicting_node_row_as_missing_input():
    spec = importlib.util.spec_from_file_location(
        "build_board_under_test", REPO / "tools" / "refresh" / "build_board.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from hbuilder.errors import MissingInput

    rows = [{"group_id": "grp-1", module.ROW_DATA_KEY: {"1": ["grp-2"]}}]
    with pytest.raises(MissingInput, match="disagrees with itself"):
        module._effect_rows_by_group(rows)
