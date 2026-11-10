"""tracker_check.py: the VPS watchdog sends a notification when the tracker stops reading."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import tracker_check as tc  # noqa: E402

NOW = 1_793_900_000.0
OPEN = [{"start_time": (NOW - 3600) * 1000, "chapter_end_time": (NOW + 3600) * 1000}]
ENDED = [{"start_time": (NOW - 9000) * 1000, "chapter_end_time": (NOW - 3600) * 1000}]
REFUSED = "GameError: /rpc.api.Master/Get rejected: grpc-status 3 OUTDATED_APP (code 2004)"


def _status(**regions):
    return {"regions": [{"region": k, **v} for k, v in regions.items()]}


def test_a_silent_region_during_an_open_event_is_a_problem_without_sensitive_text():
    msg = tc.problem(
        _status(us={"borders_at": NOW - 60}, jp={"borders_at": NOW - 45 * 60, "error": REFUSED}), OPEN, NOW
    )
    assert msg == "hb-tracker: event open, borders not updating -- jp: 45 min without a read (OUTDATED_APP)"
    assert "rpc.api" not in msg and "game-hololive" not in msg


def test_no_event_open_is_quiet_but_a_dead_api_is_not():
    assert tc.problem(_status(us={"borders_at": NOW - 99999}), ENDED, NOW) is None
    assert "not answering" in tc.problem(None, ENDED, NOW)


def test_a_notification_only_when_the_state_changes():
    first = tc.problem(_status(jp={"borders_at": NOW - 45 * 60}), OPEN, NOW)
    later = tc.problem(_status(jp={"borders_at": NOW - 60 * 60}), OPEN, NOW)
    assert tc.decide(None, first) == first  # new problem
    assert tc.decide(tc._key(first), later) is None  # same problem, 15 min later
    assert tc.decide(tc._key(first), None) == "hb-tracker: reading again, all regions OK"
    assert tc.decide(None, None) is None  # fine and was fine


def test_error_class_keeps_only_a_label():
    assert tc.error_class(REFUSED) == "OUTDATED_APP"
    assert tc.error_class("ConnectionResetError: [Errno 104] by 10.0.0.1") == "ConnectionResetError"
    assert tc.error_class(None) is None


def test_a_crash_loop_notifies_once_per_half_hour():
    assert tc.failed_due({}, NOW)
    assert not tc.failed_due({"failed_at": NOW - 60}, NOW)
    assert tc.failed_due({"failed_at": NOW - 31 * 60}, NOW)
