"""What the page shows from the two-stage answers cache (`dumps/two_stage/`,
built by `tools/recon/proxy/two_stage_build.py` after every refresh): the best
(leader, five members) for one song, or in general when no song is selected.

This module only READS the cache and says how far to trust what it read:

    state "fresh"    the answer's key is today's inputs' key
    state "stale"    there is an answer, computed from OTHER inputs (a card or a
                     chart changed since) — shown, and labelled as such
    state "missing"  nothing computed for this song yet

and never calls it certified: these are SEARCHED answers.
`payload(song)` is the whole JSON of `GET /api/two-stage`.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable

_PROXY = Path(__file__).resolve().parents[1] / "recon" / "proxy"
if str(_PROXY) not in sys.path:
    sys.path.insert(0, str(_PROXY))

import two_stage_build as build  # noqa: E402

ROWS = 8
# the global ranking (top teams across leaders) served by default; `?top=` up to the
# cached size (50 in `two_stage_build.SETTINGS["ranking"]`)
RANKING = 50
# the row fields the builder writes on the first ORDER_ROWS rows / ranking entries
# (version 4); absent (null) in older files
_QUANTILES = ("p10", "p50", "p90", "song_max", "dist_mean", "dist_verified")


def _team(r: dict) -> dict:
    """One served team (a per-leader row or a global-ranking entry)."""
    out = {"leader": r.get("leader"), "leader_name": r.get("leader_name"),
           "sings": bool(r.get("sings")), "status": r.get("status"), "best": r.get("best"),
           "best_plain": r.get("best_plain"),
           "members": r.get("members"), "member_names": r.get("member_names"),
           # the slot order (null in older files, below the first ORDER_ROWS
           # and in the general answer,
           # where it is per song -- POST /api/row-detail computes it for any row)
           "member_order": r.get("member_order"),
           "refined": bool(r.get("refined"))}
    out.update({k: r.get(k) for k in _QUANTILES})
    return out


_KEYS_TTL = 20.0
_keys_cache: dict[str, tuple[float, tuple]] = {}


def current_keys(basis: str = "maxed",
                 difficulty: str = "EXPERT") -> tuple[dict[str, str], str] | None:
    """`(key per song, general key)` for today's inputs on `basis`, or `None`
    when they cannot be read. Recomputed at most every `_KEYS_TTL` seconds: it
    re-reads and digests the input files (~0.5 s), and the page asks on every
    song change."""
    now = time.monotonic()
    slot = basis if difficulty == "EXPERT" else f"{basis}/{difficulty}"
    held = _keys_cache.get(slot)
    if held is not None and now - held[0] < _KEYS_TTL:
        return held[1]
    try:
        keys = build.today_keys(basis, difficulty)
    except Exception:  # noqa: BLE001 - "cannot vouch" is an answer, not a crash
        return None
    _keys_cache[slot] = (now, keys)
    return keys


def payload(song: str | None, *, basis: str = "maxed",
            keys: Callable[[], tuple | None] | None = None,
            cache_dir: Path | None = None, difficulty: str = "EXPERT",
            top: int = RANKING) -> dict:
    """The JSON for one song (`song` a music id) or, with no song, in general,
    on `basis` ("maxed" | "account"; anything else is "maxed"), at `difficulty`
    (EXPERT by default: the plain files; another difficulty reads its own
    `*_<difficulty>.json`, built by `two_stage_build.py --difficulty`)."""
    cache_dir = cache_dir or build.CACHE
    if basis not in build.BASES:
        basis = "maxed"
    difficulty = build._difficulty(difficulty)
    if keys is None:
        def keys(basis=basis):
            return current_keys(basis, difficulty)
    song = (song or "").strip()
    if song and not all(ch.isalnum() or ch in "_-" for ch in song):
        return {"scope": "song", "song": song, "state": "missing", "error": "not a music id"}
    cached = build._read(cache_dir / (build.song_file(song, basis, difficulty) if song
                                      else build.general_file(basis, difficulty)))
    status = build._read(cache_dir / "status.json") or {}
    running = status.get("state") == "running" and build._alive(int(status.get("pid", 0) or 0))
    out: dict = {
        "scope": "song" if song else "general",
        "song": song or None,
        "certified": False,
        "basis_id": basis,
        "basis": build.BASIS_TEXT[basis],
        "difficulty": difficulty,
        "build": {"running": running, "stage": status.get("stage") if running else None,
                  "basis": status.get("basis") if running else None,
                  "done": status.get("done") if running else None,
                  "total": status.get("total") if running else None,
                  "last_state": status.get("state"), "updated": status.get("updated")},
    }
    if cached is None:
        out["state"] = "missing"
        return out
    today = keys()
    if today is None:
        fresh = None
    else:
        fresh = cached.get("key") == (today[0].get(song) if song else today[1])
    out["state"] = "fresh" if fresh else ("stale" if fresh is False else "unverified")
    stats = cached.get("stats") or {}
    out.update({
        "written": cached.get("written"),
        "objective": cached.get("objective") or f"expected score on this song's {difficulty} chart",
        "method": cached.get("method"),
        "singers": cached.get("singers"), "singer_type": cached.get("singer_type"),
        "charts": cached.get("charts"),
        "searched_leaders": stats.get("searched"), "leaders": stats.get("leaders"),
        "legal_sets": stats.get("legal_sets"),
        "exact_evals": stats.get("exact_evals") or stats.get("sample_exact_evals"),
        # one row per leader (the secondary view), each with its next sets
        "rows": [{**_team(r), "alternatives": r.get("alternatives") or []}
                 for r in (cached.get("rows") or [])[:ROWS]],
        # the GLOBAL top teams across leaders, best first (song answers built
        # by two-stage-build-4 on; null when the file predates it)
        "ranking": ([_team(r) for r in cached["ranking"][:max(1, int(top))]]
                    if isinstance(cached.get("ranking"), list) else None),
        "ranking_size": len(cached["ranking"]) if isinstance(cached.get("ranking"), list) else None,
    })
    return out


def singers_payload(*, basis: str = "maxed", keys: Callable[[], tuple | None] | None = None,
                    cache_dir: Path | None = None, difficulty: str = "EXPERT") -> dict:
    """The per-singer answers (`GET /api/two-stage?scope=singers`): for every
    character, the best team with her as leader over the songs she sings. Same
    three states as `payload`, judged by the key alone."""
    cache_dir = cache_dir or build.CACHE
    if basis not in build.BASES:
        basis = "maxed"
    difficulty = build._difficulty(difficulty)
    if keys is None:
        def keys(basis=basis):
            return current_keys(basis, difficulty)
    out: dict = {"scope": "singers", "certified": False, "basis_id": basis,
                 "basis": build.BASIS_TEXT[basis], "difficulty": difficulty}
    cached = build._read(cache_dir / build.singers_file(basis, difficulty))
    if cached is None:
        out["state"] = "missing"
        return out
    today = keys()
    fresh = None if today is None else cached.get("key") == build.singers_key(today[1])
    out["state"] = "fresh" if fresh else ("stale" if fresh is False else "unverified")
    out.update({
        "written": cached.get("written"), "method": cached.get("method"),
        "rows": [{"chr": r.get("chr"), "name": r.get("name"), "songs": len(r.get("songs") or []),
                  "leader": r.get("leader"), "leader_name": r.get("leader_name"), "best": r.get("best"),
                  "members": r.get("members"), "member_names": r.get("member_names")}
                 for r in (cached.get("rows") or [])],
    })
    return out
