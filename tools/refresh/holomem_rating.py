"""The account's Holomem Rating, live off the wire: `dumps/holomem_rating.json`.

What it reads (one login):

- `User/Get`: the songs the account owns (`user_music_list`) and every highest-score record
  the rating is built from (`user_music_character_highest_score_list`: holomem x song x
  difficulty -> highest score and its rating).
- `Music/ListHighestScoreRatingRankingRank`: the account's own rank on each holomem's
  ranking, every holomem in one request.
- `Music/ListHighestScoreRatingRankingRewardThresholdRankingRankInfo`: each holomem's reward
  borders (the rating at rank 1, 10, 100, 1000), one request per holomem.

The two ranking reads are refused (`DATE_CHANGED`) once the in-game day has rolled over until
the game client is opened; the records still come from `User/Get`, and the last ranking read
is kept with its date.

    python holomem_rating.py            # live, writes dumps/holomem_rating.json
    python holomem_rating.py --dry-run  # what it would send

Every endpoint goes through `account_snapshot.call`, whose read-only whitelist is the one
place that decides what may be sent.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import atomic_io

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402
from hbuilder.domain import rating as rating_rules  # noqa: E402
from hbuilder.errors import run_cli  # noqa: E402

DUMPS = paths.DUMPS
sys.path.insert(0, str(paths.REFRESH_DIR))

import account_snapshot as snap  # noqa: E402

OUT = DUMPS / "holomem_rating.json"
MUSIC_JSON = DUMPS / "holodori" / "main_Music.json"

RANK_PATH = "/rpc.api.Music/ListHighestScoreRatingRankingRank"
BORDER_PATH = "/rpc.api.Music/ListHighestScoreRatingRankingRewardThresholdRankingRankInfo"

# MusicDifficultyType
DIFFICULTY = {1: "EASY", 2: "NORMAL", 3: "HARD", 4: "EXPERT", 5: "MASTER"}
# UserMusic (entity/transaction/user_music.gen.proto)
MUSIC_SUB = {2: "music_id", 8: "released_time"}
# UserMusicCharacterHighestScore.HighestScoreInfo
INFO_SUB = {1: "difficulty", 2: "score", 3: "rating", 4: "updated"}


def rating_songs(music_rows: list[dict]) -> list[rating_rules.RatingSong]:
    """Every song of the master as a `RatingSong`."""
    out = []
    for row in music_rows:
        d = row.get("data") or row
        out.append(
            rating_rules.RatingSong(
                music_id=str(d.get("id") or row.get("id")),
                singers=tuple(d.get("characterIds") or ()),
                singer_type=str(d.get("musicSingerType") or "").rsplit("_", 1)[-1].lower(),
                rating_target=bool(d.get("isHighestScoreRatingTarget")),
                start_ms=int(d.get("startTime") or 0),
            )
        )
    return out


def rated_holomems(songs: list[rating_rules.RatingSong]) -> list[str]:
    """The holomems some song counts for, in id order."""
    return sorted({c for s in songs for c in s.singers if rating_rules.counts_for(s, c)})


def decode_userdata(body: bytes) -> tuple[list[str], dict]:
    """`(owned music ids, {holomem: {music: {difficulty: {score, rating, updated}}}})` from an
    inflated `User/Get` body."""
    schema = snap.load_userdata_schema()
    if not schema:
        raise SystemExit(
            f"UserData schema not found -- holomem_rating needs the .proto tree at {snap.PROTO_DIR}"
        )
    level = snap.locate_userdata(body, schema)
    if not level:
        raise SystemExit("no UserData message located in the User/Get response")
    owned: list[str] = []
    records: dict[str, dict[str, dict[str, dict]]] = {}
    for fn, wt, payload in level:
        if wt != 2 or fn not in schema:
            continue
        name = schema[fn]
        if name == "user_music_list":
            row = snap.sub(payload, MUSIC_SUB)
            if row.get("music_id"):
                owned.append(str(row["music_id"]))
        elif name == "user_music_character_highest_score_list":
            chr_id = music_id = None
            infos = []
            for f2, _w2, v2 in snap.fields(payload):
                if f2 == 2:
                    chr_id = v2.decode()
                elif f2 == 3:
                    music_id = v2.decode()
                elif f2 == 100:
                    infos.append({INFO_SUB[a]: b for a, _, b in snap.fields(v2) if a in INFO_SUB})
            if not (chr_id and music_id):
                continue
            for info in infos:
                diff = DIFFICULTY.get(int(info.get("difficulty") or 0))
                if diff is None:
                    continue
                records.setdefault(chr_id, {}).setdefault(music_id, {})[diff] = {
                    "score": int(info.get("score") or 0),
                    "rating": int(info.get("rating") or 0),
                    "updated": int(info.get("updated") or 0),
                }
    return sorted(set(owned)), records


def _read(host, token, ident, master_version, path: str, body: bytes) -> bytes:
    res = snap.call(
        host, path, snap.frame(snap.seal(body)), token=token, ident=ident, master_version=master_version
    )
    return _payload(path, res)


def decode_ranks(body: bytes) -> dict[str, int]:
    """`{holomem: rank}` from a `ListHighestScoreRatingRankingRankResponse` (rank_infos=1
    {rank=1, character_id=2}); an unranked holomem is left out."""
    out: dict[str, int] = {}
    for fn, wt, v in snap.fields(body):
        if fn == 1 and wt == 2:
            info = {a: b for a, _, b in snap.fields(v)}
            chr_id = info.get(2)
            if isinstance(chr_id, bytes) and info.get(1):
                out[chr_id.decode()] = int(info[1])
    return out


def decode_borders(body: bytes) -> tuple[list[dict], int]:
    """`([{rank, rating}], updated ms)` from a `...ThresholdRankingRankInfoResponse`
    (threshold_rank_infos=1 {rank=1, score=2}, updated_ranking_time=2). The ranking's `score`
    is the holomem's rating."""
    rows: list[dict] = []
    updated = 0
    for fn, wt, v in snap.fields(body):
        if fn == 1 and wt == 2:
            info = {a: b for a, _, b in snap.fields(v)}
            rows.append({"rank": int(info.get(1) or 0), "rating": int(info.get(2) or 0)})
        elif fn == 2 and wt == 0:
            updated = int(v)
    rows.sort(key=lambda r: r["rank"])
    return rows, updated


# The border request names one holomem (its message has no list), so the 54 reads go a few at a
# time on one connection: 8.8 s one by one, 2.2 s with 4 in flight, identical answers. The server
# serialises an account's requests behind a lock and refuses with LOCK_FAILED when too many wait
# on it (seen once at 16 in flight, never at 4-8); a refused read is retried on its own.
BORDER_STREAMS = 4


def _payload(path: str, res: tuple[dict, dict, bytes]) -> bytes:
    rh, tr, data = res
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st not in (None, "0"):
        raise RuntimeError(f"{path} refused: grpc-status {st} {tr.get('grpc-message', '')}")
    for msg in snap.unframe(data):
        if msg:
            return snap.unseal(msg)
    return b""


def read_borders(host, token, ident, master_version, holomems: list[str]) -> list[bytes]:
    """Each holomem's border response, in order: BORDER_STREAMS at a time, then one by one for
    any the server refused for its lock."""
    bodies = [snap.pb_bytes_field(1, c.encode()) for c in holomems]
    answers = snap.call_many(
        host,
        [(BORDER_PATH, snap.frame(snap.seal(b))) for b in bodies],
        streams=BORDER_STREAMS,
        token=token,
        ident=ident,
        master_version=master_version,
    )
    out = []
    for body, res in zip(bodies, answers, strict=True):
        if "LOCK_FAILED" in res[1].get("grpc-message", ""):
            out.append(_read(host, token, ident, master_version, BORDER_PATH, body))
        else:
            out.append(_payload(BORDER_PATH, res))
    return out


def read_ranking(host, token, ident, master_version, holomems: list[str]) -> dict:
    """The account's rank on every holomem's ranking and each one's reward borders."""
    body = b"".join(snap.pb_bytes_field(1, c.encode()) for c in holomems)
    ranks = decode_ranks(_read(host, token, ident, master_version, RANK_PATH, body))
    borders: dict[str, list[dict]] = {}
    updated: dict[str, int] = {}
    for chr_id, answer in zip(
        holomems, read_borders(host, token, ident, master_version, holomems), strict=True
    ):
        rows, at = decode_borders(answer)
        borders[chr_id] = rows
        if at:
            updated[chr_id] = at
    return {
        "read_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ranks": ranks,
        "borders": borders,
        "borders_updated": updated,
    }


def summary(records: dict) -> dict[str, int]:
    """`{holomem: rating}` from the records."""
    out = {}
    for chr_id, by_song in records.items():
        per_song = {m: max((d["rating"] for d in diffs.values()), default=0) for m, diffs in by_song.items()}
        out[chr_id] = rating_rules.holomem_rating(per_song)
    return out


def previous_ranking() -> dict | None:
    try:
        return json.loads(OUT.read_text("utf-8")).get("ranking")
    except (OSError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="say what would be sent")
    parser.add_argument("--no-ranking", action="store_true", help="read only the records (User/Get)")
    args = parser.parse_args()

    songs = rating_songs(json.loads(MUSIC_JSON.read_text("utf-8")))
    holomems = rated_holomems(songs)
    if args.dry_run:
        host, credential = snap.load_credential("main")
        print(f"host       : {host}")
        print(f"credential : {len(credential)} B, not printed")
        print(f"would POST : /rpc.api.Master/Get, /rpc.api.Auth/Login, /rpc.api.User/Get, {RANK_PATH},")
        print(f"             {BORDER_PATH} x {len(holomems)}")
        print(f"would write: {OUT}")
        return 0

    host, ident, master_version, token = snap.session("main")
    owned, records = decode_userdata(snap.userdata(host, token, ident, master_version))
    if not owned:
        # an empty write would read as an account that owns no song
        print("no user_music_list decoded -- refusing to write", file=sys.stderr)
        return 1

    ranking = previous_ranking()
    ranking_note = None
    if not args.no_ranking:
        try:
            ranking = read_ranking(host, token, ident, master_version, holomems)
        except RuntimeError as exc:
            if "DATE_CHANGED" not in str(exc):
                raise
            ranking_note = (
                "the in-game day has rolled over: the ranking reads are refused until the game "
                "client is opened once"
            )
            print(f"  ranking: {ranking_note}; the last read is kept", file=sys.stderr)

    totals = summary(records)
    document = {
        "_source": "live User/Get + Music ranking reads",
        "_captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "owned_songs": owned,
        "records": records,
        "ratings": totals,
        "total": sum(totals.values()),
        "ranking": ranking,
        "ranking_note": ranking_note,
    }
    DUMPS.mkdir(parents=True, exist_ok=True)
    atomic_io.write_bytes(OUT, json.dumps(document, indent=1, ensure_ascii=False).encode("utf-8"))
    ranked = len((ranking or {}).get("ranks") or {})
    print(
        f"wrote {OUT}  ({len(owned)} songs owned, {len(records)} holomems with records, "
        f"total rating {document['total']:,}, {ranked} holomems ranked)"
    )
    return 0


if __name__ == "__main__":
    run_cli(main)
