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
already hold the leaf context, or in the server process when the pool has one worker;
the solver is recon-side (`highspy`), imported on first use. Nothing here writes a file.
"""
from __future__ import annotations

import dataclasses
import json
import sys

from hbuilder import paths

if str(paths.REFRESH_DIR) not in sys.path:
    sys.path.insert(0, str(paths.REFRESH_DIR))
DUMPS = paths.DUMPS

_FILES: dict = {}


def _read(name: str):
    """A dumps file, re-read when it changed on disk (a refresh rewrites board_state.json
    while the server and its workers live on: a plain cache would serve the old boards)."""
    path = DUMPS / name
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
    collection = json.loads((paths.STATE / "collection.json").read_text("utf-8"))
    document, _report, _problems = build_board.build(
        state, _read("skilltree.json"), _read("master_SkillTreeNode.json"), level=None,
        collection=collection, caps_by_rarity=caps_by_rarity_from_mapping(_read("card_caps.json")))
    return document.get("characters") or {}


# what `bench_from_plan` reads besides the planner's own inputs (`_plan_stamp`)
_PLAN_INPUTS = ("board_state.json", "skilltree.json", "master_SkillTreeNode.json", "card_caps.json")
_PLANS: dict = {}
# a search holds each re-planned row's first plan and each shown row's final one
_PLANS_HELD = 64
# `opts["candidates"] = FIRST`: the planner's first candidate alone (`first_opts`)
FIRST = "first"
# what a plan holds besides `Assumptions.plan_opts()`, never passed to the planner itself
_NOT_SOLVER_OPTS = ("candidates", "judge")


def first_opts(opts: dict) -> dict:
    """Plan options for the planner's first candidate alone: today's plan, with the Act
    levels `refine_rows` re-plans the shown rows around. A plan of its own (its own key);
    no judge picks it, so it is the same whatever leaf scores it."""
    return {**{k: v for k, v in opts.items() if k != "judge"}, "candidates": FIRST}


def judged_opts(opts: dict, assumptions, difficulty: str, leaf, levels=None) -> dict:
    """`opts` with what the judge that picks among the planner's candidates (`leaf_judge`)
    is built from, beyond the account files: the account levers, the difficulty, how the
    run is played (on the leaf's own config) and the level ranges. Held under these, a plan
    is never served to a request whose leaf would have picked another one."""
    cfg = leaf.cfg
    return {**opts, "judge": json.dumps(
        [assumptions.key(), str(difficulty), cfg.platform, bool(cfg.song_auto),
         cfg.run_state.combo, levels], default=str)}


def _plan_stamp() -> tuple:
    """Every file a held plan was made from: the planner's account inputs
    (`board_plan_solver._account_stamp`: catalogue, owned cards, collection, config, the
    boards, wallet, levels and master tables) and what `bench_from_plan` reads. A refresh
    rewrites them, and a plan made from the old ones is not served (same rule as `_read`)."""
    import board_plan_solver as bps  # noqa: PLC0415

    files = [DUMPS / n for n in _PLAN_INPUTS] + [paths.STATE / "collection.json"]
    return bps._account_stamp() + tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in files)


def plan_for(leader_id: str, member_ids, song, opts: dict, should_stop=None, judge=None,
             scored: dict | None = None, parallel: bool = False):
    """`(team board state, off-team bench characters, the whole plan {chr: {nodes}})`
    of the plan within the wallet, or None when the wallet cannot fund one. The
    whole plan matters for its cost: a from-scratch plan resets EVERY board (the
    off-team ones keep only their green), and those refunds fund the team.

    Held per process for the same team, song, options and account files: opening a
    row would otherwise solve this ~12 s plan twice, for /api/row-detail and again for
    /api/board-plan-account. Callers only read it.

    `judge(team_state, bench) -> score`: the caller's own score of a plan (`leaf_judge`); the
    planner's candidate plans are rated by it (`board_plan_solver.solve_team_board`), so the
    plan kept is the one that scores best where it is reported. A held plan is returned as
    held, whoever solved it. `scored` (a dict) receives `"score"`: the judge's score of the
    plan returned, when this call judged it (not for a held plan). `parallel`: the planner's
    second and third candidates solve in two threads (a caller waiting on one plan; the
    search's workers already fill the cores). With `first_opts` the plan is the planner's
    first candidate alone, held with its Act levels (`held[3]`). A caller with a judge passes
    `judged_opts` (a plan another leaf picked is another plan)."""
    key = _plan_key(leader_id, member_ids, song, opts)
    stamp = _plan_stamp()
    held = held_plan(leader_id, member_ids, song, opts)
    if held is not None:
        return held[1]
    import board_plan_solver as bps  # noqa: PLC0415

    # `opts`: `Assumptions.plan_opts()` (char_level, resources, max_resets, branches,
    # targets, no_refund, all_invited)
    statuses: list[str] = []
    first = opts.get("candidates") == FIRST
    solve_opts = {k: v for k, v in opts.items() if k not in _NOT_SOLVER_OPTS}
    levels: dict = {}
    team_chrs = {"chr-" + c.split("-")[1] for c in (leader_id, *member_ids)}
    rate = None
    # each judged candidate's (plan, bench, score): the one kept needs neither again
    judged: list = []
    if judge is not None:
        def rate(board):
            bench = bench_from_plan(board, team_chrs)
            value = judge({c: v for c, v in board.items() if c in team_chrs}, bench)
            judged.append((board, bench, value))
            return value
    solved = bps.solve_team_board(
        scenario="account", song=song, pool=1, for_team=[leader_id, *member_ids],
        develop_bench=True, include_sockets=False, should_stop=should_stop,
        statuses=statuses, judge=rate, parallel_candidates=parallel,
        candidates=FIRST if first else "all", levels_out=levels if first else None, **solve_opts)
    # a plan any of whose MILPs stopped on the time limit depends on the machine's load:
    # held here, but flagged so `plan_and_score` never carries it into a search, and every
    # answer that uses it says so (`plan_optimal`)
    limited = any(s != "kOptimal" for s in statuses)
    if solved is None:
        out = None
    else:
        kept = next(((bench, value) for board, bench, value in judged if board is solved), None)
        out = ({c: v for c, v in solved.items() if c in team_chrs},
               kept[0] if kept else bench_from_plan(solved, team_chrs), solved)
        if kept and scored is not None:
            scored["score"] = kept[1]
    _hold(key, (stamp, out, limited, levels) if first else (stamp, out, limited))
    return out


def _plan_key(leader_id, member_ids, song, opts) -> tuple:
    return (leader_id, tuple(member_ids), song, json.dumps(opts, sort_keys=True, default=str))


def _hold(key, entry) -> None:
    _PLANS.pop(key, None)
    if len(_PLANS) >= _PLANS_HELD:
        _PLANS.pop(next(iter(_PLANS)))
    _PLANS[key] = entry


def _loose(opts_json: str) -> str:
    """A plan's options without its judge (`judged_opts`)."""
    return json.dumps({k: v for k, v in json.loads(opts_json).items() if k != "judge"},
                      sort_keys=True, default=str)


def held_plan(leader_id, member_ids, song, opts):
    """`(stamp, plan_for(...), time-limited)` as this process holds it from the account
    files as they are now (`_plan_stamp`), or None: a worker hands it back so the server's
    row detail does not solve the same plan again. The same team, song and options
    (`judged_opts`: the same judge too). A team asked in another member order gets the
    plan held under the order it was solved in: a row opens in its slot order, the search
    held the row's plan in the search's (a row it served on its first plan alone, under its
    judge too). A caller with no judge (the account's board plan for the row on screen)
    takes the team's latest plan."""
    stamp = _plan_stamp()
    key = _plan_key(leader_id, member_ids, song, opts)
    held = _PLANS.get(key)
    if held is None or held[0] != stamp:
        team = (key[0], frozenset(key[1]), key[2])
        loose = "judge" not in opts and opts.get("candidates") != FIRST
        want = _loose(key[3]) if loose else key[3]
        held = next((entry for k, entry in reversed(_PLANS.items())
                     if entry[0] == stamp and (k[0], frozenset(k[1]), k[2]) == team
                     and (_loose(k[3]) if loose else k[3]) == want), None)
    return held


def plan_optimal(leader_id, member_ids, song, opts) -> bool | None:
    """Whether the plan this process holds for the team solved every MILP to optimality
    (False: one stopped on the solver's time limit); None when no plan is held."""
    held = held_plan(leader_id, member_ids, song, opts)
    return None if held is None or len(held) < 3 else held[2] is False


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


def leaf_judge(leaf, by_id, tree_for, budget_for, leader_id, member_ids, chart_indices=None):
    """`plan_for`'s judge on a ranking leaf: a plan scored as `plan_and_score` scores a row --
    the team's boards and the off-team bench from the plan, root sockets chosen with the
    team's, `parallel_refine.evaluate` on the leaf's charts."""
    from hbuilder.solver import parallel_refine as pr  # noqa: PLC0415

    def judge(team_state, bench):
        ctx = dataclasses.replace(leaf, board_state=team_state, bench_characters=bench,
                                  socket_bench=True)
        return float(pr.evaluate(ctx, by_id, tree_for, budget_for,
                                 (leader_id, tuple(member_ids), chart_indices), plan_cache=None))
    return judge


def _leaf_side(local, generation):
    """`(leaf, by_id, tree_for, budget_for, should_stop)` of a task: a RefinePool worker's own
    (`local` None; it stops once the parent stops `generation`), or the one-worker pool's
    in-process helpers."""
    from hbuilder.solver import parallel_refine as pr  # noqa: PLC0415

    if local is not None:
        return local
    should_stop = (lambda: pr.stopped(generation)) if generation is not None else None
    return pr._CTX, pr._BY_ID, pr._tree_for, pr._budget_for, should_stop


def plan_and_score(item, local=None):
    """`(index, leader_id, member_ids, song, chart_indices, plan options[, carried plan[,
    generation]])` -> `(index, score or None, team board state or None, reason, the held
    plan for `remember`)`. In a RefinePool worker (`local` None) the leaf context is the
    worker's own (`_CTX`) and the generation is `dispatch`'s: once the parent stops it the
    plan stops before its next MILP. A pool of one worker has no worker processes and runs
    it in the server process with `local = (ctx, by_id, tree_for, budget_for, should_stop)`,
    the pool's in-process helpers. A carried plan (`carried_plan` in the parent) is the one
    `plan_for` would solve again from the same account files: it is held here and used."""
    index, leader_id, member_ids, song, chart_indices, opts, *rest = item
    carried = rest[0] if rest else None
    generation = rest[1] if len(rest) > 1 else None
    try:
        from hbuilder.solver import parallel_refine as pr  # noqa: PLC0415

        leaf, by_id, tree_for, budget_for, should_stop = _leaf_side(local, generation)
        if should_stop is not None and should_stop():
            return index, None, None, "the search was cancelled", None
        if carried is not None and carried[0] == _plan_stamp():
            _hold(_plan_key(leader_id, member_ids, song, opts), carried)
        scored: dict = {}
        planned = plan_for(leader_id, member_ids, song, opts, should_stop=should_stop,
                           judge=leaf_judge(leaf, by_id, tree_for, budget_for, leader_id,
                                            member_ids, chart_indices), scored=scored)
        if planned is None:
            return index, None, None, "the wallet cannot fund a plan for this team", None
        team_state, bench, _whole = planned
        # the plan's own judge is this very evaluation; a held plan is evaluated here
        value = scored.get("score")
        if value is None:
            # the bench's root sockets chosen with the team's (each card once, account-wide)
            ctx = dataclasses.replace(leaf, board_state=team_state, bench_characters=bench,
                                      socket_bench=True)
            value = pr.evaluate(ctx, by_id, tree_for, budget_for,
                                (leader_id, tuple(member_ids), chart_indices), plan_cache=None)
        return (index, float(value), team_state, None,
                held_plan(leader_id, member_ids, song, opts))
    # SystemExit too: it would end the worker, and `imap_unordered` waits for a lost task
    # forever -- a search that never returns and cannot be cancelled
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - one team failing is a row note, not a dead search
        return index, None, None, f"{type(exc).__name__}: {exc}", None


def refine_and_score(item, local=None):
    """`(index, leader_id, member_ids, song, chart_indices, plan options, the row's held first
    plan (`first_opts`), candidate[, generation])` -> `(index, candidate, score or None,
    (team board state, bench, whole plan) or None, time-limited)`: one of the planner's other
    candidates around the row's first plan (`board_plan_solver.solve_candidate`), judged as
    `plan_and_score` judges a row. Worker and one-worker pool as there; a candidate that
    fails leaves the row on its first plan."""
    index, leader_id, member_ids, song, chart_indices, opts, first, candidate, *rest = item
    try:
        import board_plan_solver as bps  # noqa: PLC0415

        leaf, by_id, tree_for, budget_for, should_stop = _leaf_side(local, rest[0] if rest else None)
        if should_stop is not None and should_stop():
            return index, candidate, None, None, False
        statuses: list[str] = []
        board = bps.solve_candidate(
            candidate, first[1][2], first[3], scenario="account", song=song, pool=1,
            for_team=[leader_id, *member_ids], develop_bench=True, should_stop=should_stop,
            statuses=statuses, **{k: v for k, v in opts.items() if k not in _NOT_SOLVER_OPTS})
        if board is None:
            return index, candidate, None, None, False
        team_chrs = {"chr-" + c.split("-")[1] for c in (leader_id, *member_ids)}
        team_state = {c: v for c, v in board.items() if c in team_chrs}
        bench = bench_from_plan(board, team_chrs)
        score = leaf_judge(leaf, by_id, tree_for, budget_for, leader_id, member_ids,
                           chart_indices)(team_state, bench)
        return (index, candidate, score, (team_state, bench, board),
                any(s != "kOptimal" for s in statuses))
    except (Exception, SystemExit):  # noqa: BLE001 - a candidate is an improvement, never a failure
        return index, candidate, None, None, False


class _BestCandidate:
    """The plan each refined row keeps: the best of its first plan and its candidates, a tie
    keeping the earliest (`board_plan_solver.CANDIDATES`), the plan one `plan_for` would keep."""

    def __init__(self) -> None:
        import board_plan_solver as bps  # noqa: PLC0415

        self._order = bps.CANDIDATES
        self.best: dict = {}
        self.first_of: dict = {}

    def add_row(self, row) -> list:
        """`row` as `refine_rows` takes it; returns its candidate items (none without a first
        plan that carries its Act levels)."""
        import board_plan_solver as bps  # noqa: PLC0415

        self.best[row[0]] = (row[7], 0, None)
        self.first_of[row[0]] = row[6]
        if row[6] is None or len(row[6]) <= 3:
            return []
        return [(*row[:7], name) for name in bps.other_candidates(row[3])]

    def add_result(self, out) -> None:
        index, name, score, planned, limited = out
        rank = self._order.index(name)
        if score is not None and (score, -rank) > self.best[index][:2]:
            first = self.first_of[index]
            self.best[index] = (score, -rank, (first[0], planned, bool(limited or first[2])))

    def refined(self) -> dict:
        return {index: (score, entry[1][0], entry) for index, (score, _r, entry) in self.best.items()
                if entry is not None}


def refine_rows(pool, rows, cancel=None, progress=None) -> dict:
    """The rows a search shows, re-planned with the planner's other candidates in the
    engine's workers (one task per candidate, `refine_and_score`). `rows`: `[(index,
    leader_id, member_ids, song, chart_indices, plan options, the held first plan, its
    score)]`. Returns `{index: (score, team board state, held plan)}` for every row a
    candidate beats -- the best of the first plan and the candidates, a tie keeping the
    earliest (`board_plan_solver.CANDIDATES`): the plan one `plan_for` would keep."""
    acc = _BestCandidate()
    items = [it for r in rows for it in acc.add_row(r)]
    for done, out in enumerate(dispatch(pool, items, cancel, task=refine_and_score), 1):
        if progress:
            progress(done, len(items))
        acc.add_result(out)
    return acc.refined()


def plan_and_refine(pool, items, *, opts, shown: int, rank, cancel=None, progress=None,
                    on_plan=None) -> dict:
    """`dispatch` (each team's first plan; `items` as there) and then `refine_rows` for the
    `shown` best planned rows, OVERLAPPED: a row's candidates start as soon as it is certain
    to be among the rows shown -- fewer than `shown` rows can still rank above it: those
    planned above it plus every plan not back yet (a failed plan only drops out) -- so the
    workers a finished first wave leaves idle take them while the last first plans run. The
    same tasks on the same inputs as the two calls one after the other, so the same answer.

    `rank(index, score)`: a planned row's sort key, lowest first (the caller's own order of
    the rows shown). `opts`: the candidates' plan options. `on_plan(result)`: each first
    plan's `plan_and_score` result, as it completes. `progress(done, total)` counts both
    kinds of task (`total` estimated until the rows shown are known). On a cancel this
    returns early, as `dispatch` does. Returns `refine_rows`'s answer."""
    import board_plan_solver as bps  # noqa: PLC0415

    acc = _BestCandidate()
    item_of = {it[0]: it for it in items}
    got: dict = {}
    started: set = set()
    state = {"pending": len(items), "done": 0, "queued": 0,
             "total": len(items) + len(bps.other_candidates(items[0][3]) if items else ())
             * min(shown, len(items))}

    def ready() -> list:
        """The candidate tasks of every row now certain to be shown."""
        order = sorted(got, key=lambda k: rank(k, got[k][0]))
        new = []
        for position, k in enumerate(order[:shown]):
            if k in started or position + state["pending"] >= shown:
                continue
            started.add(k)
            value, held = got[k]
            new += [(refine_and_score, it)
                    for it in acc.add_row((k, *item_of[k][1:5], opts, held, value))]
        return new

    def more(task, out) -> list:
        state["done"] += 1
        follow: list = []
        if task is refine_and_score:
            acc.add_result(out)
        else:
            state["pending"] -= 1
            if on_plan:
                on_plan(out)
            index, value, _state, _reason, held = out
            if value is not None:
                got[index] = (float(value), held)
            follow = ready()
        state["queued"] += len(follow)
        if not state["pending"]:
            # every first plan is back: the rows shown, and so the task count, are known
            state["total"] = len(items) + state["queued"]
        if progress:
            progress(state["done"], max(state["total"], state["done"]))
        return follow

    first = [(plan_and_score, (*it[:6], it[6] if len(it) > 6 else None)) for it in items]
    for _pair in _dispatch_tasks(pool, first, cancel, more):
        pass
    return acc.refined()


def dispatch(pool, items, cancel=None, task=None):
    """`plan_and_score` over `items` in the engine's `RefinePool` `pool`, yielding each
    result as it completes. At most one task per worker process is in flight, the next
    submitted as one finishes, in `items` order -- the throughput of `imap_unordered`, but
    a set `cancel` (a threading.Event, polled every half second) stops it: no queued task
    is started, and the running ones stop before their next MILP (`pool.stop_through`;
    otherwise a cancelled search's plans keep running and the next search waits up to
    ~70 s behind them). On cancel this simply returns; the caller raises its
    own Cancelled. Each item gets the carried plan slot (None) when it lacks one, and
    this batch's generation.

    A pool of one worker (`pool._pool` None) plans here, one item after another, with the
    pool's own in-process helpers: the same `plan_and_score` on the same inputs, so the
    same numbers. A set `cancel` stops before the next item and before the next MILP.
    `task`: another task with the same contract (`refine_and_score`; its items complete)."""
    task = task or plan_and_score
    if task is plan_and_score:
        items = [(*it[:6], it[6] if len(it) > 6 else None) for it in items]
    for _task, out in _dispatch_tasks(pool, [(task, it) for it in items], cancel):
        yield out


def _dispatch_tasks(pool, pending, cancel=None, more=None):
    """`dispatch`'s engine over `(task, item)` pairs, yielding `(task, result)` as each
    completes. `more(task, result)` may return further pairs, queued after those waiting."""
    import queue  # noqa: PLC0415

    pending = list(pending)
    workers = pool._pool
    if workers is None:
        stop = cancel.is_set if cancel is not None else None
        local = (pool.ctx, pool._by_id, pool._tree_for, pool._budget_for, stop)
        while pending:
            if cancel is not None and cancel.is_set():
                return
            task, it = pending.pop(0)
            out = task(it, local)
            if more is not None:
                pending += more(task, out)
            yield task, out
        return
    generation = pool.new_generation()
    done: queue.Queue = queue.Queue()
    in_flight = 0
    limit = max(1, int(getattr(workers, "_processes", 0) or len(pending) or 1))
    while pending or in_flight:
        while pending and in_flight < limit:
            task, it = pending.pop(0)
            workers.apply_async(task, ((*it, generation),),
                                callback=lambda out, task=task: done.put((task, out)),
                                error_callback=done.put)
            in_flight += 1
        while True:
            if cancel is not None and cancel.is_set():
                pool.stop_through(generation)
                return
            try:
                got = done.get(timeout=0.5)
                break
            except queue.Empty:
                continue
        in_flight -= 1
        if isinstance(got, BaseException):
            raise got
        task, out = got
        if more is not None:
            pending += more(task, out)
        yield task, out
