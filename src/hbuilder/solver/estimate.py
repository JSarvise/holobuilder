"""The constrained-pool arrangement projection and the throughput-file
provenance contract `--estimate` reads.

Pure computation only — `project_arrangements` and `project_duration` never
enumerate and never score. The one narrow I/O this module performs is
`load_throughput`'s read of the benchmark's provenance-carrying output file
(`bench/throughput.json`, written by `bench/bench_solver.py`);
an absent file is a normal pre-benchmark state, not an error.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

from hbuilder.domain.team import MEMBER_COUNT

# The confirmation threshold is a UX taste choice, not a model
# constant. It belongs in code (here) or as an argparse default and must
# never appear in config.json, which holds values with a real-but-unknown
# answer (probability tiers, which --extract may one day overwrite) or
# that are player-dependent (assumed_run_state, expected_fires_per_run).
# Placing "how long before I ask for confirmation" beside the probability
# tiers would falsely imply it affects results.
CONFIRM_THRESHOLD_SECONDS: float = 30.0

THROUGHPUT_PATH: Path = Path("bench/throughput.json")

# The upper bound of `derive_threshold_pool_size`'s search. Far above any real
# collection (a large one is ~90 and the full roster is 167); it exists only so a
# broken measurement raises instead of looping.
_THRESHOLD_SEARCH_CEILING: int = 200

# The single declaration of the producer (bench/bench_solver.py) /
# consumer (load_throughput, here) contract for the throughput file, so
# the two sides cannot drift out of sync with each other.
THROUGHPUT_REQUIRED_KEYS: tuple[str, ...] = (
    "seconds_per_eval",
    "seconds_per_set",
    "config_hash",
    "schema_version",
    "measured_at",
)


class ThroughputSchemaError(ValueError):
    """Raised by `load_throughput` when a present `bench/throughput.json`
    is missing one or more of `THROUGHPUT_REQUIRED_KEYS`. A benchmark
    whose output shape has rotted must fail loudly here, never silently
    produce a stale or partial duration projection."""


@dataclass(frozen=True, slots=True)
class Throughput:
    """One measured-throughput record, read verbatim from
    `bench/throughput.json`. `config_hash` is compared against the
    current run's `config_hash(...)` by `project_duration` to detect a
    measurement taken before the scoring model changed.

    **Two costs, because the solver has two.** `score_card_set(members,
    leaders, cfg)` pays a set-invariant prefix (gate resolution over the five
    members, every member EV, `base_stat_sum`, `team_rate_up`) once per member
    SET, then one `combine()` per candidate leader. A single per-arrangement
    rate cannot describe both regimes: on a 91-card pool the prefix is ~8x
    one leader's marginal cost, so an unconstrained run (91 leaders per set,
    prefix amortised) and a `--leader` run (1 leader per set, prefix paid in
    full) differ by ~8x per arrangement. Projecting the second from the first
    would price a 178 s run at `0.04 s`.
    """

    seconds_per_eval: float
    seconds_per_set: float
    config_hash: str
    schema_version: str
    measured_at: str


@dataclass(frozen=True, slots=True)
class Projection:
    """The frozen, slotted record the CLI prints before every search and
    `--estimate` returns directly."""

    pool_size: int
    locked_count: int
    leader_fixed: bool
    set_count: int
    arrangement_count: int
    seconds_or_none: float | None
    throughput_warning: str | None


def project_arrangements(
    pool_size: int, locked_count: int, leader_fixed: bool
) -> tuple[int, int]:
    """The full formula, computed from the *constrained* pool — never the
    raw one. It counts both of the enumerator's families. Let `N` =
    `pool_size` (after `--exclude`/ownership filtering) and `k` =
    `locked_count` (the number of
    distinct cards locked by `--require` and `--leader` combined):

        family A — every lock is a member:
            A_sets = C(N - k, 5 - k)
            A_arrangements = A_sets * (1 if leader_fixed else N)

        family B — exactly one lock is deferred to the outfit slot:
            B_deferrals = 0 if k == 0 else (1 if leader_fixed else k)
            B_sets = B_deferrals * C(N - k, 6 - k)
            B_arrangements = B_sets          (one admissible leader each)

        set_count = A_sets + B_sets
        arrangement_count = A_arrangements + B_arrangements

    The leader factor is `N`, not 6. A team is 5 members plus an outfit slot
    that sits OUTSIDE them, so the leader is chosen from the whole pool —
    including a card already among the five — rather than being one of six
    positions taking a turn (C(54,5) * 54, not C(54,6) * 6).

    **Family B cannot be left out.** `--require x` is satisfied by x being a
    *member* OR by x being the *leader*, so a set that omits x is still a
    candidate — `solver.exact._iter_member_sets` enumerates both families.
    Counting only family A on a 91-card pool with the leader pinned and two
    members required (k=3) gives C(88,2) = 3,828 arrangements against 113,564
    actually scored: a 29.7x under-count.

    A second consequence: `--require x` alone (no `--leader`) does NOT reduce
    the SET count at all. A_sets + B_sets = C(N-1,4) + C(N-1,5) = C(N,5),
    exactly the unconstrained figure — every 5-set remains reachable, either
    holding x or leaving x for the outfit slot. It reduces *arrangements*
    (9.1x at N=54), and since the per-set prefix is unchanged the wall-clock
    saving is smaller still.

    `set_count` counts the sets the LOCKS permit; `solver.exact.iter_candidates`
    additionally drops sets that repeat a holomem (R1), which depends on the
    pool's holomem multiplicities and so cannot be computed here. On a
    91-card pool 89.98% of 5-sets survive R1, so this over-counts by ~11% —
    the safe direction for a confirmation gate.

    Degenerate inputs give zero rather than a negative binomial argument:
    `math.comb` already returns 0 when the choose-count exceeds the pool, and
    a negative choose-count means that family cannot exist at all. Note that
    `locked_count == 6` is NOT degenerate: five locks are the members and the
    sixth is the outfit.
    """
    free = pool_size - locked_count
    a_choose = MEMBER_COUNT - locked_count
    b_choose = MEMBER_COUNT + 1 - locked_count

    a_sets = math.comb(free, a_choose) if 0 <= a_choose <= free else 0
    if locked_count == 0:
        b_sets = 0
    else:
        deferrals = 1 if leader_fixed else locked_count
        b_sets = deferrals * math.comb(free, b_choose) if 0 <= b_choose <= free else 0

    set_count = a_sets + b_sets
    arrangement_count = a_sets * (1 if leader_fixed else pool_size) + b_sets
    return (set_count, arrangement_count)


def derive_threshold_pool_size(
    throughput: Throughput, threshold_seconds: float
) -> int:
    """The smallest unconstrained pool size whose projected EXACT duration
    exceeds `threshold_seconds` — i.e. what `cli.HEURISTIC_THRESHOLD_POOL_SIZE`
    must equal, computed rather than chosen.

    That constant depends on the benchmarked cost model, and measured rates
    move by tens of percent between runs, so it must not be picked by hand.
    This is the derivation, in one place, so `bench/bench_solver.py` prints
    the same number `tests/test_cli_heuristic.py` asserts and neither can
    drift from the rates.

    It stays a constant in `cli.py` rather than being computed at import: the
    pool-size rule is precisely the FALLBACK for when `bench/throughput.json` is
    absent or rejected, so a value read from that file at run time would vanish
    exactly when it is needed (`should_use_heuristic`).

    Searching upward from `MEMBER_COUNT + 1` (the smallest pool that can field
    a team at all) and returning the first crossing makes the boundary tight by
    construction — the property `test_cli_heuristic.py` checks separately, and
    the one a hand-picked N can satisfy loosely while silently sending pools the
    exact solver could still finish to the beam.

    `SEARCH_CEILING` bounds the walk so a pathologically cheap measurement
    cannot loop forever; a model that never crosses 300 s below a 200-card pool
    is a broken measurement, and raising is the honest answer.
    """
    for pool_size in range(MEMBER_COUNT + 1, _THRESHOLD_SEARCH_CEILING):
        set_count, arrangement_count = project_arrangements(pool_size, 0, False)
        seconds = (
            set_count * throughput.seconds_per_set
            + arrangement_count * throughput.seconds_per_eval
        )
        if seconds > threshold_seconds:
            return pool_size
    raise ValueError(
        f"no pool size below {_THRESHOLD_SEARCH_CEILING} projects longer than "
        f"{threshold_seconds}s at seconds_per_set="
        f"{throughput.seconds_per_set!r} / seconds_per_eval="
        f"{throughput.seconds_per_eval!r} — that is a broken measurement, not "
        f"a fast one"
    )


def config_hash(cfg_document: dict) -> str:
    """SHA-256 hex digest of the *normalized merged config document* —
    `json.dumps(cfg_document, sort_keys=True, ensure_ascii=False)` — never
    the raw file bytes.

    The merged document (the one `load_scoring_config` assembles by
    combining a present `config.json` with `DEFAULT_CONFIG` at
    top-level-key granularity) is what actually determines scoring: it is
    invariant to formatting and key order, and it is identical whether a
    given value came from the file or from `DEFAULT_CONFIG`. Hashing file
    bytes would report a false change for a mere reformat, and it has no
    stable answer at all for an absent file (there are no bytes to hash),
    even though an absent file still produces a fully-determined merged
    document. Neither failure mode is acceptable for the question
    `project_duration` actually needs answered — "did the scoring model
    that produced this run change since the benchmark was measured?" —
    so the normalized merged config is hashed instead.
    """
    normalized = json.dumps(cfg_document, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_throughput(path: Path = THROUGHPUT_PATH) -> Throughput | None:
    """Return `None` when `path` does not exist — a normal state before
    the benchmark has ever been run, not an error. When present, every
    key in `THROUGHPUT_REQUIRED_KEYS` must appear, or this raises
    `ThroughputSchemaError` naming every missing key, so a benchmark
    output whose shape has rotted is a loud failure rather than a
    silently stale projection."""
    if not path.exists():
        return None

    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)

    missing = [key for key in THROUGHPUT_REQUIRED_KEYS if key not in doc]
    if missing:
        raise ThroughputSchemaError(
            f"{path}: missing required key(s) {missing!r} — "
            f"THROUGHPUT_REQUIRED_KEYS is the one declared contract both "
            f"bench/bench_solver.py and load_throughput honor, and a "
            f"benchmark output that no longer satisfies it must fail "
            f"loudly rather than silently project a partial duration"
        )

    return Throughput(
        seconds_per_eval=float(doc["seconds_per_eval"]),
        seconds_per_set=float(doc["seconds_per_set"]),
        config_hash=str(doc["config_hash"]),
        schema_version=str(doc["schema_version"]),
        measured_at=str(doc["measured_at"]),
    )


def project_duration(
    arrangement_count: int,
    throughput: Throughput | None,
    current_config_hash: str,
    *,
    set_count: int,
) -> tuple[float | None, str | None]:
    """Returns `(seconds, warning)`, priced as

        seconds = set_count * seconds_per_set
                + arrangement_count * seconds_per_eval

    — two terms because the solver has two costs (see `Throughput`). Pricing
    only the arrangements would make a `--leader` run project ~8x short: with
    the leader pinned there is one arrangement per set, so the set-invariant
    prefix is the whole run and the marginal per-leader rate describes almost
    none of it.

    `set_count` is a REQUIRED keyword — the second value
    `project_arrangements` already returns, threaded explicitly. A default of
    zero would silently drop the per-set term, which is exactly the
    under-pricing the two-rate model exists to prevent, so there is no default
    and a caller that forgets fails at the call, not at the projection.

    - No throughput measurement (`throughput is None`): `(None, "no
      measured throughput available; run bench/bench_solver.py to
      project a duration")` — an unmeasurable projection is not evidence
      of a long run, so the CLI must not treat this as a reason to
      confirm.
    - A measurement whose `config_hash` differs from
      `current_config_hash`: the computed seconds **and** a warning
      naming both hashes and stating that the measurement predates the
      current scoring model — reporting a stale per-eval cost
      with full confidence is exactly what this guards against.
    - A matching hash: `(seconds, None)`.
    """
    if throughput is None:
        return (
            None,
            "no measured throughput available; run bench/bench_solver.py "
            "to project a duration",
        )

    seconds = (
        set_count * throughput.seconds_per_set
        + arrangement_count * throughput.seconds_per_eval
    )

    if throughput.config_hash != current_config_hash:
        return (
            seconds,
            f"throughput measurement describes a DIFFERENT scoring model than "
            f"this run (measured against config_hash="
            f"{throughput.config_hash!r}, current config_hash="
            f"{current_config_hash!r}) — this duration may be inaccurate. "
            f"Re-run bench/bench_solver.py with the same --board/--no-board "
            f"choice as this run to project from matching rates",
        )

    return (seconds, None)
