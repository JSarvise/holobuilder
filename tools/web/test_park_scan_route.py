"""The Park page's "Scan all areas": `scan` in GET /api/park and POST /api/park/scan.

The route never runs the real scan here (it logs into the account): `scripts.run_recon_script`
is replaced, and what is checked is the gate around it (off unless HB_PARK_SCAN=1, one login at
a time) and that it starts park_scan.py."""
from __future__ import annotations

import datetime
import json
import time
import urllib.error
import urllib.request

import pytest

from hbuilder import game_day
from hbweb import admin, config, park, scripts


def _post(base: str) -> tuple[int, dict]:
    req = urllib.request.Request(f"{base}/api/park/scan", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture
def last_file(monkeypatch, tmp_path):
    path = tmp_path / "park_scan_last.json"
    monkeypatch.setattr(config, "PARK_SCAN_LAST_PATH", path)
    monkeypatch.setattr(game_day, "reset_hour", lambda *a: 5)
    return path


def test_off_by_default(web_base, monkeypatch, last_file):
    monkeypatch.delenv(config.PARK_SCAN_ENV, raising=False)
    ran = []
    monkeypatch.setattr(scripts, "run_recon_script", lambda *a, **k: ran.append(a))
    code, body = _post(web_base)
    assert code == 403 and "Settings" in body["error"] and ran == []
    assert park.scan_status()["enabled"] is False


def test_on_runs_park_scan_and_reports_the_record(web_base, monkeypatch, last_file):
    monkeypatch.setenv(config.PARK_SCAN_ENV, "1")
    seen = []

    def fake(h, script, tag, args=None):
        seen.append(script)
        last_file.write_text(json.dumps({"counted": True, "areas": [{"area": "area-a03"}]}), "utf8")
        return 0, "done\n"
    monkeypatch.setattr(scripts, "run_recon_script", fake)
    code, body = _post(web_base)
    assert code == 200 and body["returncode"] == 0
    assert seen == [config.PARK_SCAN_SCRIPT] and config.PARK_SCAN_SCRIPT.name == "park_scan.py"
    assert body["last"]["areas"] == [{"area": "area-a03"}]
    # released in the handler's `finally`, just after the reply is sent
    deadline = time.monotonic() + 5
    while admin.REFRESH_LOCK.locked() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not admin.REFRESH_LOCK.locked()


def test_one_login_at_a_time(web_base, monkeypatch, last_file):
    monkeypatch.setenv(config.PARK_SCAN_ENV, "1")
    monkeypatch.setattr(scripts, "run_recon_script", lambda *a, **k: (0, ""))
    assert admin.REFRESH_LOCK.acquire(blocking=False)
    try:
        code, body = _post(web_base)
    finally:
        admin.REFRESH_LOCK.release()
    assert code == 409 and "already running" in body["error"]


def test_status_says_when_todays_scan_is_used(monkeypatch, last_file):
    monkeypatch.setenv(config.PARK_SCAN_ENV, "1")
    now = datetime.datetime(2026, 11, 8, 23, 2, tzinfo=datetime.timezone.utc)
    start = game_day.day_start_ms(now, 5)
    assert park.scan_status(now)["done_today"] is False and park.scan_status(now)["last"] is None
    last_file.write_text(json.dumps({"counted": True, "game_day_start": start,
                                     "finished_at": "x", "calls": [1, 2]}), "utf8")
    st = park.scan_status(now)
    assert st["enabled"] and st["done_today"] and st["next_at"] == start + game_day.DAY_MS
    assert "calls" not in st["last"]                      # the page gets the summary only
    last_file.write_text(json.dumps({"counted": False, "game_day_start": start}), "utf8")
    assert park.scan_status(now)["done_today"] is False   # a scan that never moved: not used


def test_the_park_payload_carries_the_scan_status(web_base, monkeypatch, last_file):
    monkeypatch.setattr(park, "park_export", lambda: None)
    with urllib.request.urlopen(f"{web_base}/api/park", timeout=30) as r:
        payload = json.loads(r.read())
    assert set(payload["scan"]) == {"enabled", "done_today", "next_at", "last"}


def test_the_day_start_the_page_uses_follows_the_masters_reset_hour(web_base, monkeypatch, last_file):
    """The Park page marks a symbol roll from before `game_day_start` as stale; the hour is the
    master's resetTimingHour, never one written into the page."""
    monkeypatch.setattr(park, "park_export", lambda: None)
    for hour in (5, 3):
        monkeypatch.setattr(game_day, "reset_hour", lambda *a, h=hour: h)
        before = datetime.datetime.now(datetime.timezone.utc)
        with urllib.request.urlopen(f"{web_base}/api/park", timeout=30) as r:
            got = json.loads(r.read())["game_day_start"]
        after = datetime.datetime.now(datetime.timezone.utc)
        assert got in {game_day.day_start_ms(before, hour), game_day.day_start_ms(after, hour)}
        assert datetime.datetime.fromtimestamp(got / 1000, game_day.JST).hour == hour
