"""How the search's C kernels are compiled and named, on Windows and Linux alike.

The flags are part of the contract, not a preference: `-ffp-contract=off` (no fused
multiply-add, which would round differently) and no `-ffast-math`, so every kernel is
bit-identical to the Python it replaces, on either platform.

The library is `<name>.dll` on Windows and `lib<name>.so` elsewhere: a `<name>.so` next to
`<name>.py` would be taken by Python's import system as an extension module of the same name.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

CFLAGS = ["-O2", "-ffp-contract=off", "-fno-fast-math", "-shared", "-fPIC"]


def library(folder: Path, name: str, platform: str = sys.platform) -> Path:
    """Where kernel `name`'s compiled library lives."""
    return folder / (f"{name}.dll" if platform == "win32" else f"lib{name}.so")


def build(source: Path, target: Path, what: str, force: bool = False) -> Path:
    """Compile `source` into `target` when it is missing or older than its source.

    The compiler writes a private file that then replaces `target` in one step, so processes
    building at once (the server and its spawned workers) never load a half-written library.
    When the replace is refused because another process already holds a fresh `target` open
    (Windows), that one is used."""
    if not force and target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    gcc = shutil.which("gcc")
    if gcc is None:
        # no compiler (the desktop app on a user's PC): the library shipped with these sources
        # is theirs, whatever the order an update wrote the files in (an extraction stamps each
        # file as it goes, so a source can come out a few ms newer than its library)
        if target.exists():
            return target
        raise RuntimeError(f"no gcc on PATH; the {what} kernel cannot be built")
    tmp = target.with_name(f"{target.stem}.{os.getpid()}.tmp{target.suffix}")
    try:
        subprocess.run([gcc, *CFLAGS, "-o", str(tmp), str(source)], check=True)
        try:
            os.replace(tmp, target)
        except PermissionError:
            if not (target.exists() and target.stat().st_mtime >= source.stat().st_mtime):
                raise
    finally:
        tmp.unlink(missing_ok=True)
    return target
