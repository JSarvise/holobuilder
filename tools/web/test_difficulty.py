"""`difficulty` (EASY | NORMAL | HARD | EXPERT) — the chart a run scores on.

EXPERT is the default, but a player who plays by hand may
find EXPERT too hard; another difficulty is another chart (other notes, another
note count), so the score — and possibly the best team — must be computed on
THAT chart. EXPERT lives in charts.json, the other three only in
charts_all.json (`build_charts.py --all-out`).

Covered here: the parser (default EXPERT, unknown -> 400), the chart a song-mode
solve hands the re-ranker, a real chart at two difficulties scoring differently,
the two-stage cache keys/files (EXPERT unchanged, others distinct), the
certificate guard refusing an EXPERT certificate for a HARD row, and the HTTP
surface (/solve 400, /api/meta listing).

    python -m pytest tools/web/test_difficulty.py
"""
from __future__ import annotations

import json
import queue
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import certificate as cert_mod
import certindex
import server as web_server
from hbuilder.domain.card import Stats
from hbuilder.domain.song import charts_from_mapping, load_charts, score_song_detailed
from hbuilder.domain.song_rank import RankedOnSong, RankedOnSongs

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "tools" / "recon" / "proxy"))
from conftest import make_card, make_scoring_config, make_skill  # noqa: E402

import two_stage_build as tsb  # noqa: E402

MUSIC_ID = "m9991"


def _entry(difficulty: str, n_notes: int, level: int) -> dict:
    return {
        "music_id": MUSIC_ID, "difficulty": difficulty, "difficulty_level": level,
        "live_score_coefficient_permil": 4,
        "notes": [[float(t), 1000, 0] for t in range(1, n_notes + 1)],
        "special_cues": [5.0, 10.0, 15.0, 20.0, 25.0],
    }


def _two_difficulty_library():
    return charts_from_mapping({"schema_version": "charts-2", "charts": {
        f"{MUSIC_ID}/EXPERT": _entry("EXPERT", 40, 26),
        f"{MUSIC_ID}/HARD": _entry("HARD", 28, 18),
    }}, source="test_difficulty")


def _pool(size: int = 8):
    return tuple(
        make_card(
            f"card_{index}", member=f"Holomem{index}",
            stats=Stats(performance=100 + 13 * index, technique=90 + 7 * index,
                        sense=80 + 3 * index),
            costume_skill=make_skill("COSTUME", family="stat_up", effect_type="all_stats",
                                     magnitude=5.0 + 3.0 * index),
        )
        for index in range(size)
    )


# -- the parser ---------------------------------------------------------------
def test_absent_or_empty_difficulty_is_expert():
    for body in ({}, {"difficulty": None}, {"difficulty": ""}, {"difficulty": "expert"}):
        assert web_server._parse_difficulty(body) == "EXPERT", body


def test_an_unknown_difficulty_is_a_400_not_a_silent_expert():
    for bad in ("insane", "MASTER", 3):
        with pytest.raises(web_server._HttpError) as caught:
            web_server._parse_difficulty({"difficulty": bad})
        assert caught.value.code == 400 and "difficulty" in caught.value.obj["error"]


def test_expert_reads_charts_json_and_the_rest_charts_all():
    assert web_server._charts_path("EXPERT") == web_server.CHARTS_PATH
    for name in ("EASY", "NORMAL", "HARD"):
        assert web_server._charts_path(name) == web_server.CHARTS_ALL_PATH
    assert web_server.CHARTS_ALL_PATH.name == "charts_all.json"


# -- a song-mode solve hands the re-ranker the chart AT the difficulty ----------
def _solve(monkeypatch, tmp_path, **kwargs):
    pool = _pool()
    config = make_scoring_config()
    monkeypatch.setattr(web_server, "load_inputs", lambda *a, **k: (pool, pool, config, None))
    monkeypatch.setattr(web_server, "BOARD_DISABLED", True)
    monkeypatch.setattr(web_server, "COLLECTION_PATH", tmp_path / "no-such-collection.json")
    monkeypatch.setattr(web_server, "SONG_TITLES_PATH", tmp_path / "no-such-titles.json")
    library = _two_difficulty_library()
    seen: dict = {"paths": []}

    def loader(path):
        seen["paths"].append(Path(path).name)
        return library

    monkeypatch.setattr(web_server, "load_charts", loader)

    def spy_rank(teams, chart, cfg, *, filtered_from=None, progress=None):
        seen["chart"] = chart
        rows = [RankedOnSong(team=t, leader_id=t.leader.id,
                             member_ids=tuple(sorted(c.id for c in t.members)),
                             expected_score=1000.0 * (i + 1),
                             member_order=tuple(c.id for c in t.members))
                for i, t in enumerate(teams)]
        rows.sort(key=lambda r: -r.expected_score)
        return RankedOnSongs(teams=tuple(rows), charts=(chart.music_id,),
                             filtered_from=len(teams) if filtered_from is None else filtered_from)

    monkeypatch.setattr(web_server, "rank_on_chart", spy_rank)
    job = web_server.Job(job_id="test-difficulty")
    web_server.run_solve_job(job, top_n=3, art_dir=Path("does-not-exist"), song=MUSIC_ID, **kwargs)
    events = []
    while True:
        try:
            events.append(job.events.get_nowait())
        except queue.Empty:
            break
    result = next(e for e in events if e.get("type") == "result")
    return result["payload"], seen


def test_the_song_solve_scores_the_chart_at_the_requested_difficulty(monkeypatch, tmp_path):
    payload, seen = _solve(monkeypatch, tmp_path)
    assert seen["chart"].difficulty == "EXPERT" and len(seen["chart"].notes) == 40
    assert seen["paths"] == ["charts.json"]
    assert payload["song"]["difficulty"] == "EXPERT"
    assert "difficulty" not in payload["certificate_basis"]      # EXPERT shape unchanged

    payload, seen = _solve(monkeypatch, tmp_path, difficulty="HARD")
    assert seen["chart"].difficulty == "HARD" and len(seen["chart"].notes) == 28
    assert seen["paths"] == ["charts_all.json"]
    assert payload["song"]["difficulty"] == "HARD"
    assert payload["certificate_basis"]["difficulty"] == "HARD"


def test_the_same_team_scores_differently_on_another_difficulty():
    library = _two_difficulty_library()
    pool = _pool()
    cfg = make_scoring_config()
    expert = library.get(MUSIC_ID)                 # the default is EXPERT
    hard = library.get(MUSIC_ID, "HARD")
    assert expert.difficulty == "EXPERT" and len(expert.notes) != len(hard.notes)
    on_expert = score_song_detailed(pool[1:6], (pool[0],), expert, cfg)[0].total_score
    on_hard = score_song_detailed(pool[1:6], (pool[0],), hard, cfg)[0].total_score
    assert on_expert != on_hard


@pytest.mark.skipif(not cert_mod.CHARTS_ALL_PATH.is_file(), reason="charts_all.json not on disk")
def test_real_charts_differ_by_difficulty_in_notes_and_score():
    library = load_charts(str(cert_mod.CHARTS_ALL_PATH))
    song = library.music_ids[0]
    expert, hard = library.get(song, "EXPERT"), library.get(song, "HARD")
    assert expert is not None and hard is not None
    assert len(expert.notes) != len(hard.notes)
    pool = _pool()
    cfg = make_scoring_config()
    assert (score_song_detailed(pool[1:6], (pool[0],), expert, cfg)[0].total_score
            != score_song_detailed(pool[1:6], (pool[0],), hard, cfg)[0].total_score)
    # charts.json's EXPERT entry is charts_all.json's, byte for byte, so the
    # EXPERT default reads the same chart from either file
    assert (cert_mod.chart_digest(song) ==
            cert_mod.chart_digest(song, charts_path=cert_mod.CHARTS_ALL_PATH))


# -- the two-stage cache --------------------------------------------------------
def test_two_stage_keys_are_unchanged_for_expert_and_distinct_per_difficulty():
    digests = {"m0001": "a" * 64, "m0002": "b" * 64}
    today = tsb.keys_for("ref", digests)
    assert tsb.keys_for("ref", digests, "maxed", "EXPERT") == today
    assert tsb.keys_for("ref", digests, "maxed", "expert") == today
    seen = {today[1]}
    for name in ("EASY", "NORMAL", "HARD"):
        songs, general = tsb.keys_for("ref", digests, "maxed", name)
        assert all(songs[s] != today[0][s] for s in digests) and general not in seen
        seen.add(general)
    account = tsb.keys_for("ref", digests, "account", "HARD")
    assert account != tsb.keys_for("ref", digests, "maxed", "HARD")


def test_two_stage_files_keep_their_expert_names():
    assert tsb.song_file("m0001") == "song_m0001.json"
    assert tsb.song_file("m0001", "maxed", "EXPERT") == "song_m0001.json"
    assert tsb.general_file() == "general.json" and tsb.singers_file() == "singers.json"
    assert tsb.song_file("m0001", "account", "HARD") == "account_song_m0001_hard.json"
    assert tsb.general_file("maxed", "easy") == "general_easy.json"
    assert tsb.singers_file("account-max", "NORMAL") == "accountmax_singers_normal.json"
    with pytest.raises(ValueError):
        tsb.song_file("m0001", "maxed", "insane")


# -- certificates are EXPERT: a HARD row never borrows one ------------------------
def test_the_certificate_guard_compares_the_difficulty():
    entry = {"inputs_hash": "h", "universe": "u", "budget_kind": "flat", "budget": 453,
             "uptime_model": "finite", "platform": "mobile"}
    expert = cert_mod.certificate_basis(universe="u", basis="maxed", budget_kind="flat",
                                        budget=453, uptime_model="finite", platform="mobile")
    assert "difficulty" not in expert
    assert certindex.CertificateIndex._mismatch(entry, "h", expert) is None
    hard = cert_mod.certificate_basis(universe="u", basis="maxed", budget_kind="flat",
                                      budget=453, uptime_model="finite", platform="mobile",
                                      difficulty="hard")
    assert hard["difficulty"] == "HARD"
    assert "difficulty" in certindex.CertificateIndex._mismatch(entry, "h", hard)
    assert certindex.CertificateIndex._mismatch({**entry, "difficulty": "HARD"}, "h", hard) is None


# -- the HTTP surface -----------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def web_base():
    port = _free_port()
    server = web_server.ThreadingServer((web_server.BIND_HOST, port), web_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _post(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.mark.parametrize("route", ["/solve", "/acquire", "/api/run", "/api/optimize-rerank",
                                   "/api/compare", "/api/timeline"])
def test_an_unknown_difficulty_is_rejected_with_400(web_base, route):
    body = {"song": "m0334", "difficulty": "insane",
            "teams": [list("abcdef"), list("ghijkl")], "cards": list("abcdef")}
    status, doc = _post(web_base + route, body)
    assert status == 400, (route, status, doc)
    assert "difficulty" in doc["error"]


def test_songs_route_rejects_an_unknown_difficulty(web_base):
    try:
        urllib.request.urlopen(web_base + "/api/songs?difficulty=insane", timeout=30)
        raise AssertionError("expected a 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_meta_lists_the_difficulties(web_base):
    with urllib.request.urlopen(web_base + "/api/meta", timeout=60) as response:
        body = json.loads(response.read())
    listing = body["difficulties"]
    assert listing["default"] == "EXPERT"
    on_disk = cert_mod.available_difficulties()
    assert listing["available"] == [d for d in cert_mod.DIFFICULTIES if d in on_disk]
    assert listing["charts"] == {d: len(on_disk[d]) for d in listing["available"]}
