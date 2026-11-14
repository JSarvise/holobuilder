"""The Optimizer's Holomem Rating goal: which songs count for a holomem, where the account
stands (`dumps/holomem_rating.json`, the refresh's `holomem_rating` step), and the two
searches.

- **Each song**: one search per song, the holomem leading (a run is credited to its leader),
  ranked by Max (every ACTIVE fires: the high-score ranking keeps a player's best run) or by
  the expectation. Each song is played at its hardest chart within the player's level.
- **One team**: a single team for the holomem's songs, ranked by the rating it would leave
  (`rating.merged_value`), on one board for every song or a board per song.

The rating rules themselves are `hbuilder.domain.rating`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from hbuilder import paths
from hbuilder.domain import rating

RATING_PATH = paths.DUMPS / "holomem_rating.json"

# Songs searched when the request names no count, and the most a request may ask for (a
# song search is ~10-25 s).
DEFAULT_SONGS = 5
MAX_SONGS = 20
# A team for several songs is searched on at most this many charts (each team is scored on
# every one of them).
MAX_MERGED_SONGS = 8


def _read_json(path) -> Any:
    import json  # noqa: PLC0415

    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def rating_doc() -> dict | None:
    doc = _read_json(RATING_PATH)
    return doc if isinstance(doc, dict) else None


def music_songs() -> dict[str, rating.RatingSong]:
    """Every song of the master as a `RatingSong`, by music id."""
    from hbweb import songs  # noqa: PLC0415

    out = {}
    for d in songs.music_rows():
        music_id = str(d.get("id") or "")
        if not music_id:
            continue
        out[music_id] = rating.RatingSong(
            music_id=music_id,
            singers=tuple(d.get("characterIds") or ()),
            singer_type=str(d.get("musicSingerType") or "").rsplit("_", 1)[-1].lower(),
            rating_target=bool(d.get("isHighestScoreRatingTarget")),
            start_ms=int(d.get("startTime") or 0),
        )
    return out


def chart_table() -> dict[str, dict[str, dict]]:
    """`{music: {DIFFICULTY: {level, coefficient, notes}}}` of every chart on disk."""
    from hbweb import config  # noqa: PLC0415

    raw = _read_json(config.CHARTS_ALL_PATH) or {}
    out: dict[str, dict[str, dict]] = {}
    for chart in (raw.get("charts") or {}).values():
        if not isinstance(chart, dict):
            continue
        notes = chart.get("notes")
        out.setdefault(str(chart.get("music_id") or ""), {})[str(chart.get("difficulty") or "").upper()] = {
            "level": int(chart.get("difficulty_level") or 0),
            "coefficient": int(chart.get("live_score_coefficient_permil") or 0),
            "notes": len(notes) if isinstance(notes, list) else int(notes or 0),
        }
    return out


def chart_scale(level: int, coefficient_permil: int, notes: int) -> float:
    """A team-free estimate of how much a chart can score, to order a holomem's songs before
    any is searched: the music coefficient (`1 + coef/1000 x (level - 5)`, the factor every
    note carries) times the mean combo bonus (+1 % per 100 notes, up to +10 % from 1000)."""
    mc = 1.0 + coefficient_permil / 1000.0 * (level - 5)
    n = max(1, notes)
    combo = sum(min(k // 100, 10) / 100.0 for k in range(1, n + 1)) / n
    return mc * (1.0 + combo)


def holomem_names() -> dict[str, str]:
    from hbweb import config  # noqa: PLC0415

    tree = _read_json(config.SKILLTREE_PATH) or {}
    return {m["chr"]: m["name"] for m in tree.get("members") or [] if m.get("chr") and m.get("name")}


def current_per_song(doc: Mapping | None, chr_id: str) -> dict[str, dict]:
    """`{music: {rating, score, difficulty}}`: the holomem's record on each song, at its best
    difficulty."""
    out: dict[str, dict] = {}
    for music_id, diffs in ((doc or {}).get("records") or {}).get(chr_id, {}).items():
        best = max(
            diffs.items(), key=lambda kv: (kv[1].get("rating", 0), kv[1].get("score", 0)), default=None
        )
        if best:
            out[music_id] = {
                "rating": int(best[1].get("rating") or 0),
                "score": int(best[1].get("score") or 0),
                "difficulty": best[0],
            }
    return out


def next_border(borders: Sequence[Mapping], rank: int | None, rating_now: int) -> dict | None:
    """The next reward border above the account: the best tier it is not in yet (the top
    1000 when it is not ranked), with the rating it takes."""
    better = [b for b in borders if b.get("rank") and (rank is None or int(b["rank"]) < rank)]
    if not better:
        return None
    target = max(better, key=lambda b: int(b["rank"]))
    need = int(target.get("rating") or 0)
    return {"rank": int(target["rank"]), "rating": need, "gap": max(0, need - rating_now)}


def song_pool(
    chr_id: str,
    *,
    max_level: int,
    owned_only: bool = True,
    doc: Mapping | None = None,
    now_ms: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """`(songs, left out)` for `chr_id`'s rating: each counted song at its hardest chart within
    `max_level` (`rating.pick_chart`), best estimated first; left out, each with why."""
    from hbweb import songs  # noqa: PLC0415

    doc = rating_doc() if doc is None else doc
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    owned = set((doc or {}).get("owned_songs") or ())
    current = current_per_song(doc, chr_id)
    titles, _singers = songs.song_labels()
    names = holomem_names()
    charts = chart_table()
    out, skipped = [], []
    for music_id, song in music_songs().items():
        if not rating.counts_for(song, chr_id, now_ms):
            continue
        base = {
            "music_id": music_id,
            "title": titles.get(music_id) or music_id,
            "singer_type": song.singer_type,
            "singers": [names.get(c, c) for c in song.singers],
            "owned": music_id in owned,
            "current": current.get(music_id),
        }
        levels = {d: c["level"] for d, c in charts.get(music_id, {}).items()}
        if not levels:
            skipped.append({**base, "why": "no chart on this PC yet (refresh)"})
            continue
        if owned_only and owned and music_id not in owned:
            skipped.append({**base, "why": "you do not own this song"})
            continue
        difficulty = rating.pick_chart(levels, max_level)
        if difficulty is None:
            skipped.append({**base, "why": f"every chart is above level {max_level}"})
            continue
        c = charts[music_id][difficulty]
        out.append(
            {
                **base,
                "difficulty": difficulty,
                "level": c["level"],
                "scale": round(chart_scale(c["level"], c["coefficient"], c["notes"]), 5),
            }
        )
    out.sort(key=lambda s: (-s["scale"], s["music_id"]))
    return out, skipped


def status(chr_id: str, *, max_level: int, owned_only: bool = True) -> dict:
    """Where the account stands on `chr_id`'s ranking, and the songs that count for it."""
    doc = rating_doc()
    current = current_per_song(doc, chr_id)
    per_song = {m: r["rating"] for m, r in current.items()}
    now = rating.holomem_rating(per_song)
    ranking = (doc or {}).get("ranking") or {}
    rank = (ranking.get("ranks") or {}).get(chr_id)
    borders = (ranking.get("borders") or {}).get(chr_id) or []
    pool, skipped = song_pool(chr_id, max_level=max_level, owned_only=owned_only, doc=doc)
    return {
        "holomem": chr_id,
        "name": holomem_names().get(chr_id, chr_id),
        "have_records": doc is not None,
        "captured_at": (doc or {}).get("_captured_at"),
        "rating": now,
        "top": [{"music_id": m, "rating": r} for m, r in rating.top_songs(per_song)],
        "rank": rank,
        "borders": borders,
        "next_border": next_border(borders, rank, now),
        "ranking_read_at": ranking.get("read_at"),
        "ranking_note": (doc or {}).get("ranking_note"),
        "account_total": (doc or {}).get("total"),
        "songs": pool,
        "skipped": skipped,
    }


def holomem_list() -> list[dict]:
    """Every holomem some song counts for, with the account's rating and rank on hers."""
    doc = rating_doc() or {}
    ratings = doc.get("ratings") or {}
    ranks = ((doc.get("ranking") or {}).get("ranks")) or {}
    names = holomem_names()
    rated = {c for s in music_songs().values() for c in s.singers if rating.counts_for(s, c)}
    return sorted(
        (
            {"chr": c, "name": names.get(c, c), "rating": int(ratings.get(c) or 0), "rank": ranks.get(c)}
            for c in rated
        ),
        key=lambda r: r["chr"],
    )


# ---------------------------------------------------------------------------- searches
def predicted(current: Mapping[str, Mapping], new: Mapping[str, int]) -> dict:
    """The holomem's rating now and once each song in `new` (music -> rating) is played."""
    per_song = {m: int(r["rating"]) for m, r in current.items()}
    after = rating.rating_with(per_song, new)
    merged = dict(per_song)
    for m, r in new.items():
        if r > merged.get(m, 0):
            merged[m] = r
    return {
        "now": rating.holomem_rating(per_song),
        "after": after,
        "gain": after - rating.holomem_rating(per_song),
        "top": [
            {"music_id": m, "rating": r, "new": r > per_song.get(m, 0)} for m, r in rating.top_songs(merged)
        ],
    }


def run_each_song(
    opts: Mapping,
    *,
    engine_for: Callable[[str], Any],
    emit: Callable[[dict], None],
    cancel=None,
) -> dict:
    """One search per song of `opts["songs"]` (each `{music_id, difficulty, ...}`), the
    holomem leading. `engine_for(difficulty)` -> an acquired engine (released here). Emits a
    `song` event per song as it is done."""
    current = opts.get("current") or {}
    objective = opts.get("objective") or "max"
    teams = int(opts.get("teams") or 3)
    # by difficulty, so each engine is started once
    order = sorted(range(len(opts["songs"])), key=lambda k: (opts["songs"][k]["difficulty"], k))
    results: dict[int, dict] = {}
    for done, k in enumerate(order):
        if cancel is not None and cancel.is_set():
            from ondemand import Cancelled  # noqa: PLC0415

            raise Cancelled()
        song = opts["songs"][k]
        emit(
            {
                "type": "progress",
                "stage": "songs",
                "done": done,
                "total": len(order),
                "song": song["music_id"],
            }
        )
        eng = engine_for(song["difficulty"])
        try:
            payload = eng.search(
                song["music_id"],
                opts["filters"],
                emit=None,
                cancel=cancel,
                top=max(teams, 3),
                objective=objective,
                **({"assumptions": opts["assumptions"]} if opts.get("assumptions") is not None else {}),
            )
        finally:
            eng.release()
        rows = []
        for r in (payload.get("ranking") or [])[:teams]:
            value = (
                r.get("song_max") if objective == "max" and r.get("song_max") is not None else r.get("best")
            )
            rows.append(
                {
                    "leader": r["leader"],
                    "members": r["members"],
                    "member_names": r.get("member_names"),
                    "leader_name": r.get("leader_name"),
                    "expected": r.get("best"),
                    "max": r.get("song_max"),
                    "rating": rating.song_rating(float(value or 0)),
                    "member_order": r.get("member_order"),
                    "refined": r.get("refined"),
                }
            )
        mine = current.get(song["music_id"]) or {}
        best = rows[0]["rating"] if rows else 0
        per_song = {m: int(v["rating"]) for m, v in current.items()}
        entry = {
            **song,
            "rows": rows,
            "rating": best,
            "current_rating": int(mine.get("rating") or 0),
            "gain": rating.gain(per_song, song["music_id"], best),
            "seconds": payload.get("seconds"),
            "materials_failed": payload.get("materials_failed") or [],
            "basis": payload.get("basis"),
        }
        results[k] = entry
        emit({"type": "song", "song": entry})
    out = [results[k] for k in range(len(opts["songs"]))]
    return {
        "mode": "each",
        "objective": objective,
        "songs": out,
        "predicted": predicted(current, {s["music_id"]: s["rating"] for s in out}),
    }


def run_one_team(
    opts: Mapping,
    *,
    engine_for: Callable[[tuple[str, ...], frozenset | None], Any],
    emit: Callable[[dict], None],
    cancel=None,
) -> dict:
    """One team for the songs of `opts["songs"]` (`engine_for(chart keys, board cast)` -> an
    acquired engine, released here)."""
    from hbuilder.domain.board_plan import SongCast  # noqa: PLC0415

    songs_ = list(opts["songs"])[:MAX_MERGED_SONGS]
    keys = tuple(f"{s['music_id']}/{s['difficulty']}" for s in songs_)
    current = opts.get("current") or {}
    objective = opts.get("objective") or "max"
    cast = None
    if opts.get("one_board", True):
        scopes = {s.get("singer_type") or "" for s in songs_}
        cast = SongCast((opts["holomem"],), scopes.pop() if len(scopes) == 1 else "")
    emit({"type": "progress", "stage": "songs", "done": 0, "total": 1})
    eng = engine_for(keys, cast)
    try:
        payload = eng.search_merged(
            opts["filters"],
            current={m: int(v["rating"]) for m, v in current.items()},
            objective=objective,
            emit=emit,
            cancel=cancel,
            top=int(opts.get("teams") or 3),
            **({"assumptions": opts["assumptions"]} if opts.get("assumptions") is not None else {}),
        )
    finally:
        eng.release()
    by_key = {f"{s['music_id']}/{s['difficulty']}": s for s in songs_}
    for row in payload.get("rows") or []:
        for c in row["charts"]:
            s = by_key.get(c["key"]) or {}
            c.update({k: s.get(k) for k in ("music_id", "title", "difficulty", "level")})
            c["current_rating"] = int((current.get(s.get("music_id")) or {}).get("rating") or 0)
        row["predicted"] = predicted(current, {c["music_id"]: c["rating"] for c in row["charts"]})
    return {
        "mode": "one",
        "objective": objective,
        "songs": songs_,
        **payload,
        "predicted": (payload.get("rows") or [{}])[0].get("predicted"),
    }
