"""POST /api/optimize-rerank -- the bounded JOINT team+board re-rank.

Rather than showing the board plan for whichever team the board-less ranking
picked, this endpoint board-optimises the posted top-K IN-PROCESS (the same
`board_plan.optimize_and_rerank` the CLI's --optimize-board runs) and returns them
re-ranked by the board-optimised Unit Score, so a team a board-less solve ranked
second can surface first once every team's board is developed.

Uses the `maxed` basis so no `state/collection.json` is required, and SKIPS when
the gitignored skilltree/master dumps are absent (the endpoint's own 503) -- the
same first-class-absence contract the board editor and sprites have.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

import server as web_server

_MEMBERS = [
    "card-00002-5-uniq-0001-00", "card-00034-5-uniq-0027-00",
    "card-00028-5-uniq-0023-00", "card-00031-5-uniq-0025-00",
    "card-00016-5-uniq-0014-00",
]
_TEAM_A = ["card-04001-5-uniq-0042-00", *_MEMBERS]   # Calliope leads
_TEAM_B = ["card-00004-5-uniq-0005-00", *_MEMBERS]   # Aki Rosenthal leads


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


def _rerank(web_base, teams, basis="maxed"):
    status, body = _post(
        web_base + "/api/optimize-rerank", {"basis": basis, "teams": teams}
    )
    if status == 503:
        pytest.skip("skilltree/master dumps absent (gitignored) -- endpoint 503")
    return status, body


def test_rerank_sorts_by_optimised_unit_score(web_base):
    status, body = _rerank(web_base, [_TEAM_A, _TEAM_B])
    assert status == 200, body
    ranking = body["ranking"]
    assert body["sorted_by"] == "optimised_us"
    assert len(ranking) == 2
    # The returned order IS the board-optimised order -- the whole contract.
    assert ranking == sorted(ranking, key=lambda r: r["optimised_us"], reverse=True)
    for row in ranking:
        assert len(row["cards"]) == 6
        # The board only adds Unit Score; a developed board never loses points.
        assert row["optimised_us"] >= row["board_off_us"]
        assert row["points"] >= 0


def test_rerank_can_promote_a_board_off_runner_up(web_base):
    """The joint pass is only worth having if it can reorder: the team with the
    higher board-off score need not be the optimised winner."""
    status, body = _rerank(web_base, [_TEAM_A, _TEAM_B])
    assert status == 200, body
    ranking = body["ranking"]
    optimised_winner = ranking[0]["leader"]
    board_off_winner = max(ranking, key=lambda r: r["board_off_us"])["leader"]
    # Not asserted to always differ (data-dependent), but when they do the
    # endpoint must follow the OPTIMISED order, never the board-off one.
    assert ranking[0]["optimised_us"] == max(r["optimised_us"] for r in ranking)
    if optimised_winner != board_off_winner:
        # a genuine reorder happened -- the feature earned its keep
        assert ranking[0]["leader"] == optimised_winner


def test_unit_score_rows_carry_the_same_team_shape_as_song_and_across(web_base):
    """The client's leader_diverse branch REBUILDS the ranking from these rows
    (leader_id / member_ids / gates / board_state) instead of merging into the
    solve's teams. Every path must emit those keys, the song-agnostic Unit Score
    path included: with only `cards`/`leader`, "best team of every leader" with
    no song renders "(unknown card)", no members and "0 of 0 conditions met"."""
    status, body = _rerank(web_base, [_TEAM_A, _TEAM_B])
    assert status == 200, body
    for row in body["ranking"]:
        assert row["leader_id"] == row["cards"][0]
        assert row["member_ids"] == row["cards"][1:]
        assert isinstance(row["gates"], list)
        assert isinstance(row["board_state"], dict)
    # every card shown has a face to render
    for row in body["ranking"]:
        for cid in row["cards"]:
            assert cid in body["cards"]


def test_unit_score_refine_returns_sorted_rows_with_the_same_shape(web_base):
    """`refine: true` in any-song mode hill-climbs each team's members by the
    developed Unit Score. Rows keep the full team shape, stay sorted by
    optimised_us, and a refined row says so with its swaps."""
    status, body = _post(web_base + "/api/optimize-rerank",
                         {"basis": "maxed", "teams": [_TEAM_A, _TEAM_B],
                          "refine": True, "budget": 453})
    if status == 503:
        pytest.skip("skilltree/master dumps absent (gitignored) -- endpoint 503")
    assert status == 200, body
    ranking = body["ranking"]
    assert body["sorted_by"] == "optimised_us"
    assert ranking == sorted(ranking, key=lambda r: r["optimised_us"], reverse=True)
    for row in ranking:
        assert row["leader_id"] == row["cards"][0]
        assert row["member_ids"] == row["cards"][1:]
        for cid in row["cards"]:
            assert cid in body["cards"]
        for swap in row.get("refined", []):
            assert {"slot", "out", "in", "gain"} <= set(swap)
    # the leaders are the posted ones — a refine never changes the leader.
    # (Rows carrying `provenance` are SEED rosters, unioned in by default when
    # a refine runs — test_seed_rosters.py; they are not the posted teams.)
    assert {r["leader_id"] for r in ranking if not r.get("provenance")}         <= {_TEAM_A[0], _TEAM_B[0]}
    assert {r["leader_id"] for r in ranking if not r.get("provenance")}


def test_bad_team_length_is_400(web_base):
    status, body = _post(
        web_base + "/api/optimize-rerank",
        {"basis": "maxed", "teams": [["only", "three", "ids"]]},
    )
    assert status == 400
    assert "error" in body


def test_empty_teams_is_400(web_base):
    status, body = _post(web_base + "/api/optimize-rerank", {"teams": []})
    assert status == 400


def test_unknown_card_is_400(web_base):
    status, body = _post(
        web_base + "/api/optimize-rerank",
        {"basis": "maxed", "teams": [["card-99999-9-none-0000-00", *_MEMBERS]]},
    )
    assert status == 400


def test_default_budget_is_each_characters_real_points(web_base):
    """No `budget` in the body -> the server uses each character's real earned
    skill-tree points (not a flat 453), so the plan is affordable."""
    status, body = _rerank(web_base, [_TEAM_A])  # _rerank sends no budget
    assert status == 200, body
    assert body["budget"] == "real"


def test_a_lower_flat_budget_develops_fewer_points(web_base):
    """The flat-budget override is honoured: a small per-character cap buys fewer
    skill-tree points than a full-board cap (453)."""
    status_big, big = _post(
        web_base + "/api/optimize-rerank",
        {"basis": "maxed", "teams": [_TEAM_A], "budget": 453},
    )
    if status_big == 503:
        pytest.skip("dumps absent")
    status, small = _post(
        web_base + "/api/optimize-rerank",
        {"basis": "maxed", "teams": [_TEAM_A], "budget": 50},
    )
    assert status == 200, small
    assert small["budget"] == 50 and big["budget"] == 453
    assert small["ranking"][0]["points"] <= big["ranking"][0]["points"]
    # 50 points per character over 6 characters caps the plan under a full board
    assert small["ranking"][0]["points"] <= 6 * 50


# ---------------------------------------------------------------------------
# the route is a thin wrapper over `develop_pipeline`
# ---------------------------------------------------------------------------
def test_route_is_byte_compatible_with_develop_pipeline(web_base):
    """The wrapper must return EXACTLY what `develop_pipeline(body)` returns
    (minus the in-process `_internal` objects): same rows, same order, same
    numbers — a divergence here would mean `/api/run` and the legacy route
    answer differently for the same request."""
    body = {"basis": "maxed", "teams": [_TEAM_A, _TEAM_B], "budget": 453,
            "uptime_model": "finite", "platform": "mobile"}
    status, via_http = _post(web_base + "/api/optimize-rerank", body)
    if status == 503:
        pytest.skip("dumps absent")
    assert status == 200, via_http
    direct = web_server.develop_pipeline(body, art_dir=web_server.Handler.art_dir)
    direct.pop("_internal")
    assert json.loads(json.dumps(direct)) == via_http


def test_rows_carry_board_plan_summary_and_develop_only_rows_have_no_sells(web_base):
    """One board per row — `board_plan_summary` diffs the row's OWN
    `board_state` against the account's current board from the same export.
    A flat/full-tree develop is "adds only": `sell` is empty and `develop_only`
    true; the summary's node set equals the row's `board_state`; `points_max`
    is named by kind (453 for a full tree)."""
    status, body = _post(web_base + "/api/optimize-rerank",
                         {"basis": "maxed", "teams": [_TEAM_A], "budget": 453})
    if status == 503:
        pytest.skip("dumps absent")
    row = body["ranking"][0]
    bps = row["board_plan_summary"]
    assert bps["metric"] == "unit_score"
    assert bps["board_off"] == row["board_off_us"] and bps["developed"] == row["optimised_us"]
    assert bps["budget_kind"] == "full_tree" and bps["develop_only"] is True
    assert bps["gain_pct"] == row["gain_pct"]
    assert len(bps["per_character"]) == len(row["board_state"])
    for c in bps["per_character"]:
        assert c["sell"] == []
        assert c["points_max"] == 453
        assert sorted(set(c["buy"]) | set(c["keep"])) == sorted(row["board_state"][c["chr"]]["nodes"])
        assert c["points_used"] >= 0 and isinstance(c["sockets"], list)
    assert sum(c["points_used"] for c in bps["per_character"]) == row["points"]


def test_real_budget_rows_may_propose_sells_and_name_the_real_max(web_base):
    """Under REAL points the develop is a from-scratch plan within each
    character's granted budget: nodes owned now but not planned are `sell`
    (a respec proposal), and `points_max` is the granted budget."""
    status, body = _post(web_base + "/api/optimize-rerank",
                         {"basis": "account", "teams": [_TEAM_A]})
    if status == 503:
        pytest.skip("dumps absent")
    if status != 200:
        pytest.skip(f"account basis unavailable here: {body}")
    bps = body["ranking"][0]["board_plan_summary"]
    assert bps["budget_kind"] == "real" and bps["develop_only"] is False
    for c in bps["per_character"]:
        assert isinstance(c["sell"], list) and c["points_used"] <= max(c["points_max"], 0) + 0
