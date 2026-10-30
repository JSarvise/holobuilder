"""`platform` (mobile | pc) — the flick-weight toggle reaches every scoring route.

The master's `LiveNote` PERFECT row differs between the mobile and PC columns in
exactly one cell (FLICK: 1050 vs 1000, in `main_LiveNote.json`), the
chart file carries a per-note flick flag instead of a platform, and the choice is
made at scoring time through `ScoringConfig.platform`. The web sends `platform`
with /solve, /acquire, the develop (/api/optimize-rerank) and the timeline; the
server parses all four through `_parse_platform`, whose contract is: a valid
value passes, anything else is the mobile default — never an error, it is a
what-if.

These drive `run_solve_job` directly (as test_song_solve.py does) with the
re-ranker stubbed at the `rank_on_chart` seam, so the test observes the `cfg`
the ranking is scored with rather than a number that could agree by accident.

    python -m pytest tools/web/test_solve_platform.py
"""
from __future__ import annotations

import queue
import sys
from pathlib import Path

import pytest

import server as web_server
from hbuilder.domain.card import Stats
from hbuilder.domain.song import charts_from_mapping
from hbuilder.domain.song_rank import RankedOnSong, RankedOnSongs

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from conftest import make_card, make_scoring_config, make_skill  # noqa: E402

MUSIC_ID = "m9990"


def _chart_library():
    document = {
        "schema_version": "charts-2",
        "charts": {
            f"{MUSIC_ID}/EXPERT": {
                "music_id": MUSIC_ID,
                "difficulty": "EXPERT",
                "difficulty_level": 26,
                "live_score_coefficient_permil": 4,
                "notes": [[float(t), 1000, 1 if t % 3 == 0 else 0] for t in range(1, 31)],
                "special_cues": [5.0, 10.0, 15.0, 20.0, 25.0],
            }
        },
    }
    return charts_from_mapping(document, source="test_solve_platform")


def _pool(size: int = 8):
    return tuple(
        make_card(
            f"card_{index}",
            member=f"Holomem{index}",
            stats=Stats(performance=100 + 13 * index, technique=90 + 7 * index, sense=80 + 3 * index),
            costume_skill=make_skill("COSTUME", family="stat_up", effect_type="all_stats",
                                     magnitude=5.0 + 3.0 * index),
        )
        for index in range(size)
    )


def _run(monkeypatch, tmp_path, **kwargs):
    """Run one song-mode job; return (events, the cfg the re-ranker was handed)."""
    pool = _pool()
    config = make_scoring_config()
    monkeypatch.setattr(web_server, "load_inputs", lambda *a, **k: (pool, pool, config, None))
    monkeypatch.setattr(web_server, "BOARD_DISABLED", True)
    monkeypatch.setattr(web_server, "COLLECTION_PATH", tmp_path / "no-such-collection.json")
    monkeypatch.setattr(web_server, "SONG_TITLES_PATH", tmp_path / "no-such-titles.json")
    library = _chart_library()
    monkeypatch.setattr(web_server, "load_charts", lambda _path: library)
    seen: dict = {}

    def spy_rank(teams, chart, cfg, *, filtered_from=None, progress=None):
        seen["cfg"] = cfg
        seen["chart"] = chart
        rows = [
            RankedOnSong(team=team, leader_id=team.leader.id,
                         member_ids=tuple(sorted(c.id for c in team.members)),
                         expected_score=1000.0 * (index + 1),
                         member_order=tuple(c.id for c in team.members))
            for index, team in enumerate(teams)
        ]
        rows.sort(key=lambda r: -r.expected_score)
        return RankedOnSongs(
            teams=tuple(rows), charts=(chart.music_id,),
            filtered_from=len(teams) if filtered_from is None else filtered_from,
        )

    monkeypatch.setattr(web_server, "rank_on_chart", spy_rank)
    job = web_server.Job(job_id="test-platform")
    web_server.run_solve_job(job, top_n=3, art_dir=Path("does-not-exist"), song=MUSIC_ID, **kwargs)
    events = []
    while True:
        try:
            events.append(job.events.get_nowait())
        except queue.Empty:
            break
    assert any(e.get("type") == "result" for e in events), [e.get("type") for e in events]
    return events, seen


def test_parse_platform_accepts_the_two_clients_and_defaults_to_mobile():
    assert web_server._parse_platform({"platform": "pc"}) == "pc"
    assert web_server._parse_platform({"platform": "PC"}) == "pc"
    assert web_server._parse_platform({"platform": "mobile"}) == "mobile"
    for bad in ({}, {"platform": ""}, {"platform": None}, {"platform": "steam"}, {"platform": 3}):
        assert web_server._parse_platform(bad) == "mobile", bad


def test_the_platform_reaches_the_song_reranker_and_defaults_to_mobile(monkeypatch, tmp_path):
    _, seen = _run(monkeypatch, tmp_path)
    assert seen["cfg"].platform == "mobile"
    _, seen = _run(monkeypatch, tmp_path, platform="pc")
    assert seen["cfg"].platform == "pc"
    _, seen = _run(monkeypatch, tmp_path, platform="mobile")
    assert seen["cfg"].platform == "mobile"
    # The stubbed chart really carries the flag the platform acts on.
    assert seen["chart"].flick_count() == 10
    assert seen["chart"].notes_perfect_coefficient_sum("mobile") == pytest.approx(30.5)
    assert seen["chart"].notes_perfect_coefficient_sum("pc") == pytest.approx(30.0)


def test_the_frontend_sends_platform_on_every_scoring_call():
    """The four bodies the page builds (solve, acquire, develop, timeline via
    whatIf) each name `platform`, and the toggle defaults to mobile."""
    static = Path(web_server.__file__).resolve().parent / "static"
    app = (static / "app.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")
    assert app.count('platform: ($("platform") && $("platform").value) || "mobile"') >= 3
    assert 'body.platform = ($("platform") && $("platform").value) || "mobile"' in app
    assert 'data-seg="platform"' in html
    assert '<option value="mobile" selected>' in html
    assert '<option value="pc">' in html
