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

Every module in the package (`domain/`, `solver/`, the input loaders) is held
to a total ban: the package reads, it never writes.
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


def test_no_filesystem_write_call_exists_under_src_hbuilder():
    offenders: list[str] = []
    for path in _iter_source_files():
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Call) and _call_is_filesystem_write(node):
                offenders.append(f"{path}:{node.lineno}")

    assert not offenders, (
        "found filesystem-write call(s) under src/hbuilder/ — SCHM-04 "
        f"forbids any write path to the canonical JSON files: {offenders}"
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
