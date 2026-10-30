"""`two_stage_build.py`: the keys that decide what a refresh recomputes, and the
lock that keeps it to one builder. The searches themselves are covered by
`tools/web/test_two_stage.py`; nothing here scores a team."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import two_stage_build as build  # noqa: E402


def test_a_new_song_changes_only_its_own_key_and_the_general_one():
    digests = {"m0001": "a" * 64, "m0002": "b" * 64}
    songs, general = build.keys_for("ref", digests)
    more, general_more = build.keys_for("ref", {**digests, "m0003": "c" * 64})
    assert more["m0001"] == songs["m0001"] and more["m0002"] == songs["m0002"]
    assert "m0003" in more and general_more != general


def test_a_changed_chart_changes_that_song_and_general_and_nothing_else():
    digests = {"m0001": "a" * 64, "m0002": "b" * 64}
    songs, general = build.keys_for("ref", digests)
    edited, general_edited = build.keys_for("ref", {**digests, "m0002": "d" * 64})
    assert edited["m0001"] == songs["m0001"]
    assert edited["m0002"] != songs["m0002"] and general_edited != general


def test_a_changed_card_or_model_changes_every_key():
    digests = {"m0001": "a" * 64, "m0002": "b" * 64}
    songs, general = build.keys_for("ref", digests)
    moved, general_moved = build.keys_for("another-reference", digests)
    assert all(moved[s] != songs[s] for s in digests) and general_moved != general


def test_the_builder_version_and_settings_are_part_of_every_key(monkeypatch):
    digests = {"m0001": "a" * 64}
    songs, general = build.keys_for("ref", digests)
    monkeypatch.setattr(build, "VERSION", build.VERSION + "x")
    bumped, general_bumped = build.keys_for("ref", digests)
    assert bumped != songs and general_bumped != general
    monkeypatch.undo()
    monkeypatch.setitem(build.SETTINGS, "margin", 0.5)
    assert build.keys_for("ref", digests)[0] != songs


def test_chart_digests_read_both_file_shapes_and_only_expert(tmp_path):
    entries = [{"music_id": "m0001", "difficulty": "EXPERT", "notes": [1, 2]},
               {"music_id": "m0001", "difficulty": "HARD", "notes": [1]},
               {"music_id": "m0002", "difficulty": "EXPERT", "notes": [3]}]
    as_list, as_dict = tmp_path / "list.json", tmp_path / "dict.json"
    as_list.write_text(json.dumps({"charts": entries}), encoding="utf-8")
    as_dict.write_text(json.dumps({"charts": {f"{e['music_id']}|{e['difficulty']}": e
                                              for e in entries}}), encoding="utf-8")
    digests = build.chart_digests(as_list)
    assert set(digests) == {"m0001", "m0002"} and digests == build.chart_digests(as_dict)
    entries[0]["notes"] = [1, 2, 3]
    as_list.write_text(json.dumps({"charts": entries}), encoding="utf-8")
    changed = build.chart_digests(as_list)
    assert changed["m0001"] != digests["m0001"] and changed["m0002"] == digests["m0002"]


def test_one_builder_at_a_time_and_a_dead_builders_lock_is_taken_over(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "CACHE", tmp_path)
    monkeypatch.setattr(build, "LOCK", tmp_path / "builder.lock")
    assert build._alive(os.getpid()) and not build._alive(0)
    assert build.acquire_lock()                                   # nobody holds it
    assert build.acquire_lock()                                   # our own lock is ours
    build.LOCK.write_text(json.dumps({"pid": os.getppid()}), encoding="utf-8")
    assert build._alive(os.getppid()) and not build.acquire_lock()   # a live other process
    dead = 2 ** 22 + 12344
    while build._alive(dead):
        dead += 4
    build.LOCK.write_text(json.dumps({"pid": dead}), encoding="utf-8")
    assert build.acquire_lock()
    assert json.loads(build.LOCK.read_text("utf-8"))["pid"] == os.getpid()


def test_a_cache_file_is_written_whole_or_not_at_all(tmp_path):
    target = tmp_path / "song_m0001.json"
    build._write(target, {"key": "k", "rows": [{"leader": "x"}]})
    assert build._read(target) == {"key": "k", "rows": [{"leader": "x"}]}
    assert not list(tmp_path.glob("*.tmp"))
    target.write_text("{not json", encoding="utf-8")
    assert build._read(target) is None and build._read(tmp_path / "missing.json") is None


def test_the_account_basis_has_its_own_keys_and_files_and_leaves_the_maxed_ones_alone():
    digests = {"m0001": "a" * 64, "m0002": "b" * 64}
    maxed = build.keys_for("ref", digests)
    assert build.keys_for("ref", digests, "maxed") == maxed          # the cache built before stays valid
    account = build.keys_for("ref", digests, "account")
    assert account[1] != maxed[1] and all(account[0][s] != maxed[0][s] for s in digests)
    assert build.song_file("m0001") == "song_m0001.json" and build.general_file() == "general.json"
    assert build.song_file("m0001", "account") == "account_song_m0001.json"
    assert build.general_file("account") == "account_general.json"


# -- difficulty (the rest lives in tools/web/test_difficulty.py) --
def test_chart_digests_pick_the_requested_difficulty(tmp_path):
    entries = {"m0001/EXPERT": {"music_id": "m0001", "difficulty": "EXPERT", "notes": [1, 2]},
               "m0001/HARD": {"music_id": "m0001", "difficulty": "HARD", "notes": [1]}}
    path = tmp_path / "charts.json"
    path.write_text(json.dumps({"charts": entries}), encoding="utf-8")
    expert, hard = build.chart_digests(path), build.chart_digests(path, "HARD")
    assert expert == build.chart_digests(path, "EXPERT") and expert["m0001"] != hard["m0001"]


def test_two_stage_view_reads_the_difficultys_own_file(tmp_path):
    sys.path.insert(0, str(HERE.parents[2] / "tools" / "web"))
    import two_stage_view  # noqa: PLC0415

    (tmp_path / "song_m0001.json").write_text(json.dumps({"key": "k-expert", "rows": []}),
                                              encoding="utf-8")
    (tmp_path / "song_m0001_hard.json").write_text(json.dumps({"key": "k-hard", "rows": []}),
                                                   encoding="utf-8")
    keys = {"EXPERT": ({"m0001": "k-expert"}, "g"), "HARD": ({"m0001": "k-hard"}, "g")}
    out = two_stage_view.payload("m0001", keys=lambda: keys["EXPERT"], cache_dir=tmp_path)
    assert out["state"] == "fresh" and out["difficulty"] == "EXPERT"
    out = two_stage_view.payload("m0001", keys=lambda: keys["HARD"], cache_dir=tmp_path,
                                 difficulty="hard")
    assert out["state"] == "fresh" and out["difficulty"] == "HARD"
    assert two_stage_view.payload("m0002", keys=lambda: keys["HARD"], cache_dir=tmp_path,
                                  difficulty="HARD")["state"] == "missing"


def test_the_maxed_basis_scores_every_bench_holomem_on_a_complete_board():
    """The web's "Maxed account" (basis `maxed`): every holomem off the team has its
    whole tree, not the account's captured board (`dumps/board.json`, 5-66 nodes a
    holomem). Real dumps; skipped without them."""
    import pytest

    try:
        built = build.leaf_inputs("maxed", "EXPERT")
        skilltree = json.loads((HERE / "dumps" / "skilltree.json").read_text(encoding="utf-8"))
    except (OSError, SystemExit, KeyError) as exc:
        pytest.skip(f"the real inputs are not available here: {exc!r}")
    bench = built["ctx"].bench_characters
    # every node of the holomem's tree but the four sockets (S-*), whose Connect card is
    # folded into the other nodes' `connect_multiplier`
    full = {m["name"]: {n for n in skilltree["models"][m["model"]] if not n.startswith("S-")}
            for m in skilltree["members"] if m.get("model") in skilltree["models"]}
    assert len(bench) == len(full) >= 54
    for name, nodes in bench.items():
        assert {e["node"] for e in nodes} == full[name], name
    # and the Connect cards are in: level 2, so some multiplier is above 1
    assert any(e.get("connect_multiplier", 1) > 1 for nodes in bench.values() for e in nodes)


def test_the_maxed_bench_as_a_board_state_is_every_whole_tree_with_its_connect_cards():
    """`maxed_bench_state()`: what the web's board panel shows as every holomem's target on
    a maxed account (what to buy from the current board state)
    -- the same bench the score used, as `{chr: {nodes, slots}}`."""
    import pytest

    try:
        state = build.maxed_bench_state()
        skilltree = json.loads((HERE / "dumps" / "skilltree.json").read_text(encoding="utf-8"))
    except (OSError, SystemExit, KeyError) as exc:
        pytest.skip(f"the real inputs are not available here: {exc!r}")
    trees = {m["chr"]: set(skilltree["models"][m["model"]])
             for m in skilltree["members"] if m.get("model") in skilltree["models"]}
    assert set(state) == set(trees)
    for chr_id, entry in state.items():
        assert set(entry["nodes"]) == trees[chr_id], chr_id
        assert set(entry["slots"]) <= {n for n in trees[chr_id] if n.startswith("S-")}
    assert sum(len(e["slots"]) for e in state.values()) >= len(state)     # sockets filled


def test_the_maxed_bench_targets_are_only_what_scores():
    """`maxed_bench_targets()` (no useless nodes): per holomem
    only greens / yellows the scorer counts and the socketed Connect cards -- no mini-game,
    work or live reward, EXP, Hologold or LIFE node, no bench red or blue."""
    import pytest

    from hbuilder.domain.board import parse_effect_id

    # Noel / Soleil Kiss + Fubuki, Matsuri, Flare, Noel, Anya: no Advent member
    team = ["card-00022-5-uniq-0063-00", "card-00007-5-uniq-0008-00", "card-00022-5-uniq-0063-00",
            "card-00021-5-uniq-0017-00", "card-00006-5-uniq-0007-00", "card-03005-5-uniq-0084-00"]
    try:
        targets = build.maxed_bench_targets(team)
    except (OSError, SystemExit, KeyError) as exc:
        pytest.skip(f"the real inputs are not available here: {exc!r}")
    bench = next(iter(build._MAXED_BENCH.values()))[0]
    skilltree = json.loads((HERE / "dumps" / "skilltree.json").read_text(encoding="utf-8"))
    name_of = {m["chr"]: m["name"] for m in skilltree["members"]}
    team_chrs = {"chr-" + c.split("-")[1] for c in team}
    for chr_id, entry in targets.items():
        effects = {e["node"]: e["effects"] for e in bench[name_of[chr_id]]}
        for node in entry["nodes"]:
            if node.startswith("S-"):
                assert node in entry["slots"], (chr_id, node)
                continue
            parsed = [parse_effect_id(x, branch=node[0]) for x in effects[node]]
            assert any(p.kind != "ignored" for p in parsed), (chr_id, node)
            # a bench red or blue reaches no team; a team red only when its singer sings
            assert node[0] in "GY" or (chr_id in team_chrs and any(p.conditional for p in parsed)), (chr_id, node)
            # no green for a group the team lacks ("to Advent" without an Advent member)
            assert not any(p.scope == "grouping" and "advent" in str(p.grouping) for p in parsed), (chr_id, node)
    assert all(entry["nodes"] for entry in targets.values())
