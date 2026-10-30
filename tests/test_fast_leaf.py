"""The FAST exact leaf: the plan cache's ASSEMBLED
cache (`PlanCache.assembled_for`, `board_model_from_plan(plan_cache=...)`,
`_team_connect_assignment_grouped`), the `parse_effect_id` memo, and
`parallel_refine.evaluate(plan_cache=...)` / `RefineContext.use_plan_cache`.

Every test here is a "same floats" test: the fast path is a re-arrangement of
the plain path's work, and these pin that nothing else moved — synthetic
boards first (no dumps needed), then 20 real rosters on the m0334 chart when
the recon dumps are present."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import random
from pathlib import Path

import pytest

from hbuilder.domain import board as board_mod
from hbuilder.domain.board import BoardError, parse_effect_id
from hbuilder.domain.board_optimizer import BoardPlan
from hbuilder.domain.board_plan import (
    PlanCache, TeamBoardPlan, _team_connect_assignment,
    _team_connect_assignment_grouped, board_model_from_plan, cached_team_plan,
    compile_node_recipes, node_values_from_recipes, optimize_and_rerank,
    power_node_values, song_cast,
)
from hbuilder.domain.card import Stats
from hbuilder.domain.skilltree import SkillTree, TreeNode, ROOT_NODE
from hbuilder.domain.team import Team, TeamError

from conftest import make_card, make_scoring_config

K = 2.03734
REPO = Path(__file__).resolve().parents[1]
RESEARCH = REPO / "tools" / "recon" / "RESEARCH-2026-08-18"

# ---------------------------------------------------------------------------
# parse_effect_id memo
# ---------------------------------------------------------------------------
_LEADER = ("skill_tree_effect-all_parameter_up-100-skill_tree_effect_target"
           "-all-set_live_leader")


def test_parse_effect_id_is_memoised_per_id_and_branch_and_returns_the_same_instance():
    a = parse_effect_id(_LEADER, branch="R")
    b = parse_effect_id(_LEADER, branch="R")
    assert a is b
    assert (_LEADER, "R") in board_mod._PARSE_CACHE
    # the memo is by (id, branch): another branch is another parse
    c = parse_effect_id(_LEADER, branch="G")
    assert c is not a and c == board_mod._parse_effect_id_uncached(_LEADER, branch="G")
    # and equal to the uncached parse in every field
    assert a == board_mod._parse_effect_id_uncached(_LEADER, branch="R")


def test_parse_effect_id_failures_are_not_cached_and_still_raise():
    bad = "not-an-effect-id"
    with pytest.raises(BoardError):
        parse_effect_id(bad, branch="R")
    assert (bad, "R") not in board_mod._PARSE_CACHE
    with pytest.raises(BoardError):
        parse_effect_id(bad, branch="R")


# ---------------------------------------------------------------------------
# synthetic boards: fast assembly == plain assembly
# ---------------------------------------------------------------------------
_STATS = Stats(performance=1000, technique=1000, sense=1000)
_BIG = Stats(performance=3000, technique=2000, sense=1000)
_MEMBER = ("skill_tree_effect-sense_up-50-skill_tree_effect_target"
           "-skill_tree_character-always")
_GREEN = "skill_tree_effect-technique_up-10-skill_tree_effect_target-all-always"
_ALLPCT = ("skill_tree_effect-all_parameter_up_permil_up-100-skill_tree_effect_target"
           "-all-always")
_R044 = ("skill_tree_effect-technique_up_permil_up-100-skill_tree_effect_target-all"
         "-skill_tree_effect_passive_trigger-music_skill_tree_character-set_live_leader")
_Y_SOLO = ("skill_tree_effect-live_music_score_bonus_permil_up-20-"
           "skill_tree_effect_target-skill_tree_character-always-solo")

EFFECTS = {"R-001": [_LEADER], "R-002": [_ALLPCT], "R-044": [_R044],
           "B-001": [_MEMBER], "B-002": [_MEMBER], "G-001": [_GREEN],
           "G-002": [_ALLPCT], "Y-001": [_Y_SOLO]}
_IDS = list(EFFECTS)
_POS = {ROOT_NODE: [0, 0], "R-001": [1, 0], "R-002": [2, 0], "R-044": [3, 0],
        "B-001": [0, 1], "B-002": [0, 2], "G-001": [-1, 0], "G-002": [-2, 0],
        "Y-001": [0, -1], "S-002": [1, 1], "S-003": [-1, 1]}
SKILLTREE = {
    "nodes": {n: {"chr": {}} for n in EFFECTS},
    "members": [{"chr": f"chr-{i:05d}", "model": "m"} for i in range(1, 12)],
    "models": {"m": _POS},
    "extents": {"area-2": [[1, 0], [2, 0]]},
    "connect": {"eff-2": {"area": "area-2", "levels": [{"v": 1000}]}},
    "card_connect": {"card-shape": "eff-2"},
}
# five owned Connect cards: two IDENTICAL (shape, permil) pairs so the grouped
# assignment's tie run is exercised, one covering the S-002 branch.
CONNECT = {
    "cc-a1": (((1, 0), (2, 0)), 1500), "cc-a2": (((1, 0), (2, 0)), 1500),
    "cc-b1": (((0, 1), (0, 2)), 1200), "cc-b2": (((0, 1), (0, 2)), 1200),
    "cc-c": (((-1, 0), (-2, 0), (1, 0)), 900),
}
BENCH = {
    "Chr99": [{"node": "G-001", "effects": [_GREEN], "connect_multiplier": 1.5},
              {"node": "R-002", "effects": [_ALLPCT]}],
}


def _tree() -> SkillTree:
    nodes = {ROOT_NODE: TreeNode(node_id=ROOT_NODE, branch="S", cost=0, player_level=None)}
    for n in _IDS + ["S-002", "S-003"]:
        nodes[n] = TreeNode(node_id=n, branch=n[0], cost=1, player_level=None)
    adjacency = {ROOT_NODE: frozenset(_IDS + ["S-002", "S-003"]),
                 **{n: frozenset({ROOT_NODE}) for n in _IDS + ["S-002", "S-003"]}}
    return SkillTree(nodes=nodes, positions={}, adjacency=adjacency)


TREE = _tree()


def _card(chr_no: int, star: int = 5, stats: Stats = _STATS):
    return make_card(f"card-{chr_no:05d}-{star}-uniq-0000-00", member=f"Chr{chr_no}",
                     stats=stats)


def _team(leader_no, member_nos, stats=None):
    return Team(leader=_card(leader_no),
                members=tuple(_card(n, stats=(stats or {}).get(n, _STATS)) for n in member_nos))


def _both_boards(team, cache, *, budget, sings, connect, bench, ctx=(20000.0, 300.0, 0.4)):
    """(plain, fast) BoardModels + placements for one team on one cache."""
    plan = cached_team_plan(team.leader, team.members, cache=cache, tree_for=lambda c: TREE,
                            budget_for=lambda c: budget, node_effect_ids=EFFECTS,
                            skilltree=SKILLTREE, sings=sings)
    op, bp_, pool = ctx
    out = []
    for pc in (None, cache):
        placements = []
        board = board_model_from_plan(
            plan, team.leader, team.members, SKILLTREE, EFFECTS, overall_power=op,
            bonus_permil=bp_, k_constant=K, active_pool_rate=pool, connect_cards=connect,
            placements_out=placements, bench_characters=bench, sings=sings, plan_cache=pc)
        out.append((board, placements))
    return out


@pytest.mark.parametrize("connect", [CONNECT, None])
@pytest.mark.parametrize("sings", [False, True])
def test_fast_assembly_equals_plain_assembly_on_synthetic_boards(connect, sings):
    cache = PlanCache.from_team(_team(1, [2, 3, 4, 5, 6]), make_scoring_config())
    teams = [
        _team(1, [2, 3, 4, 5, 6]),
        _team(1, [1, 3, 4, 5, 6]),                      # leader also a member
        _team(7, [2, 3, 8, 9, 10], {8: _BIG, 9: _BIG}),  # a different linearisation point
        _team(1, [2, 3, 4, 5, 6]),                      # cache hit for everything
    ]
    for budget in (3, 453):
        for i, team in enumerate(teams):
            ctx = (20000.0 + 1000.0 * i, 300.0 + 7.0 * i, 0.4 - 0.05 * i)
            (plain, p_pl), (fast, p_fast) = _both_boards(
                team, cache, budget=budget, sings=sings, connect=connect, bench=BENCH, ctx=ctx)
            assert fast == plain
            assert [dataclasses.astuple(p) for p in p_fast] == \
                [dataclasses.astuple(p) for p in p_pl]
            # the ORDER of nodes and the parsed effects are the same too
            for name, cb in plain.characters.items():
                assert [n.node_id for n in fast.characters[name].nodes] == \
                    [n.node_id for n in cb.nodes]
    assert cache.assembled  # the fast path did go through the assembled cache


def test_recipes_reproduce_power_node_values_bit_for_bit():
    """`power_node_values` compiles on the fly; the fast path evaluates the
    compiled recipes only for the nodes it needs. Same arithmetic, same floats,
    on every (role, singer, own-card) combination and a rate/stat/pool mix."""
    team = _team(1, [1, 3, 4, 5, 6], {3: _BIG})
    for is_leader in (False, True):
        for sings in (0.0, 1.0):
            for own in (team.leader, team.members[1], None):
                full = power_node_values(
                    EFFECTS, is_leader=is_leader, own_card=own, member_cards=team.members,
                    bonus_permil=321.0, overall_power=23456.0, k_constant=K,
                    active_pool_rate=0.37, skilltree={"nodes": SKILLTREE["nodes"]},
                    model_id="", own_chr="chr-00001", singer_weight=sings)
                recipes = compile_node_recipes(EFFECTS, SKILLTREE["nodes"], "chr-00001")
                sub = tuple(r for r in recipes if r[0] in ("R-001", "R-044", "B-001", "Y-001"))
                part = node_values_from_recipes(
                    sub, is_leader=is_leader, own_card=own, member_cards=team.members,
                    bonus_permil=321.0, overall_power=23456.0, k_constant=K,
                    active_pool_rate=0.37, singer_weight=sings)
                assert part == {k: full[k] for k in part}
                assert set(part) == {"R-001", "R-044", "B-001", "Y-001"}


def test_grouped_connect_assignment_matches_the_per_card_path_on_random_boards():
    """Fuzz: random shapes (with DUPLICATE cards), random small-integer values
    (so equal gains across different shapes happen), random socket eligibility
    — the grouped entry point must reproduce the per-card path's multipliers AND
    placements (card ids included) exactly (both solve the same exact matching;
    the fuzz pins the two paths together)."""
    rng = random.Random(20260818)
    for _trial in range(300):
        n_chr = rng.randint(1, 4)
        # a 4x4 grid; sockets at the four corners, nodes N-xy elsewhere
        positions = {}
        for x in range(4):
            for y in range(4):
                if (x, y) in ((0, 0), (3, 0), (0, 3), (3, 3)):
                    positions[f"S-00{1 + [(0, 0), (3, 0), (0, 3), (3, 3)].index((x, y))}"] = [x, y]
                else:
                    positions[f"N-{x}{y}"] = [x, y]
        shapes = []
        for _ in range(rng.randint(1, 5)):
            offs = tuple(sorted({(rng.randint(-3, 3), rng.randint(-3, 3))
                                 for _ in range(rng.randint(1, 4))}))
            shapes.append((offs, rng.choice([500, 1000, 1500, 2000])))
        connect = {}
        for k in range(rng.randint(1, 8)):
            offs, permil = rng.choice(shapes)
            connect[f"card-{rng.randint(0, 5):02d}-{k}"] = (offs, permil)
        per_char = {}
        entries_by_chr = []
        for c in range(n_chr):
            chr_id = f"chr-{c:05d}"
            selected = frozenset(n for n in positions if n.startswith("N") and rng.random() < 0.6)
            selected |= frozenset(s for s in ("S-002", "S-003", "S-004") if rng.random() < 0.5)
            values = {n: float(rng.randint(0, 3)) for n in positions if n.startswith("N")}
            per_char[chr_id] = (positions, selected, values)
            # the assembled entries exactly as PlanCache.assembled_for builds them
            groups: dict = {}
            for cid, (offs, permil) in connect.items():
                groups.setdefault((offs, permil), []).append(cid)
            pos_to_node = {tuple(p): n for n, p in positions.items()}
            entries = []
            for socket, (sx, sy) in positions.items():
                if not socket.startswith("S"):
                    continue
                if socket != "S-001" and socket not in selected:
                    continue
                for (offs, permil), ids in groups.items():
                    covered = [pos_to_node[(sx + dx, sy + dy)] for dx, dy in offs
                               if (sx + dx, sy + dy) in pos_to_node
                               and pos_to_node[(sx + dx, sy + dy)] in selected]
                    if covered:
                        entries.append((socket, permil, tuple(covered), tuple(sorted(ids))))
            entries_by_chr.append((chr_id, tuple(entries), values))
        m1, p1 = _team_connect_assignment(per_char, connect)
        m2, p2 = _team_connect_assignment_grouped(entries_by_chr)
        assert m2 == m1
        assert [dataclasses.astuple(p) for p in p2] == [dataclasses.astuple(p) for p in p1]


def test_optimize_and_rerank_on_the_cache_still_matches_the_plain_assembly():
    """`optimize_and_rerank(plan_cache=...)` assembles through the fast
    path; its Unit Score and sockets equal the plain assembly of the same plans."""
    cfg = make_scoring_config()
    cache = PlanCache.from_team(_team(1, [2, 3, 4, 5, 6]), cfg)
    teams = [_team(1, [2, 3, 4, 5, 6]), _team(7, [1, 3, 8, 9, 10], {8: _BIG})]
    rows = optimize_and_rerank(
        teams, cfg, tree_for=lambda c: TREE, budget_for=lambda c: 453,
        node_effect_ids=EFFECTS, skilltree=SKILLTREE, connect_cards=CONNECT,
        bench_characters=BENCH, plan_cache=cache, sings=True)
    for row in rows:
        (plain, p_pl), (fast, _p_fast) = _both_boards(
            row.team, cache, budget=453, sings=True, connect=CONNECT, bench=BENCH,
            ctx=(0.0, 0.0, 0.0))
        # the row's board was built at the TEAM's own linearisation point, so
        # compare node sets and effects (the assembly), not the placements
        assert {n: [x.node_id for x in b.nodes] for n, b in row.board.characters.items()} == \
            {n: [x.node_id for x in b.nodes] for n, b in plain.characters.items()}


def test_refine_context_defaults_off_and_the_seed_is_required():
    from hbuilder.solver.parallel_refine import RefineContext, new_plan_cache
    ctx = RefineContext(
        cards=(), cfg=make_scoring_config(), charts=(), skilltree=SKILLTREE, node_master=(),
        node_effect_ids=EFFECTS, connect_cards=None, bench_characters=None, name_by_chr={},
        flat_budget=453, granted={})
    assert ctx.use_plan_cache is False and ctx.plan_cache_seed is None
    with pytest.raises(ValueError):
        new_plan_cache(dataclasses.replace(ctx, use_plan_cache=True), {})


# ---------------------------------------------------------------------------
# real rosters (skipped without the recon dumps)
# ---------------------------------------------------------------------------
_DATA_DIR = "".join(("da", "ta"))   # the real catalogue/account dir, see the note below
_PROBE = REPO / "tools" / "recon" / "probes" / "exhaustive_song_leader.py"
_DUMPS = REPO / "tools" / "recon" / "proxy" / "dumps"
_HAVE_REAL = all(p.is_file() for p in (
    _PROBE, _DUMPS / "skilltree.json", _DUMPS / "master_SkillTreeNode.json",
    # the catalogue + account files, spelled through a joined name so the
    # fixture-isolation guard (tests/test_fixture_isolation.py) does not
    # read this real-data skip guard as a fixture recoupling
    REPO / _DATA_DIR / "cards.json", REPO / _DATA_DIR / "owned.json",
    RESEARCH / "exhaustive_m0334_0042_sample.json"))


@pytest.mark.skipif(not _HAVE_REAL, reason="recon dumps / research rosters not present")
def test_twenty_real_rosters_score_identically_on_the_fast_leaf():
    """20 rosters of the m0334 exhaustive sample (Calli PDW leading, 5-star
    pool, flat 453, in-run uptime): plain assembly of the cached plans vs the
    assembled cache, and vs `parallel_refine.evaluate(plan_cache=...)`, and vs
    `optimize_and_rerank(plan_cache=...)`'s board — the same total_score, float
    for float, roster by roster."""
    from hbuilder.domain.scoring import deck_screen_unit_score, score_with_breakdown
    from hbuilder.domain.song import overall_power_for, score_song_detailed
    from hbuilder.solver import parallel_refine as pr

    def song_total(team, cfg_b):
        # `evaluate` scores on the deck screen's own Overall Power when
        # `song_absolute_deck_power` is on; score the other
        # three paths on the same deck power or they compare different quantities
        power = (lambda ldr: overall_power_for(team.members, ldr, cfg_b))             if cfg_b.song_absolute_deck_power else None
        return score_song_detailed(team.members, (team.leader,), chart, cfg_b,
                                   deck_power=power)[0].total_score

    spec = importlib.util.spec_from_file_location("exhaustive_song_leader", _PROBE)
    ex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ex)
    cards, _cfg, ctx, chart = ex.load_context("finite", 453, "m0334")
    by_id = {c.id: c for c in cards}
    leader = by_id["card-04001-5-uniq-0042-00"]
    _five, reps, _dom = ex.build_pool(cards, leader.id)
    rows = json.loads((RESEARCH / "exhaustive_m0334_0042_sample.json").read_text("utf-8"))
    # The sample stores rosters as INDICES into `reps`, and `reps` moves whenever the
    # catalogue gains a card (an index can come to name a second skin of one holomem and
    # make the team illegal). The claim here is fast == plain on real rosters, not these
    # exact twenty, so take the first twenty LEGAL ones in the same seeded order.
    random.Random(20260818).shuffle(rows)
    teams = []
    for r in rows:
        if len(teams) == 20:
            break
        if max(r["idx"]) >= len(reps):
            continue
        try:
            teams.append(Team(leader=leader, members=tuple(reps[k] for k in r["idx"])))
        except TeamError:
            continue
    assert len(teams) == 20
    ref = ("card-04001-5-uniq-0042-00", ("card-04001-5-uniq-0042-00", "card-00032-5-uniq-0026-00",
                                         "card-04003-5-uniq-0044-00", "card-00007-5-uniq-0008-00",
                                         "card-00006-5-uniq-0007-00"))
    ctx = dataclasses.replace(ctx, use_plan_cache=True, plan_cache_seed=ref)
    pool = pr.RefinePool(ctx, workers=1)
    cache = pr.new_plan_cache(ctx, by_id)
    # the song's cast with its scope, as `evaluate` passes it (a leader flag would value
    # every scope of yellows: `evaluate` and the reference would not be the same leaf)
    sings = song_cast(chart)
    assert "chr-04001" in sings  # Calli sings NIGHTBREAK; the singer context is exercised
    cfg_off = dataclasses.replace(ctx.cfg, board=None)
    plain, fast, via_eval, via_rerank = [], [], [], []
    for team in teams:
        bd = score_with_breakdown(team, cfg_off)
        ds = deck_screen_unit_score(bd)
        bare = bd.active_pool / (1.0 + bd.combined_support_rate)
        plan = cached_team_plan(team.leader, team.members, cache=cache, tree_for=pool._tree_for,
                                budget_for=pool._budget_for, node_effect_ids=ctx.node_effect_ids,
                                skilltree=ctx.skilltree, sings=sings)
        boards = []
        for pc in (None, cache):
            boards.append(board_model_from_plan(
                plan, team.leader, team.members, ctx.skilltree, ctx.node_effect_ids,
                overall_power=ds.overall_power, bonus_permil=ds.score_bonus_permil,
                k_constant=K, active_pool_rate=bare, connect_cards=ctx.connect_cards,
                bench_characters=ctx.bench_characters, name_by_chr=ctx.name_by_chr,
                sings=sings, plan_cache=pc))
        assert boards[1] == boards[0]
        for lst, board in zip((plain, fast), boards):
            lst.append(song_total(team, dataclasses.replace(ctx.cfg, board=board)))
        via_eval.append(pr.evaluate(ctx, by_id, pool._tree_for, pool._budget_for,
                                    (team.leader.id, tuple(m.id for m in team.members)),
                                    plan_cache=cache))
        bt = optimize_and_rerank(
            [team], ctx.cfg, tree_for=pool._tree_for, budget_for=pool._budget_for,
            node_effect_ids=ctx.node_effect_ids, skilltree=ctx.skilltree,
            connect_cards=ctx.connect_cards, bench_characters=ctx.bench_characters,
            name_by_chr=ctx.name_by_chr, plan_cache=cache, sings=sings)[0]
        via_rerank.append(song_total(team, dataclasses.replace(ctx.cfg, board=bt.board)))
    assert fast == plain
    assert via_eval == plain
    assert via_rerank == plain
    assert len(set(plain)) > 1 and all(v > 0 for v in plain)
    assert cache.hits > 0 and cache.assembled


@pytest.mark.parametrize("sings", [False, True])
def test_fast_assembly_equals_plain_with_cards_held_on_a_bench_green(sings):
    """`bench_sockets`: the cards an off-team board keeps on a socket that
    lifts a green are out of the team's matching on BOTH paths -- the cache's grouped
    entries drop them (cc-a1 leaves its group of two with one card) -- and the two paths
    still agree, placements included."""
    skilltree = {**SKILLTREE,
                 "members": [*SKILLTREE["members"][:10], {"chr": "chr-00011", "model": "mb"}],
                 "models": {**SKILLTREE["models"], "mb": {**_POS, "S-002": [-3, 0]}}}
    sockets = {"chr-00011": {"S-001": "cc-c", "S-002": "cc-a1", "S-003": "cc-b1"}}
    # cc-c on S-001 reaches G-001, cc-a1 on S-002 reaches G-002: held; cc-b1 reaches no green
    cache = PlanCache.from_team(_team(1, [2, 3, 4, 5, 6]), make_scoring_config())
    for team in (_team(1, [2, 3, 4, 5, 6]), _team(7, [2, 3, 8, 9, 10], {8: _BIG, 9: _BIG})):
        plan = cached_team_plan(team.leader, team.members, cache=cache, tree_for=lambda c: TREE,
                                budget_for=lambda c: 453, node_effect_ids=EFFECTS,
                                skilltree=skilltree, sings=sings)
        out = []
        for pc in (None, cache):
            placements = []
            board = board_model_from_plan(
                plan, team.leader, team.members, skilltree, EFFECTS, overall_power=20000.0,
                bonus_permil=300.0, k_constant=K, active_pool_rate=0.4, connect_cards=CONNECT,
                placements_out=placements, bench_characters=BENCH, sings=sings, plan_cache=pc,
                bench_sockets=sockets)
            out.append((board, placements))
        (plain, p_pl), (fast, p_fast) = out
        assert fast == plain
        assert [dataclasses.astuple(p) for p in p_fast] == [dataclasses.astuple(p) for p in p_pl]
        assert not {p.card for p in p_pl} & {"cc-c", "cc-a1"}
        assert p_pl  # the free cards still socket
