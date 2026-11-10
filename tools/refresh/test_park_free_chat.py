"""Free Chats: which rows are conversations, the read log that only grows, the account parse,
the facility spawn points the chats stand at, and the refresh writing it all."""
from __future__ import annotations

import json
import struct
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import account_snapshot as snap  # noqa: E402
import park_free_chat as fc  # noqa: E402
import park_map  # noqa: E402


# --------------------------------------------------------------------------- catalogue

MAPPINGS = {
    "tm-chat": {"id": "tm-chat", "parkTalkGroupId": "talk-chat", "parkNpcGroupId": "g1",
                "parkPositionId": "pos-house"},
    "tm-crowd": {"id": "tm-crowd", "parkNpcGroupId": "g2", "parkPositionId": "pos-house"},
    "tm-balloon": {"id": "tm-balloon", "parkTalkGroupId": "talk-balloon",
                   "parkNpcGroupId": "g2", "parkPositionId": "pos-house"},
    "tm-inside": {"id": "tm-inside", "parkTalkGroupId": "talk-inside", "parkNpcGroupId": "g1",
                  "parkPositionId": "pos-inside"},
}
TALKS = {
    "talk-chat": [{"groupId": "talk-chat", "number": 2}, {"groupId": "talk-chat", "number": 1}],
    "talk-balloon": [{"groupId": "talk-balloon", "number": 1, "isBalloon": True}],
    "talk-inside": [{"groupId": "talk-inside", "number": 1}],
}


def test_only_rows_with_a_talk_that_is_not_a_balloon_are_conversations():
    """The client gives the TALK action only to a mapping with a talk group whose first line is
    not a balloon (ParkSpawnDataExtension.GetActionType); the rest stand in the background."""
    rows = [{"id": f"f-{k}", "parkTargetMappingId": k} for k in MAPPINGS]
    got = [r["id"] for r in fc.interactive_rows(rows, MAPPINGS, TALKS)]
    assert got == ["f-tm-chat", "f-tm-inside"]


def test_cast_is_slot_ordered_and_deduplicated():
    group = [{"number": 1, "parkNpcId": "n-b"}, {"parkNpcId": "n-a"}, {"number": 2, "parkNpcId": "n-a2"}]
    who = {"n-a": "chr-00001", "n-b": "chr-00002", "n-a2": "chr-00001"}
    assert fc.cast_of(group, who) == ["chr-00001", "chr-00002"]


def test_an_interior_site_is_the_building_of_the_same_name():
    facilities = {"facility-a01-multiplay": {}, "facility-a01-house": {}}
    assert fc.site_of({"areaId": "area-a01", "facilityId": "facility-a01-house"},
                      facilities) == ("facility-a01-house", False)
    assert fc.site_of({"areaId": "area-a01-multiplay"}, facilities) == ("facility-a01-multiplay", True)
    assert fc.site_of({"areaId": "area-a09-nowhere"}, facilities) == (None, False)


def test_conditions_read_in_the_games_terms():
    rows = {
        "cd-q": [{"number": 1, "type": "ConditionType_CONDITION_TYPE_PARK_QUEST_CLEAR",
                  "resourceId1": "quest-main-w1-03-10", "resourceId2": "1", "min": "8"}],
        "cd-f": [{"number": 1, "type": "ConditionType_CONDITION_TYPE_FACILITY_LEVEL",
                  "resourceId1": "facility-a03-livestage", "min": "1", "max": "1"}],
        "cd-t": [{"number": 1, "type": "ConditionType_CONDITION_TYPE_PARK_TIME_PERIOD",
                  "resourceId1": "NOON"}],
        "cd-w": [{"number": 1, "type": "ConditionType_CONDITION_TYPE_PARK_QUEST_CLEAR",
                  "resourceId1": "quest-main-w1-03-10", "resourceId2": "1", "min": "8"},
                 {"number": 2, "type": "ConditionType_CONDITION_TYPE_NEGATIVE_SET",
                  "resourceId1": "cd-q9"}],
        "cd-q9": [{"number": 1, "type": "ConditionType_CONDITION_TYPE_PARK_QUEST_CLEAR",
                   "resourceId1": "quest-main-w1-03-10", "resourceId2": "1", "min": "9"}],
    }
    kw = dict(quest_titles={"quest-main-w1-03-10": "Major Announcement"},
              facility_names={"facility-a03-livestage": "Soda Pop Lagoon Stage"}, all_rows=rows)
    assert fc.condition_text(rows["cd-q"], **kw) == "Main quest 3-10 step 8 cleared (“Major Announcement”)"
    assert fc.condition_text(rows["cd-f"], **kw) == "Soda Pop Lagoon Stage built"
    assert fc.condition_text(rows["cd-t"], **kw) == "Noon only"
    assert fc.condition_text(rows["cd-w"], **kw) == (
        "Main quest 3-10 step 8 cleared (“Major Announcement”), "
        "until Main quest 3-10 step 9 (“Major Announcement”)")


def test_build_catalogue_from_named_master_rows():
    m = {
        "ParkTalkFreeGrouping": [{"groupId": "grp", "areaId": "area-a01", "conditionGroupId": "cd-q"}],
        "ParkTalkFree": [{"id": f"f-{k}", "parkTalkFreeGroupingGroupId": "grp",
                          "parkTargetMappingId": k} for k in MAPPINGS],
        "ParkTargetMapping": list(MAPPINGS.values()),
        "ParkTalk": [line for lines in TALKS.values() for line in lines],
        "ParkNpcGroup": [{"id": "g1", "parkNpcId": "n1"}, {"id": "g1", "number": 1, "parkNpcId": "n2"},
                         {"id": "g2", "parkNpcId": "n1"}],
        "ParkNpc": [{"id": "n1", "sdCostumeId": "sdcos-00001-x"}, {"id": "n2", "sdCostumeId": "sdcos-00002-x"}],
        "SdCostume": [{"id": "sdcos-00001-x", "characterId": "chr-00001"}],
        "ParkPosition": [{"id": "pos-house", "areaId": "area-a01", "facilityId": "facility-a01-house",
                          "position": "free_npc_01"},
                         {"id": "pos-inside", "areaId": "area-a01-multiplay", "position": "free_npc_01"}],
        "Facility": [{"id": "facility-a01-house", "nameLangId": "la-h", "areaId": "area-a01"},
                     {"id": "facility-a01-multiplay", "nameLangId": "la-m", "areaId": "area-a01"}],
        "LangFacility_Eng": [{"id": "la-h", "text": "holo Home"}, {"id": "la-m", "text": "Fortune Network"}],
        "FacilityAppearance": [
            {"facilityId": "facility-a01-house", "priority": 1, "mapImageAssetId": "area-a01-house-00",
             "iconAssetIds": ["house"]},
            {"facilityId": "facility-a01-house", "priority": 5, "assetId": "locked"}],
        "Condition": [{"groupId": "cd-q", "number": 1,
                       "type": "ConditionType_CONDITION_TYPE_PARK_QUEST_CLEAR",
                       "resourceId1": "quest-main-w1-01-02"}],
        "LangParkQuest_Eng": [{"id": "la-quest-main-w1-01-02-title", "text": "Construction Begins!"}],
        "Character": [{"id": "chr-00001", "nameEng": "Tokino Sora", "shortNameEng": "Sora"}],
    }
    cat = fc.build_catalogue(m)
    assert set(cat["chats"]) == {"f-tm-chat", "f-tm-inside"}
    chat = cat["chats"]["f-tm-chat"]
    assert chat["site"] == "facility-a01-house" and chat["spot"] == "free_npc_01"
    # n2's costume is not in SdCostume: the costume id itself names the character
    assert chat["cast"] == ["chr-00001", "chr-00002"]
    assert cat["sites"]["facility-a01-multiplay"]["inside"] == "area-a01-multiplay"
    # the built appearance is the highest-priority row WITH a map image, not the last row
    assert cat["sites"]["facility-a01-house"]["map_image"] == "area-a01-house-00"
    assert cat["sites"]["facility-a01-house"]["icon"] == "house"
    assert cat["conditions"]["cd-q"] == "Main quest 1-2 cleared (“Construction Begins!”)"
    assert cat["characters"]["chr-00001"] == {"name": "Tokino Sora", "short": "Sora"}


# --------------------------------------------------------------------------- the account

def _detail(grouping, talk_free, read):
    body = snap.pb_bytes_field(2, grouping.encode()) + snap.pb_bytes_field(3, talk_free.encode())
    for c in read:
        body += snap.pb_bytes_field(4, c.encode())
    return body


def test_parse_userdata_reads_picks_and_details():
    pick = snap.pb_bytes_field(2, b"grp") + snap.pb_bytes_field(3, b"area-a01")
    blob = (snap.pb_bytes_field(fc.F_PICK, pick)
            + snap.pb_bytes_field(fc.F_DETAIL, _detail("grp", "chat-1", ["chr-00001", "chr-00002"]))
            + snap.pb_bytes_field(fc.F_DETAIL, _detail("grp", "chat-2", [])))
    got = fc.parse_userdata(snap.fields(blob), snap.fields)
    assert got["picks"] == [{"area": "area-a01", "grouping": "grp"}]
    assert got["details"] == [
        {"grouping": "grp", "talk_free": "chat-1", "read": ["chr-00001", "chr-00002"]},
        {"grouping": "grp", "talk_free": "chat-2", "read": []}]


def _resp(*details, area="area-a01"):
    return {"picks": [{"area": area, "grouping": "grp"}],
            "details": [{"grouping": "grp", "talk_free": t, "read": r} for t, r in details]}


def test_read_marks_only_ever_grow():
    """A later response without the marks (or without the chat) never un-reads it."""
    log = fc.merge_log(None, _resp(("chat-1", ["chr-00001"]), ("chat-2", [])),
                       "2026-08-30T02:29:44+02:00")
    log = fc.merge_log(log, _resp(("chat-1", []), ("chat-3", [])), "2026-10-26T02:37:44+01:00")
    assert log["chats"]["chat-1"]["read"] == ["chr-00001"]
    assert log["chats"]["chat-1"]["read_seen"] == "2026-08-30T02:29:44+02:00"
    assert log["chats"]["chat-1"]["seen"] == "2026-10-26T02:37:44+01:00"
    assert log["chats"]["chat-2"] == {"read": [], "seen": "2026-08-30T02:29:44+02:00", "read_seen": None}
    assert [o["at"] for o in log["observations"]] == ["2026-08-30T02:29:44+02:00",
                                                      "2026-10-26T02:37:44+01:00"]


def test_times_compare_as_instants_across_a_dst_change():
    """+02:00 summer and +01:00 winter: 02:00+01:00 is LATER than 02:30+02:00, which a text
    comparison gets backwards."""
    log = fc.merge_log(None, _resp(("c", [])), "2026-10-26T02:00:00+01:00")
    log = fc.merge_log(log, _resp(("c", ["chr-00001"])), "2026-10-25T02:30:00+02:00")
    assert log["chats"]["c"]["seen"] == "2026-10-26T02:00:00+01:00"
    assert log["chats"]["c"]["read_seen"] == "2026-10-25T02:30:00+02:00"
    assert log["observations"][0]["at"] == "2026-10-25T02:30:00+02:00"


def test_keep_drops_the_background_rows():
    log = fc.merge_log(None, _resp(("chat-1", []), ("crowd", [])), "2026-08-30T02:29:44+02:00",
                       keep={"chat-1"})
    assert set(log["chats"]) == {"chat-1"}
    assert log["observations"][0]["chats"] == 1


def test_a_catalogue_prunes_unread_background_rows_kept_before_it_existed():
    log = fc.merge_log(None, _resp(("chat-1", []), ("crowd", []), ("odd", ["chr-00001"])),
                       "2026-08-30T02:29:44+02:00")
    log = fc.merge_log(log, _resp(("chat-1", [])), "2026-08-31T02:29:44+02:00", keep={"chat-1"})
    # a read mark is never dropped, even on a row the catalogue does not list
    assert set(log["chats"]) == {"chat-1", "odd"}


def test_update_log_round_trips_and_grows(tmp_path):
    path = tmp_path / "park_free_chat_log.json"
    fc.update_log(path, _resp(("chat-1", ["chr-00001"])), "2026-08-30T02:29:44+02:00", None)
    fc.update_log(path, _resp(("chat-2", [])), "2026-08-31T02:29:44+02:00", None)
    doc = json.loads(path.read_bytes())
    assert doc["version"] == fc.LOG_VERSION
    assert set(doc["chats"]) == {"chat-1", "chat-2"} and len(doc["observations"]) == 2


def test_catalogue_ids_absent_export_is_none(tmp_path):
    assert fc.catalogue_ids(tmp_path / "park_spots.json") is None
    (tmp_path / "park_spots.json").write_bytes(json.dumps({"free_chat": {"chats": {"a": {}}}}).encode())
    assert fc.catalogue_ids(tmp_path / "park_spots.json") == {"a"}


def test_write_park_state_folds_free_chat_into_the_log(monkeypatch, tmp_path):
    state = {"random": {}, "opened_treasures": [], "fixed_collected": [],
             "free_chat": _resp(("chat-1", ["chr-00001"]))}
    monkeypatch.setattr(snap, "DUMPS", tmp_path)
    monkeypatch.setattr(snap, "load_userdata_schema", lambda: {})
    monkeypatch.setattr(snap, "locate_userdata", lambda blob, schema: [(1, 2, b"x")])
    monkeypatch.setitem(sys.modules, "park_treasure",
                        types.SimpleNamespace(park_state=lambda fields: json.loads(json.dumps(state))))
    snap.write_park_state(b"blob", "main")
    saved = json.loads((tmp_path / "park_state.json").read_text("utf8"))
    assert "free_chat" not in saved, "the detail rows belong in the log, not the daily state"
    assert saved["free_chat_picks"] == state["free_chat"]["picks"]
    log = json.loads((tmp_path / "park_free_chat_log.json").read_text("utf8"))
    assert log["chats"]["chat-1"]["read"] == ["chr-00001"]
    assert log["chats"]["chat-1"]["seen"] == saved["_captured_at"]


# --------------------------------------------------------------------------- facility spots

def _s(text):
    b = text.encode()
    return struct.pack("<i", len(b)) + b + b"\0" * ((4 - len(b) % 4) % 4)


def _point(name, x, z):
    return _s(name) + struct.pack("<7f", x, 0.0, z, 0, 0, 0, 1)


def _points(*pts):
    return struct.pack("<i", len(pts)) + b"".join(_point(*p) for p in pts)


def _facility(fid, x, z, spawn):
    return (_s(fid) + struct.pack("<ii", 0, 0) + struct.pack("<3f", x, 0.0, z)
            + struct.pack("<3f", 0, 0, 0) + _points(*spawn) + _point("warp", 0, 0))


def _scene(*facilities):
    return (b"\0" * 28 + _s("env_data") + _points() + _points(("reward_random_01", 1, 2))
            + _points(("player", 0, 0)) + struct.pack("<i", len(facilities)) + b"".join(facilities))


def test_facility_spawn_points_are_keyed_by_facility():
    """`free_npc_01` exists in every facility: only the facility array can place it."""
    raw = _scene(_facility("facility-a01-house", 35.8, -18.2, [("free_npc_01", 30.0, -20.0)]),
                 _facility("facility-a01-station", 12.0, -36.2, [("free_npc_01", 10.0, -40.0)]),
                 _facility("facility-a01-station", 12.0, -36.2, [("free_npc_02", 11.0, -41.0)]))
    got = park_map.parse_facilities(raw)
    assert got["facility-a01-house"]["spawn"]["free_npc_01"] == pytest.approx((30.0, -20.0))
    assert got["facility-a01-station"]["spawn"]["free_npc_01"] == pytest.approx((10.0, -40.0))
    # a repeated facility id merges its spawn points
    assert set(got["facility-a01-station"]["spawn"]) == {"free_npc_01", "free_npc_02"}
    assert got["facility-a01-house"]["position"] == pytest.approx((35.8, -18.2))


def test_facility_parse_must_end_at_the_end_of_the_buffer():
    raw = _scene(_facility("facility-a01-house", 1, 2, []))
    assert park_map.parse_facilities(raw)
    with pytest.raises(ValueError):
        park_map.parse_facilities(raw + b"\0\0\0\0")
