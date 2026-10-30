"""The OBJECTIVE LEAF of developed rows: the web's song-mode headline is the v2
objective S(T, C) — the team on the FIXED plans of ONE PlanCache seeded for the
run — and the per-team greedy re-develop (`optimize_and_rerank` per team) stays
on every row as the LABELLED what-if.

Why this exists: on an every-leader run (e.g. m0334) some certified leaders'
best rows would render certificate status ERROR although their roster IS the
certified argmax, because the develop leaf exceeds the certified plan-cache
float by 0.2-0.7 % and the guard treats row > EXACT as an error. The guard is
right; the develop number is the wrong objective for the headline.

What is asserted:

* `certindex.attach` (no dumps needed): on the plan-cache leaf a row above an
  EXACT certificate is still ERROR; on the develop leaf (`basis.leaf ==
  "develop"`) it is HEURISTIC with the what-if note, never ERROR; a develop
  row at/below the certificate keeps EXACT and names the leaf;
* `CertificateIndex.plan_cache_seed_for` returns a table's seed only through
  the same guard (hash + basis), and the real m0334 table's seed when present;
* the pipeline (skips without the gitignored dumps): song rows carry BOTH
  numbers (`song_est` objective, `song_est_develop` what-if) and both boards,
  `certificate_basis.leaf == "plan_cache"`, ranking sorted by `song_est`, the
  `objective` meta names the seed; `objective_leaf: "develop"` pins the legacy
  rows byte-for-byte (its `song_est` / board == the default run's what-if) with
  `leaf == "develop"` and no what-if keys;
* the develop-above-certificate case, simulated: a fake table row whose score is BELOW the
  develop score but EQUAL to the plan-cache score → EXACT, is_certified_argmax
  true under the default; HEURISTIC (not ERROR) under the legacy flag;
* the Compare view (board=develop, song) carries the same pair and the
  comparator says which is which; Calli PDW + the m0334 incumbent is
  3,437,830 on the objective.

    python -m pytest tools/web/test_objective_leaf.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import certindex
import compare_view
import server as web_server
from hbuilder.domain.model_version import MODEL_VERSION

REPO = Path(__file__).resolve().parents[2]
TABLE_M0334 = REPO / "tools/recon/RESEARCH-2026-08-18/PHASE-6/per_leader/m0334/per_leader_table_m0334.json"

CALLI_PDW = "card-04001-5-uniq-0042-00"
M1_ROSTER = [CALLI_PDW, CALLI_PDW, "card-00032-5-uniq-0026-00",
             "card-04003-5-uniq-0044-00", "card-00007-5-uniq-0008-00",
             "card-00006-5-uniq-0007-00"]
CALLI_RSH = "card-04001-5-uniq-0071-00"
RSH_ROSTER = [CALLI_RSH, "card-03004-5-uniq-0073-00", "card-03005-5-uniq-0037-00",
              "card-03006-5-uniq-0038-00", "card-06004-5-uniq-0060-00",
              "card-06005-5-uniq-0061-00"]
# Marine "Crimson Captain"'s certified argmax (m0334 table row, 3,030,833):
# one of the 8 leaders whose per-team re-develop beats the fixed plan
# (3,044,887, +0.46 %)
MARINE = "card-00023-5-uniq-0019-00"
MARINE_ROSTER = [MARINE, "card-00006-5-uniq-0007-00", "card-00007-5-uniq-0008-00",
                 "card-00021-5-uniq-0064-00", "card-00022-5-uniq-0018-00",
                 "card-00032-5-uniq-0026-00"]

BASIS = {"universe": "5star-maxed-catalogue", "basis": "maxed", "budget_kind": "flat",
         "budget": 453, "uptime_model": "finite", "platform": "mobile"}
BODY = {"basis": "maxed", "song": "m0334", "budget": 453, "uptime_model": "finite",
        "platform": "mobile", "pool": "5star", "members_first_k": 0, "seeds": False}


def _has_dumps() -> bool:
    return web_server.SKILLTREE_PATH.is_file() and web_server.NODE_MASTER_PATH.is_file()


def _pipeline(body: dict) -> dict:
    try:
        return web_server.develop_pipeline(body, art_dir=web_server.Handler.art_dir)
    except web_server._HttpError as exc:
        if exc.code == 503:
            pytest.skip("skilltree/master dumps absent (gitignored)")
        raise


class _SeededIndex(certindex.CertificateIndex):
    """Rows injected, not read from disk (test_no_writes forbids fixtures)."""

    def __init__(self, rows):
        super().__init__(table_roots=(), cert_roots=())
        self.rows = list(rows)

    def entries(self, song=None, leader=None):
        return [r for r in self.rows
                if (song is None or r["song"] == song)
                and (leader is None or r["leader"] == leader)]


def _entry(song, leader, ids, score, inputs_hash, *, seed=None, status="EXACT"):
    return {
        "song": song, "leader": leader, "leader_name": leader, "status": status,
        "score": score, "ids": sorted(ids), "members": list(ids), "leader_also_member": False,
        "upper_bound": score, "gap": 0.0, "leaves_evaluated": 1, "n_rosters": 8441052,
        "seconds": 1.0, "inputs_hash": inputs_hash, "model_version": MODEL_VERSION,
        "git_head": "abc", "git_dirty": False, "written": "2026-08-19T00:00:00",
        "certificate": "fixture/certificate.json", "source": "fixture/table.json",
        "source_kind": "table", "plan_cache_seed": seed, **BASIS,
    }


# ---------------------------------------------------------------------------
# 1. certindex.attach: the leaf decides ERROR vs HEURISTIC (no dumps)
# ---------------------------------------------------------------------------
def _hc(leaf):
    return certindex.heuristic_certificate(
        model_version=MODEL_VERSION, inputs_hash="h" * 64,
        basis={**BASIS, "leaf": leaf}, generator="test", stages=["s"])


def test_attach_plan_cache_leaf_row_above_exact_is_error():
    idx = _SeededIndex([_entry("mX", "L", ["a", "b", "c", "d", "e"], 1000.0, "h" * 64)])
    cert = certindex.attach(_hc("plan_cache"), song="mX", leader="L",
                            member_ids=["a", "b", "c", "d", "e"], row_score=1010.0, index=idx)
    assert cert["status"] == "ERROR" and "refined > exact" in cert["error"]
    assert cert["basis"]["leaf"] == "plan_cache"


def test_attach_develop_leaf_row_above_exact_is_heuristic_with_the_whatif_note():
    idx = _SeededIndex([_entry("mX", "L", ["a", "b", "c", "d", "e"], 1000.0, "h" * 64)])
    cert = certindex.attach(_hc("develop"), song="mX", leader="L",
                            member_ids=["a", "b", "c", "d", "e"], row_score=1010.0, index=idx)
    assert cert["status"] == "HEURISTIC"
    assert "error" not in cert
    assert cert["note"] == ("scored on the per-team develop leaf (what-if), not the "
                            "certified objective")
    assert cert["is_certified_argmax"] is True          # the roster IS the argmax
    assert cert["certified"]["score"] == 1000.0          # the certificate stays visible
    # BOUNDED above its upper bound: the same rule
    idx2 = _SeededIndex([_entry("mX", "L", ["a", "b", "c", "d", "e"], 1000.0, "h" * 64,
                                status="BOUNDED")])
    cert2 = certindex.attach(_hc("develop"), song="mX", leader="L",
                             member_ids=["a", "b", "c", "d", "e"], row_score=1010.0, index=idx2)
    assert cert2["status"] == "HEURISTIC" and "error" not in cert2


def test_attach_develop_leaf_row_at_or_below_exact_keeps_exact_and_names_the_leaf():
    idx = _SeededIndex([_entry("mX", "L", ["a", "b", "c", "d", "e"], 1000.0, "h" * 64)])
    cert = certindex.attach(_hc("develop"), song="mX", leader="L",
                            member_ids=["a", "b", "c", "d", "e"], row_score=1000.0, index=idx)
    assert cert["status"] == "EXACT" and cert["is_certified_argmax"] is True
    assert "develop leaf" in cert["leaf_note"]
    # no leaf at all (a board-off /solve row): unchanged behaviour, no leaf note
    hc = certindex.heuristic_certificate(model_version=MODEL_VERSION, inputs_hash="h" * 64,
                                         basis=BASIS, generator="test", stages=["s"])
    cert0 = certindex.attach(hc, song="mX", leader="L", member_ids=["a", "b", "c", "d", "e"],
                             row_score=1000.0, index=idx)
    assert cert0["status"] == "EXACT" and "leaf_note" not in cert0


# ---------------------------------------------------------------------------
# 2. the table's plan-cache seed, through the guard
# ---------------------------------------------------------------------------
def test_plan_cache_seed_for_passes_only_through_the_guard():
    seed = {"leader": "L", "members": ["L", "a", "b", "c", "d"]}
    idx = _SeededIndex([_entry("mX", "L", ["a", "b", "c", "d", "e"], 1.0, "h" * 64, seed=seed)])
    assert idx.plan_cache_seed_for("mX", "h" * 64, BASIS) == (
        "L", ("L", "a", "b", "c", "d"), "fixture/table.json")
    # another hash, another platform, another song, no hash: nothing
    assert idx.plan_cache_seed_for("mX", "g" * 64, BASIS) is None
    assert idx.plan_cache_seed_for("mX", "h" * 64, {**BASIS, "platform": "pc"}) is None
    assert idx.plan_cache_seed_for("mY", "h" * 64, BASIS) is None
    assert idx.plan_cache_seed_for("mX", None, BASIS) is None
    # `ignore`: the Compare view skips the universe (it searches nothing)
    assert idx.plan_cache_seed_for("mX", "h" * 64, {**BASIS, "universe": "maxed-catalogue"}) is None
    assert idx.plan_cache_seed_for("mX", "h" * 64, {**BASIS, "universe": "maxed-catalogue"},
                                   ignore=("universe",)) is not None
    # a table without a seed: None, never a guess
    idx2 = _SeededIndex([_entry("mX", "L", ["a", "b", "c", "d", "e"], 1.0, "h" * 64)])
    assert idx2.plan_cache_seed_for("mX", "h" * 64, BASIS) is None


def test_real_m0334_table_exposes_its_seed_read_only():
    if not TABLE_M0334.is_file():
        pytest.skip("m0334 per-leader table not on this checkout")
    doc = json.loads(TABLE_M0334.read_bytes().decode("utf-8"))
    entries = certindex._entries_from_table(doc, TABLE_M0334)
    assert entries and all(e["plan_cache_seed"] == doc["plan_cache_seed"] for e in entries)
    found = certindex.default_index().plan_cache_seed_for(
        "m0334", doc["inputs_hash"], {k: doc[k] for k in BASIS})
    assert found is not None
    assert found[0] == doc["plan_cache_seed"]["leader"]
    assert list(found[1]) == doc["plan_cache_seed"]["members"]


# ---------------------------------------------------------------------------
# 3. the pipeline: both numbers, both boards, the objective sorts; legacy pinned
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def runs():
    if not _has_dumps():
        pytest.skip("skilltree/master dumps absent (gitignored)")
    body = {**BODY, "teams": [M1_ROSTER, RSH_ROSTER, MARINE_ROSTER]}
    default = _pipeline(body)
    legacy = _pipeline({**body, "objective_leaf": "develop"})
    return default, legacy


def _by_leader(doc):
    return {r["leader_id"]: r for r in doc["ranking"]}


def test_rows_carry_both_numbers_and_both_boards_sorted_by_the_objective(runs):
    default, _ = runs
    assert default["certificate_basis"]["leaf"] == "plan_cache"
    assert default["objective"]["leaf"] == "plan_cache"
    assert default["objective"]["plan_cache_seed"]["leader"]
    assert default["objective"]["seed_source"]
    rows = default["ranking"]
    assert rows == sorted(rows, key=lambda r: r["song_est"], reverse=True)
    assert default["sorted_by"] == "song_est"
    for r in rows:
        assert isinstance(r["song_est"], int) and isinstance(r["song_est_develop"], int)
        assert r["objective_leaf"] == "plan_cache"
        assert r["board_plan_summary"]["leaf"] == "plan_cache"
        assert r["board_plan_summary_develop"]["leaf"] == "develop"
        assert r["board_plan_summary"]["developed"] == r["song_est"]
        assert r["board_plan_summary_develop"]["developed"] == r["song_est_develop"]
        assert r["board_state"] and r["board_state_develop"]
        wi = r["develop_whatif"]
        assert wi["leaf"] == "develop" and wi["song_est"] == r["song_est_develop"]
        assert wi["delta_pct"] == pytest.approx(
            100.0 * (r["song_est_develop"] - r["song_est"]) / r["song_est"], abs=0.01)
        assert "greedy re-develop" in wi["label"]
        assert r["certificate"]["basis"]["leaf"] == "plan_cache"
    # one of the 8: the greedy re-develop beats the fixed plan on this roster
    sora = _by_leader(default)[MARINE]
    assert sora["song_est_develop"] > sora["song_est"]
    assert sora["develop_whatif"]["delta_pct"] > 0
    assert sora["board_state"] != sora["board_state_develop"]


def test_legacy_flag_pins_the_per_team_develop_rows_byte_for_byte(runs):
    default, legacy = runs
    assert legacy["certificate_basis"]["leaf"] == "develop"
    assert legacy["objective"] == {"leaf": "develop"}
    assert legacy["ranking"] == sorted(legacy["ranking"], key=lambda r: r["song_est"],
                                       reverse=True)
    d, l = _by_leader(default), _by_leader(legacy)
    assert set(d) == set(l)
    for leader, lrow in l.items():
        drow = d[leader]
        # no what-if keys on a legacy row
        for k in ("song_est_develop", "board_state_develop", "board_plan_summary_develop",
                  "develop_whatif", "objective_leaf", "points_develop"):
            assert k not in lrow, k
        assert "leaf" not in lrow["board_plan_summary"]
        # the legacy headline / board / summary ARE the default run's what-if
        assert lrow["song_est"] == drow["song_est_develop"]
        assert lrow["board_state"] == drow["board_state_develop"]
        assert lrow["points"] == drow["points_develop"]
        dev_summary = {k: v for k, v in drow["board_plan_summary_develop"].items() if k != "leaf"}
        assert lrow["board_plan_summary"] == dev_summary
        assert lrow["song_off"] == drow["song_off"]
        assert lrow["certificate"]["basis"]["leaf"] == "develop"


def test_certified_argmax_is_exact_on_the_objective_and_not_error_on_the_legacy_leaf(
        runs, monkeypatch):
    """The develop-above-certificate case, simulated: a fake table row whose certified score is
    BELOW the develop score but EQUAL to the plan-cache score."""
    default, _ = runs
    if default.get("inputs_hash") is None:
        pytest.skip(f"no inputs_hash on this checkout: {default.get('inputs_hash_error')}")
    sora = _by_leader(default)[MARINE]
    assert sora["song_est_develop"] > sora["song_est"] + certindex.SCORE_TOLERANCE
    seed = default["objective"]["plan_cache_seed"]
    rows = [_entry("m0334", MARINE, sora["member_ids"], float(sora["song_est"]),
                   default["inputs_hash"], seed=seed)]
    monkeypatch.setattr(certindex, "_INDEX", _SeededIndex(rows))
    body = {**BODY, "teams": [M1_ROSTER, MARINE_ROSTER]}
    doc = _pipeline(body)
    # the fake table's seed IS the objective's seed (same hash, same basis)
    assert doc["objective"]["plan_cache_seed"] == seed
    assert doc["objective"]["seed_source"].startswith("per-leader table")
    row = _by_leader(doc)[MARINE]
    assert row["song_est"] == sora["song_est"]
    cert = row["certificate"]
    assert cert["status"] == "EXACT", cert
    assert cert["is_certified_argmax"] is True
    assert "error" not in cert
    # the legacy leaf on the same fake table: HEURISTIC with the note, never ERROR
    doc_dev = _pipeline({**body, "objective_leaf": "develop"})
    cert_dev = _by_leader(doc_dev)[MARINE]["certificate"]
    assert cert_dev["status"] == "HEURISTIC", cert_dev
    assert cert_dev["is_certified_argmax"] is True
    assert cert_dev["note"] == ("scored on the per-team develop leaf (what-if), not the "
                                "certified objective")
    assert "error" not in cert_dev


def test_real_m0334_certificates_attach_exact_argmax_to_both_calli_rows(runs):
    """On the checkout that carries the m0334 table the two Calli rows are the
    certified argmax at the certified floats: RSH 3,452,260, PDW 3,437,830."""
    default, _ = runs
    if not TABLE_M0334.is_file():
        pytest.skip("m0334 per-leader table not on this checkout")
    if default["objective"].get("seed_source", "").startswith("per-leader table") is False:
        pytest.skip("this checkout's inputs_hash is not the table's; the seed fell back")
    rows = _by_leader(default)
    for leader, expected in ((CALLI_PDW, 3437830), (CALLI_RSH, 3452260)):
        row = rows[leader]
        assert row["song_est"] == expected
        assert row["certificate"]["status"] == "EXACT", row["certificate"]
        assert row["certificate"]["is_certified_argmax"] is True


# ---------------------------------------------------------------------------
# 4. the Compare view: the same pair, named
# ---------------------------------------------------------------------------
def test_compare_develop_carries_objective_and_whatif_and_names_which_is_which():
    if not _has_dumps():
        pytest.skip("skilltree/master dumps absent (gitignored)")
    body = {"teams": [M1_ROSTER, MARINE_ROSTER], "basis": "maxed", "board": "develop",
            "budget": 453, "song": "m0334", "uptime_model": "finite", "platform": "mobile"}
    try:
        doc = compare_view.build_compare(body, art_dir=web_server.Handler.art_dir)
    except compare_view.CompareError as exc:
        if exc.code == 503:
            pytest.skip(exc.message)
        raise
    assert doc["objective"]["leaf"] == "plan_cache"
    assert doc["certificate_basis"]["leaf"] == "plan_cache"
    assert "objective_note" in doc["diff"] and "song_est_develop" in doc["diff"]
    a, b = doc["teams"]
    for t in (a, b):
        s = t["score"]
        assert s["headline"] == s["song_est"]
        assert isinstance(s["song_est_develop"], float)
        assert t["develop_whatif"]["song_est"] == round(s["song_est_develop"])
        assert t["board_plan_summary"]["leaf"] == "plan_cache"
        assert t["board_plan_summary_develop"]["leaf"] == "develop"
        assert t["board_state"] and t["board_state_develop"]
        assert t["certificate"]["basis"]["leaf"] == "plan_cache"
    # the what-if beats the objective on Marine's roster, not on the incumbent's
    assert b["score"]["song_est_develop"] > b["score"]["song_est"]
    if doc["objective"]["seed_source"].startswith("per-leader table"):
        assert round(a["score"]["headline"]) == 3437830     # Calli PDW + incumbent
    # legacy flag: single number, develop leaf
    doc_dev = compare_view.build_compare({**body, "objective_leaf": "develop"},
                                         art_dir=web_server.Handler.art_dir)
    assert doc_dev["objective"] == {"leaf": "develop"}
    assert doc_dev["certificate_basis"]["leaf"] == "develop"
    for t in doc_dev["teams"]:
        assert "song_est_develop" not in t["score"] and "develop_whatif" not in t
        assert t["board_plan_summary"]["leaf"] == "develop"
    assert round(doc_dev["teams"][1]["score"]["headline"]) == b["develop_whatif"]["song_est"]
