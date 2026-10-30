"""`song.active_coverage` — the two visibility numbers the ranking row shows for a
song solve: how much of the song has SOME active live, and how much of the summed
active value the engine's MAX rule throws away when actives overlap.

Pure arithmetic on `timeline()`'s own windows, so it cannot disagree with the
picture. Tested on hand-built lanes, not on a real chart (the numbers are checkable
by hand)."""
from __future__ import annotations

import pytest

from hbuilder.domain.song import active_coverage


def _tl(song_seconds, lanes):
    return {"song_seconds": song_seconds, "lanes": [
        {"active": [dict(start=s, end=e, magnitude=m, probability_base=p)
                    for s, e, m, p in wins], "special": []}
        for wins in lanes]}


def test_no_actives_is_zero_cover_and_zero_loss():
    cov = active_coverage(_tl(100.0, [[], []]))
    assert cov == {"active_cover_pct": 0.0, "active_overlap_loss_pct": 0.0,
                   "active_dead_start_s": 100.0}


def test_disjoint_windows_cover_adds_and_lose_nothing():
    # 0-10 and 20-30 of a 100 s song: 20 % covered, no overlap.
    cov = active_coverage(_tl(100.0, [[(0, 10, 100, 1.0)], [(20, 30, 50, 1.0)]]))
    assert cov["active_cover_pct"] == pytest.approx(20.0)
    assert cov["active_overlap_loss_pct"] == pytest.approx(0.0)
    assert cov["active_dead_start_s"] == pytest.approx(0.0)


def test_full_overlap_pays_only_the_strongest():
    # two windows exactly on top of each other, both certain: the sum would be
    # 150, the max is 100 -> a third of the summed value is lost.
    cov = active_coverage(_tl(100.0, [[(10, 20, 100, 1.0)], [(10, 20, 50, 1.0)]]))
    assert cov["active_cover_pct"] == pytest.approx(10.0)
    assert cov["active_overlap_loss_pct"] == pytest.approx(100.0 / 3.0)
    assert cov["active_dead_start_s"] == pytest.approx(10.0)


def test_probability_weights_the_expected_max():
    # p=0.5 each, equal magnitude 100 on the same window: E[max] = 100*(1-0.25) = 75,
    # sum = 100 -> 25 % lost.
    cov = active_coverage(_tl(100.0, [[(0, 10, 100, 0.5)], [(0, 10, 100, 0.5)]]))
    assert cov["active_overlap_loss_pct"] == pytest.approx(25.0)


def test_windows_past_the_end_are_clamped_and_none_probability_counts_as_certain():
    cov = active_coverage(_tl(50.0, [[(40, 70, 100, None)]]))
    assert cov["active_cover_pct"] == pytest.approx(20.0)
    assert cov["active_overlap_loss_pct"] == pytest.approx(0.0)
