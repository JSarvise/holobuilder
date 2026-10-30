"""`POST /api/run` — the ONE job (REDESIGN-PLAN.md Phase B): search →
(every leader) → develop → (refine) → rank, one SSE stream, one `result`.

* routes: 202 with `job_id` + `plan`; the event stream is ordered
  plan → stage(search) → … → stage(rank done) → result → end, and NOTHING
  typed `result` is emitted before the develop stage is done;
* cancel between teams: a fake pipeline that honours the shared cancel flag
  between teams makes the stream end with `cancelled{stage:'develop'}` and no
  `result`;
* an error inside develop is `error{stage:'develop'}` and no `result`;
* the final payload's `explain` is keyed by roster for EVERY team, and it is
  computed for the FINAL roster on its developed board (`board:"developed"`)
  — the real pipeline on two real rosters (maxed basis, no collection file
  needed), stage 1 stubbed to those two rosters so the run is seconds;
* `warnings[]` and `notice[]` are present on the result;
* `board:'off'` runs no develop and still answers with `explain` by roster;
* the `/api/run` result rows carry the Part-1 certificate fields.

    python -m pytest tools/web/test_run_route.py
"""
from __future__ import annotations

import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import certindex
import server as web_server
from hbuilder.domain.team import Team
from hbuilder.solver.exact import RankedTeam

CALLI_PDW = "card-04001-5-uniq-0042-00"
ROSTER_A = [CALLI_PDW, "card-00002-5-uniq-0001-00", "card-00034-5-uniq-0027-00",
            "card-00028-5-uniq-0023-00", "card-00031-5-uniq-0025-00", "card-00016-5-uniq-0014-00"]
ROSTER_B = ["card-00004-5-uniq-0005-00", "card-00002-5-uniq-0001-00", "card-00034-5-uniq-0027-00",
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


def _events(url, on_event=None, timeout=240):
    """Read the SSE stream to `end`; `on_event(ev)` may act (e.g. cancel)."""
    out = []
    with urllib.request.urlopen(url, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8").rstrip("\n")
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            out.append(ev)
            if on_event:
                on_event(ev)
            if ev.get("type") == "end":
                break
    return out


def _stub_exact(rosters):
    def solve_exact(pool, cfg, *, top_n, progress=None, constraint=None,
                    set_constraint=None, locked_ids=(), leader_id=None):
        by_id = {c.id: c for c in pool}
        out = []
        for ids in rosters:
            team = Team(leader=by_id[ids[0]], members=tuple(by_id[c] for c in ids[1:]))
            out.append(RankedTeam(score=float(100 - len(out)), leader_id=ids[0],
                                  member_ids=tuple(sorted(ids[1:])), team=team))
        return tuple(out)
    return solve_exact


def _stub_stage1(monkeypatch, rosters):
    monkeypatch.setattr(web_server, "should_use_heuristic", lambda *a, **k: False)
    monkeypatch.setattr(web_server, "solve_exact", _stub_exact(rosters))
    monkeypatch.setattr(certindex, "_INDEX", certindex.CertificateIndex(table_roots=(), cert_roots=()))


def _fake_pipeline(rows_per_team=1, fail=False, sleep=0.0):
    """A pipeline double: emits stage/progress per team, honours cancel BETWEEN
    teams, returns rows in the song shape."""
    def pipeline(body, *, art_dir, emit=None, cancel=None):
        teams = body.get("teams") or []
        emit({"type": "stage", "stage": "develop", "state": "active", "total": len(teams)})
        rows = []
        for k, ids in enumerate(teams, start=1):
            if fail:
                raise web_server._HttpError(500, {"error": "boom in develop"})
            time.sleep(sleep)                       # "developing" team k
            if cancel is not None and cancel.is_set():
                raise web_server._Cancelled()       # honoured between teams
            emit({"type": "progress", "stage": "develop", "done": k, "total": len(teams)})
            rows.append({"leader_id": ids[0], "member_ids": ids[1:], "cards": ids,
                         "leader": ids[0], "gates": [], "board_state": {},
                         "song_est": 1000.0 - k, "song_off": 900.0, "song_max": 1100.0,
                         "member_order": ids[1:], "points": 0, "gain_pct": 11.1,
                         "certificate": {"status": "HEURISTIC"},
                         "board_plan_summary": {"per_character": []}})
        return {"ranking": rows, "sorted_by": "song_est", "cards": {}, "song": body.get("song"),
                "basis": body.get("basis"), "budget": body.get("budget", "real"),
                "model_version": "test", "inputs_hash": "h" * 64,
                "certificate_basis": {"universe": "u"}, "_internal": {}}
    return pipeline


def _has_dumps():
    return web_server.SKILLTREE_PATH.is_file() and web_server.NODE_MASTER_PATH.is_file()


# ---------------------------------------------------------------------------
def test_run_routes_202_with_plan_and_streams_in_order(web_base, monkeypatch):
    _stub_stage1(monkeypatch, [ROSTER_A, ROSTER_B])
    monkeypatch.setattr(web_server, "develop_pipeline", _fake_pipeline())
    status, started = _post(web_base + "/api/run", {
        "basis": "maxed", "board": "develop", "budget": "full", "song": "m0334",
        "top": 5, "develop_top": 2, "uptime_model": "finite", "platform": "mobile"})
    assert status == 202, started
    assert started["job_id"] and [s["id"] for s in started["plan"]] == ["search", "develop", "rank"]
    assert started["opts"]["budget"] == 453 and started["opts"]["develop_top"] == 2
    events = _events(f"{web_base}/events/{started['job_id']}")
    types = [e["type"] for e in events]
    assert types[0] == "plan" and types[-1] == "end"
    assert types.count("result") == 1 and types[-2] == "result"
    # stage order: search active → search done → develop active → … → rank done → result
    stage_seq = [(e["stage"], e["state"]) for e in events if e["type"] == "stage"]
    assert stage_seq[0] == ("search", "active")
    assert ("search", "done") in stage_seq and ("develop", "active") in stage_seq
    assert stage_seq.index(("develop", "active")) > stage_seq.index(("search", "done"))
    assert stage_seq[-1] == ("rank", "done")
    assert types.index("result") > types.index("stage") and any(
        e["type"] == "progress" and e.get("stage") == "develop" for e in events)
    payload = next(e for e in events if e["type"] == "result")["payload"]
    assert payload["sorted_by"] == "song_score" and len(payload["teams"]) == 2
    assert payload["teams"][0]["song_score"] == 999.0 and payload["teams"][0]["rank"] == 1
    assert isinstance(payload["warnings"], list) and isinstance(payload["notice"], list)
    assert payload["model_version"] == "test" and payload["inputs_hash"] == "h" * 64
    assert payload["teams"][0]["certificate"] == {"status": "HEURISTIC"}
    assert payload["developed"]["funnel"] == 2 and payload["_developed"] is True


def test_cancel_between_teams_emits_cancelled_and_no_result(web_base, monkeypatch):
    _stub_stage1(monkeypatch, [ROSTER_A, ROSTER_B])
    monkeypatch.setattr(web_server, "develop_pipeline", _fake_pipeline(sleep=0.4))
    status, started = _post(web_base + "/api/run", {
        "basis": "maxed", "board": "develop", "song": "m0334", "top": 5, "develop_top": 2})
    assert status == 202
    jid = started["job_id"]

    def on_event(ev):
        if ev["type"] == "progress" and ev.get("stage") == "develop" and ev.get("done") == 1:
            _post(f"{web_base}/cancel/{jid}", {})

    events = _events(f"{web_base}/events/{jid}", on_event)
    types = [e["type"] for e in events]
    assert "result" not in types
    cancelled = next(e for e in events if e["type"] == "cancelled")
    assert cancelled["stage"] == "develop"
    assert types[-1] == "end"


def test_error_in_develop_is_typed_and_no_result(web_base, monkeypatch):
    _stub_stage1(monkeypatch, [ROSTER_A])
    monkeypatch.setattr(web_server, "develop_pipeline", _fake_pipeline(fail=True))
    status, started = _post(web_base + "/api/run", {
        "basis": "maxed", "board": "develop", "song": "m0334", "top": 5})
    events = _events(f"{web_base}/events/{started['job_id']}")
    types = [e["type"] for e in events]
    assert "result" not in types
    err = next(e for e in events if e["type"] == "error")
    assert err["stage"] == "develop" and "boom" in err["message"]
    assert types[-1] == "end"


def test_board_off_answers_without_develop_and_explains_by_roster(web_base, monkeypatch):
    _stub_stage1(monkeypatch, [ROSTER_A, ROSTER_B])
    called = {"n": 0}

    def no_pipeline(*a, **k):
        called["n"] += 1
        raise AssertionError("develop must not run with board=off")

    monkeypatch.setattr(web_server, "develop_pipeline", no_pipeline)
    status, started = _post(web_base + "/api/run", {
        "basis": "maxed", "board": "off", "song": "m0334", "top": 2})
    assert [s["id"] for s in started["plan"]] == ["search", "rank"]
    events = _events(f"{web_base}/events/{started['job_id']}")
    payload = next(e for e in events if e["type"] == "result")["payload"]
    assert called["n"] == 0
    for t in payload["teams"]:
        key = "|".join([t["leader_id"], *sorted(t["member_ids"])])
        assert key in payload["explain"], list(payload["explain"])
        assert payload["explain"][key]["lines"]
    assert payload["certificate_basis"]["budget_kind"] == "none"


@pytest.mark.skipif(not _has_dumps(), reason="skilltree/master dumps absent (gitignored)")
def test_real_pipeline_final_rosters_carry_explain_on_their_developed_board(web_base, monkeypatch):
    """The real develop on two real rosters (maxed, flat 453): every final team
    has `explain[roster]` computed for THAT roster with `board:"developed"`,
    `board_plan_summary` per row, and the certificate fields."""
    _stub_stage1(monkeypatch, [ROSTER_A, ROSTER_B])
    status, started = _post(web_base + "/api/run", {
        "basis": "maxed", "board": "develop", "budget": "full", "song": "m0334",
        "top": 5, "develop_top": 2, "uptime_model": "finite", "platform": "mobile",
        "pool": "5star"})
    assert status == 202, started
    events = _events(f"{web_base}/events/{started['job_id']}")
    types = [e["type"] for e in events]
    assert types.count("result") == 1, [e for e in events if e["type"] == "error"]
    payload = next(e for e in events if e["type"] == "result")["payload"]
    assert len(payload["teams"]) == 2 and payload["sorted_by"] == "song_score"
    for t in payload["teams"]:
        key = "|".join([t["leader_id"], *sorted(t["member_ids"])])
        ex = payload["explain"][key]
        assert ex["board"] == "developed" and ex["lines"] and ex["terms"] and ex["contributions"]
        assert ex["lines"][0].startswith(f"RANK {t['rank']}: LEADER {t['leader_id']}")
        bps = t["board_plan_summary"]
        assert bps["budget_kind"] == "full_tree" and bps["develop_only"] is True
        assert bps["per_character"] and all(c["sell"] == [] for c in bps["per_character"])
        # same instant, same basis: the section's node set IS the row's board_state
        for c in bps["per_character"]:
            planned = sorted(set(c["buy"]) | set(c["keep"]))
            assert planned == sorted(t["board_state"][c["chr"]]["nodes"])
            assert c["points_max"] == 453
        assert t["certificate"]["status"] in ("EXACT", "BOUNDED", "HEURISTIC")
        assert t["certificate"]["basis"]["universe"] == "5star-maxed-catalogue"
    assert payload["model_version"] and "inputs_hash" in payload
    assert payload["explain_by_rank"] == []      # stage-1 attribution never survives
