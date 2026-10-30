"""Event ranking TOP 100: the latest board and how each player moves, from the 24/7 tracker.

READ-ONLY. The store is `tools/recon/proxy/dumps/cutoffs/<region>/<chapter_id>.top.jsonl`,
written on the VPS by `tools/recon/proxy/event_tracker.py` and merged here by `tracker_pull.py`:
one line per read, `{captured_at, final, region, event_id, chapter_id, kind, rows: [[rank, score,
public_user_id, name], ...]}`. Modelled on the community tool holodori.best's leaderboard (rank
and Event Pt change over the last hour, a player's own series), but from reads every couple of
minutes instead of every one, so "gains" are counted per read, not per Live: two Lives between
two reads count once.
"""
from __future__ import annotations

import json
from pathlib import Path

import cutoffs_view

HOUR_MS = 3600 * 1000
PACE_WINDOW_MS = 3 * HOUR_MS
_CACHE: dict[Path, tuple[float, int, list[dict]]] = {}


def read_top(path: Path) -> list[dict]:
    """Every read of one chapter, oldest first (cached on the file's mtime and size)."""
    try:
        st = path.stat()
    except OSError:
        return []
    hit = _CACHE.get(path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    reads = []
    for line in path.read_bytes().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue  # a torn last line: skip, never fail
        if isinstance(r, dict) and r.get("captured_at") and isinstance(r.get("rows"), list):
            reads.append(r)
    reads.sort(key=lambda r: r["captured_at"])
    _CACHE[path] = (st.st_mtime, st.st_size, reads)
    return reads


def _at_or_before(reads: list[dict], t: int) -> dict | None:
    best = None
    for r in reads:
        if r["captured_at"] <= t:
            best = r
        else:
            break
    return best


def board(reads: list[dict], *, now: int | None = None) -> list[dict]:
    """The latest read's rows, each with its last-hour movement (pure)."""
    if not reads:
        return []
    last = reads[-1]
    t = last["captured_at"]
    hour = _at_or_before(reads, t - HOUR_MS)
    pace_from = _at_or_before(reads, t - PACE_WINDOW_MS) or reads[0]
    by = lambda r: {row[2]: row for row in (r or {}).get("rows", []) if row[2]}  # noqa: E731
    h, p = by(hour), by(pace_from)
    # the reads of the last hour, plus the one before it as the baseline of the first gain
    recent = [r for r in reads if r["captured_at"] > t - HOUR_MS]
    if hour is not None:
        recent.insert(0, hour)
    out = []
    for rank, score, uid, name in last["rows"]:
        gains, prev, last_gain = 0, None, None
        for r in recent:
            row = next((x for x in r["rows"] if x[2] == uid), None)
            if row is None:
                continue
            if prev is not None and row[1] > prev:
                gains += 1
                last_gain = r["captured_at"]
            prev = row[1]
        old = h.get(uid) if hour else None
        base = p.get(uid)
        span = t - pace_from["captured_at"]
        out.append({
            "rank": rank, "score": score, "uid": uid, "name": name,
            # null when the hour-ago read did not have them in the top 100 (or there is none)
            "gain_1h": (score - old[1]) if old else None,
            "rank_1h": (old[0] - rank) if old else None,
            "reads_with_gain_1h": gains,
            "last_gain_at": last_gain,
            "pace_h": round((score - base[1]) * HOUR_MS / span) if base and span >= HOUR_MS else None,
        })
    return out


def player_series(reads: list[dict], uid: str) -> list[list[int]]:
    """[[captured_at, score], ...] of one player while in the top 100."""
    out = []
    for r in reads:
        row = next((x for x in r["rows"] if x[2] == uid), None)
        if row is not None:
            out.append([r["captured_at"], row[1]])
    return out


def top_files(store: Path, region: str) -> list[Path]:
    d = store / region
    return sorted(d.glob("*.top.jsonl")) if d.is_dir() else []


def build_top(store: Path, region: str, *, now: int, chapter_id: str | None = None,
              uid: str | None = None, kind: str | None = None) -> dict:
    """The `/api/event/top` payload. Raises LookupError when nothing is stored. One chapter's
    file holds every ranking's boards (`kind` points / total / music:<id>); `kind` picks one,
    by default the chapter's main ranking (points, else total)."""
    files = {p.name[:-len(".top.jsonl")]: p for p in top_files(store, region)}
    if not files:
        raise LookupError(f"no top-100 reads stored for region {region!r} -- the 24/7 tracker "
                          "writes them (tools/recon/proxy/event_tracker.py)")
    metas = [m for m in cutoffs_view.list_chapters(store, region) if m.get("chapter_id") in files]
    if chapter_id is None:
        open_now = [m for m in metas if (m.get("start_time") or 0) <= now
                    <= cutoffs_view.chapter_end(m)]
        chapter_id = (open_now or metas or [{"chapter_id": sorted(files)[-1]}])[0]["chapter_id"]
    if chapter_id not in files:
        raise LookupError(f"no top-100 reads for chapter {chapter_id!r}")
    meta = next((m for m in metas if m.get("chapter_id") == chapter_id), {}) or {}
    every = read_top(files[chapter_id])
    kinds = sorted({r.get("kind") or "points" for r in every},
                   key=lambda k: (k != "points", k != "total", k))
    if kind is None:
        kind = next((k for k in kinds if not k.startswith("music:")), kinds[0] if kinds else "points")
    reads = [r for r in every if (r.get("kind") or "points") == kind]
    last = reads[-1] if reads else {}
    out = {
        "region": region, "chapter_id": chapter_id, "event_id": meta.get("event_id"),
        "event_name": meta.get("event_name"), "chapter_number": meta.get("chapter_number"),
        "start_time": meta.get("start_time"), "end_time": cutoffs_view.chapter_end(meta) or None,
        "now": now, "kind": kind, "kinds": kinds, "captured_at": last.get("captured_at"),
        "final": bool(last.get("final")), "reads": len(reads),
        "first_read": reads[0]["captured_at"] if reads else None,
        "chapters": [{"chapter_id": c, "chapter_number": next(
            (m.get("chapter_number") for m in metas if m.get("chapter_id") == c), None)}
            for c in sorted(files)],
        "rows": board(reads, now=now),
    }
    if uid:
        out["player"] = {"uid": uid, "series": player_series(reads, uid)}
    return out
