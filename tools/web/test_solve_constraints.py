"""`POST /solve` search constraints — require / leader / exclude / include-unowned.

`run_solve_job` passes the page's pins through `resolve_selectors` (the CLI's own
selector resolver), so the page can pin a leader, force-include or ban a card.
These drive `run_solve_job` directly with a synthetic pool (the same way
test_song_solve.py does) and assert the constraint actually shapes the ranking,
and that a bad id / conflict is reported as an error event rather than crashing.
"""
from __future__ import annotations

import queue
import sys
from pathlib import Path

import server as web_server
from hbuilder.domain.card import Stats

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from conftest import make_card, make_scoring_config, make_skill  # noqa: E402


def _pool(size: int = 8):
    return tuple(
        make_card(
            f"card_{index}",
            member=f"Holomem{index}",
            stats=Stats(
                performance=100 + 13 * index,
                technique=90 + 7 * index,
                sense=80 + 3 * index,
            ),
            costume_skill=make_skill(
                "COSTUME", family="stat_up", effect_type="all_stats",
                magnitude=5.0 + 3.0 * index,
            ),
        )
        for index in range(size)
    )


def _run(monkeypatch, tmp_path, **constraints):
    pool = _pool()
    config = make_scoring_config()
    monkeypatch.setattr(
        web_server, "load_inputs", lambda *a, **k: (pool, pool, config, None)
    )
    monkeypatch.setattr(web_server, "BOARD_DISABLED", True)
    monkeypatch.setattr(
        web_server, "COLLECTION_PATH", tmp_path / "no-such-collection.json"
    )
    job = web_server.Job(job_id="test-constraints")
    web_server.run_solve_job(
        job, top_n=5, art_dir=Path("does-not-exist"), song="", **constraints
    )
    events = []
    while True:
        try:
            events.append(job.events.get_nowait())
        except queue.Empty:
            break
    return events


def _teams(events):
    results = [e for e in events if e.get("type") == "result"]
    assert results, f"no result event; got {[e.get('type') for e in events]}"
    return results[-1]["payload"]["teams"]


def _errors(events):
    return [e["message"] for e in events if e.get("type") == "error"]


def test_leader_constraint_pins_the_leader_of_every_team(monkeypatch, tmp_path):
    events = _run(monkeypatch, tmp_path, leader=("card_3",))
    assert not _errors(events)
    teams = _teams(events)
    assert teams, "no teams returned"
    assert all(t["leader_id"] == "card_3" for t in teams), (
        f"leader not pinned: {[t['leader_id'] for t in teams]}"
    )


def test_exclude_bars_a_card_from_every_team(monkeypatch, tmp_path):
    # card_7 is the highest-stat card, so it would normally appear; ban it.
    events = _run(monkeypatch, tmp_path, exclude=("card_7",))
    assert not _errors(events)
    for t in _teams(events):
        roster = {t["leader_id"], *t["member_ids"]}
        assert "card_7" not in roster, f"excluded card in team: {roster}"


def test_require_locks_a_card_into_every_team(monkeypatch, tmp_path):
    events = _run(monkeypatch, tmp_path, require=("card_1",))
    assert not _errors(events)
    teams = _teams(events)
    assert teams
    for t in _teams(events):
        roster = {t["leader_id"], *t["member_ids"]}
        assert "card_1" in roster, f"required card missing: {roster}"


def test_unknown_card_id_is_reported_as_an_error_not_a_crash(monkeypatch, tmp_path):
    events = _run(monkeypatch, tmp_path, require=("card_nope",))
    errors = _errors(events)
    assert errors, "an unknown required id should emit an error event"
    assert "card_nope" in errors[0]
    # and no result is produced
    assert not [e for e in events if e.get("type") == "result"]


def test_exclude_that_conflicts_with_require_is_reported(monkeypatch, tmp_path):
    events = _run(monkeypatch, tmp_path, require=("card_2",), exclude=("card_2",))
    errors = _errors(events)
    assert errors, "a require/exclude conflict should emit an error event"
    assert "card_2" in errors[0]


def test_estimate_reports_cost_without_solving(monkeypatch, tmp_path):
    events = _run(monkeypatch, tmp_path, estimate=True)
    types = [e.get("type") for e in events]
    assert "projection" in types
    assert "estimate" in types, types
    assert "result" not in types, "estimate must never enumerate a result"
    est = next(e for e in events if e.get("type") == "estimate")
    assert est["arrangement_count"] > 0


def test_a_non_estimate_run_still_produces_a_result(monkeypatch, tmp_path):
    events = _run(monkeypatch, tmp_path, estimate=False)
    assert any(e.get("type") == "result" for e in events)


def test_force_exact_is_accepted_and_still_solves(monkeypatch, tmp_path):
    # On a tiny pool the search is exact anyway; this only pins that the flag is
    # threaded and does not break the run.
    events = _run(monkeypatch, tmp_path, force_exact=True)
    proj = next(e for e in events if e.get("type") == "projection")
    assert proj["solver"] == "exact"
    assert any(e.get("type") == "result" for e in events)
