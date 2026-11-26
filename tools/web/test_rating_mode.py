"""The Holomem Rating goal's song selection and account figures (`rating_mode.py`)."""

from __future__ import annotations

import dataclasses

import pytest
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


def test_the_chart_factor_k_reorders_the_songs():
    """`MusicDifficultyChart` field 7 multiplies every note: a lower level with K above 1
    can out-value a higher one, and K below 1 lowers a chart."""
    assert R.chart_scale(26, 5, 1000, 10000) == R.chart_scale(26, 5, 1000)
    assert R.chart_scale(26, 5, 1000, 10855) == pytest.approx(R.chart_scale(26, 5, 1000) * 1.0855)
    assert R.chart_scale(26, 5, 1000, 9807) < R.chart_scale(26, 5, 1000)
    assert R.chart_scale(25, 5, 1000, 10855) > R.chart_scale(27, 5, 1000)


def test_song_pool_orders_by_the_chart_factor_k(monkeypatch):
    songs = [
        rating.RatingSong("m1", ("chr-1",), "solo", True),
        rating.RatingSong("m2", ("chr-1",), "solo", True),
    ]
    charts = {
        "m1": {"EXPERT": _chart(27)},
        "m2": {"EXPERT": {**_chart(26), "k": 10855}},
    }
    _fake(monkeypatch, songs, charts)
    doc = {"owned_songs": ["m1", "m2"], "records": {}}
    pool, _ = R.song_pool("chr-1", max_level=40, owned_only=True, doc=doc, now_ms=10)
    assert [s["music_id"] for s in pool] == ["m2", "m1"]
    # without K the higher level leads
    charts["m2"]["EXPERT"]["k"] = 10000
    pool, _ = R.song_pool("chr-1", max_level=40, owned_only=True, doc=doc, now_ms=10)
    assert [s["music_id"] for s in pool] == ["m1", "m2"]


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


class _MergedEngine:
    def __init__(self, rows):
        self.rows = rows
        self.released = 0

    def search_merged(self, filters, **kw):
        return {"rows": self.rows}

    def release(self):
        self.released += 1


def test_one_team_says_what_each_song_adds_to_the_rating():
    """One team: each song's `gain` is what it alone adds to the holomem's rating (her three
    best songs), as "A team per song" reports it, not the distance above her record."""
    current = {"m1": {"rating": 33}, "m2": {"rating": 510}, "m3": {"rating": 572}, "m4": {"rating": 540}}
    rows = [
        {
            "leader": "L",
            "members": ["a"],
            "charts": [{"key": "m1/EXPERT", "rating": 580}, {"key": "m2/EXPERT", "rating": 500}],
        }
    ]
    opts = {
        "songs": [{"music_id": "m1", "difficulty": "EXPERT"}, {"music_id": "m2", "difficulty": "EXPERT"}],
        "current": current,
        "objective": "max",
        "one_board": False,
        "filters": None,
    }
    eng = _MergedEngine(rows)
    out = R.run_one_team(opts, engine_for=lambda keys, cast: eng, emit=lambda e: None)
    assert eng.released == 1
    m1, m2 = out["rows"][0]["charts"]
    # top three 572 + 540 + 510 = 1622; 580 pushes 510 out: +70, not 580 - 33 = 547
    assert (m1["current_rating"], m1["gain"]) == (33, 70)
    # 500 is below her record on m2 and below her top three: nothing
    assert m2["gain"] == 0


@dataclasses.dataclass(frozen=True)
class _Filters:
    leader_card: str = ""
    require: tuple = ()


class _ChartEngine:
    """`search` answers by (difficulty, pinned?): a list of (leader, members, max) rows."""

    def __init__(self, book, diff, calls):
        self.book, self.diff, self.calls = book, diff, calls

    def search(self, song, filters, **kw):
        pinned = bool(filters.require)
        self.calls.append((song, self.diff, pinned))
        rows = self.book[(song, self.diff, pinned)]
        return {
            "ranking": [
                {"leader": L, "members": list(m), "best": v * 0.95, "song_max": v} for L, m, v in rows
            ]
        }

    def served_value(self, song, leader, members, assumptions=None, objective="max"):
        """A maxed engine's direct Max of one team (`("direct")` in the book), else None: the
        check then searches pinned to the team."""
        got = self.book.get((song, self.diff, "direct"))
        if got is not None:
            self.calls.append((song, self.diff, "direct"))
        return got

    def release(self):
        pass


def _run_checked(book, songs, current=None):
    calls, events = [], []
    opts = {"songs": songs, "current": current or {}, "objective": "max", "teams": 1, "filters": _Filters()}
    out = R.run_each_song(opts, engine_for=lambda d: _ChartEngine(book, d, calls), emit=events.append)
    return out, calls, events


def test_a_songs_other_chart_replaces_it_when_it_rates_higher():
    """Rating each: after the nominal pass, a song with a contender is probed there with its #1
    team, searched in full when the probe passes the gate, and sent again on the other chart
    when that chart's #1 rates higher, with its own difficulty and level."""
    song = {
        "music_id": "m1",
        "difficulty": "EXPERT",
        "level": 26,
        "contender": {"difficulty": "HARD", "level": 18},
    }
    book = {
        ("m1", "EXPERT", False): [("L", ("a",), 4_795_398)],  # rating 960
        ("m1", "HARD", True): [("L", ("a",), 5_082_031)],  # the probe: 1017
        ("m1", "HARD", False): [("L", ("b",), 5_096_933)],  # 1020
    }
    out, calls, events = _run_checked(book, [song])
    s = out["songs"][0]
    assert (s["difficulty"], s["level"], s["rating"]) == ("HARD", 18, 1020)
    assert s["rows"][0]["members"] == ["b"]
    assert s["chart_check"] == {
        "difficulty": "EXPERT",
        "level": 26,
        "other": "HARD",
        "other_level": 18,
        "rating": 960,
        "searched": True,
        "wins": True,
        "probe_rating": 1017,
        "other_rating": 1020,
    }
    assert calls == [("m1", "EXPERT", False), ("m1", "HARD", True), ("m1", "HARD", False)]
    sent = [e["song"]["difficulty"] for e in events if e["type"] == "song"]
    assert sent == ["EXPERT", "HARD"]
    assert out["predicted"]["after"] == 1020


def test_a_probe_below_the_gate_costs_no_full_search():
    song = {
        "music_id": "m1",
        "difficulty": "EXPERT",
        "level": 26,
        "contender": {"difficulty": "HARD", "level": 22},
    }
    book = {
        ("m1", "EXPERT", False): [("L", ("a",), 4_000_000)],
        ("m1", "HARD", True): [("L", ("a",), 4_000_000 / (1 + R.CHART_SEARCH_GAP) - 1)],
    }
    out, calls, events = _run_checked(book, [song])
    s = out["songs"][0]
    assert s["difficulty"] == "EXPERT" and s["chart_check"]["searched"] is False
    assert ("m1", "HARD", False) not in calls
    assert [e["song"]["difficulty"] for e in events if e["type"] == "song"] == ["EXPERT"]


def test_a_tie_keeps_the_nominal_chart_and_no_contender_no_check():
    tie = {
        "music_id": "m1",
        "difficulty": "EXPERT",
        "level": 26,
        "contender": {"difficulty": "HARD", "level": 22},
    }
    plain = {"music_id": "m2", "difficulty": "EXPERT", "level": 27}
    book = {
        ("m1", "EXPERT", False): [("L", ("a",), 4_000_000)],  # 800
        ("m1", "HARD", True): [("L", ("a",), 3_999_000)],
        ("m1", "HARD", False): [("L", ("b",), 4_000_000)],  # 800 as well
        ("m2", "EXPERT", False): [("L", ("c",), 3_000_000)],
    }
    out, calls, _events = _run_checked(book, [tie, plain])
    a, b = out["songs"]
    assert a["difficulty"] == "EXPERT" and a["chart_check"]["searched"] and not a["chart_check"]["wins"]
    assert "chart_check" not in b and all(c[0] != "m2" or c[1] == "EXPERT" for c in calls)


def test_a_direct_probe_costs_no_pinned_search():
    """Where a search serves the plain develop (objective Max, maxed), the nominal #1 is scored on
    the other chart directly; a full search that beats it is served as it is."""
    song = {
        "music_id": "m1",
        "difficulty": "EXPERT",
        "level": 26,
        "contender": {"difficulty": "HARD", "level": 18},
    }
    book = {
        ("m1", "EXPERT", False): [("L", ("a",), 4_795_398)],  # 960
        ("m1", "HARD", "direct"): 5_082_031,  # 1017
        ("m1", "HARD", False): [("L", ("b",), 5_096_933)],  # 1020
    }
    out, calls, _events = _run_checked(book, [song])
    s = out["songs"][0]
    assert (s["difficulty"], s["rating"], s["rows"][0]["members"]) == ("HARD", 1020, ["b"])
    assert s["chart_check"]["probe_rating"] == 1017
    assert calls == [("m1", "EXPERT", False), ("m1", "HARD", "direct"), ("m1", "HARD", False)]


def test_the_other_charts_number_one_is_the_better_of_its_search_and_the_probe():
    """A full search can settle below a team already scored there: the probe's team is then
    the other chart's #1, with its own row (a direct probe has none: searched pinned for it)."""
    song = {
        "music_id": "m1",
        "difficulty": "EXPERT",
        "level": 26,
        "contender": {"difficulty": "HARD", "level": 18},
    }
    book = {
        ("m1", "EXPERT", False): [("L", ("a",), 4_795_398)],  # 960
        ("m1", "HARD", "direct"): 4_880_000,  # 976
        ("m1", "HARD", True): [("L", ("a",), 4_880_000)],
        ("m1", "HARD", False): [("L", ("b",), 4_845_000)],  # 969
    }
    out, calls, _events = _run_checked(book, [song])
    s = out["songs"][0]
    assert (s["difficulty"], s["rating"], s["rows"][0]["members"]) == ("HARD", 976, ["a"])
    assert s["chart_check"]["other_rating"] == 976 and s["chart_check"]["wins"]
    assert calls[-1] == ("m1", "HARD", True)


def test_the_gate_is_the_nominal_ratings_border_not_its_value():
    """Another chart changes a song only above the nominal #1's rating border (ceil to 5,000):
    a probe within the gap of the nominal value but not of that border earns no full search."""
    song = {
        "music_id": "m1",
        "difficulty": "EXPERT",
        "level": 26,
        "contender": {"difficulty": "HARD", "level": 22},
    }
    book = {
        ("m1", "EXPERT", False): [("L", ("a",), 4_000_001)],  # 801: the border is 4,005,000
        ("m1", "HARD", "direct"): 3_955_000,  # x 1.0125 = 4,004,437: above the value, below the border
    }
    out, calls, _events = _run_checked(book, [song])
    assert out["songs"][0]["chart_check"]["searched"] is False
    assert ("m1", "HARD", False) not in calls
