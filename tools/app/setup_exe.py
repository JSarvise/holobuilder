"""The installer an update runs: holobuilder-setup.exe beside app/, kept at the release's own.

The app's Update button hands over to that exe, so whatever installer.py it was frozen from runs
the update. Each release's signed version.json names its own installer (`setup_url`,
`setup_sha256`; build_app.py), and the copy is brought up to it twice: by the app just before it
hands over (launcher.update_now), and by every install or update run (installer.py). A fix to the
update path then reaches installed apps with the next release, instead of never.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path

NAME = "holobuilder-setup.exe"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bring_current(
    folder: Path, meta: dict, fetch: Callable[[str], bytes], *, running: Path | None = None
) -> str:
    """Make `folder`/NAME the installer `meta` (a verified version.json) names. Answers what
    happened: "current" (already it), "replaced", "copied" (a release that names no installer:
    `running`, the exe of this run, is copied in when it is not already that file) or "kept".
    Raises when the download does not match the signed hash; the exe in place then stays."""
    target = folder / NAME
    want = meta.get("setup_sha256")
    if not want:
        if running is not None and running.is_file():
            if not target.exists() or running.resolve() != target.resolve():
                shutil.copy2(running, target)
                return "copied"
        return "kept"
    if target.is_file() and sha256_of(target) == want:
        return "current"
    data = fetch(meta.get("setup_url") or f"app/{NAME}")
    if hashlib.sha256(data).hexdigest() != want:
        raise ValueError("the installer download does not match the release's signed sha256")
    fresh = folder / f".{NAME}.{os.getpid()}.new"
    try:
        fresh.write_bytes(data)
        if target.exists():
            # Windows refuses to overwrite a running program but lets it be renamed: the exe
            # running this update moves aside, and a later run deletes it (`remove_aside`)
            os.replace(target, folder / f".{NAME}.{os.getpid()}.old")
        os.replace(fresh, target)
    finally:
        fresh.unlink(missing_ok=True)
    return "replaced"


def remove_aside(folder: Path) -> None:
    """Delete the installers earlier runs moved aside (a still running one stays)."""
    for p in folder.glob(f".{NAME}.*.old"):
        try:
            p.unlink()
        except OSError:
            pass
