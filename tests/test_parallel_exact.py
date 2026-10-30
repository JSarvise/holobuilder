"""The parallel exact search must return `solve_exact`'s answer EXACTLY —
same scores, same arrangements, same order, same tie-breaks.

"Identical best score at every worker count" is not enough. A best *score* is
the weak half of the claim: two
different arrangements can tie, and a merge that loses the serial tie-break
would still print the same number while recommending a different team. So this
module asserts the full Top-N record — score, leader id and member ids, in
order — against the serial solver, at several worker counts.

**Why the merge is exact, so a failure here means a real defect and not a
tolerance issue.** Each worker returns its own `heapq.nlargest(top_n)` under
`exact._heap_key`; the parent reruns `nlargest` over the union. Any arrangement
absent from its own shard's Top-N has `top_n` better arrangements in that shard
alone, so it cannot be in the global Top-N — the union is sufficient. And
`_heap_key` is injective over arrangements (two distinct arrangements cannot
share both leader id and member ids), so it is a total order and reproduces the
serial ordering rather than merely agreeing on the set.

These tests spawn real processes. That is the point: `multiprocessing` on
Windows uses `spawn`, so they exercise the pickle round trip of `Card` and
`ScoringConfig` that `Unknown.__reduce__` exists to survive — the defect that
would otherwise make workers disagree with the parent for a reason no diff of
the search code could show.
"""

from __future__ import annotations

import itertools

import pytest

from hbuilder.domain.card import EffectTarget, GateCondition, Stats
from hbuilder.solver.exact import canonical_pool, solve_exact
from hbuilder.solver.parallel import (
    _index_pairs,
    _sets_with_prefix,
    default_workers,
    solve_exact_parallel,
)

from conftest import make_card, make_scoring_config, make_skill


def record(teams) -> list[tuple[float, str, tuple[str, ...]]]:
    """The whole user-visible answer, not just its first score."""
    return [(team.score, team.leader_id, team.member_ids) for team in teams]


POOL_SIZE = 12

_TYPES = ("Cute", "Cool", "Passion")
_GENERATIONS = ("1期生", "2期生", "3期生", "holoX")


@pytest.fixture(scope="module")
def search_pool():
    """A hermetic `POOL_SIZE`-card pool, built here rather than read from disk.

    The committed 6-card fixture gives `C(6,5) = 6` member sets — one per
    `(i, j)` shard, too few for a merge bug to have anywhere to hide. And the
    obvious alternative, `data/cards.json`, is the hand-edited
    authoritative catalogue: a test keyed to it would change meaning every
    time a card is edited, which is exactly the coupling
    `test_fixture_isolation.py` exists to forbid. So the pool is constructed.

    Constructed **deliberately awkwardly**, because the shapes where a
    partition bug would hide are the ones that make the per-set prefix and the
    per-leader tail differ:

    - three types and four generations across twelve cards, so a
      `deck_card_attribute` gate resolves differently per member set;
    - count-limited PASSIVE targets (`count=2`) on more cards than any set can
      hold, so `_order_varying_indices` binds and the 120-ordering slot search
      actually runs;
    - a costume skill on every card, so every one of the twelve is a live
      leader choice rather than a filler;
    - stats that differ per card, so two arrangements do not tie by accident
      and the tie-break under test is exercised on distinct values.
    """
    cards = []
    for index in range(POOL_SIZE):
        card_id = f"pool_{index:02d}"
        cards.append(
            make_card(
                card_id,
                type_=_TYPES[index % len(_TYPES)],
                generations=frozenset({_GENERATIONS[index % len(_GENERATIONS)]}),
                stats=Stats(
                    performance=900 + index * 31,
                    technique=850 + index * 47,
                    sense=800 + index * 53,
                ),
                active=make_skill(
                    "ACTIVE",
                    family="conditional_score_up",
                    effect_type="score",
                    duration=8.0,
                    interval=20.0,
                    probability_tier="Medium",
                    magnitude=40.0 + index,
                    condition=(
                        GateCondition(kind="type", target=_TYPES[index % 3], count=2)
                        if index % 2 == 0
                        else None
                    ),
                ),
                passive=make_skill(
                    "PASSIVE",
                    family="stat_up",
                    effect_type="all_stats" if index % 3 == 0 else "performance",
                    magnitude=12.0 + index,
                    target=EffectTarget(kind="type", value=_TYPES[index % 3], count=2),
                ),
                costume_skill=make_skill(
                    "COSTUME",
                    family="stat_up",
                    effect_type=("performance", "technique", "sense")[index % 3],
                    magnitude=60.0 + index * 5,
                ),
            )
        )
    return canonical_pool(cards)


# ---------------------------------------------------------------------------
# The partition itself, testable without spawning anything
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", [6, 7, 9, 12, 20])
def test_the_index_pairs_partition_the_combination_space_exactly(size):
    """Every 5-card combination belongs to exactly one `(i, j)` shard — no set
    produced twice, none missed. Asserted as a multiset against
    `itertools.combinations`, so a duplicate cannot hide behind a set union."""
    pool = tuple(range(size))
    sharded = [
        combo
        for i, j in _index_pairs(size)
        for combo in itertools.combinations(range(size), 5)
        if combo[0] == i and combo[1] == j
    ]
    expected = list(itertools.combinations(range(size), 5))

    assert sorted(sharded) == sorted(expected)
    assert len(sharded) == len(expected)
    del pool


def test_a_shards_sets_are_ascending_and_carry_the_pairs_two_cards(search_pool):
    """`_heap_key`'s tie-break rests on `member_ids` being ascending, and
    `_sets_with_prefix` gets that from the pool being canonical rather than by
    sorting. If it ever stopped holding, the parallel path would tie-break
    differently from the serial one and only on ties."""
    produced = 0
    for i, j in _index_pairs(len(search_pool)):
        for members in _sets_with_prefix(search_pool, i, j):
            ids = [card.id for card in members]
            assert ids == sorted(ids)
            assert members[0] is search_pool[i]
            assert members[1] is search_pool[j]
            produced += 1
    assert produced == len(list(itertools.combinations(search_pool, 5)))


def test_a_pool_too_small_to_shard_produces_no_pairs():
    """A pair is only dispatched when at least three cards sit above `j`.
    Below five cards no combination exists at all and the generator is empty,
    which is what stops an under-sized pool from being dispatched as work."""
    # Five cards make exactly one set — the whole pool — so `(0, 1)` is a
    # real shard, not an empty one. `solve_exact_parallel` still hands a
    # pool this size to `solve_exact` rather than spawning for one set.
    assert list(_index_pairs(5)) == [(0, 1)]
    assert list(_index_pairs(4)) == []
    assert list(_index_pairs(0)) == []


# ---------------------------------------------------------------------------
# The claim itself: bit-identical to serial, with real processes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("workers", [2, 3])
def test_the_parallel_answer_is_bit_identical_to_the_serial_one(search_pool, workers):
    cfg = make_scoring_config()

    serial = record(solve_exact(search_pool, cfg, top_n=5))
    parallel = record(solve_exact_parallel(search_pool, cfg, top_n=5, workers=workers))

    assert parallel == serial
    # Exact float equality, spelled out: the loop above is `==` on tuples, and
    # a reader could reasonably wonder whether something rounded on the way
    # through a pickle.
    assert [team[0] for team in parallel] == [team[0] for team in serial]


def test_two_worker_counts_agree_with_each_other_not_just_with_serial(search_pool):
    """Shards complete in a nondeterministic order under `imap_unordered`. If
    the merge were order-sensitive, the answer would move between worker counts
    while each run still looked self-consistent."""
    cfg = make_scoring_config()

    assert record(solve_exact_parallel(search_pool, cfg, top_n=5, workers=2)) == record(
        solve_exact_parallel(search_pool, cfg, top_n=5, workers=4)
    )


def test_one_worker_takes_the_serial_path_and_returns_the_serial_answer(search_pool):
    cfg = make_scoring_config()

    assert record(solve_exact_parallel(search_pool, cfg, top_n=5, workers=1)) == record(
        solve_exact(search_pool, cfg, top_n=5)
    )


def test_a_pool_smaller_than_a_team_returns_empty_rather_than_raising(search_pool):
    """`solve_exact` answers an under-sized pool with an empty tuple and never
    raises. The parallel path has its own early return for that case, so it
    needs its own assertion."""
    cfg = make_scoring_config()

    assert solve_exact_parallel(search_pool[:4], cfg, top_n=5, workers=2) == ()


def test_zero_or_negative_workers_is_refused_by_name():
    cfg = make_scoring_config()

    with pytest.raises(ValueError, match="workers must be >= 1"):
        solve_exact_parallel((), cfg, workers=0)


def test_default_workers_is_at_least_one():
    """`os.cpu_count()` may return `None`; a `Pool(processes=None)` would then
    mean "however many the platform picks", which is not the same statement as
    the one `default_workers`'s docstring makes."""
    assert default_workers() >= 1


def test_the_progress_callback_counts_shards_and_reaches_the_total(search_pool):
    """It is documented as a SHARD counter, not an arrangement counter. A
    caller wiring it to a progress bar labelled "arrangements" would be off by
    four orders of magnitude, so what it counts is pinned."""
    cfg = make_scoring_config()
    seen: list[int] = []

    solve_exact_parallel(search_pool, cfg, top_n=3, workers=2, progress=seen.append)

    assert seen == list(range(1, len(list(_index_pairs(len(search_pool)))) + 1))
