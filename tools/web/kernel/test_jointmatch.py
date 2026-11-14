"""`jointmatch.c` against `hbuilder.domain.joint_matching.JointMatching`: the same matching and
the same dual potentials, `==` (not approx), solved and re-solved with rows changed; and the
seam `board_plan.JOINT_MATCHING` that `enable()` sets."""

from __future__ import annotations

import random

import pytest

from hbuilder.domain.joint_matching import JointMatching

jointmatch = pytest.importorskip("jointmatch")


@pytest.fixture(scope="module", autouse=True)
def _built():
    try:
        jointmatch.load()
    except Exception as exc:  # noqa: BLE001 - no compiler here
        pytest.skip(f"the joint matching kernel cannot be built here: {exc!r}")


def _state(py: JointMatching):
    return py.u, py.v, py.col_of_row


def _instance(rng: random.Random, n: int, m: int):
    weights = [
        [rng.choice((0.0, round(rng.random() * 9, 6), rng.random() * 1e3, 1.0 + 1e-9)) for _ in range(m)]
        for _ in range(n)
    ]
    return weights, [rng.randint(1, 12) for _ in range(m)]


def test_the_compiled_matching_is_the_python_one_bit_for_bit():
    rng = random.Random(20261114)
    for _ in range(40):
        n, m = rng.randint(1, 220), rng.randint(1, 20)
        weights, caps = _instance(rng, n, m)
        py = JointMatching(weights, caps)
        c = jointmatch.CJointMatching(weights, caps)
        assert c.state() == _state(py)
        assert c.assignment() == py.assignment()
        for _team in range(4):
            rows = rng.sample(range(n), rng.randint(1, min(n, 24)))
            changes = {r: [rng.choice((0.0, rng.random() * 50)) for _ in range(m)] for r in rows}
            py_t, c_t = py.solve(changes), c.solve(changes)
            assert c_t.state() == _state(py_t)
            assert c_t.assignment() == py_t.assignment() == c.assignment_after(changes)
        # the base is left as it was
        assert c.state() == _state(py)


def test_enable_injects_the_compiled_class(monkeypatch):
    from hbuilder.domain import board_plan

    monkeypatch.setattr(board_plan, "JOINT_MATCHING", None)
    monkeypatch.setenv(board_plan.JOINT_MATCHING_ENV, "")  # restored (removed) at teardown
    assert board_plan._joint_matching_class() is JointMatching
    assert jointmatch.enable()
    assert board_plan._joint_matching_class() is jointmatch.CJointMatching
