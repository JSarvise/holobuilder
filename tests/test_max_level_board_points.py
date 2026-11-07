"""`hbuilder.inputs.max_level_board_points`: the Holomem Board points a max-level holomem
holds, read from the master's `CharacterLevel` export (the maxed basis' board budget)."""

from __future__ import annotations

import json
import sys

import pytest

from hbuilder import paths
from hbuilder.domain.skilltree import TOTAL_BOARD_POINTS
from hbuilder.errors import MissingInput
from hbuilder.inputs import CHARACTER_LEVEL_PATH, max_level_board_points


def _row(group: str, level: int, points: int | None) -> dict:
    data = {"groupId": group, "level": level}
    if points is not None:
        data["skillTreePointQuantity"] = points
    return {"group_id": group, "level": level, "data": data}


def test_the_points_of_every_level_of_the_group_are_summed(tmp_path):
    path = tmp_path / "main_CharacterLevel.json"
    path.write_text(json.dumps([
        _row("level-group-1", 1, None),       # level 1 grants nothing (the field is elided)
        _row("level-group-1", 2, 5),
        _row("level-group-1", 3, 7),
        _row("level-group-2", 2, 100),        # another group's levels are not counted
    ]), encoding="utf-8")
    assert max_level_board_points(path) == 12
    assert max_level_board_points(path, group="level-group-2") == 100


def test_the_wrapped_export_shape_reads_the_same(tmp_path):
    path = tmp_path / "main_CharacterLevel.json"
    path.write_text(json.dumps({"rows": [_row("level-group-1", 2, 5), _row("level-group-1", 3, 7)]}),
                    encoding="utf-8")
    assert max_level_board_points(path) == 12


def test_an_absent_unreadable_or_groupless_export_is_a_missing_input(tmp_path):
    with pytest.raises(MissingInput):
        max_level_board_points(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(MissingInput):
        max_level_board_points(bad)
    other = tmp_path / "other.json"
    other.write_text(json.dumps([_row("level-group-2", 2, 5)]), encoding="utf-8")
    with pytest.raises(MissingInput):
        max_level_board_points(other)


def test_the_real_export_is_the_master_curve_at_max_level_and_below_the_whole_tree():
    """The export against the refresh's own reader of the master (`tree_budget`, the
    SQLCipher master when the game is installed), and below what the whole tree costs."""
    if not (paths.REPO / CHARACTER_LEVEL_PATH).is_file():
        pytest.skip("the master export is absent (the refresh writes it)")
    if str(paths.REFRESH_DIR) not in sys.path:
        sys.path.insert(0, str(paths.REFRESH_DIR))
    import tree_budget
    import tree_optimise

    points = max_level_board_points()
    assert points == tree_budget.budget_curve()[tree_optimise.MAX_CHARACTER_LEVEL]
    assert 0 < points < TOTAL_BOARD_POINTS
