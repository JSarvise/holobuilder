"""The account merge into state/collection.json: a snapshot without cards is refused (an account
always owns cards, so an empty list is a failed read that would empty the collection), and every
merge backs up the file it is about to change."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import account_snapshot as snapshot  # noqa: E402

ORIGINAL = {"schema_version": "collection-1", "cards": {"card-a": {"level": 3}}, "player": {"level": 31}}


def _account(tmp_path, monkeypatch) -> tuple[Path, Path]:
    state, dumps = tmp_path / "state", tmp_path / "dumps"
    state.mkdir()
    (state / "collection.json").write_text(json.dumps(ORIGINAL), "utf8")
    monkeypatch.setattr(snapshot, "STATE", state)
    monkeypatch.setattr(snapshot, "DUMPS", dumps)
    return state / "collection.json", dumps


def _snap(*levels: int) -> dict:
    return {"cards": [{"card_id": f"card-{i}", "level": lv} for i, lv in enumerate(levels)], "posters": []}


def test_a_snapshot_without_cards_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    collection, dumps = _account(tmp_path, monkeypatch)
    before = collection.read_bytes()
    assert snapshot.merge_collection({"cards": [], "posters": []}) is False
    assert snapshot.merge_collection({}) is False
    assert collection.read_bytes() == before and not dumps.exists()


def test_every_merge_backs_up_the_file_it_changes(tmp_path, monkeypatch):
    collection, dumps = _account(tmp_path, monkeypatch)
    monkeypatch.setattr(snapshot, "COLLECTION_BACKUPS_KEPT", 2)
    states = []
    for levels in ((10,), (20,), (30,)):
        states.append(collection.read_text("utf8"))
        assert snapshot.merge_collection(_snap(*levels)) is True
    backups = sorted(
        (dumps / "collection_backups").glob("collection-*.json"), key=lambda p: p.stat().st_mtime_ns
    )
    # one per merge, the newest two kept, each the file as it was just before its merge
    assert [b.read_text("utf8") for b in backups] == states[1:]
    assert json.loads((dumps / "collection_before_import.json").read_text("utf8")) == ORIGINAL
    merged = json.loads(collection.read_text("utf8"))
    assert merged["cards"] == {"card-0": {"level": 30, "bloom": 0, "limit_break": 0}}
    assert merged["player"]["level"] == 31  # hand-entered, kept
