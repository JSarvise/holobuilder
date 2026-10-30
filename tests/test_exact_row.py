"""`hbuilder.solver.exact_row` — the valid partial-set bound and the
branch-and-bound that proves a per-(song, leader) row on the plan-cache leaf.

Everything runs on a SYNTHETIC pool (nine cards, eight holomems, the leader's
own card among them), a synthetic chart with five cues and a synthetic skill
tree with sockets, Connect cards and a bench board — no dumps needed. The
leaf is `parallel_refine.evaluate(plan_cache=...)`, the same function the
exhaustive route and the B&B call, so "bound >= exact" here is a statement
about the real leaf's arithmetic, not a model of it."""

from __future__ import annotations

import dataclasses
import itertools
import json

import pytest

from hbuilder.domain.board_plan import node_effect_ids_from_master
from hbuilder.domain.card import EffectTarget, GateCondition, SlotOverride, SlotRider, Stats
from hbuilder.domain.song import ChartNote, SongChart
from hbuilder.solver import exact_row as er
from hbuilder.solver import parallel_refine as pr

from conftest import make_card, make_scoring_config, make_skill

# ---------------------------------------------------------------------------
# synthetic skill tree (orthogonal adjacency from positions), Connect, bench
# ---------------------------------------------------------------------------
_E = {
    "R-001": "skill_tree_effect-all_parameter_up-100-skill_tree_effect_target-all-set_live_leader",
    "R-002": ("skill_tree_effect-all_parameter_up_permil_up-100-skill_tree_effect_target"
              "-all-set_live_leader"),
    "R-003": ("skill_tree_effect-live_active_skill_effect_up_permil_up-50-skill_tree_effect_target"
              "-all-set_live_leader"),
    "B-001": "skill_tree_effect-sense_up-50-skill_tree_effect_target-skill_tree_character-always",
    "B-002": ("skill_tree_effect-live_active_skill_activation_probability_up_permil_up-30-"
              "skill_tree_effect_target-skill_tree_character-always"),
    "B-003": ("skill_tree_effect-live_active_skill_cool_time_shorten_permil_up-40-"
              "skill_tree_effect_target-skill_tree_character-always"),
    "G-001": "skill_tree_effect-technique_up-10-skill_tree_effect_target-all-always",
    "G-002": "skill_tree_effect-all_parameter_up_permil_up-100-skill_tree_effect_target-all-always",
    "Y-001": ("skill_tree_effect-live_music_score_bonus_permil_up-20-"
              "skill_tree_effect_target-skill_tree_character-always-solo"),
}
_POS = {"S-001": [0, 0], "R-001": [1, 0], "R-002": [2, 0], "R-003": [3, 0],
        "B-001": [0, 1], "B-002": [0, 2], "B-003": [0, 3],
        "G-001": [-1, 0], "G-002": [-2, 0], "Y-001": [0, -1], "S-002": [1, 1]}
_COST = {n: 1 for n in _E}
_COST.update({"S-001": 0, "S-002": 1})
SKILLTREE = {
    "nodes": {n: {"br": n[0], "chr": {}} for n in _POS},
    "members": [{"chr": f"chr-{i:05d}", "model": "m"} for i in range(1, 12)],
    "models": {"m": _POS},
    "extents": {"area-2": [[1, 0], [2, 0]]},
    "connect": {"eff-2": {"area": "area-2", "levels": [{"v": 1000}]}},
    "card_connect": {"card-shape": "eff-2"},
}
MASTER = [{"group_id": n, "data": {"6": [_E[n]] if n in _E else [], "9": [_COST[n]]}}
          for n in _POS]
CONNECT = {
    "cc-a1": (((1, 0), (2, 0)), 1500), "cc-a2": (((1, 0), (2, 0)), 1500),
    "cc-b": (((0, 1), (0, 2)), 1200),
    "cc-c": (((-1, 0), (-2, 0), (1, 0)), 900),
}
BENCH = {
    "Chr9": [{"node": "G-001", "effects": [_E["G-001"]], "connect_multiplier": 1.5},
             {"node": "G-002", "effects": [_E["G-002"]]}],
    "Chr2": [{"node": "G-001", "effects": [_E["G-001"]], "connect_multiplier": 2.0}],
}


# ---------------------------------------------------------------------------
# synthetic cards: every kind of term the bound relaxes
# ---------------------------------------------------------------------------
def _sp(mag, dur):
    return make_skill("SPECIAL", family="score_support", magnitude=mag, duration=dur)


def _ac(mag, dur, itv, tier, cond=None, override=None):
    return make_skill("ACTIVE", magnitude=mag, duration=dur, interval=itv,
                      probability_tier=tier, condition=cond, override=override)


def _pa(mag, effect="performance", target=None, cond=None):
    return make_skill("PASSIVE", family="stat_up", effect_type=effect, magnitude=mag,
                      target=target, condition=cond)


def _sup(mag, target=None, cond=None):
    return make_skill("PASSIVE", family="score_support", magnitude=mag, target=target,
                      condition=cond)


CUTE2 = GateCondition(kind="type", target="Cute", count=2)
GEN1_2 = GateCondition(kind="generation", target="G1", count=2)
T_CUTE2 = EffectTarget(kind="type", value="Cute", count=2)
T_GEN2 = EffectTarget(kind="generation", value="G2", count=2)
T_SELF = EffectTarget(kind="self", value=None, count=None)


def _card(no, type_, gens, stats, special, active, passive, costume=None):
    return make_card(f"card-{no:05d}-5-uniq-{no:04d}-00", member=f"Chr{no}", type_=type_,
                     generations=frozenset(gens), stats=Stats(*stats), special=special,
                     active=active, passive=passive, costume_skill=costume)


POOL = [
    _card(1, "Cute", {"G1"}, (3000, 2500, 2600), _sp(30, 10), _ac(50, 10, 25, "High"),
          _sup(15, target=T_CUTE2),
          # objective-v2.2: the outfit also carries a `score_support` RIDER on a
          # DIFFERENT gate (2+ Cute) — the shape of the 11 real rider outfits
          costume=make_skill("COSTUME", family="stat_up", effect_type="all_stats",
                             magnitude=20, condition=GEN1_2,
                             rider=SlotRider(family="score_support", effect_type="score",
                                             magnitude=25.0, condition=CUTE2, target=None))),
    _card(2, "Happy", {"G1"}, (2800, 2900, 2400), _sp(40, 8), _ac(45, 9, 30, "Medium", CUTE2),
          _pa(30, "performance", target=T_GEN2)),
    _card(3, "Pure", {"G2"}, (2600, 2700, 3000), _sp(25, 12),
          _ac(30, 11, 20, "Low", override=SlotOverride(magnitude=60, condition=CUTE2)),
          _pa(25, "all_stats", target=T_SELF, cond=CUTE2)),
    _card(3, "Pure", {"G2"}, (2900, 2400, 2500), _sp(20, 9), _ac(55, 8, 28, "High"),
          _sup(20)),                                            # a second Chr3 card
    _card(4, "Cute", {"G2"}, (2700, 2600, 2800), _sp(35, 11), _ac(40, 10, 22, "Medium"),
          _pa(35, "sense", target=T_CUTE2)),
    _card(5, "Cute", {"G1", "G2"}, (2500, 3000, 2600), _sp(28, 10), _ac(60, 7, 35, "Low"),
          make_skill("ACTIVE", family="activation_rate_up", effect_type="activation_rate",
                     magnitude=10, duration=8, interval=30, probability_tier="High")),
    _card(6, "Happy", {"G3"}, (3100, 2400, 2500), _sp(32, 12), _ac(48, 9, 26, "High", GEN1_2),
          _pa(20, "technique")),
    _card(7, "Pure", {"G3"}, (2600, 2600, 2600), _sp(22, 8), _ac(52, 12, 24, "Medium"),
          _sup(12, target=T_GEN2)),
    _card(8, "Cute", {"G1"}, (2400, 2500, 3200), _sp(38, 10),
          _ac(35, 10, 21, "High", CUTE2, SlotOverride(magnitude=70, condition=CUTE2)),
          _pa(40, "performance", target=T_CUTE2, cond=GEN1_2)),
]
LEADER = POOL[0]


def _chart() -> SongChart:
    notes = []
    t = 0.0
    for i in range(240):
        notes.append(ChartNote(time=round(t, 3), perfect_permil=100 if i % 7 == 3 else 1000))
        t += 0.45 if i % 3 else 0.55
    return SongChart(music_id="m9001", difficulty="EXPERT", difficulty_level=26,
                     live_score_coefficient_permil=4, notes=tuple(notes),
                     special_cues=(15.0, 35.0, 55.0, 75.0, 95.0),
                     singers=("chr-00001",), singer_type="solo")


CHART = _chart()
POOL_IDS = [c.id for c in POOL]
HOL = tuple(c.member for c in POOL)


def _context(*, connect=CONNECT, bench=BENCH) -> pr.RefineContext:
    cfg = make_scoring_config()
    cfg = dataclasses.replace(cfg, board=None, active_uptime_model="finite")
    return pr.RefineContext(
        cards=tuple(POOL), cfg=cfg, charts=(CHART,), skilltree=SKILLTREE,
        node_master=MASTER, node_effect_ids=node_effect_ids_from_master(MASTER),
        connect_cards=connect, bench_characters=bench,
        name_by_chr={f"chr-{i:05d}": f"Chr{i}" for i in range(1, 12)},
        flat_budget=12, granted={}, use_plan_cache=True,
        plan_cache_seed=(LEADER.id, tuple(POOL_IDS[i] for i in (0, 1, 2, 4, 5))))


@pytest.fixture(scope="module")
def world():
    ctx = _context()
    by_id = {c.id: c for c in POOL}
    rp = pr.RefinePool(ctx, workers=1)
    cache = pr.new_plan_cache(ctx, by_id)

    def leaf(idx):
        return pr.evaluate(ctx, by_id, rp._tree_for, rp._budget_for,
                           (LEADER.id, tuple(POOL_IDS[i] for i in idx)), plan_cache=cache)

    bound = er.RowBound(ctx, cache, LEADER, POOL, CHART, tree_for=rp._tree_for,
                        budget_for=rp._budget_for, sings=True)
    rosters = list(er.legal_rosters(HOL, 5))
    exact = {r: leaf(r) for r in rosters}
    return {"ctx": ctx, "bound": bound, "leaf": leaf, "rosters": rosters, "exact": exact,
            "rp": rp, "cache": cache, "by_id": by_id}


# ---------------------------------------------------------------------------
# the bound dominates the leaf — on every roster and every partial set
# ---------------------------------------------------------------------------
def test_pool_exercises_every_relaxed_term(world):
    """The fixture is only a test of the bound if the pool carries the terms
    the bound relaxes: gates that can and cannot fire, an override, targeted
    stat passives and supports, a team-wide support, a rate-up fold, cues,
    sockets and a bench board."""
    b = world["bound"]
    assert len(world["rosters"]) == 91           # C(9,5) minus the C(7,3) two-Chr3 rosters
    kinds = set()
    for c in b.cards:
        for e in c.stat_entries:
            kinds.add(("stat", type(e.target).__name__, e.gate is not None))
        for a in c.actives:
            kinds.add(("active", a.gate is not None, a.override_gate is not None))
        for s in c.supports:
            kinds.add(("support", s.targeted, s.gate is not None))
        if c.rate_up > 0.0:
            kinds.add(("rate_up",))
    assert ("active", True, True) in kinds and ("active", False, False) in kinds
    assert ("support", True, False) in kinds and ("support", False, False) in kinds
    assert ("stat", "EffectTarget", True) in kinds and ("stat", "NoneType", False) in kinds
    assert ("rate_up",) in kinds
    assert b.outfit_stat is not None and b.outfit_stat.gate is not None
    # the outfit's rider support, on its own gate, team-wide
    assert len(b.outfit_supports) == 1 and b.outfit_supports[0].gate is not None
    assert not b.outfit_supports[0].targeted and b.outfit_supports[0].mag == 0.25
    assert any(v.entries for vs in b.variants.values() for v in vs)       # sockets seen
    assert any(v.kind == "bench" for vs in b.variants.values() for v in vs)
    assert b.seu_hat > 0.0            # (Y is 0 here: the planner values solo yellows off charts.json)
    assert all(c.actives and c.specials for c in b.cards)


def test_bound_dominates_the_leaf_on_every_legal_roster(world):
    b, exact = world["bound"], world["exact"]
    ratios = []
    for r, e in exact.items():
        ub = b.ub(r)
        assert ub >= e, (r, ub, e)
        ratios.append(ub / e)
    # tight enough to be a bound worth having, loose enough to be a relaxation
    assert 1.0 <= min(ratios) and max(ratios) < 2.0


def test_p_one_mode_is_looser_than_tier_mode_and_still_valid(world):
    ctx, rp, cache = world["ctx"], world["rp"], world["cache"]
    b1 = er.RowBound(ctx, cache, LEADER, POOL, CHART, tree_for=rp._tree_for,
                     budget_for=rp._budget_for, sings=True, p_mode="one")
    b = world["bound"]
    for r, e in world["exact"].items():
        assert b1.ub(r) >= b.ub(r) >= e


@pytest.mark.parametrize("depth", [1, 2, 3, 4])
def test_partial_set_bound_dominates_every_completion(world, depth):
    b, exact = world["bound"], world["exact"]
    n = len(POOL)
    seen = 0
    for idx in itertools.combinations(range(n), depth):
        if len({HOL[i] for i in idx}) != depth:
            continue
        comps = [r for r in exact if set(idx) <= set(r)]
        if not comps:
            continue
        seen += 1
        best = max(exact[r] for r in comps)
        ub = b.ub(idx)
        assert ub >= best, (idx, ub, best)
        # ... and never below the tightest child bound (monotone down the tree)
        assert ub >= max(b.ub(r) for r in comps) - 1e-6
    assert seen > 0


def test_bound_refuses_models_it_is_not_written_for(world):
    from hbuilder.domain.scoring_models import MultiplicativeStacking, SummedActiveSkills
    ctx, rp, cache = world["ctx"], world["rp"], world["cache"]
    with pytest.raises(ValueError):
        er.RowBound(dataclasses.replace(ctx, cfg=dataclasses.replace(
            ctx.cfg, stacking_model=MultiplicativeStacking())), cache, LEADER, POOL, CHART,
            tree_for=rp._tree_for, budget_for=rp._budget_for, sings=True)
    with pytest.raises(ValueError):
        er.RowBound(dataclasses.replace(ctx, cfg=dataclasses.replace(
            ctx.cfg, active_stacking_model=SummedActiveSkills())), cache, LEADER, POOL, CHART,
            tree_for=rp._tree_for, budget_for=rp._budget_for, sings=True)


# ---------------------------------------------------------------------------
# chart structure and counting helpers
# ---------------------------------------------------------------------------
def test_chart_structure_sum_ceil_equals_the_per_note_sum():
    import math
    st = er.ChartStructure.of(CHART)
    for a, b, f in ((0, 240, 3.7), (13, 77, 12345.678), (200, 201, 0.5), (50, 50, 9.0)):
        direct = sum(math.ceil(f * st.weights[n]) for n in range(a, b))
        assert st.sum_ceil(a, b, f) == direct
    # cue ranges: exactly the notes with cue <= t < cue + duration
    lo, hi = st.cue_range(2, 9.0)
    assert all(55.0 <= st.times[n] < 64.0 for n in range(lo, hi))
    assert lo == 0 or st.times[lo - 1] < 55.0
    assert hi == len(st.times) or st.times[hi] >= 64.0


def test_count_completions_matches_the_enumeration():
    for depth in (0, 1, 2, 3, 4):
        for idx in itertools.combinations(range(len(POOL)), depth):
            if len({HOL[i] for i in idx}) != depth:
                continue
            brute = sum(1 for r in er.legal_rosters(HOL, 5) if r[:depth] == idx)
            assert er._count_completions(idx, HOL) == brute, idx


# ---------------------------------------------------------------------------
# branch and bound finds the brute-force optimum
# ---------------------------------------------------------------------------
def test_branch_and_bound_finds_the_brute_force_optimum(world, tmp_path):
    ctx, b, exact, leaf = world["ctx"], world["bound"], world["exact"], world["leaf"]
    best_r = max(exact, key=exact.get)
    # a weak seed: the lexicographically first roster
    seed = world["rosters"][0]
    # the module READS the resume JSONL; the caller writes it (SCHM-04: no
    # write path under hbuilder) — here through `resume_sink`
    resume = tmp_path / "prefixes.jsonl"
    written: list[dict] = []
    def sink(row: dict) -> None:
        written.append(row)
        with resume.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + chr(10))
    res = er.branch_and_bound(ctx, b, LEADER, POOL, incumbent_score=exact[seed],
                              incumbent_idx=seed, workers=1, leaf_fn=leaf,
                              resume_path=resume, resume_sink=sink)
    assert res.status == "EXACT"
    assert written and all(r["kind"] == "prefix" for r in written)
    assert res.best_score == exact[best_r]
    assert exact[res.best_idx] == exact[best_r]      # a value-optimum (ties allowed)
    assert res.leaves_evaluated + res.leaves_pruned + res.leaves_bound_pruned == len(exact)
    assert res.leaves_evaluated < len(exact)          # something was pruned
    # resuming a finished run re-reads the prefixes and evaluates nothing new
    n_written = len(written)
    res2 = er.branch_and_bound(ctx, b, LEADER, POOL, incumbent_score=exact[seed],
                               incumbent_idx=seed, workers=1, leaf_fn=leaf,
                               resume_path=resume, resume_sink=sink)
    assert res2.status == "EXACT" and res2.best_score == exact[best_r]
    assert res2.leaves_evaluated == res.leaves_evaluated
    assert len(written) == n_written                  # nothing new was run, so nothing new was sunk


def test_branch_and_bound_seeded_with_the_optimum_reports_it_and_prunes_more(world):
    ctx, b, exact, leaf = world["ctx"], world["bound"], world["exact"], world["leaf"]
    best_r = max(exact, key=exact.get)
    res = er.branch_and_bound(ctx, b, LEADER, POOL, incumbent_score=exact[best_r],
                              incumbent_idx=best_r, workers=1, leaf_fn=leaf)
    assert res.status == "EXACT" and res.best_score == exact[best_r]
    assert exact[res.best_idx] == exact[best_r]
    seed = world["rosters"][0]
    weak = er.branch_and_bound(ctx, b, LEADER, POOL, incumbent_score=exact[seed],
                               incumbent_idx=seed, workers=1, leaf_fn=leaf)
    assert res.leaves_evaluated <= weak.leaves_evaluated


def test_deadline_gives_a_bounded_certificate_with_an_upper_bound(world):
    ctx, b, exact, leaf = world["ctx"], world["bound"], world["exact"], world["leaf"]
    seed = world["rosters"][0]
    calls = {"n": 0}

    def slow_leaf(idx):
        calls["n"] += 1
        return leaf(idx)

    res = er.branch_and_bound(ctx, b, LEADER, POOL, incumbent_score=exact[seed],
                              incumbent_idx=seed, workers=1, leaf_fn=slow_leaf,
                              deadline=0.0)
    assert res.status == "BOUNDED"
    assert res.upper_bound is not None and res.upper_bound >= max(exact.values())
    assert res.prefixes_done < res.prefixes_total


def test_local_search_1swap_reaches_a_1swap_local_optimum_and_never_worsens(world):
    exact = world["exact"]
    holomems = tuple(c.member for c in POOL)
    calls = {"batches": 0, "rosters": 0}

    def batch(rosters):
        calls["batches"] += 1
        calls["rosters"] += len(rosters)
        return [exact[tuple(r)] for r in rosters]

    seed = world["rosters"][0]
    score, roster, passes, scored = er.local_search_1swap(holomems, seed, batch)
    assert score == exact[roster] and score >= exact[seed]
    assert scored == calls["rosters"] and passes >= 1
    # local optimum: no legal single replacement scores higher
    for slot in range(len(roster)):
        others = roster[:slot] + roster[slot + 1:]
        used = {holomems[i] for i in others}
        for j in range(len(holomems)):
            if j in roster or holomems[j] in used:
                continue
            cand = tuple(sorted(others + (j,)))
            assert exact[cand] <= score
    # seeded on the optimum it stops after one pass without moving
    best_r = max(exact, key=exact.get)
    s2, r2, p2, _ = er.local_search_1swap(holomems, best_r, batch, start_score=exact[best_r])
    assert (s2, r2, p2) == (exact[best_r], tuple(sorted(best_r)), 1)
