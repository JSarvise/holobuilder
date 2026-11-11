"""Whole-file writes that never leave a half-written file behind.

`Path.write_text` truncates the target and then writes it: a crash, a full disk or a
Windows refusal in between leaves the file empty or cut short. For the refresh's outputs
that is the user's account (`state/collection.json`), the catalogue (`data/cards.json`), the
credential, and every dump the web reads: every refresh script writes them through here. On Windows, opening a file for writing can also fail with `OSError: [Errno 22]
Invalid argument` while another process (e.g. a second refresh) holds it for a moment.

Here the bytes go to a uniquely named temp file next to the target (same directory, so
the rename never crosses a volume), are flushed to disk, and then `os.replace` swaps it in
-- atomic on NTFS and POSIX alike: a reader sees the old file or the new one, never a
mix. A refused replace (a reader holding the target, an antivirus scan) is retried for a
few seconds before giving up; the temp file is removed on failure.

`write_text` opens the temp file exactly as `Path.write_text` opens its target (text mode,
`newline=None`), so a file's line endings on disk do not change by switching to it.

`private=True` (the account credential, the game's keys) makes the file readable by its owner
only on Linux and macOS (mode 600, set on the temp file before a byte is written); Windows keeps
a per-user folder's own permissions.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

ATTEMPTS = 6


def _temp_for(path: Path) -> Path:
    # unique per writer: two writers of one path never share (and clobber) a temp file
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")


def _replace(tmp: Path, path: Path, attempts: int) -> None:
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5 * (attempt + 1))


def _write(path, payload, mode: str, attempts: int, private: bool = False, **open_kwargs) -> None:
    path = Path(path)
    tmp = _temp_for(path)
    try:
        with open(tmp, mode, **open_kwargs) as handle:
            if private and os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace(tmp, path, attempts)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_bytes(path, data: bytes, *, attempts: int = ATTEMPTS, private: bool = False) -> None:
    """`Path(path).write_bytes(data)`, atomically."""
    _write(path, data, "wb", attempts, private)


def write_text(
    path,
    text: str,
    encoding: str | None = "utf-8",
    errors: str | None = None,
    *,
    attempts: int = ATTEMPTS,
    private: bool = False,
) -> None:
    """`Path(path).write_text(text, encoding, errors)`, atomically (same newline
    translation as `Path.write_text`)."""
    _write(path, text, "w", attempts, private, encoding=encoding, errors=errors)
