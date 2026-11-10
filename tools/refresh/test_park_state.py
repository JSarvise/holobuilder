"""The account refresh saves today's Park pick (`account_snapshot.write_park_state`), so the
Park page reads it instead of logging in."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import account_snapshot as snap  # noqa: E402


def test_write_park_state_saves_the_pick_with_its_capture_time(monkeypatch, tmp_path):
    state = {
        "random": {"area-a01": {"picked": ["s1"], "collected": [], "last_refreshed_time": 7}},
        "opened_treasures": ["t1"],
        "fixed_collected": [],
    }
    monkeypatch.setattr(snap, "DUMPS", tmp_path)
    monkeypatch.setattr(snap, "load_userdata_schema", lambda: {})
    monkeypatch.setattr(snap, "locate_userdata", lambda blob, schema: [(1, 2, b"x")])
    monkeypatch.setitem(
        sys.modules, "park_treasure", types.SimpleNamespace(park_state=lambda fields: dict(state))
    )
    snap.write_park_state(b"blob", "main")
    saved = json.loads((tmp_path / "park_state.json").read_text("utf8"))
    assert saved["random"] == state["random"] and saved["opened_treasures"] == ["t1"]
    assert saved["_captured_at"][:2] == "20" and saved["_captured_at"][-6] in "+-"


def test_no_userdata_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(snap, "DUMPS", tmp_path)
    monkeypatch.setattr(snap, "load_userdata_schema", lambda: {})
    monkeypatch.setattr(snap, "locate_userdata", lambda blob, schema: None)
    snap.write_park_state(b"blob", "main")
    assert not (tmp_path / "park_state.json").exists()
