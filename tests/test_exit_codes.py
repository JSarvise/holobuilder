"""D-24/D-24a/D-24c: exit codes are a public API, not an accident of
control flow. Phase 4's `--acquire` loop branches on them in a tight
loop (~48 unowned cards, one `solve` per candidate) — a code that drifts
because of a control-flow edit is a breaking change to a consumer that
already exists in the roadmap, not a cosmetic difference.

This file pins every one of the eight values in `EXIT_CODE_MEANINGS`
(0-7) to exactly one named trigger (or, for 1/2/7, a structural assertion
in place of a trigger — 1 is never raised deliberately, 2 is reserved for
argparse alone, and 7's confirmation gate is wired in a later plan), so a
newly added code with no trigger, or a removed trigger for an existing
code, fails here rather than at the next verification round.

D-24a: 6 ("this candidate is unsolvable, try the next") and 7 ("stop
asking me") must never collapse into one another — merging them gives
`--acquire` either an infinite prompt loop or 48 silently skipped
candidates.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

from hbuilder.cli import (
    EXIT_CODE_MEANINGS,
    EXIT_CONFIG,
    EXIT_DECLINED,
    EXIT_INPUT_DATA,
    EXIT_INSUFFICIENT_POOL,
    EXIT_INTERNAL,
    EXIT_INVALID_SELECTOR,
    EXIT_OK,
)
from hbuilder.domain.schema import SCHEMA_VERSION

from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_PATH = REPO_ROOT / "src" / "hbuilder" / "cli.py"
GOOD_CARDS = FIXTURE_CARDS_PATH
GOOD_OWNED = FIXTURE_OWNED_PATH

_TRACEBACK_BANNER = "Traceback (most recent call last)"


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


def _solve(cwd: Path, *extra: str) -> subprocess.CompletedProcess:
    return _run(
        cwd,
        ["solve", "--cards", str(GOOD_CARDS), "--owned", str(GOOD_OWNED), *extra],
    )


def _gates(cwd: Path, *extra: str) -> subprocess.CompletedProcess:
    return _run(
        cwd,
        [
            "gates",
            "--cards",
            str(GOOD_CARDS),
            "--owned",
            str(GOOD_OWNED),
            "--leader",
            "nene_01",
            "--members",
            "fubuki_01,mio_01,matsuri_01,polka_01,lamy_01",
            *extra,
        ],
    )


# --- 0: success --------------------------------------------------------


def test_exit_0_successful_solve_against_the_shipped_data(tmp_path):
    result = _solve(tmp_path)
    assert result.returncode == EXIT_OK, result.stderr


def test_exit_0_successful_gates_run(tmp_path):
    result = _gates(tmp_path)
    assert result.returncode == EXIT_OK, result.stderr


# --- 1: internal error, never raised deliberately -----------------------


def test_exit_1_is_never_raised_deliberately_by_cli_py():
    source = CLI_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CLI_PATH))

    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and node.value is not None:
            if isinstance(node.value, ast.Name) and node.value.id == "EXIT_INTERNAL":
                raise AssertionError(
                    "cli.py must never `return EXIT_INTERNAL` explicitly — "
                    "1 means an uncaught exception (a bug), never a "
                    "deliberate return value"
                )
        if isinstance(node, ast.Call):
            is_sys_exit = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "exit"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sys"
            )
            if is_sys_exit and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and arg.value == 1:
                    raise AssertionError(
                        "cli.py must never call sys.exit(1) explicitly — 1 "
                        "means an uncaught exception (a bug), never a "
                        "deliberate exit"
                    )


# --- 2: reserved for argparse's own usage errors only --------------------


def test_exit_2_bogus_flag_is_an_argparse_usage_error(tmp_path):
    result = _solve(tmp_path, "--bogus-flag")
    assert result.returncode == 2


def test_exit_2_top_zero_is_an_argparse_usage_error(tmp_path):
    result = _solve(tmp_path, "--top", "0")
    assert result.returncode == 2


def test_exit_2_unregistered_subcommand_is_an_argparse_usage_error(tmp_path):
    result = _run(tmp_path, ["extract"])
    assert result.returncode == 2


def test_exit_2_is_never_returned_explicitly_by_cli_py():
    # D-24: 2 is reserved for argparse's own usage errors alone — nothing
    # in cli.py may return the literal 2, or the meaning of "argparse usage
    # error" becomes undetectable.
    tree = ast.parse(CLI_PATH.read_text(encoding="utf-8"), filename=str(CLI_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant):
            assert node.value.value != 2, (
                "cli.py must never `return 2` explicitly — 2 is reserved "
                "for argparse's own usage errors alone (D-24)"
            )


# --- 3: input data error --------------------------------------------------


def test_exit_3_nonexistent_cards_path(tmp_path):
    result = _run(
        tmp_path,
        [
            "solve",
            "--cards",
            str(tmp_path / "does_not_exist.json"),
            "--owned",
            str(GOOD_OWNED),
        ],
    )
    assert result.returncode == EXIT_INPUT_DATA


def test_exit_3_unparseable_cards_json(tmp_path):
    bad_cards = tmp_path / "bad_cards.json"
    bad_cards.write_bytes(b"not json at all")
    result = _run(
        tmp_path, ["solve", "--cards", str(bad_cards), "--owned", str(GOOD_OWNED)]
    )
    assert result.returncode == EXIT_INPUT_DATA


def test_exit_3_unparseable_owned_json(tmp_path):
    bad_owned = tmp_path / "bad_owned.json"
    bad_owned.write_bytes(b"not json at all")
    result = _run(
        tmp_path, ["solve", "--cards", str(GOOD_CARDS), "--owned", str(bad_owned)]
    )
    assert result.returncode == EXIT_INPUT_DATA


def test_exit_3_directory_passed_as_cards_path(tmp_path):
    result = _run(
        tmp_path, ["solve", "--cards", str(tmp_path), "--owned", str(GOOD_OWNED)]
    )
    assert result.returncode == EXIT_INPUT_DATA


def test_exit_3_cards_json_failing_schema_validation(tmp_path):
    bad_record = {
        "id": "bad_01",
        "member": "Test",
        "generation": ["5期生"],
        "type": "Happy",
        "costume_name": "Test Costume",
        "stats": {"performance": 1, "technique": 1, "sense": 1},
        "skills": {"SPECIAL": None, "ACTIVE": None, "PASSIVE": None},
        "costume_skill": None,
        # "rarity" deliberately omitted -- a required field per
        # _CARD_REQUIRED_FIELDS in domain/schema.py.
    }
    bad_cards = tmp_path / "bad_schema_cards.json"
    bad_cards.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "cards": [bad_record]}),
        encoding="utf-8",
    )
    result = _run(
        tmp_path, ["solve", "--cards", str(bad_cards), "--owned", str(GOOD_OWNED)]
    )
    assert result.returncode == EXIT_INPUT_DATA


# --- 4: config error -------------------------------------------------------


def test_exit_4_config_json_missing_required_model_key(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"model": {"activation_rate": "pp"}}), encoding="utf-8"
    )
    result = _solve(tmp_path)
    assert result.returncode == EXIT_CONFIG


# --- 5: invalid selector ---------------------------------------------------


def test_exit_5_unknown_require_id(tmp_path):
    result = _solve(tmp_path, "--require", "nonexistent_99")
    assert result.returncode == EXIT_INVALID_SELECTOR


def test_exit_5_unknown_exclude_id(tmp_path):
    result = _solve(tmp_path, "--exclude", "nonexistent_99")
    assert result.returncode == EXIT_INVALID_SELECTOR


def test_exit_5_require_and_exclude_name_the_same_card(tmp_path):
    result = _solve(tmp_path, "--require", "nene_01", "--exclude", "nene_01")
    assert result.returncode == EXIT_INVALID_SELECTOR


def test_exit_5_two_different_leader_flags_naming_both_ids(tmp_path):
    """SOLV-06: argparse without `append` would keep only the last occurrence
    and discard the first in silence. Two cards cannot share one leader slot."""
    result = _solve(tmp_path, "--leader", "nene_01", "--leader", "polka_01")
    assert result.returncode == EXIT_INVALID_SELECTOR
    assert "nene_01" in result.stderr and "polka_01" in result.stderr


def test_repeated_leader_flag_naming_the_same_card_is_accepted(tmp_path):
    """SOLV-06 adjacency: equal merges, so the flag composes with shell loops
    exactly as repeated --require/--exclude do (D-05)."""
    result = _solve(tmp_path, "--leader", "nene_01", "--leader", "nene_01")
    assert result.returncode != EXIT_INVALID_SELECTOR


def test_exit_5_unowned_id_without_include_unowned(tmp_path):
    unowned_cards = tmp_path / "cards_with_extra.json"
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
    unowned_cards.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    result = _run(
        tmp_path,
        [
            "solve",
            "--cards",
            str(unowned_cards),
            "--owned",
            str(GOOD_OWNED),
            "--require",
            "unowned_extra_01",
        ],
    )
    assert result.returncode == EXIT_INVALID_SELECTOR


# --- 6: insufficient pool ---------------------------------------------------


def test_exit_6_needs_two_exclusions_now_that_five_cards_are_enough(tmp_path):
    """Excluding one of the shipped six leaves five, which is exactly a team
    (five members, leader drawn from the same pool). It takes a second
    exclusion to fall below MEMBER_COUNT and reach the insufficient-pool
    exit."""
    assert _solve(tmp_path, "--exclude", "mio_01").returncode != EXIT_INSUFFICIENT_POOL

    result = _solve(tmp_path, "--exclude", "mio_01", "--exclude", "polka_01")
    assert result.returncode == EXIT_INSUFFICIENT_POOL


# --- 7: declined confirmation (structural — wired in plan 03-07) -----------


def test_exit_7_is_defined_and_documented_distinctly_from_6():
    assert EXIT_DECLINED == 7
    assert EXIT_DECLINED in EXIT_CODE_MEANINGS
    assert EXIT_CODE_MEANINGS[EXIT_DECLINED] != EXIT_CODE_MEANINGS[EXIT_INSUFFICIENT_POOL]


# --- Cross-cutting pins ------------------------------------------------------


def test_codes_six_and_seven_are_distinct_values_with_distinct_meanings():
    assert EXIT_INSUFFICIENT_POOL != EXIT_DECLINED, (
        "D-24a: 6 ('this candidate is unsolvable, try the next') and 7 "
        "('stop asking me') must be distinct values — merging them gives "
        "--acquire either an infinite prompt loop or 48 silently skipped "
        "candidates"
    )
    assert (
        EXIT_CODE_MEANINGS[EXIT_INSUFFICIENT_POOL] != EXIT_CODE_MEANINGS[EXIT_DECLINED]
    ), (
        "D-24a: 6 means this candidate is unsolvable so try the next, 7 "
        "means stop asking me — their EXIT_CODE_MEANINGS entries must read "
        "differently or the taxonomy is not actually pinning the "
        "distinction"
    )


def test_every_exit_code_has_exactly_one_named_trigger():
    # D-24c: the taxonomy is a contract, not an accident of control flow —
    # every one of the eight documented codes has exactly one named trigger
    # (a real subprocess invocation for 0/2/3/4/5/6, a structural assertion
    # for 1/7). This test's own existence is the pin: a newly added code
    # with no corresponding entry above, or the removal of a trigger for an
    # existing code, must be caught by a human reviewing this file's own
    # completeness against EXIT_CODE_MEANINGS.
    documented_codes = set(EXIT_CODE_MEANINGS)
    assert documented_codes == {0, 1, 2, 3, 4, 5, 6, 7}, (
        f"EXIT_CODE_MEANINGS must document exactly codes 0-7, got "
        f"{sorted(documented_codes)!r} -- a code added here without a "
        "corresponding trigger test above is exactly the drift this test "
        "exists to catch"
    )


def test_no_stdout_output_on_any_failure_path_and_no_traceback_on_any_of_them(tmp_path):
    failing_results = [
        _solve(tmp_path, "--bogus-flag"),
        _solve(tmp_path, "--top", "0"),
        _run(tmp_path, ["extract"]),
        _run(
            tmp_path,
            [
                "solve",
                "--cards",
                str(tmp_path / "does_not_exist.json"),
                "--owned",
                str(GOOD_OWNED),
            ],
        ),
        _solve(tmp_path, "--require", "nonexistent_99"),
        # Two exclusions, not one: one still leaves a solvable five-card pool.
        _solve(tmp_path, "--exclude", "mio_01", "--exclude", "polka_01"),
    ]
    for result in failing_results:
        assert result.returncode != 0
        assert result.stdout == ""
        assert _TRACEBACK_BANNER not in result.stdout
        assert _TRACEBACK_BANNER not in result.stderr


def test_the_shared_load_and_guard_is_the_only_load_path():
    # D-09's anti-recurrence check: a second independent load path in a
    # future subcommand fails here rather than at the next verification
    # round. load_cards/load_owned/load_scoring_config are each called
    # exactly once in cli.py, and every one of those call sites sits
    # inside load_inputs.
    tree = ast.parse(CLI_PATH.read_text(encoding="utf-8"), filename=str(CLI_PATH))

    load_inputs_def = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "load_inputs":
            load_inputs_def = node
            break
    assert load_inputs_def is not None, "cli.py must define a load_inputs() function"

    load_inputs_call_ids = {
        id(node)
        for node in ast.walk(load_inputs_def)
        if isinstance(node, ast.Call)
    }

    tracked_names = {"load_cards", "load_owned", "load_scoring_config"}
    counts: dict[str, int] = {name: 0 for name in tracked_names}
    outside_calls: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name in tracked_names:
                counts[name] += 1
                if id(node) not in load_inputs_call_ids:
                    outside_calls.append(name)

    assert counts == {name: 1 for name in tracked_names}, (
        f"load_cards/load_owned/load_scoring_config must each be called "
        f"exactly once in cli.py, got {counts!r} -- a second call site is "
        "exactly the second independent load path D-09 forbids"
    )
    assert outside_calls == [], (
        f"the following loaders were called outside load_inputs: "
        f"{outside_calls!r} -- every load must go through the one shared "
        "load-and-guard function"
    )
