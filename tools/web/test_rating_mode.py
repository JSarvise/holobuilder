"""The Holomem Rating goal's song selection and account figures (`rating_mode.py`)."""

from __future__ import annotations

import rating_mode as R

from hbuilder.domain import rating


def _fake(monkeypatch, songs, charts, names=None):
    from hbweb import songs as songs_mod

    monkeypatch.setattr(R, "music_songs", lambda: {s.music_id: s for s in songs})
    monkeypatch.setattr(R, "chart_table", lambda: charts)
    monkeypatch.setattr(R, "holomem_names", lambda: names or {"chr-1": "One", "chr-2": "Two"})
    monkeypatch.setattr(
        songs_mod, "song_labels", lambda: ({s.music_id: s.music_id.upper() for s in songs}, {})
    )


def _chart(level, notes=1000, coef=4):
    return {"level": level, "coefficient": coef, "notes": notes}


def test_song_pool_picks_the_hardest_chart_within_reach(monkeypatch):
    songs = [
        rating.RatingSong("m1", ("chr-1",), "solo", True),
        rating.RatingSong("m2", ("chr-1", "chr-2"), "group", True),
        rating.RatingSong("m3", ("chr-1", "chr-2", "chr-3"), "all", False),  # all-member: never
        rating.RatingSong("m4", ("chr-1",), "solo", True),  # not owned
        rating.RatingSong("m5", ("chr-1",), "solo", True),  # every chart above the level
    ]
    charts = {
        "m1": {"HARD": _chart(18), "EXPERT": _chart(27)},
        "m2": {"HARD": _chart(19), "EXPERT": _chart(29)},
        "m3": {"EXPERT": _chart(26)},
        "m4": {"EXPERT": _chart(25)},
        "m5": {"EXPERT": _chart(31)},
    }
    _fake(monkeypatch, songs, charts)
    doc = {
        "owned_songs": ["m1", "m2", "m3", "m5"],
        "records": {
            "chr-1": {
                "m1": {
                    "HARD": {"score": 2_000_000, "rating": 400},
                    "EXPERT": {"score": 900_000, "rating": 180},
                }
            }
        },
    }
    pool, skipped = R.song_pool("chr-1", max_level=28, owned_only=True, doc=doc, now_ms=10)
    assert [(s["music_id"], s["difficulty"]) for s in pool] == [("m1", "EXPERT"), ("m2", "HARD")]
    assert pool[0]["current"] == {"rating": 400, "score": 2_000_000, "difficulty": "HARD"}
    why = {s["music_id"]: s["why"] for s in skipped}
    assert set(why) == {"m4", "m5"}
    assert "own" in why["m4"] and "level 28" in why["m5"]
    # every song, owned or not
    pool, _ = R.song_pool("chr-1", max_level=40, owned_only=False, doc=doc, now_ms=10)
    assert {s["music_id"] for s in pool} == {"m1", "m2", "m4", "m5"}


def test_songs_ordered_by_estimated_chart_value():
    # a higher level raises every note's value; more notes raise the mean combo bonus
    assert R.chart_scale(27, 4, 1000) > R.chart_scale(25, 4, 1000)
    assert R.chart_scale(27, 4, 1200) > R.chart_scale(27, 4, 600)
    assert abs(R.chart_scale(5, 4, 1) - 1.0) < 1e-12


def test_next_border():
    borders = [
        {"rank": 1, "rating": 1700},
        {"rank": 10, "rating": 1620},
        {"rank": 100, "rating": 1490},
        {"rank": 1000, "rating": 1300},
    ]
    assert R.next_border(borders, 57, 1450) == {"rank": 10, "rating": 1620, "gap": 170}
    assert R.next_border(borders, None, 1200) == {"rank": 1000, "rating": 1300, "gap": 100}
    assert R.next_border(borders, 1, 1750) is None
    assert R.next_border([], 57, 1450) is None


def test_predicted_keeps_records_and_marks_new_songs():
    current = {"m1": {"rating": 435}, "m2": {"rating": 521}, "m3": {"rating": 440}}
    p = R.predicted(current, {"m4": 600, "m2": 500})
    assert p["now"] == 1396 and p["after"] == 1561 and p["gain"] == 165
    assert p["top"] == [
        {"music_id": "m4", "rating": 600, "new": True},
        {"music_id": "m2", "rating": 521, "new": False},
        {"music_id": "m3", "rating": 440, "new": False},
    ]


class _Engine:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.released = 0

    def search(self, song, filters, **kw):
        self.calls.append((song, kw["objective"]))
        return {"ranking": self.rows[song], "seconds": 1.0}

    def release(self):
        self.released += 1


def test_each_song_rates_each_songs_best_team_by_its_max():
    rows = {
        "m1": [{"leader": "L", "members": ["a"], "best": 2_000_000.0, "song_max": 2_500_000.0}],
        "m2": [{"leader": "L", "members": ["b"], "best": 1_000_000.0, "song_max": 1_100_000.0}],
    }
    eng = _Engine(rows)
    events = []
    opts = {
        "songs": [{"music_id": "m1", "difficulty": "EXPERT"}, {"music_id": "m2", "difficulty": "HARD"}],
        "current": {"m2": {"rating": 300}},
        "objective": "max",
        "teams": 1,
        "filters": None,
    }
    out = R.run_each_song(opts, engine_for=lambda d: eng, emit=events.append)
    assert eng.released == 2
    assert sorted(eng.calls) == [("m1", "max"), ("m2", "max")]
    assert [s["rating"] for s in out["songs"]] == [500, 220]
    assert out["songs"][1]["gain"] == 0  # 220 is below the record of 300
    assert out["predicted"]["after"] == 800
    # grouped by difficulty (one engine each), EXPERT before HARD
    assert [e["song"]["music_id"] for e in events if e["type"] == "song"] == ["m1", "m2"]
