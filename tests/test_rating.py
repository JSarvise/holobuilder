"""Holomem Rating arithmetic (`hbuilder.domain.rating`)."""

from __future__ import annotations

import pytest

from hbuilder.domain import rating as R


@pytest.mark.parametrize(
    ("score", "expected"),
    [(0, 0), (-5, 0), (1, 1), (5000, 1), (5001, 2), (852977, 171), (2417185, 484), (1205601, 242)],
)
def test_song_rating_is_ceil_over_5000(score, expected):
    assert R.song_rating(score) == expected


def test_needed_score_is_the_first_score_of_a_step():
    for r in (1, 2, 171, 484):
        assert R.song_rating(R.needed_score(r)) == r
        assert R.song_rating(R.needed_score(r) - 1) == r - 1
    assert R.needed_score(0) == 0


def test_best_difficulty_per_song_then_top_three():
    records = [
        ("m1", "EXPERT", 435),
        ("m1", "MASTER", 138),  # the same song: only its best difficulty counts
        ("m2", "MASTER", 521),
        ("m3", "EXPERT", 440),
        ("m4", "EXPERT", 173),
    ]
    per_song = R.best_per_song(records)
    assert per_song == {"m1": 435, "m2": 521, "m3": 440, "m4": 173}
    assert R.top_songs(per_song) == [("m2", 521), ("m3", 440), ("m1", 435)]
    assert R.holomem_rating(per_song) == 1396


def test_a_record_only_rises():
    current = {"m1": 435, "m2": 521, "m3": 440, "m4": 173}
    assert R.rating_with(current, {"m4": 100}) == 1396
    assert R.gain(current, "m4", 100) == 0
    # the fourth song overtakes the third
    assert R.gain(current, "m4", 450) == 15
    # a new run of a counted song replaces its old rating
    assert R.gain(current, "m2", 530) == 9
    # a song with no record yet
    assert R.gain({}, "m9", 300) == 300


def test_counts_for_solo_and_group_but_not_all():
    solo = R.RatingSong("m1", ("chr-1",), "solo", True)
    group = R.RatingSong("m2", ("chr-1", "chr-2"), "group", True)
    every = R.RatingSong("m3", ("chr-1", "chr-2", "chr-3"), "all", False)
    unflagged = R.RatingSong("m4", ("chr-1",), "solo", False)
    future = R.RatingSong("m5", ("chr-1",), "solo", True, start_ms=2_000)
    assert R.counts_for(solo, "chr-1")
    assert R.counts_for(group, "chr-2")
    assert not R.counts_for(group, "chr-3")
    assert not R.counts_for(every, "chr-1")
    assert not R.counts_for(unflagged, "chr-1")
    assert R.counts_for(future, "chr-1", now_ms=3_000)
    assert not R.counts_for(future, "chr-1", now_ms=1_000)


def test_pick_chart_takes_the_highest_level_within_reach():
    levels = {"EASY": 6, "NORMAL": 12, "HARD": 18, "EXPERT": 25, "MASTER": 29}
    assert R.pick_chart(levels, 30) == "MASTER"
    assert R.pick_chart(levels, 28) == "EXPERT"
    assert R.pick_chart(levels, 25) == "EXPERT"
    assert R.pick_chart(levels, 5) is None
    # a tie on level goes to the harder difficulty
    assert R.pick_chart({"EXPERT": 26, "MASTER": 26}, 30) == "MASTER"


def test_merged_value_ranks_by_rating_then_mean():
    songs = ("m1", "m2", "m3", "m4")
    # same rating, higher mean wins
    a = R.merged_value([1_000_000, 1_000_000, 1_000_000, 10], songs)
    b = R.merged_value([1_000_000, 1_000_000, 1_000_000, 900_000], songs)
    assert R.merged_rating(a) == R.merged_rating(b) == 600
    assert b > a
    # one rating step outweighs any mean difference
    c = R.merged_value([1_000_001 + 4_999, 1_000_000, 1_000_000, 0], songs)
    assert R.merged_rating(c) == 601
    assert c > b
    # the current records count: a team that only matches them gains nothing
    current = {"m1": 700, "m2": 700, "m3": 700}
    assert R.merged_rating(R.merged_value([1_000_000] * 4, songs, current)) == 2100
    with pytest.raises(ValueError):
        R.merged_value([1.0], songs)
