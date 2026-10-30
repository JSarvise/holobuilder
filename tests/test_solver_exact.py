"""D-19 (the bounded-heap Top-N guarantee) and Phase 2's D-04 (the locked
tie-break order): `solve_exact` selects its Top-N result with
`heapq.nlargest` over a generator, never by collecting every scored
arrangement into a list or tuple and then sorting.

Collect-then-sort Top-N selection is not merely wasteful at the full
~54-card collection (C(54, 6) x 6 = 154,962,990 arrangements) — it is
impossible: materialising that many scored records is gigabytes for a
single run. A future contributor "simplifying" the bounded heap into a
materialised list must trip a named test here that explains why, rather
than discover the regression as an out-of-memory crash at the full pool.

This file also pins enumeration exhaustiveness and the locked tie-break
key (SOLV-01, CLI-06) so both properties are proven behaviorally, not
merely by reading the source.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import math
import tracemalloc
from pathlib import Path

import pytest

from hbuilder.solver.exact import canonical_pool, iter_candidates, solve_exact

from conftest import make_card, make_scoring_config

REPO_ROOT = Path(__file__).resolve().parent.parent
EXACT_PATH = REPO_ROOT / "src" / "hbuilder" / "solver" / "exact.py"


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{EXACT_PATH} must define a {name}() function")


def test_solve_exact_selects_top_n_with_a_bounded_heap_never_a_materialised_list():
    # An AST structural pin, scoped to solve_exact's own FunctionDef only —
    # canonical_pool and _heap_key legitimately order/sort things elsewhere
    # in the same file, and this test must not trip on their code.
    tree = ast.parse(EXACT_PATH.read_text(encoding="utf-8"), filename=str(EXACT_PATH))
    solve_exact_def = _find_function(tree, "solve_exact")

    # (a) exactly one call to nlargest.
    nlargest_calls = [
        node
        for node in ast.walk(solve_exact_def)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "nlargest"
    ]
    assert len(nlargest_calls) == 1, (
        "solve_exact must select Top-N with exactly one heapq.nlargest call "
        "(D-19) — this is the bounded-heap seam every downstream plan "
        "(03-03's batch API, Phase 4's local-swap loop) inherits"
    )

    # (b) no collect-then-sort escape hatch: no sorted()/list() call, and no
    # list comprehension, anywhere in solve_exact's body. A tuple(...) call
    # is permitted only when it wraps the already-bounded nlargest(...)
    # result (the function's own return statement) — wrapping the raw
    # candidate stream in tuple(...) before selecting Top-N would
    # materialise every arrangement, exactly the defect this test pins.
    forbidden_name_calls = [
        node
        for node in ast.walk(solve_exact_def)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"sorted", "list"}
    ]
    assert forbidden_name_calls == [], (
        "solve_exact must never call sorted() or list() on the candidate "
        "stream — collecting then sorting is impossible at the full "
        "~54-card pool (154,962,990 arrangements), not merely wasteful"
    )

    list_comps = [node for node in ast.walk(solve_exact_def) if isinstance(node, ast.ListComp)]
    assert list_comps == [], (
        "solve_exact must never accumulate the candidate stream via a list "
        "comprehension — the generator must be consumed directly by "
        "heapq.nlargest so memory stays bounded at top_n"
    )

    tuple_calls = [
        node
        for node in ast.walk(solve_exact_def)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tuple"
    ]
    for call in tuple_calls:
        wraps_nlargest = (
            len(call.args) == 1
            and isinstance(call.args[0], ast.Call)
            and isinstance(call.args[0].func, ast.Attribute)
            and call.args[0].func.attr == "nlargest"
        )
        assert wraps_nlargest, (
            "any tuple(...) call inside solve_exact must wrap only the "
            "already-bounded nlargest(...) result — wrapping the raw "
            "candidate stream in tuple(...) before selecting Top-N would "
            "materialise every arrangement (D-19)"
        )

    # (c) the first positional argument to nlargest after top_n (i.e. the
    # candidate stream itself) must be a live iterator expression — a Call
    # (the generator function invocation) or a GeneratorExp — never an
    # ast.List or ast.ListComp, which would mean the stream was
    # materialised before nlargest ever saw it.
    nlargest_call = nlargest_calls[0]
    assert len(nlargest_call.args) >= 2, (
        "heapq.nlargest must be called with both n and an iterable "
        "positional argument"
    )
    candidate_stream_arg = nlargest_call.args[1]
    assert isinstance(candidate_stream_arg, (ast.Call, ast.GeneratorExp)), (
        "the candidate stream handed to nlargest must be a generator "
        f"expression or a generator-function call, got {type(candidate_stream_arg).__name__}"
    )


def test_iter_candidates_is_a_generator_function_so_the_stream_is_never_realised():
    assert inspect.isgeneratorfunction(iter_candidates), (
        "iter_candidates must be a generator function — solve_exact relies "
        "on consuming it lazily via heapq.nlargest (D-19)"
    )

    pool = canonical_pool([make_card("only_a"), make_card("only_b")])
    stream = iter_candidates(pool, make_scoring_config())
    assert inspect.isgenerator(stream), (
        "calling iter_candidates must return a real generator object, not "
        "a list or tuple already realised in memory"
    )


def test_enumeration_is_exhaustive_over_every_leader_and_five_member_arrangement():
    cards = [make_card(f"card_{i:02d}") for i in range(7)]
    pool = canonical_pool(cards)
    cfg = make_scoring_config()

    # 5 members chosen from 7, times a leader chosen from ALL 7 -- the outfit
    # slot sits outside the five and may reuse one of them (§27/§38).
    results = list(iter_candidates(pool, cfg))
    expected_count = math.comb(7, 5) * 7
    assert len(results) == expected_count

    pairs = [(candidate.leader_id, candidate.member_ids) for candidate in results]
    assert len(set(pairs)) == expected_count, (
        "every (leader_id, member_ids) pair must be distinct — a "
        "duplicate means the same arrangement was scored twice"
    )

    expected_pairs: set[tuple[str, tuple[str, ...]]] = set()
    for members in itertools.combinations(pool, 5):
        member_ids = tuple(card.id for card in members)
        for leader in pool:  # including a leader who is also a member (§27)
            expected_pairs.add((leader.id, member_ids))

    assert set(pairs) == expected_pairs, (
        "iter_candidates must yield every leader choice for every 5-member "
        "combination (SOLV-01) — a mismatch here means some arrangement "
        "was skipped or an unexpected one was produced"
    )


def test_exactly_equal_scores_break_ties_by_leader_id_then_member_ids_and_never_reorder():
    # Clones: identical stats, no skills at all, so base_stat_sum is
    # constant across every 6-of-7 combination and every arrangement
    # scores exactly the same float. Exact float equality is the point —
    # no tolerance comparison anywhere in this file.
    cards = [make_card(f"clone_{i:02d}") for i in range(7)]
    cfg = make_scoring_config()

    result_a = solve_exact(cards, cfg, top_n=1000)
    result_b = solve_exact(cards, cfg, top_n=1000)

    assert result_a == result_b, (
        "two solve_exact runs over identical inputs must return identical "
        "results (CLI-06) — exact ties must never reorder between runs"
    )

    scores = {candidate.score for candidate in result_a}
    assert len(scores) == 1, (
        "a clone pool (identical stats, no skills) must produce exactly "
        "one distinct score across every arrangement — otherwise this is "
        "not actually exercising the tie-break path"
    )

    target_keys = [
        (-candidate.score, candidate.leader_id, candidate.member_ids) for candidate in result_a
    ]
    assert target_keys == sorted(target_keys), (
        "the result sequence must be non-decreasing under "
        "(-score, leader_id, member_ids) — Phase 2's D-04 locked tie-break "
        "order"
    )


def test_pool_of_exactly_five_solves_because_the_leader_may_also_be_a_member():
    """Five cards are exactly enough now: they are the five members, and the
    leader is drawn from the same pool. Under the old model this was the
    empty edge."""
    cards = [make_card(f"only_{i}") for i in range(5)]
    cfg = make_scoring_config()

    assert len(solve_exact(cards, cfg)) == 5, "one member set, five leaders"


def test_pool_smaller_than_five_yields_no_arrangements_and_returns_an_empty_result():
    cards = [make_card(f"only_{i}") for i in range(4)]
    cfg = make_scoring_config()

    assert solve_exact(cards, cfg) == (), (
        "a pool with fewer than MEMBER_COUNT cards must yield zero "
        "arrangements and solve_exact must return an empty tuple rather than "
        "raising (the SOLV-01 empty edge)"
    )


def test_enumeration_order_is_independent_of_the_input_card_order():
    cards_forward = [make_card(f"card_{i:02d}") for i in range(7)]
    cards_reversed = list(reversed(cards_forward))
    cfg = make_scoring_config()

    result_forward = solve_exact(cards_forward, cfg, top_n=42)
    result_reversed = solve_exact(cards_reversed, cfg, top_n=42)

    assert result_forward == result_reversed, (
        "solve_exact's result must be a pure function of the id set, "
        "independent of owned.json's array order — canonical_pool is what "
        "anchors CLI-06's determinism guarantee, not incidental input order"
    )


@pytest.mark.slow
def test_top_n_selection_stays_memory_bounded_on_a_pool_where_materialising_would_be_visible():
    # 16 cards -> C(16, 6) x 6 = 48,048 arrangements. Materialising one
    # small record per arrangement would cost roughly 10 MB; a bounded
    # heap over top_n=10 should stay well under that. At the full ~54-card
    # pool (154,962,990 arrangements) the same collect-then-sort mistake
    # would cost gigabytes, not merely be slow (D-19).
    cards = [make_card(f"card_{i:02d}") for i in range(16)]
    cfg = make_scoring_config()

    tracemalloc.start()
    try:
        result = solve_exact(cards, cfg, top_n=10)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(result) == 10
    assert peak < 4 * 1024 * 1024, (
        f"peak traced memory was {peak} bytes, exceeding the 4 MiB bound. "
        "D-19's bounded heap is what keeps memory at top_n regardless of "
        "pool size — at the full ~54-card pool (154,962,990 arrangements) "
        "a materialised list instead of a streamed bounded heap would use "
        "gigabytes rather than a few kilobytes"
    )


# --- the constraint must prune WORK, not just output -------------------------


def test_locked_ids_skip_scoring_entirely_for_member_sets_that_cannot_qualify(monkeypatch):
    """`--require` promises a smaller run (`project_arrangements` reports
    `C(N-k, 5-k)` sets), and the solver must actually do that much work.

    Applying the constraint only per-(set, leader) AFTER `score_card_set` has
    run makes the flag prune the OUTPUT while the enumeration still scores
    every set — the projection and the real cost then disagree, which is the
    same defect shape as a payload reporting a count the solver never used.
    Counted here rather than timed, so it fails deterministically."""
    from hbuilder.solver import exact as exact_mod

    cards = [make_card(f"card_{i:02d}") for i in range(10)]
    pool = canonical_pool(cards)
    cfg = make_scoring_config()

    calls = 0
    real = exact_mod.score_card_set

    def counting(members, leaders, config):
        nonlocal calls
        calls += 1
        return real(members, leaders, config)

    monkeypatch.setattr(exact_mod, "score_card_set", counting)

    # Four of the five member slots locked: only the sets containing all four
    # can ever yield an arrangement.
    locked = {"card_00", "card_01", "card_02", "card_03"}

    def constraint(members, leader):
        present = {c.id for c in members} | {leader.id}
        return locked.issubset(present)

    def set_constraint(members):
        return len(locked - {c.id for c in members}) <= 1

    list(
        exact_mod.iter_candidates(
            pool, cfg, constraint=constraint, set_constraint=set_constraint
        )
    )

    unconstrained_sets = math.comb(10, 5)
    assert calls < unconstrained_sets, (
        f"score_card_set ran {calls} times out of {unconstrained_sets} possible "
        "member sets -- the lock pruned nothing, so --require costs as much as "
        "an unconstrained run while the projection claims otherwise"
    )
    # C(6,1) sets hold all four locks; C(6,2)*4 defer exactly one to the leader.
    assert calls == math.comb(6, 1) + 4 * math.comb(6, 2)


def test_injecting_locked_cards_yields_exactly_the_same_arrangements_as_filtering():
    """The fast path must be an optimisation, not a different search.

    `_iter_member_sets` builds locked runs by choosing the free slots from the
    unlocked pool and splicing the locks in, instead of walking every C(N,5)
    and rejecting most. Cheaper only counts if the RESULT is identical, so this
    compares the full arrangement set against the filter path rather than
    comparing counts."""
    from hbuilder.solver import exact as exact_mod

    cards = [make_card(f"card_{i:02d}") for i in range(9)]
    pool = canonical_pool(cards)
    cfg = make_scoring_config()
    locked = frozenset({"card_01", "card_04"})

    def constraint(members, leader):
        return locked.issubset({c.id for c in members} | {leader.id})

    def set_constraint(members):
        return len(locked - {c.id for c in members}) <= 1

    def as_pairs(results):
        return sorted((r.leader_id, r.member_ids, r.score) for r in results)

    filtered = as_pairs(
        exact_mod.iter_candidates(
            pool, cfg, constraint=constraint, set_constraint=set_constraint
        )
    )
    injected = as_pairs(
        exact_mod.iter_candidates(
            pool,
            cfg,
            constraint=constraint,
            set_constraint=set_constraint,
            locked_ids=locked,
        )
    )

    assert injected == filtered, (
        "injecting the locked cards changed the search, not just its cost"
    )
    assert filtered, "fixture precondition: the locked run must yield something"


def test_injected_enumeration_never_yields_the_same_member_set_twice():
    """Family A (every lock a member) and family B (one lock deferred to the
    outfit slot) must not overlap, or a set would be scored — and ranked —
    twice."""
    from hbuilder.solver.exact import _iter_member_sets

    pool = canonical_pool([make_card(f"card_{i:02d}") for i in range(9)])
    sets = list(_iter_member_sets(pool, frozenset({"card_01", "card_04"})))

    as_ids = [tuple(c.id for c in s) for s in sets]
    assert len(as_ids) == len(set(as_ids)), "a member set was produced twice"
    assert all(list(ids) == sorted(ids) for ids in as_ids), "not ascending by id"
