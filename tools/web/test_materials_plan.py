"""`materials_plan`'s worker side and its dispatch: a cancelled search
stops its plans, a plan the parent already holds is carried instead of solved again, a
time-limited plan is never carried, and a SystemExit inside a plan is a row note, not a
hung search. No solver, no pool: the plan itself is stubbed.

    python -m pytest tools/web/test_materials_plan.py
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import materials_plan
from hbuilder.solver import parallel_refine as pr


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


def test_only_a_plan_solved_to_optimality_is_carried():
    key = ("card-00001-5-a-0000-00", ("m",), "m0020", {})
    materials_plan._hold(materials_plan._plan_key(*key), (("stamp",), "plan", True))
    assert materials_plan.carried_plan(*key) is None
    materials_plan._hold(materials_plan._plan_key(*key), (("stamp",), "plan", False))
    assert materials_plan.carried_plan(*key) == (("stamp",), "plan", False)


def test_a_system_exit_in_a_plan_is_a_row_note(monkeypatch):
    def boom(*_a, **_k):
        raise SystemExit("missing dumps/board_state.json")
    monkeypatch.setattr(materials_plan, "plan_for", boom)
    out = materials_plan.plan_and_score((1, "card-00001-5-a-0000-00", ("m",), "m0020", None, {}))
    assert out[1] is None and out[3].startswith("SystemExit")
