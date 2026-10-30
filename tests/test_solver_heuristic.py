"""The beam search, validated against `solve_exact` rather than against its
own docstrings.

Every claim the heuristic makes about itself is treated here as unproven until
a test checks it. Two failure shapes have regression tests that fail on the
wrong answer rather than merely on a direction:

- `test_the_top_n_holds_distinct_arrangements_not_one_repeated` — the Top-N
  must not return the SAME arrangement repeatedly, which happens if
  `candidates` is a list every seed appends the same scored arrangements to.
- `test_the_per_feature_branch_is_what_finds_the_gate_carrying_team` — without
  the per-feature branch, at `branch=6` the beam misses the best team by 86.3%
  and does not emit `PAIRWISE_GATE_MISS` if that label is only an *eviction*
  observation, because nothing carrying the gate is ever built.

Numbers, not directions. Every assertion below pins a value the solver actually
produces; where a failure shape is guarded, the test also pins the wrong answer
it would produce, so a silent regression fails an assertion instead of passing
a loosened inequality.
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from hbuilder.domain.card import EffectTarget, GateCondition, Stats
from hbuilder.domain.scoring import score_card_set, score_with_breakdown
from hbuilder.solver.exact import canonical_pool, solve_exact
from hbuilder.solver.heuristic import (
    DEFAULT_FEATURE_BRANCH,
    FailureMode,
    _open_conditions,
    _quota_features,
    solve_heuristic,
    solve_heuristic_labelled,
)
from hbuilder.solver.selectors import (
    Selectors,
    constraint_from,
    resolve_selectors,
    set_constraint_from,
)

from conftest import make_card, make_scoring_config, make_skill

TYPES = ("Cute", "Cool", "Passion")


# ---------------------------------------------------------------------------
# Fixtures — three pools, each shaped for one question
# ---------------------------------------------------------------------------


def plain_pool(size: int = 9) -> list:
    """No gates anywhere: the beam's answer is decided by stats alone, so any
    disagreement with `solve_exact` is a search defect and cannot be blamed on
    gate resolution."""
    return [
        make_card(
            f"c{index:02d}",
            type_=TYPES[index % 3],
            generations=frozenset({f"gen{index % 2 + 1}"}),
            stats=Stats(100 + index * 7, 100 + index * 3, 100 + index * 5),
        )
        for index in range(size)
    ]


def gate_passive(magnitude: float, count: int, target: str = "Cute"):
    return make_skill(
        "PASSIVE",
        family="stat_up",
        effect_type="all_stats",
        magnitude=magnitude,
        condition=GateCondition(kind="type", target=target, count=count),
    )


def gated_pool() -> list:
    """The pairwise-gate case, built deliberately hostile.

    Six Cute cards with terrible stats each carry a `4+ Cute` gate worth +400%.
    Any one, two or three of them scores *worse* than a Cool card; all four
    together score 7.3x the best Cool-only team. So the gate "only fires once
    particular members are combined", the partials that lead to it look weak at
    every level before the last, and static potential — measured by dropping a
    card into a reference skeleton, where the gate also does not fire — ranks
    all six of them below all eight Cool cards.
    """
    cute = [
        make_card(
            f"cute{index}",
            type_="Cute",
            stats=Stats(60, 60, 60),
            passive=gate_passive(400.0, 4),
        )
        for index in range(6)
    ]
    cool = [
        make_card(
            f"cool{index}",
            type_="Cool",
            stats=Stats(200 + index * 9, 200, 200),
        )
        for index in range(8)
    ]
    return cute + cool


def cluster_pool() -> list:
    """Three mutually exclusive same-value gate clusters. A team can satisfy at
    most one `4+` gate, so which cluster a seed's static-potential ordering
    favours decides its answer — the shape `SEED_DISAGREEMENT` exists to
    report."""
    pool = []
    for type_name, magnitude in (("Cute", 300.0), ("Cool", 305.0), ("Passion", 310.0)):
        for index in range(5):
            pool.append(
                make_card(
                    f"{type_name.lower()}{index}",
                    type_=type_name,
                    stats=Stats(80 + index, 80, 80),
                    passive=gate_passive(magnitude, 4, type_name),
                )
            )
    return pool


def arrangements(teams) -> list:
    return [(team.leader_id, team.member_ids) for team in teams]


# ---------------------------------------------------------------------------
# 1. Agreement with the reference solver
# ---------------------------------------------------------------------------


def test_the_beam_reproduces_solve_exacts_entire_top_ten_on_a_plain_pool():
    """Not "close to" — the same ten arrangements, in the same order, with the
    same scores. The two solvers share `_heap_key`, so an equal-scoring tie must
    break identically as well."""
    pool = plain_pool(9)
    cfg = make_scoring_config()

    exact = solve_exact(pool, cfg, top_n=10)
    beam = solve_heuristic(pool, cfg, top_n=10)

    assert exact[0].score == 1950.0
    assert exact[0].leader_id == "c00"
    assert exact[0].member_ids == ("c04", "c05", "c06", "c07", "c08")
    assert arrangements(beam) == arrangements(exact)
    assert [team.score for team in beam] == [team.score for team in exact]


def test_the_beam_reproduces_solve_exact_on_a_pool_where_a_gate_decides():
    """The plain pool cannot distinguish a stat-greedy search from a gate-aware
    one. This one can: the best team is 23,511.0 and every Cool-only team is
    3,225.0, a 7.29x difference that exists only because four Cute cards were
    assembled together."""
    pool = gated_pool()
    cfg = make_scoring_config()

    exact = solve_exact(pool, cfg, top_n=1)
    result = solve_heuristic_labelled(pool, cfg, top_n=1)

    assert exact[0].score == 23511.0
    assert exact[0].member_ids == ("cool7", "cute0", "cute1", "cute2", "cute3")
    assert result.teams[0].score == 23511.0
    assert result.teams[0].member_ids == exact[0].member_ids


def test_the_beam_agrees_with_exact_under_every_selector_lock():
    """`--require`, `--leader`, `--exclude` and the two combined, each through
    the same `resolve_selectors` -> `constraint_from` / `set_constraint_from`
    path `cli.py` uses. A beam that honoured locks by a second, private rule
    would pass a hand-written check and fail this one."""
    pool = plain_pool(9)
    cfg = make_scoring_config()
    cases = (
        Selectors(require=(), leader=("c02",), exclude=(), include_unowned=False),
        Selectors(require=("c01", "c03"), leader=(), exclude=(), include_unowned=False),
        Selectors(require=(), leader=(), exclude=("c07", "c08"), include_unowned=False),
        Selectors(require=("c05",), leader=("c02",), exclude=(), include_unowned=False),
    )
    for selectors in cases:
        candidate_pool = resolve_selectors(pool, pool, selectors)
        shared = {
            "constraint": constraint_from(candidate_pool),
            "set_constraint": set_constraint_from(candidate_pool),
            "locked_ids": candidate_pool.locked_ids,
        }
        exact = solve_exact(
            candidate_pool.cards,
            cfg,
            top_n=10,
            leader_id=candidate_pool.leader_id,
            **shared,
        )
        beam = solve_heuristic(candidate_pool.cards, cfg, top_n=10, **shared)
        assert arrangements(beam) == arrangements(exact), selectors
        assert len(beam) == 10


def test_a_lock_is_satisfied_by_the_outfit_slot_not_only_by_membership():
    """The leader-outside-the-members family. A card that is a superb outfit and a
    dreadful member must be admissible as the LEADER of a team it is not a
    member of — `C(N-1,4) + C(N-1,5) = C(N,5)` is exactly this second family.

    A beam that only ever seeded the locks as members would return a
    plausible-looking team here and be wrong by 2.87x."""
    pool = [
        make_card(
            f"c{index:02d}",
            type_=TYPES[index % 3],
            stats=Stats(300 + index * 7, 300 + index * 3, 300 + index * 5),
        )
        for index in range(8)
    ]
    pool.append(
        make_card(
            "zz_outfit",
            type_="Cute",
            stats=Stats(1, 1, 1),
            costume_skill=make_skill(
                "COSTUME", family="stat_up", effect_type="all_stats", magnitude=90.0
            ),
        )
    )
    cfg = make_scoring_config()
    candidate_pool = resolve_selectors(
        pool,
        pool,
        Selectors(require=("zz_outfit",), leader=(), exclude=(), include_unowned=False),
    )
    shared = {
        "constraint": constraint_from(candidate_pool),
        "set_constraint": set_constraint_from(candidate_pool),
        "locked_ids": candidate_pool.locked_ids,
    }
    exact = solve_exact(pool, cfg, top_n=5, leader_id=candidate_pool.leader_id, **shared)
    beam = solve_heuristic(pool, cfg, top_n=5, **shared)

    assert exact[0].score == 9262.5
    assert exact[0].leader_id == "zz_outfit"
    # The whole point: the lock is NOT among the five members.
    assert "zz_outfit" not in exact[0].member_ids
    assert arrangements(beam) == arrangements(exact)


@pytest.mark.slow
def test_locked_runs_agree_with_exact_over_thirty_random_real_pools():
    """The synthetic-card lock test above proves the plumbing; this proves it on
    the cards the tool is actually pointed at, over all five lock shapes and a
    pool the beam has to genuinely search.

    Expected: **30/30 identical Top-10**, and every returned team
    satisfies its locks, holds five distinct holomem and honours the leader pin.
    Slow-marked because it is 30 exact solves at N=11; deselected by default,
    which is the same bargain `test_bench_smoke.py` makes.
    """
    from pathlib import Path

    from hbuilder.cli import build_parser
    from hbuilder.domain.loader import load_cards, load_owned
    from hbuilder.domain.scoring_config import load_scoring_config

    # The canonical files' location comes from the CLI's own `--cards`/`--owned`
    # defaults rather than a path literal — `test_fixture_isolation.py` forbids
    # re-hardcoding it, and reading the tool's default is stricter than a second
    # constant would be, because it cannot disagree with the tool.
    repo_root = Path(__file__).resolve().parent.parent
    cli_defaults = build_parser().parse_args(["solve"])
    cards_path = repo_root / cli_defaults.cards
    owned_path = repo_root / cli_defaults.owned
    if not owned_path.exists():
        pytest.skip("the owner's real owned.json is not present")

    cards = load_cards(cards_path)
    owned = load_owned(owned_path, cards)
    cfg = load_scoring_config(repo_root / "config.json")

    def sample(rng, size):
        by_holomem = {}
        for card in sorted(owned, key=lambda item: item.id):
            by_holomem.setdefault(card.member, []).append(card)
            names = sorted(by_holomem)
        chosen = rng.sample(sorted(by_holomem), size)
        return tuple(sorted((rng.choice(by_holomem[n]) for n in chosen), key=lambda c: c.id))

    import random as _random

    rng = _random.Random(2026)
    identical = 0
    for trial in range(30):
        pool = sample(rng, 11)
        ids = [card.id for card in pool]
        kind = trial % 5
        if kind == 0:
            selectors = Selectors(
                require=(rng.choice(ids),), leader=(), exclude=(), include_unowned=False
            )
        elif kind == 1:
            selectors = Selectors(
                require=tuple(rng.sample(ids, 2)), leader=(), exclude=(), include_unowned=False
            )
        elif kind == 2:
            selectors = Selectors(
                require=(), leader=(rng.choice(ids),), exclude=(), include_unowned=False
            )
        elif kind == 3:
            first, second = rng.sample(ids, 2)
            selectors = Selectors(
                require=(first,), leader=(second,), exclude=(), include_unowned=False
            )
        else:
            selectors = Selectors(
                require=(), leader=(), exclude=(rng.choice(ids),), include_unowned=False
            )

        candidate_pool = resolve_selectors(list(pool), list(pool), selectors)
        shared = {
            "constraint": constraint_from(candidate_pool),
            "set_constraint": set_constraint_from(candidate_pool),
            "locked_ids": candidate_pool.locked_ids,
        }
        exact = solve_exact(
            candidate_pool.cards, cfg, top_n=10, leader_id=candidate_pool.leader_id, **shared
        )
        beam = solve_heuristic(candidate_pool.cards, cfg, top_n=10, **shared)
        if arrangements(beam) == arrangements(exact):
            identical += 1
        for team in beam:
            assert candidate_pool.locked_ids <= (set(team.member_ids) | {team.leader_id})
            assert len({card.member for card in team.team.members}) == 5
            if candidate_pool.leader_id is not None:
                assert team.leader_id == candidate_pool.leader_id

    assert identical == 30


# ---------------------------------------------------------------------------
# 2. The regression: one arrangement is not ten
# ---------------------------------------------------------------------------


def test_the_top_n_holds_distinct_arrangements_not_one_repeated():
    """REGRESSION. If `candidates` is a list, four seeds and the swap polish each
    append the same scored arrangements to it, and `heapq.nlargest` over that
    list returns each one once per time it was appended.

    On this exact pool that failure gives ten rows holding only **three**
    distinct arrangements — `c00`, `c01` and `c02` leading the same member set,
    four/four/two times. Exact returns ten distinct leaders over the same set.
    """
    pool = [
        make_card(
            f"c{index:02d}",
            type_=TYPES[index % 3],
            stats=Stats(100 + index * 7, 100 + index * 3, 100 + index * 5),
            passive=gate_passive(30.0 + index, 3) if index % 3 == 0 else None,
        )
        for index in range(10)
    ]
    cfg = make_scoring_config()
    beam = solve_heuristic(pool, cfg, top_n=10)
    exact = solve_exact(pool, cfg, top_n=10)

    assert len(beam) == 10
    assert len(set(arrangements(beam))) == 10, (
        "ten rows must be ten different arrangements — three was the defect"
    )
    assert arrangements(beam) == arrangements(exact)
    assert [team.leader_id for team in beam] == [f"c{i:02d}" for i in range(10)]


# ---------------------------------------------------------------------------
# 3. The game rules the exact solver enforces
# ---------------------------------------------------------------------------


def test_every_returned_team_is_five_distinct_holomem():
    """R1 is an ENUMERATION constraint, not a filter. Two skins of one character
    are not a team, and a beam that only checked card ids would build one."""
    pool = plain_pool(9)
    # Two ids, one holomem: the case a card-id check passes and R1 rejects.
    pool.append(make_card("c09", type_="Cute", stats=Stats(900, 900, 900), member="Member c00"))
    pool.append(make_card("c10", type_="Cool", stats=Stats(900, 900, 900), member="Member c01"))
    cfg = make_scoring_config()
    beam = solve_heuristic(pool, cfg, top_n=10)

    assert len(beam) == 10
    for team in beam:
        assert len(team.team.members) == 5
        assert len({card.member for card in team.team.members}) == 5
        assert len(set(team.member_ids)) == 5
    assert arrangements(beam) == arrangements(solve_exact(pool, cfg, top_n=10))


def test_the_outfit_slot_sits_outside_the_five_and_contributes_no_stats():
    """The leader is a slot outside the members; it
    supplies its costume skill and nothing else. Two leaders whose costume slots
    are both empty but whose STAT LINES differ by 100x must score the same team
    identically — if the beam ever folded a leader's stats in, this separates."""
    members = tuple(
        make_card(f"m{index}", type_=TYPES[index % 3], stats=Stats(100, 100, 100))
        for index in range(5)
    )
    member_ids = tuple(card.id for card in members)
    poor = make_card("lead_poor", type_="Cool", stats=Stats(1, 1, 1))
    rich = make_card("lead_rich", type_="Cool", stats=Stats(9000, 9000, 9000))
    cfg = make_scoring_config()

    def score_of(sixth, leader_id):
        # C(6,5) x 6 = 36 arrangements, so top_n=36 is the whole space and the
        # lookup cannot fail for a ranking reason.
        beam = solve_heuristic(list(members) + [sixth], cfg, top_n=36)
        return next(
            team.score
            for team in beam
            if team.member_ids == member_ids and team.leader_id == leader_id
        )

    # 9,000x the stat line, same score: the outfit slot supplies its costume
    # skill and nothing else.
    assert score_of(poor, "lead_poor") == 1500.0
    assert score_of(rich, "lead_rich") == 1500.0
    # And the leader genuinely need not be one of the five.
    assert "lead_poor" not in member_ids and "lead_rich" not in member_ids


def test_the_reported_score_survives_an_independent_re_score():
    """The beam reports `score_card_set`'s number. Re-scoring the same `Team`
    through `score_with_breakdown` — the single-team path, a different function
    — must reproduce it exactly. A beam that scored partials under one rule and
    reported under another would pass every agreement test above and fail this
    one."""
    pool = gated_pool()
    cfg = make_scoring_config()
    for team in solve_heuristic(pool, cfg, top_n=5):
        assert score_with_breakdown(team.team, cfg).total_score == team.score


# ---------------------------------------------------------------------------
# 4. Determinism (the CLI's reproducibility rests on this)
# ---------------------------------------------------------------------------


def test_the_same_pool_and_the_same_seed_give_the_same_answer():
    pool = plain_pool(9)
    cfg = make_scoring_config()
    first = solve_heuristic(pool, cfg, top_n=10)
    second = solve_heuristic(pool, cfg, top_n=10)

    assert arrangements(first) == arrangements(second)
    assert [team.score for team in first] == [team.score for team in second]
    # Pool order must not matter either: `canonical_pool` is the anchor.
    shuffled = list(reversed(pool))
    assert arrangements(solve_heuristic(shuffled, cfg, top_n=10)) == arrangements(first)


def test_rng_seed_is_a_no_op_below_a_beam_width_of_twenty_four():
    """A design smell worth having written down rather than
    discovered: the extra seeds perturb only the top
    `max(MEMBER_COUNT, beam_width // 4)` cards by stat sum, so **seed diversity
    is keyed off the beam width**. Below `beam_width = 24` that expression pins
    to `MEMBER_COUNT = 5` and `rng_seed` changes nothing at all — same teams,
    same observations, byte for byte.

    Above it the seeds do diverge, but on this pool only the observation trail
    moves; the answer is 19,965.0 either way. So multi-seeding is buying
    robustness that is not visible in the score at these sizes, which is a
    finding, not a failure.
    """
    pool = cluster_pool()
    cfg = make_scoring_config()

    def run(width, rng_seed):
        return solve_heuristic_labelled(
            pool, cfg, top_n=5, beam_width=width, branch=2, seeds=3, rng_seed=rng_seed
        )

    narrow_zero, narrow_99 = run(6, 0), run(6, 99)
    assert [o.detail for o in narrow_zero.observations] == [
        o.detail for o in narrow_99.observations
    ]
    assert len(narrow_zero.observations) == 144

    wide_zero, wide_99 = run(48, 0), run(48, 99)
    assert len(wide_zero.observations) == 233
    assert len(wide_99.observations) == 216
    assert wide_zero.teams[0].score == wide_99.teams[0].score == 19965.0
    assert narrow_zero.teams[0].score == 19965.0


# ---------------------------------------------------------------------------
# 5. The mechanisms, one at a time
# ---------------------------------------------------------------------------


def test_quota_features_are_exactly_the_ones_a_trigger_names():
    """A feature no gate condition and no count-limited effect target names
    cannot change any team's score by being present, so reserving beam width for
    it buys nothing. The plain pool has three types and two generations and must
    therefore yield NO features at all."""
    cfg = make_scoring_config()
    del cfg
    assert _quota_features(canonical_pool(plain_pool(9))) == ()
    assert _quota_features(canonical_pool(gated_pool())) == (("type", "Cute"),)
    assert _quota_features(canonical_pool(cluster_pool())) == (
        ("type", "Cool"),
        ("type", "Cute"),
        ("type", "Passion"),
    )


def test_open_conditions_names_the_reachable_unfired_gate_and_prices_it():
    """The bonus is `magnitude/100` discounted by `1/(1+shortfall)`, summed. Two
    Cute members of a `4+ Cute` gate worth 400.0, with three slots left: each of
    the two carries the gate, each is 2 short, so each contributes
    `4.0 / 3 = 1.333...` and the rate is `2.666...`."""
    cfg = make_scoring_config()
    cute = tuple(card for card in gated_pool() if card.type == "Cute")[:2]
    found, rate = _open_conditions(cute, cfg, 3)

    assert len(found) == 2
    assert found[0].threshold == 4
    assert found[0].shortfall == 2
    assert found[0].magnitude == 400.0
    assert rate == pytest.approx(2.6666666666666665)

    # Unreachable within the slots left is not "open": one slot cannot close a
    # shortfall of two, so the gate is worth zero rather than a discounted
    # fraction.
    assert _open_conditions(cute, cfg, 1) == ((), 0.0)
    # And a gate nobody on the partial carries is not open either.
    cool = tuple(card for card in gated_pool() if card.type == "Cool")[:2]
    assert _open_conditions(cool, cfg, 3) == ((), 0.0)


def test_the_per_feature_branch_is_what_finds_the_gate_carrying_team():
    """REGRESSION, and the reason `DEFAULT_FEATURE_BRANCH` exists.

    Taking only the `branch` best extensions by static potential is not enough.
    Static potential scores a card as the fifth member of a reference skeleton,
    where a `4+ Cute` gate does not fire either — so the six cards that make the
    best team rank last, and at `branch=6` none of them is ever built into a
    state: 3,225.0 against exact's 23,511.0, an **86.3%** miss, from a beam that
    reports only `BEAM_TRUNCATION` and `QUOTA_RESCUE`.

    The per-feature quota cannot repair it: it re-admits partials the global
    cut DROPPED, and a partial that was never built was never dropped.
    """
    pool = gated_pool()
    cfg = make_scoring_config()
    crippled = {"beam_width": 8, "branch": 6, "seeds": 2}

    without = solve_heuristic_labelled(pool, cfg, top_n=1, feature_branch=0, **crippled)
    with_it = solve_heuristic_labelled(pool, cfg, top_n=1, feature_branch=2, **crippled)

    assert without.teams[0].score == 3225.0
    assert without.teams[0].member_ids == ("cool3", "cool4", "cool5", "cool6", "cool7")
    assert with_it.teams[0].score == 23511.0
    assert with_it.teams[0].member_ids == ("cool7", "cute0", "cute1", "cute2", "cute3")
    assert (23511.0 - 3225.0) / 23511.0 == pytest.approx(0.86283, abs=1e-5)
    assert DEFAULT_FEATURE_BRANCH == 2


def test_the_swap_polish_reports_the_gain_it_actually_made():
    """`SWAP_LOCAL_OPTIMUM` is emitted when polish strictly improved on the
    beam's own best — and NOT when it found nothing, which is the informative
    half. Turning polish off must lose the improvement, or the label was
    describing something else."""
    pool = gated_pool()
    cfg = make_scoring_config()
    tuned = {"beam_width": 4, "branch": 4, "seeds": 4, "feature_branch": 1}

    polished = solve_heuristic_labelled(pool, cfg, top_n=1, polish_teams=3, **tuned)
    unpolished = solve_heuristic_labelled(pool, cfg, top_n=1, polish_teams=0, **tuned)

    assert FailureMode.SWAP_LOCAL_OPTIMUM in polished.modes()
    assert FailureMode.SWAP_LOCAL_OPTIMUM not in unpolished.modes()
    gains = [
        observation.margin
        for observation in polished.observations
        if observation.mode is FailureMode.SWAP_LOCAL_OPTIMUM
    ]
    assert gains and all(gain > 0.0 for gain in gains)
    assert polished.teams[0].score == 23511.0


# ---------------------------------------------------------------------------
# 6. Failure modes are OBSERVED, never assumed
# ---------------------------------------------------------------------------


def test_a_run_that_observes_nothing_reports_nothing():
    """The claim the whole labelling scheme rests on. Six cards and a beam wide
    enough to hold every partial: nothing is ever cut, nothing rescued, nothing
    unreached, no swap improves, and the observation list is EMPTY.

    A label emitted on every approximate run would be worth exactly as much as
    no label at all; this is the test that says these are not that.
    """
    pool = [make_card(f"s{index}", stats=Stats(100 + index, 100, 100)) for index in range(6)]
    cfg = make_scoring_config()
    result = solve_heuristic_labelled(pool, cfg, top_n=3)

    assert result.observations == ()
    assert result.modes() == frozenset()
    assert result.teams[0].score == 1515.0
    assert arrangements(result.teams) == arrangements(solve_exact(pool, cfg, top_n=3))


@pytest.mark.parametrize(
    "mode, pool_factory, options",
    (
        (
            FailureMode.BEAM_TRUNCATION,
            lambda: [make_card(f"s{i}", stats=Stats(100 + i, 100, 100)) for i in range(6)],
            {"beam_width": 2, "branch": 3, "seeds": 2},
        ),
        (FailureMode.QUOTA_RESCUE, gated_pool, {"beam_width": 8, "branch": 6, "seeds": 2}),
        (
            FailureMode.OPEN_CONDITION_RESCUE,
            gated_pool,
            {"beam_width": 8, "branch": 6, "seeds": 2, "feature_branch": 2},
        ),
        (
            FailureMode.PAIRWISE_GATE_MISS,
            gated_pool,
            {"beam_width": 8, "branch": 6, "seeds": 2, "feature_branch": 0},
        ),
        (
            FailureMode.EXTENSION_HORIZON,
            gated_pool,
            {"beam_width": 8, "branch": 6, "seeds": 2, "feature_branch": 0},
        ),
        (
            FailureMode.SWAP_LOCAL_OPTIMUM,
            gated_pool,
            {"beam_width": 4, "branch": 4, "seeds": 4, "feature_branch": 1},
        ),
        (
            FailureMode.SEED_DISAGREEMENT,
            cluster_pool,
            {
                "beam_width": 48,
                "branch": 2,
                "seeds": 2,
                "feature_branch": 0,
                "polish_teams": 0,
            },
        ),
    ),
)
def test_every_failure_mode_is_reachable_and_names_something_concrete(
    mode, pool_factory, options
):
    """Each of the seven modes, emitted by a pool and a configuration built to
    produce that situation. A mode no run can ever emit is dead code pretending
    to be a safety property; this is the test that would catch it."""
    cfg = make_scoring_config()
    result = solve_heuristic_labelled(pool_factory(), cfg, top_n=1, **options)

    assert mode in result.modes(), f"{mode.value} was never observed"
    found = [
        observation for observation in result.observations if observation.mode is mode
    ]
    for observation in found:
        assert observation.detail.strip()
        # "may have been missed" is a caveat; a card id, a gate or a level is a
        # finding. Every detail must carry at least one.
        assert any(
            token in observation.detail
            for token in ("level", "card", "cute", "cool", "quota", "seeds", "pool")
        ), observation.detail


def test_the_horizon_names_the_cards_no_state_was_ever_built_with():
    """`EXTENSION_HORIZON` is computed from the ids the run actually reached,
    not from `branch` and `len(pool)`. It must name the cards, and it must be
    ABSENT when the branch covers the pool — otherwise it is a constant."""
    pool = gated_pool()
    cfg = make_scoring_config()
    narrow = solve_heuristic_labelled(
        pool, cfg, top_n=1, beam_width=8, branch=6, seeds=2, feature_branch=0
    )
    wide = solve_heuristic_labelled(pool, cfg, top_n=1)

    horizon = [
        observation
        for observation in narrow.observations
        if observation.mode is FailureMode.EXTENSION_HORIZON
    ]
    assert len(horizon) == 1
    assert "4 of 14 pool cards were never extended into" in horizon[0].detail
    assert "cute2" in horizon[0].detail
    assert horizon[0].margin == pytest.approx(4 / 14)

    # `branch=24` covers a 14-card pool, so nothing is unreached.
    assert FailureMode.EXTENSION_HORIZON not in wide.modes()


def test_an_unreached_gate_carrier_raises_the_named_failure_mode():
    """The pairwise-gate miss in its harshest form. `PAIRWISE_GATE_MISS` must
    fire not only when a partial carrying a 2+-short gate is EVICTED but also
    when the gate's carriers are never reached at all — then nothing is
    evicted, and an eviction-only label would say nothing."""
    pool = gated_pool()
    cfg = make_scoring_config()
    result = solve_heuristic_labelled(
        pool, cfg, top_n=1, beam_width=8, branch=6, seeds=2, feature_branch=0
    )
    misses = [
        observation
        for observation in result.observations
        if observation.mode is FailureMode.PAIRWISE_GATE_MISS
        and "never reached" in observation.detail
    ]
    assert misses, "the unreached gate carrier must raise PAIRWISE_GATE_MISS"
    assert "type:Cute>=4" in misses[0].detail
    assert result.teams[0].score == 3225.0  # and the run really was that wrong


# ---------------------------------------------------------------------------
# 7. Slot order
# ---------------------------------------------------------------------------


def test_the_objective_is_a_function_of_the_canonical_member_set_so_dedup_is_lossless():
    """The beam deduplicates states by member SET. That is lossless because the
    beam presents every set to `score_card_set` in ONE canonical order —
    `tuple(sorted(slots, key=card.id))` (heuristic.py builds members that way at
    every level and in `_partial_value`) — so the objective it evaluates is a
    deterministic function of the set, and two states holding the same cards in
    different commit orders carry the identical value.

    Recipients are ranked by the stat the effect raises (690/690 server rows).
    The payout is
    `rate * that stat`, so even when two matchers TIE on it and slot order picks
    between them, the SUM is the same either way — the objective is
    order-invariant by construction, checked here first over all 120 orders.
    Which member is NAMED on a tie still follows slot order, and the beam's
    canonical ascending-id order fixes that deterministically (the path is
    covered in `tests/test_slot_order.py` section 4b).

    Also pins that each count-1 effect reaches the top of ITS OWN stat:
    performance and technique go to different members here.
    """
    cute_one = EffectTarget(kind="type", value="Cute", count=1)

    def stat_passive(effect_type):
        return make_skill(
            "PASSIVE",
            family="stat_up",
            effect_type=effect_type,
            magnitude=50.0,
            target=cute_one,
        )

    # Performance 300 / 160 / 140: A is the unique performance recipient.
    # Technique 0 / 100 / 100: B and C TIE, slot order names one of them, and the
    # payout is 0.5 * 100 = 50 whichever it is — so every ordering scores the same.
    a = make_card("A", type_="Cute", stats=Stats(300, 0, 0))
    b = make_card("B", type_="Cute", stats=Stats(160, 100, 0))
    c = make_card("C", type_="Cute", stats=Stats(140, 100, 0))
    d = make_card("D", type_="Cool", passive=stat_passive("performance"))
    e = make_card("E", type_="Cool", passive=stat_passive("technique"))
    members = (a, b, c, d, e)
    cfg = make_scoring_config()

    values = {
        score_card_set(order, (a,), cfg)[0].total_score
        for order in itertools.permutations(members)
    }
    assert len(values) == 1, (
        "the objective must not depend on member order"
    )
    # performance -> A: +150. technique -> B or C: +50. A slot-order rule like
    # `best_order` puts BOTH effects on the first Cute in the deck, and its best
    # deck leads with A (+150, and A has no technique to raise): 50 short.
    best_order = dataclasses.replace(cfg, slot_order_rule="best_order")
    assert score_card_set(members, (a,), best_order)[0].total_score == values.pop() - 50.0

    # The beam's canonicalization (ascending id) is what makes dedup lossless even
    # on an exact TIE: three equal-total Cute, count-1 effects, presented in
    # ascending-id order pick the id-earliest member deterministically. Two commit
    # orders of the same set therefore carry the identical value under the beam.
    ta = make_card("A", type_="Cute", stats=Stats(300, 100, 100))
    tb = make_card("B", type_="Cute", stats=Stats(100, 300, 100))
    tc = make_card("C", type_="Cute", stats=Stats(100, 100, 300))
    tie_members = (ta, tb, tc, d, e)
    canonical = tuple(sorted(tie_members, key=lambda card: card.id))
    canonical_value = score_card_set(canonical, (ta,), cfg)[0].total_score
    # Reordering the commit order but re-canonicalizing (as the beam does) is stable.
    for order in itertools.permutations(tie_members):
        recanon = tuple(sorted(order, key=lambda card: card.id))
        assert score_card_set(recanon, (ta,), cfg)[0].total_score == canonical_value


def test_the_beam_reports_the_winning_slot_order_for_each_returned_team():
    """A tool that recommends a team without its slot order hands the player
    the median case. `None` is the honest report when no
    count-limited effect binds and every ordering scores identically."""
    cute_one = EffectTarget(kind="type", value="Cute", count=1)
    a = make_card("A", type_="Cute", stats=Stats(300, 100, 100))
    b = make_card("B", type_="Cute", stats=Stats(100, 300, 100))
    c = make_card("C", type_="Cute", stats=Stats(100, 100, 300))
    d = make_card(
        "D",
        type_="Cool",
        passive=make_skill(
            "PASSIVE",
            family="stat_up",
            effect_type="performance",
            magnitude=50.0,
            target=cute_one,
        ),
    )
    e = make_card("E", type_="Cool")
    f = make_card("F", type_="Passion", stats=Stats(90, 90, 90))
    cfg = make_scoring_config()
    result = solve_heuristic_labelled([a, b, c, d, e, f], cfg, top_n=3)

    assert set(result.member_orders) == {team.member_ids for team in result.teams}
    for member_ids, order in result.member_orders.items():
        assert order is None or sorted(order) == sorted(member_ids)
