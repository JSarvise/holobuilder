"""The Optimizer's Holomem Rating goal: `GET /api/rating` (every holomem, the account's rating
and rank on each), `GET /api/rating/status?holomem=&max_level=&owned=` (one holomem: rating,
rank, borders, the songs that count) and `POST /api/rating/run` (the searches, as a job read
off `/events/<id>`). The logic is `rating_mode.py`."""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from hbuilder.errors import MissingInput
from hbweb import basis, jobs

if TYPE_CHECKING:
    from hbweb.router import Handler

MAX_LEVEL_DEFAULT = 40


def _query(h: Handler) -> dict[str, str]:
    return {k: v[-1] for k, v in parse_qs(urlparse(h.path).query).items()}


def _level(raw: Any) -> int:
    try:
        return max(1, min(40, int(raw)))
    except (TypeError, ValueError):
        return MAX_LEVEL_DEFAULT


def serve_rating(h: Handler) -> None:
    """`GET /api/rating`: every holomem a song counts for, with the account's rating and rank."""
    import rating_mode  # noqa: PLC0415

    try:
        doc = rating_mode.rating_doc() or {}
        ranking = doc.get("ranking") or {}
        h.reply_json(
            200,
            {
                "holomems": rating_mode.holomem_list(),
                "have_records": bool(doc),
                "captured_at": doc.get("_captured_at"),
                "total": doc.get("total"),
                "ranking_read_at": ranking.get("read_at"),
                "ranking_note": doc.get("ranking_note"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        h.reply_error(exc)


def serve_status(h: Handler) -> None:
    """`GET /api/rating/status?holomem=chr-…&max_level=N&owned=1`."""
    import rating_mode  # noqa: PLC0415

    q = _query(h)
    chr_id = q.get("holomem") or ""
    if not chr_id.startswith("chr-"):
        h.reply_json(400, {"error": "holomem must be a chr id"})
        return
    try:
        h.reply_json(
            200,
            rating_mode.status(
                chr_id, max_level=_level(q.get("max_level")), owned_only=q.get("owned", "1") != "0"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        h.reply_error(exc)


def parse_run(body: Mapping) -> dict:
    """The options of a `POST /api/rating/run` body (ValueError on a bad one)."""
    import leaf_inputs  # noqa: PLC0415
    import ondemand  # noqa: PLC0415
    import rating_mode  # noqa: PLC0415

    chr_id = str(body.get("holomem") or "")
    if not chr_id.startswith("chr-"):
        raise ValueError("holomem must be a chr id")
    mode = str(body.get("mode") or "each")
    if mode not in ("each", "one"):
        raise ValueError("mode must be each or one")
    objective = str(body.get("objective") or "max")
    if objective not in ("max", "expected"):
        raise ValueError("objective must be max or expected")
    basis_name = str(body.get("basis") or "account")
    if basis_name not in leaf_inputs.BASES:
        raise ValueError(f"basis must be one of {sorted(leaf_inputs.BASES)}")
    assumptions = None
    if isinstance(body.get("assumptions"), dict):
        assumptions = ondemand.parse_assumptions(body["assumptions"])
        basis_name = "account"
    max_level = _level(body.get("max_level"))
    owned_only = body.get("owned_only") is not False
    pool, _skipped = rating_mode.song_pool(chr_id, max_level=max_level, owned_only=owned_only)
    if not pool:
        raise ValueError("no song of this holomem can be played: none is owned, charted or within your level")
    wanted = body.get("songs")
    if isinstance(wanted, list) and wanted:
        by_id = {s["music_id"]: s for s in pool}
        chosen = [by_id[m] for m in (str(x) for x in wanted) if m in by_id]
        if not chosen:
            raise ValueError("none of the songs asked for counts for this holomem within your level")
    else:
        count = int(body.get("song_count") or rating_mode.DEFAULT_SONGS)
        chosen = pool[: max(1, min(rating_mode.MAX_SONGS, count))]
    cap = rating_mode.MAX_SONGS if mode == "each" else rating_mode.MAX_MERGED_SONGS
    chosen = chosen[:cap]
    filters = ondemand.parse_filters(body)
    name = rating_mode.holomem_names().get(chr_id)
    if not name:
        raise ValueError(f"unknown holomem {chr_id}")
    # a run is credited to its leader: the holomem always leads
    filters = dataclasses.replace(filters, leader_member=name)
    doc = rating_mode.rating_doc()
    return {
        "holomem": chr_id,
        "name": name,
        "mode": mode,
        "objective": objective,
        "one_board": body.get("one_board") is not False,
        "teams": max(1, min(10, int(body.get("teams") or 3))),
        "max_level": max_level,
        "owned_only": owned_only,
        "songs": chosen,
        "current": rating_mode.current_per_song(doc, chr_id),
        "basis": basis_name,
        "assumptions": assumptions,
        "filters": filters,
        "play": ondemand.parse_play(body),
    }


def start_run(h: Handler) -> None:
    """`POST /api/rating/run`: 202 `{job_id}`; events on `/events/<id>`. Read-only."""
    try:
        body = h.read_json_body()
    except json.JSONDecodeError:
        h.reply_json(400, {"error": "the body is not valid JSON"})
        return
    try:
        opts = parse_run(body)
    except (TypeError, ValueError, KeyError) as exc:
        h.reply_json(400, {"error": str(exc.args[0] if exc.args else exc)})
        return
    job = jobs.launch(run_job, opts, "rating")
    shown = {k: v for k, v in opts.items() if k not in ("filters", "assumptions", "current")}
    h.reply_json(202, {"job_id": job.job_id, "opts": shown})


def _engine_kwargs(opts: Mapping) -> dict:

    kw: dict[str, Any] = {"workers": basis.web_workers(), "play": opts.get("play") or ("mobile", False)}
    if opts.get("assumptions") is not None:
        kw["assumptions"] = opts["assumptions"]
    return kw


def run_job(job: jobs.Job, opts: Mapping, *, engine_for=None) -> None:
    """The job behind `POST /api/rating/run`. Events: `plan` -> `progress` / `song` (each
    song as it is done) -> `result{payload}` -> `end`; or `cancelled` / `error` then `end`."""
    import ondemand  # noqa: PLC0415
    import rating_mode  # noqa: PLC0415
    from job_progress import JobProgress  # noqa: PLC0415

    emit = JobProgress(job.events.put)
    t0 = time.monotonic()
    try:
        emit(
            {
                "type": "plan",
                "stages": [{"id": "songs", "label": "Search the songs"}],
                "opts": {k: v for k, v in opts.items() if k not in ("filters", "assumptions", "current")},
            }
        )
        if engine_for is None:
            jobs.require_search_inputs(
                opts["basis"], bool(getattr(opts.get("assumptions"), "materials", False))
            )
        basis_name = opts["basis"]
        kw = _engine_kwargs(opts)
        account = opts.get("assumptions") is not None

        def key_for(difficulty):
            return (
                (lambda: ondemand.account_key(difficulty))
                if account
                else (lambda: ondemand.basis_key(basis_name, difficulty))
            )

        if opts["mode"] == "each":

            def each_engine(difficulty):
                if engine_for is not None:
                    return engine_for(difficulty)
                return ondemand.engine(basis_name, difficulty, current_key=key_for(difficulty), **kw)

            payload = rating_mode.run_each_song(opts, engine_for=each_engine, emit=emit, cancel=job.cancel)
        else:

            def one_engine(keys, cast):
                if engine_for is not None:
                    return engine_for(keys, cast)
                return ondemand.engine(
                    basis_name,
                    "EXPERT",
                    current_key=key_for("EXPERT"),
                    chart_keys=keys,
                    board_cast=cast,
                    **kw,
                )

            payload = rating_mode.run_one_team(opts, engine_for=one_engine, emit=emit, cancel=job.cancel)
        payload.update(
            {
                "holomem": opts["holomem"],
                "name": opts["name"],
                "max_level": opts["max_level"],
                "play": {"platform": opts["play"][0], "auto": opts["play"][1]},
            }
        )
        emit({"type": "result", "payload": payload, "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except ondemand.Cancelled:
        emit({"type": "cancelled", "elapsed_ms": round((time.monotonic() - t0) * 1000)})
    except KeyError as exc:
        emit({"type": "error", "message": str(exc.args[0] if exc.args else exc)})
    except MissingInput as exc:
        emit({"type": "error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 — every failure is an event, never a dead stream
        import traceback  # noqa: PLC0415

        print(traceback.format_exc(), flush=True)
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        job.done = True
        emit({"type": "end"})
