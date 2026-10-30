"""`POST /api/board-doc {board_state, cards, basis, budget}` — the board-export
doc for a team's characters with a GIVEN developed board overlaid (§2.5):
build + `_apply_plan` overlay + sockets from the given slots, and NO
optimisation call — the doc a row's board section fetches lazily.

Uses the maxed basis so no collection file is needed; skips when the
gitignored dumps are absent (the endpoint's own 503).

    python -m pytest tools/web/test_board_doc_route.py
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

import server as web_server

_TEAM = ["card-04001-5-uniq-0042-00", "card-00002-5-uniq-0001-00", "card-00034-5-uniq-0027-00",
         "card-00028-5-uniq-0023-00", "card-00031-5-uniq-0025-00", "card-00016-5-uniq-0014-00"]


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
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture(scope="module")
def developed(web_base):
    """A real developed board_state for _TEAM (flat 453), from the develop route."""
    status, body = _post(web_base + "/api/optimize-rerank",
                         {"basis": "maxed", "teams": [_TEAM], "budget": 453})
    if status == 503:
        pytest.skip("skilltree/master dumps absent (gitignored)")
    assert status == 200, body
    return body["ranking"][0]


def test_board_doc_needs_a_board_state(web_base):
    status, body = _post(web_base + "/api/board-doc", {"cards": _TEAM})
    assert status == 400 and "board_state" in body["error"]


def test_board_doc_overlays_the_given_board_without_optimising(web_base, developed, monkeypatch):
    """The doc's `plan.optimised_nodes` per character IS the given node set,
    to_buy/to_sell are the diff against the current board, sockets carry the
    given placements, and no optimiser ran (optimize_and_rerank is patched
    to explode)."""
    def explode(*a, **k):
        raise AssertionError("board-doc must not optimise")

    monkeypatch.setattr(web_server, "optimize_and_rerank", explode)
    status, doc = _post(web_base + "/api/board-doc", {
        "board_state": developed["board_state"], "cards": _TEAM, "basis": "maxed", "budget": 453})
    assert status == 200, doc
    assert doc["engine"] == "given" and doc["plan_included"] is True
    assert doc["budget"] == 453 and doc["budget_kind"] == "flat"
    chars = {c["chr"]: c for c in doc["characters"]}
    assert set(chars) == set(developed["board_state"])
    for chr_id, state in developed["board_state"].items():
        c = chars[chr_id]
        plan = c["plan"]
        assert plan["optimised_nodes"] == sorted(state["nodes"])
        owned = {n["id"] for n in c["nodes"] if n.get("state") == "owned"}
        assert set(plan["to_buy"]) == set(state["nodes"]) - owned
        assert set(plan["to_sell"]) == owned - set(state["nodes"])
        assert plan["point_budget"] == 453                 # labelled by the given kind
        assert plan["point_cost"] >= 0
        assert plan["slots"] == state["slots"]
        for sock in c.get("sockets", []):
            cid = state["slots"].get(sock["socket"])
            if cid:
                assert sock["recommended_card"] == cid
    assert isinstance(doc.get("branches"), dict)


def test_board_doc_real_budget_labels_the_granted_points(web_base, developed):
    status, doc = _post(web_base + "/api/board-doc", {
        "board_state": developed["board_state"], "cards": _TEAM, "basis": "maxed"})
    assert status == 200, doc
    assert doc["budget"] == "real" and doc["budget_kind"] == "real"
    for c in doc["characters"]:
        assert c["plan"]["point_budget"] == (c.get("points") or {}).get("granted")


def test_board_doc_serves_node_values_and_connect_boosts(web_base):
    """The page prints the game's numbers: every node with a `[value]` in its
    text carries `value`/`value_text`, a placed Connect lists the nodes it
    covers, and a covered node's `boosted_value` is the game's ceiled product
    (`board_values`). No optimiser, no team needed."""
    status, doc = _post(web_base + "/api/board-doc", {
        "board_state": {"chr-03001": {"nodes": [], "slots": {}}}, "cards": [], "basis": "account"})
    if status == 503:
        pytest.skip("skilltree/master dumps absent (gitignored)")
    assert status == 200, doc
    assert doc["connect_cards"], "the Connect catalogue is served"
    (c,) = doc["characters"]
    valued = [n for n in c["nodes"] if n.get("value") is not None]
    assert valued and all(n["value_text"] for n in valued)
    for n in c["nodes"]:
        if "[value" in (n.get("effect") or ""):
            assert n["value"] is not None, n["id"]
    for sock in c["sockets"]:
        con = sock.get("connect")
        if not sock.get("placed_card"):
            assert con is None
            continue
        assert con, f"{sock['socket']} holds {sock['placed_card']} but lists no range"
        assert con["covers"] and con["permil"] > 0
        for n in c["nodes"]:
            if n["id"] in con["covers"] and n["value"] is not None:
                assert n["boosted_value"] >= n["value"]
                assert sock["socket"] in n["boosted_by"]
