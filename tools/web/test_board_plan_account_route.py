"""`POST /api/board-plan-account {cards, song?, assumptions, board_state?}` — the whole
account's planned Holomem Boards for a team (every holomem, not just the six on the team),
the doc the Optimizer's board viewer draws. Skips when the gitignored dumps are absent.
    python -m pytest tools/web/test_board_plan_account_route.py
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

import server as web_server

_TEAM = ["card-03001-5-uniq-0033-00", "card-00034-5-uniq-0027-00", "card-00031-5-uniq-0025-00",
         "card-00023-5-uniq-0019-00", "card-00030-4-cmmn-0000-00", "card-03009-4-cmmn-0000-00"]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def web_base():
    port = _free_port()
    server = web_server.ThreadingServer((web_server.BIND_HOST, port), web_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _post(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.mark.parametrize("body", [
    {"cards": _TEAM[:5], "assumptions": {"boards": "plan"}},
    {"cards": _TEAM, "assumptions": {"boards": "as_is"}},
    {"cards": _TEAM, "assumptions": {"boards": "rebuilt"}},
])
def test_bad_requests_are_refused(web_base, body):
    status, doc = _post(web_base + "/api/board-plan-account", body)
    assert status == 400 and doc.get("error")


def test_every_holomem_comes_back_with_the_plan_overlaid(web_base):
    status, doc = _post(web_base + "/api/board-plan-account",
                        {"cards": _TEAM, "song": "m0342", "assumptions": {"boards": "plan", "infinite": True}})
    if status == 503:
        pytest.skip("skilltree/master/board_export dumps absent")
    assert status == 200, doc
    chrs = {c["chr"] for c in doc["characters"]}
    team = {"chr-" + c.split("-")[1] for c in _TEAM}
    assert team <= chrs and len(chrs) > len(team)          # the bench too, not only the six
    assert set(doc["team_chrs"]) == team
    for c in doc["characters"]:
        plan = c.get("plan") or {}
        nodes = {n["id"] for n in c.get("nodes") or []}
        assert set(plan.get("to_buy") or []) <= nodes and set(plan.get("to_sell") or []) <= nodes
    assert set(doc["changed"]) <= chrs
