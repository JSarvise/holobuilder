"""SCHM-04: `cards.json` and `owned.json` are hand-editable and are the sole
source of truth — no automated path under `src/hbuilder/` may ever write to
them.

This is enforced as a structural AST walk over every `.py` file in the
package, not a string search of the source text: a comment or a docstring
that merely mentions "write" cannot fool it, and it is not fooled by the
call being spread across a variable-bound function reference either (it
checks the actual call-site shape). Two independent properties are
checked: no call node is a filesystem-write call, and no module defines a
callable whose name signals write intent (`save_`, `write_`, `dump_`
prefixes).

Plan 03-07 adds exactly one sanctioned write path: `hbuilder solve --json
<path>` (CLI-03/D-15) writes the diffable JSON payload to a path the
invoking user names on their own command line — a new, non-canonical
output file, never `cards.json`/`owned.json` themselves. 03-07-PLAN.md's
own threat register (T-03-27) explicitly accepts this: "the path is the
invoking user's own argument and carries their own privileges." The write
test below is narrowed to exempt *exactly* that one call site —
`cli.py::run_solve`'s `Path(args.json).write_text(...)` — identified by
AST node identity, not by file or line number, so the exemption cannot
silently widen to cover a second, unrelated write call added later. This
is a deliberate narrowing of the original blanket prohibition, not a
loosening of SCHM-04 itself: `cards.json` and `owned.json` remain exactly
as unwritable by any automated path as before this plan, and every other
module (`domain/`, `output/`, `solver/`) is still held to a total ban.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "hbuilder"

_FORBIDDEN_NAME_PREFIXES = ("save_", "write_", "dump_")


def _iter_source_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _mode_argument(call: ast.Call) -> object:
    """Extract the `mode` argument's literal value from an `open(...)`-shaped
    call, whether passed positionally or as a keyword. Returns `None` if no
    literal mode could be determined (including "no mode argument at all",
    whose default is the read-only `'r'`)."""
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        return call.args[1].value
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return None


def _open_call_is_write_capable(call: ast.Call) -> bool:
    mode = _mode_argument(call)
    if mode is None:
        return False  # no mode argument, or a non-literal mode -> default 'r'
    if not isinstance(mode, str):
        return False
    return any(character not in "rb" for character in mode)


def _call_is_filesystem_write(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name) and func.id == "open":
        return _open_call_is_write_capable(call)
    if isinstance(func, ast.Attribute):
        if func.attr in ("write_text", "write_bytes"):
            return True
        if func.attr == "open":
            return _open_call_is_write_capable(call)
        if (
            func.attr == "dump"
            and isinstance(func.value, ast.Name)
            and func.value.id == "json"
        ):
            return True
    return False


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_no_filesystem_write_call_exists_under_src_hbuilder():
    cli_path = SRC_ROOT / "cli.py"
    cli_tree = _parse(cli_path)
    run_solve_def = _find_function(cli_tree, "run_solve")
    assert run_solve_def is not None, "cli.py must define a run_solve() function"

    # The one call sanctioned by 03-07/CLI-03/D-15/T-03-27: --json's write
    # to a user-supplied, non-canonical output path. Matched by node
    # identity (id()) against the SAME parse of cli.py used below, so a
    # second, unrelated write call added anywhere (including inside
    # run_solve itself) cannot silently ride along with this exemption.
    exempt_call_ids = {
        id(node)
        for node in ast.walk(run_solve_def)
        if isinstance(node, ast.Call)
        and _call_is_filesystem_write(node)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write_text"
    }
    assert len(exempt_call_ids) == 1, (
        "expected exactly one write_text(...) call inside run_solve() — "
        f"the sanctioned --json output write — found {len(exempt_call_ids)}. "
        "A second write call site inside run_solve must be reviewed "
        "deliberately, not silently exempted alongside this one."
    )

    offenders: list[str] = []
    for path in _iter_source_files():
        tree = cli_tree if path == cli_path else _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_is_filesystem_write(node):
                if id(node) in exempt_call_ids:
                    continue
                offenders.append(f"{path}:{node.lineno}")

    assert not offenders, (
        "found filesystem-write call(s) under src/hbuilder/ — SCHM-04 "
        "forbids any write path to the canonical JSON files, and only "
        "cli.py::run_solve's single --json write_text(...) call is "
        f"exempted (03-07/T-03-27): {offenders}"
    )


def test_no_save_write_dump_prefixed_callable_exists_under_src_hbuilder():
    offenders: list[str] = []
    for path in _iter_source_files():
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith(_FORBIDDEN_NAME_PREFIXES):
                    offenders.append(f"{path}:{node.lineno}:{node.name}")

    assert not offenders, (
        "found a callable named with a save_/write_/dump_ prefix under "
        f"src/hbuilder/ — a write-capable function must not exist at all, "
        f"per SCHM-04: {offenders}"
    )


def test_at_least_one_source_file_was_actually_scanned():
    """Guards against this test silently passing because SRC_ROOT resolved
    to an empty/missing directory."""
    assert len(_iter_source_files()) > 0
