"""Boards planned WITHIN THE ACCOUNT'S OWN MATERIALS, for the on-demand search.

With "Assume infinite cubes and hexas" OFF (the same option the community tool
holodori.best offers), each result team's boards are planned within the cubes / hexas
the account has, counting what a reset would refund (and, optionally, a cap on how
many owned nodes the plan may reset: each costs 1,000 Hologold in game). The search's
leaf plans boards on POINTS only, so this is a second stage after the search:

* the search ranks teams with boards planned on points (materials assumed);
* the best few are re-planned EXACTLY within the wallet by
  `board_plan_solver.solve_team_board` (one joint MILP over the team's boards and
  the off-team boards' green, the shared cube/hexa wallet as constraints), and each
  is re-scored by the exact leaf on THAT board (`RefineContext.board_state`), the
  off-team boards as that plan leaves them.

The work runs inside the engine's RefinePool workers (`plan_and_score`), which
already hold the leaf context; the solver is recon-side (`highspy`), imported in
the worker on first use. Nothing here writes a file.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

_PROXY = Path(__file__).resolve().parents[1] / "recon" / "proxy"
if str(_PROXY) not in sys.path:
    sys.path.insert(0, str(_PROXY))

_FILES: dict = {}


def _read(name: str):
    """A dumps file, re-read when it changed on disk (a refresh rewrites board_state.json
    while the server and its workers live on: a plain cache would serve the old boards)."""
    path = _PROXY / "dumps" / name
    stamp = path.stat().st_mtime_ns
    held = _FILES.get(name)
    if held is None or held[0] != stamp:
        held = _FILES[name] = (stamp, json.loads(path.read_text(encoding="utf-8")))
    return held[1]


def bench_from_plan(solved: dict, team_chrs: set[str]) -> dict:
    """The off-team boards as the plan leaves them, as a `board.json` `characters`
    mapping (what the leaf's `bench_characters` reads): the captured state with
    every planned off-team board replaced (`precompute_optimisations.
    developed_bench_characters` does the same around a nominal team)."""
    import build_board  # noqa: PLC0415
    from hbuilder.domain.collection import caps_by_rarity_from_mapping  # noqa: PLC0415

    state = {k: dict(v) for k, v in _read("board_state.json").items()}
    for chr_id, plan in solved.items():
        if chr_id not in team_chrs:
            state[chr_id] = {**state.get(chr_id, {}), "nodes": plan["nodes"]}
    collection = json.loads((_PROXY.parents[2] / "state" / "collection.json").read_text("utf-8"))
    document, _report, _problems = build_board.build(
        state, _read("skilltree.json"), _read("master_SkillTreeNode.json"), level=None,
        collection=collection, caps_by_rarity=caps_by_rarity_from_mapping(_read("card_caps.json")))
    return document.get("characters") or {}


# the account files a plan is solved from: a refresh rewrites them, and a held plan
# made from the old ones must not be served (same rule as `_read`)
_PLAN_INPUTS = ("board_state.json", "inventory.json", "card_params.json", "skilltree.json",
                "master_SkillTreeNode.json", "card_caps.json", "charts.json")
_PLANS: dict = {}
_PLANS_HELD = 32


def _plan_stamp() -> tuple:
    paths = [_PROXY / "dumps" / n for n in _PLAN_INPUTS]
    paths += [_PROXY.parents[2] / "state" / "collection.json", _PROXY.parents[2] / "data" / "owned.json"]
    return tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in paths)


def plan_for(leader_id: str, member_ids, song, opts: dict, should_stop=None):
    """`(team board state, off-team bench characters, the whole plan {chr: {nodes}})`
    of the plan within the wallet, or None when the wallet cannot fund one. The
    whole plan matters for its cost: a from-scratch plan resets EVERY board (the
    off-team ones keep only their green), and those refunds fund the team.

    Held per process for the same team, song, options and account files: opening a
    row would otherwise solve this ~12 s plan twice, for /api/row-detail and again for
    /api/board-plan-account. Callers only read it."""
    key = _plan_key(leader_id, member_ids, song, opts)
    stamp = _plan_stamp()
    held = _PLANS.get(key)
    if held is not None and held[0] == stamp:
        return held[1]
    import board_plan_solver as bps  # noqa: PLC0415

    # `opts`: `Assumptions.plan_opts()` (char_level, resources, max_resets, branches,
    # targets, no_refund, all_invited)
    solved = bps.solve_team_board(
        scenario="account", song=song, pool=1, for_team=[leader_id, *member_ids],
        develop_bench=True, include_sockets=False, should_stop=should_stop, **opts)
    # a plan any of whose MILPs stopped on the time limit depends on the machine's load:
    # held here, but flagged so `plan_and_score` never carries it into a search
    limited = any(s != "kOptimal" for s in bps.LAST_STATUSES)
    if solved is None:
        out = None
    else:
        team_chrs = {"chr-" + c.split("-")[1] for c in (leader_id, *member_ids)}
        out = ({c: v for c, v in solved.items() if c in team_chrs},
               bench_from_plan(solved, team_chrs), solved)
    _hold(key, (stamp, out, limited))
    return out


def _plan_key(leader_id, member_ids, song, opts) -> tuple:
    return (leader_id, tuple(member_ids), song, json.dumps(opts, sort_keys=True, default=str))


def _hold(key, entry) -> None:
    _PLANS.pop(key, None)
    if len(_PLANS) >= _PLANS_HELD:
        _PLANS.pop(next(iter(_PLANS)))
    _PLANS[key] = entry


def held_plan(leader_id, member_ids, song, opts):
    """`(stamp, plan_for(...), time-limited)` as this process holds it, or None: a worker
    hands it back so the server's row detail does not solve the same plan again."""
    return _PLANS.get(_plan_key(leader_id, member_ids, song, opts))


def carried_plan(leader_id, member_ids, song, opts):
    """The plan this process holds for the team, to hand to a worker with the task
    (`plan_and_score`'s 7th item), or None. Only one every MILP of which solved to
    optimality: a time-limited plan is whatever the solver had at 60 s under that
    moment's load, and a re-solve may find the optimum. Carrying optimal plans saves
    re-solving them when a filtered search follows an unfiltered one (about a fifth of
    the worker time)."""
    held = held_plan(leader_id, member_ids, song, opts)
    return held if held is not None and len(held) >= 3 and held[2] is False else None


def remember(leader_id, member_ids, song, opts, entry) -> None:
    """Hold a plan a worker solved (`held_plan` there), while the account files are
    the ones it was solved from."""
    if entry is not None and entry[0] == _plan_stamp():
        _hold(_plan_key(leader_id, member_ids, song, opts), entry)


def plan_and_score(item):
    """Worker task: `(index, leader_id, member_ids, song, chart_indices, plan options[,
    carried plan[, generation]])` -> `(index, score or None, team board state or None,
    reason, the held plan for `remember`)`. Runs in a RefinePool worker (its `_CTX` is the
    engine's leaf context). A carried plan (`carried_plan` in the parent) is the one
    `plan_for` would solve again from the same account files: it is held here and used.
    The generation is `dispatch`'s: once the parent stops it the plan stops before its
    next MILP."""
    index, leader_id, member_ids, song, chart_indices, opts, *rest = item
    carried = rest[0] if rest else None
    generation = rest[1] if len(rest) > 1 else None
    try:
        from hbuilder.solver import parallel_refine as pr  # noqa: PLC0415

        should_stop = ((lambda: pr.stopped(generation)) if generation is not None else None)
        if should_stop is not None and should_stop():
            return index, None, None, "the search was cancelled", None
        if carried is not None and carried[0] == _plan_stamp():
            _hold(_plan_key(leader_id, member_ids, song, opts), carried)
        planned = plan_for(leader_id, member_ids, song, opts, should_stop=should_stop)
        if planned is None:
            return index, None, None, "the wallet cannot fund a plan for this team", None
        team_state, bench, _whole = planned
        # the bench's root sockets chosen with the team's (each card once, account-wide)
        ctx = dataclasses.replace(pr._CTX, board_state=team_state, bench_characters=bench,
                                  socket_bench=True)
        value = pr.evaluate(ctx, pr._BY_ID, pr._tree_for, pr._budget_for,
                            (leader_id, tuple(member_ids), chart_indices), plan_cache=None)
        return (index, float(value), team_state, None,
                held_plan(leader_id, member_ids, song, opts))
    # SystemExit too: it would end the worker, and `imap_unordered` waits for a lost task
    # forever -- a search that never returns and cannot be cancelled
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - one team failing is a row note, not a dead search
        return index, None, None, f"{type(exc).__name__}: {exc}", None


def dispatch(pool, items, cancel=None):
    """`plan_and_score` over `items` in the engine's `RefinePool` `pool`, yielding each
    result as it completes. At most one task per worker process is in flight, the next
    submitted as one finishes, in `items` order -- the throughput of `imap_unordered`, but
    a set `cancel` (a threading.Event, polled every half second) stops it: no queued task
    is started, and the running ones stop before their next MILP (`pool.stop_through`;
    otherwise a cancelled search's plans keep running and the next search waits up to
    ~70 s behind them). On cancel this simply returns; the caller raises its
    own Cancelled. Each item gets the carried plan slot (None) when it lacks one, and
    this batch's generation."""
    import queue  # noqa: PLC0415

    workers = pool._pool
    generation = pool.new_generation()
    pending = [(*it[:6], it[6] if len(it) > 6 else None, generation) for it in items]
    done: queue.Queue = queue.Queue()
    in_flight = 0
    limit = max(1, int(getattr(workers, "_processes", 0) or len(pending) or 1))
    while pending or in_flight:
        while pending and in_flight < limit:
            workers.apply_async(plan_and_score, (pending.pop(0),),
                                callback=done.put, error_callback=done.put)
            in_flight += 1
        while True:
            if cancel is not None and cancel.is_set():
                pool.stop_through(generation)
                return
            try:
                out = done.get(timeout=0.5)
                break
            except queue.Empty:
                continue
        in_flight -= 1
        if isinstance(out, BaseException):
            raise out
        yield out
