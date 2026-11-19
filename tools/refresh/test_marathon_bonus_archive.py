"""The bonus-rule archive (`marathon_bonus.merge_archive`): every chapter ever read stays."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import marathon_bonus as mb  # noqa: E402


def payload(fetched: float, chapters: dict[str, int]) -> dict:
    """One marathon_bonus.json: per chapter, a live group whose single row carries `permil`."""
    groups = []
    for key, permil in chapters.items():
        groups.append(
            {
                "group_id": f"live_marathon_score_bonus-{key}",
                "kind": "live",
                "rpcs": ["ListEventInfo"],
                "decrypt": "exact",
                "rows": [{"character_id": "chr-01001", "permil": permil}],
                "tables": {},
            }
        )
        groups.append({"group_id": f"music_score_bonus-{key}", "kind": "music", "rows": [], "tables": {}})
    return {
        "current_group_ids": [g["group_id"] for g in groups],
        "live_fetched_at": fetched,
        "event_songs": {k: ["m0001"] for k in chapters},
        "groups": groups,
    }


A = "marathon_event-normal-008-chapter-01"
B = "marathon_event-normal-009-chapter-01"


def test_a_chapter_that_leaves_the_event_list_stays_archived():
    arc, n = mb.merge_archive({}, payload(100.0, {A: 300}))
    assert n == 1
    arc, n = mb.merge_archive(arc, payload(200.0, {B: 500}))
    assert n == 1
    assert sorted(arc["chapters"]) == [A, B]
    entry = arc["chapters"][A]
    assert entry["fetched_at"] == 100.0
    assert entry["event_songs"] == ["m0001"]
    assert [g["kind"] for g in entry["groups"]] == ["live", "music"]
    assert entry["groups"][0]["rows"][0]["permil"] == 300
    assert "tables" not in entry["groups"][0]  # derivable from the rows: not kept twice


def test_a_newer_read_replaces_and_an_older_one_does_not():
    arc, _ = mb.merge_archive({}, payload(200.0, {A: 300}))
    arc, n = mb.merge_archive(arc, payload(100.0, {A: 999}))  # an old snapshot folded in late
    assert n == 0 and arc["chapters"][A]["groups"][0]["rows"][0]["permil"] == 300
    arc, n = mb.merge_archive(arc, payload(300.0, {A: 400}))
    assert n == 1 and arc["chapters"][A]["groups"][0]["rows"][0]["permil"] == 400


def test_the_same_read_twice_changes_nothing(tmp_path):
    path = tmp_path / "archive.json"
    assert mb.write_archive([payload(100.0, {A: 300})], str(path)) == 1
    before = path.read_bytes()
    assert mb.write_archive([payload(100.0, {A: 300})], str(path)) == 0
    assert path.read_bytes() == before
    assert sorted(json.loads(before)["chapters"]) == [A]


def test_a_lossy_decrypt_is_not_archived():
    doc = payload(100.0, {A: 300})
    doc["groups"][0]["decrypt"] = "cbc-lossy"
    arc, _ = mb.merge_archive({}, doc)
    assert [g["kind"] for g in arc["chapters"][A]["groups"]] == ["music"]
