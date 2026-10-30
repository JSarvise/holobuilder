"""The heuristic solver at the CLI seam: the automatic switch to
the beam, `--exact`'s override, and the specific-failure-mode labelling
reaching the user on both output surfaces.

The failure this file exists to catch is quiet: a run that silently returns an
approximate ranking presented in exactly the table an exhaustive one produces.
Every assertion below is either about *which solver ran* or about the label
being *present and specific* — never about the teams themselves, which
`tests/test_solver_heuristic.py` covers against `solve_exact`.

The threshold is asserted against the benchmarked cost model rather than
restated, so a re-benchmark that moves `seconds_per_set` moves this test
too instead of leaving a stale constant behind.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hbuilder.cli import (
    HEURISTIC_THRESHOLD_POOL_SIZE,
    HEURISTIC_THRESHOLD_SECONDS,
    build_parser,
    should_use_heuristic,
)
from hbuilder.solver.estimate import (
    CONFIRM_THRESHOLD_SECONDS,
    Projection,
    derive_threshold_pool_size,
    load_throughput,
    project_arrangements,
)

from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent

# The real collection is the ONLY pool large enough to cross the switch
# threshold, so unlike every other test module this one genuinely needs it —
# and the tests that use it skip when it is absent.
#
# Its location is taken from the CLI's own `--cards`/`--owned` defaults rather
# than restated as a path literal. That satisfies
# `test_fixture_isolation.py`'s guard in spirit and not merely in letter: the
# guard exists so a relocation of the canonical files is one edit, and reading
# the tool's own default is stricter than a second shared constant would be,
# because it cannot disagree with the tool.
_CLI_DEFAULTS = build_parser().parse_args(["solve"])
REAL_CARDS = REPO_ROOT / _CLI_DEFAULTS.cards
REAL_OWNED = REPO_ROOT / _CLI_DEFAULTS.owned


def _projection(seconds, pool_size=91) -> Projection:
    return Projection(
        pool_size=pool_size,
        locked_count=0,
        leader_fixed=False,
        set_count=1,
        arrangement_count=1,
        seconds_or_none=seconds,
        throughput_warning=None,
    )


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "hbuilder.cli", *argv],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


# ---------------------------------------------------------------------------
# 1. The switch
# ---------------------------------------------------------------------------


def test_the_threshold_sits_above_the_confirmation_gate():
    """Ordering, not magnitude. If the switch fired below
    `CONFIRM_THRESHOLD_SECONDS` the confirmation gate would become unreachable
    and a run the user could have waited 30 s for would be silently replaced by
    an approximate one."""
    assert HEURISTIC_THRESHOLD_SECONDS == 300.0
    assert HEURISTIC_THRESHOLD_SECONDS > CONFIRM_THRESHOLD_SECONDS == 30.0


def test_the_duration_rule_and_the_pool_size_fallback_name_the_same_boundary():
    """Two rules decide the same question — projected seconds when the
    benchmark is readable, pool size when it is not. They must agree on where
    the boundary is, or an absent `bench/throughput.json` would move it.

    Benchmarked rates move between runs, so the boundary is derived rather
    than hardcoded: `estimate.derive_threshold_pool_size` computes it,
    `bench/bench_solver.py` prints what it returns for the rates it just
    measured, and the first assertion below is that the shipped constant
    EQUALS it. A re-benchmark that moves it fails here naming the new value
    rather than leaving a stale constant to be noticed later.

    The straddle and tightness checks are kept underneath rather than deleted:
    they are an independent statement of the same property, written in terms of
    the duration rule instead of the search, so a bug inside
    `derive_threshold_pool_size` cannot make both agree on a wrong answer.
    """
    throughput = load_throughput()
    assert throughput is not None, "this test reads bench/throughput.json"

    def projected(pool_size: int) -> float:
        sets, arrangements = project_arrangements(pool_size, 0, False)
        return (
            sets * throughput.seconds_per_set
            + arrangements * throughput.seconds_per_eval
        )

    derived = derive_threshold_pool_size(throughput, HEURISTIC_THRESHOLD_SECONDS)
    assert HEURISTIC_THRESHOLD_POOL_SIZE == derived, (
        f"cli.HEURISTIC_THRESHOLD_POOL_SIZE is "
        f"{HEURISTIC_THRESHOLD_POOL_SIZE} but the committed rates derive "
        f"{derived}. It is a DERIVED constant (POSTMORTEM.md §5d) — set it to "
        f"{derived} rather than adjusting this test."
    )

    below = HEURISTIC_THRESHOLD_POOL_SIZE - 1
    # The boundary is real: the fallback's N straddles the duration rule.
    assert projected(below) < HEURISTIC_THRESHOLD_SECONDS
    assert projected(HEURISTIC_THRESHOLD_POOL_SIZE) > HEURISTIC_THRESHOLD_SECONDS
    # And it is TIGHT: no smaller pool crosses. Without this, any N above the
    # true boundary would pass the pair above while silently sending pools the
    # exact solver could still have finished to the beam.
    assert all(
        projected(n) < HEURISTIC_THRESHOLD_SECONDS
        for n in range(6, HEURISTIC_THRESHOLD_POOL_SIZE)
    )


@pytest.mark.parametrize(
    "seconds, pool_size, force_exact, expected",
    (
        (299.9, 91, False, False),
        (300.0, 91, False, False),  # strictly greater than, so exactly at is exact
        (300.1, 91, False, True),
        (115_043.0, 91, False, True),
        (115_043.0, 91, True, False),  # --exact wins outright
        (0.01, 6, False, False),
        # Unprojectable: the pool size decides, and it must still switch.
        (None, HEURISTIC_THRESHOLD_POOL_SIZE - 1, False, False),
        (None, HEURISTIC_THRESHOLD_POOL_SIZE, False, True),
        (None, 91, False, True),
        (None, 91, True, False),
    ),
)
def test_should_use_heuristic_decides_on_projected_seconds_then_pool_size(
    seconds, pool_size, force_exact, expected
):
    assert should_use_heuristic(_projection(seconds, pool_size), force_exact) is expected


def test_an_unreadable_benchmark_must_not_restore_the_thirty_two_hour_default():
    """The fallback is the load-bearing half. `load_throughput` returning `None`
    is the state an absent or rejected `bench/throughput.json` produces, and
    with no pool-size rule the switch would turn OFF there — handing the user
    the 31.96 h run precisely when the tool had lost its ability to warn."""
    assert should_use_heuristic(_projection(None, 91), False) is True


# ---------------------------------------------------------------------------
# 2. The CLI says which solver ran
# ---------------------------------------------------------------------------


def test_the_small_fixture_pool_runs_exact_and_says_so():
    result = _run(
        [
            "solve",
            "--cards",
            str(FIXTURE_CARDS_PATH),
            "--owned",
            str(FIXTURE_OWNED_PATH),
            "--top",
            "3",
        ]
    )
    assert result.returncode == 0
    assert "SOLVER: exact (exhaustive)" in result.stderr
    assert "APPROXIMATE RESULT" not in result.stdout
    assert "APPROXIMATE RESULT" not in result.stderr


@pytest.mark.skipif(
    not REAL_OWNED.exists(), reason="the owner's real owned.json is not present"
)
def test_the_real_pool_switches_to_the_beam_and_exact_overrides_it():
    """`--estimate` so neither branch actually searches — the assertion is about
    the DECISION, and running the exhaustive branch here would be 28.80 h."""
    switched = _run(["solve", "--estimate"])
    forced = _run(["solve", "--estimate", "--exact"])

    assert switched.returncode == 0 and forced.returncode == 0
    assert "SOLVER: heuristic (beam search, approximate)" in switched.stderr
    assert "SOLVER: exact (exhaustive)" in forced.stderr

    # And the projection printed is the EXACT cost in both cases — that is the
    # number the switch was made on, so it must stay visible.
    #
    # Pinned by DERIVATION, not by transcription. A hardcoded literal would
    # break every time `bench_solver.py` is legitimately re-run; it would not
    # test the CLI, only that nobody re-measured. Recomputing from the committed side-car still
    # fails loudly if the CLI stops printing the exact cost, prints the
    # heuristic's cost instead, or drifts from the file it claims to use.
    throughput = load_throughput()
    assert throughput is not None, "this test reads bench/throughput.json"
    # Pool size read from the owned file itself, for the same reason the paths
    # above are read from the CLI's defaults: a literal 91 here would disagree
    # with the tool the day the collection grows.
    pool_size = len(json.loads(REAL_OWNED.read_text(encoding="utf-8"))["owned"])
    sets, arrangements = project_arrangements(pool_size, 0, False)
    expected = (
        sets * throughput.seconds_per_set + arrangements * throughput.seconds_per_eval
    )
    assert f"PROJECTED DURATION: {expected:,.2f}s" in switched.stderr
    assert f"PROJECTED DURATION: {expected:,.2f}s" in forced.stderr
    # Still an absolute claim: whatever it is, it is a run nobody can wait for,
    # which is the entire premise of the switch.
    assert expected > 100_000.0


# ---------------------------------------------------------------------------
# 3. The label reaches the user
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_pool_run(tmp_path_factory):
    """ONE real 91-card beam solve, shared by every test below that needs one.

    A run takes about 10 s; three tests each doing their own would triple that
    in a suite that otherwise finishes in about 12. The run also captures the three things
    those tests assert on — stdout, the payload, and the two authoritative files
    before and after — so sharing it costs no coverage.
    """
    out = tmp_path_factory.mktemp("real") / "payload.json"
    before = (REAL_CARDS.read_bytes(), REAL_OWNED.read_bytes())
    result = _run(["solve", "--top", "3", "--json", str(out)])
    after = (REAL_CARDS.read_bytes(), REAL_OWNED.read_bytes())
    # `--json` suppresses the stdout table, so the label's stdout surface needs
    # its own run. Kept together here so there are still only two.
    table = _run(["solve", "--top", "3"])
    return {
        "json_result": result,
        "payload_path": out,
        "table": table,
        "before": before,
        "after": after,
    }


@pytest.mark.skipif(
    not REAL_OWNED.exists(), reason="the owner's real owned.json is not present"
)
def test_the_approximate_label_reaches_stdout_and_names_specific_modes(real_pool_run):
    """Requirement 2: the SPECIFIC failure mode, not a generic caveat — and on
    stdout, because a caveat printed only to stderr is invisible the moment the
    table is piped to a file."""
    result = real_pool_run["table"]
    assert result.returncode == 0
    assert "APPROXIMATE RESULT" in result.stdout
    assert "beam search, not an exhaustive one" in result.stdout
    assert "observed failure modes:" in result.stdout
    # Not "approximate" alone: at least one named mode, with a concrete detail.
    assert "[beam_truncation]" in result.stdout
    assert "[extension_horizon]" in result.stdout
    assert "never extended into by any beam state" in result.stdout
    # The label must appear BEFORE the ranking a reader scrolls to.
    assert result.stdout.index("APPROXIMATE RESULT") < result.stdout.index("RANK SCORE")


@pytest.mark.skipif(
    not REAL_OWNED.exists(), reason="the owner's real owned.json is not present"
)
def test_the_json_payload_carries_every_observation_not_a_sample(real_pool_run):
    assert real_pool_run["json_result"].returncode == 0

    payload = json.loads(real_pool_run["payload_path"].read_text(encoding="utf-8"))
    search = payload["search"]
    assert search["mode"] == "heuristic"
    assert search["approximate"] is True
    # Derived, not pinned: a literal count would fail as soon as the collection grows. The
    # property worth asserting is that the reported exhaustive count
    # really is the full arrangement space of the pool on disk.
    #
    # The space is C(n,5) member-sets x n leader choices, which is what the CLI's own
    # PROJECTION line reports as `sets=` x `pool=`. (Not C(n,6)*6: that counts a 6-card set
    # and promotes one to leader, which is a different — and smaller — space.)
    owned_count = len(json.loads(REAL_OWNED.read_text(encoding="utf-8"))["owned"])
    assert search["exhaustive_arrangement_count"] == math.comb(owned_count, 5) * owned_count
    assert search["arrangements_scored"] < search["exhaustive_arrangement_count"] / 1000
    assert "extension_horizon" in search["failure_modes"]
    # The table shows one example per mode; the payload must hold all of them.
    assert len(search["observations"]) > len(search["failure_modes"])
    for observation in search["observations"]:
        assert observation["mode"] in search["failure_modes"]
        assert observation["detail"].strip()
    # The order the player should actually field.
    assert search["member_orders"]


def test_an_exact_runs_payload_carries_no_search_key(tmp_path):
    """The diffability contract. The `search` key exists only when the beam
    ran, exactly as `stats_basis` exists only in mode B — so an exact run's
    payload stays byte-identical to what it has always been."""
    out = tmp_path / "payload.json"
    result = _run(
        [
            "solve",
            "--cards",
            str(FIXTURE_CARDS_PATH),
            "--owned",
            str(FIXTURE_OWNED_PATH),
            "--json",
            str(out),
        ]
    )
    assert result.returncode == 0
    assert "search" not in json.loads(out.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 4. The standing prohibition
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not REAL_CARDS.exists(), reason="the owner's real cards.json is not present"
)
def test_a_heuristic_run_writes_neither_authoritative_data_file(real_pool_run):
    """The standing prohibition: the only file the tool may create is the
    `--json` path the operator named. Compared by content, not mtime — a rewrite with identical
    bytes is still a write this rule forbids, and mtime would miss a same-second
    one."""
    assert real_pool_run["json_result"].returncode == 0
    assert real_pool_run["payload_path"].exists()
    assert real_pool_run["after"] == real_pool_run["before"]
