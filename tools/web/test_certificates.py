"""Certificates on every song-mode optimize response.

What is asserted, and why each assertion is not circular:

* a song-mode `/solve` payload carries `model_version` (the ONE constant,
  `hbuilder.domain.model_version.MODEL_VERSION`), `inputs_hash` and a
  `certificate_basis`; every row carries a `certificate` whose status is
  HEURISTIC when nothing on file matches — and it names the generator and N;
* an index seeded with one EXACT and one BOUNDED row (no filesystem: the
  package's no-writes guard forbids fixture files, so the rows are injected
  through a `CertificateIndex` subclass) upgrades the matching rows to the
  right status ONLY when the hash AND the basis tuple match; a hash mismatch
  leaves the row HEURISTIC with a `certificate_note` naming the difference —
  the "same instant, same basis" guard;
* a row that scores ABOVE an EXACT certificate on the same hash is reported
  as status "ERROR" (refined > exact on the same hash is a bug), never
  as a better team;
* the GUARD test: the server's `inputs_hash` for (Cards = maxed, uptime
  finite, platform mobile, song m0334) equals the hash the per-leader-table
  probe computes for the same basis (`exhaustive_song_leader.load_context` +
  platform — the probe's own expression, imported, not re-typed). If the two
  ever differ the failure message prints the per-file digests so the differing
  field is visible, not guessed.
* the real PHASE-5 certificate directory parses through the index (read-only)
  and the m0334 / Calli PDW EXACT row is found by (song, leader).

    python -m pytest tools/web/test_certificates.py
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import queue
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import certificate as cert_mod
import certindex
import server as web_server
from hbuilder.domain.card import Stats
from hbuilder.domain.model_version import MODEL_VERSION
from hbuilder.domain.song import charts_from_mapping
from hbuilder.solver.exact import RankedTeam
from hbuilder.domain.team import Team

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from conftest import make_card, make_scoring_config, make_skill  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
RESEARCH = REPO / "tools/recon/RESEARCH-2026-08-18"
PHASE5 = RESEARCH / "PHASE-5"
MUSIC_ID = "m9991"
CALLI_PDW = "card-04001-5-uniq-0042-00"
M1_ROSTER = [CALLI_PDW, CALLI_PDW, "card-00032-5-uniq-0026-00",
             "card-04003-5-uniq-0044-00", "card-00007-5-uniq-0008-00",
             "card-00006-5-uniq-0007-00"]


# ---------------------------------------------------------------------------
# synthetic fixtures (test_song_solve.py's shape)
# ---------------------------------------------------------------------------
def _chart_library():
    document = {
        "schema_version": "charts-1",
        "charts": {
            f"{MUSIC_ID}/EXPERT": {
                "music_id": MUSIC_ID, "difficulty": "EXPERT", "difficulty_level": 26,
                "live_score_coefficient_permil": 4,
                "notes": [[float(t), 1000] for t in range(1, 31)],
                "special_cues": [5.0, 10.0, 15.0, 20.0, 25.0],
            }
        },
    }
    return charts_from_mapping(document, source="test_certificates")


def _pool(size: int = 8):
    return tuple(
        make_card(
            f"card_{i}", member=f"Holomem{i}",
            stats=Stats(performance=100 + 13 * i, technique=90 + 7 * i, sense=80 + 3 * i),
            costume_skill=make_skill("COSTUME", family="stat_up", effect_type="all_stats",
                                     magnitude=5.0 + 3.0 * i),
        )
        for i in range(size)
    )


class _SeededIndex(certindex.CertificateIndex):
    """An index whose rows are injected, not read from disk."""

    def __init__(self, rows):
        super().__init__(table_roots=(), cert_roots=())
        self.rows = list(rows)

    def entries(self, song=None, leader=None):
        return [r for r in self.rows
                if (song is None or r["song"] == song)
                and (leader is None or r["leader"] == leader)]


def _run(monkeypatch, tmp_path, *, song=MUSIC_ID, top=3, index=None, basis="account",
         pool="all"):
    cards = _pool()
    config = make_scoring_config()
    monkeypatch.setattr(web_server, "load_inputs", lambda *a, **k: (cards, cards, config, None))
    monkeypatch.setattr(web_server, "BOARD_DISABLED", True)
    monkeypatch.setattr(web_server, "COLLECTION_PATH", tmp_path / "no-such-collection.json")
    monkeypatch.setattr(web_server, "SONG_TITLES_PATH", tmp_path / "no-such-titles.json")
    monkeypatch.setattr(web_server, "load_charts", lambda _p: _chart_library())
    monkeypatch.setattr(certindex, "_INDEX", index if index is not None else _SeededIndex([]))
    job = web_server.Job(job_id="test-cert")
    web_server.run_solve_job(job, top_n=top, art_dir=Path("does-not-exist"), song=song,
                             basis=basis, pool_filter=pool, uptime_model="finite",
                             platform="mobile")
    events = []
    while True:
        try:
            events.append(job.events.get_nowait())
        except queue.Empty:
            break
    results = [e for e in events if e.get("type") == "result"]
    assert results, f"no result event; got {[e.get('type') for e in events]}"
    return results[-1]["payload"]


def _entry(song, leader, ids, score, status, inputs_hash, basis, **over):
    e = {
        "song": song, "leader": leader, "leader_name": leader, "status": status,
        "score": score, "ids": sorted(ids), "members": ids, "leader_also_member": False,
        "upper_bound": score if status == "EXACT" else score * 1.01,
        "gap": 0.0 if status == "EXACT" else 0.01,
        "leaves_evaluated": 123, "n_rosters": 456, "seconds": 7.5,
        "inputs_hash": inputs_hash, "model_version": MODEL_VERSION,
        "git_head": "abc", "git_dirty": False, "written": "2026-08-18T12:00:00",
        "certificate": "fixture/certificate.json", "source": "fixture/table.json",
        "source_kind": "table",
        **basis,
    }
    e.update(over)
    return e


# ---------------------------------------------------------------------------
# 1. the response carries the fields, HEURISTIC by default
# ---------------------------------------------------------------------------
def test_song_response_carries_model_version_hash_and_heuristic_rows(monkeypatch, tmp_path):
    payload = _run(monkeypatch, tmp_path)
    assert payload["model_version"] == MODEL_VERSION
    assert "inputs_hash" in payload
    cb = payload["certificate_basis"]
    assert cb == {"universe": "account", "basis": "account", "budget_kind": "none",
                  "budget": None, "uptime_model": "finite", "platform": "mobile"}
    for team in payload["teams"]:
        cert = team["certificate"]
        assert cert["status"] == "HEURISTIC"
        assert cert["model_version"] == MODEL_VERSION
        assert cert["inputs_hash"] == payload["inputs_hash"]
        assert cert["basis"] == cb
        assert "candidate_generator" in cert and cert["stages"]
        assert isinstance(cert["candidates_evaluated"], int)
        assert "certificate_note" not in cert   # nothing on file: no note, no gap


def test_agnostic_payload_carries_no_certificate_fields(monkeypatch, tmp_path):
    """The song-agnostic payload is the CLI's `--json` contract; it must stay
    byte-identical (no `certificate` on its teams, no top-level hash)."""
    payload = _run(monkeypatch, tmp_path, song="")
    assert "model_version" not in payload and "inputs_hash" not in payload
    assert all("certificate" not in t for t in payload["teams"])


# ---------------------------------------------------------------------------
# 2. the guard: hash + basis match -> EXACT / BOUNDED; mismatch -> HEURISTIC + note
# ---------------------------------------------------------------------------
def _seed_from(payload, statuses):
    """Rows for the payload's first teams with DISTINCT leaders (a certificate
    is per leader), on the payload's own hash/basis. Returns (rows, indices)."""
    rows, idx, seen = [], [], set()
    for i, team in enumerate(payload["teams"]):
        if len(rows) == len(statuses):
            break
        if team["leader_id"] in seen:
            continue
        seen.add(team["leader_id"])
        rows.append(_entry(MUSIC_ID, team["leader_id"], list(team["member_ids"]),
                           float(team["song_score"]), statuses[len(rows)],
                           payload["inputs_hash"], payload["certificate_basis"]))
        idx.append(i)
    assert len(rows) == len(statuses), "pool too small for distinct leaders"
    return rows, idx


def test_matching_hash_and_basis_upgrade_rows_to_exact_and_bounded(monkeypatch, tmp_path):
    first = _run(monkeypatch, tmp_path, top=30)
    if first["inputs_hash"] is None:
        pytest.skip(f"no inputs_hash on this checkout: {first.get('inputs_hash_error')}")
    rows, (i0, i1) = _seed_from(first, ["EXACT", "BOUNDED"])
    payload = _run(monkeypatch, tmp_path, index=_SeededIndex(rows), top=30)
    assert payload["inputs_hash"] == first["inputs_hash"], "hash is not deterministic"
    c0, c1 = payload["teams"][i0]["certificate"], payload["teams"][i1]["certificate"]
    others = [t["certificate"] for t in payload["teams"]
              if t["leader_id"] not in (rows[0]["leader"], rows[1]["leader"])]
    assert others, "need a third leader in the payload"
    c2 = others[0]
    assert c0["status"] == "EXACT"
    assert c0["is_certified_argmax"] is True
    assert c0["certified"]["file"] == "fixture/certificate.json"
    assert c0["gap"] == 0.0 and c0["n_rosters"] == 456 and c0["leaves_evaluated"] == 123
    assert c0["inputs_hash"] == payload["inputs_hash"]
    assert "every one of 456 rosters" in c0["meaning"]
    assert c1["status"] == "BOUNDED"
    assert c1["gap"] == pytest.approx(0.01) and c1["upper_bound"] > c1["score"]
    assert c2["status"] == "HEURISTIC" and "certificate_note" not in c2


def test_hash_mismatch_is_heuristic_with_a_note_never_a_gap(monkeypatch, tmp_path):
    first = _run(monkeypatch, tmp_path)
    if first["inputs_hash"] is None:
        pytest.skip("no inputs_hash on this checkout")
    rows, _ = _seed_from(first, ["EXACT"])
    rows[0]["inputs_hash"] = "3047" + "0" * 60      # a table on ANOTHER basis
    payload = _run(monkeypatch, tmp_path, index=_SeededIndex(rows))
    cert = payload["teams"][0]["certificate"]
    assert cert["status"] == "HEURISTIC"
    assert "gap" not in cert and "certified" not in cert
    note = cert["certificate_note"]
    assert "table exists" in note and "30470000" in note
    assert payload["inputs_hash"][:8] in note and "different basis" in note


@pytest.mark.parametrize("field,value", [
    ("budget_kind", "flat"), ("budget", 453), ("uptime_model", "steady"),
    ("platform", "pc"), ("universe", "5star-maxed-catalogue"),
])
def test_basis_field_mismatch_is_heuristic_with_the_field_named(monkeypatch, tmp_path,
                                                                 field, value):
    first = _run(monkeypatch, tmp_path)
    if first["inputs_hash"] is None:
        pytest.skip("no inputs_hash on this checkout")
    rows, _ = _seed_from(first, ["EXACT"])
    rows[0][field] = value
    payload = _run(monkeypatch, tmp_path, index=_SeededIndex(rows))
    cert = payload["teams"][0]["certificate"]
    assert cert["status"] == "HEURISTIC"
    assert field in cert["certificate_note"] and repr(value) in cert["certificate_note"]


def test_row_above_exact_certificate_on_same_hash_is_an_error(monkeypatch, tmp_path):
    first = _run(monkeypatch, tmp_path)
    if first["inputs_hash"] is None:
        pytest.skip("no inputs_hash on this checkout")
    rows, _ = _seed_from(first, ["EXACT"])
    # the "certificate" claims a maximum 1000 points BELOW what this row scores
    rows[0]["score"] = float(first["teams"][0]["song_score"]) - 1000.0
    rows[0]["upper_bound"] = rows[0]["score"]
    payload = _run(monkeypatch, tmp_path, index=_SeededIndex(rows))
    cert = payload["teams"][0]["certificate"]
    assert cert["status"] == "ERROR"
    assert "bug" in cert["error"] and "refined > exact" in cert["error"]


def test_different_roster_than_certified_argmax_says_so(monkeypatch, tmp_path):
    first = _run(monkeypatch, tmp_path)
    if first["inputs_hash"] is None:
        pytest.skip("no inputs_hash on this checkout")
    rows, _ = _seed_from(first, ["EXACT"])
    rows[0]["ids"] = sorted(["card_0", "card_1", "card_2", "card_3", "card_4"])
    rows[0]["members"] = rows[0]["ids"]
    rows[0]["score"] = float(first["teams"][0]["song_score"]) + 5.0   # certified best is better
    rows[0]["upper_bound"] = rows[0]["score"]
    payload = _run(monkeypatch, tmp_path, index=_SeededIndex(rows))
    cert = payload["teams"][0]["certificate"]
    assert cert["status"] == "EXACT" and cert["is_certified_argmax"] is False
    assert "certified best for this leader is" in cert["note"]


# ---------------------------------------------------------------------------
# 3. the index reads the real artefacts (read-only) and parses both shapes
# ---------------------------------------------------------------------------
def test_index_parses_table_and_certificate_shapes():
    table = {"song": "mX", "universe": "U", "basis": "maxed", "budget_kind": "flat",
             "budget": 453, "uptime_model": "finite", "platform": "mobile",
             "inputs_hash": "h" * 64, "model_version": MODEL_VERSION,
             "rows": [{"leader": "L1", "status": "EXACT", "score": 10.0, "ids": ["a"],
                       "upper_bound": 10.0, "gap": 0.0, "certificate": "c1.json"},
                      {"leader": "L2", "status": "BOUNDED", "score": 9.0, "ids": ["b"],
                       "upper_bound": 9.5, "gap": 0.0556, "certificate": "c2.json"},
                      {"leader": "L3", "status": "RUNNING"}]}
    entries = certindex._entries_from_table(table, Path("t.json"))
    assert [e["leader"] for e in entries] == ["L1", "L2"]
    assert entries[0]["platform"] == "mobile" and entries[0]["inputs_hash"] == "h" * 64
    cert = {"status": "EXACT", "song": "mX", "leader": "L1", "best": {"score": 10.0, "ids": ["a"]},
            "upper_bound": 10.0, "gap": 0.0, "inputs_hash": "h" * 64, "universe": "U",
            "basis": "maxed", "budget_kind": "flat", "budget": 453, "uptime_model": "finite",
            "platform": "mobile", "git": {"head": "x", "dirty": False}}
    e = certindex._entry_from_cert(cert, Path("c.json"))
    assert e["leader"] == "L1" and e["score"] == 10.0 and e["git_head"] == "x"
    assert certindex._entry_from_cert({"status": "HEURISTIC"}, Path("c.json")) is None


@pytest.mark.skipif(not (PHASE5 / "certificate_bnb_m0334_0042.json").is_file(),
                    reason="PHASE-5 certificate not on this checkout")
def test_index_finds_the_phase5_exact_row_and_the_guard_refuses_a_foreign_hash():
    index = certindex.CertificateIndex(table_roots=(), cert_roots=(PHASE5,))
    found = index.entries("m0334", CALLI_PDW)
    assert found and all(e["status"] in ("EXACT", "BOUNDED") for e in found)
    exact = [e for e in found if e["status"] == "EXACT"]
    assert exact and exact[0]["n_rosters"] == 8441052
    # its own hash + basis: matches; any other hash: refused with the note
    e = exact[0]
    basis = {k: e[k] for k in ("universe", "budget_kind", "budget", "uptime_model", "platform")}
    hit, note = index.lookup("m0334", CALLI_PDW, e["inputs_hash"], basis)
    assert hit is not None and note is None
    miss, note = index.lookup("m0334", CALLI_PDW, "9f1a" + "0" * 60, basis)
    assert miss is None and "different basis" in note
    # cached by mtime: a second listing re-parses nothing (same object back)
    assert index.entries("m0334", CALLI_PDW)[0] is found[0]


# ---------------------------------------------------------------------------
# 4. THE GUARD TEST: the server hashes the probe's cfg shape
# ---------------------------------------------------------------------------
def _dumps_present() -> bool:
    return all((REPO / rel).is_file() for _l, rel in cert_mod.INPUT_FILES)


def _probe_reference_hash() -> tuple[str, dict]:
    """The per-leader-table probe's own expression: `exhaustive_song_leader.
    load_context(uptime, budget, song)` + platform, hashed on the maxed basis."""
    spec = importlib.util.spec_from_file_location(
        "exhaustive_song_leader", REPO / "tools/recon/probes/exhaustive_song_leader.py")
    ex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ex)
    _cards, _cfg, ctx, _chart = ex.load_context("finite", 453, "m0334")
    ctx = dataclasses.replace(ctx, cfg=dataclasses.replace(ctx.cfg, platform="mobile"))
    return cert_mod.inputs_hash(ctx.cfg, "maxed", cert_mod.MODEL_VERSION)


@pytest.fixture(scope="module")
def probe_reference():
    if not _dumps_present():
        pytest.skip("gitignored dumps absent — no inputs_hash on this checkout")
    return _probe_reference_hash()


def _stub_exact_solver(team_ids):
    def solve_exact(pool, cfg, *, top_n, progress=None, constraint=None,
                    set_constraint=None, locked_ids=(), leader_id=None):
        by_id = {c.id: c for c in pool}
        team = Team(leader=by_id[team_ids[0]], members=tuple(by_id[c] for c in team_ids[1:]))
        return (RankedTeam(score=1.0, leader_id=team_ids[0],
                           member_ids=tuple(sorted(team_ids[1:])), team=team),)
    return solve_exact


def test_solve_song_mode_hash_equals_the_probes_hash(monkeypatch, probe_reference):
    """Cards = maxed, uptime finite, platform mobile, song m0334, REAL inputs
    (`load_inputs`, `apply_collection`, the chart library) — only the search
    itself is stubbed to one roster so the run is seconds, not a beam."""
    ref_hash, ref_files = probe_reference
    monkeypatch.setattr(web_server, "should_use_heuristic", lambda *a, **k: False)
    monkeypatch.setattr(web_server, "solve_exact", _stub_exact_solver(M1_ROSTER))
    monkeypatch.setattr(certindex, "_INDEX", _SeededIndex([]))
    job = web_server.Job(job_id="guard")
    web_server.run_solve_job(job, top_n=1, art_dir=Path("does-not-exist"), basis="maxed",
                             use_board=False, song="m0334", uptime_model="finite",
                             platform="mobile")
    events = []
    while True:
        try:
            events.append(job.events.get_nowait())
        except queue.Empty:
            break
    results = [e for e in events if e.get("type") == "result"]
    assert results, [e for e in events if e.get("type") == "error"]
    payload = results[-1]["payload"]
    assert payload["inputs_hash"] == ref_hash, (
        "server hash != probe hash; per-file digests (probe): "
        + json.dumps(ref_files, indent=1)
        + f"\nserver error: {payload.get('inputs_hash_error')}")
    assert payload["certificate_basis"]["platform"] == "mobile"
    assert payload["certificate_basis"]["uptime_model"] == "finite"


# Deliberately no test that a per-leader table on disk is still on today's basis --
# that would be a freshness alarm on a frozen research result, red after every new
# card. The hash RECIPE is pinned by `test_solve_song_mode_hash_equals_the_probes_hash`,
# and what the product does with a table on another basis (no certificate, a note
# naming the hash) by `test_hash_mismatch_is_heuristic_with_a_note_never_a_gap`.


# ---------------------------------------------------------------------------
# 5. /api/optimize-rerank carries the same fields (HTTP, maxed basis)
# ---------------------------------------------------------------------------
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


def test_optimize_rerank_song_rows_carry_certificates_on_the_probes_hash(web_base, probe_reference):
    ref_hash, _ = probe_reference
    status, body = _post(web_base + "/api/optimize-rerank", {
        "basis": "maxed", "teams": [M1_ROSTER], "song": "m0334", "budget": 453,
        "uptime_model": "finite", "platform": "mobile", "pool": "5star"})
    if status == 503:
        pytest.skip(body.get("error"))
    assert status == 200, body
    assert body["model_version"] == MODEL_VERSION
    assert body["inputs_hash"] == ref_hash
    assert body["certificate_basis"] == {
        "universe": "5star-maxed-catalogue", "basis": "maxed", "budget_kind": "flat",
        "budget": 453, "uptime_model": "finite", "platform": "mobile",
        # the developed rows' headline is the fixed-plan objective (plan-cache
        # leaf) — test_objective_leaf.py; the guard itself ignores the field
        "leaf": "plan_cache"}
    row = body["ranking"][0]
    cert = row["certificate"]
    assert cert["status"] in ("EXACT", "BOUNDED", "HEURISTIC", "ERROR")
    assert cert["inputs_hash"] == ref_hash and cert["basis"] == body["certificate_basis"]
    assert cert["stages"] and cert["candidates_evaluated"] == 1
    if cert["status"] in ("EXACT", "BOUNDED"):
        # a real certificate on file for this leader on THIS basis: it must be
        # about the same objective — the row cannot beat an EXACT maximum
        assert cert["status"] != "ERROR", cert
        assert cert["certified"]["file"]


# ---------------------------------------------------------------------------
# the members-first enumeration key: per-file lines + ONE chart
# ---------------------------------------------------------------------------
def test_enumeration_key_recipe_chart_digest_and_tolerance(tmp_path):
    import hashlib
    import members_first_store     # the recon-side writer (tools/web/ writes nothing)
    charts = {"schema_version": "charts-2", "difficulty": "EXPERT", "charts": {
        "m0001/EXPERT": {"music_id": "m0001", "difficulty": "EXPERT", "notes": [[1, 2], [3, 4]]},
        "m0002/EXPERT": {"music_id": "m0002", "difficulty": "EXPERT", "notes": [[5, 6]]}}}
    path = tmp_path / "charts.json"
    members_first_store.write_text_file(path, json.dumps(charts, indent=2))
    # chart_digest = sha256 of the canonical JSON of THAT song's entry
    canon = json.dumps(charts["charts"]["m0001/EXPERT"], sort_keys=True, separators=(",", ":"))
    assert cert_mod.chart_digest("m0001", charts_path=path) == hashlib.sha256(canon.encode()).hexdigest()
    assert cert_mod.chart_digest("m0002", charts_path=path) != cert_mod.chart_digest("m0001", charts_path=path)
    assert cert_mod.chart_digest("m0003", charts_path=path) is None
    # a new song APPENDED does not change an existing song's digest
    charts["charts"]["m0009/EXPERT"] = {"music_id": "m0009", "difficulty": "EXPERT", "notes": []}
    path2 = tmp_path / "charts2.json"
    members_first_store.write_text_file(path2, json.dumps(charts))
    assert cert_mod.chart_digest("m0001", charts_path=path2) == cert_mod.chart_digest("m0001", charts_path=path)
    # the list shape (entries with music_id) is read too
    path3 = tmp_path / "charts3.json"
    members_first_store.write_text_file(path3, json.dumps(list(charts["charts"].values())))
    assert cert_mod.chart_digest("m0001", charts_path=path3) == cert_mod.chart_digest("m0001", charts_path=path)
    with pytest.raises(OSError):
        cert_mod.chart_digest("m0001", charts_path=tmp_path / "missing.json")
    # the keys: the recipe's lines (cards, collection, board, skilltree,
    # master_stn, cfg, [chart,] model_version), one per line, sha256
    per = {"cards": "a" * 64, "board": "b" * 64, "charts": "c" * 64, "skilltree": "d" * 64,
           "master_stn": "e" * 64, "collection": "f" * 64, "cfg": "g" * 64}
    ref_lines = [f"cards={'a' * 64}", f"collection={'f' * 64}", f"board={'b' * 64}",
                 f"skilltree={'d' * 64}", f"master_stn={'e' * 64}", f"cfg={'g' * 64}",
                 "model_version=objective-v2.2"]
    assert cert_mod.reference_key(per, "objective-v2.2") == \
        hashlib.sha256("\n".join(ref_lines).encode()).hexdigest()
    cd = cert_mod.chart_digest("m0001", charts_path=path)
    own_lines = ref_lines[:-1] + [f"chart={cd}", "model_version=objective-v2.2"]
    assert cert_mod.enumeration_key(per, cd, "objective-v2.2") == \
        hashlib.sha256("\n".join(own_lines).encode()).hexdigest()
    # the charts.json WHOLE-file digest is NOT in either key
    assert cert_mod.reference_key({**per, "charts": "z" * 64}) == cert_mod.reference_key(per)
    assert cert_mod.enumeration_key({**per, "charts": "z" * 64}, cd) == cert_mod.enumeration_key(per, cd)
    # every other line is
    for k in cert_mod.REFERENCE_KEY_LINES:
        assert cert_mod.reference_key({**per, k: "z" * 64}) != cert_mod.reference_key(per), k
    assert cert_mod.enumeration_key(per, "other-chart") != cert_mod.enumeration_key(per, cd)
    assert cert_mod.enumeration_key(per, cd, "objective-v3") != cert_mod.enumeration_key(per, cd)
    with pytest.raises(KeyError):
        cert_mod.reference_key({"cards": "a" * 64})
    with pytest.raises(ValueError):
        cert_mod.enumeration_key(per, "")
    # the tolerant wrapper: (own, ref, chart, error)
    ek, rk, c, err = cert_mod.try_enumeration_keys(per, "m0001", charts_path=path)
    assert (ek, rk, c, err) == (cert_mod.enumeration_key(per, cd), cert_mod.reference_key(per), cd, None)
    ek, rk, c, err = cert_mod.try_enumeration_keys(per, "m0003", charts_path=path)
    assert ek is None and rk == cert_mod.reference_key(per) and c is None
    assert "no EXPERT chart for m0003" in err
    assert cert_mod.try_enumeration_keys(None, "m0001", charts_path=path)[3].startswith("run carries no per-file")
    assert cert_mod.try_enumeration_keys({"cards": "x"}, "m0001", charts_path=path)[3]
    ek, rk, c, err = cert_mod.try_enumeration_keys(per, "m0001", charts_path=tmp_path / "missing.json")
    assert ek is None and rk and "chart digest of m0001" in err
    # and the real per-file dict `inputs_hash` returns feeds them unchanged
    cfg = make_scoring_config()
    h, real_per, err = cert_mod.try_inputs_hash(cfg, "maxed")
    if err:
        pytest.skip(f"no inputs_hash on this checkout: {err}")
    assert set(cert_mod.REFERENCE_KEY_LINES) <= set(real_per)
    assert cert_mod.reference_key(real_per) == cert_mod.reference_key(
        {k: real_per[k] for k in cert_mod.REFERENCE_KEY_LINES})
