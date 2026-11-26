"""The per-chart score factor K (`MusicDifficultyChart.scoreCoefficientPermyriadMultiply`).

`CalcScore` multiplies every note's value by `K = permyriad / 10000` after `(1 + Y)` and
before the ceil. `SongChart.music_coefficient()` carries it, so every per-note constant
built from it holds K inside the ceil.
"""

from __future__ import annotations

import dataclasses
import math

import pytest
from support.builders import make_card, make_scoring_config, make_skill

from hbuilder.domain.song import (
    ChartNote,
    OverallPower,
    SongChart,
    SongError,
    charts_from_mapping,
    score_song_detailed,
)


def _chart(permyriad: int = 10000) -> SongChart:
    return SongChart(
        music_id="m0405",
        difficulty="EXPERT",
        difficulty_level=26,
        live_score_coefficient_permil=5,
        notes=tuple(ChartNote(time=t, perfect_permil=1000) for t in (1.0, 21.0, 25.0, 41.0, 45.0, 61.0)),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
        score_coefficient_permyriad=permyriad,
    )


def _active(card_id: str, member: str):
    return make_card(
        card_id,
        member=member,
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            magnitude=50.0,
            duration=10.0,
            interval=20.0,
            probability_tier="Medium",
        ),
    )


MEMBERS = tuple(_active(f"m{i}", m) for i, m in enumerate(("Marine", "Nene", "Lui", "Calli", "Ina")))
LEADER = make_card("lead", member="Miko")


def test_music_coefficient_carries_k():
    base = 1.0 + 0.005 * (26 - 5)
    assert _chart().music_coefficient() == pytest.approx(base)
    assert _chart(10904).music_coefficient() == pytest.approx(base * 1.0904)
    assert _chart(9807).music_coefficient() == pytest.approx(base * 0.9807)  # K below 1


def test_a_chart_file_without_the_field_reads_k_as_one():
    row = {
        "music_id": "m0001",
        "difficulty": "EXPERT",
        "difficulty_level": 26,
        "live_score_coefficient_permil": 4,
        "notes": [[1.0, 1000, 0], [2.0, 1000, 0]],
        "special_cues": [1.0, 2.0, 3.0, 4.0, 5.0],
    }
    plain = charts_from_mapping({"charts": {"m0001/EXPERT": row}})
    assert plain.get("m0001", "EXPERT").score_coefficient_permyriad == 10000
    scaled = charts_from_mapping({"charts": {"m0001/EXPERT": {**row, "score_coefficient_permyriad": 10855}}})
    assert scaled.get("m0001", "EXPERT").score_coefficient_permyriad == 10855
    assert scaled.get("m0001", "EXPERT").music_coefficient() == pytest.approx(
        plain.get("m0001", "EXPERT").music_coefficient() * 1.0855
    )


def test_a_non_positive_k_is_refused():
    with pytest.raises(SongError):
        _chart(0)


@pytest.mark.parametrize("permyriad", [10904, 9807])
def test_each_note_value_scales_by_k_before_its_ceil(permyriad):
    cfg = make_scoring_config()
    power = OverallPower.fixed(20000)
    rows_plain: dict = {}
    rows_k: dict = {}
    plain = score_song_detailed(MEMBERS, (LEADER,), _chart(), cfg, deck_power=power, note_rows=rows_plain)[0]
    scaled = score_song_detailed(
        MEMBERS, (LEADER,), _chart(permyriad), cfg, deck_power=power, note_rows=rows_k
    )[0]
    k = permyriad / 10000.0
    assert plain.member_order == scaled.member_order
    for a, b in zip(rows_plain["lead"], rows_k["lead"], strict=True):
        assert b.base_value == pytest.approx(a.base_value * k, rel=1e-12)
        assert b.skill_rate == pytest.approx(a.skill_rate, rel=1e-12)
        # the ceil is taken on the value that already carries K, not on the plain value
        assert b.expected_value == math.ceil(b.base_value * (1.0 + b.skill_rate))
    assert scaled.total_score == pytest.approx(plain.total_score * k, rel=1e-3)


def test_k_is_a_field_of_the_chart_and_replace_keeps_the_rest():
    chart = _chart(10904)
    plain = dataclasses.replace(chart, score_coefficient_permyriad=10000)
    assert plain.notes == chart.notes
    assert chart.music_coefficient() / plain.music_coefficient() == pytest.approx(1.0904)
