"""`POST /solve` with `across_songs` — rank by the MEAN expected score over every
chart, with a WINS count (the CLI's --across-songs).

Drives run_solve_job directly with a synthetic pool + a 2-chart library (the same
way test_song_solve.py does), so no dumps are needed. workers=1 keeps it in-process
(rank_across_charts_parallel runs the serial path), so nothing spawns.
"""
from __future__ import annotations

import queue
import sys
from pathlib import Path

import server as web_server
from hbuilder.domain.card import Stats
from hbuilder.domain.song import charts_from_mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from conftest import make_card, make_scoring_config, make_skill  # noqa: E402


def _two_chart_library():
    doc = {"schema_version": "charts-1", "charts": {}}
    for mid in ("m9990", "m9991"):
        doc["charts"][f"{mid}/EXPERT"] = {
            "music_id": mid,
            "difficulty": "EXPERT",
            "difficulty_level": 26,
            "live_score_coefficient_permil": 4,
            "notes": [[float(t), 1000] for t in range(1, 31)],
            "special_cues": [5.0, 10.0, 15.0, 20.0, 25.0],
        }
    return charts_from_mapping(doc, source="test_across")


def _pool(size: int = 8):
    return tuple(
        make_card(
            f"card_{i}",
            member=f"Holomem{i}",
            stats=Stats(performance=100 + 13 * i, technique=90 + 7 * i, sense=80 + 3 * i),
            costume_skill=make_skill(
                "COSTUME", family="stat_up", effect_type="all_stats",
                magnitude=5.0 + 3.0 * i,
            ),
        )
        for i in range(size)
    )


def _run_across(monkeypatch, tmp_path):
    pool = _pool()
    cfg = make_scoring_config()
    monkeypatch.setattr(
        web_server, "load_inputs", lambda *a, **k: (pool, pool, cfg, None)
    )
    monkeypatch.setattr(web_server, "BOARD_DISABLED", True)
    monkeypatch.setattr(web_server, "COLLECTION_PATH", tmp_path / "none.json")
    monkeypatch.setattr(web_server, "load_charts", lambda _p: _two_chart_library())
    job = web_server.Job(job_id="test-across")
    web_server.run_solve_job(
        job, top_n=3, art_dir=Path("nope"), song="", across_songs=True
    )
    events = []
    while True:
        try:
            events.append(job.events.get_nowait())
        except queue.Empty:
            break
    return events


def test_across_songs_ranks_by_mean_with_wins(monkeypatch, tmp_path):
    events = _run_across(monkeypatch, tmp_path)
    assert not [e for e in events if e.get("type") == "error"], events
    result = next(e for e in events if e.get("type") == "result")
    payload = result["payload"]
    assert payload["sorted_by"] == "across_song"
    assert payload["across"]["chart_count"] == 2
    teams = payload["teams"]
    assert teams
    for t in teams:
        assert "across_score" in t, t.keys()
        assert "wins" in t
    # the table is ordered by the mean across_score, best first
    scores = [t["across_score"] for t in teams]
    assert scores == sorted(scores, reverse=True)
    # every chart is a win for someone: wins sum to at most the chart count per team
    assert sum(t["wins"] for t in teams) >= 1


def test_across_songs_emits_chart_scoped_progress(monkeypatch, tmp_path):
    events = _run_across(monkeypatch, tmp_path)
    assert any(e.get("type") == "song_progress" for e in events)
