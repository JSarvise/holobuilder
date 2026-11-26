"""The per-chart coefficients read from the master, and the refresh's report of their changes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chart_coef as cc  # noqa: E402
import refresh_all  # noqa: E402


def test_coefficients_join_the_three_tables_and_default_k_to_one():
    music = [{"id": "m0405", "data": {17: [5]}}, {"id": "m0001", "data": {17: [4]}}]
    difficulty = [
        {"data": {1: ["m0405"], 2: [4], 3: [26], 4: ["chart_m0405_expert"]}},
        {"data": {1: ["m0001"], 2: [4], 3: [25], 4: ["chart_m0001_expert"]}},
    ]
    # field 7 on the new chart; the old chart's row predates it
    charts = [
        {"data": {1: ["m0405"], 2: [4], 3: [1267], 7: [10904]}},
        {"data": {1: ["m0001"], 2: [4], 3: [700]}},
    ]
    out = cc.coefficients_from_rows(music, difficulty, charts)
    assert out == {"chart_m0405_expert": (26, 5, 10904), "chart_m0001_expert": (25, 4, 10000)}


def test_a_cached_value_without_k_reads_as_one():
    assert cc.as_coef([26, 5]) == (26, 5, 10000)
    assert cc.as_coef([26, 5, 9807]) == (26, 5, 9807)
    with pytest.raises(ValueError):
        cc.as_coef([26])


def test_change_lines_name_each_moved_value_and_new_charts_off_one():
    before = {"chart_m0405_expert": (26, 5, 10904), "chart_m0001_expert": (25, 4, 10000)}
    after = {
        "chart_m0405_expert": (26, 5, 11000),
        "chart_m0001_expert": (26, 4, 10000),
        "chart_m0500_expert": (27, 5, 10200),
        "chart_m0501_expert": (27, 5, 10000),  # new at x1: not news
    }
    assert cc.change_lines(before, after) == [
        "chart level changed: m0001 EXPERT 25 -> 26",
        "chart K changed: m0405 EXPERT 10904 -> 11000",
        "new chart with K 10200: m0500 EXPERT",
    ]
    assert cc.change_lines(after, after) == []


def test_chart_file_maps_compare_alike():
    doc = {
        "charts": {
            "m0405/EXPERT": {
                "music_id": "m0405",
                "difficulty": "EXPERT",
                "difficulty_level": 26,
                "live_score_coefficient_permil": 5,
                "score_coefficient_permyriad": 10904,
            },
            "m0001/EXPERT": {
                "music_id": "m0001",
                "difficulty": "EXPERT",
                "difficulty_level": 25,
                "live_score_coefficient_permil": 4,
            },
        }
    }
    coef = cc.from_chart_file(doc)
    assert coef == {"m0405 EXPERT": (26, 5, 10904), "m0001 EXPERT": (25, 4, 10000)}
    moved = dict(coef, **{"m0405 EXPERT": (26, 5, 10950)})
    assert cc.change_lines(coef, moved) == ["chart K changed: m0405 EXPERT 10904 -> 10950"]


def test_unnamed_fields_are_named_per_table():
    schema = {
        "messages": {"MusicDifficultyChart": [{"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}], "Setting": [{"n": 1}]}
    }
    rows = {
        "MusicDifficultyChart": [{"data": {1: ["m0405"], 2: [4], 3: [1], 4: [1], 7: [10904]}}],
        "Setting": [{"data": {1: [2300]}}],
    }
    assert cc.unnamed_fields(rows, schema) == {"MusicDifficultyChart": [7]}
    lines = cc.unnamed_field_lines(rows, schema)
    assert len(lines) == 1 and "MusicDifficultyChart" in lines[0] and lines[0].endswith(": 7")


def test_the_committed_schema_names_field_7():
    import json

    schema = json.loads(cc.SCHEMA.read_text(encoding="utf-8"))
    numbers = {f["n"] for f in schema["messages"]["MusicDifficultyChart"]}
    assert numbers == {1, 2, 3, 4, 7}


def test_refresh_delta_reports_a_k_change(tmp_path):
    path = tmp_path / "charts_all.json"
    before = refresh_all.Snap(
        "charts_all.json coefficients", path, True, {"charts": 1}, coef={"m0405 EXPERT": (26, 5, 10904)}
    )
    after = refresh_all.Snap(
        "charts_all.json coefficients", path, True, {"charts": 1}, coef={"m0405 EXPERT": (26, 5, 11000)}
    )
    assert refresh_all.delta_lines(before, after) == [
        "charts_all.json coefficients: chart K changed: m0405 EXPERT 10904 -> 11000"
    ]
    # unchanged: the usual one-line "no change", with no per-chart rows
    (line,) = refresh_all.delta_lines(after, after)
    assert "no change" in line and "m0405" not in line
    # a first build lists the charts whose K is not x1
    created = refresh_all.delta_lines(refresh_all.Snap("x", path, False), after)
    assert created[1].endswith("new chart with K 11000: m0405 EXPERT")


def test_the_chart_step_probes_the_coefficients():
    from refresh_steps import STEPS

    (step,) = [s for s in STEPS if s.name == "chart_lib"]
    assert "chart_coefficients" in step.probes
    import argparse

    probes = refresh_all.probe_table(argparse.Namespace(account="main"))
    assert probes["chart_coefficients"] is refresh_all.probe_chart_coefficients
