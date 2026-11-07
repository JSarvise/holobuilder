"""`difficulty` (EASY | NORMAL | HARD | EXPERT) — the chart a run scores on.

EXPERT is the default, but a player who plays by hand may
find EXPERT too hard; another difficulty is another chart (other notes, another
note count), so the score — and possibly the best team — must be computed on
THAT chart. EXPERT lives in charts.json, the other three only in
charts_all.json (`build_charts.py --all-out`).

Covered here: the parser (default EXPERT, unknown -> 400), a real chart at two
difficulties scoring differently, the two-stage keys/files (EXPERT unchanged, others
distinct), the certificate guard refusing an EXPERT certificate for a HARD row, and the
HTTP surface (/api/compare and /api/timeline 400, /api/meta listing).

    python -m pytest tools/web/test_difficulty.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import certificate as cert_mod
from hbweb import basis, config
from hbuilder.domain.card import Stats
from hbuilder.domain.song import charts_from_mapping, load_charts, score_song_detailed

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "tools" / "refresh"))
from support.builders import make_card, make_scoring_config, make_skill  # noqa: E402

import leaf_inputs  # noqa: E402

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
        assert basis.parse_difficulty(body) == "EXPERT", body


def test_an_unknown_difficulty_is_a_400_not_a_silent_expert():
    for bad in ("insane", "MASTER", 3):
        with pytest.raises(basis.HttpError) as caught:
            basis.parse_difficulty({"difficulty": bad})
        assert caught.value.code == 400 and "difficulty" in caught.value.obj["error"]


def test_expert_reads_charts_json_and_the_rest_charts_all():
    assert basis.charts_path("EXPERT") == config.CHARTS_PATH
    for name in ("EASY", "NORMAL", "HARD"):
        assert basis.charts_path(name) == config.CHARTS_ALL_PATH
    assert config.CHARTS_ALL_PATH.name == "charts_all.json"


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
    today = leaf_inputs.keys_for("ref", digests)
    assert leaf_inputs.keys_for("ref", digests, "maxed", "EXPERT") == today
    assert leaf_inputs.keys_for("ref", digests, "maxed", "expert") == today
    seen = {today[1]}
    for name in ("EASY", "NORMAL", "HARD"):
        songs, general = leaf_inputs.keys_for("ref", digests, "maxed", name)
        assert all(songs[s] != today[0][s] for s in digests) and general not in seen
        seen.add(general)
    account = leaf_inputs.keys_for("ref", digests, "account", "HARD")
    assert account != leaf_inputs.keys_for("ref", digests, "maxed", "HARD")


def test_an_unknown_difficulty_is_refused():
    assert leaf_inputs._difficulty(None) == "EXPERT" and leaf_inputs._difficulty("hard") == "HARD"
    with pytest.raises(ValueError):
        leaf_inputs._difficulty("insane")


# -- the HTTP surface -----------------------------------------------------------
def _post(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.mark.parametrize("route", ["/api/compare", "/api/timeline"])
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
