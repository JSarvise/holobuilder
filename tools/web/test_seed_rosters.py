"""Seed rosters (tools/web/seed_rosters.py) -- known-strong rosters UNIONED with
the beam's candidates before the develop, so the beam's recall failure
(it can miss the best known general roster) cannot hide a roster the run's own
evaluator ranks first.

Unit tests cover the index (file + artefact scanners, missing dirs, mtime
cache) and `filter_seeds` (pool, Ban, Must-include, leader pin, dedupe); the
pipeline tests drive `develop_pipeline` on the `maxed` basis and SKIP when the
gitignored skilltree/master dumps are absent (the pipeline's own 503).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import seed_rosters as sr
import server as web_server

# the community tool holodori.best's v3.1.1 roster (seed_rosters.json row 0)
_NOEL = "card-00022-5-uniq-0063-00"
_HOLODORI = [_NOEL, "card-00007-5-uniq-0008-00", _NOEL, "card-00021-5-uniq-0017-00",
             "card-00006-5-uniq-0007-00", "card-06005-5-uniq-0061-00"]
_KOBO = "card-03009-5-uniq-0041-00"
_MEMBERS = [
    "card-00002-5-uniq-0001-00", "card-00034-5-uniq-0027-00",
    "card-00028-5-uniq-0023-00", "card-00031-5-uniq-0025-00",
    "card-00016-5-uniq-0014-00",
]
_TEAM_A = ["card-04001-5-uniq-0042-00", *_MEMBERS]   # Calliope leads


def _has_dumps() -> bool:
    return web_server.SKILLTREE_PATH.is_file() and web_server.NODE_MASTER_PATH.is_file()


def _pipeline(body: dict) -> dict:
    try:
        return web_server.develop_pipeline(body, art_dir=web_server.Handler.art_dir)
    except web_server._HttpError as exc:
        if exc.code == 503:
            pytest.skip("skilltree/master dumps absent (gitignored)")
        raise


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------
def test_committed_seed_file_parses_and_names_the_known_rosters():
    idx = sr.SeedIndex(table_roots=(), across_roots=())
    seeds = idx.seeds("all")
    keys = {s.key for s in seeds}
    assert (_NOEL, frozenset(_HOLODORI[1:])) in keys
    noel_kobo = [s for s in seeds if s.leader == _NOEL and _KOBO in s.members]
    assert noel_kobo and noel_kobo[0].provenance.startswith("seed:seed_rosters.json#")
    # every committed row is a legal shape: 1 leader + 5 members, all ids
    for s in seeds:
        assert len(s.members) == sr.MEMBER_COUNT and s.leader.startswith("card-")


def test_missing_artefact_dirs_and_missing_file_are_no_seeds(tmp_path: Path):
    idx = sr.SeedIndex(seed_file=tmp_path / "absent.json",
                       table_roots=(tmp_path / "no-such-dir",),
                       across_roots=(tmp_path / "nor-this",))
    assert idx.seeds("all") == []
    assert idx.refresh() == 0


def test_artefact_parsers_read_table_rows_and_across_top():
    """The two artefact shapes, parsed from documents (no file is written:
    test_no_writes.py scans this file too)."""
    table = sr._from_table({
        "song": "m0334",
        "rows": [{"leader": "L1", "ids": ["a", "b", "c", "d", "e"], "status": "exact",
                  "leader_name": "One"},
                 {"leader": "L2", "ids": ["a", "b"]},            # malformed: skipped
                 {"leader": "L3", "ids": ["a", "b", "c", "d", "L3"], "status": "bounded"}],
    }, Path("per_leader/m0334/per_leader_table_m0334.json"))
    assert [s.leader for s in table] == ["L1", "L3"]
    assert table[0].provenance == "seed:per_leader/m0334:exact"
    assert table[1].provenance == "seed:per_leader/m0334:bounded"
    across = sr._from_across({
        "top": [{"rank": 1, "leader": "L1", "members": ["a", "b", "c", "d", "e"]},
                {"rank": 2, "leader": "L9", "members": ["p", "q", "r", "s", "t"], "label": "nine"}],
    }, Path("across_ranking_x.json"))
    assert [s.leader for s in across] == ["L1", "L9"]
    assert across[1].provenance == "seed:across_ranking_x#2" and across[1].note == "nine"
    # not the shapes: no seeds, no error
    assert sr._from_table([], Path("x.json")) == []
    assert sr._from_across({"rows": []}, Path("x.json")) == []
    assert sr._from_seed_file({"leader": "L"}, Path("x.json")) == []


def test_default_index_scans_the_committed_artefacts_and_dedupes_by_leader_and_member_set():
    """Read-only against the repo: the hand file first, then the research
    per-leader tables and across rankings; scope="file" is the hand file only."""
    idx = sr.default_index()
    all_seeds = idx.seeds("all")
    file_seeds = idx.seeds("file")
    assert file_seeds and all(s.source.startswith("seed_rosters.json#") for s in file_seeds)
    assert len(all_seeds) >= len(file_seeds)
    assert len({s.key for s in all_seeds}) == len(all_seeds)
    # the hand file is listed first, so a roster it shares with an artefact
    # keeps the hand file's provenance
    assert all_seeds[:len(file_seeds)] == file_seeds
    if any(k == "table" for k, _p in idx._list()):
        assert any(s.source.startswith("per_leader/") for s in all_seeds)


# ---------------------------------------------------------------------------
# filter_seeds: the rules a seed must pass to enter a run
# ---------------------------------------------------------------------------
def _seed(leader, members, source="t"):
    return sr.SeedRoster(leader=leader, members=tuple(members), source=source)


def test_filter_drops_seeds_outside_the_pool_or_banned_or_missing_a_required_card():
    s = _seed("L", "abcde")
    pool = set("Labcde")
    assert sr.filter_seeds([s], pool_ids=pool) == [s]
    assert sr.filter_seeds([s], pool_ids=pool - {"c"}) == []          # not in the run's pool
    assert sr.filter_seeds([s], pool_ids=pool, exclude=["d"]) == []   # Ban
    assert sr.filter_seeds([s], pool_ids=pool, exclude=["L"]) == []   # Ban on the leader
    assert sr.filter_seeds([s], pool_ids=pool, require=["a", "z"]) == []   # Must-include missing
    assert sr.filter_seeds([s], pool_ids=pool, require=["a"]) == [s]
    assert sr.filter_seeds([s], pool_ids=pool, leader=["X"]) == []    # leader pin
    assert sr.filter_seeds([s], pool_ids=pool, leader=["L"]) == [s]


def test_filter_dedupes_against_taken_and_among_seeds_by_leader_and_member_set():
    a = _seed("L", "abcde", "one")
    b = _seed("L", "edcba", "two")            # same set, different order
    c = _seed("a", "Lbcde", "three")          # leader/member role swap: a DIFFERENT team
    out = sr.filter_seeds([a, b, c], pool_ids=set("Labcde"))
    assert out == [a, c]
    out = sr.filter_seeds([a, b, c], pool_ids=set("Labcde"),
                          taken=[("L", frozenset("abcde"))])
    assert out == [c]


# ---------------------------------------------------------------------------
# the pipeline: seeds are developed and ranked like any other candidate
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _has_dumps(), reason="skilltree/master dumps absent (gitignored)")
def test_seeds_join_the_developed_set_with_provenance_and_the_same_row_shape():
    doc = _pipeline({"basis": "maxed", "teams": [_TEAM_A], "budget": 453,
                     "seeds": "file", "uptime_model": "finite", "platform": "mobile"})
    rows = doc["ranking"]
    keys = {(r["leader_id"], frozenset(r["member_ids"])) for r in rows}
    assert (_NOEL, frozenset(_HOLODORI[1:])) in keys, "the holodori seed was not developed"
    seeded = [r for r in rows if r.get("provenance")]
    assert seeded and all(r["provenance"].startswith("seed:seed_rosters.json#") for r in seeded)
    posted = [r for r in rows if r["cards"] == _TEAM_A]
    assert posted and "provenance" not in posted[0]
    # same shape and same sort as every other row -- seeds are not privileged
    assert rows == sorted(rows, key=lambda r: r["optimised_us"], reverse=True)
    for r in seeded:
        assert {"cards", "leader_id", "member_ids", "gates", "board_state",
                "optimised_us", "board_plan_summary"} <= set(r)
        for cid in r["cards"]:
            assert cid in doc["cards"]          # faces for cards the solve never showed


@pytest.mark.skipif(not _has_dumps(), reason="skilltree/master dumps absent (gitignored)")
def test_a_seed_with_a_banned_card_is_dropped_and_dedupe_keeps_one_row():
    # Ban Noel: every Noel-led seed is dropped, never forced
    doc = _pipeline({"basis": "maxed", "teams": [_TEAM_A], "budget": 453,
                     "seeds": "file", "exclude": [_NOEL]})
    assert all(_NOEL not in r["cards"] for r in doc["ranking"])
    # posting that roster yourself: the seed dedupes against it (one
    # row, no provenance -- it was the caller's team first)
    doc = _pipeline({"basis": "maxed", "teams": [_HOLODORI], "budget": 453, "seeds": "file"})
    holo = [r for r in doc["ranking"] if r["cards"] == _HOLODORI]
    assert len(holo) == 1 and "provenance" not in holo[0]
    # by (leader, member set), so a member-order permutation is the same seed
    assert len({(r["leader_id"], frozenset(r["member_ids"])) for r in doc["ranking"]}) \
        == len(doc["ranking"])


@pytest.mark.skipif(not _has_dumps(), reason="skilltree/master dumps absent (gitignored)")
def test_seeds_are_off_by_default_for_a_plain_posted_develop():
    """The legacy contract of `/api/optimize-rerank` -- develop THESE rosters --
    is byte-compatible: no `seeds` key, no leader_diverse, no refine -> only the
    posted teams come back."""
    doc = _pipeline({"basis": "maxed", "teams": [_TEAM_A], "budget": 453})
    assert [r["cards"] for r in doc["ranking"]] == [_TEAM_A]


@pytest.mark.skipif(not _has_dumps(), reason="skilltree/master dumps absent (gitignored)")
def test_refine_leaf_flag_pins_the_legacy_develop_leaf(monkeypatch):
    """`refine_leaf`: "plan_cache" (default) seeds ONE fixed PlanCache from the
    run's #1 developed team; "develop" is the per-candidate develop leaf
    (RefineContext.use_plan_cache False, no seed) -- so a caller can pin that
    behaviour byte-for-byte."""
    seen: list = []
    real_pool = web_server.RefinePool

    class SpyPool(real_pool):
        def __init__(self, ctx, **kw):
            seen.append(ctx)
            super().__init__(ctx, workers=1, **{k: v for k, v in kw.items() if k != "workers"})

    monkeypatch.setattr(web_server, "RefinePool", SpyPool)
    body = {"basis": "maxed", "teams": [_TEAM_A, _HOLODORI], "budget": 453,
            "refine": True, "seeds": False}
    doc_dev = _pipeline({**body, "refine_leaf": "develop"})
    assert seen and seen[-1].use_plan_cache is False and seen[-1].plan_cache_seed is None
    top_dev = doc_dev["ranking"][0]
    doc_pc = _pipeline(body)                                  # default: plan_cache
    ctx = seen[-1]
    assert ctx.use_plan_cache is True
    # the seed is the run's #1 DEVELOPED team (one of the two posted leaders --
    # a refine never changes a leader), fixed for the whole run
    assert ctx.plan_cache_seed[0] in {_TEAM_A[0], _NOEL}
    assert len(ctx.plan_cache_seed[1]) == 5
    assert top_dev["leader_id"] in {_TEAM_A[0], _NOEL}
    # the final ranking is on each team's OWN developed board either way
    for doc in (doc_dev, doc_pc):
        assert doc["sorted_by"] == "optimised_us"
        assert doc["ranking"] == sorted(doc["ranking"], key=lambda r: r["optimised_us"],
                                        reverse=True)
