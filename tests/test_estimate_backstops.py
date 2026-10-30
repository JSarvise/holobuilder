"""Four `--estimate`/benchmark backstops.

All four sit on the path between `bench/throughput.json` and the number the
confirmation gate decides on. The projection counts two families of member
sets and the throughput file carries two cost terms, so a regression in any of
these changes who gets prompted and what they are told:

- the confirmation gate's behaviour **exactly at** `CONFIRM_THRESHOLD_SECONDS`
  (a `>` that becomes a `>=` changes who gets prompted, silently);
- the `--estimate` stale-benchmark warning firing on a `config_hash` mismatch
  end to end, not just in `project_duration`'s unit test;
- `bench/throughput.json`'s key order being stable across two runs, so the
  committed file diffs cleanly instead of churning;
- `seconds_per_eval` stored at full precision, with rounding only where a
  human reads it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from hbuilder.solver.estimate import (
    CONFIRM_THRESHOLD_SECONDS,
    THROUGHPUT_REQUIRED_KEYS,
    Throughput,
    load_throughput,
    project_duration,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
THROUGHPUT_FILE = REPO_ROOT / "bench" / "throughput.json"
BENCH_SCRIPT = REPO_ROOT / "bench" / "bench_solver.py"

# bench/ is a standalone-script directory, deliberately outside src/ and never
# on pytest's `pythonpath`, so the producer is loaded by file
# path — the same way `tests/test_bench_smoke.py` does it. Importing it costs
# nothing; only `measure()` is slow, and nothing here calls it.
_bench_spec = importlib.util.spec_from_file_location(
    "bench_solver_backstops", BENCH_SCRIPT
)
assert _bench_spec is not None and _bench_spec.loader is not None
bench_solver = importlib.util.module_from_spec(_bench_spec)
_bench_spec.loader.exec_module(bench_solver)

# The two rates in the committed file, measured with the LEADER COLLAPSE in
# place (`score_card_set` scores one leader per OUTFIT class). Pinned at FULL precision so a silent re-round, or a
# collapse back to one blended rate, has to fail a test rather than pass one.
COMMITTED_SECONDS_PER_EVAL = 1.4744207267448301e-05  # 14.74 us
COMMITTED_SECONDS_PER_SET = 0.00016242790315522957  # 162.43 us

# An earlier pre-collapse measurement, kept as a value not to drift back to. Re-measuring is
# legitimate and expected; silently drifting BACK is not, and neither is
# "fixing" a failure here by pasting whatever the file happens to contain
# without re-running `bench_solver.py`. Asserted against explicitly below.
SUPERSEDED_SECONDS_PER_EVAL = 2.4098965292438605e-05  # 24.10 us, pre-collapse
SUPERSEDED_SECONDS_PER_SET = 0.0001196969196430413  # 119.70 us, pre-collapse


# --- the confirmation gate, exactly at the threshold -------------------------


def _run_solve_with_projection(monkeypatch, seconds: float, answer: str = "n"):
    """Drive `cli.run_solve`'s confirmation decision at a chosen projected
    duration, with stdout claiming to be a terminal (the gate is
    interactivity-gated, and pytest's captured stdout is not a tty)."""
    from hbuilder import cli

    prompted: list[float] = []

    def fake_confirm(projected_seconds: float) -> bool:
        prompted.append(projected_seconds)
        return answer == "y"

    monkeypatch.setattr(cli, "confirm_or_decline", fake_confirm)
    monkeypatch.setattr(
        cli.sys.stdout, "isatty", lambda: True, raising=False
    )
    monkeypatch.setattr(
        cli,
        "project_duration",
        lambda *args, **kwargs: (seconds, None),
    )
    return prompted


def _solve_argv():
    from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH

    return [
        "solve",
        "--cards",
        str(FIXTURE_CARDS_PATH),
        "--owned",
        str(FIXTURE_OWNED_PATH),
        "--top",
        "1",
    ]


@pytest.mark.parametrize(
    "seconds,expect_prompt",
    [
        (CONFIRM_THRESHOLD_SECONDS - 0.001, False),
        (CONFIRM_THRESHOLD_SECONDS, False),
        (CONFIRM_THRESHOLD_SECONDS + 0.001, True),
    ],
    ids=["just-under", "exactly-at", "just-over"],
)
def test_the_confirmation_gate_prompts_strictly_above_the_threshold(
    monkeypatch, seconds, expect_prompt
):
    """The gate is `> CONFIRM_THRESHOLD_SECONDS`, so a run projected at
    exactly the threshold proceeds without asking. Pinned because the
    boundary is where a `>` quietly becoming a `>=` (or the reverse) hides:
    both spellings behave identically on every input except this one."""
    from hbuilder import cli

    prompted = _run_solve_with_projection(monkeypatch, seconds, answer="y")
    exit_code = cli.main(_solve_argv())

    assert exit_code == 0
    assert bool(prompted) is expect_prompt, (
        f"projected {seconds}s against a {CONFIRM_THRESHOLD_SECONDS}s threshold: "
        f"{'expected' if expect_prompt else 'did not expect'} a confirmation prompt"
    )


def test_declining_the_confirmation_aborts_before_enumerating(monkeypatch, capsys):
    from hbuilder import cli

    _run_solve_with_projection(
        monkeypatch, CONFIRM_THRESHOLD_SECONDS + 1.0, answer="n"
    )
    exit_code = cli.main(_solve_argv())

    assert exit_code == cli.EXIT_DECLINED
    assert "declined" in capsys.readouterr().err


# --- the stale-benchmark warning, end to end ---------------------------------


def test_estimate_warns_on_stderr_when_the_benchmark_predates_the_config(
    monkeypatch, capsys
):
    """`project_duration`'s unit test proves the warning STRING; this proves
    the CLI actually prints it, on stderr, beside the duration it qualifies —
    a warning computed and dropped would leave `--estimate` reporting a stale
    per-eval cost with full confidence."""
    from hbuilder import cli
    from hbuilder.solver.estimate import Throughput

    stale = Throughput(
        seconds_per_eval=1e-05,
        seconds_per_set=1e-04,
        config_hash="a-hash-from-before-the-model-changed",
        schema_version="1",
        measured_at="2026-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(cli, "load_throughput", lambda *a, **k: stale)

    exit_code = cli.main(_solve_argv() + ["--estimate"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "PROJECTED DURATION" in captured.err
    assert "WARNING" in captured.err
    assert "a-hash-from-before-the-model-changed" in captured.err
    assert captured.out == "", "--estimate answers on stderr and ranks nothing"


# --- the committed throughput file -------------------------------------------


def test_the_committed_throughput_file_satisfies_the_declared_contract():
    """Not a tautology: `THROUGHPUT_REQUIRED_KEYS` includes `seconds_per_set`,
    and a committed file without it raises rather than projecting from a model
    it no longer describes. This catches the case
    where the schema moved and the file was not re-measured."""
    throughput = load_throughput(THROUGHPUT_FILE)

    assert throughput is not None, f"{THROUGHPUT_FILE} is missing"
    assert throughput.seconds_per_eval > 0.0
    assert throughput.seconds_per_set >= 0.0


def test_the_throughput_file_key_order_is_stable_so_it_diffs_cleanly():
    """`write_throughput` serialises with `sort_keys=True`, so re-encoding the
    committed document must reproduce its own bytes. Two runs then differ only
    in the values that were actually measured — a key-order churn in a
    committed file is noise that hides the one line that matters."""
    raw = THROUGHPUT_FILE.read_text(encoding="utf-8")
    doc = json.loads(raw)

    assert raw == json.dumps(doc, sort_keys=True) + "\n"
    assert list(doc) == sorted(THROUGHPUT_REQUIRED_KEYS)


def test_both_measured_costs_are_stored_at_full_precision_never_rounded():
    """Precision: `json.dumps` encodes a float byte-identically to its
    `repr()`, so the repr must appear verbatim in the file. Rounding belongs
    only where a duration is rendered for a human — a value rounded on the way
    to disk cannot be un-rounded by the projection that reads it."""
    raw = THROUGHPUT_FILE.read_text(encoding="utf-8")
    doc = json.loads(raw)

    for key in ("seconds_per_eval", "seconds_per_set"):
        value = doc[key]
        assert isinstance(value, float)
        assert repr(value) in raw, f"{key} was rounded on the way to disk"
        assert json.loads(json.dumps(value)) == value


def test_the_two_measured_costs_are_not_secretly_the_same_number():
    """If the benchmark ever collapses back to one blended per-arrangement
    rate, the honest thing is a loud failure here rather than a projection
    that silently under-prices every leader-pinned run."""
    doc = json.loads(THROUGHPUT_FILE.read_text(encoding="utf-8"))

    assert doc["seconds_per_set"] != doc["seconds_per_eval"]


def test_the_committed_rates_are_still_the_two_measured_2026_08_03(tmp_path):
    """Pins the ARTEFACT, not a counter: the exact bytes on disk. The point of
    the pair is that they DIFFER; one blended rate standing in for both can make
    a 178 s run print 0.04 s, so the pre-collapse pair is asserted against too.

    The LEADER COLLAPSE moves the two rates in OPPOSITE directions — which is the
    whole reason two rates are kept rather than one. Per eval is **1.63x cheaper**
    (24.10 -> 14.74 us) because the inner loop does not rescore a leader per
    outfit class; per set is **1.36x dearer** (119.70 -> 162.43 us) because that
    work moves into a per-set prefix the bench measures at ~11 leader-evals. A
    single blended rate cannot express that, and the ratio below is the shape of
    it: 4.97 pre-collapse, 11.02 now.

    These rates set `HEURISTIC_THRESHOLD_POOL_SIZE` (37), the pool size above
    which the exact search hands over to the beam."""
    doc = json.loads(THROUGHPUT_FILE.read_text(encoding="utf-8"))

    assert doc["seconds_per_eval"] == COMMITTED_SECONDS_PER_EVAL
    assert doc["seconds_per_set"] == COMMITTED_SECONDS_PER_SET
    # Not the pre-collapse rates. Re-measuring is expected; drifting back, or
    # a stale file surviving a scoring-model change, is not.
    assert doc["seconds_per_eval"] != SUPERSEDED_SECONDS_PER_EVAL
    assert doc["seconds_per_set"] != SUPERSEDED_SECONDS_PER_SET
    assert doc["seconds_per_set"] / doc["seconds_per_eval"] == pytest.approx(
        11.016, abs=0.001
    )
    # Displayed in microseconds these are the two figures the docs quote.
    assert round(doc["seconds_per_eval"] * 1e6, 2) == 14.74
    assert round(doc["seconds_per_set"] * 1e6, 2) == 162.43


# --- two runs of the producer, not one reading of its output -----------------


def test_two_producer_runs_write_the_same_key_order_with_different_values(tmp_path):
    """The committed-file test above inspects ONE artefact and can only prove
    that file happens to be sorted. This drives the producer twice and compares
    the two artefacts it actually wrote — the check the backstop names.

    Both runs must lay the keys down in the same order even though every
    measured value differs, so a real re-benchmark shows up in a diff as the
    values that moved and nothing else. `measured_at` is the one field expected
    to differ between two runs of identical inputs, so it is excluded from the
    same-input comparison rather than asserted stable."""
    cfg_document = {"probability_tiers": {"Low": 0.35, "High": 0.75}}
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    bench_solver.write_throughput(1.5e-05, 9.75e-05, cfg_document, first)
    bench_solver.write_throughput(9.125e-05, 2.5e-04, cfg_document, second)

    raw_first = first.read_text(encoding="utf-8")
    raw_second = second.read_text(encoding="utf-8")
    doc_first = json.loads(raw_first)
    doc_second = json.loads(raw_second)

    assert list(doc_first) == list(doc_second) == sorted(THROUGHPUT_REQUIRED_KEYS)
    assert raw_first != raw_second, "two different measurements wrote one file"
    assert doc_first["seconds_per_eval"] != doc_second["seconds_per_eval"]
    assert doc_first["seconds_per_set"] != doc_second["seconds_per_set"]
    # Both are re-serialisable to their own bytes, so the order is a property
    # of the writer and not of these particular values.
    assert raw_first == json.dumps(doc_first, sort_keys=True) + "\n"
    assert raw_second == json.dumps(doc_second, sort_keys=True) + "\n"


def test_two_producer_runs_on_identical_inputs_differ_only_in_measured_at(tmp_path):
    """Re-running the benchmark without changing anything measurable must not
    churn the committed file. Anything beyond the timestamp moving would mean
    the writer is non-deterministic, and a noisy diff is how the one line that
    matters gets missed."""
    cfg_document = {"probability_tiers": {"Low": 0.35, "High": 0.75}}
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"

    bench_solver.write_throughput(1.5e-05, 9.75e-05, cfg_document, first)
    bench_solver.write_throughput(1.5e-05, 9.75e-05, cfg_document, second)

    doc_first = json.loads(first.read_text(encoding="utf-8"))
    doc_second = json.loads(second.read_text(encoding="utf-8"))

    del doc_first["measured_at"], doc_second["measured_at"]
    assert doc_first == doc_second
    assert doc_first["config_hash"] == doc_second["config_hash"]


# --- full precision stored, rounding only where a human reads ----------------


def test_load_throughput_returns_the_stored_float_bit_for_bit(tmp_path):
    """The storage half of the backstop is already pinned above; this is the
    read half. A projection built from a rounded-on-read rate is wrong by the
    same mechanism as one rounded on write, and nothing else in the suite
    proves `load_throughput` hands back the exact float it parsed."""
    throughput = load_throughput(THROUGHPUT_FILE)

    assert throughput is not None
    assert throughput.seconds_per_eval == COMMITTED_SECONDS_PER_EVAL
    assert throughput.seconds_per_set == COMMITTED_SECONDS_PER_SET
    # Not merely close: the 2-decimal microsecond forms a human sees are NOT
    # what is stored, and must not become what is stored.
    assert throughput.seconds_per_eval != 25.96e-06
    assert throughput.seconds_per_set != 111.29e-06


def test_the_projection_is_full_precision_and_only_the_printed_line_is_rounded(
    monkeypatch, capsys
):
    """"Rounding only at display" is two claims. First: `project_duration`
    returns the unrounded product, so a caller that wants the exact number can
    have it. Second: the CLI's `PROJECTED DURATION` line is the only place a
    round happens — `{:,.2f}` — and the full-precision value must not leak into
    it, nor the rounded one back into the projection."""
    from hbuilder import cli

    throughput = Throughput(
        seconds_per_eval=COMMITTED_SECONDS_PER_EVAL,
        seconds_per_set=COMMITTED_SECONDS_PER_SET,
        config_hash="matching",
        schema_version="2",
        measured_at="2026-08-03T09:34:03.091296+00:00",
    )

    # A 91-card unconstrained pool: C(91,5) sets and one arrangement per leader
    # per set. Held FIXED as this test's reference shape rather than tracking a
    # real collection — the claim under test is the arithmetic and the rounding,
    # and a moving input would make every earlier figure below unfalsifiable.
    # (`bench_solver.py` prints the live projection.)
    set_count, arrangement_count = 46_504_458, 4_231_905_678
    seconds, warning = project_duration(
        arrangement_count, throughput, "matching", set_count=set_count
    )

    assert warning is None
    # The exact IEEE-754 result of the two-term sum, not a rounded stand-in.
    expected = (
        set_count * COMMITTED_SECONDS_PER_SET
        + arrangement_count * COMMITTED_SECONDS_PER_EVAL
    )
    assert seconds == expected
    assert seconds != round(seconds, 2)
    # 69,950 s = 19.43 h, the figure the current rates give and the one the
    # confirmation gate decides on. Earlier scoring models gave 31.96 h, 41.45 h,
    # 28.80 h and 29.88 h on this shape; each is asserted against below, because
    # drifting BACK to one is what a stale side-car file looks like.
    assert seconds / 3600 == pytest.approx(19.43, abs=0.01)
    assert round(seconds) == 69_950
    assert seconds / 3600 != pytest.approx(31.96, abs=0.01)  # superseded run
    assert seconds / 3600 != pytest.approx(41.45, abs=0.01)  # superseded run
    assert seconds / 3600 != pytest.approx(28.80, abs=0.01)  # superseded, boardless
    assert seconds / 3600 != pytest.approx(29.88, abs=0.01)  # superseded, pre-collapse
    # A single-rate model — pricing only the arrangements, with no
    # per-set term at all — is 17.33 h here, and asserting it does NOT come
    # back is the whole point of storing two rates rather than one blended one.
    #
    # ⚠ The collapse WIDENS that gap, the opposite of what a cheaper eval
    # suggests: the per-set term is 10.8 % of the total (5.3 % pre-collapse),
    # because the collapse moves work out of the arrangements and into the set
    # prefix. A blended single rate gets less honest, not more.
    arrangements_only = arrangement_count * COMMITTED_SECONDS_PER_EVAL
    assert seconds > arrangements_only
    assert arrangements_only / 3600 == pytest.approx(17.33, abs=0.01)

    # Now the display half, through the real CLI printer.
    monkeypatch.setattr(cli, "project_duration", lambda *a, **k: (seconds, None))
    exit_code = cli.main(_solve_argv() + ["--estimate"])
    err = capsys.readouterr().err

    assert exit_code == 0
    assert f"PROJECTED DURATION: {seconds:,.2f}s" in err
    assert repr(seconds) not in err, "the display is supposed to round"
    assert f"{seconds:,.2f}" != str(seconds)
