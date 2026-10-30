"""Pins the producer/consumer contract between
`bench/bench_solver.py` and `hbuilder.solver.estimate`.

Every test in this file is `@pytest.mark.slow` — deselected by default
(`addopts = "-m 'not slow'"`) so the default
suite stays in seconds while a marked run still catches rot.

The defect this file pins is *not* "the benchmark crashed" — that is loud
and obvious on its own. It is the quieter failure: the benchmark still
runs, still exits 0, and its output still parses as JSON, while its
output no longer carries the field `--estimate` actually reads.
Asserting the exit status alone would sail straight past that. Every
schema assertion here is **set equality** against
`estimate.THROUGHPUT_REQUIRED_KEYS` — imported, never a second
hand-copied key list — so both a dropped key and an unannounced extra
key fail loudly, and a rename on either side of the contract breaks this
file rather than silently drifting.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hbuilder.domain.loader import load_cards
from hbuilder.solver.estimate import (
    THROUGHPUT_REQUIRED_KEYS,
    Throughput,
    load_throughput,
    project_duration,
)

from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent
GOOD_CARDS = FIXTURE_CARDS_PATH
GOOD_OWNED = FIXTURE_OWNED_PATH
BENCH_SCRIPT = REPO_ROOT / "bench" / "bench_solver.py"

# bench/ is a standalone-script directory, deliberately outside src/ and
# never on pytest's own `pythonpath` ini setting — so it is
# loaded here via its file path rather than a package import, without
# mutating sys.path for the rest of the test session.
_spec = importlib.util.spec_from_file_location("bench_solver", BENCH_SCRIPT)
assert _spec is not None and _spec.loader is not None
bench_solver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench_solver)


def _run_bench(out_path: Path, *, pool_size: int = 12) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            str(BENCH_SCRIPT),
            "--pool-size",
            str(pool_size),
            "--out",
            str(out_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


@pytest.mark.slow
def test_bench_writes_a_file_whose_schema_matches_what_estimate_reads(tmp_path):
    out_path = tmp_path / "throughput.json"

    result = _run_bench(out_path)

    assert result.returncode == 0, result.stderr
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    # Set equality, not a subset check — a dropped key AND an unannounced
    # extra key must both fail here.
    assert set(doc) == set(THROUGHPUT_REQUIRED_KEYS), (
        f"bench_solver's output keys {set(doc)!r} must exactly match "
        f"estimate.THROUGHPUT_REQUIRED_KEYS {set(THROUGHPUT_REQUIRED_KEYS)!r}"
    )

    # Closes the loop: the producer's output is proven readable by the
    # actual consumer, not by a second hand-written parser in this test.
    throughput = load_throughput(out_path)
    assert isinstance(throughput, Throughput)


@pytest.mark.slow
def test_the_measured_per_eval_cost_is_a_positive_finite_float_at_full_precision(tmp_path):
    out_path = tmp_path / "throughput.json"

    result = _run_bench(out_path)

    assert result.returncode == 0, result.stderr
    raw_text = out_path.read_text(encoding="utf-8")
    doc = json.loads(raw_text)

    seconds_per_eval = doc["seconds_per_eval"]
    assert isinstance(seconds_per_eval, float)
    assert seconds_per_eval > 0.0
    assert math.isfinite(seconds_per_eval)

    # Precision: the same round-trip-exact JSON encoding the
    # --json payload relies on — re-encoding must reproduce the
    # exact float, and its repr() must appear verbatim in the file bytes,
    # proving no rounding happened anywhere on the way to disk.
    assert json.loads(json.dumps(seconds_per_eval)) == seconds_per_eval
    assert repr(seconds_per_eval) in raw_text


@pytest.mark.slow
def test_two_full_bench_runs_agree_on_key_order_and_disagree_only_on_measurements(
    tmp_path,
):
    """`tests/test_estimate_backstops.py` pins this at the `write_throughput`
    level, where the values are chosen. This runs the whole script twice, so
    the measured values are whatever the machine produced — the case the
    backstop is actually about.

    Key ORDER must be identical across the two artefacts (a committed file that
    churns its key order hides the one value that moved), while `measured_at`
    must differ (two runs at two times) and `config_hash`/`schema_version` must
    not (nothing about the model changed between them)."""
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    assert _run_bench(first).returncode == 0
    assert _run_bench(second).returncode == 0

    raw_first = first.read_text(encoding="utf-8")
    raw_second = second.read_text(encoding="utf-8")
    doc_first = json.loads(raw_first)
    doc_second = json.loads(raw_second)

    assert list(doc_first) == list(doc_second) == sorted(THROUGHPUT_REQUIRED_KEYS)
    assert raw_first == json.dumps(doc_first, sort_keys=True) + "\n"
    assert raw_second == json.dumps(doc_second, sort_keys=True) + "\n"

    assert doc_first["config_hash"] == doc_second["config_hash"]
    assert doc_first["schema_version"] == doc_second["schema_version"]
    assert doc_first["measured_at"] != doc_second["measured_at"], (
        "two runs stamped the same instant — measured_at is not being "
        "re-read, so a stale file could never be told apart from a fresh one"
    )

    # Timing noise is expected in the values; a *structural* collapse is not.
    for doc in (doc_first, doc_second):
        assert doc["seconds_per_eval"] > 0.0
        assert doc["seconds_per_set"] > 0.0
        assert doc["seconds_per_set"] != doc["seconds_per_eval"]


@pytest.mark.slow
def test_provenance_is_recorded_so_a_stale_measurement_can_be_detected(tmp_path):
    out_path = tmp_path / "throughput.json"

    result = _run_bench(out_path)

    assert result.returncode == 0, result.stderr
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert "config_hash" in doc
    assert "schema_version" in doc
    assert "measured_at" in doc

    throughput = load_throughput(out_path)

    matching_seconds, matching_warning = project_duration(
        1000, throughput, throughput.config_hash, set_count=100
    )
    assert matching_seconds is not None
    assert matching_warning is None

    stale_seconds, stale_warning = project_duration(
        1000, throughput, "deliberately-different-config-hash", set_count=100
    )
    assert stale_seconds is not None
    assert stale_warning is not None
    assert "scoring model" in stale_warning


@pytest.mark.slow
def test_a_pool_smaller_than_six_is_refused_rather_than_measured():
    real_cards = load_cards(GOOD_CARDS)

    with pytest.raises(bench_solver.SyntheticPoolError, match="6"):
        bench_solver.build_synthetic_pool(real_cards, 5)


@pytest.mark.slow
def test_synthetic_pool_generation_is_reproducible_across_runs():
    real_cards = load_cards(GOOD_CARDS)

    pool_a = bench_solver.build_synthetic_pool(real_cards, 16)
    pool_b = bench_solver.build_synthetic_pool(real_cards, 16)
    assert pool_a == pool_b, (
        "two build_synthetic_pool calls at the same size, seeded from the "
        "same SYNTHETIC_SEED, must build an identical pool"
    )

    pool_c = bench_solver.build_synthetic_pool(real_cards, 20)
    assert pool_a != pool_c, (
        "a different requested size must actually produce a different "
        "pool — reproducibility must not be accidentally the trivial "
        "same-input-same-output case"
    )


@pytest.mark.slow
def test_estimate_degrades_named_when_no_throughput_file_exists(tmp_path):
    # cwd=tmp_path contains no bench/ directory at all, so
    # THROUGHPUT_PATH's relative "bench/throughput.json" cannot resolve —
    # the normal pre-benchmark state.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hbuilder.cli",
            "solve",
            "--cards",
            str(GOOD_CARDS),
            "--owned",
            str(GOOD_OWNED),
            "--estimate",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "arrangements=" in result.stderr
    assert "unknown" in result.stderr.lower()
    assert "bench_solver.py" in result.stderr
    assert result.stdout == ""


@pytest.mark.slow
def test_a_zero_length_measurement_is_refused_rather_than_divided_by():
    # measure()'s error path, exercised directly with a synthetic zero
    # interval — a machine fast enough to complete a small pool inside the
    # timer's resolution must report a broken measurement, never an
    # infinite throughput.
    with pytest.raises(bench_solver.MeasurementError, match="0.0s"):
        bench_solver.validate_elapsed_seconds(0.0, 12)
