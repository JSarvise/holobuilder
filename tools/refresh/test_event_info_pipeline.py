"""The refresh fetches the open event's bonus tables.

`account_snapshot.py --refresh-events` writes the decrypted `Event/ListEventInfo` body to
dumps/event_info_live.bin; `marathon_bonus.py` reads it like a capture and lists the groups that
are open now; `refresh_all.py` runs both on every refresh.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def test_list_event_info_is_whitelisted_as_an_exact_read():
    import account_snapshot as snap

    assert "/rpc.api.Event/ListEventInfo" in snap.ALLOWED
    assert "/rpc.api.Event/ListEventInfoForPortal" not in snap.ALLOWED


def test_a_live_event_file_is_read_as_one_exact_plaintext(tmp_path):
    import marathon_bonus as mb

    blob = tmp_path / "event_info_live.bin"
    blob.write_bytes(b"\x0a\x00")
    got = list(mb._plaintexts(str(blob)))
    assert got == [("/rpc.api.Event/ListEventInfo", b"\x0a\x00", "exact")]


def test_the_refresh_fetches_events_and_rebuilds_the_bonus_tables():
    import argparse

    import refresh_all as ra

    args = argparse.Namespace(account="main", merge_collection=False, python=None)
    cmd, _env, _cwd = ra.cmd_account(args)
    assert "--refresh-events" in cmd
    assert any(step.name == "event_bonus" for step in ra.STEPS)


def test_song_bonus_rows_of_different_songs_sharing_a_number_are_all_kept(monkeypatch):
    # every solo song's single row is #1 on the wire: keyed by `number` alone, the merge would
    # keep only the last song's row (normal-008: 6 songs -> 2)
    import marathon_bonus as mb

    gid = "music_score_bonus-marathon_event-normal-008-chapter-01"
    rows = [{"group_id": gid, "music_id": "m0369", "number": 1, "card_id": "card-00007-5-uniq-0096-00", "permil": 100},
            {"group_id": gid, "music_id": "m0369", "number": 2, "card_id": "card-03007-5-uniq-0100-00", "permil": 100},
            {"group_id": gid, "music_id": "m0551", "number": 1, "card_id": "card-00007-5-uniq-0096-00", "permil": 100},
            {"group_id": gid, "music_id": "m0371", "number": 1, "card_id": "card-06005-5-uniq-0097-00", "permil": 100}]
    monkeypatch.setattr(mb, "_plaintexts", lambda _p: [("/rpc.api.Event/ListEventInfo", b"", "exact")] * 2)
    monkeypatch.setattr(mb, "groups", lambda _pt: {(gid, "music"): list(rows)})
    monkeypatch.setattr(mb, "COLLISIONS", [])
    got = mb.collect(["a", "b"])[(gid, "music")]["rows"]
    assert sorted((r["music_id"], r["number"]) for r in got) == [
        ("m0369", 1), ("m0369", 2), ("m0371", 1), ("m0551", 1)]
    assert mb.COLLISIONS == []


def test_two_different_rows_under_one_key_are_reported_not_swallowed(monkeypatch):
    import marathon_bonus as mb

    gid = "live_marathon_score_bonus-x"
    rows = [{"group_id": gid, "number": 1, "card_id": "card-1", "permil": 300},
            {"group_id": gid, "number": 1, "card_id": "card-2", "permil": 300}]
    monkeypatch.setattr(mb, "_plaintexts", lambda _p: [("/rpc.api.Event/ListEventInfo", b"", "exact")])
    monkeypatch.setattr(mb, "groups", lambda _pt: {(gid, "live"): list(rows)})
    monkeypatch.setattr(mb, "COLLISIONS", [])
    mb.collect(["a"])
    assert len(mb.COLLISIONS) == 1
