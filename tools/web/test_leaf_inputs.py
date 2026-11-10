"""`leaf_inputs.py`: the keys that tell fresh inputs from stale ones, and the
maxed basis's bench. The searches themselves are covered by
`tools/web/test_two_stage.py`; nothing here scores a team."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import leaf_inputs as build  # noqa: E402

from hbuilder.errors import MissingInput  # noqa: E402


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


def test_the_version_and_settings_are_part_of_every_key(monkeypatch):
    digests = {"m0001": "a" * 64}
    songs, general = build.keys_for("ref", digests)
    monkeypatch.setattr(build, "VERSION", build.VERSION + "x")
    bumped, general_bumped = build.keys_for("ref", digests)
    assert bumped != songs and general_bumped != general
    monkeypatch.undo()
    monkeypatch.setitem(build.SETTINGS, "margin", 0.5)
    assert build.keys_for("ref", digests)[0] != songs


def test_chart_digests_read_both_file_shapes_and_only_expert(tmp_path):
    entries = [
        {"music_id": "m0001", "difficulty": "EXPERT", "notes": [1, 2]},
        {"music_id": "m0001", "difficulty": "HARD", "notes": [1]},
        {"music_id": "m0002", "difficulty": "EXPERT", "notes": [3]},
    ]
    as_list, as_dict = tmp_path / "list.json", tmp_path / "dict.json"
    as_list.write_text(json.dumps({"charts": entries}), encoding="utf-8")
    as_dict.write_text(
        json.dumps({"charts": {f"{e['music_id']}|{e['difficulty']}": e for e in entries}}), encoding="utf-8"
    )
    digests = build.chart_digests(as_list)
    assert set(digests) == {"m0001", "m0002"} and digests == build.chart_digests(as_dict)
    entries[0]["notes"] = [1, 2, 3]
    as_list.write_text(json.dumps({"charts": entries}), encoding="utf-8")
    changed = build.chart_digests(as_list)
    assert changed["m0001"] != digests["m0001"] and changed["m0002"] == digests["m0002"]


def test_the_account_basis_has_its_own_keys_and_leaves_the_maxed_ones_alone():
    digests = {"m0001": "a" * 64, "m0002": "b" * 64}
    maxed = build.keys_for("ref", digests)
    assert build.keys_for("ref", digests, "maxed") == maxed
    account = build.keys_for("ref", digests, "account")
    assert account[1] != maxed[1] and all(account[0][s] != maxed[0][s] for s in digests)


# -- difficulty (the rest lives in tools/web/test_difficulty.py) --
def test_chart_digests_pick_the_requested_difficulty(tmp_path):
    entries = {
        "m0001/EXPERT": {"music_id": "m0001", "difficulty": "EXPERT", "notes": [1, 2]},
        "m0001/HARD": {"music_id": "m0001", "difficulty": "HARD", "notes": [1]},
    }
    path = tmp_path / "charts.json"
    path.write_text(json.dumps({"charts": entries}), encoding="utf-8")
    expert, hard = build.chart_digests(path), build.chart_digests(path, "HARD")
    assert expert == build.chart_digests(path, "EXPERT") and expert["m0001"] != hard["m0001"]


def _bench_reach(chr_id: str) -> set[str]:
    import precompute_optimisations as pc

    skilltree = json.loads((build.DUMPS / "skilltree.json").read_text(encoding="utf-8"))
    node_master = json.loads((build.DUMPS / "master_SkillTreeNode.json").read_text(encoding="utf-8"))
    return set(pc.bench_reach_nodes(skilltree, node_master, chr_id))


def test_the_maxed_basis_scores_every_bench_holomem_at_max_level():
    """The web's "Maxed account" (basis `maxed`): every holomem off the team holds a
    max-level holomem's points and has bought, with them, every green and yellow the
    scorer counts -- not the account's captured board (`dumps/board.json`, 5-66 nodes a
    holomem). The team's boards are planned at the same max-level points. Real dumps;
    skipped without them."""
    import pytest

    try:
        built = build.leaf_inputs("maxed", "EXPERT")
        skilltree = json.loads((build.DUMPS / "skilltree.json").read_text(encoding="utf-8"))
    except (OSError, MissingInput, KeyError) as exc:
        pytest.skip(f"the real inputs are not available here: {exc!r}")
    assert built["ctx"].flat_budget == build.maxed_budget()
    bench = built["ctx"].bench_characters
    members = [m for m in skilltree["members"] if m.get("model") in skilltree["models"]]
    assert len(bench) == len(members) >= 54
    for m in members:
        nodes = {e["node"] for e in bench[m["name"]]}
        # the sockets (S-*) are folded into the other nodes' `connect_multiplier`
        wanted = {n for n in _bench_reach(m["chr"]) if not n.startswith("S-")}
        assert wanted <= nodes, (m["name"], sorted(wanted - nodes))
    # and the Connect cards are in: level 2, so some multiplier is above 1
    assert any(e.get("connect_multiplier", 1) > 1 for nodes in bench.values() for e in nodes)


def test_the_maxed_bench_as_a_board_state_is_a_max_level_board_with_its_connect_cards():
    """`maxed_bench_state()`: what the web's board panel shows as every holomem's target on
    a maxed account (what to buy from the current board state) -- the same bench the
    score used, as `{chr: {nodes, slots}}`, each within a max-level holomem's points."""
    import pytest

    from hbuilder.domain.board_plan import node_costs_from_master

    try:
        state = build.maxed_bench_state()
        skilltree = json.loads((build.DUMPS / "skilltree.json").read_text(encoding="utf-8"))
        node_master = json.loads((build.DUMPS / "master_SkillTreeNode.json").read_text(encoding="utf-8"))
    except (OSError, MissingInput, KeyError) as exc:
        pytest.skip(f"the real inputs are not available here: {exc!r}")
    costs = node_costs_from_master(node_master)
    trees = {
        m["chr"]: set(skilltree["models"][m["model"]])
        for m in skilltree["members"]
        if m.get("model") in skilltree["models"]
    }
    assert set(state) == set(trees)
    for chr_id, entry in state.items():
        nodes = set(entry["nodes"])
        assert _bench_reach(chr_id) <= nodes <= trees[chr_id], chr_id
        assert sum(costs.get(n, 0) for n in nodes) <= build.maxed_budget(), chr_id
        assert set(entry["slots"]) <= {n for n in nodes if n.startswith("S-")}
    assert sum(len(e["slots"]) for e in state.values()) >= len(state)  # sockets filled


def test_the_max_level_points_are_part_of_every_key(monkeypatch):
    """A game update that moves the max-level board points moves every key (the maxed
    engine is rebuilt), as a settings change does."""
    digests = {"m0001": "a" * 64}
    monkeypatch.setattr(build, "maxed_budget", lambda: 361)
    songs, general = build.keys_for("ref", digests)
    monkeypatch.setattr(build, "maxed_budget", lambda: 380)
    moved, general_moved = build.keys_for("ref", digests)
    assert moved != songs and general_moved != general


def test_the_maxed_bench_targets_are_only_what_scores():
    """`maxed_bench_targets()` (no useless nodes): per holomem
    only greens / yellows the scorer counts and the socketed Connect cards -- no mini-game,
    work or live reward, EXP, Hologold or LIFE node, no bench red or blue."""
    import pytest

    from hbuilder.domain.board import parse_effect_id

    # Noel / Soleil Kiss + Fubuki, Matsuri, Flare, Noel, Anya: no Advent member
    team = [
        "card-00022-5-uniq-0063-00",
        "card-00007-5-uniq-0008-00",
        "card-00022-5-uniq-0063-00",
        "card-00021-5-uniq-0017-00",
        "card-00006-5-uniq-0007-00",
        "card-03005-5-uniq-0084-00",
    ]
    try:
        targets = build.maxed_bench_targets(team)
    except (OSError, MissingInput, KeyError) as exc:
        pytest.skip(f"the real inputs are not available here: {exc!r}")
    bench = next(iter(build._MAXED_BENCH.values()))[0]
    skilltree = json.loads((build.DUMPS / "skilltree.json").read_text(encoding="utf-8"))
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
            assert node[0] in "GY" or (chr_id in team_chrs and any(p.conditional for p in parsed)), (
                chr_id,
                node,
            )
            # no green for a group the team lacks ("to Advent" without an Advent member)
            assert not any(p.scope == "grouping" and "advent" in str(p.grouping) for p in parsed), (
                chr_id,
                node,
            )
    assert all(entry["nodes"] for entry in targets.values())
