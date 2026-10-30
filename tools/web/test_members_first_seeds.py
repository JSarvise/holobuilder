"""Members-first seeds (tools/web/members_first_index.py + the develop
pipeline's `members_first` stage): a cached bare-proxy enumeration of a song,
on the SAME inputs_hash as the run, seeds every leader's members-first best
roster into the develop set with provenance `seed:members-first K=<k>`; a
different hash / pool / an unfinished enumeration seeds nothing and says why;
`members_first_k: 0` disables.

The index tests use a FAKE enumeration dir (tmp_path, `HBUILDER_MEMBERS_FIRST_DIR`)
over a 9-card 5★ pool (126 legal sets, 1 KB of scores). The pipeline tests
shrink the run's 5★ universe to the same 9 cards by monkeypatching
`server._is_five_star` (so the enumeration's pool IS the run's pool, the
guard's requirement), run serially (`HBUILDER_WEB_WORKERS=1`) and SKIP when the
gitignored skilltree/master dumps are absent.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import members_first_index as mfi
import certificate as cert_mod
import server as web_server
import members_first_store  # tools/recon/proxy: on sys.path once `server` is imported
from hbuilder.solver import members_first as mf

CALLI_PDW = "card-04001-5-uniq-0042-00"
NOEL = "card-00022-5-uniq-0063-00"
POOL9 = [
    CALLI_PDW,                      # Mori Calliope, gate Myth x2
    "card-00032-5-uniq-0026-00",    # Shishiro Botan
    "card-04003-5-uniq-0044-00",    # Ninomae Ina'nis
    "card-00007-5-uniq-0008-00",    # Natsuiro Matsuri
    "card-00006-5-uniq-0007-00",    # Shirakami Fubuki, gate 1期生 x2
    NOEL,                           # Shirogane Noel, gate 3期生 x2
    "card-03009-5-uniq-0041-00",    # Kobo Kanaeru
    "card-04012-5-uniq-0051-00",    # Hakos Baelz, gate Pure x2
    "card-00021-5-uniq-0017-00",    # Shiranui Flare
]
SEED = {"leader": CALLI_PDW, "members": POOL9[:5]}
SONG = "m0334"


def _has_dumps() -> bool:
    return web_server.SKILLTREE_PATH.is_file() and web_server.NODE_MASTER_PATH.is_file()


@pytest.fixture
def cards():
    cards, _owned, _cfg, _prov = web_server.load_inputs(
        web_server.CARDS_PATH, web_server.OWNED_PATH, False)
    by_id = {c.id: c for c in cards}
    return [by_id[c] for c in sorted(POOL9)]


def _write_enumeration(d: Path, pool_ids, scores, *, song=SONG, proxy="fake",
                       inputs_hash="0" * 64, finished=True, n_sets=None, extra=None) -> Path:
    # the writer lives on the recon side (tools/recon/proxy/members_first_store.py):
    # nothing under tools/web/ may write a file, test files included (test_no_writes.py)
    meta_extra = {"proxy_card": "card-04012-5-bare-0051-00", "proxy_holomem": "Hakos Baelz",
                  "budget": 453, "uptime": "finite", "platform": "mobile", **(extra or {})}
    members_first_store.write_enumeration(
        d, song=song, proxy=proxy, pool=pool_ids, scores=scores, inputs_hash=inputs_hash,
        plan_cache_seed=SEED, finished=finished, n_sets=n_sets, extra=meta_extra)
    return d


def _fake_scores(n: int, seed: int = 7) -> list[float]:
    # deterministic, distinct, not monotone in the index
    return [float(((i * 7919 + seed) % 1009) * 1000 + i) for i in range(n)]


# ---------------------------------------------------------------------------
# the index: scanning, the guard, loading, best_for_leaders
# ---------------------------------------------------------------------------
def test_index_lists_complete_and_incomplete_enumerations(tmp_path, cards):
    pool = [c.id for c in cards]
    n = 126
    _write_enumeration(tmp_path / "a", pool, _fake_scores(n), inputs_hash="a" * 64)
    _write_enumeration(tmp_path / "b", pool, _fake_scores(n)[:20], inputs_hash="b" * 64,
                       n_sets=n)                                    # partial scores
    _write_enumeration(tmp_path / "c", pool, _fake_scores(n), inputs_hash="c" * 64,
                       finished=False)                              # running
    members_first_store.write_text_file(tmp_path / "d" / "members_first_m0001_x.meta.json", "{not json")
    idx = mfi.MembersFirstIndex([tmp_path / "a", tmp_path / "b", tmp_path / "c",
                                 tmp_path / "d", tmp_path / "missing"], use_env=False)
    ens = idx.enumerations(SONG)
    assert {e.inputs_hash[0] for e in ens} == {"a", "b", "c"}
    by = {e.inputs_hash[0]: e for e in ens}
    assert by["a"].complete and by["a"].problem is None
    assert not by["b"].complete and "20 of 126" in by["b"].problem
    assert not by["c"].complete and "not finished" in by["c"].problem
    assert idx.enumerations(SONG, complete_only=True) == [by["a"]]
    assert idx.enumerations("m9999") == []
    # a summary is JSON-able and names the files repo-relative or absolute
    json.dumps(by["a"].summary())
    assert by["a"].summary()["pool_size"] == 9 and by["a"].summary()["n_sets"] == 126


def test_available_guards_hash_pool_and_completeness_and_prefers_the_newest(tmp_path, cards):
    pool = [c.id for c in cards]
    n = 126
    _write_enumeration(tmp_path / "a", pool, _fake_scores(n), inputs_hash="a" * 64,
                       extra={"finished": "2026-08-19T02:00:00"})
    _write_enumeration(tmp_path / "a2", pool, _fake_scores(n, 3), inputs_hash="a" * 64,
                       proxy="other", extra={"finished": "2026-08-19T03:00:00"})
    _write_enumeration(tmp_path / "b", pool, _fake_scores(n)[:20], inputs_hash="b" * 64, n_sets=n)
    idx = mfi.MembersFirstIndex([tmp_path / "a", tmp_path / "a2", tmp_path / "b"], use_env=False)
    e, why = idx.available(SONG, "a" * 64, pool)
    assert e is not None and why is None and e.proxy == "other"       # newest finished wins
    e, why = idx.available(SONG, "a" * 64, pool[:-1] + ["card-99999-5-uniq-9999-00"])
    assert e is None and "pool differs" in why and "run-only 1" in why
    e, why = idx.available(SONG, "b" * 64, pool)
    assert e is None and "20 of 126" in why
    e, why = idx.available(SONG, "c" * 64, pool)
    assert e is None and why.startswith("enumeration hash aaaaaaaaaaaa") and "≠ run cccccccccccc" in why
    e, why = idx.available("m9999", "a" * 64, pool)
    assert e is None and "no members-first enumeration on file for m9999" in why
    e, why = idx.available(SONG, None, pool)
    assert e is None and "no inputs_hash" in why
    # without pool ids the hash alone decides
    e, _ = idx.available(SONG, "a" * 64)
    assert e is not None
    # the flat budget is not in the hash: a different one is refused by name
    e, why = idx.available(SONG, "a" * 64, pool, budget=300)
    assert e is None and "enumeration budget 453 ≠ run 300" in why
    assert idx.available(SONG, "a" * 64, pool, budget=453)[0] is not None


def test_env_dir_is_scanned_and_a_missing_env_is_no_dir(monkeypatch, tmp_path, cards):
    monkeypatch.delenv(mfi.ENV_DIRS, raising=False)
    assert mfi.env_dirs() == ()
    monkeypatch.setenv(mfi.ENV_DIRS, os.pathsep.join([str(tmp_path / "x"), str(tmp_path / "y")]))
    assert mfi.env_dirs() == (tmp_path / "x", tmp_path / "y")
    _write_enumeration(tmp_path / "y", [c.id for c in cards], _fake_scores(126), inputs_hash="y" * 64)
    idx = mfi.MembersFirstIndex([tmp_path / "none"])
    assert idx.dirs[-2:] == (tmp_path / "x", tmp_path / "y")
    assert [e.inputs_hash[0] for e in idx.enumerations(SONG)] == ["y"]


def test_load_builds_the_descending_order_and_the_tables_once(tmp_path, cards):
    pool = [c.id for c in cards]
    scores = _fake_scores(126)
    _write_enumeration(tmp_path, pool, scores, inputs_hash="a" * 64)
    idx = mfi.MembersFirstIndex([tmp_path], use_env=False)
    e, _ = idx.available(SONG, "a" * 64, pool)
    loaded = idx.load(e, cards)
    assert list(loaded.scores) == scores
    assert [scores[i] for i in loaded.order] == sorted(scores, reverse=True)
    assert loaded.tables.n == 126 and loaded.tables.pool_ids == tuple(pool)
    assert loaded.tables.roster_ids(0) == tuple(pool[:5])
    assert set(loaded.seconds) == {"read", "order", "tables"}
    assert idx.load(e, cards) is loaded                        # cached per process
    # tables are shared per pool, not per song
    _write_enumeration(tmp_path / "s2", pool, scores, song="m0001", inputs_hash="a" * 64)
    idx2 = mfi.MembersFirstIndex([tmp_path, tmp_path / "s2"], use_env=False)
    l1 = idx2.load(idx2.available(SONG, "a" * 64, pool)[0], cards)
    l2 = idx2.load(idx2.available("m0001", "a" * 64, pool)[0], cards)
    assert l1.tables is l2.tables and l1 is not l2
    # a scores file that disagrees with the meta is refused at load
    _write_enumeration(tmp_path / "bad", pool, scores + [1.0], inputs_hash="d" * 64, n_sets=127)
    idx3 = mfi.MembersFirstIndex([tmp_path / "bad"], use_env=False)
    e3, why = idx3.available(SONG, "d" * 64, pool)
    assert e3 is not None
    with pytest.raises(ValueError):
        idx3.load(e3, cards)


def test_best_for_leaders_returns_the_best_candidate_per_leader_and_reports(tmp_path, cards):
    pool = [c.id for c in cards]
    scores = _fake_scores(126)
    _write_enumeration(tmp_path, pool, scores, inputs_hash="a" * 64)
    idx = mfi.MembersFirstIndex([tmp_path], use_env=False)
    by_id = {c.id: c for c in cards}
    calls: list[tuple[str, int]] = []

    # a fake exact leaf: the leader's own card in the set is worth a lot,
    # otherwise the sum of the pool indices — deterministic, leader-dependent
    def batch(leader, sets):
        calls.append((leader.id, len(sets)))
        return [sum(pool.index(m) for m in ms) + (1000.0 if leader.id in ms else 0.0)
                for ms in sets]

    leaders = [by_id[CALLI_PDW], by_id[NOEL]]
    seen = []
    best, meta = idx.best_for_leaders(SONG, "a" * 64, cards, leaders, 4, batch,
                                      progress=lambda p, t, l: seen.append((p, t, l.id)))
    assert meta["available"] and meta["k"] == 4 and meta["answered"] == 2
    assert meta["enumeration_hash"] == "a" * 64 and meta["proxy"] == "fake"
    assert seen == [(1, 2, CALLI_PDW), (2, 2, NOEL)]
    loaded = idx.load(idx.available(SONG, "a" * 64, pool)[0], cards)
    for ld in leaders:
        cands = mf.candidate_indices(loaded.order, loaded.tables, ld, 4)
        sc, mids, n = best[ld.id]
        assert n == len(cands) and mids in {loaded.tables.roster_ids(i) for i in cands}
        assert sc == max(batch(ld, [loaded.tables.roster_ids(i) for i in cands]))
        # the own-holomem tier put the leader's own card among the candidates
        assert any(ld.id in loaded.tables.roster_ids(i) for i in cands)
    assert [c for c in calls if c[0] == CALLI_PDW][0][1] == best[CALLI_PDW][2]
    assert meta["candidates_scored"] == sum(v[2] for v in best.values())
    # K=0 disables, a wrong hash reports, nothing raises
    best0, meta0 = idx.best_for_leaders(SONG, "a" * 64, cards, leaders, 0, batch)
    assert best0 == {} and not meta0["available"] and "disabled" in meta0["reason"]
    bestx, metax = idx.best_for_leaders(SONG, "x" * 64, cards, leaders, 4, batch)
    assert bestx == {} and not metax["available"] and "≠ run xxxxxxxxxxxx" in metax["reason"]


# ---------------------------------------------------------------------------
# the pipeline: seeds with provenance + the `members_first` meta
# ---------------------------------------------------------------------------
def _pipeline(body: dict) -> dict:
    try:
        return web_server.develop_pipeline(body, art_dir=web_server.Handler.art_dir)
    except web_server._HttpError as exc:
        if exc.code == 503:
            pytest.skip("skilltree/master dumps absent (gitignored)")
        raise


@pytest.fixture
def small_universe(monkeypatch, tmp_path):
    """The run's 5★ universe = POOL9; serial pools; a fresh index reading
    `tmp_path / "enum"` through the env var; the static seeds off."""
    nine = set(POOL9)
    monkeypatch.setattr(web_server, "_is_five_star", lambda c: c.id in nine)
    monkeypatch.setenv("HBUILDER_WEB_WORKERS", "1")
    monkeypatch.setenv(mfi.ENV_DIRS, str(tmp_path / "enum"))
    monkeypatch.setattr(mfi, "_INDEX", None)
    return tmp_path / "enum"


def _body(**kw):
    return {"basis": "maxed", "song": SONG, "pool": "5star", "budget": 453,
            "uptime_model": "finite", "platform": "mobile", "seeds": False,
            "teams": [[CALLI_PDW, *POOL9[:5]], [NOEL, *POOL9[1:6]]], "refine": True,
            **kw}


@pytest.mark.skipif(not _has_dumps(), reason="skilltree/master dumps absent (gitignored)")
def test_seeds_appear_with_provenance_and_meta_on_the_same_hash(small_universe, cards):
    pool = [c.id for c in cards]
    # every-leader on the 9-card universe (no refine, so a seeded row keeps
    # its roster and its provenance); a first run learns the run's own
    # inputs_hash (no enumeration on this pool yet)
    body = _body(leader_diverse=True, refine=False)
    doc0 = _pipeline(body)
    assert doc0["inputs_hash"] and doc0["members_first"]["available"] is False
    # nothing on THIS pool yet; the RESEARCH enumerations stay visible and are
    # refused for their pool or -- once a refresh moves the board/skilltree
    # under them -- for their inputs
    reason0 = doc0["members_first"]["reason"]
    assert "no members-first enumeration" in reason0 or "pool differs" in reason0 \
        or "enumeration differs" in reason0, reason0
    assert "mode" not in doc0["members_first"]
    scores = _fake_scores(126)
    _write_enumeration(small_universe, pool, scores, inputs_hash=doc0["inputs_hash"])
    mfi.default_index().refresh()            # the listing is cached for 2 s
    t0 = time.perf_counter()
    doc = _pipeline({**body, "members_first_k": 2})
    wall = time.perf_counter() - t0
    m = doc["members_first"]
    assert m["available"] is True and m["k"] == 2 and m["song"] == SONG
    assert m["mode"] == "own" and m["source_songs"] == [SONG] and len(m["sources"]) == 1
    assert m["enumeration_hash"] == doc["inputs_hash"] == doc0["inputs_hash"]
    assert m["proxy"] == "fake" and m["leaders"] == 9 and m["answered"] == 9
    assert m["leaders_seeded"] == m["added"] + m["already_seeded"]
    assert m["leaders_seeded"] + m["already_in_beam"] == 9
    assert m["provenance"] == "seed:members-first K=2"
    assert {r["leader"] for r in m["rows"]} == set(POOL9)
    assert m["wall_seconds"] < wall
    # the seeded rows are in the ranking with the provenance, and they are
    # one of the leader's candidate sets under the fake bare order
    idx = mfi.MembersFirstIndex([small_universe], use_env=False)
    loaded = idx.load(idx.available(SONG, doc["inputs_hash"], pool)[0], cards)
    by_id = {c.id: c for c in cards}
    seeded = [r for r in doc["ranking"] if str(r.get("provenance", "")).startswith("seed:members-first")]
    assert len(seeded) == m["added"]
    for r in m["rows"]:
        cands = {loaded.tables.roster_ids(i)
                 for i in mf.candidate_indices(loaded.order, loaded.tables, by_id[r["leader"]], 2)}
        assert tuple(r["members"]) in cands
    for r in seeded:
        assert r["provenance"] == "seed:members-first K=2"
        assert set(r["member_ids"]) <= set(pool) and r["leader_id"] in POOL9
        assert r["certificate"]["status"] in ("HEURISTIC", "EXACT", "BOUNDED")
        assert any(s.startswith("seeds: members-first K=2") for s in r["certificate"]["stages"])
    # no row is listed twice; rows are ranked by the developed per-song score,
    # seeds not privileged
    assert len({(r["leader_id"], frozenset(r["member_ids"])) for r in doc["ranking"]}) \
        == len(doc["ranking"])
    assert [r["song_est"] for r in doc["ranking"]] == sorted(
        (r["song_est"] for r in doc["ranking"]), reverse=True)
    # the refine path (posted teams, refine on) seeds for the posted leaders only
    doc_r = _pipeline(_body(members_first_k=2))
    mr = doc_r["members_first"]
    assert mr["available"] is True and mr["leaders"] == 2 and mr["answered"] == 2
    assert {r["leader"] for r in mr["rows"]} == {CALLI_PDW, NOEL}


@pytest.mark.skipif(not _has_dumps(), reason="skilltree/master dumps absent (gitignored)")
def test_hash_mismatch_seeds_nothing_and_says_why(small_universe, cards):
    _write_enumeration(small_universe, [c.id for c in cards], _fake_scores(126),
                       inputs_hash="0" * 64)
    doc = _pipeline(_body())
    m = doc["members_first"]
    assert m["available"] is False and m["k"] is None        # K unresolved: no source, none posted
    # (the real m0334 enumeration in RESEARCH is listed too — refused on its
    # 66-card pool — so the reason names every candidate's first difference)
    assert ("enumeration hash 000000000000… ≠ run " + doc["inputs_hash"][:12] + "…") in m["reason"]
    assert not any(str(r.get("provenance", "")).startswith("seed:members-first")
                   for r in doc["ranking"])


@pytest.mark.skipif(not _has_dumps(), reason="skilltree/master dumps absent (gitignored)")
def test_k_zero_disables_and_non_song_or_non_5star_runs_say_so(small_universe, cards):
    doc0 = _pipeline(_body())
    _write_enumeration(small_universe, [c.id for c in cards], _fake_scores(126),
                       inputs_hash=doc0["inputs_hash"])
    mfi.default_index().refresh()
    doc = _pipeline(_body(members_first_k=0))
    assert doc["members_first"] == {"available": False, "k": 0,
                                    "reason": "members_first_k is 0 (disabled)"}
    assert not any(str(r.get("provenance", "")).startswith("seed:members-first")
                   for r in doc["ranking"])
    doc = _pipeline(_body(pool="all"))
    assert doc["members_first"]["available"] is False
    assert "not the enumerations' universe" in doc["members_first"]["reason"]
    doc = _pipeline(_body(refine=False))
    assert "neither every-leader" in doc["members_first"]["reason"]
    doc = _pipeline(_body(budget=300))
    assert "enumeration budget 453 ≠ run 300" in doc["members_first"]["reason"]
    # a plain Unit-Score develop carries no `members_first` at all (song mode only)
    doc = _pipeline({**_body(), "song": "", "refine": False})
    assert "members_first" not in doc


@pytest.mark.skipif(not _has_dumps(), reason="skilltree/master dumps absent (gitignored)")
def test_unfinished_enumeration_is_not_used(small_universe, cards):
    doc0 = _pipeline(_body())
    _write_enumeration(small_universe, [c.id for c in cards], _fake_scores(126),
                       inputs_hash=doc0["inputs_hash"], finished=False)
    mfi.default_index().refresh()
    doc = _pipeline(_body())
    assert doc["members_first"]["available"] is False
    assert "not finished" in doc["members_first"]["reason"]


def test_index_module_opens_files_read_only():
    """`test_no_writes.py` walks the package; this is the specific promise:
    the index never opens anything for writing."""
    import ast
    src = (Path(mfi.__file__)).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
            modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
            assert modes and modes[0] == "rb"


# ---------------------------------------------------------------------------
# cross-song TRANSFER: references from other songs' enumerations
# ---------------------------------------------------------------------------
OTHER = "m0139"     # a song that has a chart (the pipeline test runs on it)


def test_sources_prefers_own_else_transfers_from_references_on_the_same_basis(tmp_path, cards):
    pool = [c.id for c in cards]
    n = 126
    h = "a" * 64
    # an enumeration of song A only -> a run on song B transfers from A
    _write_enumeration(tmp_path / "a", pool, _fake_scores(n), song="m0001", inputs_hash=h,
                       extra={"finished": "2026-08-19T02:00:00"})
    idx = mfi.MembersFirstIndex([tmp_path / "a"], use_env=False)
    mode, srcs, why = idx.sources("m0002", h, pool, budget=453)
    assert mode == "transfer" and [e.song for e in srcs] == ["m0001"] and why is None
    assert idx.available("m0002", h, pool)[0] is None              # `available` stays own-only
    # own enumeration present -> own wins, whatever references exist
    _write_enumeration(tmp_path / "b", pool, _fake_scores(n, 2), song="m0002", inputs_hash=h)
    idx = mfi.MembersFirstIndex([tmp_path / "a", tmp_path / "b"], use_env=False)
    mode, srcs, why = idx.sources("m0002", h, pool, budget=453)
    assert mode == "own" and [e.song for e in srcs] == ["m0002"]
    # a reference on another hash / pool / budget / unfinished is refused by name
    _write_enumeration(tmp_path / "c", pool, _fake_scores(n), song="m0003", inputs_hash="c" * 64)
    _write_enumeration(tmp_path / "d", pool[:-1] + ["card-99999-5-uniq-9999-00"], _fake_scores(n),
                       song="m0004", inputs_hash=h)
    _write_enumeration(tmp_path / "e", pool, _fake_scores(n), song="m0005", inputs_hash=h,
                       extra={"budget": 300})
    _write_enumeration(tmp_path / "f", pool, _fake_scores(n), song="m0006", inputs_hash=h,
                       finished=False)
    idx = mfi.MembersFirstIndex([tmp_path / d for d in "cdef"], use_env=False)
    mode, srcs, why = idx.sources("m0009", h, pool, budget=453)
    assert mode is None and srcs == []
    assert "no members-first enumeration on file for m0009" in why
    assert "reference m0003: hash cccccccccccc" in why and "reference m0004: pool differs" in why
    assert "reference m0005: budget 300 ≠ run 453" in why and "reference m0006: enumeration not finished" in why
    # two references on the same basis are both sources (newest first); a
    # third on a different plan_cache_seed is left out (a different leaf)
    _write_enumeration(tmp_path / "g", pool, _fake_scores(n), song="m0007", inputs_hash=h,
                       extra={"finished": "2026-08-19T05:00:00"})
    members_first_store.write_enumeration(
        tmp_path / "h", song="m0008", proxy="fake", pool=pool, scores=_fake_scores(n),
        inputs_hash=h, plan_cache_seed={"leader": NOEL, "members": POOL9[1:6]},
        extra={"budget": 453, "finished": "2026-08-19T06:00:00"})
    idx = mfi.MembersFirstIndex([tmp_path / "a", tmp_path / "g", tmp_path / "h"], use_env=False)
    mode, srcs, why = idx.sources("m0009", h, pool, budget=453)
    assert mode == "transfer" and [e.song for e in srcs] == ["m0007", "m0001"]
    # no hash -> nothing
    assert idx.sources("m0009", None, pool)[0] is None


def test_best_for_leaders_transfer_unions_the_tiers_and_labels_the_mode(tmp_path, cards):
    pool = [c.id for c in cards]
    by_id = {c.id: c for c in cards}
    h = "a" * 64
    s1, s2 = _fake_scores(126, 1), _fake_scores(126, 5)
    _write_enumeration(tmp_path / "a", pool, s1, song="m0001", inputs_hash=h)
    _write_enumeration(tmp_path / "b", pool, s2, song="m0002", inputs_hash=h)
    idx = mfi.MembersFirstIndex([tmp_path / "a", tmp_path / "b"], use_env=False)
    leaders = [by_id[CALLI_PDW], by_id[NOEL]]

    def batch(leader, sets):
        return [sum(pool.index(m) for m in ms) + (1000.0 if leader.id in ms else 0.0) for ms in sets]

    best, meta = idx.best_for_leaders("m0009", h, cards, leaders, None, batch, budget=453)
    assert meta["available"] and meta["mode"] == "transfer" and meta["k"] == mfi.DEFAULT_K_TRANSFER
    assert sorted(meta["source_songs"]) == ["m0001", "m0002"]
    assert {s["song"] for s in meta["sources"]} == {"m0001", "m0002"} and "TRANSFERRED" in meta["label"]
    assert isinstance(meta["measured_recall"], dict) and meta["measured_recall"] == {}
    assert "no members_first_*_transfer.json" in meta["measured_recall_note"]
    # the candidates are the UNION over both references' tiers (at K=3)
    best3, meta3 = idx.best_for_leaders("m0009", h, cards, leaders, 3, batch, budget=453)
    l1 = idx.load(idx.available("m0001", h, pool)[0], cards)
    l2 = idx.load(idx.available("m0002", h, pool)[0], cards)
    for ld in leaders:
        c1 = {l1.tables.roster_ids(i) for i in mf.candidate_indices(l1.order, l1.tables, ld, 3)}
        c2 = {l2.tables.roster_ids(i) for i in mf.candidate_indices(l2.order, l2.tables, ld, 3)}
        sc, mids, n = best3[ld.id]
        assert n == len(c1 | c2) and mids in (c1 | c2)
        assert sc == max(batch(ld, sorted(c1 | c2)))
    assert meta3["k"] == 3 and meta3["load_seconds"].keys() == {"m0001", "m0002"}
    # own enumeration present -> own, K default 2000
    _write_enumeration(tmp_path / "own", pool, s1, song="m0009", inputs_hash=h)
    idx = mfi.MembersFirstIndex([tmp_path / "a", tmp_path / "own"], use_env=False)
    _best, meta_o = idx.best_for_leaders("m0009", h, cards, leaders, None, batch, budget=453)
    assert meta_o["mode"] == "own" and meta_o["k"] == mfi.DEFAULT_K and meta_o["source_songs"] == ["m0009"]
    # nothing on file at all
    idx = mfi.MembersFirstIndex([tmp_path / "none"], use_env=False)
    b0, m0 = idx.best_for_leaders("m0009", h, cards, leaders, None, batch)
    assert b0 == {} and m0["available"] is False and m0["k"] is None
    assert "no members-first enumeration on file for m0009" in m0["reason"]


def test_measured_recall_reads_the_transfer_reports_on_file_and_invents_nothing(tmp_path):
    members_first_store.write_text_file(tmp_path / "members_first_m0139_transfer.json", json.dumps({
        "song": "m0139", "sources": ["m0334"], "k_tiers": [100, 500, 2000],
        "summary": {"100": {"rows": 6, "zero_loss": 4, "max_loss": 0.0038, "same_roster": 4},
                    "500": {"rows": 6, "zero_loss": 6, "max_loss": 0.0, "same_roster": 6},
                    "2000": {"rows": 6, "zero_loss": 6, "max_loss": 0.0, "same_roster": 6}}}))
    members_first_store.write_text_file(tmp_path / "members_first_m0001_transfer.json", "{broken")
    idx = mfi.MembersFirstIndex([tmp_path], use_env=False)
    reps = idx.transfer_reports()
    assert len(reps) == 1 and reps[0]["song"] == "m0139" and sorted(reps[0]["by_k"]) == [100, 500, 2000]
    assert idx.measured_recall(500) == {"m0139 from m0334": "6/6 rows at K=500, 0 loss"}
    assert idx.measured_recall(100) == {"m0139 from m0334": "4/6 rows at K=100, max loss 0.38%"}
    # a K the report did not measure -> every K it has
    assert idx.measured_recall(7)["m0139 from m0334"].count("rows at K=") == 3
    assert mfi.MembersFirstIndex([tmp_path / "none"], use_env=False).measured_recall(500) == {}


@pytest.mark.skipif(not _has_dumps(), reason="skilltree/master dumps absent (gitignored)")
def test_pipeline_transfers_from_another_songs_enumeration_and_says_so(small_universe, cards):
    pool = [c.id for c in cards]
    body = _body(leader_diverse=True, refine=False)
    doc0 = _pipeline(body)
    assert doc0["members_first"]["available"] is False
    # an enumeration of ANOTHER song on this run's hash / pool / budget
    _write_enumeration(small_universe, pool, _fake_scores(126), song=OTHER,
                       inputs_hash=doc0["inputs_hash"])
    mfi.default_index().refresh()
    doc = _pipeline({**body, "members_first_k": 2})
    m = doc["members_first"]
    assert m["available"] is True, m
    assert m["mode"] == "transfer" and m["k"] == 2
    assert m["song"] == SONG and m["source_songs"] == [OTHER]
    assert m["sources"][0]["song"] == OTHER and m["enumeration"]["song"] == OTHER
    assert m["leaders"] == 9 and m["answered"] == 9
    assert m["provenance"] == f"seed:members-first K=2 transfer:{OTHER}"
    assert "TRANSFERRED" in m["label"] and isinstance(m["measured_recall"], dict)
    seeded = [r for r in doc["ranking"] if str(r.get("provenance", "")).startswith("seed:members-first")]
    assert len(seeded) == m["added"] > 0
    for r in seeded:
        assert r["provenance"] == f"seed:members-first K=2 transfer:{OTHER}"
        assert r["certificate"]["status"] in ("HEURISTIC", "EXACT", "BOUNDED")
        assert any(s.startswith("seeds: members-first K=2") and f"of {OTHER} TRANSFERRED" in s
                   for s in r["certificate"]["stages"])
    # the objective's plan-cache seed: the per-leader table's when it matches
    # this run (m0334's does, on the real tree), else the reference's
    src = doc["objective"]["seed_source"]
    assert src.startswith("per-leader table") or src.startswith(
        f"members-first enumeration of {OTHER} (same inputs_hash, transfer reference)")
    # absent K -> the transfer default
    doc2 = _pipeline(body)
    assert doc2["members_first"]["mode"] == "transfer"
    assert doc2["members_first"]["k"] == mfi.DEFAULT_K_TRANSFER
    # the song's own enumeration, once on file, is preferred (K default 2000)
    _write_enumeration(small_universe, pool, _fake_scores(126, 3), song=SONG,
                       inputs_hash=doc0["inputs_hash"])
    mfi.default_index().refresh()
    doc3 = _pipeline(body)
    assert doc3["members_first"]["mode"] == "own" and doc3["members_first"]["k"] == mfi.DEFAULT_K
    assert doc3["members_first"]["source_songs"] == [SONG]


# ---------------------------------------------------------------------------
# the enumeration's OWN key: a new song in charts.json retires
# nothing; a changed card retires by name; legacy metas keep the whole hash
# ---------------------------------------------------------------------------
_PER = {"cards": "c" * 64, "board": "b" * 64, "charts": "0" * 64, "skilltree": "s" * 64,
        "master_stn": "m" * 64, "collection": "k" * 64, "cfg": "f" * 64}
MV = "objective-v2.2"


def _has_charts() -> bool:
    return cert_mod.CHARTS_PATH.is_file()


def _keyed_extra(song: str, per: dict = _PER, *, charts_path=None, finished="2026-08-19T02:00:00"):
    """The meta fields the enumerator stamps (`build()` / `rekey`): per-file
    digests + the song's chart digest -> enumeration_key / reference_key."""
    cd = cert_mod.chart_digest(song, charts_path=charts_path)
    assert cd, f"no chart for {song}"
    return {"inputs_hash_files": dict(per), "chart_digest": cd,
            "enumeration_key": cert_mod.enumeration_key(per, cd, MV),
            "reference_key": cert_mod.reference_key(per, MV), "finished": finished}


@pytest.fixture
def charts_plus_one(tmp_path):
    """A scratch charts.json = the real one + ONE fake chart (m9998/EXPERT, a
    copy of m0334's entry); the path the index digests the run song's chart
    from (`certificate.CHARTS_PATH`) is pointed at it by the tests."""
    if not _has_charts():
        pytest.skip("charts.json absent (gitignored)")
    raw = json.loads(cert_mod.CHARTS_PATH.read_bytes().decode("utf-8"))
    fake = dict(raw["charts"]["m0334/EXPERT"])
    fake["music_id"] = "m9998"
    raw["charts"]["m9998/EXPERT"] = fake
    path = tmp_path / "charts_plus_one" / "charts.json"
    members_first_store.write_text_file(path, json.dumps(raw))
    return path


def test_enumeration_key_survives_a_new_song_in_charts_json(tmp_path, monkeypatch, cards,
                                                             charts_plus_one):
    pool = [c.id for c in cards]
    n = 126
    # two KEYED enumerations (m0334, m0139), stamped against the REAL charts.json
    # (the one they were "scored on"), whole hash "a"*64
    _write_enumeration(tmp_path / "a", pool, _fake_scores(n), song="m0334", inputs_hash="a" * 64,
                       extra=_keyed_extra("m0334", finished="2026-08-19T09:53:20"))
    _write_enumeration(tmp_path / "b", pool, _fake_scores(n, 3), song="m0139", inputs_hash="a" * 64,
                       extra=_keyed_extra("m0139", finished="2026-08-19T13:37:37"))
    idx = mfi.MembersFirstIndex([tmp_path / "a", tmp_path / "b"], use_env=False)
    e334 = idx.enumerations("m0334")[0]
    assert not e334.legacy and e334.reference_key == idx.enumerations("m0139")[0].reference_key
    assert e334.enumeration_key != idx.enumerations("m0139")[0].enumeration_key   # the chart differs
    assert e334.summary()["guard"] == "enumeration_key"
    # THE DAY A SONG LANDS: charts.json gains m9998 -> the run's whole
    # inputs_hash changes ("a" -> "b"), its charts digest changes; the per-file
    # digests of everything else are the same
    monkeypatch.setattr(cert_mod, "CHARTS_PATH", charts_plus_one)
    run_per = {**_PER, "charts": "1" * 64}
    new_hash = "b" * 64
    # own sources on the old songs: still OWN
    mode, srcs, why = idx.sources("m0334", new_hash, pool, budget=453,
                                  inputs_hash_files=run_per, model_version=MV)
    assert (mode, [e.song for e in srcs], why) == ("own", ["m0334"], None)
    mode, srcs, why = idx.sources("m0139", new_hash, pool, budget=453,
                                  inputs_hash_files=run_per, model_version=MV)
    assert (mode, [e.song for e in srcs], why) == ("own", ["m0139"], None)
    # the NEW song, and any third song: TRANSFER from both (newest first)
    for song in ("m9998", "m0144"):
        mode, srcs, why = idx.sources(song, new_hash, pool, budget=453,
                                      inputs_hash_files=run_per, model_version=MV)
        assert (mode, [e.song for e in srcs], why) == ("transfer", ["m0139", "m0334"], None), song
    # the run's keys are reported; the whole hash is kept for reference
    rk = idx.run_keys("m0334", new_hash, run_per, MV)
    assert rk.enumeration_key == e334.enumeration_key and rk.reference_key == e334.reference_key  # gitleaks:allow (cache keys, not secrets)
    assert rk.inputs_hash == new_hash and rk.summary()["guard"] == "enumeration_key"
    # `available` agrees (own-only), and the whole-hash call (no per-file
    # digests) refuses — the fallback, with the reason saying so
    assert idx.available("m0334", new_hash, pool, inputs_hash_files=run_per, model_version=MV)[0] is e334
    e, why = idx.available("m0334", new_hash, pool)
    assert e is None and "whole-hash guard: run carries no per-file digests" in why
    # best_for_leaders through the same guard labels it
    by_id = {c.id: c for c in cards}
    best, meta = idx.best_for_leaders("m0144", new_hash, cards, [by_id[CALLI_PDW]], 3,
                                      lambda ld, sets: [1.0] * len(sets), budget=453,
                                      inputs_hash_files=run_per, model_version=MV)
    assert meta["available"] and meta["mode"] == "transfer" and meta["guard"] == "reference_key"
    assert meta["run_keys"]["reference_key"] == e334.reference_key and CALLI_PDW in best
    best, meta = idx.best_for_leaders("m0334", new_hash, cards, [by_id[CALLI_PDW]], 3,
                                      lambda ld, sets: [1.0] * len(sets), budget=453,
                                      inputs_hash_files=run_per, model_version=MV)
    assert meta["mode"] == "own" and meta["guard"] == "enumeration_key"


def test_a_changed_card_or_chart_retires_by_name(tmp_path, monkeypatch, cards, charts_plus_one):
    pool = [c.id for c in cards]
    n = 126
    _write_enumeration(tmp_path / "a", pool, _fake_scores(n), song="m0334", inputs_hash="a" * 64,
                       extra=_keyed_extra("m0334"))
    _write_enumeration(tmp_path / "b", pool, _fake_scores(n, 3), song="m0139", inputs_hash="a" * 64,
                       extra=_keyed_extra("m0139"))
    idx = mfi.MembersFirstIndex([tmp_path / "a", tmp_path / "b"], use_env=False)
    # a CARD changed (cards.json digest differs): own AND transfer refused, the file named
    per_cards = {**_PER, "cards": "d" * 64}
    mode, srcs, why = idx.sources("m0334", "z" * 64, pool, budget=453,
                                  inputs_hash_files=per_cards, model_version=MV)
    assert mode is None and srcs == []
    assert "enumeration differs in data/cards.json" in why
    assert "reference m0139: differs in data/cards.json" in why
    # board + cfg changed: both named
    per_two = {**_PER, "board": "e" * 64, "cfg": "g" * 64}
    mode, srcs, why = idx.sources("m0144", "z" * 64, pool, budget=453,
                                  inputs_hash_files=per_two, model_version=MV)
    assert mode is None and "differs in tools/recon/proxy/dumps/board.json, cfg" in why
    # a different model version: named
    mode, srcs, why = idx.sources("m0334", "a" * 64, pool, budget=453,
                                  inputs_hash_files=_PER, model_version="objective-v9")
    assert mode is None and "model_version objective-v2.2 ≠ objective-v9" in why
    # the CHART of m0334 itself changed (re-charted): its own enumeration is
    # retired naming the chart — but it stays a TRANSFER reference for other
    # songs, and m0139's enumeration still serves m0334 by transfer
    raw = json.loads(charts_plus_one.read_bytes().decode("utf-8"))
    raw["charts"]["m0334/EXPERT"]["notes"] = list(raw["charts"]["m0334/EXPERT"]["notes"])[:-1]
    recharted = tmp_path / "recharted" / "charts.json"
    members_first_store.write_text_file(recharted, json.dumps(raw))
    monkeypatch.setattr(cert_mod, "CHARTS_PATH", recharted)
    mode, srcs, why = idx.sources("m0334", "y" * 64, pool, budget=453,
                                  inputs_hash_files=_PER, model_version=MV)
    assert mode == "transfer" and [e.song for e in srcs] == ["m0139"]
    e, why = idx.available("m0334", "y" * 64, pool, inputs_hash_files=_PER, model_version=MV)
    assert e is None and "differs in the chart of m0334 in charts.json" in why
    mode, srcs, why = idx.sources("m0144", "y" * 64, pool, budget=453,
                                  inputs_hash_files=_PER, model_version=MV)
    assert mode == "transfer" and sorted(e.song for e in srcs) == ["m0139", "m0334"]
    # a song with no chart at all: no own key (said so), transfer still works
    mode, srcs, why = idx.sources("m9999", "y" * 64, pool, budget=453,
                                  inputs_hash_files=_PER, model_version=MV)
    assert mode == "transfer"
    assert idx.run_keys("m9999", "y" * 64, _PER, MV).fallback.startswith("no EXPERT chart")


def test_legacy_meta_without_key_falls_back_to_the_whole_hash_and_says_so(tmp_path, cards,
                                                                           charts_plus_one):
    pool = [c.id for c in cards]
    n = 126
    # a legacy meta (no enumeration_key) on hash "a"; a keyed one on m0139
    _write_enumeration(tmp_path / "a", pool, _fake_scores(n), song="m0334", inputs_hash="a" * 64)
    _write_enumeration(tmp_path / "b", pool, _fake_scores(n, 3), song="m0139", inputs_hash="a" * 64,
                       extra=_keyed_extra("m0139"))
    idx = mfi.MembersFirstIndex([tmp_path / "a", tmp_path / "b"], use_env=False)
    legacy = idx.enumerations("m0334")[0]
    assert legacy.legacy and legacy.summary()["guard"] == "whole-hash (legacy meta)"
    # same whole hash -> usable (own), with or without per-file digests
    assert idx.available("m0334", "a" * 64, pool)[0] is legacy
    assert idx.available("m0334", "a" * 64, pool, inputs_hash_files=_PER, model_version=MV)[0] is legacy
    # a new song landed (hash "b"): the legacy meta IS retired — the keyed one is not
    mode, srcs, why = idx.sources("m0334", "b" * 64, pool, budget=453,
                                  inputs_hash_files=_PER, model_version=MV)
    assert mode == "transfer" and [e.song for e in srcs] == ["m0139"]
    e, why = idx.available("m0334", "b" * 64, pool, inputs_hash_files=_PER, model_version=MV)
    assert e is None and "whole-hash guard: legacy meta without enumeration_key" in why
    assert "hash aaaaaaaaaaaa… ≠ run bbbbbbbbbbbb…" in why
    # as a reference the legacy meta is refused the same way, by name
    mode, srcs, why = idx.sources("m0144", "b" * 64, pool, budget=453,
                                  inputs_hash_files=_PER, model_version=MV)
    assert mode == "transfer" and [e.song for e in srcs] == ["m0139"]
    best, meta = idx.best_for_leaders("m0334", "a" * 64, cards, [cards[0]], 3,
                                      lambda ld, sets: [1.0] * len(sets), budget=453,
                                      inputs_hash_files=_PER, model_version=MV)
    assert meta["available"] and meta["guard"] == "whole-hash (legacy meta)"
    # no hash, no digests -> nothing, the whole-hash message
    assert idx.sources("m0334", None, pool)[2].startswith("this run carries no inputs_hash")


def test_the_real_metas_on_disk_are_keyed_and_agree_with_the_current_files():
    """The two enumerations in RESEARCH (m0334, m0139) were stamped by
    `rekey`: their keys recompute from their own stored per-file digests and
    chart digest, and an enumeration whose chart has since moved is refused
    rather than served. Whether the two share one basis or their charts have
    moved is a fact about the data on disk, so it is not asserted."""
    idx = mfi.MembersFirstIndex(use_env=False)
    real = {e.song: e for e in idx.enumerations()
            if e.proxy == "nonsinger" and e.song in ("m0334", "m0139")}
    if len(real) < 2:
        pytest.skip("the RESEARCH metas are not on this checkout")
    if not _has_charts():
        pytest.skip("charts.json absent (gitignored)")
    for e in real.values():
        assert not e.legacy and e.inputs_hash_files and e.chart_digest
        assert cert_mod.reference_key(e.inputs_hash_files, e.model_version) == e.reference_key
        assert cert_mod.enumeration_key(e.inputs_hash_files, e.chart_digest,
                                        e.model_version) == e.enumeration_key
        # A refresh can move the chart under an enumeration. That is not a
        # failure of the enumeration; serving it anyway would be. So: on the
        # very files it was stamped with, a moved chart must make it refused.
        if cert_mod.chart_digest(e.song) != e.chart_digest:
            got, why = idx.available(e.song, "x" * 64, e.pool,
                                     inputs_hash_files=e.inputs_hash_files,
                                     model_version=e.model_version)
            assert got is None and why, (e.song, why)
    # Two songs never share an enumeration key. Whether they share a
    # reference_key depends on when each was enumerated -- a fact about the
    # data on disk, not about the code -- so it is not asserted.
    assert real["m0334"].enumeration_key != real["m0139"].enumeration_key


@pytest.mark.skipif(not _has_dumps(), reason="skilltree/master dumps absent (gitignored)")
def test_pipeline_uses_a_keyed_enumeration_whose_whole_hash_differs(small_universe, cards):
    """The server passes its per-file digests to the index: an enumeration
    stamped with the run's enumeration_key is used even when its whole
    `inputs_hash` is another one (the day a song lands), and the response
    names the guard; a legacy meta on that other hash is not."""
    pool = [c.id for c in cards]
    body = _body(leader_diverse=True, refine=False)
    doc0 = _pipeline(body)
    per = doc0.get("inputs_hash_files")
    assert per and set(cert_mod.REFERENCE_KEY_LINES) <= set(per)
    # the song's own enumeration, keyed on this run's digests, whole hash "0"*64
    _write_enumeration(small_universe, pool, _fake_scores(126), inputs_hash="0" * 64,
                       extra=_keyed_extra(SONG, per))
    mfi.default_index().refresh()
    doc = _pipeline({**body, "members_first_k": 2})
    m = doc["members_first"]
    assert m["available"] is True and m["mode"] == "own" and m["guard"] == "enumeration_key", m
    assert m["enumeration_hash"] == "0" * 64 != doc["inputs_hash"]
    assert m["run_keys"]["enumeration_key"] == m["enumeration"]["enumeration_key"]
    assert doc["objective"]["seed_source"].startswith("per-leader table") or \
        "same enumeration_key" in doc["objective"]["seed_source"]
    # a reference of another song keyed the same way, whole hash "0"*64: transfer
    _write_enumeration(small_universe / "ref", pool, _fake_scores(126, 5), song=OTHER,
                       inputs_hash="0" * 64, extra=_keyed_extra(OTHER, per))
    import os as _os
    _os.environ[mfi.ENV_DIRS] = _os.pathsep.join([str(small_universe), str(small_universe / "ref")])
    mfi._INDEX = None
    doc2 = _pipeline({**_body(leader_diverse=True, refine=False, song=OTHER), "members_first_k": 2})
    m2 = doc2["members_first"]
    assert m2["available"] and m2["mode"] == "own" and m2["source_songs"] == [OTHER]
    doc3 = _pipeline({**_body(leader_diverse=True, refine=False, song="m0144"), "members_first_k": 2})
    m3 = doc3["members_first"]
    assert m3["available"] and m3["mode"] == "transfer" and m3["guard"] == "reference_key"
    assert sorted(m3["source_songs"]) == sorted([SONG, OTHER])
