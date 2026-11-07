"""The Event page: the open event (`GET /api/event`), the songs ranked for it
(`GET /api/event/songs`), its search (`POST /api/event/run`, a job) and one team's
points (`POST /api/event/points`); the ranking borders (`GET /api/event/cutoffs`), the
top 100 (`GET /api/event/top`) and past events (`GET /api/event/history`).

`tools/web/event_mode.py` holds the event logic and `cutoffs_view.py` / `top_view.py`
the border views; these are the reads, the background poller and the jobs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

from hbuilder.inputs import _LoadFailure, load_inputs

import cutoffs_poller

from hbweb import basis, config, jobs, songs


def tracker_configured() -> bool:
    """Whether the 24/7 VPS tracker is reachable (over SSH or over HTTPS)."""
    return config.CUTOFFS_REMOTE_CONFIG.is_file() or config.SERVER_API_PATH.is_file()


# The border store is appended to by one reader at a time: the poller's reads and the
# top-100 pulls share this lock.
CUTOFFS_SYNC_LOCK = threading.Lock()
# The local poller (started by `router.main`, every 5 min; guests only).
# `tools/web/cutoffs_poller.py` has the policy; HBUILDER_CUTOFFS_POLL_MIN=<min> sets the
# interval and =0 turns it off. With the VPS tracker configured it PULLS the tracker's
# reads instead of logging the same guest accounts in from here: two readers on one
# guest would keep ending each other's session.
CUTOFFS_POLL_SCRIPT = (config.CUTOFFS_PULL_SCRIPT if tracker_configured()
                       else config.CUTOFFS_SYNC_SCRIPT)
CUTOFFS_POLLER = cutoffs_poller.CutoffsPoller(
    cutoffs_poller.ScriptRunner(CUTOFFS_POLL_SCRIPT, config.REPO_ROOT), CUTOFFS_SYNC_LOCK)

# The top 100 is read ON DEMAND, only while someone is looking at it: each
# GET /api/event/top renews a viewer's lease on the VPS tracker for that region (at most once
# every TOP_WATCH_EVERY_S per region) and pulls what it read, in the background; the page asks
# again about every minute while it is open, so it sees the board the lease made.
TOP_WATCH_EVERY_S = 50
_TOP_WATCH_AT: dict[str, float] = {}
_TOP_WATCH_MU = threading.Lock()


def kick_top_watch(region: str, kind: str | None = None) -> bool:
    """Start one background `tracker_pull.py --watch <region>`; False when not configured or
    one ran for this region less than TOP_WATCH_EVERY_S ago."""
    if not tracker_configured():
        return False
    now = time.time()
    with _TOP_WATCH_MU:
        key = f"{region}|{kind or ''}"   # a song's board holds its own lease
        if now - _TOP_WATCH_AT.get(key, 0.0) < TOP_WATCH_EVERY_S:
            return False
        _TOP_WATCH_AT[key] = now

    def run() -> None:
        # the pull appends to the store: never alongside the poller's read
        if not CUTOFFS_SYNC_LOCK.acquire(timeout=90):
            return
        try:
            subprocess.run([sys.executable, "-u", str(config.CUTOFFS_PULL_SCRIPT), "--watch", region,
                            *(["--kind", kind] if kind else [])],
                           cwd=str(config.REPO_ROOT), capture_output=True, timeout=120,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"  [top-watch] {region}: {type(exc).__name__}: {exc}", flush=True)
        finally:
            CUTOFFS_SYNC_LOCK.release()

    threading.Thread(target=run, name=f"top-watch-{region}", daemon=True).start()
    return True


def event_unavailable(exc: Exception) -> dict:
    return {"available": False, "error": (
        f"no current event: {exc}. The refresh writes tools/recon/proxy/dumps/"
        "marathon_bonus.json (Event/ListEventInfo); run tools/refresh/refresh_all.py.")}


def chr_names(cards) -> dict[str, str]:
    """chr-id -> holomem name of the catalogue's cards."""
    names: dict[str, str] = {}
    for card in cards:
        parts = card.id.split("-")
        if len(parts) >= 2:
            names.setdefault(f"chr-{parts[1]}", card.member)
    return names


def serve_event(h) -> None:
    """`GET /api/event[?event=<key>]` — the newest open event's bonus rows and
    the account's bonus cards (`event_mode.event_payload`). Pure read; 503
    when the refresh has not written marathon_bonus.json or nothing is open."""
    import event_mode  # noqa: PLC0415

    query = parse_qs(urlparse(h.path).query)
    key = (query.get("event") or [None])[0] or None
    try:
        raw, event, keys = event_mode.load_event_bundle(config.EVENT_BONUS_PATH, key)
    except (OSError, ValueError, KeyError, event_mode.mar.SchemaError) as exc:
        # FileNotFoundError, a JSON error, SchemaError (a ValueError) — all 503
        h.reply_json(503, event_unavailable(exc))
        return
    try:
        cards, owned, _cfg, _prov = load_inputs(config.CARDS_PATH, config.OWNED_PATH)
    except _LoadFailure as exc:
        h.reply_json(500, {"error": f"could not load the catalogue: {exc}"})
        return
    collection = ((config.read_json(config.COLLECTION_PATH) or {}).get("cards") or {})
    titles, singers = songs.song_labels()
    try:
        meta = config.read_json(config.EVENT_META_PATH)
    except (OSError, ValueError):  # a torn event_meta.json costs the title, not the page
        meta = None
    h.reply_json(200, event_mode.event_payload(
        raw, event, open_keys=keys, cards_by_id={c.id: c for c in cards},
        owned_ids=[c.id for c in owned], collection=collection, titles=titles,
        chr_names=chr_names(cards), meta=meta,
        art_dir=config.EVENT_ART_DIR, singers=singers,
        music_meta={str(d.get("id")): d for d in songs.music_rows() if d.get("id")}))


def serve_event_songs(h) -> None:
    """`GET /api/event/songs[?boosts=3&difficulty=EXPERT]` — every song ranked
    by the song-length multiplier L (an ESTIMATE, team-independent)."""
    import event_mode  # noqa: PLC0415

    query = parse_qs(urlparse(h.path).query)
    try:
        boosts = int((query.get("boosts") or [event_mode.DEFAULT_BOOSTS])[0])
    except ValueError:
        boosts = -1
    if not 1 <= boosts <= 10:
        h.reply_json(400, {"error": "'boosts' must be an integer 1..10"})
        return
    try:
        difficulty = basis.parse_difficulty({"difficulty": (query.get("difficulty") or [None])[0]})
    except basis.HttpError as exc:
        h.reply_json(exc.code, exc.obj)
        return
    try:
        # `event`: one chapter (a Spotlight event's chapters each have their own bonuses)
        _raw, event, _keys = event_mode.load_event_bundle(
            config.EVENT_BONUS_PATH, (query.get("event") or [None])[0] or None)
    except (OSError, ValueError, KeyError, event_mode.mar.SchemaError):  # the ranking stands without an event
        event = None
    titles, singers = songs.song_labels()
    h.reply_json(200, event_mode.songs_payload(
        songs.music_rows(), titles=titles, singers=singers, boosts=boosts, event=event,
        charted=songs.chart_note_counts(difficulty), difficulty=difficulty))


def event_points(h) -> None:
    """`POST /api/event/points` -- one team's Event Pt per Live, solo or with the room's
    MULTI SCORE typed in (`event_mode.event_points_for`). Instant: no search."""
    import event_mode  # noqa: PLC0415

    try:
        body = h.read_json_body()
        h.reply_json(200, event_mode.event_points_for(body))
    except json.JSONDecodeError:
        h.reply_json(400, {"error": "the body is not valid JSON"})
    except event_mode.EventRequestError as exc:
        h.reply_json(400, {"error": str(exc)})


def start_event_run(h) -> None:
    """`POST /api/event/run {objective, song, difficulty?, boosts?, holopass?,
    workers?, top?, event?}` — 202 `{job_id, plan, opts}`; events on
    `/events/<id>`; cancel with `POST /cancel/<id>`."""
    import event_mode  # noqa: PLC0415

    try:
        body = h.read_json_body()
    except json.JSONDecodeError:
        h.reply_json(400, {"error": "the body is not valid JSON"})
        return
    try:
        opts = event_mode.parse_event_body(body)
        # Highlight is a pool, not one song: the difficulty alone is checked here
        opts["difficulty"] = basis.parse_difficulty(
            body, "" if event_mode.is_highlight(opts["song"]) else opts["song"])
    except event_mode.EventRequestError as exc:
        h.reply_json(400, {"error": str(exc)})
        return
    except basis.HttpError as exc:
        h.reply_json(exc.code, exc.obj)
        return
    if not config.EVENT_BONUS_PATH.is_file():
        h.reply_json(503, event_unavailable(FileNotFoundError(str(config.EVENT_BONUS_PATH))))
        return
    job = jobs.launch(jobs.run_event_job, opts, "event")
    h.reply_json(202, {"job_id": job.job_id, "plan": [dict(s) for s in jobs.EVENT_STAGES],
                       "opts": opts})


def _region_of(h, query) -> str | None:
    """The `region` query parameter (`us` by default), or None having answered 400."""
    import cutoffs_view  # noqa: PLC0415

    region = ((query.get("region") or ["us"])[0] or "us").lower()
    if region not in cutoffs_view.REGIONS:
        h.reply_json(400, {"error": f"region must be one of {', '.join(cutoffs_view.REGIONS)}"})
        return None
    return region


def _grades():
    import cutoffs_view  # noqa: PLC0415

    return cutoffs_view.load_grades(config.read_json(config.CUTOFFS_GRADE_PATH),
                                    config.read_json(config.CUTOFFS_GRADE_LANG_PATH))


def serve_event_cutoffs(h) -> None:
    """`GET /api/event/cutoffs?region=us[&chapter=<id>][&kind=points]` — the stored
    border series per reward tier plus a prediction of each tier's final. Pure read of
    dumps/cutoffs/ (`cutoffs_view.build_view`). 400 on a bad region, 503
    `{available:false,error}` when nothing has been synced for that region yet."""
    import cutoffs_view  # noqa: PLC0415

    query = parse_qs(urlparse(h.path).query)
    region = _region_of(h, query)
    if region is None:
        return
    grades = _grades()
    try:
        view = cutoffs_view.build_view(
            config.CUTOFFS_STORE_DIR, region, now=int(time.time() * 1000),
            chapter_id=(query.get("chapter") or [None])[0] or None,
            kind=(query.get("kind") or [None])[0] or None, grades=grades)
    except LookupError as exc:
        h.reply_json(503, {"available": False, "region": region, "error": str(exc)})
        return
    except (OSError, ValueError) as exc:
        h.reply_json(500, {"error": f"{type(exc).__name__}: {exc}"})
        return
    h.reply_json(200, view)


def serve_event_tracker(h) -> None:
    """`GET /api/event/tracker` -- is the 24/7 tracker still reading? `{configured, open, stale:
    [{region, minutes, last_read_at, error}], stale_after_min}` (`tracker_health.health`): a
    region with no border read for 30 min while an event is open. Pure read."""
    import tracker_health  # noqa: PLC0415

    if not tracker_configured():
        h.reply_json(200, {"configured": False, "open": False, "stale": []})
        return
    h.reply_json(200, {"configured": True, **tracker_health.health(
        config.CUTOFFS_STORE_DIR, config.EVENT_META_PATH, time.time())})


def serve_event_top(h) -> None:
    """`GET /api/event/top?region=us[&chapter=<id>][&kind=<k>][&uid=<public id>]` -- the latest top-100
    board of a chapter with each player's last-hour movement, and with `uid` that player's
    series (`top_view.build_top` over dumps/cutoffs/<region>/<chapter>.top.jsonl, which the
    24/7 tracker writes). Pure read. 400 on a bad region, 503 `{available:false}` when the
    tracker has not stored a board for it."""
    import top_view  # noqa: PLC0415

    query = parse_qs(urlparse(h.path).query)
    region = _region_of(h, query)
    if region is None:
        return
    uid = (query.get("uid") or [None])[0] or None
    live = tracker_configured()
    kind = (query.get("kind") or [None])[0] or None
    if kind is not None and not (kind in ("points", "total") or (
            kind.startswith("music:") and kind[6:].replace("_", "").isalnum())):
        h.reply_json(400, {"error": "kind must be points, total or music:<id>"})
        return
    if live and not uid:
        kick_top_watch(region, kind)
    try:
        view = top_view.build_top(
            config.CUTOFFS_STORE_DIR, region, now=int(time.time() * 1000),
            chapter_id=(query.get("chapter") or [None])[0] or None, uid=uid,
            kind=kind)
    except LookupError as exc:
        h.reply_json(503, {"available": False, "region": region, "error": str(exc),
                           "watching": live})
        return
    except (OSError, ValueError) as exc:
        h.reply_json(500, {"error": f"{type(exc).__name__}: {exc}"})
        return
    view["watching"] = live
    h.reply_json(200, view)


def serve_event_history(h) -> None:
    """`GET /api/event/history` -- every marathon event on record, newest first, with the
    account's result per chapter and the final borders (`cutoffs_view.history_view` over
    `event_history.build()`, the refresh step's own build, made in memory). Pure read;
    `{events: []}` before the first build."""
    import cutoffs_view  # noqa: PLC0415

    grades = _grades()
    # built per request from what is on disk (the final borders the
    # tracker pull brings in every 5 min must show without a refresh); the file the
    # refresh wrote is the fallback when the build cannot run
    doc = None
    try:
        import event_history  # noqa: PLC0415
        doc = event_history.build()[0]
    except Exception:  # noqa: BLE001 -- the stored history still answers
        doc = None
    if doc is None:
        doc = config.read_json(config.EVENT_HISTORY_PATH) or {}
    h.reply_json(200, {"events": cutoffs_view.history_view(doc, grades, songs.song_titles())})
