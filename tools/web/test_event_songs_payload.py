"""The Event page lists EVERY song of the chapter: `marathon_bonus.json`'s
`event_songs` (the server's per-song ranking), each with group / solo and whether a card lifts
its score -- not only the songs that have a song-score bonus row.

    python -m pytest tools/web/test_event_songs_payload.py
"""
from __future__ import annotations

import event_mode

CHAPTER = "marathon_event-normal-008-chapter-01"


def _payload():
    raw = {"current_group_ids": [f"music_score_bonus-{CHAPTER}"],
           "event_songs": {CHAPTER: ["m0369", "m0551", "m0999"]},
           "groups": [{"group_id": f"music_score_bonus-{CHAPTER}", "kind": "music",
                       "rows": [{"music_id": "m0369", "number": 1, "character_id": "chr-00007",
                                 "permil": 100},
                                {"music_id": "m0551", "number": 1, "character_id": "chr-00007",
                                 "permil": 100}]}]}
    raw, event, keys = event_mode.bundle_from_raw(raw)
    return event_mode.event_payload(
        raw, event, open_keys=keys, cards_by_id={}, owned_ids=[], collection={},
        titles={"m0369": "KaienSpectacle", "m0551": "ENDLESS FES"}, chr_names={},
        singers={"m0369": "Natsuiro Matsuri, Vestia Zeta"},
        music_meta={"m0369": {"musicSingerType": "MusicSingerType_MUSIC_SINGER_TYPE_GROUP"},
                    "m0551": {"musicSingerType": "MusicSingerType_MUSIC_SINGER_TYPE_SOLO"}})


def test_every_event_song_is_listed_with_its_kind_even_without_a_bonus_row():
    got = _payload()
    assert got["event_songs"] == ["m0369", "m0551", "m0999"]
    by_id = {s["music_id"]: s for s in got["songs"]}
    assert by_id["m0369"] == {"music_id": "m0369", "title": "KaienSpectacle",
                              "singer": "Natsuiro Matsuri, Vestia Zeta", "singer_type": "group",
                              "song_bonus": True}
    assert by_id["m0551"]["singer_type"] == "solo" and by_id["m0551"]["song_bonus"]
    assert by_id["m0999"]["song_bonus"] is False and by_id["m0999"]["singer_type"] is None


def test_the_singer_type_reads_the_master_enum():
    assert event_mode.singer_type({"musicSingerType": "MusicSingerType_MUSIC_SINGER_TYPE_GROUP"}) == "group"
    assert event_mode.singer_type(None) is None
