"""tracker_health.py: the Event page warns when the tracker stops reading during an open event."""
from __future__ import annotations

from pathlib import Path

import tracker_health as th

NOW = 1_793_900_000.0
OPEN = {"events": [{"event_id": "e", "starts_at": "2026-11-01T00:00:00+00:00",
                    "ends_at": "2026-12-01T00:00:00+00:00"}]}
CLOSED = {"events": [{"event_id": "e", "starts_at": "2026-10-01T00:00:00+00:00",
                      "ends_at": "2026-10-10T00:00:00+00:00"}]}


def _status(**regions):
    return {"regions": [{"region": k, **v} for k, v in regions.items()]}


def test_open_comes_from_the_listed_events_or_the_trackers_own_chapters():
    assert th.event_open(OPEN, [], NOW)
    assert not th.event_open(CLOSED, [], NOW)
    chapter = {"start_time": (NOW - 60) * 1000, "chapter_end_time": (NOW + 60) * 1000}
    assert th.event_open(CLOSED, [chapter], NOW)
    assert not th.event_open(None, [], NOW)


def test_a_region_silent_past_30_minutes_is_stale_with_its_error():
    st = _status(us={"borders_at": NOW - 300, "error": None},
                 jp={"borders_at": NOW - 31 * 60, "error": "OUTDATED_APP"},
                 **{"as": {"borders_at": None, "error": "boom"}})
    got = {r["region"]: r for r in th.stale_regions(st, NOW)}
    assert set(got) == {"jp", "as"}
    assert got["jp"]["minutes"] == 31 and got["jp"]["error"] == "OUTDATED_APP"
    assert got["as"]["minutes"] is None


def test_an_old_status_without_borders_at_falls_back_to_ok_at():
    assert th.stale_regions(_status(us={"ok_at": NOW - 3600}), NOW)[0]["minutes"] == 60
    assert th.stale_regions(_status(us={"ok_at": NOW - 60}), NOW) == []


def _files(monkeypatch, files: dict[str, object]) -> None:
    """`tracker_health` reads through `_read` and globs the chapter metas: both served from a dict
    (this directory's tests write no files)."""
    monkeypatch.setattr(th, "_read", lambda path: files.get(Path(path).name))
    monkeypatch.setattr(Path, "glob", lambda self, pattern: iter(()))


def test_health_warns_only_while_an_event_is_open(monkeypatch):
    files = {"tracker_status.json": _status(us={"borders_at": NOW - 7200}), "event_meta.json": OPEN}
    _files(monkeypatch, files)
    h = th.health(Path("store"), Path("event_meta.json"), NOW)
    assert h["open"] and h["stale"][0]["region"] == "us" and h["stale_after_min"] == 30
    files["event_meta.json"] = CLOSED
    assert th.health(Path("store"), Path("event_meta.json"), NOW)["stale"] == []


def test_no_status_pulled_means_no_warning(monkeypatch):
    _files(monkeypatch, {"event_meta.json": OPEN})
    h = th.health(Path("store"), Path("event_meta.json"), NOW)
    assert h["status"] is False and h["stale"] == []
