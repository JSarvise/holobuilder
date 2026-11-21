"""Multi Live fever: the chart's section, the per-note weight and the chart builder's marker rule."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hbuilder.domain.song import _chart_weights
from hbuilder.domain.song_chart import (
    DEFAULT_FEVER_WINDOW,
    ChartNote,
    SongChart,
    SongError,
    charts_from_mapping,
)

CUES = (1.0, 2.0, 3.0, 4.0, 5.0)


def _chart(fever=(10.0, 20.0), times=(5.0, 10.0, 15.0, 20.0, 25.0)) -> SongChart:
    return SongChart(
        music_id="m0001",
        difficulty="EXPERT",
        difficulty_level=26,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=t, perfect_permil=1000) for t in times),
        special_cues=CUES,
        fever=fever,
    )


def test_a_note_on_the_start_is_outside_and_one_on_the_end_inside():
    # the client judges a note before it refreshes the section flag for the frame
    chart = _chart()
    assert [chart.in_fever(n.time) for n in chart.notes] == [False, False, True, True, False]


def test_the_fever_weight_multiplies_only_the_section_and_keeps_the_divisor():
    chart = _chart()
    plain, plain_sum = _chart_weights(chart, "mobile", False)
    multi, multi_sum = _chart_weights(chart, "mobile", False, 1.5)
    assert multi_sum == plain_sum
    assert [m / p for m, p in zip(multi, plain)] == [1.0, 1.0, 2.5, 2.5, 1.0]


def test_on_auto_the_weight_applies_after_the_auto_column():
    # the AUTO column maps 1000 -> 800; the fever pays on what the note scored
    chart = _chart()
    weights, _ = _chart_weights(chart, "mobile", True, 0.25)
    assert weights == pytest.approx((0.8, 0.8, 1.0, 1.0, 0.8))


def test_a_chart_without_a_section_refuses_a_multi_score():
    chart = _chart(fever=None)
    assert _chart_weights(chart, "mobile", False)[0]  # solo is fine
    with pytest.raises(SongError, match="no fever section"):
        _chart_weights(chart, "mobile", False, 1.5)


def test_a_section_that_does_not_end_after_it_starts_is_refused():
    with pytest.raises(SongError, match="does not end after it starts"):
        _chart(fever=(20.0, 20.0))


def test_the_chart_file_carries_the_section_and_an_old_file_reads_without_it():
    row = {
        "music_id": "m0001",
        "difficulty": "EXPERT",
        "difficulty_level": 26,
        "live_score_coefficient_permil": 4,
        "notes": [[1.0, 1000, 0], [2.0, 1000, 1]],
        "special_cues": list(CUES),
    }
    old = charts_from_mapping({"charts": {"m0001/EXPERT": row}})
    assert old.get("m0001").fever is None
    new = charts_from_mapping({"charts": {"m0001/EXPERT": {**row, "fever": [64.7853, 84.7848]}}})
    assert new.get("m0001").fever == (64.7853, 84.7848)


def _builder():
    refresh = Path(__file__).resolve().parents[1] / "tools" / "refresh"
    if str(refresh) not in sys.path:
        sys.path.insert(0, str(refresh))
    import build_charts  # noqa: PLC0415

    return build_charts


def _marks(*cells):
    return [
        {"channel": ch, "value": v, "beat": float(beat), "time": float(beat)} for ch, v, beat in cells
    ] + [{"channel": "0B", "value": "01", "beat": 0.5, "time": 0.5}]


def test_the_builder_reads_the_four_markers_in_order():
    bc = _builder()
    good = _marks(("0C", "01", 1), ("0C", "02", 2), ("0D", "01", 3), ("0D", "02", 4))
    assert bc.fever_window(good) == [3.0, 4.0]


@pytest.mark.parametrize(
    "cells",
    [
        # a point missing
        (("0C", "01", 1), ("0C", "02", 2), ("0D", "01", 3)),
        # a point twice: the client discards them all
        (("0C", "01", 1), ("0C", "02", 2), ("0D", "01", 3), ("0D", "01", 3.5), ("0D", "02", 4)),
        # out of order
        (("0C", "01", 1), ("0C", "02", 2), ("0D", "01", 5), ("0D", "02", 4)),
        # a cell other than 01/02
        (("0C", "01", 1), ("0C", "02", 2), ("0D", "01", 3), ("0D", "03", 4)),
    ],
)
def test_invalid_markers_fall_back_to_the_clients_default(cells):
    assert _builder().fever_window(_marks(*cells)) is None
    assert DEFAULT_FEVER_WINDOW == (60.0, 80.0)
