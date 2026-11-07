"""The 24/7 tracker's watchdog on the VPS: push a phone notification (ntfy) when it stops reading.

    python tracker_check.py              # what a systemd timer runs every 15 min
    python tracker_check.py --test       # send one test notification
    python tracker_check.py --failed     # what the service's OnFailure= unit runs

The tracker can stay "active" for days while every read is refused (a game update it has not
followed), so a crash alert alone would miss it. Every run reads the tracker's own API
(`/status`) and its chapter metas: while an event is open (`tracker_health.event_open`), a region
with no border read for 30 min (`tracker_health.stale_regions`) is a problem; an API that does not
answer is one at any time. A notification goes out when the state CHANGES (problem, a different
problem, recovered), never every 15 min; the last state is kept in STATE_FILE.

The ntfy topic is a long random name that only the VPS holds: `NTFY_TOPIC` in the unit's
EnvironmentFile, never in the repository. Messages carry no account, host or credential data:
regions, minutes and the error's class (e.g. OUTDATED_APP).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "web"))      # tracker_health.py lives with the Event page
import tracker_health  # noqa: E402

STATUS_URL = os.environ.get("HB_TRACKER_STATUS_URL", "http://127.0.0.1:8790/status")
STORE = Path(os.environ.get("HB_TRACKER_STORE", str(HERE.parents[1] / "tools/recon/proxy/dumps/cutoffs")))
STATE_FILE = Path(os.environ.get("HB_TRACKER_CHECK_STATE", "/var/lib/hb-tracker-check/state.json"))
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh")
# OnFailure= fires on EVERY crash even under Restart=always (measured on the VPS's systemd 255),
# so a crash loop would notify every RestartSec: one "failed" notification per this window
FAILED_EVERY_S = 30 * 60
_KNOWN = ("OUTDATED_APP", "OUTDATED_MASTER", "IN_MAINTENANCE", "DATE_CHANGED", "EVENT_OUT_OF_TERM")


def error_class(error: str | None) -> str | None:
    """A short, non-sensitive label for an error text: a known game code or the exception name."""
    if not error:
        return None
    for code in _KNOWN:
        if code in error:
            return code
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", error)
    return m.group(1) if m else "error"


def problem(status: dict | None, metas: list[dict], now: float) -> str | None:
    """The message for the current problem, or None when everything is fine."""
    if status is None:
        return "hb-tracker: its API is not answering (service down?)"
    if not tracker_health.event_open(None, metas, now):
        return None
    stale = tracker_health.stale_regions(status, now)
    if not stale:
        return None
    parts = []
    for r in stale:
        age = "never read" if r["minutes"] is None else f"{r['minutes']} min without a read"
        cls = error_class(r.get("error"))
        parts.append(f"{r['region']}: {age}" + (f" ({cls})" if cls else ""))
    return "hb-tracker: event open, borders not updating -- " + "; ".join(parts)


def _key(message: str | None) -> str | None:
    """The state a message stands for, without its minute counts (they grow every run)."""
    return None if message is None else re.sub(r"\d+ min", "N min", message)


def decide(previous: str | None, message: str | None) -> str | None:
    """What to send now: a new or changed problem, a recovery, or nothing."""
    if _key(message) == previous:
        return None
    if message is None:
        return "hb-tracker: reading again, all regions OK"
    return message


def failed_due(state: dict, now: float) -> bool:
    """Whether a "service failed" notification goes out now (none in the last FAILED_EVERY_S)."""
    return now - float(state.get("failed_at") or 0) >= FAILED_EVERY_S


def _load_state() -> dict:
    try:
        doc = json.loads(STATE_FILE.read_text("utf8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), "utf8")


def notify(text: str, topic: str | None = None) -> None:
    topic = topic or os.environ.get("NTFY_TOPIC", "")
    if not topic:
        raise SystemExit("NTFY_TOPIC is not set")
    req = urllib.request.Request(f"{NTFY_URL}/{topic}", data=text.encode(),
                                 headers={"Title": "hololive-builder tracker", "Tags": "warning"})
    urllib.request.urlopen(req, timeout=20).read()  # noqa: S310 -- the ntfy server


def _status() -> dict | None:
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=10) as r:  # noqa: S310 -- localhost
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def _metas(store: Path) -> list[dict]:
    out = []
    for p in store.glob("*/*.meta.json"):
        try:
            out.append(json.loads(p.read_text("utf8")))
        except (OSError, ValueError):
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--test", action="store_true", help="send one test notification")
    ap.add_argument("--failed", action="store_true", help="the service failed (OnFailure=)")
    args = ap.parse_args(argv)
    if args.test:
        notify("hb-tracker: test notification, alerts are set up")
        return 0
    state, now = _load_state(), time.time()
    if args.failed:
        if failed_due(state, now):
            notify("hb-tracker: the service crashed (systemd restarts it; repeats are muted 30 min)")
            _save_state({**state, "failed_at": int(now)})
        return 0
    message = problem(_status(), _metas(STORE), now)
    send = decide(state.get("key"), message)
    if send:
        notify(send)
        print(send)
    _save_state({**state, "key": _key(message), "at": int(now)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
