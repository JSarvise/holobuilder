"""The validation harness: exact search versus the beam, measured at pool
sizes where the beam actually binds (N=40/N=48).

`bench/bench_heuristic.py` already compares the two solvers
at N=9…20; this script exists because that range is the range in which
**the beam is often effectively exhaustive**, so its 72/72 measures the
absence of a correctness bug rather than the quality of the pruning.

Four things this adds over `bench_heuristic.py`, and each is why it is a
separate script rather than a flag on that one:

1. **A parallel exact solver** (`--workers`), stdlib `multiprocessing`,
   partitioned by `(i, j)` first-two-member index pair — partitioning by
   first index alone costs 2.2x.
   Without it N=48 is 42 min a trial and the design is unrunnable.
   `test_validate_search.py` pins that it returns exactly what `solve_exact`
   returns on a pool small enough to check both.
2. **The binding measurement** (`--binding`). At small N the level-k child
   count is below `beam_width`, so nothing is ever cut and the comparison is
   vacuous. This reports the measured N at which the beam first truncates.
3. **Loss attribution** (`--attribute`). When the beam misses, the exact
   optimum's member set is traced through the beam's own per-level records
   (`solve_heuristic_labelled(trace=...)`) to find the level at which the
   optimum stopped being reachable, and whether it was *never built*
   (branch/extension horizon) or *built and evicted* (beam truncation).
   A bare hit rate cannot distinguish those and they have different fixes.
4. **The open-condition credit sweep** (`--open-modes`), which prices
   `heuristic._open_conditions`' three modes against the same exact answers.

Standalone script: outside `src/`, imported by no production module,
writes nothing but the `--json` path it is given. It never writes
`data/cards.json` or `data/owned.json`.

Usage:

    PYTHONPATH=src python bench/validate_search.py --binding
    PYTHONPATH=src python bench/validate_search.py --sizes 40 --trials 30
    PYTHONPATH=src python bench/validate_search.py --sizes 48 --trials 30 \\
        --json bench/validation-48.json
    PYTHONPATH=src python bench/validate_search.py --leader-dedup
"""

from __future__ import annotations

import argparse
import itertools
import json
import multiprocessing as mp
import random
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from hbuilder.domain.card import Card
from hbuilder.domain.loader import load_cards, load_owned
from hbuilder.domain.scoring import score_card_set
from hbuilder.domain.scoring_config import ScoringConfig, load_scoring_config
from hbuilder.domain.team import MEMBER_COUNT
from hbuilder.solver.dominance import (
    dominant_leaders as _dominant_leaders,
    outfit_magnitude,
    outfit_signature as _outfit_signature,
)
from hbuilder.solver.exact import canonical_pool
from hbuilder.solver.heuristic import solve_heuristic_labelled
from hbuilder.solver.parallel import default_workers
from hbuilder.solver.parallel import solve_exact_parallel as _solve_exact_parallel

TOP_N: int = 10
DEFAULT_SEED: int = 20260803

# One `Arrangement` is `(score, leader_id, member_ids)` — the shape this file's
# comparisons and its `--json` records are written in.
Arrangement = tuple[float, str, tuple[str, ...]]


def solve_exact_parallel(
    pool: tuple[Card, ...],
    cfg: ScoringConfig,
    *,
    top_n: int = TOP_N,
    workers: int = 0,
) -> tuple[Arrangement, ...]:
    """The SHIPPED parallel exact solver, flattened to this file's tuple shape.

    The harness deliberately has no exact solver of its own: validating the
    beam against a private copy would prove something about the harness, while
    validating it against the solver `--exact` actually runs proves something
    about the tool. `tests/test_parallel_exact.py` asserts the shipped solver's
    whole Top-N record against `solve_exact`.

    `workers=0` means "every logical core".
    """
    teams = _solve_exact_parallel(
        pool, cfg, top_n=top_n, workers=workers or default_workers()
    )
    return tuple((t.score, t.leader_id, t.member_ids) for t in teams)


# ---------------------------------------------------------------------------
# Pool sampling
# ---------------------------------------------------------------------------


def sample_pool(
    cards: tuple[Card, ...], size: int, rng: random.Random
) -> tuple[Card, ...]:
    """A `size`-card pool with `size` DISTINCT holomem — same rule, and same
    reason, as `bench_heuristic.sample_pool`: R1 makes a holomem-repeating pool
    smaller than its card count suggests, and differently so for the two
    solvers, which would confound the agreement number with the pool."""
    by_holomem: dict[str, list[Card]] = {}
    for card in sorted(cards, key=lambda item: item.id):
        by_holomem.setdefault(card.member, []).append(card)
    holomem = sorted(by_holomem)
    if len(holomem) < size:
        raise ValueError(
            f"pool of {size} distinct holomem requested, source has {len(holomem)}"
        )
    chosen = rng.sample(holomem, size)
    return tuple(
        sorted((rng.choice(by_holomem[name]) for name in chosen), key=lambda c: c.id)
    )


# ---------------------------------------------------------------------------
# Where the optimum was lost
# ---------------------------------------------------------------------------

LOSS_NEVER_BUILT = "never_built"
LOSS_EVICTED = "evicted"
LOSS_NOT_A_SEARCH_FAILURE = "not_a_search_failure"
LOSS_NONE = "none"


def attribute_loss(
    optimum_members: tuple[str, ...], trace: list[dict]
) -> dict:
    """Where in the beam the exact optimum stopped being reachable.

    The beam builds one card at a time and deduplicates by member SET, so the
    optimum `M*` is still reachable at level `k` iff some `k`-subset of `M*`
    survived that level. This walks `k = 1…5` and reports the FIRST level at
    which no subset survived, and — the part a hit rate cannot give you —
    whether any such subset was *built* at that level at all:

    - **`never_built`**: no `k`-subset of `M*` was even generated. The cause is
      upstream of selection — `_extend` takes the `branch` best by static
      potential plus `feature_branch` per feature, so the card is past the
      extension horizon. Widening the beam cannot fix this.
    - **`evicted`**: a `k`-subset was built and then cut by `_select_level`.
      This one *is* a width/ranking-key failure, and widening the beam or
      fixing the ranking key can fix it.

    Levels are pooled across seeds and lock groups: the optimum only has to
    survive in ONE of them to be reachable, so a per-seed report would
    overstate the loss.
    """
    by_level_built: dict[int, set[frozenset[str]]] = {}
    by_level_kept: dict[int, set[frozenset[str]]] = {}
    for record in trace:
        level = record["level"]
        by_level_built.setdefault(level, set()).update(record["built"])
        by_level_kept.setdefault(level, set()).update(record["kept"])

    target = frozenset(optimum_members)
    for level in range(1, MEMBER_COUNT + 1):
        subsets = {frozenset(combo) for combo in itertools.combinations(target, level)}
        built = by_level_built.get(level, set())
        kept = by_level_kept.get(level, set())
        if subsets & kept:
            continue
        return {
            "lost_at_level": level,
            "reason": LOSS_EVICTED if (subsets & built) else LOSS_NEVER_BUILT,
            "subsets_built": len(subsets & built),
            "subsets_possible": len(subsets),
            "level_built": len(built),
            "level_kept": len(kept),
        }
    # `M*` survived every level. The beam scored it against every admissible
    # leader in `_score_set`, so if the returned top-1 still differs the cause
    # is not the search at all — it is the objective or the tie-break, and
    # calling that a search miss would misattribute it.
    return {
        "lost_at_level": None,
        "reason": LOSS_NOT_A_SEARCH_FAILURE,
        "subsets_built": None,
        "subsets_possible": None,
        "level_built": None,
        "level_kept": None,
    }


def binding_profile(trace: list[dict], beam_width: int) -> dict:
    """Whether the beam width actually bound on this run, and by how much.

    At small N the level-k child count is below `beam_width`, `_select_level`
    returns everything, and the comparison against exact is vacuous — that is
    so a beam problem can go unnoticed there. This reports, per level, the max child count any (seed, group) produced against
    the width."""
    per_level: dict[int, int] = {}
    for record in trace:
        level = record["level"]
        per_level[level] = max(per_level.get(level, 0), len(record["built"]))
    return {
        "beam_width": beam_width,
        "max_children_per_level": {str(k): v for k, v in sorted(per_level.items())},
        "binds": any(count > beam_width for count in per_level.values()),
        "binding_levels": sorted(
            level for level, count in per_level.items() if count > beam_width
        ),
    }


# ---------------------------------------------------------------------------
# One trial
# ---------------------------------------------------------------------------


def run_trial(
    pool: tuple[Card, ...],
    cfg: ScoringConfig,
    workers: int,
    beam_options: dict,
) -> dict:
    started = time.perf_counter()
    exact = solve_exact_parallel(pool, cfg, top_n=TOP_N, workers=workers)
    exact_seconds = time.perf_counter() - started

    trace: list[dict] = []
    started = time.perf_counter()
    result = solve_heuristic_labelled(
        pool, cfg, top_n=TOP_N, trace=trace, **beam_options  # type: ignore[arg-type]
    )
    beam_seconds = time.perf_counter() - started
    beam = result.teams
    if not exact or not beam:
        raise ValueError("a pool produced no teams from one of the two solvers")

    exact_top = (exact[0][1], exact[0][2])
    beam_top = (beam[0].leader_id, beam[0].member_ids)
    exact_score = exact[0][0]
    beam_score = beam[0].score
    gap = (exact_score - beam_score) / exact_score if exact_score else 0.0
    exact_set = {(item[1], item[2]) for item in exact}
    beam_set = {(team.leader_id, team.member_ids) for team in beam}

    identity = exact_top == beam_top
    score_match = exact_score == beam_score
    loss = (
        {"lost_at_level": None, "reason": LOSS_NONE}
        if score_match
        else attribute_loss(exact[0][2], trace)
    )
    return {
        "pool_ids": [card.id for card in pool],
        "identity_match": identity,
        "score_match": score_match,
        "exact_score": exact_score,
        "beam_score": beam_score,
        "gap": gap,
        "top10_overlap": len(exact_set & beam_set),
        "exact_seconds": exact_seconds,
        "beam_seconds": beam_seconds,
        "arrangements_scored": result.arrangements_scored,
        "modes": sorted(mode.value for mode in result.modes()),
        "loss": loss,
        "binding": binding_profile(
            trace, beam_options.get("beam_width", result.beam_width)
        ),
    }


def summarise(size: int, trials: list[dict]) -> dict:
    identity = sum(1 for t in trials if t["identity_match"])
    score_equal = sum(1 for t in trials if t["score_match"])
    gaps = sorted(t["gap"] for t in trials if not t["score_match"])
    reasons: dict[str, int] = {}
    levels: dict[str, int] = {}
    for trial in trials:
        if trial["score_match"]:
            continue
        reasons[trial["loss"]["reason"]] = reasons.get(trial["loss"]["reason"], 0) + 1
        key = str(trial["loss"]["lost_at_level"])
        levels[key] = levels.get(key, 0) + 1
    summary = {
        "pool_size": size,
        "trials": len(trials),
        "top1_identity_matches": identity,
        "top1_identity_rate": identity / len(trials),
        "top1_score_matches": score_equal,
        "top1_score_rate": score_equal / len(trials),
        "top10_overlap_mean": statistics.fmean(t["top10_overlap"] for t in trials),
        "top10_overlap_min": min(t["top10_overlap"] for t in trials),
        "disagreements": len(gaps),
        "loss_reasons": reasons,
        "loss_levels": levels,
        "binds_on_trials": sum(1 for t in trials if t["binding"]["binds"]),
        "exact_seconds_mean": statistics.fmean(t["exact_seconds"] for t in trials),
        "beam_seconds_mean": statistics.fmean(t["beam_seconds"] for t in trials),
        "modes_seen": sorted({m for t in trials for m in t["modes"]}),
    }
    summary["gap_mean"] = statistics.fmean(gaps) if gaps else None
    summary["gap_median"] = statistics.median(gaps) if gaps else None
    summary["gap_max"] = max(gaps) if gaps else None
    return summary


def wilson_interval(hits: int, trials: int) -> tuple[float, float]:
    """95 % Wilson score interval. The hit rate is reported with a confidence
    interval rather than a bare "11/11", and Wilson is the one that stays inside [0,1] and stays sensible at hits == trials — which is
    exactly the case being reported, and exactly the case the normal
    approximation reports as a zero-width interval."""
    if trials == 0:
        return (0.0, 1.0)
    z = 1.959963985
    phat = hits / trials
    denom = 1.0 + z * z / trials
    centre = (phat + z * z / (2 * trials)) / denom
    half = (
        z * ((phat * (1 - phat) / trials + z * z / (4 * trials * trials)) ** 0.5)
    ) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


# ---------------------------------------------------------------------------
# Leader dedup — an EXACT reduction, measured from data
# ---------------------------------------------------------------------------


# `outfit_signature` and `dominant_leaders` are imported from
# `src/hbuilder/solver/dominance.py`, so what `--leader-dedup` measures below is
# the reduction the SOLVER actually applies, not a second implementation that
# could drift from it. The signature must include the slot `rider` (8 of the
# 167 outfits carry one); omitting it merges leaders that are not equivalent.


def outfit_signature(card: Card, board_discriminates: bool) -> tuple:
    """The shipped signature. Kept as a one-line alias so this file's own tests
    and its `--leader-dedup` report keep their positional-argument spelling."""
    return _outfit_signature(card, board_discriminates)


def dominant_leaders(pool: tuple[Card, ...], cfg: ScoringConfig) -> tuple[Card, ...]:
    """The shipped reduction."""
    return _dominant_leaders(pool, cfg)


def measure_leader_dedup(pool: tuple[Card, ...], cfg: ScoringConfig) -> dict:
    board_discriminates = getattr(cfg, "board", None) is not None
    signatures: dict[tuple, set] = {}
    for card in pool:
        costume = card.costume_skill
        signatures.setdefault(
            outfit_signature(card, board_discriminates), set()
        ).add(None if costume is None else costume.magnitude)
    survivors = dominant_leaders(pool, cfg)
    combos = sum(len(values) for values in signatures.values())
    return {
        "pool": len(pool),
        "board_discriminates": board_discriminates,
        "distinct_signatures": len(signatures),
        "signature_magnitude_combinations": combos,
        "magnitude_levels_dominated": combos - len(signatures),
        "cards_surviving_dominance": len(survivors),
        "class_reduction": len(pool) / len(signatures) if signatures else 0.0,
        "card_reduction": len(pool) / len(survivors) if survivors else 0.0,
        "dominated_families": sorted(
            f"{key[1]}/{key[2]}: {sorted(values, key=str)}"
            for key, values in signatures.items()
            if len(values) > 1
        ),
    }


def verify_leader_dedup(
    pool: tuple[Card, ...], cfg: ScoringConfig, samples: int, rng: random.Random
) -> dict:
    """**The check that is worth more than the speedup.** For `samples` random
    member sets, enumerate leaders both ways — the whole pool, and the
    dominance survivors — and require the best score to be **bit-identical**.

    If it is not, the dominance rule is wrong, and a class count would never
    have said so. `verify artefacts, not counters`."""
    survivors = dominant_leaders(pool, cfg)
    holomem_groups: dict[str, list[Card]] = {}
    for card in pool:
        holomem_groups.setdefault(card.member, []).append(card)
    names = sorted(holomem_groups)
    if len(names) < MEMBER_COUNT:
        return {"samples": 0, "violations": 0, "worst_delta": 0.0}
    violations = 0
    worst = 0.0
    for _ in range(samples):
        picked = rng.sample(names, MEMBER_COUNT)
        members = tuple(
            sorted(
                (rng.choice(holomem_groups[name]) for name in picked),
                key=lambda card: card.id,
            )
        )
        full = max(item.total_score for item in score_card_set(members, pool, cfg))
        reduced = max(
            item.total_score for item in score_card_set(members, survivors, cfg)
        )
        if full != reduced:
            violations += 1
            worst = max(worst, abs(full - reduced))
    return {
        "samples": samples,
        "leaders_full": len(pool),
        "leaders_reduced": len(survivors),
        "violations": violations,
        "worst_delta": worst,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="exact vs heuristic, measured")
    parser.add_argument("--cards", default="data/cards.json")
    parser.add_argument("--owned", default="data/owned.json")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--sizes", type=int, nargs="+", default=[40])
    parser.add_argument("--trials", type=int, default=30)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--beam-width", type=int, default=None)
    parser.add_argument("--branch", type=int, default=None)
    parser.add_argument(
        "--open-modes",
        nargs="+",
        default=None,
        help="sweep these open-condition modes over the same pools "
        "(discounted / full / off)",
    )
    parser.add_argument(
        "--binding",
        action="store_true",
        help="report the N at which the beam width first binds, and stop",
    )
    parser.add_argument(
        "--leader-dedup",
        action="store_true",
        help="measure and VERIFY the leader-signature reduction, and stop",
    )
    parser.add_argument("--json", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    cards = load_cards(Path(args.cards))
    owned_path = Path(args.owned)
    source = load_owned(owned_path, cards) if owned_path.exists() else cards
    cfg = load_scoring_config(Path(args.config))
    rng = random.Random(args.seed)
    print(f"source: {owned_path if owned_path.exists() else args.cards} "
          f"({len(source)} cards)")

    beam_options: dict = {}
    if args.beam_width is not None:
        beam_options["beam_width"] = args.beam_width
    if args.branch is not None:
        beam_options["branch"] = args.branch

    if args.leader_dedup:
        pool = canonical_pool(source)
        measured = measure_leader_dedup(pool, cfg)
        verified = verify_leader_dedup(pool, cfg, 200, random.Random(args.seed))
        print(json.dumps({"dedup": measured, "verification": verified}, indent=2))
        if args.json:
            Path(args.json).write_text(
                json.dumps(
                    {"dedup": measured, "verification": verified},
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        return 0

    if args.binding:
        rows = []
        for size in args.sizes:
            pool = sample_pool(source, size, rng)
            trace: list[dict] = []
            solve_heuristic_labelled(pool, cfg, top_n=TOP_N, trace=trace, **beam_options)  # type: ignore[arg-type]
            profile = binding_profile(
                trace, beam_options.get("beam_width", 96)
            )
            profile["pool_size"] = size
            rows.append(profile)
            print(
                f"N={size:>3} width={profile['beam_width']:>4} "
                f"binds={profile['binds']!s:>5} "
                f"levels={profile['binding_levels']} "
                f"max_children={profile['max_children_per_level']}"
            )
        if args.json:
            Path(args.json).write_text(
                json.dumps({"binding": rows}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        return 0

    modes = args.open_modes or [None]
    summaries: list[dict] = []
    records: dict[str, list[dict]] = {}
    for size in args.sizes:
        pools = [sample_pool(source, size, rng) for _ in range(args.trials)]
        # Exact is computed ONCE per pool and shared across the mode sweep —
        # the modes must be compared against the same truth, and exact is
        # ~99 % of the wall clock at these sizes.
        exact_by_pool: list[tuple[Arrangement, ...]] = []
        exact_seconds: list[float] = []
        for index, pool in enumerate(pools):
            started = time.perf_counter()
            exact_by_pool.append(
                solve_exact_parallel(pool, cfg, top_n=TOP_N, workers=args.workers)
            )
            exact_seconds.append(time.perf_counter() - started)
            print(f"  N={size} exact {index + 1}/{len(pools)} "
                  f"{exact_seconds[-1]:.1f}s", flush=True)
        for mode in modes:
            options = dict(beam_options)
            label = f"{size}"
            if mode is not None:
                options["open_condition_mode"] = mode
                label = f"{size}:{mode}"
            trials: list[dict] = []
            for index, (pool, exact) in enumerate(zip(pools, exact_by_pool)):
                trace: list[dict] = []
                started = time.perf_counter()
                result = solve_heuristic_labelled(
                    pool, cfg, top_n=TOP_N, trace=trace, **options  # type: ignore[arg-type]
                )
                beam_seconds = time.perf_counter() - started
                beam = result.teams
                exact_top = (exact[0][1], exact[0][2])
                beam_top = (beam[0].leader_id, beam[0].member_ids)
                gap = (
                    (exact[0][0] - beam[0].score) / exact[0][0] if exact[0][0] else 0.0
                )
                score_match = exact[0][0] == beam[0].score
                trials.append(
                    {
                        "trial": index,
                        "pool_ids": [card.id for card in pool],
                        "identity_match": exact_top == beam_top,
                        "score_match": score_match,
                        "exact_score": exact[0][0],
                        "beam_score": beam[0].score,
                        "gap": gap,
                        "top10_overlap": len(
                            {(item[1], item[2]) for item in exact}
                            & {(t.leader_id, t.member_ids) for t in beam}
                        ),
                        "exact_seconds": exact_seconds[index],
                        "beam_seconds": beam_seconds,
                        "arrangements_scored": result.arrangements_scored,
                        "modes": sorted(m.value for m in result.modes()),
                        "loss": (
                            {"lost_at_level": None, "reason": LOSS_NONE}
                            if score_match
                            else attribute_loss(exact[0][2], trace)
                        ),
                        "binding": binding_profile(
                            trace, options.get("beam_width", result.beam_width)
                        ),
                    }
                )
                print(
                    f"  N={size}{'' if mode is None else ' [' + mode + ']'} "
                    f"trial {index}: "
                    f"{'MATCH' if trials[-1]['identity_match'] else 'DIFFER'} "
                    f"gap={gap * 100:+.4f}% overlap={trials[-1]['top10_overlap']}/10 "
                    f"loss={trials[-1]['loss']['reason']}"
                    f"@{trials[-1]['loss']['lost_at_level']} "
                    f"binds={trials[-1]['binding']['binds']}",
                    flush=True,
                )
            records[label] = trials
            summary = summarise(size, trials)
            summary["open_condition_mode"] = mode
            low, high = wilson_interval(summary["top1_score_matches"], len(trials))
            summary["top1_score_ci95"] = [low, high]
            summaries.append(summary)

    print("")
    print(f"{'N':>4} {'mode':<11} {'trials':>6} {'top1 id':>9} {'top1 score':>11} "
          f"{'ci95':>16} {'top10':>6} {'gap max':>9} {'binds':>6}")
    for summary in summaries:
        gap_max = "—" if summary["gap_max"] is None else f"{summary['gap_max']*100:.3f}%"
        low, high = summary["top1_score_ci95"]
        print(
            f"{summary['pool_size']:>4} "
            f"{str(summary['open_condition_mode'] or 'shipped'):<11} "
            f"{summary['trials']:>6} "
            f"{summary['top1_identity_matches']:>4}/{summary['trials']:<4} "
            f"{summary['top1_score_matches']:>5}/{summary['trials']:<5} "
            f"{low*100:>6.1f}-{high*100:<6.1f}% "
            f"{summary['top10_overlap_mean']:>6.2f} {gap_max:>9} "
            f"{summary['binds_on_trials']:>3}/{summary['trials']:<3}"
        )
        if summary["loss_reasons"]:
            print(f"       loss reasons: {summary['loss_reasons']} "
                  f"at levels {summary['loss_levels']}")
    print("")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"summaries": summaries, "trials": records}, indent=2,
                       sort_keys=True),
            encoding="utf-8",
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
