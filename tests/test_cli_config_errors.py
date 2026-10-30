"""CR-01 + WR-05 coverage: a `config.json` whose `model` object omits a
required key raises `SchemaError` from `load_scoring_config`, and that
`SchemaError` must reach the CLI's shared `load_inputs` guard rather than
escaping as an unhandled traceback. This is proven end to end via a real
subprocess invocation of the CLI, not only against the unit-level
validator, and pinned structurally by an AST walk asserting the
`load_scoring_config()` call site sits inside `load_inputs()`'s `try`
block.

D-24b: this file used to run this failure mode through a standalone
gate-report script and assert exit code 2. D-09 folds that script into
`hbuilder gates`, behind the CLI's one shared `load_inputs` guard,
and D-24 reassigns config failures to `EXIT_CONFIG` (4) — a different
file with a different fix than the input-data cases in
`tests/test_cli_input_errors.py`, and exit 2 is now reserved for
argparse's own usage errors alone. This is a deliberate reassignment,
recorded here, not a test loosened around a passing suite.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH

CARDS_PATH = FIXTURE_CARDS_PATH
OWNED_PATH = FIXTURE_OWNED_PATH
REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_PATH = REPO_ROOT / "src" / "hbuilder" / "cli.py"


def _run_cli(cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "hbuilder.cli",
            "gates",
            "--cards",
            str(REPO_ROOT / CARDS_PATH),
            "--owned",
            str(REPO_ROOT / OWNED_PATH),
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


def test_cli_reports_malformed_config_json_on_stderr_with_exit_code_4(tmp_path):
    # D-24b: reassigned from 2 to 4 (EXIT_CONFIG) — 2 is now reserved for
    # argparse's own usage errors only, per D-24.
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"model": {"activation_rate": "pp"}}), encoding="utf-8"
    )

    result = _run_cli(tmp_path)

    assert result.returncode == 4
    assert result.stdout == ""
    assert "model.stacking" in result.stderr


def test_cli_with_no_config_json_in_cwd_falls_back_to_defaults_and_exits_zero(tmp_path):
    result = _run_cli(tmp_path)

    assert result.returncode == 0, result.stderr


def test_load_scoring_config_is_called_inside_load_inputs_schema_error_guarded_try_block():
    tree = ast.parse(CLI_PATH.read_text(encoding="utf-8"), filename=str(CLI_PATH))

    load_inputs_def = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "load_inputs":
            load_inputs_def = node
            break
    assert load_inputs_def is not None, "cli.py must define a load_inputs() function"

    try_node = None
    for node in ast.walk(load_inputs_def):
        if isinstance(node, ast.Try):
            try_node = node
            break
    assert try_node is not None, "load_inputs() must contain a try block"

    found_call = False
    for node in ast.walk(ast.Module(body=try_node.body, type_ignores=[])):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "load_scoring_config"
        ):
            found_call = True
            break

    assert found_call, (
        "load_scoring_config() must be called inside load_inputs()'s try "
        "block — an unguarded call site outside the try is what turns a "
        "malformed config.json into an unhandled traceback instead of the "
        "tool's normal stderr + exit-code error path"
    )
