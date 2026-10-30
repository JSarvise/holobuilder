"""ONE number set per row (row_detail.py, BACKEND-GAPS.md §1-3).

The property under test: a ranked row's detail views — /api/compare with the
row's `board_state`, /api/timeline with it, /api/row-detail for a two-stage row
— return the ROW'S OWN figures (estimate, ceiling, slot order, deck power), and
the Score Bonus split / run law / grade thresholds are served. Real-data cases
use the maxed basis and SKIP when the gitignored dumps are absent; the pure
helpers run on synthetic inputs. Writes nothing (test_no_writes.py)."""
from __future__ import annotations

import json
import socket
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import event_mode
import row_detail
import server as web_server

SONG = "m0334"
_MEMBERS = [
    "card-04001-5-uniq-0042-00", "card-00032-5-uniq-0026-00",
    "card-04003-5-uniq-0044-00", "card-00007-5-uniq-0008-00",
    "card-00006-5-uniq-0007-00",
]
_TEAM = ["card-04001-5-uniq-0042-00", *_MEMBERS]


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


def _req(url: str, obj: dict | None = None) -> tuple[int, dict]:
    data = None if obj is None else json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="GET" if obj is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _skip_without_dumps(status: int, doc: dict) -> None:
    if status == 503 or (status == 400 and "chart" in str(doc.get("error", ""))):
        pytest.skip(f"dumps absent: {doc.get('error')}")


# -- the pure helpers -----------------------------------------------------------
def test_modal_order_is_the_order_best_on_most_charts():
    a, b = ("x", "y", "z", "u", "v"), ("y", "x", "z", "u", "v")
    rows = [types.SimpleNamespace(member_order=o) for o in (a, b, a, a)]
    order, share = row_detail.modal_order(rows)
    assert order == list(a) and share == 0.75
    assert row_detail.modal_order([]) == (None, None)


def _put(path: Path, obj) -> None:
    # through the builder's own writer (tools/recon/proxy), as
    # test_two_stage_view does: nothing in tools/web writes to disk
    import two_stage_build  # noqa: PLC0415

    two_stage_build._write(path, obj)


def test_ranks_are_read_from_the_master_tables(tmp_path):
    holo = tmp_path
    _put(holo / "main_LiveDeckEvaluationRank.json", [
        {"type": 1, "plus": 0, "data": {"type": "LiveDeckRankType_LIVE_DECK_RANK_TYPE_D", "order": 1}},
        {"type": 5, "plus": 2, "data": {"type": "LiveDeckRankType_LIVE_DECK_RANK_TYPE_S", "plus": 2,
                                         "threshold": "1500000", "order": 11}},
    ])
    _put(holo / "main_LiveScoreEvaluationRank.json", [
        {"group_id": "live_score_rank-s001", "plus": 0, "data": {
            "evaluationRankType": "LiveEvaluationRankType_LIVE_EVALUATION_RANK_TYPE_A", "score": "600000"}},
        {"group_id": "live_score_rank-s001", "plus": 1, "data": {
            "evaluationRankType": "LiveEvaluationRankType_LIVE_EVALUATION_RANK_TYPE_S", "plus": 1,
            "score": "1200000"}},
    ])
    _put(holo / "main_Music.json", [
        {"id": "m0001", "data": {"id": "m0001", "singleLiveScoreEvaluationRankGroupId": "live_score_rank-s001"}},
        {"id": "m0002", "data": {"id": "m0002", "singleLiveScoreEvaluationRankGroupId": "live_score_rank-s001"}},
        {"id": "m0003", "data": {"id": "m0003", "singleLiveScoreEvaluationRankGroupId": "live_score_rank-s002"}},
    ])
    got = row_detail.load_ranks(holo)
    assert got["available"]
    assert [g["grade"] for g in got["unit_score"]] == ["D", "S+2"]
    assert got["unit_score"][1]["threshold"] == 1_500_000
    assert got["song"]["default_group"] == "live_score_rank-s001"
    assert [g["grade"] for g in got["song"]["groups"]["live_score_rank-s001"]] == ["A", "S+"]
    assert got["song"]["exceptions"] == {"m0003": "live_score_rank-s002"}
    assert row_detail.load_ranks(tmp_path / "nowhere")["available"] is False


def test_event_row_detail_fills_the_row_and_survives_a_failure():
    row = {"score": 1000}
    event_mode.add_row_detail(row, {}, "L", ["a"], detail_fn=lambda l, m: {
        "song_est": 1000.4, "song_max": 1200, "member_order": ["a"], "deck_power": 5.0,
        "board_state": {"chr-1": {"nodes": [], "slots": {}}},
        "panel": {"overall_power": 10, "unit_score": 99.5},
        "score_bonus": {"unit_score": 98, "score_bonus_permil": 7}})
    assert row["member_order"] == ["a"] and row["unit_score"] == 98 and row["detail_matches"]
    broken = {"score": 1}
    event_mode.add_row_detail(broken, {}, "L", [], detail_fn=lambda l, m: 1 / 0)
    assert "ZeroDivisionError" in broken["detail_error"]


def test_run_body_carries_materials_and_leader_options():
    opts = web_server._parse_run_body({"materials": "infinite", "leader_member": "Mori Calliope",
                                       "leader_card": "card-x", "no_leader_overlap": True})
    assert opts["materials"] == "infinite"
    assert opts["leader_member"] == "Mori Calliope" and opts["leader_card"] == "card-x"
    assert opts["no_leader_overlap"] is True
    plain = web_server._parse_run_body({})
    assert plain["materials"] is None and plain["no_leader_overlap"] is False


# -- the routes -------------------------------------------------------------------
def test_ranks_route(web_base):
    status, doc = _req(web_base + "/api/ranks")
    assert status == 200
    if not doc.get("available"):
        pytest.skip(doc.get("reason"))
    assert doc["unit_score"] and doc["song"]["groups"]


def test_row_detail_route_rejects_a_bad_body(web_base):
    assert _req(web_base + "/api/row-detail", {"song": SONG, "cards": _TEAM[:3]})[0] == 400
    assert _req(web_base + "/api/row-detail", {"song": SONG, "cards": _TEAM,
                                               "basis": "nope"})[0] == 400


def test_compare_state_mode_needs_the_board_state(web_base):
    status, doc = _req(web_base + "/api/compare", {
        "teams": [_TEAM, _TEAM], "basis": "maxed", "board": "state", "song": SONG})
    _skip_without_dumps(status, doc)
    assert status == 400 and "board_state" in doc["error"]


def test_a_rows_board_gives_the_same_numbers_on_every_route(web_base):
    """The row (develop), the panel (/api/compare board "state") and the
    timeline (/api/timeline board_state) must be ONE number set."""
    status, dev = _req(web_base + "/api/compare", {
        "teams": [_TEAM, _TEAM], "basis": "maxed", "board": "develop", "song": SONG,
        "budget": 453})
    _skip_without_dumps(status, dev)
    assert status == 200, dev
    row = dev["teams"][0]
    state = row["board_state"]
    assert state
    status, cmp_ = _req(web_base + "/api/compare", {
        "teams": [{"cards": _TEAM, "board_state": state,
                   "member_order": row["score"]["member_order"]}] * 2,
        "basis": "maxed", "board": "state", "song": SONG, "budget": 453, "distribution": True})
    assert status == 200, cmp_
    got = cmp_["teams"][0]
    assert got["score"]["song_est"] == row["score"]["song_est"]
    assert got["score"]["song_max"] == row["score"]["song_max"]
    assert got["score"]["deck_power"] == row["score"]["deck_power"]
    assert got["score"]["member_order"] == row["score"]["member_order"]
    assert got["score"]["member_order_requested_matches"] is True
    split = got["score_bonus"]
    assert [l["key"] for l in split["lines"]] == ["active", "outfit", "passive", "board", "special"]
    assert split["score_bonus_permil"] == sum(l["permil"] for l in split["lines"])
    law = got["distribution"]
    assert law["available"] and law["verified"] is True
    assert law["min"] <= law["mean"] <= law["max"]

    status, tl = _req(web_base + "/api/timeline", {
        "song": SONG, "cards": _TEAM, "basis": "maxed", "board_state": state})
    assert status == 200 and tl["available"], tl
    assert tl["song_est"] == row["score"]["song_est"]
    assert tl["deck_power"] == row["score"]["deck_power"]
    assert tl["member_order"] == row["score"]["member_order"]
    assert tl["board_rebuild"]


def test_row_detail_route_on_a_two_stage_row(web_base):
    """A cached two-stage row re-developed on its own leaf: board, slot order,
    split and law from ONE board (the law reproduces the figure exactly)."""
    import two_stage_build as tsb

    cached = tsb._read(tsb.CACHE / tsb.song_file(SONG, "account")) or {}
    if not cached.get("rows"):
        pytest.skip("no cached account two-stage answer for m0334 (dumps gitignored)")
    row = cached["rows"][0]
    status, doc = _req(web_base + "/api/row-detail", {
        "song": SONG, "basis": "account", "cards": [row["leader"], *row["members"]],
        "distribution": True, "timeline": True})
    _skip_without_dumps(status, doc)
    assert status == 200, doc
    assert doc["refined"] == bool(row.get("refined"))
    assert sorted(doc["member_order"]) == sorted(row["members"])
    assert doc["board_state"] and doc["timeline"]["lanes"]
    assert doc["distribution"]["verified"] is True
    assert doc["song_max"] >= doc["song_est"]
    assert "row_matches" in doc          # false only when the cache is stale
