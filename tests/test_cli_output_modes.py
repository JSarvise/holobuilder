"""Pins determinism,
`--explain`/`--json` orthogonality, per-run assumption reporting on every
output surface, and the never-prompt-when-piped guarantee — all driven
against the real `hbuilder` CLI as a subprocess, matching
`tests/test_cli_input_errors.py`'s harness shape (copied env with
`PYTHONPATH`, `capture_output=True`, `encoding="utf-8"`) rather than a
second hand-rolled runner.

The `--acquire` loop calls `solve` roughly 48 times without a human
watching; the non-interactive tests here are what proves that loop can never
hang on a confirmation prompt.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from hbuilder.domain.schema import SCHEMA_VERSION
from hbuilder.solver.exact import DEFAULT_TOP_N
from hbuilder.domain.scoring_config import DEFAULT_CONFIG

from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent
GOOD_CARDS = FIXTURE_CARDS_PATH
GOOD_OWNED = FIXTURE_OWNED_PATH

_ASSUMPTION_LINE_PREFIXES = (
    "PROBABILITY_TIERS:",
    "FAMILY_WEIGHTS:",
    "SPECIAL.EXPECTED_FIRES_PER_RUN:",
    "MODEL.STACKING:",
    "MODEL.ACTIVATION_RATE:",
    "ASSUMED_RUN_STATE:",
)


def _run(cwd: Path, argv: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "hbuilder.cli", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _solve(
    cwd: Path, *extra: str, cards: Path = GOOD_CARDS, owned: Path = GOOD_OWNED
) -> subprocess.CompletedProcess:
    return _run(cwd, ["solve", "--cards", str(cards), "--owned", str(owned), *extra])


def _team_row_lines(stdout: str) -> list[str]:
    """Every table row (never the header, the assumptions lines, or the
    footer) — the shipped table's row lines are the only lines that start
    with an ASCII digit (the rank)."""
    return [line for line in stdout.splitlines() if line and line[0].isdigit()]


def _assumptions_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith(_ASSUMPTION_LINE_PREFIXES)]


# --- byte-identical rerun ----------------------------------------------------


def test_byte_identical_json_rerun_over_identical_inputs(tmp_path):
    out_a = tmp_path / "a.json"
    out_b = tmp_path / "b.json"

    result_a = _solve(tmp_path, "--json", str(out_a))
    result_b = _solve(tmp_path, "--json", str(out_b))

    assert result_a.returncode == 0, result_a.stderr
    assert result_b.returncode == 0, result_b.stderr
    assert out_a.read_bytes() == out_b.read_bytes()


def test_byte_identical_plain_table_rerun_over_identical_inputs(tmp_path):
    result_a = _solve(tmp_path)
    result_b = _solve(tmp_path)

    assert result_a.returncode == 0, result_a.stderr
    assert result_b.returncode == 0, result_b.stderr
    assert result_a.stdout.encode("utf-8") == result_b.stdout.encode("utf-8")


# --- top-N boundary ----------------------------------------------------------


def test_top_1_prints_exactly_one_team_row(tmp_path):
    result = _solve(tmp_path, "--top", "1")

    assert result.returncode == 0, result.stderr
    assert len(_team_row_lines(result.stdout)) == 1


def test_top_100_against_the_shipped_pool_prints_every_arrangement_no_filler(tmp_path):
    """The shipped six-card pool yields C(6,5) member sets x 6 leader choices
    = 36 arrangements, not 6. Asking for 100 must print
    all 36 and pad with nothing."""
    result = _solve(tmp_path, "--top", "100")

    assert result.returncode == 0, result.stderr
    assert len(_team_row_lines(result.stdout)) == 36


def test_top_0_is_an_argparse_usage_error(tmp_path):
    result = _solve(tmp_path, "--top", "0")

    assert result.returncode == 2


def test_top_negative_one_is_an_argparse_usage_error(tmp_path):
    result = _solve(tmp_path, "--top", "-1")

    assert result.returncode == 2


# --- --explain (depth) / --json (format) orthogonality ---------------------


def test_json_and_explain_together_attaches_contributions_only_to_the_named_rank(tmp_path):
    out_path = tmp_path / "out.json"

    result = _solve(tmp_path, "--json", str(out_path), "--explain", "2")

    assert result.returncode == 0, result.stderr
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    teams = payload["teams"]
    assert "contributions" in teams[1]
    for index, team in enumerate(teams):
        if index != 1:
            assert "contributions" not in team


def test_explain_without_json_prints_the_table_and_a_per_skill_attribution_block(tmp_path):
    result = _solve(tmp_path, "--explain", "2")

    assert result.returncode == 0, result.stderr
    assert "RANK SCORE LEADER MEMBERS GATES INCOMPLETE" in result.stdout
    assert "RANK 2:" in result.stdout
    # The shipped pool yields 36 arrangements, so the default top-N (10)
    # is what bounds the table rather than the arrangement count itself.
    assert len(_team_row_lines(result.stdout)) == DEFAULT_TOP_N


def test_explain_out_of_range_exits_5_and_names_the_valid_range(tmp_path):
    result = _solve(tmp_path, "--explain", "99")

    assert result.returncode == 5
    assert "1" in result.stderr
    assert "6" in result.stderr


# --- assumption reporting on every surface ----------------------------------


def test_text_mode_stdout_contains_assumptions_and_completeness_lines(tmp_path):
    result = _solve(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "PROBABILITY_TIERS:" in result.stdout
    assert "COMPLETENESS:" in result.stdout


def test_json_mode_payload_contains_config_provenance_and_completeness_sections(tmp_path):
    out_path = tmp_path / "out.json"

    result = _solve(tmp_path, "--json", str(out_path))

    assert result.returncode == 0, result.stderr
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert "provenance" in payload["config"]
    assert "completeness" in payload
    assert "unknown_fields" in payload["completeness"]


# --- pool.selectors value TYPES are part of the contract -------------------


def test_pool_selectors_leader_is_a_string_or_null_never_a_list(tmp_path):
    """`--leader` is repeatable so selector conflicts are reportable, but
    it still collapses to at most one card. The payload must report the
    RESOLVED leader, so an existing diff consumer sees the same value type it
    always did rather than a silently reshaped list."""
    unset = tmp_path / "unset.json"
    one = tmp_path / "one.json"
    repeated = tmp_path / "repeated.json"

    assert _solve(tmp_path, "--json", str(unset)).returncode == 0
    assert _solve(tmp_path, "--json", str(one), "--leader", "nene_01").returncode == 0
    assert _solve(
        tmp_path, "--json", str(repeated), "--leader", "nene_01", "--leader", "nene_01"
    ).returncode == 0

    def _leader(path):
        return json.loads(path.read_text(encoding="utf-8"))["pool"]["selectors"]["leader"]

    assert _leader(unset) is None
    assert _leader(one) == "nene_01"
    # Repeating the same card must not change the reported value.
    assert _leader(repeated) == "nene_01"


def _projected_arrangements(stderr: str) -> int:
    """The `arrangements=` figure out of the stderr PROJECTION line."""
    for line in stderr.splitlines():
        if line.startswith("PROJECTION:"):
            field = [f for f in line.split() if f.startswith("arrangements=")][0]
            return int(field.split("=", 1)[1].replace(",", ""))
    raise AssertionError(f"no PROJECTION line in stderr:\n{stderr}")


def test_json_arrangement_count_equals_the_stderr_projection(tmp_path):
    """`pool.arrangement_count` must be the selector-aware
    figure the solver actually enumerated — the same number run_solve already
    prints — so the JSON and the stderr projection can never disagree."""
    for extra in ([], ["--leader", "nene_01"], ["--require", "nene_01"]):
        out_path = tmp_path / f"out{len(extra)}{''.join(extra)}.json".replace("-", "")
        result = _solve(tmp_path, "--json", str(out_path), *extra)
        assert result.returncode == 0, result.stderr
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["pool"]["arrangement_count"] == _projected_arrangements(
            result.stderr
        ), f"disagreement with {extra or 'no selectors'}"


def test_json_owned_count_is_the_true_owned_file_count_not_the_searched_pool(tmp_path):
    """`owned_count` answers "how many cards do I own", `candidate_count`
    answers "how many were searched". `--include-unowned` admits a card that is
    not owned, so the two MUST differ — with the shipped 6-card fixture alone
    they coincide and a wrong `owned_count` would go unnoticed."""
    extended = tmp_path / "cards_with_unowned.json"
    doc = json.loads(GOOD_CARDS.read_text(encoding="utf-8"))
    doc["cards"].append(
        {
            "id": "unowned_extra_01",
            "member": "Extra Member",
            "generation": ["5期生"],
            "type": "Happy",
            "rarity": "SSR",
            "costume_name": "Extra Costume",
            "stats": {"performance": 1, "technique": 1, "sense": 1},
            "skills": {"SPECIAL": None, "ACTIVE": None, "PASSIVE": None},
            "costume_skill": None,
        }
    )
    extended.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    out_path = tmp_path / "out.json"
    result = _solve(
        tmp_path,
        "--json",
        str(out_path),
        "--include-unowned",
        "--require",
        "unowned_extra_01",
        cards=extended,
    )

    assert result.returncode == 0, result.stderr
    pool = json.loads(out_path.read_text(encoding="utf-8"))["pool"]
    assert pool["owned_count"] == 6, "six cards are owned, whatever was searched"
    assert pool["candidate_count"] == 7, "the unowned card widened the search"
    assert pool["owned_count"] != pool["candidate_count"]


# --- assumptions: byte-identical-to-default is still file-sourced ----------


def test_config_json_byte_identical_to_default_is_still_reported_file_sourced(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps(DEFAULT_CONFIG, sort_keys=True), encoding="utf-8"
    )

    result = _solve(tmp_path)

    assert result.returncode == 0, result.stderr
    tiers_line = next(
        line for line in result.stdout.splitlines() if line.startswith("PROBABILITY_TIERS:")
    )
    assert "(config.json)" in tiers_line


# --- assumptions: fully populated collection still reports 0 unknowns ------


def test_fully_populated_collection_still_emits_completeness_line_with_zero_unknown(tmp_path):
    doc = json.loads(GOOD_CARDS.read_text(encoding="utf-8"))
    for card in doc["cards"]:
        if card["id"] == "mio_01":
            card["skills"]["ACTIVE"]["magnitude"] = 20.0
    fully_populated_cards = tmp_path / "cards_fully_populated.json"
    fully_populated_cards.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    result = _solve(tmp_path, cards=fully_populated_cards)

    assert result.returncode == 0, result.stderr
    completeness_line = next(
        line for line in result.stdout.splitlines() if line.startswith("COMPLETENESS:")
    )
    assert "0 unknown field(s)" in completeness_line


# --- assumption ordering ------------------------------------------------------


def test_assumptions_header_line_order_is_identical_across_runs(tmp_path):
    result_a = _solve(tmp_path)
    result_b = _solve(tmp_path)

    assert result_a.returncode == 0, result_a.stderr
    assert result_b.returncode == 0, result_b.stderr
    assert _assumptions_lines(result_a.stdout) == _assumptions_lines(result_b.stdout)
    assert len(_assumptions_lines(result_a.stdout)) > 0


# --- non-interactive never prompts, projection/progress on stderr ----------


def test_non_interactive_run_above_threshold_proceeds_without_prompting(tmp_path):
    # A synthetic throughput measurement whose seconds_per_eval, times the
    # shipped 6-arrangement fixture, projects well above
    # CONFIRM_THRESHOLD_SECONDS (30s) -- this is exactly the run that
    # would prompt if a human were watching. subprocess.run's
    # capture_output=True makes stdout/stdin non-tty, so this must
    # proceed without blocking -- the guarantee the
    # --acquire loop (~48 solve calls, unattended) depends on.
    bench_dir = tmp_path / "bench"
    bench_dir.mkdir()
    (bench_dir / "throughput.json").write_text(
        json.dumps(
            {
                "seconds_per_eval": 100.0,
                # The per-SET half of the cost model. Zero here
                # keeps this fixture's projection exactly `arrangements * 100s`,
                # so the number this test reasons about is unchanged; the key
                # itself is required, and a throughput file missing it is a
                # loud ThroughputSchemaError rather than a partial projection.
                "seconds_per_set": 0.0,
                "config_hash": "irrelevant-for-this-test",
                "schema_version": SCHEMA_VERSION,
                "measured_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    result = _solve(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Proceed?" not in result.stdout
    assert "Proceed?" not in result.stderr
    assert "PROJECTED DURATION:" in result.stderr
    assert "unknown" not in result.stderr.split("PROJECTED DURATION:")[1].splitlines()[0]


def test_projection_and_progress_lines_appear_only_on_stderr(tmp_path):
    result = _solve(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "PROJECTION:" in result.stderr
    assert "progress:" in result.stderr
    assert "PROJECTION:" not in result.stdout
    assert "progress:" not in result.stdout


# --- --estimate answers without enumerating ---------------------------------


def test_estimate_exits_0_prints_arrangement_count_and_no_team_rows(tmp_path):
    result = _solve(tmp_path, "--estimate")

    assert result.returncode == 0, result.stderr
    assert "arrangements=" in result.stderr
    assert result.stdout == ""


# --- the unscored-mechanics footer -----------------------------------------


def test_shipped_fixture_carries_no_rider_and_prints_no_unscored_mechanics_footer(tmp_path):
    """The shipped fixture (`tests/fixtures/cards.json`)'s 15 populated slots all carry `"rider": {"kind":
    "none"}` -- the footer's absence here matches
    that fixture fact exactly, the same "no footer when nothing is
    unscored" discipline `format_provisional_footer` already follows."""
    result = _solve(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "UNDERESTIMATE" not in result.stdout
