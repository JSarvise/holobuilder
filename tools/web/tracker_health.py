"""Is the 24/7 event tracker still reading? The Event page's warning when it is not.

The tracker on the VPS (`tools/refresh/event_tracker.py`) records per region when it last stored a border read (`borders_at` in
its `status.json`, pulled to `dumps/cutoffs/tracker_status.json` by `tracker_pull.py`). While an
event is open it reads every 5 min, so a region silent for longer than STALE_AFTER_S during an
open event is a tracker in trouble (a refused client version, an expired proxy, a dead
service) and the page says so. Outside an event nothing is read and nothing is reported.

"Open" comes from two sources, either is enough: the events the account refresh listed
(`event_meta.json`, `starts_at` / `ends_at`), and the chapters the tracker itself stored
(`<region>/*.meta.json`, `start_time` / `chapter_end_time` in ms). A status file that stopped
arriving (the pull fails) carries old read times, so it raises the same warning.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

STALE_AFTER_S = 30 * 60


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def event_open(event_meta: dict | None, chapter_metas: list[dict], now: float) -> bool:
    """True while any listed event or stored chapter is between its start and its end."""
    for ev in (event_meta or {}).get("events") or []:
        start, end = _ts(ev.get("starts_at")), _ts(ev.get("ends_at"))
        if start is not None and end is not None and start <= now <= end:
            return True
    for ch in chapter_metas:
        start, end = ch.get("start_time"), ch.get("chapter_end_time")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) \
                and start / 1000 <= now <= end / 1000:
            return True
    return False


def stale_regions(status: dict | None, now: float, stale_after: float = STALE_AFTER_S) -> list[dict]:
    """The regions whose last border read is older than `stale_after` (or that never read).
    A status from before `borders_at` existed falls back to `ok_at`."""
    out = []
    for r in (status or {}).get("regions") or []:
        last = r.get("borders_at") if "borders_at" in r else r.get("ok_at")
        if last is not None and now - last <= stale_after:
            continue
        out.append({"region": r.get("region"), "last_read_at": last,
                    "minutes": None if last is None else int((now - last) // 60),
                    "error": r.get("error")})
    return out


def _read(path: Path):
    try:
        return json.loads(path.read_text("utf8"))
    except (OSError, ValueError):
        return None


def health(store: Path, event_meta_path: Path, now: float) -> dict:
    """{"open", "stale": [...], "stale_after_min", "status": bool} for the Event page."""
    status = _read(store / "tracker_status.json")
    metas = [m for m in (_read(p) for p in sorted(store.glob("*/*.meta.json"))) if isinstance(m, dict)]
    is_open = event_open(_read(event_meta_path), metas, now)
    return {"open": is_open, "status": status is not None,
            "stale": stale_regions(status, now) if is_open and status is not None else [],
            "stale_after_min": STALE_AFTER_S // 60}
