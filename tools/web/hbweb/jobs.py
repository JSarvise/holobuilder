"""Searches run as jobs: `POST /api/search` (the Optimizer) and `POST /api/event/run`
(the Event page) each start one in a thread and answer its id; the page reads the
job's progress and result off `GET /events/<id>` as Server-Sent Events, and
`POST /cancel/<id>` stops it. The two job runners are here, beside the registry.
"""

from __future__ import annotations

import dataclasses
import json
import queue
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hbuilder.errors import MissingInput
from hbweb import basis, config, songs

if TYPE_CHECKING:
    from hbweb.router import Handler


@dataclass
class Job:
    job_id: str
    events: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    cancel: threading.Event = field(default_factory=threading.Event)
    done: bool = False
    streamed: bool = False  # a page opened `GET /events/<id>`


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
# A job whose stream no page opens within this time (the page closed right after starting it)
# is cancelled, and leaves the registry once it has ended: nothing would ever read it.
STREAM_GRACE_S = 120.0

# The stages an event job announces in its `plan` event (and `POST /api/event/run` answers).
EVENT_STAGES = (
    {
        "id": "build",
        "label": "Account basis",
        "detail": "every owned card at its real level and bloom, boards rebuilt for the song",
    },
    {"id": "score_search", "label": "Search by score", "detail": "two-stage search; its best is S_max"},
    {
        "id": "event_search",
        "label": "Search by event objective",
        "detail": "the same search ranking by the event objective",
    },
    {"id": "bound", "label": "Bound", "detail": "every member set that could still win, exact-scored"},
)


def launch(runner: Callable[[Job, Mapping], None], opts: Mapping, name: str) -> Job:
    """Register a new job and start `runner(job, opts)` in a daemon thread."""
    job = Job(job_id=uuid.uuid4().hex)
    with JOBS_LOCK:
        JOBS[job.job_id] = job
    threading.Thread(target=runner, args=(job, opts), name=f"{name}-{job.job_id[:8]}", daemon=True).start()
    threading.Thread(target=_unclaimed, args=(job,), name=f"unclaimed-{job.job_id[:8]}", daemon=True).start()
    return job


def _unclaimed(job: Job, poll_s: float = 1.0) -> None:
    """Cancel `job` when no page opened its stream within STREAM_GRACE_S, then drop it from
    the registry once it has ended."""
    deadline = time.monotonic() + STREAM_GRACE_S
    while time.monotonic() < deadline:
        if job.streamed:
            return
        time.sleep(poll_s)
    if job.streamed:
        return
    job.cancel.set()
    while not job.done:
        time.sleep(poll_s)
    with JOBS_LOCK:
        if not job.streamed:
            JOBS.pop(job.job_id, None)


# A song's other chart (`ChartLibrary.contender`: HARD against EXPERT) is offered only when its
# best team scores at least CHART_MIN_GAIN more: our HARD/EXPERT ratios agree with a user's
# simulated sheet to a correlation of 0.90, so a smaller gain is within the model's error and not
# worth an unfamiliar chart. A full search on the other chart found its own #1 up to
# CHART_SEARCH_GAP above the picked chart's #1 scored there (0 on 21 of 30 songs, 1.85 % at most):
# a probe below CHART_PROBE_GATE cannot reach the minimum gain, so its full search is skipped.
CHART_MIN_GAIN = 0.005
CHART_SEARCH_GAP = 0.0185
CHART_PROBE_GATE = (1.0 + CHART_MIN_GAIN) / (1.0 + CHART_SEARCH_GAP)


def _check_chart(
    payload: dict, difficulty: str, contender: Mapping, filters, search_at, top: int, probe_at=None
) -> dict:
    """The `chart` event that follows a song's result: is the song's other chart worth playing?
    The served #1 team is scored on it first: directly (`probe_at(chart, leader, members)`, the
    number a search serves it at, `ondemand.Engine.served_value`), else by a search pinned to it
    (the account's boards, planned on that chart). Only a probe at CHART_PROBE_GATE or above earns
    the other chart a full search; the other chart's #1 is the better of that search's #1 and the
    probed team (a search can settle below a team already scored), and it wins when it scores
    CHART_MIN_GAIN more. `check` says what was tried; `payload` is the other chart's search when
    it wins (its rows are what the page then shows), else None."""
    other = contender["other"]
    levels = contender.get("levels") or {}
    first = payload["ranking"][0]
    check: dict[str, Any] = {
        "difficulty": difficulty,
        "level": levels.get(difficulty),
        "other": other,
        "other_level": levels.get(other),
        "searched": False,
        "wins": False,
    }
    pinned = dataclasses.replace(
        filters, leader_member="", leader_card=first["leader"], require=tuple(first["members"])
    )
    probe_rows: list = []
    rescored = probe_at(other, first["leader"], first["members"]) if probe_at is not None else None
    if rescored is None:
        probe_rows = search_at(other, pinned, top=1).get("ranking") or []
        rescored = (probe_rows[0] if probe_rows else {}).get("best")
    check["probe"] = rescored
    if rescored is None or rescored < CHART_PROBE_GATE * first["best"]:
        return {"type": "chart", "check": check, "payload": None}
    alt = search_at(other, filters, top=top)
    best_other = ((alt.get("ranking") or [{}])[0]).get("best")
    if best_other is None or rescored > best_other:
        # the probed team beats the search's #1: served first with its own row
        if not probe_rows:
            probe_rows = search_at(other, pinned, top=1).get("ranking") or []
        if probe_rows:
            mine = probe_rows[0]
            same = (mine["leader"], sorted(mine["members"]))
            rest = [r for r in alt.get("ranking") or [] if (r["leader"], sorted(r["members"])) != same]
            alt["ranking"] = [mine, *rest][: max(top, len(rest))]
            best_other = mine.get("best")
    check.update(searched=True, other_best=best_other)
    if best_other is None:
        return {"type": "chart", "check": check, "payload": None}
    check["gain"] = best_other / first["best"] - 1.0
    check["wins"] = check["gain"] >= CHART_MIN_GAIN
    if not check["wins"]:
        return {"type": "chart", "check": check, "payload": None}
    alt["chart_check"] = check
    return {"type": "chart", "check": check, "payload": alt}


def run_search_job(job: Job, opts: Mapping, *, engine_for: Callable[[str, str], Any] | None = None) -> None:
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
        emit(
            {
                "type": "plan",
                "stages": [
                    {"id": "build", "label": "Load your account"},
                    {"id": "search", "label": "Search every leader and team"},
                    {"id": "refine", "label": "Refine the best teams"},
                ],
                "opts": {k: v for k, v in opts.items() if k not in ("filters", "assumptions")},
            }
        )
        basis_name, difficulty = opts["basis"], opts["difficulty"]
        assumptions = opts.get("assumptions")
        if engine_for is None:
            require_search_inputs(basis_name, bool(getattr(assumptions, "materials", False)))

        def engine_at(diff: str):
            # the engine keeps the account loaded between searches and reloads it when a
            # refresh moved its inputs (`ondemand.account_key`): instant when warm
            if engine_for is not None:
                return engine_for(basis_name, diff)
            common: dict[str, Any] = {
                "workers": basis.web_workers(),
                "levels": opts.get("levels"),
                "max_level": opts.get("max_level"),
                "play": opts.get("play") or ("mobile", False),
            }
            if assumptions is not None:
                return ondemand.engine(
                    basis_name,
                    diff,
                    assumptions=assumptions,
                    current_key=lambda: ondemand.account_key(diff),
                    **common,
                )
            return ondemand.engine(
                basis_name, diff, current_key=lambda: ondemand.basis_key(basis_name, diff), **common
            )

        # this request's plan options: one engine serves every option of the re-plan
        own = {"assumptions": assumptions} if assumptions is not None and engine_for is None else {}

        def search_at(diff: str, filters, *, top: int, progress=None, eng=None) -> dict:
            eng = eng if eng is not None else engine_at(diff)
            try:
                return eng.search(
                    opts["song"],
                    filters,
                    emit=progress,
                    cancel=job.cancel,
                    top=top,
                    objective=opts.get("objective") or "expected",
                    **own,
                )
            finally:
                if engine_for is None:
                    eng.release()  # `ondemand.engine` took it for this search

        def probe_at(diff: str, leader: str, members) -> float | None:
            # a team's served value on `diff` scored directly, or None (`Engine.served_value`)
            eng = engine_at(diff)
            try:
                value = getattr(eng, "served_value", None)
                return (
                    None
                    if value is None
                    else value(
                        opts["song"], leader, members, assumptions, opts.get("objective") or "expected"
                    )
                )
            finally:
                if engine_for is None:
                    eng.release()

        emit({"type": "stage", "stage": "build", "state": "active"})
        first = engine_at(difficulty)
        emit({"type": "stage", "stage": "build", "state": "done"})
        emit({"type": "stage", "stage": "search", "state": "active"})
        top = opts.get("top") or ondemand.RANKING
        payload = search_at(difficulty, opts["filters"], top=top, progress=emit, eng=first)
        emit({"type": "result", "payload": payload, "elapsed_ms": round((time.monotonic() - t0) * 1000)})
        # after the answer: the song's other chart, checked while the page shows this one
        contender = opts.get("contender")
        if contender and payload.get("ranking"):
            try:
                event = _check_chart(
                    payload, difficulty, contender, opts["filters"], search_at, top, probe_at=probe_at
                )
            except ondemand.Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 -- the answer above stands; say the check failed
                import traceback  # noqa: PLC0415

                print(traceback.format_exc(), flush=True)
                event = {
                    "type": "chart",
                    "check": {"other": contender["other"], "error": str(exc)},
                    "payload": None,
                }
            event["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
            emit(event)
    except ondemand.Cancelled:
        emit({"type": "cancelled", "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except KeyError as exc:
        emit({"type": "error", "message": str(exc.args[0] if exc.args else exc)})
    except MissingInput as exc:  # its message names the file and what to run
        emit({"type": "error", "message": str(exc)})
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
    seconds = next((d.get("playingSeconds") for d in songs.music_rows() if d.get("id") == opts["song"]), None)
    song_info = {"title": titles.get(opts["song"]), "singer": singers.get(opts["song"]), "seconds": seconds}
    if event_mode.is_highlight(opts["song"]):
        song_info = {
            "title": "Highlight",
            "music_rows": songs.music_rows(),
            "titles": titles,
            "singers": singers,
            # a level cap: each song at its own chart (`songs.chart_picks`)
            "charted": (
                set(songs.chart_note_counts(opts["difficulty"]) or ())
                if opts.get("max_level") is None
                else set(songs.chart_picks(opts["max_level"]))
            ),
        }
    return song_info


def require_search_inputs(basis_name: str, materials: bool) -> None:
    """`MissingInput` naming every file this search needs that is not on disk, and what builds
    them: one clear message before the search starts, instead of a failure deep inside it."""
    missing = config.missing_search_inputs(
        account=basis_name not in ("maxed", basis.MAXED_ACCOUNT_BASIS), materials=materials
    )
    if missing:
        names = ", ".join(str(p) for p in missing)
        raise MissingInput(
            f"This search needs data that is not on this PC: {names}. The refresh builds it from "
            "the game and your account: run refresh.cmd (./refresh.sh on Linux) in the holobuilder "
            "folder, or restart the app."
        )


def run_event_job(job: Job, opts: Mapping, *, runner: Callable[..., Any] | None = None) -> None:
    """The job behind `POST /api/event/run`. Events: `plan` -> `stage` / `progress`
    (per stage) -> `result{payload}` -> `end`; or `cancelled` / `error` then `end`.
    `runner` replaces `event_mode.run_account_search` (tests)."""
    import event_mode  # noqa: PLC0415
    from job_progress import JobProgress  # noqa: PLC0415

    emit = JobProgress(job.events.put)  # + eta_s / best_so_far on progress events
    t0 = time.monotonic()
    try:
        emit({"type": "plan", "stages": [dict(s) for s in EVENT_STAGES], "opts": dict(opts)})
        if runner is None:
            plan = (opts.get("assumptions") or {}) if isinstance(opts.get("assumptions"), dict) else {}
            require_search_inputs(
                str(opts.get("basis") or "account"),
                plan.get("boards") == "plan" and not plan.get("infinite"),
            )
        raw, event, _keys = event_mode.load_event_bundle(config.EVENT_BONUS_PATH, opts.get("event"))
        collection = (config.read_json(config.COLLECTION_PATH) or {}).get("cards") or {}
        song_info = _event_song_info(opts)
        if event_mode.is_highlight(opts["song"]):
            # the songs the Highlight draw favours (`event_mode.highlight_weights`)
            song_info["event_songs"] = event_mode.chapter_songs(raw, event)
        run = runner or event_mode.run_account_search
        payload = run(
            opts,
            emit=emit,
            cancel=job.cancel,
            event=event,
            collection=collection,
            song_info=song_info,
            workers=opts.get("workers") or basis.web_workers(),
        )
        emit({"type": "result", "payload": payload, "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except event_mode.EventCancelled:
        emit({"type": "cancelled", "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except MissingInput as exc:  # its message names the file and what to run
        emit({"type": "error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 — every failure is an event, never a dead stream
        import traceback  # noqa: PLC0415

        print(traceback.format_exc(), flush=True)
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        job.done = True
        emit({"type": "end"})


def cancel(h: Handler, job_id: str) -> None:
    """`POST /cancel/<id>`: ask a running job to stop."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        h.reply_json(404, {"error": "unknown job"})
        return
    job.cancel.set()
    h.reply_json(200, {"job_id": job_id, "cancelling": True})


def serve_events(h: Handler, job_id: str) -> None:
    """`GET /events/<id>`: the job's events as a Server-Sent Events stream, until `end`."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        h.reply(404, b"unknown job", "text/plain; charset=utf-8")
        return
    job.streamed = True
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
            h.wfile.write(b"data: " + json.dumps(event).encode("utf-8") + b"\n\n")
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
