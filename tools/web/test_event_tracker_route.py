"""`GET /api/event/tracker`: the Event page's stale-tracker warning (tracker_health.py)."""
from __future__ import annotations

import json
import urllib.request

import tracker_health

from hbweb import event


def _get(base: str) -> dict:
    with urllib.request.urlopen(f"{base}/api/event/tracker", timeout=30) as r:
        return json.loads(r.read())


def test_without_a_tracker_there_is_nothing_to_warn_about(web_base, monkeypatch):
    monkeypatch.setattr(event, "tracker_configured", lambda: False)
    assert _get(web_base) == {"configured": False, "open": False, "stale": []}


def test_a_stale_region_reaches_the_page(web_base, monkeypatch):
    monkeypatch.setattr(event, "tracker_configured", lambda: True)
    stale = [{"region": "jp", "minutes": 95, "last_read_at": 1, "error": "OUTDATED_APP"}]
    monkeypatch.setattr(tracker_health, "health", lambda store, meta, now: {
        "open": True, "status": True, "stale": stale, "stale_after_min": 30})
    got = _get(web_base)
    assert got["configured"] is True and got["stale"] == stale
