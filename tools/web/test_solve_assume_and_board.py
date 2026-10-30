"""Two customizability filters on `POST /solve`:

- Board OFF must be a true EMPTY-board run: neither the capture NOR the user's
  EDITED board is scored, so the ranking answers the from-scratch question.
- The level/bloom what-if (`assume_level`/`assume_bloom`) must reach
  `apply_collection`, whose own correctness the domain tests pin.

Both drive `run_solve_job` directly, the way test_solve_constraints.py does.
"""
from __future__ import annotations

import queue
import sys
from pathlib import Path

import server as web_server
from hbuilder.cli import CollectionBasis
from hbuilder.domain.card import Stats

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from conftest import make_card, make_scoring_config  # noqa: E402


class _NoBoard:
    """A board basis with no model (the OFF / absent state)."""
    model = None

    def lines(self):
        return ()


def _pool(size: int = 6):
    return tuple(
        make_card(
            f"card_{i}", member=f"Holomem{i}",
            stats=Stats(performance=100 + 13 * i, technique=90 + 7 * i, sense=80 + 3 * i),
        )
        for i in range(size)
    )


def _events(job):
    out = []
    while True:
        try:
            out.append(job.events.get_nowait())
        except queue.Empty:
            return out


def test_board_off_ignores_even_the_edited_board(monkeypatch, tmp_path):
    """Board OFF => `loaded: False`, even though an EDITED board is available. If
    the gate regresses, `_edited_board_model`'s board is applied and loaded flips
    to True — which this pins against."""
    pool = _pool()
    monkeypatch.setattr(web_server, "load_inputs", lambda *a, **k: (pool, pool, make_scoring_config(), None))
    monkeypatch.setattr(web_server, "BOARD_DISABLED", False)
    monkeypatch.setattr(web_server, "COLLECTION_PATH", tmp_path / "no-collection.json")
    monkeypatch.setattr(web_server, "load_board_basis", lambda *a, **k: _NoBoard())
    monkeypatch.setattr(web_server, "_edited_board_model", lambda: (object(), ["edited board present"]))

    job = web_server.Job(job_id="test-board-off")
    web_server.run_solve_job(job, top_n=3, art_dir=Path("nope"), song="", use_board=False)
    board_events = [e for e in _events(job) if e.get("type") == "board"]
    assert board_events, "no board event emitted"
    assert board_events[-1]["loaded"] is False, (
        "board OFF still loaded a board — the edited board was applied when it "
        "should have been ignored"
    )


def test_assume_level_and_bloom_reach_apply_collection(monkeypatch, tmp_path):
    """The web forwards `assume_level`/`assume_bloom` to apply_collection (whose
    own correctness the domain tests pin). Capture the kwargs it is called with."""
    pool = _pool()
    monkeypatch.setattr(web_server, "load_inputs", lambda *a, **k: (pool, pool, make_scoring_config(), None))
    monkeypatch.setattr(web_server, "BOARD_DISABLED", True)
    monkeypatch.setattr(web_server, "COLLECTION_PATH", tmp_path / "collection.json")

    seen = {}

    def _capture(cards, path, **kwargs):
        seen.update(kwargs)
        return cards, CollectionBasis(None, None, 0, len(cards), (), (), False)

    monkeypatch.setattr(web_server, "apply_collection", _capture)

    job = web_server.Job(job_id="test-assume")
    web_server.run_solve_job(
        job, top_n=3, art_dir=Path("nope"), song="", basis="account",
        assume_level="max", assume_bloom=4,
    )
    assert seen.get("assume_level") == "max"
    assert seen.get("assume_bloom") == 4
