"""A missing chart file is named, with what builds it, wherever a search, a row's details or an
event view first needs the charts: never an AttributeError on a library that is not there."""

from __future__ import annotations

from pathlib import Path

import leaf_inputs
import pytest

from hbuilder.errors import MissingInput


def test_a_missing_chart_file_names_the_file_and_the_refresh(tmp_path):
    missing = tmp_path / "charts.json"
    with pytest.raises(MissingInput) as caught:
        leaf_inputs.chart_library(missing)
    message = str(caught.value)
    assert "charts.json" in message and str(tmp_path) in message
    assert "refresh.cmd" in message and "refresh.sh" in message


def test_a_present_chart_file_is_read(tmp_path):
    path = tmp_path / "charts.json"
    path.write_text('{"schema_version": 1, "charts": {}}', encoding="utf-8")
    assert leaf_inputs.chart_library(path).music_ids == ()


def test_a_search_names_every_missing_input_and_the_refresh(monkeypatch, tmp_path):
    from hbweb import config, jobs

    monkeypatch.setattr(config, "CAPS_PATH", tmp_path / "card_caps.json")
    monkeypatch.setattr(config, "INVENTORY_PATH", tmp_path / "inventory.json")
    with pytest.raises(MissingInput) as caught:
        jobs.require_search_inputs("account", materials=True)
    message = str(caught.value)
    assert "card_caps.json" in message and "inventory.json" in message and "refresh.cmd" in message


# what only the account basis reads (`config.missing_search_inputs`)
ACCOUNT_FILES = {
    "owned.json",
    "collection.json",
    "account_snapshot_main.json",
    "board_state.json",
    "inventory.json",
    "skill_ladder.json",
    "master_index.json",
}


def _inputs_under(monkeypatch, tmp_path, *, absent: set[str]) -> None:
    """Every `*_PATH` file of `config` moved under `tmp_path`, each on disk unless its name is in
    `absent`: the check then sees only this test's files, on any PC and in a clean clone."""
    from hbweb import config

    for attr in dir(config):
        value = getattr(config, attr)
        if attr.endswith("_PATH") and isinstance(value, (Path, str)):  # BOARD_PATH is a relative str
            moved = tmp_path / attr / Path(value).name
            moved.parent.mkdir()
            if moved.name not in absent:
                moved.write_text("{}", encoding="utf-8")
            monkeypatch.setattr(config, attr, moved)


def test_the_maxed_basis_does_not_need_the_account_files(monkeypatch, tmp_path):
    from hbweb import jobs

    _inputs_under(monkeypatch, tmp_path, absent=ACCOUNT_FILES)
    jobs.require_search_inputs("maxed", materials=False)
    jobs.require_search_inputs("maxed-account", materials=False)
    with pytest.raises(MissingInput) as caught:
        jobs.require_search_inputs("account", materials=False)
    for name in ACCOUNT_FILES - {"inventory.json"}:
        assert name in str(caught.value)


def test_a_file_that_only_drops_a_bonus_is_still_required(monkeypatch, tmp_path):
    """Without the master Poster table the maxed basis would score without its Memory Bonus,
    and without the skill rungs the account's cards would keep their catalogue skill levels:
    both change a score in silence, so the search refuses to start instead."""
    from hbweb import jobs

    _inputs_under(monkeypatch, tmp_path, absent={"main_Poster.json", "skill_ladder.json"})
    with pytest.raises(MissingInput, match="main_Poster.json"):
        jobs.require_search_inputs("maxed", materials=False)
    with pytest.raises(MissingInput, match="skill_ladder.json"):
        jobs.require_search_inputs("account", materials=False)


def test_the_maxed_bonuses_read_no_owned_file(monkeypatch, tmp_path):
    import ondemand

    monkeypatch.setattr(ondemand.leaf_inputs, "OWNED_PATH", tmp_path / "owned.json")
    ondemand.maxed_bonuses()  # would raise reading a missing owned.json
