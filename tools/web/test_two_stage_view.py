"""`two_stage_view.payload` — what `GET /api/two-stage` returns from the answers
cache. Held to: it never calls an answer certified, it tells fresh from stale
from missing by the KEY and nothing else, and it never reads outside the cache
directory. No scoring; the cache files here are written by hand."""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import two_stage_view as view  # noqa: E402

# Fixture files go through the BUILDER's own writer (`two_stage_build._write`,
# under tools/recon/proxy): nothing in tools/web writes to disk, tests included
# (`test_no_writes.py`), and it is the writer the real cache is made with.
_put = view.build._write


def _rows(n: int) -> list[dict]:
    return [{"leader": f"card-{i}", "leader_name": f"Leader {i}", "sings": i == 0,
             "status": "searched" if i < 2 else "triage", "best": 3_000_000.0 - i,
             "members": [f"m{i}{k}" for k in range(5)], "member_names": [f"N{k}" for k in range(5)],
             "private": "not for the page"} for i in range(n)]


def _cache(tmp_path: Path) -> Path:
    _put(tmp_path / "song_m0001.json", {
        "key": "key-m0001", "song": "m0001", "singers": ["chr-00001"], "singer_type": "solo",
        "written": "2026-10-13T19:00:00", "method": "two-stage search",
        "stats": {"searched": 2, "leaders": 84, "legal_sets": 28250016, "exact_evals": 72358},
        "rows": _rows(20)})
    _put(tmp_path / "general.json", {
        "key": "key-general", "charts": 215, "objective": "mean over every chart",
        "stats": {"searched": 12, "leaders": 84, "legal_sets": 28250016, "sample_exact_evals": 161135},
        "rows": _rows(3)})
    return tmp_path


def _keys(song_key="key-m0001", general_key="key-general"):
    return lambda: ({"m0001": song_key, "m0002": "key-m0002"}, general_key)


def test_a_song_answer_is_fresh_only_while_its_key_is_todays_key(tmp_path):
    cache = _cache(tmp_path)
    fresh = view.payload("m0001", keys=_keys(), cache_dir=cache)
    assert fresh["state"] == "fresh" and fresh["scope"] == "song" and fresh["certified"] is False
    assert fresh["rows"][0]["leader_name"] == "Leader 0" and fresh["rows"][0]["sings"] is True
    assert len(fresh["rows"]) == view.ROWS and "private" not in fresh["rows"][0]
    assert fresh["searched_leaders"] == 2 and fresh["exact_evals"] == 72358
    stale = view.payload("m0001", keys=_keys(song_key="another"), cache_dir=cache)
    assert stale["state"] == "stale" and stale["rows"] and stale["certified"] is False
    unverified = view.payload("m0001", keys=lambda: None, cache_dir=cache)
    assert unverified["state"] == "unverified"          # the inputs could not be read: no claim


def test_no_song_is_the_general_answer_with_its_own_key(tmp_path):
    cache = _cache(tmp_path)
    general = view.payload("", keys=_keys(), cache_dir=cache)
    assert general["scope"] == "general" and general["state"] == "fresh" and general["charts"] == 215
    assert general["exact_evals"] == 161135 and len(general["rows"]) == 3
    assert view.payload(None, keys=_keys(general_key="moved"), cache_dir=cache)["state"] == "stale"


def test_a_song_with_no_file_is_missing_and_a_bad_id_never_touches_the_disk(tmp_path):
    cache = _cache(tmp_path)
    _put(tmp_path.parent / "outside.json", {"key": "x", "rows": _rows(1)})
    assert view.payload("m0002", keys=_keys(), cache_dir=cache)["state"] == "missing"
    for bad in ("../outside", "m0001/../../x", "m0001.json", "a b"):
        out = view.payload(bad, keys=_keys(), cache_dir=cache)
        assert out["state"] == "missing" and "rows" not in out


def test_the_build_status_is_running_only_while_its_process_is_alive(tmp_path):
    cache = _cache(tmp_path)
    _put(cache / "status.json",
         {"pid": os.getpid(), "state": "running", "stage": "songs", "done": 7, "total": 40})
    build = view.payload("m0001", keys=_keys(), cache_dir=cache)["build"]
    assert build == {"running": True, "stage": "songs", "basis": None, "done": 7, "total": 40,
                     "last_state": "running", "updated": None}
    _put(cache / "status.json",
         {"pid": 0, "state": "running", "stage": "songs", "done": 7, "total": 40})
    dead = view.payload("m0001", keys=_keys(), cache_dir=cache)["build"]
    assert dead["running"] is False and dead["done"] is None and dead["last_state"] == "running"


def test_each_basis_reads_its_own_files_and_an_unknown_basis_is_the_maxed_one(tmp_path):
    cache = _cache(tmp_path)
    _put(cache / "account_song_m0001.json", {
        "key": "key-account", "song": "m0001", "basis": "account", "stats": {},
        "rows": [dict(_rows(1)[0], leader_name="Owned leader")]})
    maxed = view.payload("m0001", keys=_keys(), cache_dir=cache)
    assert maxed["basis_id"] == "maxed" and maxed["rows"][0]["leader_name"] == "Leader 0"
    account = view.payload("m0001", basis="account", keys=_keys(song_key="key-account"), cache_dir=cache)
    assert account["basis_id"] == "account" and account["state"] == "fresh"
    assert account["rows"][0]["leader_name"] == "Owned leader" and account["basis"] != maxed["basis"]
    # the maxed key does not vouch for the account answer, and the other way round
    assert view.payload("m0001", basis="account", keys=_keys(), cache_dir=cache)["state"] == "stale"
    assert view.payload("", basis="account", keys=_keys(), cache_dir=cache)["state"] == "missing"
    assert view.payload("m0001", basis="../x", keys=_keys(), cache_dir=cache)["basis_id"] == "maxed"


def test_the_per_singer_answers_are_vouched_for_by_their_own_key(tmp_path):
    cache = _cache(tmp_path)
    assert view.singers_payload(keys=_keys(), cache_dir=cache)["state"] == "missing"
    _put(cache / "singers.json", {
        "key": view.build.singers_key("key-general"), "basis": "maxed",
        "rows": [{"chr": "chr-00001", "name": "A", "songs": ["m0001", "m0002"], "leader": "card-1",
                  "leader_name": "A / X", "best": 3_100_000.0, "members": ["m1"], "member_names": ["N1"],
                  "per_song": {"m0001": 1.0}}]})
    fresh = view.singers_payload(keys=_keys(), cache_dir=cache)
    assert fresh["state"] == "fresh" and fresh["certified"] is False and fresh["scope"] == "singers"
    assert fresh["rows"] == [{"chr": "chr-00001", "name": "A", "songs": 2, "leader": "card-1",
                              "leader_name": "A / X", "best": 3_100_000.0, "members": ["m1"],
                              "member_names": ["N1"]}]
    assert view.singers_payload(keys=_keys(general_key="moved"), cache_dir=cache)["state"] == "stale"
    assert view.singers_payload(basis="account", keys=_keys(), cache_dir=cache)["state"] == "missing"


def test_the_max_level_basis_reads_its_own_files(tmp_path):
    cache = _cache(tmp_path)
    _put(cache / "accountmax_general.json", {"key": "key-max", "basis": "account-max", "stats": {},
                                             "rows": [dict(_rows(1)[0], leader_name="Max leader")]})
    out = view.payload("", basis="account-max", keys=_keys(general_key="key-max"), cache_dir=cache)
    assert out["basis_id"] == "account-max" and out["state"] == "fresh"
    assert out["rows"][0]["leader_name"] == "Max leader" and "max level" in out["basis"]
    assert view.payload("", basis="account", keys=_keys(general_key="key-max"), cache_dir=cache)["state"] == "missing"


def test_rows_carry_their_slot_order_when_the_builder_wrote_it(tmp_path):
    rows = _rows(3)
    rows[0]["member_order"] = list(reversed(rows[0]["members"]))
    rows[0]["refined"] = True
    _put(tmp_path / "song_m0001.json", {"key": "k", "song": "m0001", "rows": rows})
    got = view.payload("m0001", keys=lambda: ({"m0001": "k"}, "g"), cache_dir=tmp_path)
    assert got["rows"][0]["member_order"] == rows[0]["member_order"]
    assert got["rows"][0]["refined"] is True
    assert got["rows"][1]["member_order"] is None      # an older file: no order
