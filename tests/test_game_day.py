"""hbuilder.game_day: the game day starts at the master's Setting.resetTimingHour, Japan time."""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from hbuilder import game_day


def _utc(*a):
    return datetime.datetime(*a, tzinfo=datetime.timezone.utc)


def _ms(d):
    return int(d.timestamp() * 1000)


def test_day_starts_at_the_reset_hour_in_japan():
    # 05:00 JST = 20:00 UTC the day before
    assert game_day.day_start_ms(_utc(2026, 11, 8, 23, 2), 5) == _ms(_utc(2026, 11, 8, 20))
    assert game_day.day_start_ms(_utc(2026, 11, 8, 19, 59), 5) == _ms(_utc(2026, 11, 7, 20))
    assert game_day.day_start_ms(_utc(2026, 11, 8, 20, 0), 5) == _ms(_utc(2026, 11, 8, 20))
    assert game_day.next_day_start_ms(_utc(2026, 11, 8, 23, 2), 5) == _ms(_utc(2026, 11, 9, 20))
    assert round(game_day.minutes_to_rollover(_utc(2026, 11, 9, 19, 55), 5)) == 5


HAND_WRITTEN = re.compile(r"\b0?\d:00 JST|timedelta\(hours=9\)|9 \* HOUR|Date\.UTC\([^)]*,\s*[45],\s*0,\s*0\)")


def test_no_code_writes_the_reset_hour_by_hand():
    """The day's start is the master's Setting.resetTimingHour (this module): no other code or
    message may carry an hour of its own (the refresh said 04:00, the Park page 05:00)."""
    repo = Path(__file__).resolve().parents[1]
    skip = ("proxyenv", "__pycache__", "node_modules", "/dumps/", "/build/")
    hits = []
    for top in ("src", "tools"):
        for p in (repo / top).rglob("*"):
            rel = p.relative_to(repo).as_posix()
            if p.suffix not in (".py", ".js") or any(s in "/" + rel for s in skip) \
                    or p.name.startswith("test_") or rel == "src/hbuilder/game_day.py":
                continue
            for n, line in enumerate(p.read_text("utf-8", errors="replace").splitlines(), 1):
                if HAND_WRITTEN.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()}")
    assert hits == []


def test_reset_hour_comes_from_the_master_setting(tmp_path):
    p = tmp_path / "main_Setting.json"
    p.write_text(json.dumps([{"id": "1", "data": {"resetTimingHour": 4}}]), "utf8")
    assert game_day.reset_hour(p) == 4
    p.write_text(json.dumps([{"id": "1", "data": {"resetTimingHour": 31}}]), "utf8")
    assert game_day.reset_hour(p) == game_day.DEFAULT_RESET_HOUR
    assert game_day.reset_hour(tmp_path / "absent.json") == game_day.DEFAULT_RESET_HOUR
