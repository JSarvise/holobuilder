"""The worker count every process pool of the search starts with."""

from __future__ import annotations

import os


def default_workers() -> int:
    """Every logical core (SMT pays on this branch-heavy work: 16 logical cores
    beat 8 physical ones by about a third). `os.cpu_count()` can return `None`.

    `HBUILDER_WORKERS` overrides it: the desktop app's Settings, for a PC that must stay
    usable while it searches."""
    raw = os.environ.get("HBUILDER_WORKERS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return os.cpu_count() or 1

