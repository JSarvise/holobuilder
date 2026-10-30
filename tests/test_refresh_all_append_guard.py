"""`refresh_all.py`'s append-only guard on `data/cards.json`.

The catalogue is the GAME'S data rather than the user's, and the `add_cards`
step may append to it — so the guard has to distinguish "two cards were added"
from "one card's magnitude changed". A sha256 taken before and after the run
cannot say which happened.

These tests exist because the guard is the only thing standing between a bad
draft and the one hand-corrected file in the repo, and a guard that is never
seen to FAIL has not been tested at all — it has been assumed.

`tools/recon/proxy/` is outside `src/hbuilder/`, so it is imported via an
explicit `sys.path` insert.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_DIR = REPO_ROOT / "tools" / "recon" / "proxy"
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

import refresh_all  # noqa: E402


def _record(card_id: str, magnitude: float = 10.0) -> dict:
    return {"id": card_id, "member": "M", "magnitude": magnitude}


def _install(tmp_path, monkeypatch, cards: list[dict]) -> Path:
    path = tmp_path / "cards.json"
    path.write_bytes(
        json.dumps({"schema_version": "2", "cards": cards}, indent=2).encode("utf-8")
    )
    monkeypatch.setattr(refresh_all, "APPEND_ONLY", path)
    return path


def test_both_authoritative_files_are_append_only_and_neither_is_digest_guarded():
    """`owned.json` is not in the never-written guard.

    The refresh should NOTICE a new card and append it, so the file follows the
    same append-only contract the catalogue has: a whole new id may appear, an
    existing entry may never move. If it ever reappears in
    `SACRED` the merge fails every run for the wrong reason; if the guard is ever
    dropped entirely, nothing checks that an append stayed an append."""
    assert refresh_all.SACRED == ()
    assert refresh_all.APPEND_ONLY.name == "cards.json"
    assert refresh_all.APPEND_ONLY_OWNED.name == "owned.json"
    assert hasattr(refresh_all, "owned_guard")


def test_the_owned_guard_fails_when_a_card_disappears(tmp_path, monkeypatch):
    """The guard has to be SEEN to fail. Losing a card is the failure the whole
    append-only contract exists to prevent."""
    path = tmp_path / "owned.json"
    path.write_bytes(json.dumps({"owned": ["a", "b"], "schema_version": "2"}).encode())
    monkeypatch.setattr(refresh_all, "APPEND_ONLY_OWNED", path)

    lines, failed = refresh_all.owned_guard(["a", "b", "c"])

    assert failed is True
    assert "APPEND-ONLY" in lines[0]
    assert any("REMOVED  c" in line for line in lines)


def test_the_owned_guard_accepts_an_addition(tmp_path, monkeypatch):
    path = tmp_path / "owned.json"
    path.write_bytes(json.dumps({"owned": ["a", "b", "c"], "schema_version": "2"}).encode())
    monkeypatch.setattr(refresh_all, "APPEND_ONLY_OWNED", path)

    lines, failed = refresh_all.owned_guard(["a", "b"])

    assert failed is False
    assert "1 added, none removed" in lines[0]


def test_the_merge_refuses_to_write_a_file_it_did_not_format(tmp_path, monkeypatch):
    """A 1-line append must never become a whole-file reformat. If the file on
    disk is not byte-identical to this tool's own serialisation, it declines."""
    path = tmp_path / "owned.json"
    path.write_bytes(b'{"owned":["a"],"schema_version":"2"}')   # compact, not this tool's format
    monkeypatch.setattr(refresh_all, "APPEND_ONLY_OWNED", path)

    lines, failed = refresh_all.merge_owned("main")

    assert failed is True
    assert "refusing to" in " ".join(lines)
    assert path.read_bytes() == b'{"owned":["a"],"schema_version":"2"}'


def test_an_unchanged_catalogue_passes_and_says_how_it_was_verified(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, [_record("a"), _record("b")])
    before = refresh_all.catalogue_records()

    lines, failed = refresh_all.catalogue_guard(before)

    assert failed is False
    assert "unchanged" in lines[0]
    assert "record by record" in lines[0]


def test_appending_a_whole_new_card_passes_and_names_it(tmp_path, monkeypatch):
    path = _install(tmp_path, monkeypatch, [_record("a")])
    before = refresh_all.catalogue_records()
    path.write_bytes(
        json.dumps(
            {"schema_version": "2", "cards": [_record("a"), _record("b")]}, indent=2
        ).encode("utf-8")
    )

    lines, failed = refresh_all.catalogue_guard(before)

    assert failed is False
    assert any("grew by 1 card" in line for line in lines)
    assert any(line.strip() == "+ b" for line in lines)


def test_a_modified_existing_card_FAILS_and_names_the_card(tmp_path, monkeypatch):
    """The failure the whole guard exists for. `b` is legitimately appended in
    the same write, which is exactly when a modification is easiest to miss."""
    path = _install(tmp_path, monkeypatch, [_record("a", 10.0)])
    before = refresh_all.catalogue_records()
    path.write_bytes(
        json.dumps(
            {"schema_version": "2", "cards": [_record("a", 99.0), _record("b")]},
            indent=2,
        ).encode("utf-8")
    )

    lines, failed = refresh_all.catalogue_guard(before)

    assert failed is True
    assert any(line.split() == ["MODIFIED", "a"] for line in lines)


def test_a_removed_card_FAILS_and_names_the_card(tmp_path, monkeypatch):
    path = _install(tmp_path, monkeypatch, [_record("a"), _record("b")])
    before = refresh_all.catalogue_records()
    path.write_bytes(
        json.dumps({"schema_version": "2", "cards": [_record("a")]}, indent=2).encode("utf-8")
    )

    lines, failed = refresh_all.catalogue_guard(before)

    assert failed is True
    assert any(line.split() == ["REMOVED", "b"] for line in lines)


def test_an_unreadable_catalogue_FAILS_rather_than_passing_quietly(tmp_path, monkeypatch):
    """A file that stopped parsing must not read as "nothing changed"."""
    path = _install(tmp_path, monkeypatch, [_record("a")])
    before = refresh_all.catalogue_records()
    path.write_bytes(b"{ not json")

    lines, failed = refresh_all.catalogue_guard(before)

    assert failed is True
    assert "unreadable" in lines[0]


def test_the_add_cards_step_is_in_the_chain_and_needs_no_network():
    step = next(s for s in refresh_all.STEPS if s.name == "add_cards")
    assert step.network is False
    assert "cards_json" in step.probes
    # It must run AFTER the draft it consumes is rebuilt.
    order = [s.name for s in refresh_all.STEPS]
    assert order.index("cards") < order.index("import") < order.index("add_cards")


def test_the_merge_holds_back_a_card_the_catalogue_does_not_have_yet(tmp_path, monkeypatch):
    """An owned id missing from data/cards.json makes the app refuse to load at all
    ("_LoadFailure: 3", e.g. a new event card pulled before the app's synced
    catalogue has it). It is held back -- appended by the first refresh after the
    catalogue gains it -- and said, never written."""
    from types import SimpleNamespace

    _install(tmp_path, monkeypatch, [_record("a"), _record("b")])
    path = tmp_path / "owned.json"
    path.write_bytes(refresh_all._owned_bytes({"owned": ["a"], "schema_version": "2"}))
    monkeypatch.setattr(refresh_all, "APPEND_ONLY_OWNED", path)
    monkeypatch.setattr(refresh_all, "DUMPS", tmp_path)
    monkeypatch.setattr(refresh_all, "probe_card_params", lambda: SimpleNamespace(
        exists=True, error=None, ids={"card": {"a", "b", "new-event-card"}}))

    lines, failed = refresh_all.merge_owned("main")

    assert failed is False
    assert json.loads(path.read_bytes())["owned"] == ["a", "b"]
    assert any("held back" in line and "new-event-card" in line for line in lines)
