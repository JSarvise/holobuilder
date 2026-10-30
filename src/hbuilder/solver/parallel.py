"""The exact search across processes — about **8.5x on 16 logical cores and
bit-identical at every worker count**.

It exists for one reason: **the heuristic needs a reference truth to be
validated against.** A single-process exact solve at N=32 takes minutes and at
N=91 more than a day, so beam-vs-exact comparisons beyond small pools need the
exact search spread over every core (a few hours at N=91).

## Why it is a partition and not a second solver

Every worker calls `exact.iter_candidates` with its own share of the member-set
enumeration handed in through `member_sets`. R1, the leader narrowings,
`score_card_set` and the ascending-`member_ids` contract are the SAME code the
serial path runs, so there is no second enumeration to keep in step. What this
module adds is exactly three things: how the space is cut, how a worker's
partial Top-N comes home, and how the shards merge.

**The merge is exact, not approximate.** Each worker returns its own
`heapq.nlargest(top_n, ...)` under `exact._heap_key`; the parent runs the same
`nlargest` over the union. The global Top-N is a subset of the union of the
per-shard Top-Ns (any arrangement outside its own shard's Top-N has `top_n`
better arrangements in that shard alone), and `_heap_key` is injective over
arrangements — two distinct arrangements cannot share both leader id and member
ids — so it is a total order and the merge reproduces the serial ordering
including its tie-breaks. That is an argument, and it is also asserted:
`tests/test_parallel_exact.py` demands bit-identical equality against
`solve_exact` at several worker counts.

## The partition, and the trap

**Partitioning by FIRST member index costs 2.2x** — 3.83x against 8.51x for
identical work — because index 0 owns
`C(N-1,4)` sets and index `N-5` owns exactly one, a 17,550:1 imbalance at N=91.
Static interleaving does not rescue it. So the unit of work here is an
`(i, j)` index PAIR — `C(N,2)` tasks, the largest a small fraction of the
total — dispatched with `imap_unordered(..., chunksize=1)` so the pool
schedules them dynamically. **Do not "simplify" this back to a first-index
split.**

Worker startup is deliberately not optimised: **about 0.11 s for 8 workers,
0.12 s for 16**, under 2 % of even a 6.77 s run and unmeasurable at full-roster
scale, so `shared_memory`, persistent pools and pickle-avoidance tricks buy
nothing. The pool is ~91 frozen dataclasses; `spawn` re-imports and re-pickles
them per worker and that is fine.

## What it deliberately does NOT do

**Only the unconstrained enumeration is parallelised, and the signature says so
by not accepting the alternative.** `constraint` and `set_constraint` are
arbitrary callables — closures over selector state — and Windows `spawn` cannot
ship a closure to a worker at all. Taking the parameters and then refusing them
at run time would be worse than not taking them. Locked runs stay on
`solve_exact`, where `_iter_member_sets` already makes them cheap by injecting
the locks instead of filtering them; the shape that needs parallelism is the
full sweep, which is precisely the shape with no locks. A parallel locked run
would need a picklable
lock *specification* (`frozenset[str]`, which ships fine) reaching
`iter_candidates`' existing `locked_ids`, not a pickled callable.
"""

from __future__ import annotations

import heapq
import itertools
import multiprocessing
import os
from typing import Iterator

from hbuilder.domain.card import Card
from hbuilder.domain.scoring_config import ScoringConfig
from hbuilder.domain.team import MEMBER_COUNT, Team
from hbuilder.solver.exact import (
    DEFAULT_TOP_N,
    RankedTeam,
    _heap_key,
    canonical_pool,
    iter_candidates,
    solve_exact,
)

# A shard's result travels as `(score, leader_id, member_ids)` rather than as a
# `RankedTeam`, because `RankedTeam.team` holds a `Team` holding six `Card`s —
# pickling `top_n` of those per worker ships the pool back over the wire once
# per result for a value the parent can rebuild from ids it already has.
ShardTeam = tuple[float, str, tuple[str, ...]]


def default_workers() -> int:
    """Every logical core. On a 16-thread desktop CPU, 8 physical cores give
    6.26x and all 16 logical **8.51x** — SMT adds a further 36 % on this
    branch-heavy workload. `os.cpu_count()` can return `None`.

    `HBUILDER_WORKERS` overrides it: the desktop app's Settings, for a PC that must stay
    usable while it searches."""
    raw = os.environ.get("HBUILDER_WORKERS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return os.cpu_count() or 1


def _index_pairs(size: int) -> Iterator[tuple[int, int]]:
    """`C(size, 2)` tasks: the indices of a member set's two lowest cards.

    Every 5-card combination of an ascending pool has exactly one such pair, so
    the pairs partition the space — no set is produced twice and none is
    missed. Pairs with fewer than three cards above `j` own no set at all and
    are skipped rather than dispatched as empty tasks.
    """
    for i in range(size):
        for j in range(i + 1, size):
            if size - j - 1 >= MEMBER_COUNT - 2:
                yield (i, j)


def _sets_with_prefix(
    pool: tuple[Card, ...], i: int, j: int
) -> Iterator[tuple[Card, ...]]:
    """Every member set whose two lowest-indexed cards are `pool[i]`, `pool[j]`.

    `pool` is canonical (ascending by `card.id`), so the yielded tuple is
    ascending by construction — the same contract `itertools.combinations` over
    the whole pool gives `_iter_member_sets`, and what keeps `member_ids`
    ascending and `_heap_key`'s tie-break well defined.
    """
    head = (pool[i], pool[j])
    for rest in itertools.combinations(pool[j + 1 :], MEMBER_COUNT - 2):
        yield head + rest


# Per-worker state, set once by the pool initializer. Module-level because
# `spawn` gives each worker a fresh interpreter and `Pool.imap_unordered` can
# only send the task, not the context: paying the pool + config pickle per
# (i, j) task instead of per worker would be C(91,2) = 4,095 round trips of it.
_POOL: tuple[Card, ...] = ()
_CFG: ScoringConfig | None = None
_TOP_N: int = DEFAULT_TOP_N
_INDEX_OF: dict[str, int] = {}
_LEADER_IDS: frozenset[str] | None = None


def _init_worker(
    pool: tuple[Card, ...],
    cfg: ScoringConfig,
    top_n: int,
    leader_ids: frozenset[str] | None,
) -> None:
    global _POOL, _CFG, _TOP_N, _INDEX_OF, _LEADER_IDS
    _POOL = pool
    _CFG = cfg
    _TOP_N = top_n
    _INDEX_OF = {card.id: position for position, card in enumerate(pool)}
    _LEADER_IDS = leader_ids


def _solve_shard(task: tuple[int, int]) -> list[ShardTeam]:
    """One `(i, j)` shard's own Top-N, keyed exactly as the serial solver keys
    its global one."""
    i, j = task
    assert _CFG is not None  # set by _init_worker before any task is dispatched
    best = heapq.nlargest(
        _TOP_N,
        iter_candidates(
            _POOL,
            _CFG,
            member_sets=_sets_with_prefix(_POOL, i, j),
            leader_ids=_LEADER_IDS,
        ),
        key=lambda candidate: _heap_key(candidate, _INDEX_OF),
    )
    return [(team.score, team.leader_id, team.member_ids) for team in best]


def solve_exact_parallel(
    cards,
    cfg: ScoringConfig,
    *,
    top_n: int = DEFAULT_TOP_N,
    workers: int | None = None,
    progress=None,
    leader_ids: frozenset[str] | None = None,
) -> tuple[RankedTeam, ...]:
    """`solve_exact`'s answer, computed across processes. Bit-identical to it.

    `workers=1` runs `solve_exact` directly rather than spinning up a pool of
    one — same answer, and it keeps a single-worker run honest as a baseline in
    a speedup table instead of measuring the pool machinery against itself.

    `progress`, when given, is called with the number of `(i, j)` shards
    completed so far. It is a shard counter, not an arrangement counter: the
    serial path's per-arrangement cadence cannot cross a process boundary
    without either shipping a counter per 250,000 arrangements or lying about
    the number.

    Takes no `constraint`, `set_constraint`, `locked_ids` or `leader_id` — see
    this module's header for why that is a signature decision rather than a
    run-time refusal.
    """
    pool = canonical_pool(cards)
    if workers is None:
        workers = default_workers()
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")
    if workers == 1 or len(pool) <= MEMBER_COUNT:
        return solve_exact(pool, cfg, top_n=top_n, leader_ids=leader_ids)

    index_of = {card.id: position for position, card in enumerate(pool)}
    card_by_id = {card.id: card for card in pool}
    tasks = list(_index_pairs(len(pool)))

    merged: list[ShardTeam] = []
    completed = 0
    context = multiprocessing.get_context("spawn")
    with context.Pool(
        processes=workers,
        initializer=_init_worker,
        initargs=(pool, cfg, top_n, leader_ids),
    ) as worker_pool:
        # chunksize=1 is the load-balancing half of the (i, j) partition and is
        # not a default worth "tuning": the point of C(N,2) small tasks is that
        # the pool can hand the next one to whichever worker is free.
        for shard in worker_pool.imap_unordered(_solve_shard, tasks, chunksize=1):
            merged.extend(shard)
            completed += 1
            if progress is not None:
                progress(completed)

    best = heapq.nlargest(
        top_n,
        merged,
        key=lambda item: (
            item[0],
            -index_of[item[1]],
            tuple(-index_of[member_id] for member_id in item[2]),
        ),
    )
    return tuple(
        RankedTeam(
            score=score,
            leader_id=leader_id,
            member_ids=member_ids,
            team=Team(
                leader=card_by_id[leader_id],
                members=tuple(card_by_id[member_id] for member_id in member_ids),
            ),
        )
        for score, leader_id, member_ids in best
    )
