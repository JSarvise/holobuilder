"""POST /api/compare -- two hand-picked teams scored side by side (compare_view.py).

Uses the `maxed` basis wherever the account file is not the thing under test,
and SKIPS (the endpoint's own 503) when the gitignored skilltree/master dumps
are absent -- the same first-class-absence contract the rerank route test has.
Writes nothing (tools/web/test_no_writes.py walks this file too).
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
    "card-04001-5-uniq-0042-00", "card-00032-5-uniq-0026-00",
    "card-04003-5-uniq-0044-00", "card-00007-5-uniq-0008-00",
    "card-00006-5-uniq-0007-00",
]
_TEAM_A = ["card-04001-5-uniq-0042-00", *_MEMBERS]   # Calliope PDW leads (also member 1)
_TEAM_B = ["card-04001-5-uniq-0071-00", *_MEMBERS]   # the other Calliope leads


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
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _compare(web_base, teams, **kw) -> tuple[int, dict]:
    body = {"teams": teams, "basis": "maxed", "board": "off",
            "platform": "mobile", "uptime_model": "finite"}
    body.update(kw)
    return _post(web_base + "/api/compare", body)


def _skip_if_dumps_missing(status: int, doc: dict) -> None:
    if status == 503:
        pytest.skip(f"develop dumps absent: {doc.get('error')}")


# -- malformed teams -> 400 ---------------------------------------------------
def test_400_on_four_members(web_base):
    status, doc = _compare(web_base, [_TEAM_A[:5], _TEAM_B])
    assert status == 400 and "team A" in doc["error"] and "6 card ids" in doc["error"]


def test_400_on_missing_card(web_base):
    bad = list(_TEAM_B)
    bad[3] = "card-99999-5-uniq-9999-00"
    status, doc = _compare(web_base, [_TEAM_A, bad])
    assert status == 400 and "unknown card id" in doc["error"] and "team B" in doc["error"]


def test_400_on_duplicate_holomem(web_base):
    # two Calliope cards as members: the same holomem twice is what Team refuses
    dup = ["card-00007-5-uniq-0008-00", "card-04001-5-uniq-0042-00",
           "card-04001-5-uniq-0071-00", *_MEMBERS[1:4]]
    status, doc = _compare(web_base, [_TEAM_A, dup])
    assert status == 400 and "invalid roster" in doc["error"]


def test_400_on_one_team(web_base):
    status, doc = _compare(web_base, [_TEAM_A])
    assert status == 400 and "exactly two" in doc["error"]


# -- the numbers ---------------------------------------------------------------
def test_identical_teams_have_zero_difference(web_base):
    status, doc = _compare(web_base, [_TEAM_A, _TEAM_A])
    assert status == 200, doc
    a, b = doc["teams"]
    assert a["score"]["headline"] == b["score"]["headline"] > 0
    assert doc["diff"]["score"]["delta"] == 0 and doc["diff"]["score"]["pct"] == 0
    assert doc["diff"]["sentence"].startswith("The two teams score identically")
    for key, d in doc["diff"]["panel"].items():
        assert d["delta"] == 0, key
    assert doc["mode"] == "unit_score"


def test_whatif_level_changes_the_score(web_base):
    base_status, base = _compare(web_base, [_TEAM_A, _TEAM_B], basis="account")
    assert base_status == 200, base
    lvl_status, low = _compare(web_base, [_TEAM_A, _TEAM_B], basis="account",
                               assume_level=20, assume_bloom=0)
    assert lvl_status == 200, low
    assert low["assumptions"]["assume_level"] == 20
    assert low["teams"][0]["score"]["headline"] < base["teams"][0]["score"]["headline"]
    assert low["teams"][1]["score"]["headline"] < base["teams"][1]["score"]["headline"]


def test_per_card_override_changes_only_that_card(web_base):
    over = {"cards": _TEAM_A, "overrides": [None, None, {"level": 20, "bloom": 0},
                                              None, None, None]}
    status, doc = _compare(web_base, [_TEAM_A, over])
    assert status == 200, doc
    a, b = doc["teams"]
    # only slot 2 carries a resolved override; the others are untouched
    assert [s["override"] is not None for s in b["slots"]] == [False, False, True, False, False, False]
    assert b["slots"][2]["level"] == 20 and b["slots"][2]["bloom"] == 0
    assert all(s["override"] is None for s in a["slots"])
    # the overridden member's own PASSIVE row moves; every other card's rows do not
    target = _TEAM_A[2]
    for row in doc["diff"]["skills"]:
        if row["a"] is None or row["b"] is None:
            continue
        if row["card_id"] != target and row["slot_kind"] != "costume":
            # a stat_up on another card can still change if it TARGETS the overridden
            # card's stats -- rates are per-skill, not per-target, so they hold
            assert row["a"]["weighted_rate"] == pytest.approx(row["b"]["weighted_rate"]), row
    assert b["score"]["headline"] < a["score"]["headline"]
    # Member Parameter is the five members' summed stats: only one card dropped
    assert doc["diff"]["panel"]["member_parameter"]["delta"] < 0
    # overriding BOTH teams the same way restores Δ 0
    status2, doc2 = _compare(web_base, [over, over])
    assert status2 == 200 and doc2["diff"]["score"]["delta"] == 0


def test_board_off_vs_develop_differ(web_base):
    off_status, off = _compare(web_base, [_TEAM_A, _TEAM_B], song="m0334")
    assert off_status == 200, off
    dev_status, dev = _compare(web_base, [_TEAM_A, _TEAM_B], song="m0334",
                               board="develop", budget=453)
    _skip_if_dumps_missing(dev_status, dev)
    assert dev_status == 200, dev
    assert dev["mode"] == off["mode"] == "song"
    for i in range(2):
        assert dev["teams"][i]["score"]["headline"] > off["teams"][i]["score"]["headline"]
        assert dev["teams"][i]["board_plan_summary"]["per_character"]
        assert dev["teams"][i]["board_state"]
        assert "board_plan_summary" not in off["teams"][i]
    assert dev["assumptions"]["board"] == "develop" and dev["assumptions"]["budget"] == 453
    assert off["certificate_basis"]["budget_kind"] == "none"
    assert dev["certificate_basis"]["budget_kind"] == "flat"


def test_certificate_fields_present(web_base):
    status, doc = _compare(web_base, [_TEAM_A, _TEAM_B], song="m0334")
    assert status == 200, doc
    assert doc["model_version"] and "inputs_hash" in doc
    assert doc["certificate_basis"]["platform"] == "mobile"
    for team in doc["teams"]:
        cert = team["certificate"]
        assert cert["status"] in ("EXACT", "BOUNDED", "HEURISTIC", "ERROR")
        assert cert["model_version"] == doc["model_version"]
        assert cert["basis"] == doc["certificate_basis"]
    # song mode carries the per-song trio and the diff names its driver
    a = doc["teams"][0]["score"]
    assert a["song_off"] <= a["song_est"] <= a["song_max"]
    assert doc["diff"]["sentence"]
