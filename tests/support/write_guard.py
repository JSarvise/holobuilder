"""The test sessions' write guard: no test may write into the checkout's real `state/` or `dumps/`,
nor `data/owned.json` or `data/cards.json`.

They hold the account, this PC's server key, ~11 GB of data no test can rebuild, and the two files
with the strictest rules of all (the account's owned cards, append-only; the catalogue, whose
entries carry hand corrections). A Python audit
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
# the two files of data/ under the strictest rules (data/ itself holds fixtures tests may write)
GUARDED_FILES = frozenset(
    os.path.normcase(form)
    for p in (paths.DATA / "owned.json", paths.DATA / "cards.json")
    for form in (str(p.resolve()), os.path.abspath(p))
)
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def guarded(path, dir_fd=None) -> str | None:
    """The real path when `path` lies inside a guarded folder or is a guarded file, else None.
    A relative `path` with a `dir_fd` (how `shutil.rmtree` deletes on Linux) is relative to that
    folder, not to the working directory; when the folder cannot be named, it is not ours to judge."""
    if isinstance(path, int) or path is None:
        return None
    try:
        name = os.fsdecode(path)
        if isinstance(dir_fd, int) and not os.path.isabs(name):
            try:
                name = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), name)
            except OSError:
                return None
        full = os.path.normcase(os.path.abspath(name))
    except (TypeError, ValueError):
        return None
    return full if (full + os.sep).startswith(GUARDED) or full in GUARDED_FILES else None


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
        fds = (tuple(args) + (None, None, None, None))[2:4]
        for p, fd in zip(args[:2], fds):
            if hit := guarded(p, fd):
                _refuse("data (rename/replace)", hit)
    elif event in ("os.remove", "os.rmdir"):
        if hit := guarded(args[0], args[1] if len(args) > 1 else None):
            _refuse(f"data ({event})", hit)
    elif event in ("os.truncate", "shutil.rmtree"):
        if hit := guarded(args[0]):
            _refuse(f"data ({event})", hit)
    elif event == "os.mkdir":
        hit = guarded(args[0], args[2] if len(args) > 2 else None)
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
