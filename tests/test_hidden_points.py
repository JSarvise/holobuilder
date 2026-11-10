"""The slides' invisible points in the divisor (`SongChart.hidden_points`).

The client builds a note for every hidden relay point and curve point of a slide and weighs
each as a LONG_RELAY (100 permil) in `GetNotesPerfectCoefficientSum`, although they are never
judged. Two independent checks: holodori.best's chart scale, computed by their own tool from
the raw `.sus` files, and one note read off a real run's score popup (m0528 EXPERT, note 1:
+1301). Real-data cases SKIP without the gitignored chart library."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from hbuilder.domain.song import HIDDEN_POINT_PERMIL, ChartNote, SongChart, load_charts

CHARTS = Path(__file__).resolve().parents[1] / "tools" / "recon" / "proxy" / "dumps" / "charts_all.json"


def _chart(hidden: int) -> SongChart:
    notes = (ChartNote(1.0, 1000), ChartNote(2.0, 1000, flick=True), ChartNote(3.0, 100))
    return SongChart("m9001", "EXPERT", 26, 1100, notes, (10.0, 20.0, 30.0, 40.0, 50.0), hidden_points=hidden)


def test_each_hidden_point_weighs_a_tick_in_the_divisor():
    assert HIDDEN_POINT_PERMIL == 100
    assert _chart(0).notes_perfect_coefficient_sum("mobile") == 2.15
    assert _chart(7).notes_perfect_coefficient_sum("mobile") == 2.85  # + 7 x 0.1
    assert _chart(7).notes_perfect_coefficient_sum("pc") == 2.80  # same on both columns


@pytest.fixture(scope="module")
def library():
    if not CHARTS.exists():
        pytest.skip("chart library absent")
    return load_charts(CHARTS)


def _scale(chart: SongChart) -> float:
    """sum of w(1 + combo) over the notes / the divisor: the chart's combo scale."""
    num = sum(
        n.perfect_permil_for("mobile") / 1000 * (1 + chart.combo_coefficient(i + 1))
        for i, n in enumerate(chart.notes)
    )
    return num / chart.notes_perfect_coefficient_sum("mobile")


@pytest.mark.parametrize("music, expected", [("m0001", 1.025913), ("m0334", 1.026039), ("m0155", 1.023345)])
def test_the_chart_scale_matches_an_independent_reading(library, music, expected):
    chart = library.get(music, "EXPERT")
    assert chart.hidden_points > 0
    assert round(_scale(chart), 6) == expected


def test_a_real_runs_first_note(library):
    """m0528 EXPERT: 878 notes and 194 hidden points. The note's value without them,
    1,335.59, was ours before; the game showed +1301."""
    chart = library.get("m0528", "EXPERT")
    assert (len(chart.notes), chart.hidden_points) == (878, 194)
    without = sum(n.perfect_permil_for("mobile") for n in chart.notes) / 1000
    note_one = 1335.593749978959 * without / chart.notes_perfect_coefficient_sum("mobile")
    assert math.ceil(note_one) == 1301
