"""The account planner's Oracle scores the chart its song id names: a bare music id is the
song's EXPERT chart, `"<music>/HARD"` its HARD chart (read from `charts_all.json`). A HARD
run's plan valued on EXPERT notes buys the wrong Act levels and nodes.

    python -m pytest tools/refresh/test_tree_optimise_chart.py
"""

from __future__ import annotations

import pytest
import tree_optimise as to

from hbuilder import paths


def _needs_dumps():
    for name in ("charts.json", "charts_all.json", "skilltree.json", "card_caps.json"):
        if not (paths.DUMPS / name).exists():
            pytest.skip(f"needs dumps/{name}")


def test_a_song_id_with_its_difficulty_reads_that_chart():
    _needs_dumps()
    hard = to.Oracle(pool_size=1, song="m0365/HARD", scenario="account", quiet=True)
    expert = to.Oracle(pool_size=1, song="m0365", scenario="account", quiet=True)
    assert (hard._chart.music_id, hard._chart.difficulty) == ("m0365", "HARD")
    assert (expert._chart.music_id, expert._chart.difficulty) == ("m0365", "EXPERT")
    assert len(hard._chart.notes) < len(expert._chart.notes)


def test_an_unknown_chart_is_a_named_refusal():
    _needs_dumps()
    with pytest.raises(to.MissingInput, match="no EASYISH chart"):
        to.Oracle(pool_size=1, song="m0365/EASYISH", scenario="account", quiet=True)
