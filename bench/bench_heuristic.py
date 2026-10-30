"""Heuristic-vs-exact agreement harness — the measurement that decides whether
the beam in `hbuilder.solver.heuristic` is usable at all.

It runs heuristic and exact search over the same small collection and reports
how often and by how much they disagree on the top team.

**Why a harness and not a unit test.** A beam that reports its own confidence
proves nothing about its answers. The only statement worth making about an
approximate solver is one obtained by running the *exact* solver over the
*same* pool and diffing the two answers arrangement by arrangement. That is
what this script does, and every number it prints is measured.

Standalone script, like `bench/bench_solver.py`: lives outside `src/`,
is never imported by any production module, and writes nothing except its own
report to stdout (and, with `--json`, to the path the operator names). It never
writes `data/cards.json` or `data/owned.json`.

## What it reports

For each pool size, over `--trials` independently sampled pools:

- **top-1 identity match rate** — the fraction of pools where the beam's rank-1
  `(leader_id, member_ids)` is *the same arrangement* exact ranked 1. Identity,
  not score equality: two different arrangements that happen to tie are a match
  only if the tie-break also agrees, because that is what the user sees.
- **top-1 score match rate** — the weaker, and more honest, companion: the
  fraction where the beam's rank-1 *score* equals exact's. A beam that finds an
  equally-good different team has not lost the user anything.
- **top-10 overlap** — mean size of the intersection of the two Top-10
  arrangement sets, out of 10.
- **the score-gap distribution when they differ** — `(exact - beam) / exact` on
  the rank-1 score, reported as mean / median / p90 / max over the pools that
  disagreed. Zero-length when they never disagreed, which is stated rather than
  printed as a zero.

Pool sizes default to N=9..14: below 9 a pool barely admits a beam wider than
the space, and above 14 `solve_exact` stops finishing quickly enough to run
many trials (C(14,5) x 14 = 28,028 arrangements is already ~1.5 s).

Usage:

    PYTHONPATH=src python bench/bench_heuristic.py
    PYTHONPATH=src python bench/bench_heuristic.py --sizes 9 12 --trials 12
    PYTHONPATH=src python bench/bench_heuristic.py --json bench/agreement.json
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from hbuilder.domain.card import Card, GateCondition
from hbuilder.domain.gates import resolve_team_gates
from hbuilder.domain.loader import load_cards, load_owned
from hbuilder.domain.scoring_config import ScoringConfig, load_scoring_config
from hbuilder.solver.exact import RankedTeam, solve_exact
from hbuilder.solver.heuristic import solve_heuristic_labelled
from hbuilder.solver.parallel import default_workers, solve_exact_parallel

DEFAULT_SIZES: tuple[int, ...] = (9, 10, 11, 12, 13, 14)
DEFAULT_TRIALS: int = 8
DEFAULT_SEED: int = 20260803
TOP_N: int = 10

# ---------------------------------------------------------------------------
# The post-mortem: why did the beam miss THIS optimum?
# ---------------------------------------------------------------------------
#
# The search reports a *specific* failure mode rather than a generic
# "approximate" caveat, and `solver.heuristic` emits seven. But
# a label the search emits about ITSELF is a counter, not an artefact: it says
# what the search observed, not what actually went wrong. The only way to tell
# them apart is to look at the team the search MISSED — available here and
# nowhere else, because only this harness holds the exact answer beside the
# approximate one.
#
# So every disagreement is diagnosed twice: once by the beam (`modes`), and
# once structurally, from the exact optimum (`diagnosis`). Reporting both turns
# "the beam says it truncated" into "the beam said it truncated, and the team
# it missed did in fact need two particular members together" — or, more
# usefully, into "the beam reported nothing and the missed team needed a
# pairwise gate", which is a defect in the LABELLING rather than in the search.


def tight_multi_member_gates(team, run_state) -> list[str]:
    """Gates on a team that fire ONLY because two or more particular members
    are present together — the pairwise-gate failure mode, detected on
    the missed team rather than inferred from the search's own bookkeeping.

    "Tight" means the matcher count EQUALS the gate's own `count`: every
    matching member is load-bearing, so the gate pays nothing until the last of
    them arrives and a beam adding one card at a time sees no credit for the
    partial. A `count=2` gate with three matchers is not tight — it would have
    fired without one of them, so it cannot be what hid the team.
    """
    tight: list[str] = []
    for resolved in resolve_team_gates(team, run_state):
        for gate in (resolved.gate, resolved.override_gate):
            if gate is None or not gate.fired:
                continue
            predicate = gate.predicate
            if predicate.threshold < 2:
                continue
            # `actual_count` is the matcher count the resolver itself computed,
            # so "tight" is read off the resolution rather than recomputed here
            # — one definition of who matches, not two.
            if gate.actual_count == predicate.threshold:
                tight.append(
                    f"{resolved.card_id}:{resolved.slot_kind}:"
                    f"{predicate.dimension}={predicate.value}x{predicate.threshold}"
                )
    return sorted(set(tight))


def diagnose_miss(exact_top, beam_teams, cfg) -> dict:
    """A structural account of one miss, in the vocabulary of the failure
    modes — built from the exact answer, so it can contradict the beam."""
    beam_cards = {
        card_id
        for team in beam_teams
        for card_id in (team.leader_id, *team.member_ids)
    }
    optimum_cards = {exact_top.leader_id, *exact_top.member_ids}
    return {
        # Cards in the true optimum the beam's returned teams never used. Not
        # proof of unreachability — the beam may have built with them and
        # dropped them — but when it is non-empty, the beam's whole answer
        # never contained the winning material.
        "optimum_cards_absent_from_beam_output": sorted(optimum_cards - beam_cards),
        "tight_multi_member_gates_in_optimum": tight_multi_member_gates(
            exact_top.team, cfg.run_state
        ),
    }


def sample_pool(cards: tuple[Card, ...], size: int, rng: random.Random) -> tuple[Card, ...]:
    """A `size`-card pool with `size` DISTINCT holomem.

    R1 (five distinct holomem among the members) is an enumeration constraint,
    so a pool that repeats a holomem has a smaller reachable space than its
    card count suggests — and, worse, a *different* one for the beam than for
    exact if the beam's static-potential ordering happens to front-load one
    character's skins. One card per holomem removes that confound entirely, so
    the agreement number measures the search and nothing else.
    """
    by_holomem: dict[str, list[Card]] = {}
    for card in sorted(cards, key=lambda item: item.id):
        by_holomem.setdefault(card.member, []).append(card)
    holomem = sorted(by_holomem)
    if len(holomem) < size:
        raise ValueError(
            f"pool of {size} distinct holomem requested, source has "
            f"{len(holomem)}"
        )
    chosen = rng.sample(holomem, size)
    return tuple(sorted((rng.choice(by_holomem[name]) for name in chosen), key=lambda c: c.id))


def arrangement(team: RankedTeam) -> tuple[str, tuple[str, ...]]:
    return (team.leader_id, team.member_ids)


def compare_one(
    pool: tuple[Card, ...],
    cfg: ScoringConfig,
    workers: int = 1,
    **beam_options: object,
) -> dict:
    """Run both solvers over one pool and diff them.

    `workers > 1` takes the reference through `solve_exact_parallel`, which is
    bit-identical to `solve_exact` (`tests/test_parallel_exact.py`) and is what
    makes N=25..32 affordable at all — the sizes where the beam is actually
    under pressure. At N<=20 the beam is wider than the space it is searching
    and an agreement number there measures nothing.

    **Deliberately NOT pruned by `solver.dominance`.** Dominance preserves the
    optimum but not the Top-N tail (36 of 40 in one measurement), and this harness reports
    top-10 overlap, so a pruned reference would manufacture disagreements that
    are an artefact of the reference rather than of the beam.
    """
    started = time.perf_counter()
    if workers > 1:
        exact = solve_exact_parallel(pool, cfg, top_n=TOP_N, workers=workers)
    else:
        exact = solve_exact(pool, cfg, top_n=TOP_N)
    exact_seconds = time.perf_counter() - started

    started = time.perf_counter()
    result = solve_heuristic_labelled(pool, cfg, top_n=TOP_N, **beam_options)  # type: ignore[arg-type]
    beam_seconds = time.perf_counter() - started
    beam = result.teams

    if not exact or not beam:
        raise ValueError("a pool produced no teams from one of the two solvers")

    exact_top = arrangement(exact[0])
    beam_top = arrangement(beam[0])
    exact_score = exact[0].score
    beam_score = beam[0].score
    gap = (exact_score - beam_score) / exact_score if exact_score else 0.0
    overlap = len({arrangement(team) for team in exact} & {arrangement(team) for team in beam})

    record = {
        "pool_ids": [card.id for card in pool],
        "identity_match": exact_top == beam_top,
        "score_match": exact_score == beam_score,
        "exact_score": exact_score,
        "beam_score": beam_score,
        "gap": gap,
        "top10_overlap": overlap,
        "exact_seconds": exact_seconds,
        "beam_seconds": beam_seconds,
        "arrangements_scored": result.arrangements_scored,
        "partials_scored": result.partials_scored,
        "modes": sorted(mode.value for mode in result.modes()),
    }
    # The post-mortem runs on EVERY trial, not only the failures. A mode that
    # is also present on every hit explains nothing, and that can only be seen
    # by measuring the hits too — see `format_modes`.
    record["diagnosis"] = diagnose_miss(exact[0], beam, cfg)
    return record


def summarise(size: int, trials: list[dict]) -> dict:
    identity = sum(1 for trial in trials if trial["identity_match"])
    score_equal = sum(1 for trial in trials if trial["score_match"])
    overlaps = [trial["top10_overlap"] for trial in trials]
    # The gap distribution is over the pools that DISAGREED on score. Averaging
    # in the zeros from the agreeing pools would report a reassuringly small
    # mean that describes nothing: the question "when it is wrong, how wrong is
    # it" is a different question from "how often is it wrong", and one number
    # cannot answer both.
    gaps = sorted(trial["gap"] for trial in trials if not trial["score_match"])
    summary = {
        "pool_size": size,
        "trials": len(trials),
        "top1_identity_matches": identity,
        "top1_identity_rate": identity / len(trials),
        "top1_score_matches": score_equal,
        "top1_score_rate": score_equal / len(trials),
        "top10_overlap_mean": statistics.fmean(overlaps),
        "top10_overlap_min": min(overlaps),
        "disagreements": len(gaps),
        "exact_seconds_mean": statistics.fmean(t["exact_seconds"] for t in trials),
        "beam_seconds_mean": statistics.fmean(t["beam_seconds"] for t in trials),
        "arrangements_scored_mean": statistics.fmean(
            t["arrangements_scored"] for t in trials
        ),
        "modes_seen": sorted({mode for trial in trials for mode in trial["modes"]}),
    }
    if gaps:
        summary["gap_mean"] = statistics.fmean(gaps)
        summary["gap_median"] = statistics.median(gaps)
        summary["gap_p90"] = gaps[min(len(gaps) - 1, int(0.9 * len(gaps)))]
        summary["gap_max"] = max(gaps)
    else:
        summary["gap_mean"] = None
        summary["gap_median"] = None
        summary["gap_p90"] = None
        summary["gap_max"] = None
    return summary


def format_report(summaries: list[dict]) -> list[str]:
    lines = [
        "",
        "heuristic vs exact — MEASURED agreement",
        "",
        f"{'N':>3} {'trials':>6} {'top1 id':>8} {'top1 score':>11} "
        f"{'top10':>6} {'gap mean':>10} {'gap max':>9} {'exact s':>9} {'beam s':>8}",
    ]
    for summary in summaries:
        gap_mean = (
            "—" if summary["gap_mean"] is None else f"{summary['gap_mean'] * 100:.3f}%"
        )
        gap_max = (
            "—" if summary["gap_max"] is None else f"{summary['gap_max'] * 100:.3f}%"
        )
        lines.append(
            f"{summary['pool_size']:>3} {summary['trials']:>6} "
            f"{summary['top1_identity_matches']:>3}/{summary['trials']:<4} "
            f"{summary['top1_score_matches']:>4}/{summary['trials']:<6} "
            f"{summary['top10_overlap_mean']:>6.2f} {gap_mean:>10} {gap_max:>9} "
            f"{summary['exact_seconds_mean']:>9.3f} {summary['beam_seconds_mean']:>8.3f}"
        )
    lines.append("")
    lines.append("gap = (exact_top1 - beam_top1) / exact_top1, over the trials that DISAGREED")
    lines.append("top10 = mean size of the two Top-10 arrangement sets' intersection, out of 10")
    modes = sorted({mode for summary in summaries for mode in summary["modes_seen"]})
    lines.append(f"failure modes observed across all trials: {', '.join(modes) or 'none'}")
    lines.append("")
    return lines


def format_modes(trials: list[dict]) -> list[str]:
    """Each failure mode's rate on the trials it MISSED against the trials it
    HIT — the only form in which a label says anything.

    A mode emitted on 100 % of misses and 100 % of hits is a constant with a
    scary name; a mode emitted on 100 % of misses and 5 % of hits is a
    diagnosis. A mode can also be silent in exactly the case it exists for,
    so the discriminating statistic is the point of this table rather than an
    extra.

    The last two rows are not the beam's own labels at all: they are the
    STRUCTURAL post-mortem of the team the beam missed, computed from the exact
    answer. They are what can contradict the beam.
    """
    misses = [trial for trial in trials if not trial["score_match"]]
    hits = [trial for trial in trials if trial["score_match"]]
    every_mode = sorted({mode for trial in trials for mode in trial["modes"]})

    lines = [
        "",
        "FAILURE MODES — rate on the trials that MISSED vs the trials that HIT",
        f"  {len(misses)} missed, {len(hits)} hit, {len(trials)} total",
        "",
        f"  {'mode':<44} {'on misses':>12} {'on hits':>12}",
    ]

    def rate(rows, predicate) -> str:
        if not rows:
            return "     n/a"
        hit = sum(1 for row in rows if predicate(row))
        return f"{hit:>4}/{len(rows):<4} {hit / len(rows) * 100:5.0f}%"

    for mode in every_mode:
        lines.append(
            f"  {mode:<44} {rate(misses, lambda r, m=mode: m in r['modes']):>12} "
            f"{rate(hits, lambda r, m=mode: m in r['modes']):>12}"
        )
    lines.append("")
    lines.append("  STRUCTURAL post-mortem of the missed optimum (not the beam's own label):")
    lines.append(
        f"  {'optimum used a card absent from the beam output':<44} "
        f"{rate(misses, lambda r: bool(r['diagnosis']['optimum_cards_absent_from_beam_output'])):>12} "
        f"{rate(hits, lambda r: bool(r['diagnosis']['optimum_cards_absent_from_beam_output'])):>12}"
    )
    lines.append(
        f"  {'optimum needed a TIGHT multi-member gate':<44} "
        f"{rate(misses, lambda r: bool(r['diagnosis']['tight_multi_member_gates_in_optimum'])):>12} "
        f"{rate(hits, lambda r: bool(r['diagnosis']['tight_multi_member_gates_in_optimum'])):>12}"
    )
    lines.append("")
    if misses:
        lines.append("  every miss, named:")
        for trial in misses:
            lines.append(
                f"    N={len(trial['pool_ids'])} gap={trial['gap'] * 100:+.4f}% "
                f"overlap={trial['top10_overlap']}/10"
            )
            lines.append(f"      beam reported : {', '.join(trial['modes']) or 'NOTHING'}")
            diagnosis = trial["diagnosis"]
            lines.append(
                "      missing cards : "
                f"{', '.join(diagnosis['optimum_cards_absent_from_beam_output']) or 'none'}"
            )
            lines.append(
                "      tight gates   : "
                f"{', '.join(diagnosis['tight_multi_member_gates_in_optimum']) or 'none'}"
            )
    else:
        lines.append("  no miss to name — every trial matched on score.")
    lines.append("")
    return lines


# The ablation ladder, built as an ADD-ONE ladder rather than a remove-one one.
#
# Removing one mechanism at a time from a `beam_width=8` baseline ties every
# row at 8/8 — at that width, on a pool small enough for `solve_exact` to
# finish, the beam is already effectively exhaustive. A ladder in which
# nothing moves attributes nothing.
#
# So the baseline is the bare beam with every mechanism OFF, at the narrowest
# setting that still forms teams, and each row switches exactly ONE mechanism
# on. Then the column that moves names the mechanism that closed the gap, and a
# mechanism that moves nothing is reported as moving nothing — which is a
# finding about the beam, in either direction, rather than a blank.
ABLATION_BASELINE: dict = {
    "beam_width": 4,
    "branch": 3,
    "seeds": 1,
    "quota": 0,
    "feature_branch": 0,
    "open_condition_weight": 0.0,
    "polish_teams": 0,
}
ABLATIONS: tuple[tuple[str, dict], ...] = (
    ("baseline (every mechanism off)", {}),
    ("+ open-condition bonus", {"open_condition_weight": 0.35}),
    ("+ per-feature quota", {"quota": 2}),
    ("+ per-feature branch", {"feature_branch": 2}),
    ("+ four seeds", {"seeds": 4}),
    ("+ swap polish", {"polish_teams": 3}),
    ("all mechanisms on", {
        "open_condition_weight": 0.35,
        "quota": 2,
        "feature_branch": 2,
        "seeds": 4,
        "polish_teams": 3,
    }),
    ("shipped defaults", {"beam_width": 96, "branch": 24, "seeds": 4, "quota": 2,
                          "feature_branch": 2, "open_condition_weight": 0.35,
                          "polish_teams": 3}),
)


def run_ablation(
    source: tuple[Card, ...], cfg: ScoringConfig, sizes: list[int], trials: int, seed: int
) -> list[dict]:
    """Every ablation over the SAME pools, so the rows are comparable."""
    rng = random.Random(seed)
    pools = [
        sample_pool(source, size, rng) for size in sizes for _ in range(trials)
    ]
    exact_by_pool = []
    for pool in pools:
        exact = solve_exact(pool, cfg, top_n=TOP_N)
        exact_by_pool.append(exact)

    rows: list[dict] = []
    for label, override in ABLATIONS:
        options = dict(ABLATION_BASELINE)
        options.update(override)
        identity = 0
        score_equal = 0
        gaps: list[float] = []
        overlaps: list[int] = []
        started = time.perf_counter()
        for pool, exact in zip(pools, exact_by_pool):
            result = solve_heuristic_labelled(pool, cfg, top_n=TOP_N, **options)  # type: ignore[arg-type]
            beam = result.teams
            if arrangement(beam[0]) == arrangement(exact[0]):
                identity += 1
            if beam[0].score == exact[0].score:
                score_equal += 1
            else:
                gaps.append((exact[0].score - beam[0].score) / exact[0].score)
            overlaps.append(
                len(
                    {arrangement(t) for t in exact} & {arrangement(t) for t in beam}
                )
            )
        rows.append(
            {
                "label": label,
                "options": options,
                "pools": len(pools),
                "top1_identity": identity,
                "top1_score": score_equal,
                "top10_overlap_mean": statistics.fmean(overlaps),
                "gap_mean": statistics.fmean(gaps) if gaps else None,
                "gap_max": max(gaps) if gaps else None,
                "seconds": time.perf_counter() - started,
            }
        )
    return rows


def format_ablation(rows: list[dict]) -> list[str]:
    lines = [
        "",
        "ABLATION — one mechanism ADDED at a time, same pools, bare baseline",
        f"  baseline: {ABLATION_BASELINE}",
        "",
        f"{'variant':<30} {'top1 id':>9} {'top1 score':>11} {'top10':>6} "
        f"{'gap mean':>10} {'gap max':>9} {'s':>7}",
    ]
    for row in rows:
        gap_mean = "—" if row["gap_mean"] is None else f"{row['gap_mean'] * 100:.3f}%"
        gap_max = "—" if row["gap_max"] is None else f"{row['gap_max'] * 100:.3f}%"
        lines.append(
            f"{row['label']:<30} {row['top1_identity']:>4}/{row['pools']:<4} "
            f"{row['top1_score']:>5}/{row['pools']:<5} "
            f"{row['top10_overlap_mean']:>6.2f} {gap_mean:>10} {gap_max:>9} "
            f"{row['seconds']:>7.1f}"
        )
    lines.append("")
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cards", default="data/cards.json")
    parser.add_argument("--owned", default="data/owned.json")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--sizes", type=int, nargs="+", default=list(DEFAULT_SIZES))
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--beam-width", type=int, default=None)
    parser.add_argument("--branch", type=int, default=None)
    parser.add_argument("--seeds", type=int, default=None)
    parser.add_argument(
        "--open-condition-weight",
        type=float,
        default=None,
        help=(
            "scales the open-condition credit at heuristic.py's `rate += "
            "(magnitude/100) / (1 + shortfall)`. That credit is NOT an "
            "admissible bound in either direction, so what it costs is a "
            "measurement rather than an argument: run the same pools at 0.0 "
            "and at the default and compare the hit rates."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "processes for the EXACT reference (solver.parallel, bit-identical "
            f"to the serial path). Omit for 1; this machine reports "
            f"{default_workers()} logical cores. N=25..32 is impractical without it."
        ),
    )
    parser.add_argument(
        "--ablate",
        action="store_true",
        help="run the ablation ladder instead of the agreement sweep",
    )
    parser.add_argument(
        "--json",
        default=None,
        help="write the full per-trial record to this path (the only file this script creates)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # The report carries em dashes; Windows' default cp1252 console mangles
    # them, exactly as `cli.main` already guards against.
    sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    cards = load_cards(Path(args.cards))
    owned_path = Path(args.owned)
    source = load_owned(owned_path, cards) if owned_path.exists() else cards
    print(f"sampling pools from {owned_path if owned_path.exists() else args.cards} ({len(source)} cards)")
    cfg = load_scoring_config(Path(args.config))

    beam_options: dict = {}
    if args.beam_width is not None:
        beam_options["beam_width"] = args.beam_width
    if args.branch is not None:
        beam_options["branch"] = args.branch
    if args.seeds is not None:
        beam_options["seeds"] = args.seeds
    if args.open_condition_weight is not None:
        beam_options["open_condition_weight"] = args.open_condition_weight

    if args.ablate:
        rows = run_ablation(source, cfg, args.sizes, args.trials, args.seed)
        for line in format_ablation(rows):
            print(line)
        if args.json is not None:
            Path(args.json).write_text(
                json.dumps({"ablation": rows}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            print(f"wrote {args.json}")
        return 0

    rng = random.Random(args.seed)
    workers = args.workers if args.workers is not None else 1
    if workers > 1:
        print(f"exact reference via solve_exact_parallel, {workers} workers")
    summaries: list[dict] = []
    records: dict[str, list[dict]] = {}
    for size in args.sizes:
        trials = []
        for index in range(args.trials):
            pool = sample_pool(source, size, rng)
            trial = compare_one(pool, cfg, workers=workers, **beam_options)
            trial["trial"] = index
            trials.append(trial)
            print(
                f"  N={size} trial {index}: "
                f"{'MATCH' if trial['identity_match'] else 'DIFFER'} "
                f"gap={trial['gap'] * 100:+.4f}% overlap={trial['top10_overlap']}/10 "
                f"exact={trial['exact_seconds']:.2f}s beam={trial['beam_seconds']:.2f}s"
            )
        records[str(size)] = trials
        summaries.append(summarise(size, trials))

    for line in format_report(summaries):
        print(line)

    all_trials = [trial for group in records.values() for trial in group]
    for line in format_modes(all_trials):
        print(line)

    if args.json is not None:
        Path(args.json).write_text(
            json.dumps({"summaries": summaries, "trials": records}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
