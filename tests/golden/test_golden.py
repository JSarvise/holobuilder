"""The golden comparison as a test: opt-in, because one run takes several minutes.

Set ``HB_GOLDEN_FROZEN`` to a snapshot made by ``freeze.py`` that holds an ``expected/`` run.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import golden  # noqa: E402

FROZEN = os.environ.get("HB_GOLDEN_FROZEN")


@pytest.mark.skipif(not FROZEN, reason="HB_GOLDEN_FROZEN is not set (golden runs are opt-in)")
def test_golden_outputs_identical():
    frozen = Path(FROZEN)
    expected = frozen / "expected"
    assert expected.is_dir(), f"{expected} holds no recorded golden"
    out = Path(tempfile.mkdtemp(prefix="hbgold_run_"))
    golden.run(golden.REPO, frozen, out, workers=None)
    assert golden.diff(expected, out) == []
