from __future__ import annotations
import json, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gacha_sync as gsy  # noqa: E402
import gacha_store as gs  # noqa: E402

LIST = {"gacha_groups": [{"gacha_group_id": "g", "gachas": [
    {"gacha_id": "gacha-a", "name": "A", "type": 1, "start_time": 1, "end_time": 9,
     "pickup_card_ids": ["card-p"], "icon_asset_id": "a",
     "gacha_buttons": [{"gacha_button_id": "btn", "drawn_count": 3, "limit_count": 0}],
     "gacha_point": {"gacha_point_id": "gp", "quantity": 12}}]}]}
HIST = {"histories": [
    {"gacha_name": "A", "card_id": "card-x", "draw_time": 100},
    {"gacha_name": "A", "card_id": "card-x", "draw_time": 100},
    {"gacha_name": "Old", "card_id": "card-y", "draw_time": 50}]}
RATES = {"gacha-a": {"normal": {"normal": [{"rarity": 5, "ppm": 500000, "cards": []}]},
                     "card_select": {"error": "grpc-status 3 INVALID_ARGUMENT"}}}


def test_apply_sync_appends_and_reports_disk_deltas(tmp_path):
    s = gsy.apply_sync(tmp_path, 1000, LIST, HIST, RATES)
    assert s == {"pulls": 3, "banners": 1, "rates": 1, "counters": 1, "possible_gap": False}
    pulls = gs.read_jsonl(tmp_path / gs.PULLS)
    assert [p["gacha_id"] for p in pulls] == [None, "gacha-a", "gacha-a"]
    c = gs.read_jsonl(tmp_path / gs.COUNTERS)[0]
    assert c["gacha_point"]["quantity"] == 12 and c["buttons"][0]["drawn_count"] == 3
    r = gs.read_jsonl(tmp_path / gs.RATES)[0]
    assert r["gacha_id"] == "gacha-a" and r["synced_at"] == 1000 and "error" in r["card_select"]
    snap = json.loads((tmp_path / gs.SNAPSHOTS / "1000.json").read_text("utf8"))
    assert snap["list"] == LIST and snap["history"] == HIST


def test_apply_sync_twice_adds_no_pulls_or_banners_but_keeps_rate_history(tmp_path):
    gsy.apply_sync(tmp_path, 1000, LIST, HIST, RATES)
    s = gsy.apply_sync(tmp_path, 2000, LIST, HIST, RATES)
    assert s["pulls"] == 0 and s["banners"] == 0
    assert s["rates"] == 1 and s["counters"] == 1   # per-sync series, by design


def test_history_gap_only_when_a_full_window_starts_past_the_store(tmp_path):
    """Gacha/ListHistory returns at most HISTORY_CAP entries. When it comes back full
    AND its oldest entry is newer than everything stored, the pulls in between are
    gone from the wire for good -- the sync must SAY so, not silently under-count."""
    stored = [{"draw_time": 50, "gacha_name": "Old", "card_id": "card-y"}]
    full = [{"draw_time": 1000 + i, "gacha_name": "A", "card_id": "card-x"}
            for i in range(gsy.HISTORY_CAP)]
    assert gsy.history_gap(full, stored) is True
    # Overlap with the store: nothing was skipped.
    overlapping = [{"draw_time": 50, "gacha_name": "Old", "card_id": "card-y"}] + full[:-1]
    assert gsy.history_gap(overlapping, stored) is False
    # A partial window is the whole history: nothing can be missing.
    assert gsy.history_gap(full[:5], stored) is False
    # An empty store has nothing to have a gap against (first ever sync).
    assert gsy.history_gap(full, []) is False


def test_history_gap_when_the_last_sync_is_older_than_the_window():
    """The server also keeps only the last 30 days: a sync more than 30 days after the last
    one can have lost pulls even from a window far short of 100."""
    stored = [{"draw_time": 50, "gacha_name": "Old", "card_id": "card-y"}]
    some = [{"draw_time": 1000, "gacha_name": "A", "card_id": "card-x"}]
    day = 86400
    assert gsy.history_gap(some, stored, last_sync=0, synced_at=31 * day) is True
    assert gsy.history_gap(some, stored, last_sync=0, synced_at=10 * day) is False   # nothing pulled between
    assert gsy.history_gap(some, stored) is False


def test_apply_sync_flags_possible_gap(tmp_path):
    gsy.apply_sync(tmp_path, 1000, LIST, HIST, RATES)
    full = {"histories": [{"gacha_name": "A", "card_id": "card-x", "draw_time": 9000 + i}
                          for i in range(gsy.HISTORY_CAP)]}
    s = gsy.apply_sync(tmp_path, 2000, LIST, full, RATES)
    assert s["possible_gap"] is True


def test_alias_gacha_ids_reads_the_hand_maintained_file(tmp_path):
    p = tmp_path / "banner_aliases.json"
    p.write_bytes(json.dumps({"_doc": "notes", "Old": {"gacha_id": "gacha-old", "kind": "pickup"},
                              "NoId": {"kind": "ticket"}}).encode("utf8"))
    assert gsy.alias_gacha_ids(p) == ["gacha-old"]
    assert gsy.alias_gacha_ids(tmp_path / "absent.json") == []


def test_alias_gacha_ids_names_a_malformed_file_instead_of_raising(tmp_path, capsys):
    """Hand-maintained means hand-breakable. A half-typed file must not abort a sync
    that has ALREADY written the store."""
    p = tmp_path / "banner_aliases.json"
    p.write_bytes(b'{"Old": {"gacha_id": "gacha-old",')
    assert gsy.alias_gacha_ids(p) == []
    assert "banner_aliases.json could not be read" in capsys.readouterr().out

    p.write_bytes(b'["not", "an", "object"]')
    assert gsy.alias_gacha_ids(p) == []
