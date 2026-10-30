"""The beam-versus-exact harness, pinned where it can rot silently.

`bench/bench_heuristic.py` is the artefact that decides whether the beam is
usable at all, and its own failure mode is easy to miss: a harness that still runs, still exits 0, and still prints a
reassuring-looking table while comparing the beam against something that is not
`solve_exact`, or while summarising a set of trials it did not actually run.

The tests here are structural and cheap. The measurement itself is not a test —
it is a measurement and it takes minutes. What is asserted
here is that the thing producing those numbers still compares the two solvers
over identical pools and still counts what it says it counts.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hbuilder.domain.card import Card
from hbuilder.solver.exact import solve_exact

from conftest import make_card, make_scoring_config

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCH_SCRIPT = REPO_ROOT / "bench" / "bench_heuristic.py"

# Loaded by file path, never as a package import: `bench/` is a standalone
# script directory outside `src/` and must stay unreachable from the CLI's own
# import graph, exactly as `test_bench_smoke.py` loads its own.
_spec = importlib.util.spec_from_file_location("bench_heuristic", BENCH_SCRIPT)
assert _spec is not None and _spec.loader is not None
bench_heuristic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench_heuristic)


def _source_pool(size: int = 24) -> tuple[Card, ...]:
    types = ("Cute", "Cool", "Passion")
    return tuple(
        make_card(f"c{index:02d}", type_=types[index % 3])
        for index in range(size)
    )


def test_no_module_under_src_imports_anything_from_bench():
    """A benchmark reachable from `src/` is a benchmark that can end up in the
    CLI's own dependency graph. Asserted structurally over every
    `import`/`from ... import` in the package rather than by text search: a
    docstring is free to *name* `bench/bench_heuristic.py` as the place a
    default was measured — `solver/heuristic.py` deliberately does — while an
    actual import of it must fail here."""
    import ast

    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if name.split(".")[0].startswith("bench"):
                    offenders.append(f"{path}:{node.lineno}: {name}")
    assert offenders == [], offenders


def test_sampled_pools_hold_one_card_per_holomem():
    """R1 makes a pool that repeats a holomem smaller than its card count
    suggests. If the harness sampled such pools the agreement number would be
    measuring the pool, not the search."""
    import random

    source = _source_pool(24)
    # Two ids sharing one holomem: the collision the sampler must not emit.
    source = source + (make_card("dup", member="Member c00"),)
    rng = random.Random(3)
    for _ in range(10):
        pool = bench_heuristic.sample_pool(source, 9, rng)
        assert len(pool) == 9
        assert len({card.member for card in pool}) == 9
        assert [card.id for card in pool] == sorted(card.id for card in pool)

    with pytest.raises(ValueError):
        bench_heuristic.sample_pool(source, 999, rng)


def test_compare_one_diffs_the_two_solvers_over_the_same_pool():
    """The load-bearing claim: `exact_score` is `solve_exact`'s own number, and
    a pool where the two agree reports identity, zero gap and a full overlap."""
    pool = _source_pool(9)
    cfg = make_scoring_config()
    trial = bench_heuristic.compare_one(pool, cfg)

    assert trial["exact_score"] == solve_exact(pool, cfg, top_n=1)[0].score
    assert trial["identity_match"] is True
    assert trial["score_match"] is True
    assert trial["gap"] == 0.0
    assert trial["top10_overlap"] == 10
    assert trial["arrangements_scored"] > 0
    assert trial["partials_scored"] > 0


def test_the_gap_distribution_covers_only_the_trials_that_disagreed():
    """Averaging the agreeing trials' zeros into the gap would report a
    reassuringly small mean that describes nothing — "how often is it wrong" and
    "when it is wrong, how wrong" are different questions and one number cannot
    answer both."""
    trials = [
        {"identity_match": True, "score_match": True, "gap": 0.0, "top10_overlap": 10,
         "exact_seconds": 1.0, "beam_seconds": 0.5, "arrangements_scored": 10,
         "partials_scored": 5, "modes": []},
        {"identity_match": False, "score_match": False, "gap": 0.20, "top10_overlap": 4,
         "exact_seconds": 1.0, "beam_seconds": 0.5, "arrangements_scored": 10,
         "partials_scored": 5, "modes": ["beam_truncation"]},
        {"identity_match": False, "score_match": False, "gap": 0.10, "top10_overlap": 6,
         "exact_seconds": 1.0, "beam_seconds": 0.5, "arrangements_scored": 10,
         "partials_scored": 5, "modes": []},
    ]
    summary = bench_heuristic.summarise(12, trials)

    assert summary["trials"] == 3
    assert summary["top1_identity_matches"] == 1
    assert summary["top1_score_rate"] == pytest.approx(1 / 3)
    assert summary["disagreements"] == 2
    # 0.15, not 0.10 — the agreeing trial's zero is excluded.
    assert summary["gap_mean"] == pytest.approx(0.15)
    assert summary["gap_max"] == pytest.approx(0.20)
    assert summary["top10_overlap_mean"] == pytest.approx(20 / 3)
    assert summary["modes_seen"] == ["beam_truncation"]

    # All-agree reports None, never a zero that could be read as "measured 0%".
    agreed = bench_heuristic.summarise(12, trials[:1])
    assert agreed["gap_mean"] is None and agreed["gap_max"] is None
    assert agreed["disagreements"] == 0


def test_the_ablation_ladder_adds_exactly_one_mechanism_per_row():
    """A ladder whose rows differ in two things at once cannot attribute a
    change to either.

    It is an ADD-one ladder, not a remove-one one, and that is load-bearing: a
    remove-one ladder from a `beam_width=8` baseline had every row tie at 8/8,
    because at that width the beam is already effectively exhaustive on any
    pool `solve_exact` can finish. The baseline must therefore be the BARE
    beam — every mechanism off — or the ladder measures nothing.
    """
    labels = [label for label, _ in bench_heuristic.ABLATIONS]
    assert labels[0] == "baseline (every mechanism off)"
    assert bench_heuristic.ABLATIONS[0][1] == {}

    baseline = bench_heuristic.ABLATION_BASELINE
    assert baseline["quota"] == 0
    assert baseline["feature_branch"] == 0
    assert baseline["open_condition_weight"] == 0.0
    assert baseline["polish_teams"] == 0
    assert baseline["seeds"] == 1

    # Every row between the baseline and the two summary rows turns on exactly
    # one thing, and names a key the baseline actually has.
    for label, override in bench_heuristic.ABLATIONS[1:-2]:
        assert len(override) == 1, label
        assert set(override) <= set(baseline), label
    assert labels[-2] == "all mechanisms on"
    assert labels[-1] == "shipped defaults"


@pytest.mark.slow
def test_the_harness_runs_end_to_end_and_reports_a_table():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, str(BENCH_SCRIPT), "--sizes", "9", "--trials", "2"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "heuristic vs exact" in result.stdout
    assert "top1 id" in result.stdout
