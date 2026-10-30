"""The on-disk shape of a MEMBERS-FIRST enumeration, as ONE writer.

A members-first enumeration is stored, per (song, proxy), as:
    members_first_<song>_<proxy>.meta.json   — song, proxy, proxy_card,
        proxy_holomem, pool (card ids in the scores' index order), n_sets,
        order, chunk, inputs_hash, model_version, plan_cache_seed
        ({leader, members[5]}), budget, uptime, platform, git, started;
        `finished` + `seconds` once complete
    members_first_<song>_<proxy>.scores.bin  — n_sets × float64 (native,
        little-endian on every supported machine) in
        `hbuilder.solver.exact_row.legal_rosters(pool holomems, 5)` order
and `tools/web/members_first_index.py` reads them.

This module is the small writer the web's tests use to build FAKE
enumerations in a temp dir (`tools/web/test_no_writes.py` forbids any
file-writing call inside `tools/web/`, test files included — so the writer
lives on the recon side).
It never touches `data/` or `state/`.
"""
from __future__ import annotations

import array
import json
from pathlib import Path
from typing import Iterable, Mapping


def write_enumeration(directory: Path, *, song: str, proxy: str, pool: Iterable[str],
                      scores: Iterable[float], inputs_hash: str,
                      plan_cache_seed: Mapping | None = None, finished: bool = True,
                      n_sets: int | None = None, extra: Mapping | None = None) -> Path:
    """Write `members_first_<song>_<proxy>.{meta.json,scores.bin}` under
    `directory` (created if needed). `n_sets` defaults to `len(scores)`;
    pass a larger value with fewer scores to fake a run in progress;
    `finished=False` omits the `finished` stamp. Returns the meta path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    arr = array.array("d", [float(s) for s in scores])
    meta = {
        "song": song, "proxy": proxy, "pool": list(pool),
        "n_sets": len(arr) if n_sets is None else int(n_sets),
        "order": "exact_row.legal_rosters(pool holomems, 5) — lexicographic 5-combinations "
                 "of pool indices with distinct holomems",
        "inputs_hash": inputs_hash, "model_version": "objective-v2.2",
        "plan_cache_seed": dict(plan_cache_seed) if plan_cache_seed else None,
        "started": "2026-08-19T00:00:00",
    }
    if finished:
        meta["finished"] = "2026-08-19T01:00:00"
        meta["seconds"] = 3600.0
    if extra:
        meta.update(extra)
    stem = f"members_first_{song}_{proxy}"
    (directory / (stem + ".scores.bin")).write_bytes(arr.tobytes())
    meta_path = directory / (stem + ".meta.json")
    meta_path.write_bytes(json.dumps(meta, indent=1).encode("utf-8"))
    return meta_path


def write_text_file(path: Path, text: str) -> Path:
    """A plain text file (for a deliberately malformed meta in a test)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


__all__ = ["write_enumeration", "write_text_file"]
