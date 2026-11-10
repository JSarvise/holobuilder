"""A missing account dump is an answer the page shows, never a dropped request: the library
raises `MissingInput` (not `SystemExit`), `GET /api/resources` answers `{available: False,
reason}` naming the file, and a search job emits an `error` event before its `end`.

Every dumps directory here is a temp dir; the real dumps are only read (two small files are
copied for the search job)."""

from __future__ import annotations

import importlib
import json
import queue
import shutil
import sys
import urllib.request

import pytest
from hbweb import basis, config, jobs

from hbuilder import paths
from hbuilder.errors import MissingInput

if str(config.REFRESH_DIR) not in sys.path:
    sys.path.insert(0, str(config.REFRESH_DIR))


def _tree_optimise():
    """The `tree_optimise` module `precompute_optimisations` imports (the one in sys.modules)."""
    return importlib.import_module("tree_optimise")


def _drain(job) -> list[dict]:
    out = []
    while True:
        try:
            out.append(job.events.get_nowait())
        except queue.Empty:
            return out


def test_a_missing_account_dump_raises_missing_input_not_system_exit(tmp_path, monkeypatch):
    to = _tree_optimise()
    monkeypatch.setattr(to, "DUMPS", tmp_path)
    with pytest.raises(MissingInput, match="board_state.json"):
        to._owned_nodes()
    with pytest.raises(MissingInput, match="account_snapshot_main.json"):
        to._character_points()
    assert not issubclass(MissingInput, SystemExit) and issubclass(MissingInput, Exception)


def test_resources_answers_a_missing_board_state_as_unavailable(tmp_path, monkeypatch, serve_web):
    if basis.load_precompute() is None:
        pytest.skip("precompute_optimisations does not import here (requirements.txt)")
    to = _tree_optimise()
    monkeypatch.setattr(to, "DUMPS", tmp_path)  # no board_state.json in it
    monkeypatch.setattr(to, "_prices", lambda: {})  # no game master needed
    with serve_web() as base:
        with urllib.request.urlopen(base + "/api/resources", timeout=60) as resp:
            status, doc = resp.status, json.loads(resp.read())
    assert status == 200
    assert doc["available"] is False
    assert "board_state.json" in doc["reason"]


_ACCOUNT = all(
    (config.REPO_ROOT / p).exists()
    for p in (
        "data/owned.json",
        "state/collection.json",
        "tools/recon/proxy/dumps/skilltree.json",
        "tools/recon/proxy/dumps/board_state.json",
        "tools/recon/proxy/dumps/card_level_curve.json",
    )
)


@pytest.mark.skipif(not _ACCOUNT, reason="needs the account's data (gitignored)")
def test_a_search_job_reports_a_missing_snapshot_as_an_error_event(tmp_path, monkeypatch):
    import board_export  # noqa: PLC0415
    import ondemand  # noqa: PLC0415

    for name in ("skilltree.json", "board_state.json"):  # everything but the snapshot
        shutil.copy(paths.DUMPS / name, tmp_path / name)
    monkeypatch.setattr(board_export.to, "DUMPS", tmp_path)
    monkeypatch.setattr(board_export.to, "_prices", lambda: {})
    assumptions = ondemand.parse_assumptions(None)
    opts = {
        "song": "m0342",
        "basis": "account",
        "difficulty": "EXPERT",
        "top": 5,
        "filters": ondemand.Filters(),
        "assumptions": assumptions,
    }
    job = jobs.Job(job_id="missing-snapshot")
    # the real account build (`ondemand.Engine`), outside the engine cache
    jobs.run_search_job(
        job, opts, engine_for=lambda b, d: ondemand.Engine(b, d, workers=1, assumptions=assumptions)
    )
    events = _drain(job)
    kinds = [e["type"] for e in events]
    assert kinds[-2:] == ["error", "end"] and "result" not in kinds
    assert "MissingInput" in events[-2]["message"]
    assert "account_snapshot_main.json" in events[-2]["message"]
    assert job.done
