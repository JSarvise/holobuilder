"""`hbuilder.solver.members_first` — the file-free core of the members-first
enumeration, on a SYNTHETIC pool (nine cards, eight holomems, a leader with a
gated outfit + rider). The leaf is `parallel_refine.evaluate(plan_cache=...)`,
the same function the web's search scores with."""

from __future__ import annotations

import array
import dataclasses
import itertools

import pytest
from support.builders import make_card, make_scoring_config, make_skill

from hbuilder.domain.board_plan import node_effect_ids_from_master
from hbuilder.domain.card import EffectTarget, GateCondition, SlotOverride, SlotRider, Stats
from hbuilder.domain.song import ChartNote, SongChart
from hbuilder.domain.team import Team
from hbuilder.solver import members_first as mf
from hbuilder.solver import parallel_refine as pr

# ---------------------------------------------------------------------------
# synthetic skill tree (orthogonal adjacency from positions), Connect, bench
# ---------------------------------------------------------------------------
_E = {
    "R-001": "skill_tree_effect-all_parameter_up-100-skill_tree_effect_target-all-set_live_leader",
    "R-002": (
        "skill_tree_effect-all_parameter_up_permil_up-100-skill_tree_effect_target-all-set_live_leader"
    ),
    "R-003": (
        "skill_tree_effect-live_active_skill_effect_up_permil_up-50-skill_tree_effect_target"
        "-all-set_live_leader"
    ),
    "B-001": "skill_tree_effect-sense_up-50-skill_tree_effect_target-skill_tree_character-always",
    "B-002": (
        "skill_tree_effect-live_active_skill_activation_probability_up_permil_up-30-"
        "skill_tree_effect_target-skill_tree_character-always"
    ),
    "B-003": (
        "skill_tree_effect-live_active_skill_cool_time_shorten_permil_up-40-"
        "skill_tree_effect_target-skill_tree_character-always"
    ),
    "G-001": "skill_tree_effect-technique_up-10-skill_tree_effect_target-all-always",
    "G-002": "skill_tree_effect-all_parameter_up_permil_up-100-skill_tree_effect_target-all-always",
    "Y-001": (
        "skill_tree_effect-live_music_score_bonus_permil_up-20-"
        "skill_tree_effect_target-skill_tree_character-always-solo"
    ),
}
_POS = {
    "S-001": [0, 0],
    "R-001": [1, 0],
    "R-002": [2, 0],
    "R-003": [3, 0],
    "B-001": [0, 1],
    "B-002": [0, 2],
    "B-003": [0, 3],
    "G-001": [-1, 0],
    "G-002": [-2, 0],
    "Y-001": [0, -1],
    "S-002": [1, 1],
}
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
MASTER = [{"group_id": n, "data": {"6": [_E[n]] if n in _E else [], "9": [_COST[n]]}} for n in _POS]
CONNECT = {
    "cc-a1": (((1, 0), (2, 0)), 1500),
    "cc-a2": (((1, 0), (2, 0)), 1500),
    "cc-b": (((0, 1), (0, 2)), 1200),
    "cc-c": (((-1, 0), (-2, 0), (1, 0)), 900),
}
BENCH = {
    "Chr9": [
        {"node": "G-001", "effects": [_E["G-001"]], "connect_multiplier": 1.5},
        {"node": "G-002", "effects": [_E["G-002"]]},
    ],
    "Chr2": [{"node": "G-001", "effects": [_E["G-001"]], "connect_multiplier": 2.0}],
}


# ---------------------------------------------------------------------------
# synthetic cards: every kind of term the bound relaxes
# ---------------------------------------------------------------------------
def _sp(mag, dur):
    return make_skill("SPECIAL", family="score_support", magnitude=mag, duration=dur)


def _ac(mag, dur, itv, tier, cond=None, override=None):
    return make_skill(
        "ACTIVE",
        magnitude=mag,
        duration=dur,
        interval=itv,
        probability_tier=tier,
        condition=cond,
        override=override,
    )


def _pa(mag, effect="performance", target=None, cond=None):
    return make_skill(
        "PASSIVE", family="stat_up", effect_type=effect, magnitude=mag, target=target, condition=cond
    )


def _sup(mag, target=None, cond=None):
    return make_skill("PASSIVE", family="score_support", magnitude=mag, target=target, condition=cond)


CUTE2 = GateCondition(kind="type", target="Cute", count=2)
GEN1_2 = GateCondition(kind="generation", target="G1", count=2)
T_CUTE2 = EffectTarget(kind="type", value="Cute", count=2)
T_GEN2 = EffectTarget(kind="generation", value="G2", count=2)
T_SELF = EffectTarget(kind="self", value=None, count=None)


def _card(no, type_, gens, stats, special, active, passive, costume=None):
    return make_card(
        f"card-{no:05d}-5-uniq-{no:04d}-00",
        member=f"Chr{no}",
        type_=type_,
        generations=frozenset(gens),
        stats=Stats(*stats),
        special=special,
        active=active,
        passive=passive,
        costume_skill=costume,
    )


POOL = [
    _card(
        1,
        "Cute",
        {"G1"},
        (3000, 2500, 2600),
        _sp(30, 10),
        _ac(50, 10, 25, "High"),
        _sup(15, target=T_CUTE2),
        # objective-v2.2: the outfit also carries a `score_support` RIDER on a
        # DIFFERENT gate (2+ Cute) — the shape of the 11 real rider outfits
        costume=make_skill(
            "COSTUME",
            family="stat_up",
            effect_type="all_stats",
            magnitude=20,
            condition=GEN1_2,
            rider=SlotRider(
                family="score_support", effect_type="score", magnitude=25.0, condition=CUTE2, target=None
            ),
        ),
    ),
    _card(
        2,
        "Happy",
        {"G1"},
        (2800, 2900, 2400),
        _sp(40, 8),
        _ac(45, 9, 30, "Medium", CUTE2),
        _pa(30, "performance", target=T_GEN2),
    ),
    _card(
        3,
        "Pure",
        {"G2"},
        (2600, 2700, 3000),
        _sp(25, 12),
        _ac(30, 11, 20, "Low", override=SlotOverride(magnitude=60, condition=CUTE2)),
        _pa(25, "all_stats", target=T_SELF, cond=CUTE2),
    ),
    _card(
        3, "Pure", {"G2"}, (2900, 2400, 2500), _sp(20, 9), _ac(55, 8, 28, "High"), _sup(20)
    ),  # a second Chr3 card
    _card(
        4,
        "Cute",
        {"G2"},
        (2700, 2600, 2800),
        _sp(35, 11),
        _ac(40, 10, 22, "Medium"),
        _pa(35, "sense", target=T_CUTE2),
    ),
    _card(
        5,
        "Cute",
        {"G1", "G2"},
        (2500, 3000, 2600),
        _sp(28, 10),
        _ac(60, 7, 35, "Low"),
        make_skill(
            "ACTIVE",
            family="activation_rate_up",
            effect_type="activation_rate",
            magnitude=10,
            duration=8,
            interval=30,
            probability_tier="High",
        ),
    ),
    _card(
        6,
        "Happy",
        {"G3"},
        (3100, 2400, 2500),
        _sp(32, 12),
        _ac(48, 9, 26, "High", GEN1_2),
        _pa(20, "technique"),
    ),
    _card(
        7, "Pure", {"G3"}, (2600, 2600, 2600), _sp(22, 8), _ac(52, 12, 24, "Medium"), _sup(12, target=T_GEN2)
    ),
    _card(
        8,
        "Cute",
        {"G1"},
        (2400, 2500, 3200),
        _sp(38, 10),
        _ac(35, 10, 21, "High", CUTE2, SlotOverride(magnitude=70, condition=CUTE2)),
        _pa(40, "performance", target=T_CUTE2, cond=GEN1_2),
    ),
]
LEADER = POOL[0]


def _chart() -> SongChart:
    notes = []
    t = 0.0
    for i in range(240):
        notes.append(ChartNote(time=round(t, 3), perfect_permil=100 if i % 7 == 3 else 1000))
        t += 0.45 if i % 3 else 0.55
    return SongChart(
        music_id="m9001",
        difficulty="EXPERT",
        difficulty_level=26,
        live_score_coefficient_permil=4,
        notes=tuple(notes),
        special_cues=(15.0, 35.0, 55.0, 75.0, 95.0),
        singers=("chr-00001",),
        singer_type="solo",
    )


CHART = _chart()
POOL_IDS = [c.id for c in POOL]
HOL = tuple(c.member for c in POOL)


def _context(*, connect=CONNECT, bench=BENCH) -> pr.RefineContext:
    cfg = make_scoring_config()
    cfg = dataclasses.replace(cfg, board=None, active_uptime_model="finite")
    return pr.RefineContext(
        cards=tuple(POOL),
        cfg=cfg,
        charts=(CHART,),
        skilltree=SKILLTREE,
        node_master=MASTER,
        node_effect_ids=node_effect_ids_from_master(MASTER),
        connect_cards=connect,
        bench_characters=bench,
        name_by_chr={f"chr-{i:05d}": f"Chr{i}" for i in range(1, 12)},
        flat_budget=12,
        granted={},
        use_plan_cache=True,
        plan_cache_seed=(LEADER.id, tuple(POOL_IDS[i] for i in (0, 1, 2, 4, 5))),
    )


K_MEASURED = 15  # smallest K at which every synthetic leader's optimum is a candidate (found below:
# 8 for the gated+rider leader, 15 for the ungated one — 91 sets, so the tiers are coarse)


def _costume(cond=None, rider=None):
    return make_skill(
        "COSTUME", family="stat_up", effect_type="all_stats", magnitude=20, condition=cond, rider=rider
    )


# synthetic leaders: the pool's gated+rider outfit, a type gate, a generation
# gate on a multi-generation card, an ungated outfit, no outfit, and a leader
# whose holomem is NOT in the pool (own tier empty)
LEADERS = {
    "gated+rider (G1 x2 / Cute x2 rider)": LEADER,
    "type gate Pure x2": dataclasses.replace(
        POOL[1],
        id="card-00002-5-uniq-9002-00",
        costume_skill=_costume(GateCondition(kind="type", target="Pure", count=2)),
    ),
    "generation gate G2 x3": dataclasses.replace(
        POOL[5],
        id="card-00005-5-uniq-9005-00",
        costume_skill=_costume(GateCondition(kind="generation", target="G2", count=3)),
    ),
    "ungated outfit": dataclasses.replace(POOL[6], id="card-00006-5-uniq-9006-00", costume_skill=_costume()),
    "no outfit": dataclasses.replace(POOL[7], id="card-00007-5-uniq-9007-00", costume_skill=None),
    "holomem outside the pool": dataclasses.replace(
        POOL[4],
        id="card-00099-5-uniq-9099-00",
        member="Chr99",
        costume_skill=_costume(GateCondition(kind="type", target="Cute", count=3)),
    ),
}


@pytest.fixture(scope="module")
def world():
    ctx = _context()
    extra = [c for c in LEADERS.values() if c.id not in POOL_IDS]
    proxy = mf.bare_leader(POOL[3])  # a non-leader pool card, outfit removed
    cards = tuple(POOL) + tuple(extra) + (proxy,)
    ctx = dataclasses.replace(ctx, cards=cards)
    by_id = {c.id: c for c in cards}
    rp = pr.RefinePool(ctx, workers=1)
    cache = pr.new_plan_cache(ctx, by_id)

    def batch_score(teams):
        return [
            pr.evaluate(
                ctx,
                by_id,
                rp._tree_for,
                rp._budget_for,
                (t.leader.id, tuple(m.id for m in t.members)),
                plan_cache=cache,
            )
            for t in teams
        ]

    tables = mf.tabulate(POOL)
    rosters = [tables.roster(i) for i in range(tables.n)]
    bare = mf.score_sets(proxy, POOL, rosters, batch_score)
    order = sorted(range(tables.n), key=bare.__getitem__, reverse=True)
    exact = {name: mf.score_sets(ld, POOL, rosters, batch_score) for name, ld in LEADERS.items()}
    return {
        "tables": tables,
        "rosters": rosters,
        "bare": bare,
        "order": order,
        "exact": exact,
        "batch_score": batch_score,
        "proxy": proxy,
    }


# ---------------------------------------------------------------------------
# bare proxy, gate reader
# ---------------------------------------------------------------------------
def test_bare_leader_keeps_the_holomem_and_drops_the_outfit():
    b = mf.bare_leader(LEADER)
    assert b.member == LEADER.member and b.costume_skill is None
    assert b.id == "card-00001-5-bare-0001-00" and mf.is_bare(b) and not mf.is_bare(LEADER)
    assert b.skills == LEADER.skills and b.stats == LEADER.stats
    with pytest.raises(ValueError):
        mf.bare_leader(dataclasses.replace(LEADER, id="card-1"))


def test_gate_of_reads_kind_target_count_or_none():
    assert mf.gate_of(LEADER) == ("generation", "G1", 2)
    assert mf.gate_of(LEADERS["type gate Pure x2"]) == ("type", "Pure", 2)
    assert mf.gate_of(LEADERS["ungated outfit"]) is None
    assert mf.gate_of(LEADERS["no outfit"]) is None


# ---------------------------------------------------------------------------
# tables: every aggregate equals the brute-force count from the cards
# ---------------------------------------------------------------------------
def test_tabulate_matches_legal_rosters_and_brute_force_aggregates(world):
    t = world["tables"]
    assert t.n == 91 and t.n == len(world["rosters"])
    assert world["rosters"] == list(mf.legal_rosters(HOL, 5))
    assert t.pool_ids == tuple(POOL_IDS)
    assert t.types == ("Cute", "Happy", "Pure")
    for i, idx in enumerate(world["rosters"]):
        cards = [POOL[k] for k in idx]
        for ti, typ in enumerate(t.types):
            assert ((t.tcount[i] >> (4 * ti)) & 0xF) == sum(1 for c in cards if c.type == typ)
        for gi, g in enumerate(t.gens):
            assert bool((t.gmask[i] >> gi) & 1) == any(g in c.generations for c in cards)
        for hi, h in enumerate(t.hols):
            assert bool((t.hmask[i] >> hi) & 1) == any(c.member == h for c in cards)
        assert t.roster_ids(i) == tuple(POOL_IDS[k] for k in idx)


def _gate_satisfied(tables, gate, i: int) -> bool:
    """Does set `i` fire `gate`, by the predicate `candidate_tiers` filters with?
    (No gate -> True.)"""
    f = mf._satisfier(tables, gate)
    return True if f is None else f(i)


def test_gate_count_semantics_type_generation_unknown(world):
    t = world["tables"]
    for i, idx in enumerate(world["rosters"]):
        cards = [POOL[k] for k in idx]
        # a type gate counts members of that type
        assert _gate_satisfied(t, ("type", "Cute", 3), i) == (sum(1 for c in cards if c.type == "Cute") >= 3)
        # a generation gate counts MEMBERS carrying the generation (card 5
        # carries G1 and G2: it counts once for each), not presence
        assert _gate_satisfied(t, ("generation", "G2", 3), i) == (
            sum(1 for c in cards if "G2" in c.generations) >= 3
        )
        assert _gate_satisfied(t, ("generation", "G1", 1), i) == any("G1" in c.generations for c in cards)
        # no gate, a target no pool card carries, an unknown kind: always on
        assert _gate_satisfied(t, None, i)
        assert _gate_satisfied(t, ("type", "Cool", 1), i)
        assert _gate_satisfied(t, ("generation", "G9", 1), i)
        assert _gate_satisfied(t, ("run_state", "fever", 1), i)
    # the count-3 generation gate is satisfiable but not everywhere
    sat = [i for i in range(t.n) if _gate_satisfied(t, ("generation", "G2", 3), i)]
    assert 0 < len(sat) < t.n


# ---------------------------------------------------------------------------
# the tiers: exact semantics, then recall of the brute-force optimum
# ---------------------------------------------------------------------------
def _expected_tiers(order, tables, leader, k):
    gate = mf.gate_of(leader)
    top = set(order[:k])
    gate_tier = set() if gate is None else set([i for i in order if _gate_satisfied(tables, gate, i)][:k])
    own = set([i for i in order if leader.member in {POOL[j].member for j in tables.roster(i)}][:k])
    return top, gate_tier, own


@pytest.mark.parametrize("name", list(LEADERS))
@pytest.mark.parametrize("k", [1, 3, 10, 91, 500])
def test_tiers_are_exactly_top_k_overall_gate_satisfying_and_own_holomem(world, name, k):
    order, t = world["order"], world["tables"]
    leader = LEADERS[name]
    top, gate_tier, own = _expected_tiers(order, t, leader, k)
    got = set(mf.candidate_indices(order, t, leader, k))
    assert got == top | gate_tier | own
    if name == "holomem outside the pool":
        assert own == set()
    if mf.gate_of(leader) is None:
        assert got == top | own


def test_tiers_are_monotone_in_k_and_candidate_tiers_agrees(world):
    order, t = world["order"], world["tables"]
    tiers = mf.candidate_tiers(order, t, LEADER, (1, 3, 10, 50))
    assert tiers[1] <= tiers[3] <= tiers[10] <= tiers[50]
    for k in (1, 3, 10, 50):
        assert sorted(tiers[k]) == mf.candidate_indices(order, t, LEADER, k)
    with pytest.raises(ValueError):
        mf.candidate_tiers(order, t, LEADER, ())


def test_candidates_contain_the_brute_force_optimum_for_every_leader(world):
    """On the synthetic pool: at K = K_MEASURED the union of the three
    tiers holds the exact argmax of every synthetic leader (gated + rider,
    type gate, generation gate, ungated, none, off-pool holomem); the best of
    the candidates IS the brute-force optimum. (On a real song the same
    statement needs K in the thousands out of ~8.4M sets — K is empirical.)"""
    order, t, exact = world["order"], world["tables"], world["exact"]
    worst_k = 0
    for name, leader in LEADERS.items():
        best = max(exact[name])
        argmax = {i for i, s in enumerate(exact[name]) if s == best}
        cands = mf.candidate_indices(order, t, leader, K_MEASURED)
        assert argmax & set(cands), (name, argmax, cands)
        score_of = {i: exact[name][i] for i in cands}
        i_best, s_best = mf.best_of(score_of, cands)
        assert s_best == best and i_best in argmax
        # the smallest K that works for this leader (reported, to keep K_MEASURED honest)
        k = 1
        while not (argmax & set(mf.candidate_indices(order, t, leader, k))):
            k += 1
        worst_k = max(worst_k, k)
    assert worst_k <= K_MEASURED
    # ... and a K too small to reach a gated optimum is possible on purpose:
    # the recall is empirical, not a theorem
    assert K_MEASURED < t.n


def test_gated_leader_optimum_fires_its_gate(world):
    """The point of the gate tier: a gated outfit's optimum is a set that
    FIRES the gate (else the outfit contributes nothing and any set would do
    as well or better), so the tier that filters on it can only help."""
    t, exact = world["tables"], world["exact"]
    for name in ("gated+rider (G1 x2 / Cute x2 rider)", "type gate Pure x2", "generation gate G2 x3"):
        leader = LEADERS[name]
        best = max(exact[name])
        i = exact[name].index(best)
        assert _gate_satisfied(t, mf.gate_of(leader), i), name


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def test_score_sets_builds_teams_in_order(world):
    seen = []

    def spy(teams):
        seen.extend(teams)
        return [float(len(t.members)) for t in teams]

    rosters = world["rosters"][:3]
    out = mf.score_sets(LEADER, POOL, rosters, spy)
    assert out == [5.0, 5.0, 5.0]
    assert [tuple(m.id for m in t.members) for t in seen] == [tuple(POOL_IDS[k] for k in r) for r in rosters]
    assert all(isinstance(t, Team) and t.leader is LEADER for t in seen)
    assert mf.score_sets(LEADER, POOL, [], spy) == []


def test_best_of_breaks_ties_on_the_smallest_index():
    assert mf.best_of({3: 1.0, 1: 2.0, 2: 2.0}, [3, 2, 1]) == (1, 2.0)
    with pytest.raises(ValueError):
        mf.best_of({}, [])


def test_bare_enumeration_is_leader_agnostic_but_not_the_answer(world):
    """Sanity on the method itself: the bare scores differ from every real
    leader's (the outfit matters) and the per-leader argmaxes are not all the
    bare argmax (else the tiers would be pointless)."""
    bare, exact, order = world["bare"], world["exact"], world["order"]
    for name, sc in exact.items():
        assert sc != bare, name
    bare_best = order[0]
    argmaxes = {name: sc.index(max(sc)) for name, sc in exact.items()}
    assert any(i != bare_best for i in argmaxes.values())
    # the sets are the same combinations either way
    assert sorted(itertools.chain.from_iterable([world["rosters"]])) == world["rosters"]


# ---------------------------------------------------------------------------
# incremental update: extend an enumeration to a pool with a new card
# ---------------------------------------------------------------------------
# the synthetic POOL carries two cards with ONE id (the second Chr3 card); the update keys on ids, so it runs on a copy whose
# ids are distinct (POOL_U) with its own plan cache
POOL_U = [dataclasses.replace(c, id=f"card-{i + 1:05d}-5-uniq-{8000 + i:04d}-00") for i, c in enumerate(POOL)]
POOL_U_IDS = [c.id for c in POOL_U]
HOL_U = tuple(c.member for c in POOL_U)


@pytest.fixture(scope="module")
def world_u():
    ctx = _context()
    proxy = mf.bare_leader(POOL_U[3])
    cards = tuple(POOL_U) + (proxy,)
    ctx = dataclasses.replace(
        ctx, cards=cards, plan_cache_seed=(POOL_U_IDS[0], tuple(POOL_U_IDS[i] for i in (0, 1, 2, 4, 5)))
    )
    by_id = {c.id: c for c in cards}
    rp = pr.RefinePool(ctx, workers=1)
    cache = pr.new_plan_cache(ctx, by_id)

    def batch_score(teams):
        return [
            pr.evaluate(
                ctx,
                by_id,
                rp._tree_for,
                rp._budget_for,
                (t.leader.id, tuple(m.id for m in t.members)),
                plan_cache=cache,
            )
            for t in teams
        ]

    tables = mf.tabulate(POOL_U)
    rosters = [tables.roster(i) for i in range(tables.n)]
    bare = mf.score_sets(proxy, POOL_U, rosters, batch_score)
    return {"rosters": rosters, "bare": bare, "batch_score": batch_score, "proxy": proxy}


def test_extended_order_maps_old_to_new_and_rejects_a_reordered_pool():
    old = ["a", "c", "e"]
    assert mf.extended_order(old, ["a", "b", "c", "d", "e"]) == (0, 2, 4)
    assert mf.extended_order(old, ["a", "b", "c", "d", "e", "f"]) == (0, 2, 4)
    # a dropped old card is -1, the rest still monotone
    assert mf.extended_order(["a", "c", "x", "e"], ["a", "b", "c", "e"]) == (0, 2, -1, 3)
    with pytest.raises(ValueError):
        mf.extended_order(["c", "a"], ["a", "b", "c"])  # old order not a restriction
    with pytest.raises(ValueError):
        mf.extended_order(["a", "a"], ["a", "b"])  # duplicates
    assert mf.extended_order([], ["a"]) == ()


@pytest.mark.parametrize("gone", [0, 3, 8])
def test_update_reproduces_the_full_enumeration_byte_for_byte(world_u, gone):
    """Drop one card, enumerate the smaller pool, `merge_scores` back to the
    full pool: the scores are byte-identical to the full enumeration and only
    the sets containing the re-added card were scored."""
    proxy, batch_score = world_u["proxy"], world_u["batch_score"]
    full = array.array("d", world_u["bare"])
    small = [c for i, c in enumerate(POOL_U) if i != gone]
    small_tables = mf.tabulate(small)
    small_scores = mf.score_sets(
        proxy, small, [small_tables.roster(i) for i in range(small_tables.n)], batch_score
    )
    calls: list[int] = []

    def score_new(rosters):
        calls.append(len(rosters))
        return mf.score_sets(proxy, POOL_U, rosters, batch_score)

    seen: list[tuple[int, int]] = []
    m = mf.merge_scores(
        small_scores,
        [c.id for c in small],
        POOL_U,
        score_new,
        chunk=7,
        progress=lambda r, u: seen.append((r, u)),
    )
    assert m.scores.tobytes() == full.tobytes()
    assert m.n_sets == 91 and m.new_indices == (gone,) and m.dropped == 0 and m.dropped_ids == ()
    with_card = sum(1 for r in world_u["rosters"] if gone in r)
    assert m.sets_rescored == with_card == sum(calls)
    assert m.reused == 91 - with_card == len(small_scores)
    assert all(n <= 7 for n in calls) and seen[-1][0] == with_card and seen[-1][1] <= m.reused
    assert m.old_index == tuple(i for i in range(9) if i != gone)


def test_update_drops_the_sets_of_a_card_that_left_the_pool(world_u):
    """The reverse direction (a representative change): the old card's sets
    are skipped by position, nothing is re-scored, the result equals the
    smaller pool's own enumeration."""
    proxy, batch_score = world_u["proxy"], world_u["batch_score"]
    gone = 4
    small = [c for i, c in enumerate(POOL_U) if i != gone]
    small_tables = mf.tabulate(small)
    expected = array.array(
        "d", mf.score_sets(proxy, small, [small_tables.roster(i) for i in range(small_tables.n)], batch_score)
    )
    with pytest.raises(ValueError):  # drops need the old holomems
        mf.merge_scores(world_u["bare"], POOL_U_IDS, small, lambda r: [])
    m = mf.merge_scores(world_u["bare"], POOL_U_IDS, small, lambda r: [], old_holomems=HOL_U)
    assert m.scores.tobytes() == expected.tobytes()
    assert m.sets_rescored == 0 and m.new_indices == ()
    assert m.dropped_ids == (POOL_U_IDS[gone],) and m.dropped == sum(
        1 for r in world_u["rosters"] if gone in r
    )
    assert m.reused + m.dropped == 91
    # drop one AND add one in the same update: old (without card 3) -> new (without card 4)
    old_pool = [c for i, c in enumerate(POOL_U) if i != 3]
    old_t = mf.tabulate(old_pool)
    old_scores = mf.score_sets(proxy, old_pool, [old_t.roster(i) for i in range(old_t.n)], batch_score)
    m2 = mf.merge_scores(
        old_scores,
        [c.id for c in old_pool],
        small,
        lambda rs: mf.score_sets(proxy, small, rs, batch_score),
        old_holomems=[c.member for c in old_pool],
    )
    assert m2.scores.tobytes() == expected.tobytes()
    assert m2.new_indices == (3,) and m2.dropped_ids == (POOL_U_IDS[4],)


def test_update_refuses_scores_that_do_not_belong_to_the_old_pool(world_u):
    small = [c for i, c in enumerate(POOL_U) if i != 0]
    with pytest.raises(ValueError):  # too few old scores
        mf.merge_scores(world_u["bare"][:10], [c.id for c in small], POOL_U, lambda rs: [0.0] * len(rs))
    with pytest.raises(ValueError):  # too many old scores
        mf.merge_scores(world_u["bare"], [c.id for c in small], POOL_U, lambda rs: [0.0] * len(rs))
    with pytest.raises(ValueError):  # a scorer that miscounts
        mf.merge_scores(world_u["bare"][:56], [c.id for c in small], POOL_U, lambda rs: [0.0])
