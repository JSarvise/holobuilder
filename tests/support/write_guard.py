"""The test sessions' write guard: no test may write into the checkout's real `state/` or `dumps/`.

They hold the account, this PC's server key and ~11 GB of data no test can rebuild. A Python audit
hook turns every write into one of them -- a file opened for writing (also the temp file of an
atomic write), a rename or replace onto one, a delete, a new directory -- into a PermissionError
that names the path. Reading stays allowed: many tests read the real data. Write to `tmp_path`
instead, monkeypatching the module constant that names the file.

Both conftest.py files (this repository's and the public tree's) call `install()` before any test
module is imported. The hook covers that process (the in-process web server included), not
subprocesses a test starts. `HB_TEST_ALLOW_REAL_WRITES=1` turns it off for one run, for a
deliberate re-record.
"""

from __future__ import annotations

import os
import sys

from hbuilder import paths

# each folder as written AND as resolved: a worktree links the real dumps/ with a junction,
# and a write through the link must be refused as surely as one into its target
GUARDED = tuple(
    dict.fromkeys(
        os.path.normcase(form) + os.sep
        for p in (paths.STATE, paths.DUMPS)
        for form in (str(p.resolve()), os.path.abspath(p))
    )
)
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def guarded(path) -> str | None:
    """The real path when `path` lies inside a guarded folder, else None."""
    if isinstance(path, int) or path is None:
        return None
    try:
        full = os.path.normcase(os.path.abspath(os.fsdecode(path)))
    except (TypeError, ValueError):
        return None
    return full if (full + os.sep).startswith(GUARDED) else None


def _refuse(what: str, path: str) -> None:
    raise PermissionError(
        f"test wrote into the real {what}: {path} -- use tmp_path and "
        "monkeypatch the constant that names the file (tests/support/write_guard.py)"
    )


def _hook(event: str, args: tuple) -> None:
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            mode is None and isinstance(flags, int) and flags & WRITE_FLAGS
        )
        if writing and (hit := guarded(path)):
            _refuse("data (open for writing)", hit)
    elif event == "os.rename":
        for p in args[:2]:
            if hit := guarded(p):
                _refuse("data (rename/replace)", hit)
    elif event in ("os.remove", "os.rmdir", "os.truncate", "shutil.rmtree"):
        if hit := guarded(args[0]):
            _refuse(f"data ({event})", hit)
    elif event == "os.mkdir":
        hit = guarded(args[0])
        if hit and not os.path.exists(hit):
            _refuse("data (new directory)", hit)


_installed = False


def install() -> None:
    """Install the hook once per process, unless HB_TEST_ALLOW_REAL_WRITES=1."""
    global _installed
    if _installed or os.environ.get("HB_TEST_ALLOW_REAL_WRITES") == "1":
        return
    sys.addaudithook(_hook)
    _installed = True
