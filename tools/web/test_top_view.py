"""Top 100 (`top_view`): the board's last-hour movement and a player's series, from reads built
in memory. No files: test_no_writes.py walks this file too."""
from __future__ import annotations

import top_view as tv

H = 3_600_000


def read(t: int, rows: list[list]) -> dict:
    return {"captured_at": t, "rows": rows}


READS = [
    read(0, [[1, 1000, "A", "Ann"], [2, 900, "B", "Bo"], [3, 800, "C", "Cy"]]),
    read(H, [[1, 1500, "A", "Ann"], [2, 1200, "C", "Cy"], [3, 900, "B", "Bo"]]),
    read(H + 60_000, [[1, 1500, "A", "Ann"], [2, 1300, "C", "Cy"], [3, 900, "B", "Bo"]]),
    read(2 * H, [[1, 1700, "A", "Ann"], [2, 1400, "C", "Cy"], [3, 950, "D", "Di"]]),
]


def test_board_is_the_last_read_with_its_last_hour():
    rows = {r["uid"]: r for r in tv.board(READS)}
    assert [r["uid"] for r in tv.board(READS)] == ["A", "C", "D"]
    assert rows["A"]["gain_1h"] == 200 and rows["A"]["rank_1h"] == 0
    assert rows["C"]["gain_1h"] == 200 and rows["C"]["rank_1h"] == 0
    # gains are counted per READ that moved: C moved at H+1min and at 2H
    assert rows["C"]["reads_with_gain_1h"] == 2 and rows["C"]["last_gain_at"] == 2 * H
    # D was not in the top 100 an hour ago: no movement can be claimed
    assert rows["D"]["gain_1h"] is None and rows["D"]["rank_1h"] is None


def test_pace_needs_an_hour_of_reads():
    assert tv.board(READS[:1])[0]["pace_h"] is None
    a = next(r for r in tv.board(READS) if r["uid"] == "A")
    assert a["pace_h"] == 350        # (1700 - 1000) over 2 h, from the oldest read in the 3 h window


def test_rank_change_counts_up_as_positive():
    rows = {r["uid"]: r for r in tv.board(READS[:2])}
    assert rows["C"]["rank_1h"] == 1 and rows["B"]["rank_1h"] == -1


def test_player_series_only_while_in_the_top():
    assert tv.player_series(READS, "B") == [[0, 900], [H, 900], [H + 60_000, 900]]
    assert tv.player_series(READS, "Z") == []



def test_build_top_keeps_each_rankings_boards_apart(monkeypatch):
    # one chapter file holds the Event Pt board and its song's board (Song Score
    # shows the song's top 100); `kind` picks one, the main ranking by default
    from pathlib import Path
    reads = [{"captured_at": 5, "kind": "points", "rows": [[1, 900, "A", "a"]]},
             {"captured_at": 6, "kind": "music:m1", "rows": [[1, 3_000_000, "B", "b"]]}]
    monkeypatch.setattr(tv, "top_files", lambda store, region: [Path("c1.top.jsonl")])
    monkeypatch.setattr(tv, "read_top", lambda path: reads)
    monkeypatch.setattr(tv.cutoffs_view, "list_chapters", lambda store, region: [
        {"chapter_id": "c1", "start_time": 0, "chapter_end_time": 10}])
    pts = tv.build_top(Path("."), "us", now=20, chapter_id="c1")
    assert pts["kind"] == "points" and [r["uid"] for r in pts["rows"]] == ["A"]
    assert pts["kinds"] == ["points", "music:m1"]
    song = tv.build_top(Path("."), "us", now=20, chapter_id="c1", kind="music:m1")
    assert [r["score"] for r in song["rows"]] == [3_000_000]
