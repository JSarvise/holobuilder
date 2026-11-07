"""Board prices on a PC without the game's `sqlcipher.dll`: `tree_optimise._prices` reads the
synced master export `holodori/main_SkillTreeNode.json`, and it gives the master's prices."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hbuilder.errors import MissingInput

_REPO = Path(__file__).resolve().parents[1]
_TREE_OPTIMISE = _REPO / "tools" / "refresh" / "tree_optimise.py"


def _load():
    spec = importlib.util.spec_from_file_location("tree_optimise", _TREE_OPTIMISE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tree_optimise"] = module
    spec.loader.exec_module(module)
    return module


try:
    _TREE = _load()
except MissingInput as exc:  # gitignored account data missing
    _TREE, _WHY = None, str(exc)
else:
    _WHY = ""
needs_tree = pytest.mark.skipif(_TREE is None, reason=_WHY)


@needs_tree
def test_the_export_keeps_every_consumption_and_the_points(tmp_path):
    rows = [
        {"group_id": "R-001", "data": {"groupId": "R-001", "consumptionSkillTreePointQuantity": 3,
                                       "consumptions": [{"resourceId": "item-a", "quantity": "200"},
                                                        {"resourceId": "item-b", "quantity": "5"},
                                                        {"resourceId": "item-a", "quantity": "1"}]}},
        {"group_id": "R-001", "data": {"groupId": "R-001", "consumptionSkillTreePointQuantity": 9}},
        {"group_id": "S-001", "data": {"groupId": "S-001"}},
    ]
    path = tmp_path / "main_SkillTreeNode.json"
    path.write_text(json.dumps(rows), "utf8")
    assert _TREE._prices_from_export(path) == {
        "R-001": {"points": 3, "item-a": 201, "item-b": 5}, "S-001": {"points": 0}}


@needs_tree
def test_without_the_dll_the_prices_come_from_the_export(monkeypatch):
    monkeypatch.setattr(_TREE, "_master_readable", lambda: False)
    monkeypatch.setattr(_TREE, "_prices_from_export", lambda: {"from": "export"})
    assert _TREE._prices() == {"from": "export"}


@needs_tree
def test_the_export_gives_the_masters_prices():
    export = _TREE.paths.HOLODORI / "main_SkillTreeNode.json"
    if not _TREE._master_readable() or not export.exists():
        pytest.skip("needs the game's sqlcipher.dll and the synced export")
    assert _TREE._prices_from_export() == _TREE._prices()


def _tree_budget():
    sys.path.insert(0, str(_REPO / "tools" / "refresh"))
    import tree_budget  # noqa: PLC0415

    return tree_budget


def test_the_level_grants_export_reads_skill_tree_points_per_level(tmp_path):
    rows = [{"data": {"groupId": "level-group-1", "level": 1}},
            {"data": {"groupId": "level-group-1", "level": 2, "skillTreePointQuantity": 3}},
            {"data": {"groupId": "level-group-2", "level": 2, "skillTreePointQuantity": 9}}]
    path = tmp_path / "main_CharacterLevel.json"
    path.write_text(json.dumps(rows), "utf8")
    assert _tree_budget().level_grants_from_export(path=path) == {1: 0, 2: 3}


def test_the_level_grants_export_gives_the_masters_grants():
    tb = _tree_budget()
    import master_cache  # noqa: PLC0415

    export = tb.paths.HOLODORI / "main_CharacterLevel.json"
    if not master_cache.can_decrypt() or not export.exists():
        pytest.skip("needs the game's sqlcipher.dll and the synced export")
    assert tb.level_grants_from_export() == tb.level_grants()
