"""The game day: it starts at `Setting.resetTimingHour` (master), Japan time.

Daily things roll over at that hour: the Park's symbols and sites, the account's daily login, the
"once per game day" limits this tool keeps. Japan has no daylight saving, so the start in UTC is
the same every day.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

from hbuilder import paths

JST = datetime.timezone(datetime.timedelta(hours=9))
DEFAULT_RESET_HOUR = 5
DAY_MS = 86_400_000


def reset_hour(setting_path: Path | None = None) -> int:
    """`Setting.resetTimingHour` from the master export (`main_Setting.json`); 5 when the file
    or the field is missing or out of range."""
    path = setting_path or (paths.HOLODORI / "main_Setting.json")
    try:
        rows = json.loads(Path(path).read_bytes().decode("utf-8"))
        hour = int(rows[0]["data"]["resetTimingHour"])
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return DEFAULT_RESET_HOUR
    return hour if 0 <= hour < 24 else DEFAULT_RESET_HOUR


def day_start_ms(now: datetime.datetime, hour: int | None = None) -> int:
    """Unix ms of the start of the game day `now` falls in."""
    hour = reset_hour() if hour is None else hour
    jst = now.astimezone(JST)
    start = jst.replace(hour=hour, minute=0, second=0, microsecond=0)
    if jst < start:
        start -= datetime.timedelta(days=1)
    return int(start.timestamp() * 1000)


def next_day_start_ms(now: datetime.datetime, hour: int | None = None) -> int:
    return day_start_ms(now, hour) + DAY_MS


def minutes_to_rollover(now: datetime.datetime, hour: int | None = None) -> float:
    return (next_day_start_ms(now, hour) - now.timestamp() * 1000) / 60_000
