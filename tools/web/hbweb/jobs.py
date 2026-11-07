"""Searches run as jobs: `POST /api/search` (the Optimizer) and `POST /api/event/run`
(the Event page) each start one in a thread and answer its id; the page reads the
job's progress and result off `GET /events/<id>` as Server-Sent Events, and
`POST /cancel/<id>` stops it. The two job runners are here, beside the registry.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from hbweb import basis, config, songs


@dataclass
class Job:
    job_id: str
    events: "queue.Queue[dict[str, Any]]" = field(default_factory=queue.Queue)
    cancel: threading.Event = field(default_factory=threading.Event)
    done: bool = False


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

# The stages an event job announces in its `plan` event (and `POST /api/event/run` answers).
EVENT_STAGES = (
    {"id": "build", "label": "Account basis",
     "detail": "every owned card at its real level and bloom, boards rebuilt for the song"},
    {"id": "score_search", "label": "Search by score", "detail": "two-stage search; its best is S_max"},
    {"id": "event_search", "label": "Search by event objective",
     "detail": "the same search ranking by the event objective"},
    {"id": "bound", "label": "Bound", "detail": "every member set that could still win, exact-scored"},
)


def launch(runner: Callable[[Job, Mapping], None], opts: Mapping, name: str) -> Job:
    """Register a new job and start `runner(job, opts)` in a daemon thread."""
    job = Job(job_id=uuid.uuid4().hex)
    with JOBS_LOCK:
        JOBS[job.job_id] = job
    threading.Thread(target=runner, args=(job, opts),
                     name=f"{name}-{job.job_id[:8]}", daemon=True).start()
    return job


def run_search_job(job: Job, opts: Mapping, *, engine_for=None) -> None:
    """The job behind `POST /api/search` (the optimiser on demand):
    `ondemand.engine(basis, difficulty)` -- started once, kept alive
    -- then one joint search of the song. Events: `plan` -> `progress` (per
    round, with `best_so_far`) -> `result{payload}` -> `end`; or `cancelled` /
    `error` then `end`. `engine_for(basis, difficulty)` replaces the engine
    (tests)."""
    import ondemand  # noqa: PLC0415
    from job_progress import JobProgress  # noqa: PLC0415

    emit = JobProgress(job.events.put)
    t0 = time.monotonic()
    try:
        emit({"type": "plan", "stages": [{"id": "build", "label": "Load your account"},
                                         {"id": "search", "label": "Search every leader and team"},
                                         {"id": "refine", "label": "Refine the best teams"}],
              "opts": {k: v for k, v in opts.items() if k not in ("filters", "assumptions")}})
        basis_name, difficulty = opts["basis"], opts["difficulty"]
        assumptions = opts.get("assumptions")
        # the engine keeps the account loaded between searches and reloads it when a
        # refresh moved its inputs (`ondemand.account_key`): `build` is instant when warm
        emit({"type": "stage", "stage": "build", "state": "active"})
        if engine_for is None:
            levels = opts.get("levels")
            if assumptions is not None:
                eng = ondemand.engine(basis_name, difficulty, workers=basis.web_workers(),
                                      assumptions=assumptions, levels=levels,
                                      play=opts.get("play") or ("mobile", False),
                                      current_key=lambda: ondemand.account_key(difficulty))
            else:
                eng = ondemand.engine(
                    basis_name, difficulty, workers=basis.web_workers(), levels=levels,
                    play=opts.get("play") or ("mobile", False),
                    current_key=lambda: ondemand.basis_key(basis_name, difficulty))
        else:
            eng = engine_for(basis_name, difficulty)
        emit({"type": "stage", "stage": "build", "state": "done"})
        emit({"type": "stage", "stage": "search", "state": "active"})
        # this request's plan options: one engine serves every option of the re-plan
        own = {"assumptions": assumptions} if assumptions is not None and engine_for is None else {}
        payload = eng.search(opts["song"], opts["filters"], emit=emit, cancel=job.cancel,
                             top=opts.get("top") or ondemand.RANKING, **own)
        emit({"type": "result", "payload": payload,
              "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except ondemand.Cancelled:
        emit({"type": "cancelled", "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except KeyError as exc:
        emit({"type": "error", "message": str(exc.args[0] if exc.args else exc)})
    except Exception as exc:  # noqa: BLE001 — every failure is an event, never a dead stream
        import traceback  # noqa: PLC0415

        print(traceback.format_exc(), flush=True)
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        job.done = True
        emit({"type": "end"})


def _event_song_info(opts: Mapping) -> dict:
    """The song block an event search reports: title, singer and length of the song,
    or for a Highlight run the pool's inputs (every song's singers and the charts on disk)."""
    import event_mode  # noqa: PLC0415

    titles, singers = songs.song_labels()
    seconds = next((d.get("playingSeconds") for d in songs.music_rows()
                    if d.get("id") == opts["song"]), None)
    song_info = {"title": titles.get(opts["song"]), "singer": singers.get(opts["song"]),
                 "seconds": seconds}
    if event_mode.is_highlight(opts["song"]):
        song_info = {"title": "Highlight", "music_rows": songs.music_rows(), "titles": titles,
                     "singers": singers,
                     "charted": set(songs.chart_note_counts(opts["difficulty"]) or ())}
    return song_info


def run_event_job(job: Job, opts: Mapping, *, runner=None) -> None:
    """The job behind `POST /api/event/run`. Events: `plan` -> `stage` / `progress`
    (per stage) -> `result{payload}` -> `end`; or `cancelled` / `error` then `end`.
    `runner` replaces `event_mode.run_account_search` (tests)."""
    import event_mode  # noqa: PLC0415
    from job_progress import JobProgress  # noqa: PLC0415

    emit = JobProgress(job.events.put)     # + eta_s / best_so_far on progress events
    t0 = time.monotonic()
    try:
        emit({"type": "plan", "stages": [dict(s) for s in EVENT_STAGES], "opts": dict(opts)})
        _raw, event, _keys = event_mode.load_event_bundle(config.EVENT_BONUS_PATH, opts.get("event"))
        collection = ((config.read_json(config.COLLECTION_PATH) or {}).get("cards") or {})
        song_info = _event_song_info(opts)
        run = runner or event_mode.run_account_search
        payload = run(opts, emit=emit, cancel=job.cancel, event=event, collection=collection,
                      song_info=song_info, workers=opts.get("workers") or basis.web_workers())
        emit({"type": "result", "payload": payload,
              "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except event_mode.EventCancelled:
        emit({"type": "cancelled", "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except Exception as exc:  # noqa: BLE001 — every failure is an event, never a dead stream
        import traceback  # noqa: PLC0415

        print(traceback.format_exc(), flush=True)
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        job.done = True
        emit({"type": "end"})


def cancel(h, job_id: str) -> None:
    """`POST /cancel/<id>`: ask a running job to stop."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        h.reply_json(404, {"error": "unknown job"})
        return
    job.cancel.set()
    h.reply_json(200, {"job_id": job_id, "cancelling": True})


def serve_events(h, job_id: str) -> None:
    """`GET /events/<id>`: the job's events as a Server-Sent Events stream, until `end`."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        h.reply(404, b"unknown job", "text/plain; charset=utf-8")
        return
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-store")
    h.send_header("Connection", "keep-alive")
    # ThreadingHTTPServer + one thread per connection is what lets this
    # stream stay open while POST /cancel is still answerable.
    h.end_headers()
    try:
        while True:
            try:
                event = job.events.get(timeout=5.0)
            except queue.Empty:
                # A comment frame keeps proxies and the browser's own
                # idle timeout from closing a stream that is merely
                # between progress ticks.
                h.wfile.write(b": keepalive\n\n")
                h.wfile.flush()
                continue
            h.wfile.write(
                b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"
            )
            h.wfile.flush()
            if event.get("type") == "end":
                break
    except (BrokenPipeError, ConnectionResetError, OSError):
        # The page navigated away or the tab closed. Cancel the run --
        # a search nobody is watching should not keep a core busy.
        job.cancel.set()
    finally:
        with JOBS_LOCK:
            JOBS.pop(job_id, None)
