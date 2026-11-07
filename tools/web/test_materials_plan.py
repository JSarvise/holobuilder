"""`materials_plan`'s worker side and its dispatch: a cancelled search
stops its plans, a plan the parent already holds is carried instead of solved again, a
time-limited plan is never carried (and says so in `plan_optimal`), one worker plans in the
server process, and a SystemExit inside a plan is a row note, not a hung search. No solver,
no pool: the plan itself is stubbed.

    python -m pytest tools/web/test_materials_plan.py
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import materials_plan
from hbuilder.solver import parallel_refine as pr

_REAL_STAMP = materials_plan._plan_stamp


@pytest.fixture(autouse=True)
def _no_held_plans(monkeypatch):
    monkeypatch.setattr(materials_plan, "_PLANS", {})
    monkeypatch.setattr(materials_plan, "_plan_stamp", lambda: ("stamp",))


class _Pool:
    """A `RefinePool` whose `apply_async` runs inline; after `stop_after` submissions the
    cancel is set."""

    def __init__(self, processes=2, cancel=None, stop_after=None):
        self._pool = self
        self._processes = processes
        self.cancel, self.stop_after = cancel, stop_after
        self.submitted, self.stopped = [], []

    def new_generation(self):
        return 7

    def stop_through(self, generation):
        self.stopped.append(generation)

    def apply_async(self, fn, args, callback, error_callback):
        self.submitted.append(args[0])
        callback((args[0][0], 1.0, None, None, None))
        if self.stop_after is not None and len(self.submitted) >= self.stop_after:
            self.cancel.set()


def _items(n):
    return [(k, f"card-0000{k}-5-a-0000-00", ("m",), "m0020", None, {}) for k in range(n)]


def test_dispatch_yields_every_plan_in_order_with_its_generation():
    pool = _Pool()
    got = [r[0] for r in materials_plan.dispatch(pool, _items(5))]
    assert got == [0, 1, 2, 3, 4]
    assert all(len(it) == 8 and it[6] is None and it[7] == 7 for it in pool.submitted)
    assert pool.stopped == []


def test_a_cancel_starts_no_queued_plan_and_stops_the_running_ones():
    cancel = threading.Event()
    pool = _Pool(processes=1, cancel=cancel, stop_after=2)
    got = [r[0] for r in materials_plan.dispatch(pool, _items(6), cancel)]
    assert len(pool.submitted) == 2 and len(got) <= 2
    assert pool.stopped == [7]                      # a running plan stops before its next MILP


def test_a_plan_of_a_stopped_generation_returns_without_solving(monkeypatch):
    monkeypatch.setattr(pr, "_STOP", SimpleNamespace(value=7))
    monkeypatch.setattr(materials_plan, "plan_for",
                        lambda *a, **k: pytest.fail("a stopped plan was solved"))
    out = materials_plan.plan_and_score((3, "card-00001-5-a-0000-00", ("m",), "m0020", None, {},
                                         None, 7))
    assert out[0] == 3 and out[1] is None and "cancelled" in out[3]
    assert not pr.stopped(8)                        # the next search's plans run


def test_a_carried_plan_is_used_and_not_solved_again(monkeypatch):
    import board_plan_solver

    monkeypatch.setattr(board_plan_solver, "solve_team_board",
                        lambda **k: pytest.fail("a carried plan was solved again"))
    plan = ({"chr-00001": {"nodes": []}}, {}, {"chr-00001": {"nodes": []}})
    carried = (("stamp",), plan, False)
    monkeypatch.setattr(pr, "_CTX", SimpleNamespace(), raising=False)
    monkeypatch.setattr(pr, "evaluate", lambda *a, **k: 7.0)
    monkeypatch.setattr(materials_plan.dataclasses, "replace", lambda ctx, **k: ctx)
    out = materials_plan.plan_and_score((0, "card-00001-5-a-0000-00", ("m",), "m0020", None, {},
                                         carried, None))
    assert out[1] == 7.0 and out[2] == plan[0]


def test_a_plan_judged_while_solved_keeps_its_score_and_bench(monkeypatch):
    import board_plan_solver

    whole = {"chr-00001": {"nodes": ["R-001"]}, "chr-00009": {"nodes": ["G-001"]}}
    benches: list = []
    monkeypatch.setattr(materials_plan, "bench_from_plan",
                        lambda solved, team: benches.append(solved) or {"bench": len(benches)})

    def solve(**k):
        k["judge"](whole)                  # the planner rates its candidate with the judge
        return whole
    monkeypatch.setattr(board_plan_solver, "solve_team_board", solve)
    monkeypatch.setattr(pr, "_CTX", SimpleNamespace(), raising=False)
    evaluations: list = []
    monkeypatch.setattr(pr, "evaluate", lambda *a, **k: evaluations.append(a) or 9.0)
    monkeypatch.setattr(materials_plan.dataclasses, "replace", lambda ctx, **k: ctx)
    out = materials_plan.plan_and_score((0, "card-00001-5-a-0000-00", ("card-00002-5-b-0000-00",),
                                         "m0020", None, {}, None, None))
    # one evaluation (the judge's), one bench built: the row's score and bench are the judged ones
    assert out[1] == 9.0 and len(evaluations) == 1 and len(benches) == 1
    assert out[2] == {"chr-00001": {"nodes": ["R-001"]}}


class _OneWorker:
    """A one-worker `RefinePool`: `dispatch` runs the tasks here with these helpers."""
    _pool = None
    ctx = _by_id = _tree_for = _budget_for = None


def test_refine_rows_keeps_the_first_plan_unless_a_candidate_beats_it(monkeypatch):
    first = (("stamp",), ({"chr-1": "first"}, {}, {"chr-1": "first"}), False, {"chr-2": (1, 30)})
    scores = {(0, "bare"): 100.0, (0, "in_board"): 99.0, (1, "bare"): 60.0, (1, "in_board"): 60.0}
    seen: list = []

    def task(item, local):
        index, *_x, candidate = item
        seen.append((index, candidate, item[5]))
        planned = ({"chr-1": candidate}, {}, {"chr-1": candidate})
        return index, candidate, scores[(index, candidate)], planned, candidate == "in_board"
    monkeypatch.setattr(materials_plan, "refine_and_score", task)
    rows = [(0, "L0", ("m",), "m0020", None, {"o": 1}, first, 100.0),
            (1, "L1", ("m",), "m0020", None, {"o": 1}, first, 50.0)]
    out = materials_plan.refine_rows(_OneWorker(), rows)
    assert sorted((i, c) for i, c, _o in seen) == [(0, "bare"), (0, "in_board"),
                                                 (1, "bare"), (1, "in_board")]
    assert all(o == {"o": 1} for *_x, o in seen)              # the row's own plan options
    # row 0: a tie keeps the first plan; row 1: the tie between the two goes to the earlier
    assert list(out) == [1]
    score, state, held = out[1]
    assert score == 60.0 and state == {"chr-1": "bare"} and held == (("stamp",), (
        {"chr-1": "bare"}, {}, {"chr-1": "bare"}), False)


def test_off_a_song_only_the_in_board_candidate_is_tried(monkeypatch):
    first = (("stamp",), ({}, {}, {}), False, {})
    seen: list = []
    monkeypatch.setattr(materials_plan, "refine_and_score",
                        lambda item, local: seen.append(item[-1]) or (item[0], item[-1], None, None, False))
    assert materials_plan.refine_rows(_OneWorker(), [(0, "L", ("m",), None, None, {}, first, 1.0)]) == {}
    assert seen == ["in_board"]


def test_a_team_asked_in_another_member_order_gets_its_held_plan(monkeypatch):
    import board_plan_solver

    monkeypatch.setattr(board_plan_solver, "solve_team_board",
                        lambda **k: pytest.fail("the held plan was solved again"))
    plan = ({"chr-00001": {"nodes": ["R-001"]}}, {}, {"chr-00001": {"nodes": ["R-001"]}})
    materials_plan._hold(("card-1", ("card-2", "card-3"), "m0020", "{}"), (("stamp",), plan, False))
    # the row detail asks in the row's slot order
    assert materials_plan.plan_for("card-1", ["card-3", "card-2"], "m0020", {}) is plan
    assert materials_plan.plan_optimal("card-1", ["card-3", "card-2"], "m0020", {}) is True
    # another team, another song: not that plan
    assert materials_plan.held_plan("card-1", ["card-3", "card-4"], "m0020", {}) is None
    assert materials_plan.held_plan("card-1", ["card-3", "card-2"], "m0021", {}) is None


def test_only_a_plan_solved_to_optimality_is_carried():
    key = ("card-00001-5-a-0000-00", ("m",), "m0020", {})
    materials_plan._hold(materials_plan._plan_key(*key), (("stamp",), "plan", True))
    assert materials_plan.carried_plan(*key) is None
    materials_plan._hold(materials_plan._plan_key(*key), (("stamp",), "plan", False))
    assert materials_plan.carried_plan(*key) == (("stamp",), "plan", False)


def test_one_worker_plans_in_this_process_with_the_pools_own_helpers(monkeypatch):
    # a RefinePool of one worker has no worker processes (`_pool` None): the plans run
    # here, in order, on the pool's in-process context, never on the worker globals
    seen = []
    plan = ({"chr-00001": {"nodes": []}}, {"bench": 1}, {"chr-00001": {"nodes": []}})
    monkeypatch.setattr(materials_plan, "plan_for", lambda *a, **k: plan)
    monkeypatch.setattr(pr, "_CTX", None, raising=False)
    monkeypatch.setattr(materials_plan.dataclasses, "replace", lambda ctx, **k: (ctx, k))
    monkeypatch.setattr(pr, "evaluate",
                        lambda ctx, by_id, tree_for, budget_for, team, **k: seen.append(
                            (ctx, by_id, tree_for, budget_for, team)) or 5.0)
    pool = SimpleNamespace(_pool=None, ctx="leaf", _by_id={"id": 1}, _tree_for="trees",
                           _budget_for="budgets")
    got = list(materials_plan.dispatch(pool, _items(3)))
    assert [r[0] for r in got] == [0, 1, 2] and all(r[1] == 5.0 for r in got)
    (ctx, kw), by_id, tree_for, budget_for, _team = seen[0]
    assert (ctx, by_id, tree_for, budget_for) == ("leaf", {"id": 1}, "trees", "budgets")
    assert kw["board_state"] == plan[0] and kw["bench_characters"] == plan[1]


def test_one_worker_stops_on_cancel_between_plans(monkeypatch):
    cancel = threading.Event()
    monkeypatch.setattr(materials_plan, "plan_and_score",
                        lambda item, local=None: cancel.set() or (item[0], 1.0, None, None, None))
    pool = SimpleNamespace(_pool=None, ctx=None, _by_id={}, _tree_for=None, _budget_for=None)
    assert [r[0] for r in materials_plan.dispatch(pool, _items(4), cancel)] == [0]


def test_plan_optimal_reads_the_held_plans_time_limit_flag():
    key = ("card-00001-5-a-0000-00", ("m",), "m0020", {})
    assert materials_plan.plan_optimal(*key) is None
    materials_plan._hold(materials_plan._plan_key(*key), (("stamp",), "plan", True))
    assert materials_plan.plan_optimal(*key) is False
    materials_plan._hold(materials_plan._plan_key(*key), (("stamp",), "plan", False))
    assert materials_plan.plan_optimal(*key) is True


def test_a_system_exit_in_a_plan_is_a_row_note(monkeypatch):
    def boom(*_a, **_k):
        raise SystemExit("missing dumps/board_state.json")
    monkeypatch.setattr(materials_plan, "plan_for", boom)
    out = materials_plan.plan_and_score((1, "card-00001-5-a-0000-00", ("m",), "m0020", None, {}))
    assert out[1] is None and out[3].startswith("SystemExit")


# ---------------------------------------------------------------- a held plan is never stale
# every file the planner or the bench reads: a refresh that rewrites any of them re-plans
ACCOUNT_FILES = ["data/cards.json", "data/owned.json", "state/collection.json", "config.json",
                 *(f"tools/recon/proxy/dumps/{n}" for n in (
                     "board_state.json", "inventory.json", "card_params.json",
                     "account_snapshot_main.json", "card_level_curve.json", "card_caps.json",
                     "skilltree.json", "master_SkillTreeNode.json", "charts.json",
                     "skill_ladder.json", "master_index.json", "master_refetched/SkillTreeNode.db"))]


def test_every_file_a_plan_is_made_from_is_in_its_stamp(monkeypatch, tmp_path):
    import os

    from hbuilder import paths

    monkeypatch.setattr(materials_plan, "_plan_stamp", _REAL_STAMP)
    monkeypatch.setattr(paths, "REPO", tmp_path)
    monkeypatch.setattr(paths, "STATE", tmp_path / "state")
    monkeypatch.setattr(materials_plan, "DUMPS", tmp_path / "tools/recon/proxy/dumps")
    for rel in ACCOUNT_FILES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("{}", encoding="utf-8")
        os.utime(tmp_path / rel, ns=(10 ** 18, 10 ** 18))
    before = materials_plan._plan_stamp()
    for k, rel in enumerate(ACCOUNT_FILES, 1):
        later = 10 ** 18 + k * 10 ** 9                  # a refresh rewrote it, k seconds later
        os.utime(tmp_path / rel, ns=(later, later))
        now = materials_plan._plan_stamp()
        assert now != before, f"{rel} is not in the plan's stamp"
        before = now


def _counting_solver(monkeypatch):
    import board_plan_solver

    solved: list = []

    def solve(**k):
        solved.append(k)
        return {"chr-00001": {"nodes": [f"R-{len(solved):03d}"]}}
    monkeypatch.setattr(board_plan_solver, "solve_team_board", solve)
    monkeypatch.setattr(materials_plan, "bench_from_plan", lambda solved_, team: {})
    return solved


def test_a_held_plan_is_planned_again_after_a_refresh(monkeypatch):
    solved = _counting_solver(monkeypatch)
    stamp = ["files as they were"]
    monkeypatch.setattr(materials_plan, "_plan_stamp", lambda: tuple(stamp))
    first = materials_plan.plan_for("card-00001-5-a-0000-00", ["card-00002-5-b-0000-00"], "m0020", {})
    assert materials_plan.plan_for("card-00001-5-a-0000-00", ["card-00002-5-b-0000-00"],
                                   "m0020", {}) is first and len(solved) == 1
    stamp[0] = "a refresh rewrote the inventory"
    again = materials_plan.plan_for("card-00001-5-a-0000-00", ["card-00002-5-b-0000-00"], "m0020", {})
    assert len(solved) == 2 and again is not first
    # nor is the old one served to a row in another member order, or as the plan's flag
    assert materials_plan.held_plan("card-00001-5-a-0000-00", ["card-00002-5-b-0000-00"],
                                    "m0020", {})[1] is again


def _judged(level="account", difficulty="EXPERT", auto=False, **opts_over):
    import ondemand

    raw = {"level": level, "bloom": "account", "keep_higher": False, "sp_training": "account",
           "all_invited": False, "no_refund": False, "branches": "GBRY", "targets": [],
           "boards": "plan", "max_points": False, "infinite": False, "max_resets": None,
           "reset_min_gain": 0.01}
    a = ondemand.parse_assumptions(raw)
    leaf = SimpleNamespace(cfg=SimpleNamespace(platform="mobile", song_auto=auto,
                                               run_state=SimpleNamespace(combo=0 if auto else None)))
    return materials_plan.judged_opts({**a.plan_opts(), **opts_over}, a, difficulty, leaf)


def test_a_change_of_settings_plans_again_and_nothing_else_does(monkeypatch):
    solved = _counting_solver(monkeypatch)
    lead, team = "card-00001-5-a-0000-00", ["card-00002-5-b-0000-00", "card-00003-5-c-0000-00"]

    def plan(members=team, song="m0020", **kw):
        return materials_plan.plan_for(lead, members, song, _judged(**kw), judge=lambda *a: 1.0)
    base = plan()
    assert plan() is base and plan(members=team[::-1]) is base and len(solved) == 1
    # every lever that changes the plan or the leaf that picks it: a plan of its own
    for change in ({"song": "m0021"}, {"resources": "infinite"}, {"branches": "GR"},
                   {"max_resets": 0}, {"level": "max"}, {"difficulty": "MASTER"},
                   {"auto": True}):
        n = len(solved)
        assert plan(**change) is not base, change
        assert len(solved) == n + 1, change
    assert plan() is base and len(solved) == 8                 # the first is still held


def test_the_first_plan_is_shared_by_every_judge_and_a_caller_without_one_gets_the_latest(
        monkeypatch):
    solved = _counting_solver(monkeypatch)
    lead, team = "card-00001-5-a-0000-00", ["card-00002-5-b-0000-00"]
    first = materials_plan.plan_for(lead, team, "m0020", materials_plan.first_opts(_judged()))
    assert materials_plan.plan_for(lead, team, "m0020",
                                   materials_plan.first_opts(_judged(level="max"))) is first
    assert len(solved) == 1                                   # no judge picks the first plan
    # a judged request is never served the first plan by itself: only a search that kept it
    # for its row holds it under its judge (`remember`), and then that row opens on it
    judged = materials_plan.plan_for(lead, team, "m0020", _judged(), judge=lambda *a: 1.0)
    assert judged is not first and len(solved) == 2
    materials_plan.remember(lead, tuple(team), "m0020", _judged(level="max"),
                            (("stamp",), first, False, {}))
    assert materials_plan.plan_for(lead, team, "m0020", _judged(level="max"),
                                   judge=lambda *a: 1.0) is first and len(solved) == 2
    # the account board plan for the row on screen (no judge): the team's latest plan
    plain = {k: v for k, v in _judged().items() if k != "judge"}
    assert materials_plan.plan_for(lead, team, "m0020", plain) is first and len(solved) == 2
    assert materials_plan.plan_optimal(lead, team, "m0020", plain) is True


class _ThreadPool:
    """A `RefinePool` whose tasks finish on timers (`delay(item)` seconds), so they complete
    out of order and overlap as real workers do; `log` records each submit and completion."""

    def __init__(self, processes, delay):
        self._pool = self
        self._processes = processes
        self.delay = delay
        self.log: list = []

    def new_generation(self):
        return 1

    def stop_through(self, generation):
        pass

    def apply_async(self, fn, args, callback, error_callback):
        item = args[0]
        kind = "refine" if fn is materials_plan.refine_and_score else "plan"
        self.log.append(("submit", kind, item[0]))

        def run():
            out = fn(item)
            self.log.append(("done", kind, item[0]))
            callback(out)
        threading.Timer(self.delay(kind, item), run).start()


def _fake_tasks(monkeypatch, values, scores):
    """`plan_and_score` -> `values[index]` (None: the wallet cannot fund it), with a first plan
    that carries Act levels; `refine_and_score` -> `scores[(index, candidate)]`."""
    def plan(item, local=None):
        index = item[0]
        v = values[index]
        held = (("stamp",), ({"chr": index}, {}, {"chr": index}), False, {"lv": index})
        return (index, v, None, None if v is not None else "no", held if v is not None else None)

    def refine(item, local=None):
        index, candidate = item[0], item[7]
        planned = ({"chr": candidate}, {}, {"chr": candidate})
        return index, candidate, scores[(index, candidate)], planned, False
    monkeypatch.setattr(materials_plan, "plan_and_score", plan)
    monkeypatch.setattr(materials_plan, "refine_and_score", refine)


def _sequential(values, scores, items, shown, opts):
    """The two-phase answer: every first plan, then `refine_rows` on the `shown` best."""
    got = [k for k, v in enumerate(values) if v is not None]
    held = {k: (("stamp",), ({"chr": k}, {}, {"chr": k}), False, {"lv": k}) for k in got}
    order = sorted(got, key=lambda k: (-values[k], k))[:shown]
    rows = [(k, *items[k][1:5], opts, held[k], values[k]) for k in order]
    return materials_plan.refine_rows(_OneWorker(), rows)


def test_overlapped_replan_gives_the_two_phase_answer(monkeypatch):
    import random
    rng = random.Random(7)
    for trial in range(6):
        n = rng.randint(4, 9)
        values = [None if rng.random() < 0.15 else float(rng.randint(1, 50)) for _ in range(n)]
        scores = {(k, c): float(rng.randint(1, 60)) for k in range(n) for c in ("bare", "in_board")}
        _fake_tasks(monkeypatch, values, scores)
        items = _items(n)
        shown = rng.randint(1, n)
        pool = _ThreadPool(rng.randint(1, 4), lambda kind, item: rng.random() * 0.02)
        planned = []
        out = materials_plan.plan_and_refine(
            pool, items, opts={"o": 1}, shown=shown, rank=lambda k, v: (-v, k),
            on_plan=planned.append)
        assert sorted(r[0] for r in planned) == list(range(n))
        assert out == _sequential(values, scores, items, shown, {"o": 1}), trial


def test_a_row_sure_to_be_shown_is_refined_while_the_last_plans_run(monkeypatch):
    # six teams, four rows shown, four workers: plans 0-2 return at once, 3-5 much later.
    # After the third, three plans are out and row 0 (the best back) has 0 above it: sure to
    # be shown, refined while all three late plans run. Row 1 is not (the three late plans
    # may all beat it) and, as it turns out, is not shown.
    values = [100.0, 90.0, 10.0, 200.0, 150.0, 120.0]
    scores = {(k, c): 1.0 for k in range(6) for c in ("bare", "in_board")}
    _fake_tasks(monkeypatch, values, scores)
    pool = _ThreadPool(4, lambda kind, item: 0.3 + 0.2 * (item[0] - 3)
                       if kind == "plan" and item[0] >= 3 else 0.01)
    seen = []
    materials_plan.plan_and_refine(pool, _items(6), opts={}, shown=4, rank=lambda k, v: (-v, k),
                                   progress=lambda d, n: seen.append((d, n)))
    first_slow = pool.log.index(("done", "plan", 3))
    assert {e[2] for e in pool.log[:first_slow] if e[:2] == ("done", "refine")} == {0}
    refined = {e[2] for e in pool.log if e[:2] == ("submit", "refine")}
    assert refined == {0, 3, 4, 5}                         # the four shown, never row 1
    assert seen[-1] == (14, 14)                             # 6 plans + 4 rows x 2 candidates
