"""POST /api/boardplan threads the two orthogonal resource toggles to the planner.

The board has two distinct resource axes and the page picks each independently:
  char_level: 'current' (real per-character level -> node points) or 'max' (level 50).
  resources : 'inventory' (the account's cube/hexa wallet) or 'infinite' (the ceiling).

This mocks the (path-loaded) precompute module so the ~90s tree_optimise subprocess
never runs — the test only checks the server parses + validates both toggles and
passes them through to team_board_plan (defaults current/inventory).
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

import server as web_server

_TEAM = [
    "card-04001-5-uniq-0042-00", "card-00002-5-uniq-0001-00",
    "card-00034-5-uniq-0027-00", "card-00028-5-uniq-0023-00",
    "card-00031-5-uniq-0025-00", "card-00016-5-uniq-0014-00",
]


class _FakePrecompute:
    """Stands in for the path-loaded precompute_optimisations module."""

    last: dict = {}

    @staticmethod
    def team_board_plan(**kwargs):
        _FakePrecompute.last = dict(kwargs)
        return {
            "schema": "board-export/v0",
            "plan_included": True,
            "plan_char_level": kwargs.get("char_level"),
            "plan_resources": kwargs.get("resources"),
            "characters": [],
        }


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def web_base():
    port = _free_port()
    server = web_server.ThreadingServer(
        (web_server.BIND_HOST, port), web_server.Handler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _post(url: str, obj: dict) -> tuple[int, dict]:
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.mark.parametrize("char_level", ["current", "max"])
@pytest.mark.parametrize("resources", ["inventory", "infinite"])
def test_boardplan_threads_both_toggles(web_base, monkeypatch, char_level, resources):
    monkeypatch.setattr(web_server, "_load_precompute", lambda: _FakePrecompute)
    status, body = _post(
        web_base + "/api/boardplan",
        {"cards": _TEAM, "basis": "account",
         "char_level": char_level, "resources": resources},
    )
    assert status == 200, body
    assert _FakePrecompute.last.get("char_level") == char_level
    assert _FakePrecompute.last.get("resources") == resources
    assert body.get("plan_char_level") == char_level
    assert body.get("plan_resources") == resources


def test_boardplan_missing_solver_is_a_typed_503_with_a_hint(web_base, monkeypatch):
    """No highspy in the interpreter -> 503 {error, hint}, never a 500 the page
    relabels as "(~90s)" (REDESIGN-PLAN D4)."""
    class _NoSolver:
        @staticmethod
        def team_board_plan(**_kw):
            raise ModuleNotFoundError("No module named 'highspy'", name="highspy")

    monkeypatch.setattr(web_server, "_load_precompute", lambda: _NoSolver)
    status, body = _post(web_base + "/api/boardplan",
                         {"cards": _TEAM, "basis": "account", "engine": "solver"})
    assert status == 503
    assert "highspy" in body["error"]
    assert "pip install highspy" in body["hint"]


def test_boardplan_unknown_toggles_fall_back_to_defaults(web_base, monkeypatch):
    monkeypatch.setattr(web_server, "_load_precompute", lambda: _FakePrecompute)
    status, body = _post(
        web_base + "/api/boardplan",
        {"cards": _TEAM, "basis": "account",
         "char_level": "bogus", "resources": "bogus"},
    )
    assert status == 200, body
    assert _FakePrecompute.last.get("char_level") == "current"
    assert _FakePrecompute.last.get("resources") == "inventory"


def test_boardplan_absent_toggles_default_to_current_inventory(web_base, monkeypatch):
    monkeypatch.setattr(web_server, "_load_precompute", lambda: _FakePrecompute)
    status, body = _post(
        web_base + "/api/boardplan", {"cards": _TEAM, "basis": "account"}
    )
    assert status == 200, body
    assert _FakePrecompute.last.get("char_level") == "current"
    assert _FakePrecompute.last.get("resources") == "inventory"


@pytest.mark.parametrize("engine,expected", [
    ("solver", "solver"), ("beam", "beam"), ("bogus", "beam"), (None, "beam"),
])
def test_boardplan_threads_engine(web_base, monkeypatch, engine, expected):
    """The `engine` selector reaches team_board_plan: 'solver' (HiGHS in-process) or
    'beam' (the tree_optimise subprocess), garbage/absent -> 'beam'."""
    monkeypatch.setattr(web_server, "_load_precompute", lambda: _FakePrecompute)
    payload = {"cards": _TEAM, "basis": "account"}
    if engine is not None:
        payload["engine"] = engine
    status, body = _post(web_base + "/api/boardplan", payload)
    assert status == 200, body
    assert _FakePrecompute.last.get("engine") == expected


@pytest.mark.parametrize("given,expected", [(None, None), (0, 0), (12, 12), ("3", 3), (-4, 0)])
def test_boardplan_threads_the_reset_budget(web_base, monkeypatch, given, expected):
    """`max_resets` (owned nodes the plan may take off, 1,000 Hologold each)
    reaches team_board_plan; absent means a free rebuild."""
    monkeypatch.setattr(web_server, "_load_precompute", lambda: _FakePrecompute)
    payload = {"cards": _TEAM, "basis": "account", "engine": "solver"}
    if given is not None:
        payload["max_resets"] = given
    status, body = _post(web_base + "/api/boardplan", payload)
    assert status == 200, body
    assert _FakePrecompute.last.get("max_resets") == expected


def test_boardplan_rejects_a_reset_budget_that_is_not_a_number(web_base, monkeypatch):
    monkeypatch.setattr(web_server, "_load_precompute", lambda: _FakePrecompute)
    status, body = _post(web_base + "/api/boardplan",
                         {"cards": _TEAM, "basis": "account", "max_resets": "lots"})
    assert status == 400
    assert "max_resets" in body["error"]
