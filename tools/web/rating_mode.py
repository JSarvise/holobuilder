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

import dataclasses
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
# The difficulty the one-team engine is built at: its charts are each song's own, this names
# the leaf it starts from and the judge its board plans are held under (a row's detail
# rebuilds the board on the same leaf, `row_detail.two_stage_detail`'s `shared_board`).
ENGINE_DIFFICULTY = "EXPERT"


def one_board_cast(holomem: str, singer_types: Sequence[str]):
    """The cast ONE board for every song of a holomem's rating is planned for
    (`RefineContext.board_cast`): her, with the songs' singer type when they all share one."""
    from hbuilder.domain.board_plan import SongCast  # noqa: PLC0415

    scopes = {t or "" for t in singer_types}
    return SongCast((holomem,), scopes.pop() if len(scopes) == 1 else "")


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
    """`{music: {DIFFICULTY: {level, coefficient, k, notes}}}` of every chart on disk."""
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
            # `MusicDifficultyChart` field 7, x1 when the chart file predates it
            "k": int(chart.get("score_coefficient_permyriad") or 10000),
            "notes": len(notes) if isinstance(notes, list) else int(notes or 0),
        }
    return out


def chart_scale(level: int, coefficient_permil: int, notes: int, k_permyriad: int = 10000) -> float:
    """A team-free estimate of how much a chart can score, to order a holomem's songs before
    any is searched: the music coefficient (`1 + coef/1000 x (level - 5)`) times the chart's
    own factor (`k_permyriad / 10000`), the two factors every note carries, times the mean
    combo bonus (+1 % per 100 notes, up to +10 % from 1000)."""
    mc = (1.0 + coefficient_permil / 1000.0 * (level - 5)) * (k_permyriad / 10000.0)
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
                "scale": round(chart_scale(c["level"], c["coefficient"], c["notes"], c.get("k", 10000)), 5),
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


# A song's other chart (HARD for an EXPERT pick, `ChartLibrary.contender`) changes the answer only
# when its #1 rates higher: a value above the nominal #1's rating border (`_border`; a tie keeps the
# nominal chart). The nominal #1 team is scored there first (the probe), and the full search runs
# only when the probe is within this gap of that border. Over 72 (holomem, song) pairs (57 maxed,
# 15 of them drawn at random, and 15 on the account; objective Max) the best HARD team seen beat
# the probe by at most 1.10 %, and leaving any one song out of the measure misses no HARD win; the
# full search runs on 7 of the 57 maxed and 3 of the 15 account songs.
CHART_SEARCH_GAP = 0.0125
# by the expected score (no row of the measure above): the Score check's gap, measured on that
# objective (`jobs.CHART_SEARCH_GAP`)
CHART_SEARCH_GAP_EXPECTED = 0.0185


def _cancelled(cancel) -> None:
    if cancel is not None and cancel.is_set():
        from ondemand import Cancelled  # noqa: PLC0415

        raise Cancelled()


def _search_rows(eng, music_id: str, filters, opts: Mapping, top: int, cancel) -> tuple[dict, list[dict]]:
    """`(payload, rows)`: one search of `music_id` on `eng` (released here), its first `top`
    teams with their rating (by Max or by expected, the run's objective)."""
    objective = opts.get("objective") or "max"
    try:
        payload = eng.search(
            music_id,
            filters,
            emit=None,
            cancel=cancel,
            top=max(top, 3),
            objective=objective,
            **({"assumptions": opts["assumptions"]} if opts.get("assumptions") is not None else {}),
        )
    finally:
        eng.release()
    rows = []
    for r in (payload.get("ranking") or [])[:top]:
        value = r.get("song_max") if objective == "max" and r.get("song_max") is not None else r.get("best")
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
                # False: a MILP of the team's plan within the materials stopped on its time limit
                **({"plan_optimal": r["plan_optimal"]} if "plan_optimal" in r else {}),
            }
        )
    return payload, rows


def _value(row: Mapping, objective: str) -> float:
    """The figure a row is rated by: its Max, or its expected score (`objective`)."""
    v = row.get("max") if objective == "max" and row.get("max") is not None else row.get("expected")
    return float(v or 0)


def _entry(song: Mapping, payload: dict, rows: list[dict], current: Mapping) -> dict:
    mine = current.get(song["music_id"]) or {}
    best = rows[0]["rating"] if rows else 0
    per_song = {m: int(v["rating"]) for m, v in current.items()}
    return {
        **song,
        "rows": rows,
        "rating": best,
        "current_rating": int(mine.get("rating") or 0),
        "gain": rating.gain(per_song, song["music_id"], best),
        "seconds": payload.get("seconds"),
        "materials_failed": payload.get("materials_failed") or [],
        "basis": payload.get("basis"),
    }


def _border(value: float) -> float:
    """The highest value that still rates as `value` does (`rating.song_rating`): another
    chart's team must score above it to rate higher."""
    return float(rating.song_rating(value) * rating.RATING_DENOMINATOR)


def _team(row: Mapping) -> tuple:
    return row["leader"], tuple(sorted(row["members"]))


def _probe(entry: dict, first: dict, opts: Mapping, engine_for, cancel) -> tuple[float | None, dict | None]:
    """`(value, row or None)`: the nominal #1 team on the song's other chart, scored directly at
    the number a search serves it at (`Engine.served_value`: its best board, or on the account
    its plan within the materials for that chart; no row); else searched pinned to the team
    (its row)."""
    other = entry["contender"]["difficulty"]
    objective = opts.get("objective") or "max"
    eng = engine_for(other)
    try:
        value = eng.served_value(
            entry["music_id"], first["leader"], first["members"], opts.get("assumptions"), objective
        )
    except BaseException:
        eng.release()
        raise
    if value is not None:
        eng.release()
        return value, None
    pinned = dataclasses.replace(
        opts["filters"], leader_card=first["leader"], require=tuple(first["members"])
    )
    _p, probe = _search_rows(eng, entry["music_id"], pinned, opts, 1, cancel)
    return (_value(probe[0], objective), probe[0]) if probe else (None, None)


def _check_chart(entry: dict, opts: Mapping, engine_for, current: Mapping, cancel) -> dict:
    """`entry` on its song's other chart when that one rates higher, else `entry`; either way
    with its `chart_check` (what was tried). The rating keeps a song's best difficulty, so the
    holomem's best run of a song may be on HARD (an EXPERT with many slide points pays each note
    less). The nominal #1 team is scored there first (`_probe`); only a probe within
    `CHART_SEARCH_GAP` (by the expected score, `CHART_SEARCH_GAP_EXPECTED`) of the nominal
    #1's rating border earns a full search. The other chart's #1 is the
    better of that search's #1 and the probe (a search is not a ceiling: it can settle below a
    team already scored), and it wins on a higher rating (a tie keeps the nominal chart)."""
    other = entry["contender"]
    rows = entry["rows"]
    check: dict = {
        "difficulty": entry["difficulty"],
        "level": entry.get("level"),
        "other": other["difficulty"],
        "other_level": other.get("level"),
        "rating": entry["rating"],
        "searched": False,
        "wins": False,
    }
    if not rows:
        return {**entry, "chart_check": check}
    first = rows[0]
    teams = int(opts.get("teams") or 3)
    objective = opts.get("objective") or "max"
    _cancelled(cancel)
    probe_value, probe_row = _probe(entry, first, opts, engine_for, cancel)
    check["probe_rating"] = rating.song_rating(probe_value) if probe_value is not None else None
    gap = CHART_SEARCH_GAP if objective == "max" else CHART_SEARCH_GAP_EXPECTED
    if probe_value is None or probe_value * (1.0 + gap) <= _border(_value(first, objective)):
        return {**entry, "chart_check": check}
    _cancelled(cancel)
    payload, alt = _search_rows(
        engine_for(other["difficulty"]), entry["music_id"], opts["filters"], opts, teams, cancel
    )
    if not alt or probe_value > _value(alt[0], objective):
        # the probe's team beats the search's #1: served with its own row
        if probe_row is None:
            _cancelled(cancel)
            pinned = dataclasses.replace(
                opts["filters"], leader_card=first["leader"], require=tuple(first["members"])
            )
            _p, got = _search_rows(
                engine_for(other["difficulty"]), entry["music_id"], pinned, opts, 1, cancel
            )
            probe_row = got[0] if got else None
        if probe_row is not None:
            alt = [probe_row, *(r for r in alt if _team(r) != _team(probe_row))][:teams]
    check.update(searched=True, other_rating=alt[0]["rating"] if alt else None)
    if not alt or alt[0]["rating"] <= entry["rating"]:
        return {**entry, "chart_check": check}
    check["wins"] = True
    won = {**entry, "difficulty": other["difficulty"], "level": other.get("level")}
    return {**_entry(won, payload, alt, current), "chart_check": check}


def run_each_song(
    opts: Mapping,
    *,
    engine_for: Callable[[str], Any],
    emit: Callable[[dict], None],
    cancel=None,
) -> dict:
    """One search per song of `opts["songs"]` (each `{music_id, difficulty, ...}`), the
    holomem leading. `engine_for(difficulty)` -> an acquired engine (released here). Emits a
    `song` event per song as it is done. Then each song with a `contender` (its other chart,
    `{difficulty, level}`) is tried there (`_check_chart`), the songs grouped by that chart;
    a song whose other chart rates higher is sent again (`song`) on it."""
    current = opts.get("current") or {}
    objective = opts.get("objective") or "max"
    teams = int(opts.get("teams") or 3)
    songs = opts["songs"]
    # by difficulty, so each engine is started once
    order = sorted(range(len(songs)), key=lambda k: (songs[k]["difficulty"], k))
    results: dict[int, dict] = {}
    for done, k in enumerate(order):
        _cancelled(cancel)
        song = songs[k]
        emit(
            {
                "type": "progress",
                "stage": "songs",
                "done": done,
                "total": len(order),
                "song": song["music_id"],
            }
        )
        payload, rows = _search_rows(
            engine_for(song["difficulty"]), song["music_id"], opts["filters"], opts, teams, cancel
        )
        results[k] = _entry(song, payload, rows, current)
        emit({"type": "song", "song": results[k]})
    checks = sorted(
        (k for k in range(len(songs)) if songs[k].get("contender")),
        key=lambda k: (songs[k]["contender"]["difficulty"], k),
    )
    for done, k in enumerate(checks):
        _cancelled(cancel)
        emit(
            {
                "type": "progress",
                "stage": "charts",
                "done": done,
                "total": len(checks),
                "song": songs[k]["music_id"],
            }
        )
        checked = _check_chart(results[k], opts, engine_for, current, cancel)
        results[k] = checked
        if checked["chart_check"]["wins"]:
            emit({"type": "song", "song": checked})
    out = [results[k] for k in range(len(songs))]
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
    songs_ = list(opts["songs"])[:MAX_MERGED_SONGS]
    keys = tuple(f"{s['music_id']}/{s['difficulty']}" for s in songs_)
    current = opts.get("current") or {}
    objective = opts.get("objective") or "max"
    cast = None
    if opts.get("one_board", True):
        cast = one_board_cast(opts["holomem"], [s.get("singer_type") or "" for s in songs_])
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
    per_song = {m: int(v["rating"]) for m, v in current.items()}
    for row in payload.get("rows") or []:
        for c in row["charts"]:
            s = by_key.get(c["key"]) or {}
            c.update({k: s.get(k) for k in ("music_id", "title", "difficulty", "level")})
            c["current_rating"] = int((current.get(s.get("music_id")) or {}).get("rating") or 0)
            # what this song alone adds to the holomem's rating (her three best songs), as
            # "A team per song" reports it; above her record is not the same thing
            c["gain"] = rating.gain(per_song, str(s.get("music_id") or ""), int(c.get("rating") or 0))
        row["predicted"] = predicted(current, {c["music_id"]: c["rating"] for c in row["charts"]})
    return {
        "mode": "one",
        "objective": objective,
        "songs": songs_,
        **payload,
        "predicted": (payload.get("rows") or [{}])[0].get("predicted"),
    }
