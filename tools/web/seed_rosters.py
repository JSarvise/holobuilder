"""Read-only SEED ROSTERS for the web's develop/refine candidate set.

Why: the web's candidate chain — beam
`solve_heuristic_labelled(per_leader_best=True)` → top-60 + every 5★ leader →
develop → refine top-N — can fail to GENERATE the best known general roster
(e.g. Shirogane Noel Soleil Kiss as leader+member with Matsuri VCS, Flare EGG,
Fubuki KR, Kobo RDO), although the same evaluator scores it above the web's #1
whenever it is present. That is a candidate-generation RECALL failure, not a
scoring bug — so the remedy is more candidates, scored the same way. This module
supplies them: rosters the recon artefacts already proved strong, UNIONED with
the beam's teams before the develop, developed and ranked exactly like any
other candidate. Nothing here changes the ranking semantics; a seed wins only
if the run's own evaluator says so.

Sources (all read-only, each file parsed once per (mtime, size)):
  tools/web/seed_rosters.json
      hand-editable, committed: [{leader, members[5], note, source}, ...]
  RESEARCH-2026-08-18/PHASE-6/per_leader/<song>/per_leader_table_<song>.json
      every row (`leader`, `ids` = the 5 members), whatever its status —
      each is the best (or best-known) roster for that leader on that song
  RESEARCH-2026-08-18/PHASE-6/across_ranking_*.json
      the `top` rows (`leader`, `members`)

Rules (enforced by `filter_seeds`, applied by the server):
  * a seed enters only if all six cards exist in the run's card pool (basis,
    `pool=5star`, Ban/exclude, Must-include, leader pin) and form a legal Team —
    a seed that violates a constraint is DROPPED, never forced;
  * dedupe by (leader, member SET) against the beam's teams and among seeds;
  * every seeded row carries `provenance: "seed:<source>"`.

Nothing here writes a file.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
RESEARCH = REPO / "tools/recon/RESEARCH-2026-08-18"
DEFAULT_SEED_FILE = Path(__file__).resolve().parent / "seed_rosters.json"
DEFAULT_TABLE_ROOTS = (RESEARCH / "PHASE-6" / "per_leader",)
DEFAULT_ACROSS_ROOTS = (RESEARCH / "PHASE-6",)
_LISTING_TTL = 2.0   # seconds between directory listings

MEMBER_COUNT = 5


@dataclasses.dataclass(frozen=True)
class SeedRoster:
    leader: str
    members: tuple[str, ...]
    source: str          # short provenance, e.g. "seed_rosters.json#holodori-v3.1.1"
    note: str = ""

    @property
    def key(self) -> tuple[str, frozenset[str]]:
        return (self.leader, frozenset(self.members))

    @property
    def provenance(self) -> str:
        return f"seed:{self.source}"


def _rel(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return str(path)


def _roster(leader, members, source: str, note: str = "") -> SeedRoster | None:
    if not isinstance(leader, str) or not leader:
        return None
    if not isinstance(members, (list, tuple)) or len(members) != MEMBER_COUNT:
        return None
    ms = tuple(str(m) for m in members)
    if not all(ms):
        return None
    return SeedRoster(leader=leader, members=ms, source=source, note=str(note or ""))


def _from_seed_file(doc, path: Path) -> list[SeedRoster]:
    """`seed_rosters.json`: a list of {leader, members[5], note, source}."""
    if not isinstance(doc, list):
        return []
    out = []
    for i, row in enumerate(doc):
        if not isinstance(row, dict):
            continue
        tag = str(row.get("source") or row.get("note") or f"row{i}")
        r = _roster(row.get("leader"), row.get("members"),
                    f"{path.name}#{tag}", row.get("note", ""))
        if r is not None:
            out.append(r)
    return out


def _from_table(doc, path: Path) -> list[SeedRoster]:
    """`per_leader_table_<song>.json`: rows with `leader` and `ids` (5 members)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("rows"), list):
        return []
    song = str(doc.get("song") or path.parent.name)
    out = []
    for row in doc["rows"]:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").lower() or "row"
        r = _roster(row.get("leader"), row.get("ids"),
                    f"per_leader/{song}:{status}", str(row.get("leader_name") or ""))
        if r is not None:
            out.append(r)
    return out


def _from_across(doc, path: Path) -> list[SeedRoster]:
    """`across_ranking_*.json`: the `top` rows with `leader` and `members`."""
    if not isinstance(doc, dict) or not isinstance(doc.get("top"), list):
        return []
    stem = path.stem  # across_ranking_holodori
    out = []
    for row in doc["top"]:
        if not isinstance(row, dict):
            continue
        r = _roster(row.get("leader"), row.get("members"),
                    f"{stem}#{row.get('rank', '?')}", str(row.get("label") or ""))
        if r is not None:
            out.append(r)
    return out


class SeedIndex:
    """Scans the seed sources lazily; every file is parsed once per
    (mtime, size). Missing files or directories are simply no seeds."""

    def __init__(self, seed_file: Path | None = DEFAULT_SEED_FILE,
                 table_roots: Iterable[Path] = DEFAULT_TABLE_ROOTS,
                 across_roots: Iterable[Path] = DEFAULT_ACROSS_ROOTS) -> None:
        self.seed_file = Path(seed_file) if seed_file is not None else None
        self.table_roots = tuple(Path(p) for p in table_roots)
        self.across_roots = tuple(Path(p) for p in across_roots)
        self._files: dict[Path, tuple[tuple[float, int], list[SeedRoster]]] = {}
        self._listed_at = 0.0
        self._paths: list[tuple[str, Path]] = []

    def _list(self, force: bool = False) -> list[tuple[str, Path]]:
        now = time.monotonic()
        if not force and self._paths and now - self._listed_at < _LISTING_TTL:
            return self._paths
        paths: list[tuple[str, Path]] = []
        if self.seed_file is not None:
            paths.append(("file", self.seed_file))
        for root in self.table_roots:
            if root.is_dir():
                paths.extend(("table", p) for p in sorted(root.glob("*/per_leader_table_*.json")))
                paths.extend(("table", p) for p in sorted(root.glob("per_leader_table_*.json")))
        for root in self.across_roots:
            if root.is_dir():
                paths.extend(("across", p) for p in sorted(root.glob("across_ranking_*.json")))
        self._paths, self._listed_at = paths, now
        return paths

    def _entries(self, kind: str, path: Path) -> list[SeedRoster]:
        try:
            st = os.stat(path)
        except OSError:
            self._files.pop(path, None)
            return []
        stamp = (st.st_mtime, st.st_size)
        cached = self._files.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            doc = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            self._files[path] = (stamp, [])
            return []
        if kind == "file":
            entries = _from_seed_file(doc, path)
        elif kind == "table":
            entries = _from_table(doc, path)
        else:
            entries = _from_across(doc, path)
        self._files[path] = (stamp, entries)
        return entries

    def refresh(self) -> int:
        return sum(len(self._entries(k, p)) for k, p in self._list(force=True))

    def seeds(self, scope: str = "all") -> list[SeedRoster]:
        """`scope`: "all" (every source) | "file" (only seed_rosters.json).
        Deduped by (leader, member set), first source wins (the hand file
        is listed first, so its provenance names a roster it also lists)."""
        out: list[SeedRoster] = []
        seen: set[tuple[str, frozenset[str]]] = set()
        for kind, path in self._list():
            if scope == "file" and kind != "file":
                continue
            for r in self._entries(kind, path):
                if r.key in seen:
                    continue
                seen.add(r.key)
                out.append(r)
        return out


_INDEX: SeedIndex | None = None


def default_index() -> SeedIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = SeedIndex()
    return _INDEX


def filter_seeds(
    seeds: Iterable[SeedRoster],
    *,
    pool_ids: Iterable[str],
    exclude: Iterable[str] = (),
    require: Iterable[str] = (),
    leader: Iterable[str] = (),
    taken: Iterable[tuple[str, frozenset[str]]] = (),
) -> list[SeedRoster]:
    """The seeds a run may develop: all six cards in `pool_ids` (the run's
    candidate universe — basis, 5★ filter), none banned (`exclude`), every
    must-include card present (`require`), the leader among `leader` when
    pinned, and the roster not already in `taken` (the beam's (leader,
    member-set) keys). Order preserved; a dropped seed is dropped silently —
    never forced, never an error."""
    pool = set(pool_ids)
    ban = set(exclude)
    must = set(require)
    pins = set(leader)
    seen = set(taken)
    out: list[SeedRoster] = []
    for s in seeds:
        ids = (s.leader, *s.members)
        if any(i not in pool for i in ids):
            continue
        if any(i in ban for i in ids):
            continue
        if must and not must.issubset(set(ids)):
            continue
        if pins and s.leader not in pins:
            continue
        if s.key in seen:
            continue
        seen.add(s.key)
        out.append(s)
    return out


def build_seed_teams(
    seeds: Sequence[SeedRoster],
    by_id: Mapping[str, object],
    make_team: Callable[[object, tuple], object],
) -> list[tuple[object, SeedRoster]]:
    """Materialise `Team`s through `make_team(leader_card, member_cards)`; a
    seed that raises (an illegal roster under the domain's rules) is skipped."""
    out = []
    for s in seeds:
        try:
            team = make_team(by_id[s.leader], tuple(by_id[m] for m in s.members))
        except Exception:  # noqa: BLE001 — TeamError / KeyError: not a legal seed
            continue
        out.append((team, s))
    return out
