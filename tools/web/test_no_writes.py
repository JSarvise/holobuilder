"""The write prohibition, made structural.

> No code path may write `data/cards.json` or `data/owned.json`.

A rule enforced by discipline is a rule that fails once. This is the
enforcement: an AST walk over every `.py` file in `tools/web/`, in the
shape of `tests/test_fixture_isolation.py`'s own guard, which likewise
walks a tree rather than grepping so that a comment or docstring stays
free to *name* the two files in prose while real code cannot hide the
same string behind a different quoting or joining style.

Three independent checks, because each catches something the others do
not:

1. **Taint tracking.** Any expression that mentions `"cards.json"` or
   `"owned.json"` -- as a literal, as a `REPO_ROOT / "data" / "cards.json"`
   join, or through a module-level name bound to either -- is tainted.
   A tainted expression may only reach a **whitelisted read sink**
   (`load_inputs`, `load_cards`, `load_owned`, `Path`, `str`, ...). Any
   other call receiving it fails. This is an allowlist, not a blocklist:
   a write function nobody thought of is rejected by default, which is
   the whole point of doing it structurally.
2. **No tainted attribute-call at all** whose method name is a mutator
   (`write_text`, `unlink`, `open`, ...). Catches `CARDS_PATH.write_text(...)`,
   which check 1 alone would see as a sink it does not recognise but
   would report less precisely.
3. **Every `open()` in `tools/web/` names a read mode.** Blanket, not
   taint-scoped: this package has no business opening any file for
   writing, and a blanket rule cannot be defeated by laundering the path
   through an untracked local.

Run it with `python -m pytest tools/web/test_no_writes.py`. It is NOT in
`pyproject.toml`'s `testpaths = ["tests"]`, deliberately -- the repo-root
`python -m pytest -q` baseline (866 passed, 11 deselected) must stay
exactly where it is, and this guard is about `tools/web/`, not about the
solver's correctness suite.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

WEB_ROOT = Path(__file__).resolve().parent
_SELF = Path(__file__).resolve()

# The two protected files, by basename. Basename rather than the joined
# "data/cards.json" because `server.py` builds them as
# `REPO_ROOT / "data" / "cards.json"` -- the joined string never appears
# as one constant anywhere, which is exactly how a grep-based guard would
# have missed it.
PROTECTED_BASENAMES = frozenset({"cards.json", "owned.json"})

# Calls a tainted path may legally reach. Everything else is a failure.
# `load_inputs` is the project's single shared read path; the rest are
# path/inspection helpers that cannot write.
READ_SINKS = frozenset(
    {
        "load_inputs",
        "load_cards",
        "load_owned",
        "card_provenance_of",
        "Path",
        "str",
        "print",
        "len",
        "is_file",
        "exists",
        "resolve",
        "read_text",
        "read_bytes",
        "is_dir",
        # `server._read_json` — a JSON file or `None` when absent. Named here
        # deliberately rather than by widening the rule: it reaches only
        # `is_file` and `read_text`, both already on this list. The only reason
        # it had to be added at all is that this is an allowlist, and a read
        # helper nobody thought about is rejected by default. That is the
        # design working, not friction to be removed.
        "_read_json",
    }
)

# The one path anything under `tools/web/` may WRITE. `state/collection.json`
# is the collection editor's output. When a module gains write access to
# `state/`, this guard is EXTENDED rather than copied: the allowlist gets that
# one file and the two canonical files keep exactly the sinks they have.
WRITE_SINK_NAME = "COLLECTION_PATH"

# Check 4's mutator set, deliberately NARROWER than `MUTATORS`. Bare `write`
# and `replace` are excluded because in this package they are
# `self.wfile.write(...)` (the HTTP response body), `sys.stderr.write(...)` and
# `dataclasses.replace(...)` — none of which touch the filesystem, and treating
# them as writes would make check 4 fire constantly and get relaxed. The file
# cases they would have covered are already covered elsewhere: `open(p, "w")`
# by check 3's blanket scan, and both canonical files by checks 1 and 2's taint.
FS_MUTATORS = frozenset(
    {
        "write_text",
        "write_bytes",
        "unlink",
        "touch",
        "mkdir",
        "rmdir",
        "rename",
        "symlink_to",
        "hardlink_to",
        "chmod",
    }
)

# Method names that mutate. A tainted expression must never be the object
# of one of these, and `open` is included because `p.open("w")` is a write
# that check 3's `open(...)` scan does not see.
MUTATORS = frozenset(
    {
        "write",
        "writelines",
        "write_text",
        "write_bytes",
        "unlink",
        "touch",
        "rename",
        "replace",
        "mkdir",
        "rmdir",
        "chmod",
        "open",
        "truncate",
    }
)

READ_MODES = frozenset({"r", "rb", "rt", "br", "tr"})


def _web_files() -> list[Path]:
    # `wasmdemo/` is a standalone browser-demo build tool (setup.py/make_board.py
    # assemble WASM assets under that folder) -- NOT the request-serving server, so
    # the no-writes rule (server must not write protected paths on a request) does
    # not apply to it. Everything else under tools/web/ is in scope.
    # Test modules are not the server either: they build fixtures in pytest's tmp_path.
    return sorted(p for p in WEB_ROOT.rglob("*.py")
                  if p.resolve() != _SELF and "wasmdemo" not in p.parts
                  and not p.name.startswith("test_"))


def _parse(path: Path) -> ast.Module:
    # `utf-8-sig`, not `utf-8`: a BOM makes `ast.parse` raise a SyntaxError
    # on line 1, and a guard that fails with the wrong reason is a guard
    # whose real finding nobody reads -- a file with a BOM fails and looks
    # exactly like a caught write until the message is read.
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def _docstring_ids(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _mentions_protected(node: ast.AST, docstring_ids: set[int]) -> bool:
    """True when this subtree contains a protected basename as a real
    string constant (docstrings exempt), so prose may name the files."""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Constant)
            and isinstance(sub.value, str)
            and id(sub) not in docstring_ids
            and sub.value in PROTECTED_BASENAMES
        ):
            return True
    return False


# Calls and attributes that RETURN a path when given one. Taint follows
# these and nothing else -- which is the distinction that makes this guard
# useful rather than merely loud. `load_inputs(CARDS_PATH, ...)` returns
# card DATA, not a path; propagating taint through it would mark every
# downstream variable in the module and the check would fire on the
# solver call itself. The prohibition is about the two FILES, so the taint
# is about path-valued expressions only.
PATH_PRESERVING = frozenset(
    {"Path", "resolve", "absolute", "expanduser", "with_name", "with_suffix", "joinpath"}
)
PATH_ATTRS = frozenset({"parent", "parents"})


def _is_tainted(node: ast.AST | None, tainted: set[str], docstring_ids: set[int]) -> bool:
    """True when `node` evaluates to one of the two protected paths.

    Structural, and deliberately narrow: a literal basename, a name bound
    to such an expression, a `/` join involving either, or a
    path-preserving call/attribute on one. Not "any subtree that mentions
    a tainted name" -- see `PATH_PRESERVING`."""
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and id(node) not in docstring_ids and node.value in PROTECTED_BASENAMES
    if isinstance(node, ast.Name):
        return node.id in tainted
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _is_tainted(node.left, tainted, docstring_ids) or _is_tainted(
            node.right, tainted, docstring_ids
        )
    if isinstance(node, ast.Attribute):
        if node.attr in PATH_ATTRS:
            return _is_tainted(node.value, tainted, docstring_ids)
        return False
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name not in PATH_PRESERVING:
            return False
        if isinstance(node.func, ast.Attribute) and _is_tainted(
            node.func.value, tainted, docstring_ids
        ):
            return True
        return any(
            _is_tainted(arg, tainted, docstring_ids)
            for arg in list(node.args) + [kw.value for kw in node.keywords]
        )
    if isinstance(node, (ast.JoinedStr, ast.FormattedValue)):
        return any(
            _is_tainted(sub, tainted, docstring_ids)
            for sub in ast.iter_child_nodes(node)
        )
    return False


def _tainted_names(tree: ast.Module, docstring_ids: set[int]) -> set[str]:
    """Names bound to a protected path, to a fixed point so a chain of
    rebindings is followed."""
    tainted: set[str] = set()
    for _ in range(4):  # far past any real rebinding chain
        before = len(tainted)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None or not _is_tainted(value, tainted, docstring_ids):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                # Only a single Name target -- tuple unpacking of a call
                # result is data, not a path, and `a, b = load(...)` must
                # not taint `a`.
                if isinstance(target, ast.Name):
                    tainted.add(target.id)
        if len(tainted) == before:
            break
    return tainted


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return "<expr>"


def test_web_package_has_source_to_scan() -> None:
    """A guard that scans nothing passes vacuously: a pipeline reporting its
    own success proves nothing."""
    files = _web_files()
    assert files, f"no .py files found under {WEB_ROOT}"
    assert any(p.name == "server.py" for p in files), [p.name for p in files]


@pytest.mark.parametrize("path", _web_files(), ids=lambda p: p.name)
def test_protected_paths_reach_only_read_sinks(path: Path) -> None:
    """Check 1: a tainted expression may only be an argument to a
    whitelisted read sink."""
    tree = _parse(path)
    docstring_ids = _docstring_ids(tree)
    tainted = _tainted_names(tree, docstring_ids)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in READ_SINKS:
            continue
        args = list(node.args) + [kw.value for kw in node.keywords]
        for arg in args:
            if _is_tainted(arg, tainted, docstring_ids):
                offenders.append(f"line {node.lineno}: {name}(...) receives a protected path")
                break
    assert not offenders, (
        f"{path.name}: protected path reaches a non-read sink -> {offenders}. "
        f"Allowed sinks: {sorted(READ_SINKS)}"
    )


@pytest.mark.parametrize("path", _web_files(), ids=lambda p: p.name)
def test_protected_paths_are_never_mutated(path: Path) -> None:
    """Check 2: `CARDS_PATH.write_text(...)`, `OWNED_PATH.open("w")` and
    friends."""
    tree = _parse(path)
    docstring_ids = _docstring_ids(tree)
    tainted = _tainted_names(tree, docstring_ids)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in MUTATORS:
            continue
        if _is_tainted(node.func.value, tainted, docstring_ids):
            offenders.append(f"line {node.lineno}: .{node.func.attr}() on a protected path")
    assert not offenders, f"{path.name}: {offenders}"


def _write_offenders(tree: ast.AST) -> list[str]:
    """Check 4's scan, factored out so the guard's own guard can drive it
    against synthetic offenders the way it drives the other three."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in FS_MUTATORS:
            continue
        # Walk back to the root name so `COLLECTION_PATH.parent.mkdir(...)`
        # resolves to `COLLECTION_PATH` rather than to `parent`.
        root = func.value
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id == WRITE_SINK_NAME:
            continue
        offenders.append(
            f"line {node.lineno}: {getattr(root, 'id', type(root).__name__)}.{func.attr}(...)"
        )
    return offenders


@pytest.mark.parametrize("path", _web_files(), ids=lambda p: p.name)
def test_every_write_targets_the_one_allowed_path(path: Path) -> None:
    """Check 4: the only path `tools/web/` may mutate is `COLLECTION_PATH`.

    Checks 1 and 2 protect the two canonical files by taint — they answer
    "did *this* path reach a writer". This one is broader and blunter on
    purpose: it does not ask which path is being written, it asserts that
    every filesystem-mutating call in the package is rooted at the one
    constant allowed to be written at all. A write to a path laundered
    through a local, or to a brand-new constant nobody reviewed, fails here
    without anyone having had to add it to a blocklist first."""
    offenders = _write_offenders(_parse(path))
    assert not offenders, (
        f"{path.name}: {offenders} — the only writable path in tools/web/ "
        f"is {WRITE_SINK_NAME}"
    )


@pytest.mark.parametrize("path", _web_files(), ids=lambda p: p.name)
def test_every_open_call_is_read_mode(path: Path) -> None:
    """Check 3, blanket: nothing in `tools/web/` opens any file for
    writing. A bare `open(p)` defaults to "r" and is allowed; any explicit
    mode must be a read mode."""
    tree = _parse(path)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != "open":
            continue
        mode_node: ast.AST | None = None
        if len(node.args) >= 2:
            mode_node = node.args[1]
        for kw in node.keywords:
            if kw.arg == "mode":
                mode_node = kw.value
        if mode_node is None:
            continue  # default "r"
        if not (isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str)):
            offenders.append(f"line {node.lineno}: open() with a non-literal mode")
            continue
        if mode_node.value not in READ_MODES:
            offenders.append(f"line {node.lineno}: open(..., {mode_node.value!r})")
    assert not offenders, f"{path.name}: {offenders}"


def test_guard_actually_catches_a_write(tmp_path: Path) -> None:
    """The guard's own guard. A test that never fires is a test that has
    silently stopped working. Each of the
    three checks is run against a synthetic offender and must reject it."""
    docstring_ids: set[int]

    def offenders_for(source: str, which: str) -> list[str]:
        tree = ast.parse(source)
        nonlocal docstring_ids
        docstring_ids = _docstring_ids(tree)
        tainted = _tainted_names(tree, docstring_ids)
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if which == "sink":
                if _call_name(node) in READ_SINKS:
                    continue
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if _is_tainted(arg, tainted, docstring_ids):
                        found.append(_call_name(node))
                        break
            elif which == "mutator":
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in MUTATORS
                    and _is_tainted(node.func.value, tainted, docstring_ids)
                ):
                    found.append(node.func.attr)
            elif which == "open":
                if _call_name(node) == "open" and len(node.args) >= 2:
                    mode = node.args[1]
                    if isinstance(mode, ast.Constant) and mode.value not in READ_MODES:
                        found.append(str(mode.value))
        return found

    # Check 1: the path laundered through a module constant and a join.
    assert offenders_for(
        'P = ROOT / "data" / "cards.json"\njson.dump(obj, P)\n', "sink"
    ) == ["dump"]
    # Check 2: a mutator method on the tainted constant.
    assert offenders_for(
        'P = ROOT / "data" / "owned.json"\nP.write_text("{}")\n', "mutator"
    ) == ["write_text"]
    # Check 3: any write-mode open anywhere in the package.
    assert offenders_for('open(somewhere, "w")\n', "open") == ["w"]

    # Check 4, both halves — a check that rejects everything is as useless as
    # one that rejects nothing.
    assert _write_offenders(ast.parse('SOMEWHERE_ELSE.write_text("x")\n'))
    assert _write_offenders(ast.parse('p = Path("x")\np.unlink()\n'))
    assert _write_offenders(ast.parse('OUT_PATH.parent.mkdir()\n'))
    assert not _write_offenders(ast.parse(f'{WRITE_SINK_NAME}.write_text("x")\n'))
    assert not _write_offenders(
        ast.parse(f'{WRITE_SINK_NAME}.parent.mkdir(parents=True, exist_ok=True)\n')
    )
    # And the three non-filesystem writes this package genuinely makes must NOT
    # fire, or check 4 gets relaxed the first time it does.
    assert not _write_offenders(ast.parse("self.wfile.write(body)\n"))
    assert not _write_offenders(ast.parse('sys.stderr.write("x")\n'))
    assert not _write_offenders(ast.parse("dataclasses.replace(cfg, board=m)\n"))
    # And the legitimate shape must NOT be flagged by any of the three.
    ok = 'CARDS_PATH = ROOT / "data" / "cards.json"\nload_inputs(CARDS_PATH, OWNED_PATH, False)\n'
    assert offenders_for(ok, "sink") == []
    assert offenders_for(ok, "mutator") == []
    assert offenders_for(ok, "open") == []
