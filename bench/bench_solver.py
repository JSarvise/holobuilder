"""Measures real `solve_exact` scoring throughput over
reproducible synthetic pools, and writes the provenance-carrying
`bench/throughput.json` that `hbuilder.solver.estimate` reads.

Standalone script — lives outside `src/`, is not part of the
installed package, and is never imported by any production module. A
`grep -rc 'bench_solver' src/` returning 0 pins this: benchmark code
must not be reachable from the CLI's own import graph.

The cost wall is the scoring function, not the enumeration, so profile
before optimising. This script is that profiling step, made real and
reproducible rather than guessed:

- Synthetic pools are clones of the six real cards in
  `data/cards.json`, with perturbed stats and shuffled `type`/
  `generations`, because the benchmark needs realistic gate-hit
  *distributions* -- not real card data -- and `owned.json` holds only
  six cards, so measured throughput at a meaningful N is otherwise
  unobtainable. Every skill slot's family, effect_type, magnitude and
  condition structure is left untouched, which is what produces
  realistic gate hits rather than synthetic noise.
- Every measurement is written with provenance -- the scoring
  config hash and a UTC timestamp -- so `--estimate` can warn when
  projecting from a measurement taken before the scoring model changed,
  instead of reporting a stale per-eval cost with full confidence.
- The producer/consumer contract is declared once: `write_throughput`
  writes exactly the keys named by `estimate.THROUGHPUT_REQUIRED_KEYS`,
  imported from `hbuilder.solver.estimate` rather than restated here, so
  a key renamed on the consumer side breaks this script at import, never
  silently at the next projection.

Run directly: `PYTHONPATH=src python bench/bench_solver.py`.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from hbuilder import cli
from hbuilder.cli import _scoring_config_document
from hbuilder.domain.card import Card, Stats
from hbuilder.domain.loader import load_cards, load_owned
from hbuilder.domain.schema import CARD_TYPES, GENERATIONS, SCHEMA_VERSION
from hbuilder.domain.scoring_config import ScoringConfig, load_scoring_config
from hbuilder.domain.team import TEAM_SIZE
from hbuilder.solver.estimate import (
    THROUGHPUT_PATH,
    THROUGHPUT_REQUIRED_KEYS,
    Throughput,
    config_hash,
    derive_threshold_pool_size,
    project_arrangements,
)
from hbuilder.solver.exact import DEFAULT_TOP_N, solve_exact
from hbuilder.solver.selectors import (
    CandidatePool,
    constraint_from,
    set_constraint_from,
)

# Every synthetic pool is built from
# `random.Random(SYNTHETIC_SEED)`, never the global `random` module and
# never unseeded, so two runs at the same pool size build the identical
# pool byte-for-byte -- a throughput change then means a code change,
# never a different pool. Not tied to any calendar date on purpose; it is
# simply a fixed module constant, the same idiom `CONFIRM_THRESHOLD_SECONDS`
# uses in `solver/estimate.py`.
SYNTHETIC_SEED: int = 20260727

# Measured points spanning a range where the per-eval cost is stable but a
# run still finishes in seconds. `--pool-size` may be repeated to override
# this.
DEFAULT_POOL_SIZES: tuple[int, ...] = (12, 16, 20)

# The real collection, recomputed here via `project_arrangements` rather than
# restated as literals, so the printed projection cannot drift from the formula
# `--estimate` itself projects from.
#
# The roster size is READ from `owned.json` rather than typed: the collection
# grows, and a literal goes stale and contradicts the pool size printed next
# to it.
#
# The fallback only applies with no `owned.json` at all, where there is no
# roster to read and the projection is illustrative anyway.
_FALLBACK_ROSTER_POOL_SIZE = 91
_REQUIRE_ONE_LOCKED_COUNT = 1
# The shape of a real mode-B solve: the leader pinned, two members required.
_MODE_B_LOCKED_COUNT = 3


class SyntheticPoolError(ValueError):
    """Raised by `build_synthetic_pool` when asked for fewer than
    `TEAM_SIZE` (6) cards. A pool that cannot form a single team is not a
    measurable throughput point -- reporting a per-eval cost for it would
    be meaningless."""


class MeasurementError(ValueError):
    """Raised when an elapsed measurement is unusable. Dividing a real
    arrangement count by a zero-second interval would fabricate an
    infinite throughput; a per-eval cost of zero or infinity is a broken
    measurement, not a fast one."""


def build_synthetic_pool(real_cards: tuple[Card, ...], size: int) -> tuple[Card, ...]:
    """A reproducible measurement pool of `size` cards.

    When `real_cards` holds at least `size` distinct holomem, the pool is a
    seeded sample of REAL cards, one per holomem -- the closest analogue to the
    pool the projection is used on. Only when it does not (the six-card test
    fixture) does it fall back to cloning:

    build `size` synthetic `Card`s by cloning `real_cards`
    repeatedly, each clone given a unique id (`f"{base.id}__s{n:03d}"`), a
    seeded stat perturbation, and a seeded reassignment of `type` and
    `generations`. Every skill slot's family, effect_type, magnitude and
    condition structure is copied unchanged -- the benchmark needs
    realistic gate-hit *distributions*, not real card data, and the gate
    structure is exactly what produces them.

    Raises `SyntheticPoolError` for `size < TEAM_SIZE` -- a pool that
    cannot form a team is not a measurable point.

    Seeded from the module constant `SYNTHETIC_SEED` via a fresh
    `random.Random` instance created inside this call (never the global
    `random` module), so two separate calls at the same `size` -- in the
    same process or two different ones -- build an identical pool.
    """
    if size < TEAM_SIZE:
        raise SyntheticPoolError(
            f"a synthetic pool must hold at least {TEAM_SIZE} cards (one "
            f"set, six arrangements) to be a measurable throughput point, "
            f"got size={size}"
        )
    if not real_cards:
        raise SyntheticPoolError(
            "real_cards must be non-empty -- there is nothing to clone from"
        )

    rng = random.Random(SYNTHETIC_SEED)

    # Prefer REAL cards whenever there are enough distinct holomem to fill the
    # pool without repeating one. A clone of six base cards under-states the
    # cost -- at pool_size=12 a cloned pool priced the per-set prefix at 73us
    # against 148us measured directly on a real 91-card pool, a 2x error in
    # the term that dominates every `--leader` run.
    # Real cards carry the real spread of skill-slot counts and gate shapes,
    # which is what the prefix actually costs.
    by_holomem: dict[str, list[Card]] = {}
    for card in sorted(real_cards, key=lambda c: c.id):
        by_holomem.setdefault(card.member, []).append(card)
    if size <= len(by_holomem):
        # One card per holomem, chosen by the seeded rng rather than by lowest
        # id: taking the first id per holomem systematically selects the same
        # (typically simplest) skin every time, which biases the very cost
        # being measured.
        chosen = [rng.choice(cards) for _, cards in sorted(by_holomem.items())]
        sampled = rng.sample(chosen, size)
        return tuple(sorted(sampled, key=lambda c: c.id))

    card_types = sorted(CARD_TYPES)

    clones: list[Card] = []
    for index in range(size):
        base = real_cards[index % len(real_cards)]
        new_id = f"{base.id}__s{index:03d}"

        multiplier = rng.uniform(0.85, 1.15)
        new_stats = Stats(
            performance=max(1, round(base.stats.performance * multiplier)),
            technique=max(1, round(base.stats.technique * multiplier)),
            sense=max(1, round(base.stats.sense * multiplier)),
        )

        new_type = rng.choice(card_types)
        generation_count = rng.randint(1, min(3, len(GENERATIONS)))
        new_generations = frozenset(rng.sample(GENERATIONS, generation_count))

        clones.append(
            dataclasses.replace(
                base,
                id=new_id,
                # Each clone is its OWN holomem. Inheriting the base card's
                # `member` would leave a 20-card pool with only six distinct
                # holomem, and R1 (five distinct holomem among the members)
                # would reject ~93% of the 5-sets before scoring -- the
                # benchmark would then divide time spent walking rejected
                # combinations by the full arrangement count. A real 91-card
                # pool holds 54 holomem and ~90% of its 5-sets survive R1, so
                # a distinct-holomem synthetic pool is the closer analogue.
                member=f"{base.member}__s{index:03d}",
                stats=new_stats,
                type=new_type,
                generations=new_generations,
            )
        )

    return tuple(clones)


def validate_elapsed_seconds(elapsed_seconds: float, pool_size: int) -> None:
    """`measure`'s error path, factored into its own function so it can be
    exercised directly with a synthetic zero interval, without needing an
    implausibly-fast real `solve_exact` call to trigger it."""
    if elapsed_seconds == 0.0:
        raise MeasurementError(
            f"measured 0.0s elapsed for pool_size={pool_size} -- the "
            f"timer's resolution was insufficient to measure this pool; "
            f"pass a larger --pool-size. A per-eval cost of zero or "
            f"infinity is a broken measurement, not a fast one."
        )


def measure(
    pool: tuple[Card, ...], cfg: ScoringConfig, *, top_n: int = DEFAULT_TOP_N
) -> tuple[int, int, float]:
    """Time one `solve_exact(pool, cfg, top_n=top_n)` call with
    `time.perf_counter()` and return `(sets, arrangements_evaluated,
    elapsed_seconds)`.

    Both counts are derived from `pool`'s size via the same formula
    `--estimate` itself projects from (`project_arrangements`), never counted
    inside the hot loop -- so this measurement never measures its own
    instrumentation. That the counts are exact for these pools is not an
    assumption: `build_synthetic_pool` gives every clone its own holomem, so
    R1 rejects nothing and the closed form is reached exactly.
    """
    set_count, arrangement_count = project_arrangements(len(pool), 0, False)

    start = time.perf_counter()
    solve_exact(pool, cfg, top_n=top_n)
    elapsed_seconds = time.perf_counter() - start

    validate_elapsed_seconds(elapsed_seconds, len(pool))
    return set_count, int(arrangement_count), elapsed_seconds


def measure_leader_pinned(
    pool: tuple[Card, ...], cfg: ScoringConfig, *, top_n: int = DEFAULT_TOP_N
) -> tuple[int, int, float]:
    """The same measurement with the leader pinned to one card -- the `--leader`
    regime, where there is one arrangement per member set and the set-invariant
    prefix of `score_card_set` is therefore the whole cost.

    Driven through the shipped `constraint_from`/`set_constraint_from`
    predicates and `locked_ids`, exactly as `cli.py` drives a `--leader` run,
    so this measures the real path rather than a benchmark-only imitation. The
    pinned card is `pool[0]` after canonicalisation-independent ordering; which
    card it is does not matter, only that there is exactly one.
    """
    pinned = pool[0]
    candidate_pool = CandidatePool(
        cards=pool,
        locked_ids=frozenset({pinned.id}),
        leader_id=pinned.id,
        included_unowned_ids=(),
    )
    set_count, arrangement_count = project_arrangements(len(pool), 1, True)

    start = time.perf_counter()
    solve_exact(
        pool,
        cfg,
        top_n=top_n,
        constraint=constraint_from(candidate_pool),
        set_constraint=set_constraint_from(candidate_pool),
        locked_ids=candidate_pool.locked_ids,
    )
    elapsed_seconds = time.perf_counter() - start

    validate_elapsed_seconds(elapsed_seconds, len(pool))
    return set_count, int(arrangement_count), elapsed_seconds


def solve_cost_model(
    unconstrained: tuple[int, int, float], leader_pinned: tuple[int, int, float]
) -> tuple[float, float]:
    """Solve the two-term cost model

        elapsed = sets * seconds_per_set + arrangements * seconds_per_eval

    for `(seconds_per_set, seconds_per_eval)` from ONE unconstrained
    measurement and ONE leader-pinned measurement of the same pool, each given
    as `(sets, arrangements, elapsed_seconds)`.

    Two terms because `score_card_set(members, leaders, cfg)` has two costs: a
    set-invariant prefix (gate resolution over the five members, every member
    EV, `base_stat_sum`, `team_rate_up`) paid once per member SET, and one
    `combine()` per candidate leader. One blended per-arrangement rate cannot
    describe both regimes -- the prefix is ~8x one leader's marginal cost, so
    an unconstrained run (N leaders per set, prefix amortised) and a `--leader`
    run (one leader per set, prefix paid in full) disagree by that factor.
    Using the first to predict the second can under-estimate a 178 s run as
    0.04 s.

    Measuring both regimes directly, rather than extrapolating a line fitted
    across pool sizes, is deliberate: the leader-pinned regime is the one the
    projection is most sensitive to, and it is also the common one in practice.
    Both measured points are then reproduced exactly by the resulting model,
    and the whole of a pinned run's overhead -- the lock splice, the per-leader
    `constraint` calls -- lands in the term that prices pinned runs.

    Raises `MeasurementError` if the two runs cannot separate the terms (equal
    arrangement counts) or if the implied per-eval cost is not positive: a
    per-eval cost of zero or negative is a broken measurement, not a fast one.
    """
    sets_u, arr_u, elapsed_u = unconstrained
    sets_p, arr_p, elapsed_p = leader_pinned
    if sets_u <= 0 or sets_p <= 0:
        raise MeasurementError("a measurement enumerated no member sets at all")
    if arr_u == arr_p:
        raise MeasurementError(
            "the unconstrained and leader-pinned runs evaluated the same "
            f"number of arrangements ({arr_u}) -- they cannot separate the "
            "per-set prefix from the per-leader cost"
        )

    # elapsed_u/sets_u = a + (arr_u/sets_u) * b ; same for the pinned run.
    per_set_u = elapsed_u / sets_u
    per_set_p = elapsed_p / sets_p
    leaders_u = arr_u / sets_u
    leaders_p = arr_p / sets_p

    seconds_per_eval = (per_set_u - per_set_p) / (leaders_u - leaders_p)
    seconds_per_set = per_set_p - seconds_per_eval * leaders_p

    if seconds_per_eval <= 0.0:
        raise MeasurementError(
            f"implied seconds_per_eval={seconds_per_eval!r} is not positive -- "
            f"a per-eval cost of zero or negative is a broken measurement, not "
            f"a fast one (unconstrained {elapsed_u:.4f}s over {arr_u} "
            f"arrangements, leader-pinned {elapsed_p:.4f}s over {arr_p})"
        )
    # A negative implied prefix is noise around a genuinely small one; clamp
    # rather than let the projection subtract time.
    return (max(seconds_per_set, 0.0), seconds_per_eval)


def write_throughput(
    seconds_per_eval: float,
    seconds_per_set: float,
    cfg_document: dict,
    path: Path,
) -> None:
    """Write a JSON object carrying exactly the keys named by
    `estimate.THROUGHPUT_REQUIRED_KEYS`. `seconds_per_eval` is written at
    full float precision -- `json.dumps` encodes a float byte-identically
    to its `repr()`, the same round-trip-exact property the `--json`
    payload relies on -- never rounded here; rounding belongs only where a
    duration is rendered for a human to read. Serialized with
    `sort_keys=True` and a trailing newline so the committed file diffs
    cleanly.
    """
    doc = {
        "seconds_per_eval": seconds_per_eval,
        "seconds_per_set": seconds_per_set,
        "config_hash": config_hash(cfg_document),
        "schema_version": SCHEMA_VERSION,
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
    assert set(doc) == set(THROUGHPUT_REQUIRED_KEYS), (
        "write_throughput's output keys must exactly match "
        "estimate.THROUGHPUT_REQUIRED_KEYS -- a mismatch here is exactly "
        "the producer/consumer drift D-25a guards against"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench_solver",
        description=(
            "TEST-04: measure real solve_exact scoring throughput over "
            "reproducible synthetic pools, and write the provenance-"
            "carrying throughput file --estimate reads."
        ),
    )
    parser.add_argument(
        "--cards",
        default="data/cards.json",
        help="Path to cards.json (source of the six real cards to clone)",
    )
    parser.add_argument(
        "--owned",
        default="data/owned.json",
        help=(
            "Path to owned.json. When it exists, measurement pools are sampled "
            "from the OWNED collection -- the pool --estimate is actually used "
            "on. Absent, the sample falls back to --cards."
        ),
    )
    parser.add_argument(
        "--board",
        default=cli.DEFAULT_BOARD_PATH,
        metavar="PATH",
        help=(
            "Path to the board file to measure WITH (default %(default)s, the "
            "same default `hbuilder solve` uses). An absent file is not an "
            "error -- the board then scores as zero, exactly as it does in a "
            "solve. Pass --no-board to measure the boardless model instead."
        ),
    )
    parser.add_argument(
        "--no-board",
        action="store_true",
        help=(
            "Measure with the Holomem Board scored as zero. The measured rates "
            "then describe a `solve --no-board` run and nothing else -- the "
            "board is part of the scoring model, so it is part of config_hash "
            "and a projection will warn if the two are mixed."
        ),
    )
    parser.add_argument(
        "--pool-size",
        dest="pool_sizes",
        type=int,
        action="append",
        default=None,
        metavar="N",
        help=f"Synthetic pool size to measure (repeatable; default {DEFAULT_POOL_SIZES})",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N,
        help=f"Top-N passed to solve_exact for each measurement (default {DEFAULT_TOP_N})",
    )
    parser.add_argument(
        "--out",
        default=str(THROUGHPUT_PATH),
        metavar="PATH",
        help=f"Where to write the throughput file (default {THROUGHPUT_PATH})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)

    pool_sizes = (
        tuple(sorted(set(args.pool_sizes))) if args.pool_sizes else DEFAULT_POOL_SIZES
    )

    real_cards = load_cards(Path(args.cards))
    owned_path = Path(args.owned)
    if owned_path.exists():
        real_cards = load_owned(owned_path, real_cards)
        print(f"sampling measurement pools from {owned_path} ({len(real_cards)} cards)")
    cfg = load_scoring_config()

    # The board is measured, not assumed absent: it makes scoring several
    # times dearer, so rates measured without it would describe a model
    # nobody runs. Driven through the CLI's own `load_board_basis` so the benchmark and the
    # solve cannot disagree about which board is loaded or what an absent file
    # means.
    board_basis = cli.load_board_basis(None if args.no_board else args.board)
    for line in board_basis.lines():
        print(line)
    if board_basis.model is not None:
        cfg = dataclasses.replace(cfg, board=board_basis.model)

    cfg_document = _scoring_config_document(cfg)

    print(
        f"{'pool_size':>9} {'regime':>14} {'sets':>12} {'arrangements':>14} "
        f"{'elapsed_s':>10} {'per_set_us':>11} {'per_eval_us':>12}"
    )
    points: dict[int, tuple[tuple[int, int, float], tuple[int, int, float]]] = {}
    for size in pool_sizes:
        pool = build_synthetic_pool(real_cards, size)
        unconstrained = measure(pool, cfg, top_n=args.top_n)
        pinned = measure_leader_pinned(pool, cfg, top_n=args.top_n)
        points[size] = (unconstrained, pinned)
        for label, (set_count, arrangement_count, elapsed_seconds) in (
            ("unconstrained", unconstrained),
            ("leader-pinned", pinned),
        ):
            print(
                f"{size:>9} {label:>14} {set_count:>12,} {arrangement_count:>14,} "
                f"{elapsed_seconds:>10.4f} "
                f"{elapsed_seconds / set_count * 1_000_000:>11.2f} "
                f"{elapsed_seconds / arrangement_count * 1_000_000:>12.4f}"
            )

    # The file carries BOTH terms of the cost model, solved from the largest
    # pool's two regimes, rather than one point's blended per-arrangement
    # rate. A blended rate is only valid in the regime it was measured in
    # (unconstrained, N leaders per set); typical runs pin the leader, where
    # one arrangement per set makes the per-set prefix the entire cost.
    largest_size = max(points)
    seconds_per_set, seconds_per_eval = solve_cost_model(*points[largest_size])
    print(
        f"fitted: seconds_per_set={seconds_per_set * 1e6:.2f}us "
        f"seconds_per_eval={seconds_per_eval * 1e6:.4f}us "
        f"(prefix is {seconds_per_set / seconds_per_eval:.1f} leader-evals)"
    )

    def project(label: str, size: int, locked: int, leader_fixed: bool) -> None:
        sets, arrangements = project_arrangements(size, locked, leader_fixed)
        seconds = sets * seconds_per_set + arrangements * seconds_per_eval
        print(
            f"projected {label}: {sets:,} sets, {arrangements:,} arrangements, "
            f"{seconds:,.1f}s ({seconds / 3600:,.2f}h)"
        )

    roster_size = (
        len(real_cards) if owned_path.exists() else _FALLBACK_ROSTER_POOL_SIZE
    )
    project(f"{roster_size}-card unconstrained", roster_size, 0, False)
    project(
        f"{roster_size}-card --require x1",
        roster_size,
        _REQUIRE_ONE_LOCKED_COUNT,
        False,
    )
    project(
        f"{roster_size}-card --leader + --require x2",
        roster_size,
        _MODE_B_LOCKED_COUNT,
        True,
    )

    # `HEURISTIC_THRESHOLD_POOL_SIZE` is DERIVED from these rates. It is
    # computed here, through the same function
    # `tests/test_cli_heuristic.py` asserts the constant against, so the two
    # cannot disagree and nobody has to work it out again.
    derived = derive_threshold_pool_size(
        Throughput(
            seconds_per_eval=seconds_per_eval,
            seconds_per_set=seconds_per_set,
            config_hash=config_hash(cfg_document),
            schema_version=SCHEMA_VERSION,
            measured_at="",
        ),
        cli.HEURISTIC_THRESHOLD_SECONDS,
    )
    print(
        f"derived HEURISTIC_THRESHOLD_POOL_SIZE = {derived} "
        f"(smallest unconstrained pool projecting over "
        f"{cli.HEURISTIC_THRESHOLD_SECONDS:,.0f}s)"
    )
    if derived != cli.HEURISTIC_THRESHOLD_POOL_SIZE:
        print(
            f"  !! cli.HEURISTIC_THRESHOLD_POOL_SIZE is "
            f"{cli.HEURISTIC_THRESHOLD_POOL_SIZE} -- update it to {derived}. "
            f"tests/test_cli_heuristic.py asserts this and will fail until you do."
        )

    out_path = Path(args.out)
    write_throughput(seconds_per_eval, seconds_per_set, cfg_document, out_path)
    print(
        f"wrote {out_path} (per_eval={seconds_per_eval!r}s, "
        f"per_set={seconds_per_set!r}s, solved at pool_size={largest_size})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
