"""`bench/validate_search.py` and the two hooks it needs from
`solver/heuristic.py`, pinned by NUMBERS rather than by directions.

The harness's own failure mode is the one `CLAUDE.md`'s third practice names:
a validation script that still runs, still exits 0, and still prints a
reassuring table while comparing the beam against something that is not
`solve_exact`, or while attributing a loss to the wrong level. Every assertion
here therefore pins a value, and the two behaviour-changing ones
(`open_condition_mode`, `trace=`) also assert what the code answered BEFORE the
change, so a regression cannot pass by moving in the right direction.
"""

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

from hbuilder.domain.card import Card, GateCondition, Stats
from hbuilder.solver.exact import solve_exact
from hbuilder.solver.heuristic import (
    OPEN_CONDITION_MODES,
    _open_conditions,
    solve_heuristic_labelled,
)

from conftest import make_card, make_scoring_config, make_skill

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "bench" / "validate_search.py"

# Loaded by file path, never as a package import: `bench/` is a standalone
# script directory outside `src/` and must stay unreachable from the CLI's own
# import graph (D-25/T-03-32), exactly as the sibling bench smoke tests do.
_spec = importlib.util.spec_from_file_location("validate_search", SCRIPT)
assert _spec is not None and _spec.loader is not None
validate_search = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_search)


def _stats(value: int) -> Stats:
    return Stats(performance=value, technique=value, sense=value)


def _costume(magnitude: float, effect_type: str = "all_stats"):
    return make_skill(
        "COSTUME", family="stat_up", effect_type=effect_type, magnitude=magnitude
    )


def _pool(size: int) -> tuple[Card, ...]:
    """`size` cards, one holomem each, with a spread of stat lines and one
    costume magnitude per card so the leader role actually discriminates."""
    return tuple(
        make_card(
            f"card-{index:03d}",
            type_="Cute" if index % 2 == 0 else "Cool",
            stats=_stats(100 + 7 * index),
            costume_skill=_costume(float(10 + (index % 4) * 5)),
        )
        for index in range(size)
    )


# ---------------------------------------------------------------------------
# The parallel exact solver IS the reference truth, so it must be exact
# ---------------------------------------------------------------------------


def test_the_parallel_exact_solver_returns_what_solve_exact_returns() -> None:
    """The whole harness rests on this: if the parallel reference is not
    `solve_exact`'s answer, every hit rate it reports is measuring the wrong
    thing while looking fine.

    Compared as the full Top-10 arrangement LIST — score, leader id and member
    ids, in order — not as a set and not on the top-1 alone, because a
    divergence in the D-04 tie-break would leave the top-1 intact and reorder
    everything under it.
    """
    cfg = make_scoring_config()
    pool = _pool(11)
    reference = solve_exact(pool, cfg, top_n=10)
    parallel = validate_search.solve_exact_parallel(pool, cfg, top_n=10, workers=0)

    assert len(reference) == 10
    assert len(parallel) == 10
    assert [
        (team.score, team.leader_id, team.member_ids) for team in reference
    ] == list(parallel)


def test_the_parallel_partition_covers_every_member_set_exactly_once() -> None:
    """The `(i, j)` partition is a correctness claim, not just a scheduling
    one: a pair missed is a set never scored, and a pair double-counted is a
    duplicate arrangement in the merge. `C(11,5) = 462` sets, each appearing
    once."""
    pool = _pool(11)
    size = len(pool)
    tasks = [
        (first, second)
        for first in range(size - 5 + 1)
        for second in range(first + 1, size - 5 + 2)
    ]
    import itertools

    seen: list[tuple[str, ...]] = []
    for first, second in tasks:
        for rest in itertools.combinations(pool[second + 1 :], 3):
            seen.append(tuple(card.id for card in (pool[first], pool[second], *rest)))
    assert len(seen) == 462
    assert len(set(seen)) == 462
    assert set(seen) == {
        tuple(card.id for card in combo) for combo in itertools.combinations(pool, 5)
    }


# ---------------------------------------------------------------------------
# The two hooks added to `solver/heuristic.py`
# ---------------------------------------------------------------------------


def test_a_traced_run_returns_exactly_what_an_untraced_run_returns() -> None:
    """`trace=` is pure observation. Nothing in `_run_beam` reads it back, and
    this pins that: a traced run and an untraced run must agree on the whole
    Top-10 and on both counters, or the harness would be measuring a beam that
    only exists while it is being watched."""
    cfg = make_scoring_config()
    pool = _pool(14)
    plain = solve_heuristic_labelled(pool, cfg, top_n=10)
    trace: list[dict] = []
    traced = solve_heuristic_labelled(pool, cfg, top_n=10, trace=trace)

    assert [(t.score, t.leader_id, t.member_ids) for t in plain.teams] == [
        (t.score, t.leader_id, t.member_ids) for t in traced.teams
    ]
    assert plain.arrangements_scored == traced.arrangements_scored
    assert plain.partials_scored == traced.partials_scored
    assert trace, "a run over a 14-card pool must record at least one level"
    assert {record["level"] for record in trace} == {1, 2, 3, 4, 5}


def test_open_condition_modes_price_the_same_gate_differently() -> None:
    """The defect at `heuristic.py`'s open-condition credit, pinned as three
    NUMBERS.

    One card carries `type:Cute >= 4` at magnitude 40. On a two-Cute partial
    with three slots left the shortfall is 2, so:

    - `discounted` (the pre-change, shipped answer) charges
      `(40/100) / (1 + 2) = 0.13333...` — it UNDER-credits, because the gate,
      if it fires, pays its full 0.40;
    - `full` charges `40/100 = 0.40` — the largest the gate can be worth;
    - `off` charges `0.0`.

    `discounted` is asserted at its exact pre-change value so this test fails
    if the default is quietly switched, not merely if the arithmetic drifts.
    """
    cfg = make_scoring_config()
    gated = make_card(
        "card-gated",
        type_="Cute",
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            probability_tier="High",
            magnitude=40.0,
            condition=GateCondition(kind="type", target="Cute", count=4),
        ),
    )
    partner = make_card("card-plain", type_="Cute")
    members = (gated, partner)

    _, discounted = _open_conditions(members, cfg, 3, "discounted")
    _, full = _open_conditions(members, cfg, 3, "full")
    _, off = _open_conditions(members, cfg, 3, "off")

    assert discounted == 40.0 / 100.0 / 3.0
    assert abs(discounted - 0.13333333333333333) < 1e-15
    assert full == 0.4
    assert off == 0.0
    # And the relationship that makes it a defect rather than a taste: the
    # shipped credit is 3x smaller than the payoff the gate actually delivers.
    assert full / discounted == 3.0


def test_the_shipped_open_condition_mode_is_the_pre_change_default() -> None:
    """`solve_heuristic_labelled`'s default must still be the answer it gave
    before `open_condition_mode` existed, or adding the knob would have
    silently changed every shipped result."""
    cfg = make_scoring_config()
    pool = _pool(12)
    default = solve_heuristic_labelled(pool, cfg, top_n=10)
    explicit = solve_heuristic_labelled(
        pool, cfg, top_n=10, open_condition_mode="discounted"
    )
    assert [(t.score, t.leader_id, t.member_ids) for t in default.teams] == [
        (t.score, t.leader_id, t.member_ids) for t in explicit.teams
    ]
    assert OPEN_CONDITION_MODES == {"discounted", "full", "off"}


def test_an_unknown_open_condition_mode_is_rejected_not_ignored() -> None:
    cfg = make_scoring_config()
    try:
        solve_heuristic_labelled(_pool(8), cfg, open_condition_mode="admissible")
    except ValueError as error:
        assert "open_condition_mode" in str(error)
    else:  # pragma: no cover
        raise AssertionError("an unknown mode must raise, not fall through")


# ---------------------------------------------------------------------------
# Loss attribution — the part a hit rate cannot give you
# ---------------------------------------------------------------------------


def test_loss_attribution_separates_never_built_from_evicted() -> None:
    """`never_built` and `evicted` have different fixes — widening the beam
    repairs one and cannot touch the other — so conflating them would make the
    report actively misleading."""
    target = ("a", "b", "c", "d", "e")

    # Built at level 2 and then cut: `evicted`.
    evicted_trace = [
        {"seed": 0, "level": 1, "built": frozenset({frozenset({"a"})}),
         "kept": frozenset({frozenset({"a"})})},
        {"seed": 0, "level": 2, "built": frozenset({frozenset({"a", "b"})}),
         "kept": frozenset({frozenset({"x", "y"})})},
    ]
    evicted = validate_search.attribute_loss(target, evicted_trace)
    assert evicted["lost_at_level"] == 2
    assert evicted["reason"] == validate_search.LOSS_EVICTED
    assert evicted["subsets_built"] == 1
    assert evicted["subsets_possible"] == 10  # C(5,2)

    # Never generated at level 1 at all: `never_built`.
    unbuilt_trace = [
        {"seed": 0, "level": 1, "built": frozenset({frozenset({"z"})}),
         "kept": frozenset({frozenset({"z"})})},
    ]
    unbuilt = validate_search.attribute_loss(target, unbuilt_trace)
    assert unbuilt["lost_at_level"] == 1
    assert unbuilt["reason"] == validate_search.LOSS_NEVER_BUILT
    assert unbuilt["subsets_built"] == 0
    assert unbuilt["subsets_possible"] == 5

    # Survived every level: not a search failure at all.
    survived = [
        {
            "seed": 0,
            "level": level,
            "built": frozenset({frozenset(target[:level])}),
            "kept": frozenset({frozenset(target[:level])}),
        }
        for level in range(1, 6)
    ]
    intact = validate_search.attribute_loss(target, survived)
    assert intact["lost_at_level"] is None
    assert intact["reason"] == validate_search.LOSS_NOT_A_SEARCH_FAILURE


def test_loss_attribution_pools_across_seeds_rather_than_requiring_all() -> None:
    """The optimum only has to survive in ONE seed to still be reachable. A
    per-seed reading would report a loss on every multi-seed run."""
    target = ("a", "b", "c", "d", "e")
    trace = [
        {"seed": 0, "level": 1, "built": frozenset({frozenset({"q"})}),
         "kept": frozenset({frozenset({"q"})})},
        {"seed": 1, "level": 1, "built": frozenset({frozenset({"a"})}),
         "kept": frozenset({frozenset({"a"})})},
    ]
    assert validate_search.attribute_loss(target, trace)["lost_at_level"] == 2


def test_binding_reports_the_level_where_the_width_actually_cuts() -> None:
    """The measurement the whole design rests on: a comparison run at a size
    where nothing is ever cut is vacuous, and `binds` is what says so."""
    trace = [
        {"seed": 0, "level": 1, "built": frozenset(
            frozenset({f"c{i}"}) for i in range(3)), "kept": frozenset()},
        {"seed": 0, "level": 2, "built": frozenset(
            frozenset({f"c{i}", "z"}) for i in range(9)), "kept": frozenset()},
    ]
    profile = validate_search.binding_profile(trace, beam_width=4)
    assert profile["binds"] is True
    assert profile["binding_levels"] == [2]
    assert profile["max_children_per_level"] == {"1": 3, "2": 9}

    wide = validate_search.binding_profile(trace, beam_width=96)
    assert wide["binds"] is False
    assert wide["binding_levels"] == []


# ---------------------------------------------------------------------------
# Leader dominance — an EXACT reduction, so it must be exactly exact
# ---------------------------------------------------------------------------


def test_leader_dominance_never_changes_the_best_score() -> None:
    """**The assertion worth more than the speedup.** Enumerate leaders both
    ways over the same member sets and require BIT-IDENTICAL best scores. If
    the dominance rule were wrong this is where it shows, and a class count
    never would have said so."""
    cfg = make_scoring_config()
    pool = _pool(12)
    survivors = validate_search.dominant_leaders(pool, cfg)
    assert 0 < len(survivors) < len(pool)

    report = validate_search.verify_leader_dedup(pool, cfg, 40, random.Random(7))
    assert report["violations"] == 0
    assert report["worst_delta"] == 0.0
    assert report["leaders_reduced"] == len(survivors)


def test_leader_dominance_keeps_ties_rather_than_collapsing_them() -> None:
    """Two leaders with the identical signature AND magnitude score
    identically, so keeping one would still find the best SCORE — but
    `solve_exact`'s Top-N contains both and D-04's tie-break decides which is
    rank 1. Dropping a tie would change the reported answer, which an exact
    reduction may not do.

    `_pool(12)` gives four magnitudes (10/15/20/25) cycling over 12 cards, so
    exactly the three cards at magnitude 25 survive."""
    cfg = make_scoring_config()
    pool = _pool(12)
    survivors = validate_search.dominant_leaders(pool, cfg)
    assert len(survivors) == 3
    assert {card.costume_skill.magnitude for card in survivors} == {25.0}


def test_leader_dominance_never_compares_across_effect_types() -> None:
    """`+120 % sense` and `+50 % all_stats` are not comparable — which wins
    depends on the team's stat distribution — so they must land in different
    signatures and BOTH survive, however far apart their magnitudes are."""
    cfg = make_scoring_config()

    def costumed(index: int, effect_type: str, magnitude: float) -> Card:
        return make_card(
            f"card-{index:03d}",
            stats=_stats(100),
            costume_skill=_costume(magnitude, effect_type),
        )

    pool = (
        costumed(0, "sense", 120.0),
        costumed(1, "all_stats", 50.0),
        costumed(2, "all_stats", 40.0),
    )
    survivors = validate_search.dominant_leaders(pool, cfg)
    assert {card.id for card in survivors} == {"card-000", "card-001"}


def test_a_board_configuration_puts_the_holomem_back_in_the_key() -> None:
    """Red (leader-scope) nodes are per-holomem, so with a board configured two
    cards of different characters are not interchangeable even with identical
    outfits. Derived from the configuration, never assumed."""
    card = _pool(1)[0]
    without = validate_search.outfit_signature(card, board_discriminates=False)
    with_board = validate_search.outfit_signature(card, board_discriminates=True)

    # Asserted as a PROPERTY of the key, not as a padding convention. The
    # original spelling was `without[-1] == ""` -- this harness's own
    # implementation padded the holomem slot with an empty string so both
    # tuples stayed the same length. `src/hbuilder/solver/dominance.py`, which
    # this now delegates to, appends the element rather than padding it. The
    # property is identical either way and is the thing that has to hold: the
    # holomem is in the key when a board discriminates and absent otherwise.
    assert card.member not in without
    assert card.member in with_board
    assert with_board[: len(without)] == without
    assert without != with_board


def test_wilson_interval_stays_inside_the_unit_and_is_not_degenerate_at_all_hits() -> None:
    """`SEARCH-STRATEGY.md` §6.2 asks for a confidence interval instead of
    `11/11`. The normal approximation reports a ZERO-WIDTH interval at
    `hits == trials`, which is exactly the case being reported — that is the
    reason Wilson is used, and it is worth pinning."""
    low, high = validate_search.wilson_interval(30, 30)
    assert 0.0 < low < 1.0
    assert high == 1.0
    assert abs(low - 0.88649) < 0.0001

    low, high = validate_search.wilson_interval(11, 11)
    assert abs(low - 0.74112) < 0.0001
    assert high == 1.0
