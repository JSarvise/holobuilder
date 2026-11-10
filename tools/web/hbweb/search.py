"""The Optimizer and Versus routes: `POST /api/search` (starts a search job),
`POST /api/row-detail` (one ranked row's numbers), `POST /api/timeline` (a team's
lanes on a chart), `POST /api/compare` (two teams side by side), and the header
strip's `GET /api/meta` and `GET /api/ranks`.

Every route loads cards through the same `load_inputs` / `apply_collection` /
`load_board_basis` the search uses: a second definition of what a card is worth is
this project's named expensive failure mode.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import types
from collections.abc import Mapping
from urllib.parse import parse_qs

import compare_view
import row_detail

from hbuilder import paths
from hbuilder.domain.board import BoardError
from hbuilder.domain.collection import CollectionError
from hbuilder.domain.scoring_config import ACTIVE_UPTIME_MODELS
from hbuilder.domain.song import SongError, charts_from_mapping, timeline
from hbuilder.domain.team import MEMBER_COUNT, Team, TeamError
from hbuilder.errors import MissingInput
from hbuilder.inputs import (
    _LoadFailure,
    apply_collection,
    load_board_basis,
    load_inputs,
    max_level_board_points,
)
from hbweb import admin, basis, config, jobs, songs


def serve_ranks(h) -> None:
    """`GET /api/ranks`: the community rank tables (`row_detail.load_ranks`)."""
    h.reply_json(200, row_detail.load_ranks(paths.HOLODORI))


def start_search(h) -> None:
    """`POST /api/search {song (empty = in general), basis?: one of
    leaf_inputs.BASES,
    difficulty?, leader_member?, leader_card?, no_leader_overlap?, require?,
    exclude?, top?}` -- the optimiser ON DEMAND (`ondemand.py`): 202
    `{job_id}`; events on `/events/<id>`; cancel with `POST /cancel/<id>`.
    Read-only."""
    import leaf_inputs  # noqa: PLC0415
    import ondemand  # noqa: PLC0415

    try:
        body = h.read_json_body()
    except json.JSONDecodeError:
        h.reply_json(400, {"error": "the body is not valid JSON"})
        return
    song = str(body.get("song") or "").strip()
    # no song = in general: the mean over every chart (`ondemand.Engine.search`)
    if song and not all(ch.isalnum() or ch in "_-" for ch in song):
        h.reply_json(400, {"error": "song must be a music id (or empty for in general)"})
        return
    basis_name = str(body.get("basis") or "account")
    if basis_name not in leaf_inputs.BASES:
        h.reply_json(400, {"error": f"basis must be one of {sorted(leaf_inputs.BASES)}"})
        return
    try:
        difficulty = basis.parse_difficulty(body, song)
    except basis.HttpError as exc:
        h.reply_json(exc.code, exc.obj)
        return
    try:
        top = max(1, min(200, int(body.get("top") or ondemand.RANKING)))
    except (TypeError, ValueError):
        top = ondemand.RANKING
    # `assumptions` (the account levers, `ondemand.Assumptions`): the
    # account under them; absent, the fixed `basis`
    assumptions = None
    if isinstance(body.get("assumptions"), dict):
        try:
            assumptions = ondemand.parse_assumptions(body["assumptions"])
        except (TypeError, ValueError) as exc:
            h.reply_json(400, {"error": f"assumptions: {exc}"})
            return
        basis_name = "account"
    # holodori.best-style "any song" level range: In general over every chart of any
    # difficulty whose level is in [lo, hi]
    levels = None
    if body.get("levels") not in (None, "", []):
        try:
            lo, hi = (int(v) for v in body["levels"])
        except (TypeError, ValueError):
            h.reply_json(400, {"error": "levels must be [lowest, highest]"})
            return
        if song or not 1 <= lo <= hi <= 40:
            h.reply_json(400, {"error": "levels need no song (In general) and 1 <= lowest <= highest <= 40"})
            return
        levels = (lo, hi)
    opts = {
        "song": song,
        "basis": basis_name,
        "difficulty": difficulty,
        "top": top,
        "filters": ondemand.parse_filters(body),
        "assumptions": assumptions,
        "levels": levels,
        "play": ondemand.parse_play(body),
    }
    job = jobs.launch(jobs.run_search_job, opts, "search")
    h.reply_json(
        202,
        {
            "job_id": job.job_id,
            "opts": {k: v for k, v in opts.items() if k not in ("filters", "assumptions")},
        },
    )


def row_request(body: Mapping) -> dict:
    """The row a `/api/row-detail` (or `/api/export-team`) body names, checked
    (`basis.HttpError` on a bad one): `song`, `ids` (leader + 5), `basis`,
    `difficulty`, `refine`, `order`, `assumptions`, `pool`, `play`."""
    song = str(body.get("song") or "").strip()
    ids = [str(c) for c in (body.get("cards") or []) if c]
    if not song or len(ids) != MEMBER_COUNT + 1:
        raise basis.HttpError(
            400, {"error": f"need a song and {MEMBER_COUNT + 1} card ids (leader + 5 members)"}
        )
    basis_name = str(body.get("basis") or "account")
    import leaf_inputs  # noqa: PLC0415

    if basis_name not in leaf_inputs.BASES:
        raise basis.HttpError(400, {"error": f"basis must be one of {sorted(leaf_inputs.BASES)}"})
    difficulty = basis.parse_difficulty(body, song)
    order = body.get("member_order") if isinstance(body.get("member_order"), list) else None
    import ondemand  # noqa: PLC0415

    assumptions = None
    if isinstance(body.get("assumptions"), dict):
        try:
            assumptions = ondemand.parse_assumptions(body["assumptions"])
        except (TypeError, ValueError) as exc:
            raise basis.HttpError(400, {"error": f"assumptions: {exc}"}) from None
    pool = [str(s) for s in body.get("pool") or [] if s] if isinstance(body.get("pool"), list) else []
    return {
        "song": song,
        "ids": ids,
        "basis": basis_name,
        "difficulty": difficulty,
        "refine": body.get("refine"),
        "order": [str(c) for c in order] if order else None,
        "assumptions": assumptions,
        "pool": pool,
        # the search's platform and AUTO, or the row is not its own
        "play": ondemand.parse_play(body),
    }


def row_figures(req: Mapping, body: Mapping, *, keep_law: bool = False) -> dict:
    """The row's detail (`row_detail.two_stage_detail`, or `pool_detail` for a Highlight row
    on the whole pool) for a `row_request`. Raises what those raise."""
    ids = req["ids"]
    if req["pool"]:
        # a Highlight row on the WHOLE pool: one board, one slot order
        # (`row_detail.pool_detail`); `song` is any pool song (the difficulty's)
        out = row_detail.pool_detail(
            req["basis"],
            req["pool"],
            req["difficulty"],
            ids[0],
            ids[1:],
            assumptions=req["assumptions"],
            refine=req["refine"] is not False,
            play=req["play"],
        )
        out.update(
            {
                "available": True,
                "difficulty": req["difficulty"],
                "basis": req["basis"],
                "leader_id": ids[0],
                "member_ids": ids[1:],
            }
        )
        return out
    # the row's refinement level (`refined`: True = 1, 2 when every frequency count was
    # tried, "counts" for an In general row, its frequency counts chosen song by song);
    # anything else is the plain develop
    refine = req["refine"] if req["refine"] is True or req["refine"] in (2, "counts") else False
    out = row_detail.two_stage_detail(
        req["basis"],
        req["song"],
        req["difficulty"],
        ids[0],
        ids[1:],
        refine=refine,
        assumptions=req["assumptions"],
        want_distribution=body.get("distribution") is True,
        want_timeline=body.get("timeline") is True,
        member_order=req["order"],
        as_is=body.get("as_is") is not False,
        want_reset_price=body.get("reset_price") is True,
        play=req["play"],
        keep_law=keep_law,
        want_notes=body.get("notes") is True,
    )
    out.update(
        {
            "available": True,
            "song": req["song"],
            "difficulty": req["difficulty"],
            "basis": req["basis"],
            "leader_id": ids[0],
            "member_ids": ids[1:],
        }
    )
    return out


def _row_reply(h, make) -> None:
    """Reply `make()` as JSON; the row routes' errors as a 400 with the message (a missing
    chart or account dump, a bad team, a key not in the basis' pool), never an HTML 500."""
    try:
        out = make()
    except basis.HttpError as exc:
        h.reply_json(exc.code, exc.obj)
        return
    except KeyError as exc:
        h.reply_json(400, {"error": str(exc)})
        return
    except (TeamError, BoardError, SongError, ValueError) as exc:
        h.reply_json(400, {"error": f"{type(exc).__name__}: {exc}"})
        return
    except MissingInput as exc:  # a chart or an account dump that is not on disk
        h.reply_json(400, {"error": str(exc)})
        return
    except Exception as exc:  # noqa: BLE001 — report as JSON, never HTML 500
        h.reply_error(exc)
        return
    h.reply_json(200, out)


def row_detail_route(h) -> None:
    """`POST /api/row-detail` — ONE number set for a two-stage / event row,
    re-developed on the leaf that ranked it (`row_detail.two_stage_detail`):
    song figures, slot order, deck power, board state, Overall Power lines,
    the Score Bonus split and, on request, the timeline and the run law.

    Body: `{"song", "difficulty"?, "basis": "account"|"maxed"|"account-max",
    "platform"?, "auto"?: bool (how the run is played, as the search was asked),
    "cards": [leader, m1..m5], "refine"?: the row's `refined` (true: it was ranked with
    the activation refinement; 2: every frequency count tried too; "counts": an In
    general row, its frequency counts chosen on this song; default false),
    "member_order"?, "timeline"?: bool,
    "distribution"?: bool, "as_is"?: bool (default true: `song_est_as_is` on the
    account's captured boards + `plan_cost` of the planned boards vs the
    wallet, `row_detail._as_is`), "reset_price"?: bool (a within-wallet row
    with resets: the same team re-planned with fewer, `row_detail.reset_price`;
    a few plans more, so the page asks for it on its own), "pool"?: [music ids] (a
    Highlight row on the WHOLE pool, `row_detail.pool_detail`: one board and one slot
    order for every song; `song` is then any of them)}`. Read-only."""
    body = h.body_or_empty()
    _row_reply(h, lambda: row_figures(row_request(body), body))


def export_team_route(h) -> None:
    """`POST /api/export-team` — the row as a `.hbteam` document (`hbteam.document`): the
    request, the detail the server computes for it now (with the run law's summary), the
    numbers the page showed (`shown`), the search that ranked it (`search`) and the account
    files the score reads, so another install can replay it (`tools/web/hbteam.py replay`).
    Body: a `/api/row-detail` body plus `shown`?: {...} and `search`?: {...}. Saves the file
    in the user's Downloads folder (`team_file.save`: the desktop app's window cannot download)
    and answers `{"path", "name", "bytes"}`; writes nothing else."""
    body = h.body_or_empty()

    def make() -> dict:
        import hbteam  # noqa: PLC0415
        import team_file  # noqa: PLC0415  (tools/refresh: tools/web/ writes no files)

        req = row_request(body)
        out = row_figures(req, {**body, "distribution": True, "timeline": False, "notes": True})
        doc = hbteam.document(
            hbteam.request_of(body),
            out,
            shown=body.get("shown") if isinstance(body.get("shown"), dict) else None,
            search=body.get("search") if isinstance(body.get("search"), dict) else None,
        )
        path, csv_path = team_file.save(doc)
        return {
            "path": str(path),
            "name": path.name,
            "bytes": path.stat().st_size,
            "csv": csv_path.name if csv_path else None,
        }

    _row_reply(h, make)


def _timeline_inputs(body: Mapping, basis_name: str):
    """`(cards, cfg, maxed-account scenario)` under the SAME level/bloom what-if, uptime
    model, platform, deck-power basis and AUTO premise the ranking was solved with."""
    assume_level = basis.parse_assume(body, "assume_level", 1, 80)
    assume_bloom = basis.parse_assume(body, "assume_bloom", 0, 6)
    uptime_model = str(body.get("uptime_model") or "").lower()
    platform = basis.parse_platform(body)
    cards, owned, cfg, _prov = load_inputs(config.CARDS_PATH, config.OWNED_PATH)
    if uptime_model in ACTIVE_UPTIME_MODELS:
        cfg = dataclasses.replace(cfg, active_uptime_model=uptime_model)
    cfg = dataclasses.replace(cfg, platform=platform, **basis.deck_power_basis(body))
    cfg = dataclasses.replace(cfg, **basis.auto_premise(cfg, body))
    try:
        cards, _collection_basis, cfg, ma_scenario = basis.apply_basis(
            cards, owned, cfg, basis_name, assume_level=assume_level, assume_bloom=assume_bloom
        )
    except basis.MaxedAccountUnavailable as exc:
        raise basis.HttpError(503, {"error": str(exc)}) from None
    return cards, cfg, ma_scenario


def _timeline_board(
    team, chart, cfg, cards, basis_name: str, board_state: dict | None, use_board: bool, ma_scenario
):
    """`(board_model, optimised_model)`: the row's own board when `board_state` is
    given (rebuilt EXACTLY as the develop assembled it, `row_detail.board_from_state`:
    the basis' Connect cards, bench boards, cast names and singer context), else the
    captured board (the maxed-account scenario's bench on that basis)."""
    optimised_model = None
    if board_state:
        dev = compare_view._Developer(cfg, cards, basis_name, None, chart, ma_scenario)
        try:
            optimised_model, _placements = row_detail.board_from_state(
                team,
                board_state,
                cfg=cfg,
                skilltree=dev.skilltree,
                node_effect_ids=dev.effects,
                tree_for=dev.tree_for,
                connect_cards=dev.connect_cards,
                bench_characters=dev.bench_characters,
                name_by_chr=dev.name_by_chr,
                sings=dev.cast,
                # a state carrying its sockets keeps them (the row's own board)
                use_slots=any((v or {}).get("slots") for v in board_state.values()),
            )
        except ValueError as exc:
            raise basis.HttpError(400, {"error": f"board_state: {exc}"}) from None
    if ma_scenario is not None:
        captured = None if (config.BOARD_DISABLED or not use_board) else ma_scenario.board_model
    else:
        captured = load_board_basis(
            None if (config.BOARD_DISABLED or not use_board) else config.BOARD_PATH
        ).model
    return (optimised_model if optimised_model is not None else captured), optimised_model


def serve_timeline(h) -> None:
    """`POST /api/timeline`: when each of a team's five members is actually live, on one
    axis -- `song.timeline()`'s five lanes, the chart's own SPECIAL cues and every
    ACTIVE's cooldown lattice.

    ⛔ **The slot order is COMPUTED, never taken from the caller.** Cue `i` fires the
    `i`-th member's SPECIAL, so which member sits in which slot changes the picture AND
    the score; the lanes drawn are those of the arrangement the scorer recommends
    (`row_detail.figures`). `member_order_requested_matches` says whether the caller's
    order was that one.

    `board_state` (the optimiser's {chr:{nodes,slots}}) draws the timeline on the row's
    own board; absent, on the captured board."""
    body = h.body_or_empty()
    music_id = str(body.get("song") or "")
    wanted = [str(c) for c in (body.get("cards") or []) if c]
    if not music_id or len(wanted) != MEMBER_COUNT + 1:
        h.reply_json(
            400,
            {
                "error": (
                    f"need a song and exactly {MEMBER_COUNT + 1} card ids "
                    f"(1 leader + {MEMBER_COUNT} members); "
                    f"got song={music_id!r} and {len(wanted)} card(s)"
                ),
            },
        )
        return
    try:
        difficulty = basis.parse_difficulty(body, music_id)
    except basis.HttpError as exc:
        h.reply_json(exc.code, exc.obj)
        return
    charts_file = basis.charts_path(difficulty)
    raw = config.read_json(charts_file)
    chart_doc = ((raw or {}).get("charts") or {}).get(f"{music_id}/{difficulty}")
    if not chart_doc:
        h.reply_json(
            200,
            {
                "available": False,
                "reason": f"no {difficulty} chart for {music_id} in {charts_file.name}",
            },
        )
        return

    basis_name = basis.parse_basis(body.get("basis"))
    use_board = body.get("board", True) is not False
    board_state = body.get("board_state") if isinstance(body.get("board_state"), dict) else None
    try:
        cards, cfg, ma_scenario = _timeline_inputs(body, basis_name)
        team, cfg = _timeline_team(cards, wanted, cfg)
        chart = charts_from_mapping(
            {"schema_version": raw.get("schema_version"), "charts": {f"{music_id}/{difficulty}": chart_doc}},
            source=str(charts_file.name),
        ).get(music_id, difficulty)

        cfg = dataclasses.replace(cfg, board=None)
        board_model, optimised_model = _timeline_board(
            team, chart, cfg, cards, basis_name, board_state, use_board, ma_scenario
        )
        cfg_board = dataclasses.replace(cfg, board=board_model)

        fig = row_detail.figures(team, chart, cfg_board)
        drawing = timeline(team.members, chart, cfg_board, tuple(fig["member_order"]))
        law = (
            row_detail.distribution(
                team, chart, cfg_board, fig["member_order"], fig["deck_power"], row_score=fig["song_est"]
            )
            if body.get("distribution") is True
            else None
        )
    except basis.HttpError as exc:
        h.reply_json(exc.code, exc.obj)
        return
    except compare_view.CompareError as exc:
        h.reply_json(exc.code, {"error": exc.message})
        return
    except (_LoadFailure, CollectionError, BoardError, SongError, TeamError, MissingInput) as exc:
        # First-class, like every other bad input on this server: a readable
        # reason beats an HTML error page whose JSON parse error names a
        # column number.
        h.reply_json(200, {"available": False, "reason": f"{type(exc).__name__}: {exc}"})
        return
    h.reply_json(
        200,
        _timeline_payload(
            body,
            music_id,
            difficulty,
            basis_name,
            team.leader.id,
            fig,
            board_model,
            optimised_model,
            drawing,
            law,
        ),
    )


def _timeline_team(cards, wanted: list[str], cfg):
    """`(team, cfg)` of the six requested cards. The timeline draws any six cards; the
    deck screen's Overall Power is only defined for a legal team, so an illegal roster is
    drawn on the modelled stat pool, labelled."""
    by_id = {c.id: c for c in cards}
    missing = [c for c in wanted if c not in by_id]
    if missing:
        raise basis.HttpError(400, {"error": f"unknown card id(s): {missing}"})
    leader = by_id[wanted[0]]
    members = tuple(by_id[c] for c in wanted[1:])
    try:
        return Team(leader=leader, members=members), cfg
    except TeamError:
        return (
            types.SimpleNamespace(leader=leader, members=members),
            dataclasses.replace(cfg, song_absolute_deck_power=False),
        )


def _timeline_payload(
    body: Mapping,
    music_id: str,
    difficulty: str,
    basis_name: str,
    leader_id: str,
    fig: Mapping,
    board_model,
    optimised_model,
    drawing,
    law,
) -> dict:
    """The timeline's answer: the row's figures, the board it was drawn on, the lanes."""
    out = {
        "available": True,
        "music_id": music_id,
        "difficulty": difficulty,
        "leader_id": leader_id,
        # the row's figures (row_detail.figures), the same numbers
        # /api/compare and /api/row-detail return for this board
        "total_score": fig["song_est"],
        "song_est": fig["song_est"],
        "max_score": fig["song_max"],
        "song_max": fig["song_max"],
        "deck_power": fig["deck_power"],
        "deck_power_basis": fig["deck_power_basis"],
        "member_order": fig["member_order"],
        "basis": basis_name,
        "board": board_model is not None,
        "optimised_board": optimised_model is not None,
        "board_rebuild": "exact (board_model_from_plan)" if optimised_model is not None else None,
        "timeline": drawing,
    }
    requested = body.get("member_order")
    if isinstance(requested, list) and sorted(map(str, requested)) == sorted(fig["member_order"]):
        out["member_order_requested_matches"] = [str(c) for c in requested] == fig["member_order"]
    if law is not None:
        out["distribution"] = law
    return out


def compare(h) -> None:
    """`POST /api/compare` — two hand-picked teams scored side by side under
    one set of assumptions. All logic lives in `compare_view.build_compare`;
    this only parses the JSON and maps its typed errors to status codes."""
    body = h.body_or_empty()
    try:
        h.reply_json(200, compare_view.build_compare(body, art_dir=h.art_dir))
    except compare_view.CompareError as exc:
        h.reply_json(exc.code, {"error": exc.message})
    except Exception as exc:  # noqa: BLE001 — report as JSON, never HTML 500
        h.reply_error(exc)


def _meta_assumed(h) -> dict | None:
    """`/api/meta`'s `assumed`: the account bonuses under the query's card levers
    (`ondemand.account_bonuses`), or with `?basis=maxed` the Maxed account's
    (`ondemand.maxed_bonuses`); None when the query sets neither."""
    import ondemand  # noqa: PLC0415

    query = parse_qs(h.path.split("?", 1)[1]) if "?" in h.path else {}
    raw = {k: query[k][0] for k in ("level", "bloom", "sp_training", "keep_higher") if k in query}
    if (query.get("basis") or [""])[0] == "maxed":
        b = ondemand.maxed_bonuses()
    elif raw:
        raw["keep_higher"] = raw.get("keep_higher") in ("1", "true")
        b = ondemand.account_bonuses(ondemand.parse_assumptions(raw))
    else:
        return None
    if b is None:
        return None
    return {"power_up_permyriad": b.power_up_permyriad, "memory_permil": b.memory_permil}


def serve_meta(h) -> None:
    """`GET /api/meta`: the one-line facts the page shows before any run: how many cards
    the account owns of the catalogue, how many boards the capture carries, and
    the two account-level bonuses every score silently includes (memory /
    member power-up)."""
    try:
        cards, owned, _cfg, _prov = load_inputs(config.CARDS_PATH, config.OWNED_PATH)
        _cards2, collection_basis = apply_collection(
            cards,
            str(config.COLLECTION_PATH),
            owned_ids=[c.id for c in owned],
            maxed=False,
        )
        bonuses = collection_basis.bonuses
        bs = config.read_json(config.BOARD_STATE_PATH) or {}
        captured = sum(1 for v in bs.values() if (v or {}).get("nodes"))
        try:
            snapshot_mtime = _dt.datetime.fromtimestamp(config.SNAPSHOT_PATH.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M"
            )
        except OSError:
            snapshot_mtime = None
        charts_raw = config.read_json(config.CHARTS_PATH) or {}
        chart_count = len(charts_raw.get("charts") or {})
        difficulties = songs.difficulties_listing()
        try:
            import importlib.util  # noqa: PLC0415

            solver_available = importlib.util.find_spec("highspy") is not None
        except Exception:  # noqa: BLE001
            solver_available = False
        try:
            max_points = max_level_board_points()
        except MissingInput:
            max_points = None
        h.reply_json(
            200,
            {
                "catalogue": len(cards),
                "owned": len(owned),
                # provenance the header strip states in one line
                "snapshot_mtime": snapshot_mtime,
                # a page reloaded mid-refresh shows it running instead of offering a second
                "refresh_running": admin.REFRESH_LOCK.locked(),
                "chart_count": chart_count,
                # the chart difficulties a run may ask for (`difficulty` on the searches,
                # /api/compare, /api/timeline, ?difficulty= on /api/songs): the default, what is on disk, how many
                # charts each holds, and — only when they vary — the EXPERT
                # songs with no chart at a difficulty
                "difficulties": difficulties,
                # whether the exact HiGHS plan can run at all (typed 503 otherwise)
                "solver_available": solver_available,
                "boards_with_nodes": captured,
                # what a max-level holomem holds (the master's CharacterLevel), the "Max
                # Holomem Board Pt" switch and the maxed account; None when the export is absent
                "max_level_board_points": max_points,
                "boards_total": len(bs),
                "memory_posters": getattr(bonuses, "posters", None) if bonuses else None,
                "memory_bonus_permil": (bonuses.memory_permil if bonuses is not None else None),
                "power_up_permyriad": (
                    bonuses.power_up_permyriad
                    if bonuses is not None and bonuses.power_up_permyriad is not None
                    # `player.power_up_permyriad: "auto"` — the derived value the
                    # scoring itself uses, never a hand-typed stale one.
                    else getattr(collection_basis.derived_power_up, "permyriad", None)
                ),
                # `?level=&bloom=&sp_training=&keep_higher=` (the Optimizer's "Upgraded
                # cards") or `?basis=maxed`: the bonuses that search scores with, else None
                "assumed": _meta_assumed(h),
            },
        )
    except Exception as exc:  # noqa: BLE001 — an info strip must never 500 the page
        # `load_inputs` raises `_LoadFailure(exit code)` from the real error: say that one
        # (otherwise the page shows "_LoadFailure: 3" for "owned: id '...' is not present
        # in the loaded cards")
        if isinstance(exc, _LoadFailure) and exc.__cause__ is not None:
            exc = exc.__cause__
        h.reply_json(200, {"error": f"{type(exc).__name__}: {exc}"})
