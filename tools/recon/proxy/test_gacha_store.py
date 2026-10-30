"""gacha_store: append-only JSONL + the multiset dedupe the server's history needs.

In a multi-draw every card shares one draw_time (ms) and two
copies of the same card come back as two identical lines. So dedupe is per
(draw_time, gacha_name) by card COUNT, never by unique key.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gacha_store as gs  # noqa: E402

LIVE = [{"gacha_id": "gacha-pickup-normal-260807", "name": "Summer Duo", "type": 1,
         "start_time": 1, "end_time": 2, "pickup_card_ids": ["card-a"], "icon_asset_id": "x"}]


def _hist(dt, name, *cards):
    return [{"draw_time": dt, "gacha_name": name, "card_id": c} for c in cards]


def test_append_and_read_roundtrip(tmp_path):
    p = tmp_path / "pulls.jsonl"
    assert gs.read_jsonl(p) == []
    n = gs.append_jsonl(p, [{"a": 1, "z": "★"}, {"a": 2}])
    assert n == 2
    raw = p.read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n") and "★".encode("utf8") in raw
    assert gs.read_jsonl(p) == [{"a": 1, "z": "★"}, {"a": 2}]
    assert gs.count_lines(p) == 2


def test_new_pulls_first_time_keeps_duplicate_copies():
    hist = _hist(1000, "Summer Duo", "card-x", "card-x", "card-y")
    rows = gs.new_pulls(hist, stored=[], live_banners=LIVE, synced_at=99)
    assert [r["card_id"] for r in rows] == ["card-x", "card-x", "card-y"]
    assert all(r["gacha_id"] == "gacha-pickup-normal-260807" for r in rows)
    assert all(r["first_seen"] == 99 and r["draw_time"] == 1000 for r in rows)


def test_new_pulls_same_response_twice_adds_nothing():
    hist = _hist(1000, "Summer Duo", "card-x", "card-x", "card-y")
    first = gs.new_pulls(hist, [], LIVE, 1)
    again = gs.new_pulls(hist, first, LIVE, 2)
    assert again == []


def test_new_pulls_adds_only_missing_copies():
    stored = gs.new_pulls(_hist(1000, "Summer Duo", "card-x"), [], LIVE, 1)
    rows = gs.new_pulls(_hist(1000, "Summer Duo", "card-x", "card-x"), stored, LIVE, 2)
    assert [r["card_id"] for r in rows] == ["card-x"]


def test_new_pulls_unknown_banner_gets_null_id_and_is_never_backfilled():
    hist = _hist(5, "Sunny Summer Vacay Gacha", "card-q")
    rows = gs.new_pulls(hist, [], LIVE, 1)
    assert rows[0]["gacha_id"] is None
    # later sync where the banner IS live: existing row is untouched, nothing new
    live2 = LIVE + [{"gacha_id": "gacha-pickup-normal-260728", "name": "Sunny Summer Vacay Gacha",
                     "type": 1, "start_time": 0, "end_time": 1, "pickup_card_ids": [], "icon_asset_id": ""}]
    assert gs.new_pulls(hist, rows, live2, 2) == []


def test_banner_rows_first_seen_then_only_on_change():
    rows = gs.banner_rows(LIVE, stored=[], synced_at=10)
    assert len(rows) == 1 and rows[0]["gacha_id"] == "gacha-pickup-normal-260807"
    assert rows[0]["seen_at"] == 10 and rows[0]["raw"] == LIVE[0]
    assert gs.banner_rows(LIVE, rows, 11) == []
    changed = [dict(LIVE[0], end_time=3)]
    again = gs.banner_rows(changed, rows, 12)
    assert len(again) == 1 and again[0]["end_time"] == 3 and again[0]["seen_at"] == 12


def test_store_module_is_append_only():
    """AST guard in the shape of tools/web/test_no_writes.py: no write/truncate/delete."""
    tree = ast.parse((HERE / "gacha_store.py").read_text("utf8"))
    banned_attrs = {"write_text", "write_bytes", "unlink", "rename", "replace", "rmdir", "truncate"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                assert f.attr not in banned_attrs, f"{f.attr} in gacha_store.py"
            if isinstance(f, ast.Name) and f.id == "open":
                mode = None
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    mode = node.args[1].value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                assert mode in ("ab", "rb", "r"), f"open() mode {mode!r} in gacha_store.py"
