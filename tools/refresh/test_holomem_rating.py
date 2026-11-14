"""The Holomem Rating refresh's decoders and its whitelist (no network)."""

from __future__ import annotations

import account_snapshot as snap
import holomem_rating as H


def _varint_field(fn: int, value: int) -> bytes:
    return snap.write_varint(fn << 3) + snap.write_varint(value)


def _msg(fn: int, payload: bytes) -> bytes:
    return snap.pb_bytes_field(fn, payload)


def test_only_the_rating_reads_are_whitelisted():
    assert H.RANK_PATH in snap.ALLOWED
    assert H.BORDER_PATH in snap.ALLOWED
    assert "/rpc.api.Music/GetHighestScoreRatingRankingInfo" in snap.ALLOWED
    assert "/rpc.api.Music/SwitchFavorite" not in snap.ALLOWED
    assert "/rpc.api.Music/ReleaseMusic" not in snap.ALLOWED


def test_decode_ranks_skips_the_unranked():
    body = (
        _msg(1, _varint_field(1, 57) + _msg(2, b"chr-00036"))
        + _msg(1, _msg(2, b"chr-00001"))  # no rank: not on the ranking
        + _msg(1, _varint_field(1, 1203) + _msg(2, b"chr-03003"))
    )
    assert H.decode_ranks(body) == {"chr-00036": 57, "chr-03003": 1203}


def test_decode_borders_sorted_by_rank():
    body = (
        _msg(1, _varint_field(1, 100) + _varint_field(2, 1490))
        + _msg(1, _varint_field(1, 1) + _varint_field(2, 1620))
        + _varint_field(2, 1_794_000_000_000)
    )
    rows, updated = H.decode_borders(body)
    assert rows == [{"rank": 1, "rating": 1620}, {"rank": 100, "rating": 1490}]
    assert updated == 1_794_000_000_000


def test_rating_songs_and_holomems():
    rows = [
        {
            "id": "m1",
            "data": {
                "id": "m1",
                "characterIds": ["chr-1"],
                "musicSingerType": "MusicSingerType_MUSIC_SINGER_TYPE_SOLO",
                "isHighestScoreRatingTarget": True,
                "startTime": "1",
            },
        },
        {
            "id": "m2",
            "data": {
                "id": "m2",
                "characterIds": ["chr-1", "chr-2"],
                "musicSingerType": "MusicSingerType_MUSIC_SINGER_TYPE_GROUP",
                "isHighestScoreRatingTarget": True,
            },
        },
        {
            "id": "m3",
            "data": {
                "id": "m3",
                "characterIds": ["chr-1", "chr-3"],
                "musicSingerType": "MusicSingerType_MUSIC_SINGER_TYPE_ALL",
            },
        },
    ]
    songs = H.rating_songs(rows)
    assert [s.singer_type for s in songs] == ["solo", "group", "all"]
    assert H.rated_holomems(songs) == ["chr-1", "chr-2"]


def test_summary_takes_each_songs_best_difficulty():
    records = {
        "chr-1": {
            "m1": {"EXPERT": {"rating": 435}, "HARD": {"rating": 138}},
            "m2": {"EXPERT": {"rating": 521}},
            "m3": {"EXPERT": {"rating": 440}},
            "m4": {"EXPERT": {"rating": 173}},
        }
    }
    assert H.summary(records) == {"chr-1": 1396}
