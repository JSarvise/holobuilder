"""Guards against the coverage fixture recoupling to `data/`.

The shipped 6-card coverage fixture lives in `tests/fixtures/cards.json`/
`tests/fixtures/owned.json`, and every test module reaches it through the two
shared constants in `tests/conftest.py` (`FIXTURE_CARDS_PATH`/
`FIXTURE_OWNED_PATH`). The point is that `data/cards.json`/`data/owned.json`
can hold a real collection without breaking a single test — but only as long
as nothing in `tests/` quietly re-hardcodes a path back to `data/`.

This is enforced as a structural AST walk over every `.py` file in
`tests/`, mirroring `tests/test_source_of_truth.py`'s own approach rather
than a text search: a comment or docstring must stay free to mention
`data/cards.json` in prose (as `tests/test_gates.py` and
`tests/test_team.py` both deliberately do, to declare their own
independence from it), while actual code must not be able to hide the
same string behind a different quoting/joining style. Docstrings are
identified structurally (the first `Expr`-wrapped string statement of
each `Module`/`FunctionDef`/`AsyncFunctionDef`/`ClassDef` body), not by
position in the file, so a docstring anywhere in the module is exempt
and a same-shaped string constant used as real code anywhere else is not.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from conftest import FIXTURE_CARDS_PATH, FIXTURE_OWNED_PATH

TESTS_ROOT = Path(__file__).resolve().parent
_SELF_PATH = Path(__file__).resolve()

# The two forward-slash-joined canonical paths a test could hardcode directly
# as a single string — always forbidden outside a docstring.
_FORBIDDEN_PATH_STRINGS = {
    "data/cards.json",
    "data/owned.json",
}

# The bare "data" directory-name literal is forbidden ONLY in a path context —
# an operand of a `/` join (`REPO_ROOT / "data" / "cards.json"`, the pattern this
# plan removed) or an argument to `Path("data")`. A bare `"data"` used as a dict
# KEY is not a `data/` path: the game master's own rows carry a `"data"` field
# (`node_costs_from_master` reads `row["data"]["9"]`), and `test_board_plan.py`
# builds those rows literally — flagging that key was a false positive.
_PATH_DIR_LITERAL = "data"


def _iter_test_files() -> list[Path]:
    return sorted(p for p in TESTS_ROOT.rglob("*.py") if p.resolve() != _SELF_PATH)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docstring_constant_ids(tree: ast.Module) -> set[int]:
    """Every string `Constant` node that is a docstring — the first
    statement of a `Module`/`FunctionDef`/`AsyncFunctionDef`/`ClassDef`
    body, wrapped in an `Expr` — identified by node identity so the scan
    below can skip exactly those nodes and no others."""
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_ids.add(id(body[0].value))
    return docstring_ids


def _path_context_data_ids(tree: ast.Module) -> set[int]:
    """Node ids of `"data"` string Constants used as a PATH component — an
    operand of a `/` join (`REPO_ROOT / "data" / ...`) or an argument to a
    `Path("data")` call. A `"data"` in any other position (a dict key, a plain
    string) is not a `data/` path reference and must not be flagged."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            for side in (node.left, node.right):
                if isinstance(side, ast.Constant) and side.value == _PATH_DIR_LITERAL:
                    ids.add(id(side))
        elif isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id == "Path")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "Path")
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value == _PATH_DIR_LITERAL:
                    ids.add(id(arg))
    return ids


def _forbidden_constant_offenders(path: Path) -> list[str]:
    tree = _parse(path)
    docstring_ids = _docstring_constant_ids(tree)
    path_data_ids = _path_context_data_ids(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstring_ids:
            continue
        # The full canonical path strings are always a coupling; the bare "data"
        # directory literal is one only in a path context (join / Path()).
        if node.value in _FORBIDDEN_PATH_STRINGS or (
            node.value == _PATH_DIR_LITERAL and id(node) in path_data_ids
        ):
            offenders.append(f"{path}:{node.lineno}: {node.value!r}")
    return offenders


def test_no_hardcoded_canonical_data_path_exists_outside_a_docstring():
    offenders: list[str] = []
    for path in _iter_test_files():
        offenders.extend(_forbidden_constant_offenders(path))

    assert not offenders, (
        "found a re-hardcoded canonical data/ path outside a docstring — "
        "the SCHM-03 coverage fixture must be reached only through "
        "conftest.py's FIXTURE_CARDS_PATH/FIXTURE_OWNED_PATH constants: "
        f"{offenders}"
    )


def test_at_least_25_test_modules_were_actually_scanned():
    """Guards against this test silently passing because TESTS_ROOT
    resolved to an empty/near-empty directory (mirrors
    `test_source_of_truth.py::test_at_least_one_source_file_was_actually_scanned`)."""
    assert len(_iter_test_files()) >= 25


def test_fixture_files_exist_and_hold_exactly_the_six_synthetic_ids():
    assert FIXTURE_CARDS_PATH.exists()
    assert FIXTURE_OWNED_PATH.exists()

    cards_doc = json.loads(FIXTURE_CARDS_PATH.read_text(encoding="utf-8"))
    owned_doc = json.loads(FIXTURE_OWNED_PATH.read_text(encoding="utf-8"))

    expected_ids = {
        "nene_01",
        "fubuki_01",
        "mio_01",
        "matsuri_01",
        "polka_01",
        "lamy_01",
    }
    assert {card["id"] for card in cards_doc["cards"]} == expected_ids
    assert set(owned_doc["owned"]) == expected_ids
