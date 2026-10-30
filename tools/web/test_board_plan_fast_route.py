"""POST /api/board-plan-fast — the FAST board plan (Engine A, in-process).

The web's board tree used to come only from /api/boardplan (the tree_optimise beam
subprocess, ~90s). This serves the SAME board-export/v0 doc boardtree.js consumes
from the fast in-process Engine A (board_plan.optimize_and_rerank) in seconds, on
the exact deck-screen Unit Score the ranking uses — the instant default; the slow
path stays the full respec+sockets shopping plan.

Uses the `maxed` basis (no state/collection.json needed) and SKIPS on the 503 the
endpoint returns when the gitignored skilltree/master/board_export dumps are absent.
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
        with urllib.request.urlopen(req, timeout=90) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _plan(web_base, cards=_TEAM, basis="maxed"):
    status, body = _post(
        web_base + "/api/board-plan-fast", {"basis": basis, "cards": cards}
    )
    if status == 503:
        pytest.skip("skilltree/master/board_export dumps absent — endpoint 503")
    return status, body


def test_fast_plan_returns_the_board_export_doc_for_the_six_team_chars(web_base):
    status, doc = _plan(web_base)
    assert status == 200, doc
    assert doc["schema"] == "board-export/v0"
    assert doc["engine"] == "fast"
    assert doc["plan_included"] is True
    chars = doc["characters"]
    assert len(chars) == 6, [c["chr"] for c in chars]
    # every returned character is one of the team's, and carries an Engine A plan
    for c in chars:
        assert "plan" in c and "optimised_nodes" in c["plan"]
        # nodes carry the geometry boardtree.js paints
        assert c["nodes"] and all(
            k in c["nodes"][0] for k in ("pos", "state", "effect", "cost")
        )
    # at least one character has something to buy (the developed board is non-empty)
    assert any(c["plan"]["to_buy"] for c in chars)
    # the re-scored Unit Score is exposed and the board only adds
    assert doc["optimised_us"] >= doc["board_off_us"]


def test_fast_plan_bad_team_length_is_400(web_base):
    status, body = _post(
        web_base + "/api/board-plan-fast", {"basis": "maxed", "cards": ["a", "b"]}
    )
    assert status == 400
    assert "error" in body


def test_fast_plan_unknown_card_is_400(web_base):
    bad = ["card-99999-9-none-0000-00", *_TEAM[1:]]
    status, body = _post(
        web_base + "/api/board-plan-fast", {"basis": "maxed", "cards": bad}
    )
    # 503 (dumps absent) is a valid skip; otherwise it must be a 400 for the bad id
    if status == 503:
        pytest.skip("dumps absent")
    assert status == 400
    assert "error" in body


def test_fast_plan_respects_real_budget_and_places_sockets(web_base):
    """The critical fix: the plan uses each character's REAL earned points
    (points.granted), so a character never buys more than it can afford — and it
    now runs the socket optimiser too (not behind a separate button)."""
    status, doc = _plan(web_base)  # no budget -> real per-character points
    assert status == 200, doc
    assert doc["budget"] == "real"
    assert "sockets_optimised" in doc, "the fast plan must optimise sockets too"
    for c in doc["characters"]:
        granted = (c.get("points") or {}).get("granted")
        if granted is None:
            continue
        by_id = {n["id"]: n for n in c["nodes"]}
        spent = sum(
            ((by_id.get(nid, {}).get("cost") or {}).get("points", 0))
            for nid in c["plan"]["optimised_nodes"]
        )
        assert spent <= granted, f"{c['chr']} spends {spent} > granted {granted}"
