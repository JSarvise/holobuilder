"""02-REVIEW.md's CR-01 and 02-VERIFICATION.md's truth #6: the CLI's
error guard caught only `(SchemaError, TeamError)`, so `FileNotFoundError`
and `json.JSONDecodeError` raised by `load_cards`'s and `load_owned`'s own
`open()` and `json.load()` calls — before any validator runs — escaped as
unhandled Python tracebacks. This is the same failure class 02-05 closed
for `load_scoring_config()` alone; this module closes it for all four
call sites the re-asserted must-have names (`load_cards`, `build_team`,
`load_owned`, `load_scoring_config`), plus pins the guard's shape
structurally so a third recurrence fails a named test instead of a
verification round.

D-24b: this file used to run these five failure modes through a
standalone gate-report script and assert exit code 2. D-09 folds
that script into `hbuilder gates`, behind the CLI's one shared
`load_inputs` guard, and D-24 reassigns exit 2 to be reserved for
argparse's own usage errors alone — every one of these five load-and-guard
failures now returns `EXIT_INPUT_DATA` (3) instead. This is a deliberate
reassignment, not a test loosened around a passing suite: the reason is
recorded here and at every changed assertion.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_PATH = REPO_ROOT / "src" / "hbuilder" / "cli.py"
GOOD_CARDS = FIXTURE_CARDS_PATH
GOOD_OWNED = FIXTURE_OWNED_PATH

# The banner text every CPython traceback begins with. Bound once so all
# five failure-mode tests check the identical string, and the absence of a
# traceback is a single named assertion rather than five hand-copied ones.
_TRACEBACK_BANNER = "Traceback (most recent call last)"


def _run_cli(cwd: Path, *, cards: Path, owned: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "hbuilder.cli",
            "gates",
            "--cards",
            str(cards),
            "--owned",
            str(owned),
            "--leader",
            "nene_01",
            "--members",
            "fubuki_01,mio_01,matsuri_01,polka_01,lamy_01",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _assert_clean_failure(result: subprocess.CompletedProcess, *, expected_in_stderr: str) -> None:
    # D-24b: reassigned from 2 to 3 (EXIT_INPUT_DATA) — 2 is now reserved
    # for argparse's own usage errors only, per D-24.
    assert result.returncode == 3
    assert result.stdout == ""
    assert expected_in_stderr in result.stderr
    assert _TRACEBACK_BANNER not in result.stdout
    assert _TRACEBACK_BANNER not in result.stderr


def test_cli_reports_nonexistent_cards_path_on_stderr_with_exit_code_3(tmp_path):
    missing_cards = tmp_path / "does_not_exist_xyz.json"

    result = _run_cli(REPO_ROOT, cards=missing_cards, owned=GOOD_OWNED)

    _assert_clean_failure(result, expected_in_stderr=str(missing_cards))


def test_cli_reports_syntactically_invalid_owned_json_on_stderr_with_exit_code_3(tmp_path):
    bad_owned = tmp_path / "bad_owned_tmp.json"
    bad_owned.write_bytes(b"not json at all")

    result = _run_cli(REPO_ROOT, cards=GOOD_CARDS, owned=bad_owned)

    _assert_clean_failure(result, expected_in_stderr=str(bad_owned))


def test_cli_reports_syntactically_invalid_cards_json_on_stderr_with_exit_code_3(tmp_path):
    bad_cards = tmp_path / "bad_cards_tmp.json"
    bad_cards.write_bytes(b"not json at all")

    result = _run_cli(REPO_ROOT, cards=bad_cards, owned=GOOD_OWNED)

    _assert_clean_failure(result, expected_in_stderr=str(bad_cards))


def test_cli_reports_syntactically_invalid_config_json_on_stderr_with_exit_code_4(tmp_path):
    # The third instance of the same class: load_scoring_config's own
    # try/except catches only FileNotFoundError around its json.load, so a
    # PRESENT-but-unparseable config.json raises json.JSONDecodeError out of
    # the function. cwd=tmp_path so DEFAULT_CONFIG_PATH's relative
    # "config.json" resolves to the file written here; --cards/--owned are
    # absolute so they resolve regardless of cwd. See
    # tests/test_cli_config_errors.py for the control case (absent
    # config.json still exits 0 via the sanctioned DEFAULT_CONFIG fallback).
    #
    # D-24b: a config.json failure is EXIT_CONFIG (4), distinct from the
    # other four cases in this file (EXIT_INPUT_DATA, 3) — a different file
    # with a different fix (D-24).
    (tmp_path / "config.json").write_bytes(b"not json at all")

    result = _run_cli(tmp_path, cards=GOOD_CARDS, owned=GOOD_OWNED)

    assert result.returncode == 4
    assert result.stdout == ""
    assert "config.json" in result.stderr
    assert _TRACEBACK_BANNER not in result.stdout
    assert _TRACEBACK_BANNER not in result.stderr


def test_cli_reports_a_directory_passed_as_cards_path_as_a_named_error_with_exit_code_3(tmp_path):
    # Opening a directory raises IsADirectoryError on POSIX and
    # PermissionError on Windows; both are OSError subclasses. Deliberately
    # not asserting on errno text or exception class name -- only that the
    # OSError handler (not just FileNotFoundError) covers this case. This is
    # what distinguishes a handler catching OSError from one catching only
    # FileNotFoundError.
    result = _run_cli(REPO_ROOT, cards=tmp_path, owned=GOOD_OWNED)

    _assert_clean_failure(result, expected_in_stderr=str(tmp_path))


def _find_load_inputs(tree: ast.AST) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "load_inputs":
            return node
    raise AssertionError("cli.py must define a load_inputs() function")


def test_load_inputs_contains_exactly_one_try_block_so_the_existing_guard_pin_cannot_be_split_apart():
    # D-09: the guard pin re-points here from the old standalone
    # script's main()-scoped test — the shared load_inputs is now the
    # one and only load-and-guard site both `solve` and `gates` call.
    # tests/test_cli_config_errors.py's AST test inspects only the FIRST
    # ast.Try node it reaches inside load_inputs(). That test silently stops
    # proving anything the moment load_inputs() contains more than one try
    # block -- a future edit could split the guard so load_scoring_config
    # sits outside every guard while the older test stays green. This pin
    # makes that fragmentation fail loudly instead.
    tree = ast.parse(CLI_PATH.read_text(encoding="utf-8"), filename=str(CLI_PATH))
    load_inputs_def = _find_load_inputs(tree)

    try_nodes = [node for node in ast.walk(load_inputs_def) if isinstance(node, ast.Try)]

    assert len(try_nodes) == 1, (
        "load_inputs() must contain exactly one try block -- splitting the "
        "guard into several blocks would leave "
        "tests/test_cli_config_errors.py's existing AST pin (which "
        "inspects only the first try it finds) green while a later "
        "guarded call sat outside every guard"
    )


def test_load_inputs_handlers_name_schema_error_team_error_oserror_and_json_decode_error():
    # This failure class has now recurred twice: once as 02-05's WR-05/CR-01
    # for load_scoring_config, once as 02-REVIEW's fresh CR-01 for
    # load_cards/load_owned. Both recurrences were narrow handler tuples
    # that silently excluded exception types the loaders actually raise.
    # Exact set equality (not a subset check) is what makes a third
    # narrowing fail here instead of at the next verification round.
    tree = ast.parse(CLI_PATH.read_text(encoding="utf-8"), filename=str(CLI_PATH))
    load_inputs_def = _find_load_inputs(tree)

    handler_names: set[str] = set()
    for node in ast.walk(load_inputs_def):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                handler_names.add("<bare>")
            elif isinstance(node.type, ast.Tuple):
                for elt in node.type.elts:
                    if isinstance(elt, ast.Name):
                        handler_names.add(elt.id)
                    elif isinstance(elt, ast.Attribute):
                        handler_names.add(elt.attr)
            elif isinstance(node.type, ast.Name):
                handler_names.add(node.type.id)
            elif isinstance(node.type, ast.Attribute):
                handler_names.add(node.type.attr)

    assert handler_names == {"SchemaError", "TeamError", "OSError", "JSONDecodeError"}, (
        f"load_inputs()'s exception handlers must name exactly SchemaError, "
        f"TeamError, OSError and JSONDecodeError -- got {handler_names!r}. "
        "A bare handler or a narrowed/widened set is exactly the defect "
        "that produced both prior recurrences of this gap."
    )
