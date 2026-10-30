"""Read-only index of the cached MEMBERS-FIRST enumerations (one bare-proxy
score per legal member 5-set of a song; the file-free core is in
`hbuilder.solver.members_first`).

An enumeration is the pair
    members_first_<song>_<proxy>.meta.json   (song, proxy, pool ids in the
        scores' index order, n_sets, inputs_hash, model_version,
        plan_cache_seed, budget/uptime/platform, started/finished)
    members_first_<song>_<proxy>.scores.bin  (n_sets × float64, little-endian
        native, in `exact_row.legal_rosters(pool holomems, 5)` order)
under `RESEARCH-2026-08-18/PHASE-6/members_first/` and, optionally, every
directory in `HBUILDER_MEMBERS_FIRST_DIR` (os.pathsep-separated). It is
COMPLETE only when the meta carries `finished` and the scores file holds
exactly `n_sets` doubles — a run in progress (the enumeration is resumable
per chunk) is listed as incomplete and never used.

Keyed by (song, inputs_hash, pool ids). The "same instant, same basis" guard
is the enumeration's OWN key (`certificate.enumeration_key` /
`reference_key`): an enumeration's bare scores depend on cards / collection /
board / skilltree / master_stn / cfg / model_version and on the ONE chart it
was scored on, never on the other charts — so the whole-file `inputs_hash`
(which digests charts.json as a whole and changes the day a new song lands)
is NOT the guard. A meta stamped with `enumeration_key` (+
`reference_key`, `chart_digest`, `inputs_hash_files`) is usable as the run's
OWN source iff its `enumeration_key` equals the run's (the run's per-file
digests from `inputs_hash_files` + the run song's `chart_digest`), and as a
TRANSFER reference iff its `reference_key` (the non-chart part) equals the
run's — a new song appended to charts.json retires nothing. A mismatch gets
NOTHING and a reason naming the FILE that differs (cards.json, board.json, the
chart of the song, …) — the bare scores were computed on other inputs and
would seed the wrong rosters. A LEGACY meta (no `enumeration_key`), or a run
that carries no per-file digests, falls back to the whole-hash guard and
the reason says so. A pool that differs (the run's 5★ universe is not the
enumeration's) is refused the same way.

CROSS-SONG TRANSFER (`sources`). When the run's song has no enumeration of
its own (or its own differs in hash / pool / budget), every COMPLETE
enumeration of ANY OTHER song on the same `inputs_hash`, the same pool and
the same flat budget is a REFERENCE: its bare ranking supplies the candidate
tiers, which are then re-scored on the run's song under each real leader
(in one check, six exact rows of one song reproduced from another song's
enumeration, 6/6 at K=500, 0 loss, 1-3 s per leader). The guard is the
`reference_key` (same cards / collection / board / skilltree / master_stn /
cfg / model — the chart excluded), so a charts.json that gains a song retires
NO reference; legacy metas without the key fall back to the whole hash (which,
digesting charts.json as a whole, is the same for two songs on the same
inputs — but changes the day a song lands). With several
references their tiers are UNIONED; they must share one `plan_cache_seed`
(the leaf the web re-scores on), and the largest such group wins. K defaults
to `DEFAULT_K_TRANSFER` (500) in transfer mode and `DEFAULT_K` (2,000) when
the song's own enumeration exists; the answer is HEURISTIC either way — EXACT
only through the certificate guard. The response says `mode: "own" |
"transfer"`, the `sources`, the `k`, and `measured_recall` — whatever
`members_first_<song>_transfer.json` reports exist in the scanned
directories, summarised, never invented.

Loading (lazy, once per process per enumeration; one sample with 8,441,052
sets, a 66-card pool, python 3.13, on a machine running another 10-worker job
— uncontended figures are lower):
    scores 67.5 MB read                                              0.02 s
    sort order (8.4 M ints, `sorted(range(n), key=scores.__getitem__)`,
        kept as array('I') = 33.8 MB)                                  3.5 s
    tables (`members_first.tabulate`, per POOL, shared across songs:
        flat 42 MB + tcount 34 MB + gmask 68 MB + hmask 68 MB = 211 MB)  8–12 s
    total 15.4 s; working set +345 MB steady (67.5 scores + 33.8 order +
    211 tables + overhead), PEAK +~800 MB during the sort (the transient
    list of 8.4 M Python ints and their float keys).
`best_for_leaders` then costs, per leader, one `candidate_indices` scan
(milliseconds) plus one `batch_score` of the union of the three K-tiers
(~4,100 leaves at K=2,000, median).

Nothing here writes a file (`test_no_writes.py`: every `open` is read-mode).
"""
from __future__ import annotations

import array
import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import certificate as cert_mod
from hbuilder.solver import members_first as mf

REPO = Path(__file__).resolve().parents[2]
RESEARCH = REPO / "tools/recon/RESEARCH-2026-08-18"
DEFAULT_DIRS = (RESEARCH / "PHASE-6" / "members_first",)
ENV_DIRS = "HBUILDER_MEMBERS_FIRST_DIR"
DEFAULT_K = 2000            # the song's own enumeration: 66/66 EXACT rows on m0334
DEFAULT_K_TRANSFER = 500    # references from other songs: 6/6 on m0139 from m0334
_LISTING_TTL = 2.0   # seconds between directory listings
_META_SUFFIX = ".meta.json"
_SCORES_SUFFIX = ".scores.bin"
_TRANSFER_SUFFIX = "_transfer.json"

# `batch_score(leader_card, member_id_tuples) -> scores` — the caller's exact
# leaf (the web hands in a RefinePool on the enumeration's plan-cache seed)
BatchScore = Callable[[Any, Sequence[tuple[str, ...]]], Sequence[float]]


def _rel(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return str(path)


def env_dirs() -> tuple[Path, ...]:
    raw = os.environ.get(ENV_DIRS, "").strip()
    if not raw:
        return ()
    return tuple(Path(p.strip()) for p in raw.split(os.pathsep) if p.strip())


@dataclasses.dataclass(frozen=True)
class Enumeration:
    """One cached enumeration as the meta describes it (nothing loaded yet)."""

    song: str
    proxy: str
    proxy_card: str | None
    proxy_holomem: str | None
    inputs_hash: str | None
    model_version: str | None
    pool: tuple[str, ...]
    n_sets: int
    plan_cache_seed: tuple[str, tuple[str, ...]] | None
    budget: int | None
    uptime: str | None
    platform: str | None
    started: str | None
    finished: str | None
    seconds: float | None
    meta_path: Path
    scores_path: Path
    complete: bool
    problem: str | None = None      # why it is not usable, when it is not
    derived_from: dict | None = None    # an `update`-derived enumeration says so
    new_cards: tuple[str, ...] = ()
    sets_rescored: int | None = None
    # the enumeration's OWN validity key:
    # `enumeration_key` (non-chart lines + the song's chart) guards OWN use,
    # `reference_key` (non-chart lines only) guards TRANSFER use; None on a
    # legacy meta -> the whole-hash guard
    enumeration_key: str | None = None
    reference_key: str | None = None
    chart_digest: str | None = None
    inputs_hash_files: dict | None = None

    @property
    def legacy(self) -> bool:
        """No `enumeration_key` on the meta: guarded by the whole `inputs_hash`."""
        return self.enumeration_key is None

    @property
    def key(self) -> tuple[str, str | None, tuple[str, ...]]:
        return (self.song, self.inputs_hash, self.pool)

    def summary(self) -> dict:
        return {
            "song": self.song, "proxy": self.proxy, "proxy_card": self.proxy_card,
            "proxy_holomem": self.proxy_holomem, "inputs_hash": self.inputs_hash,
            "inputs_hash_short": (self.inputs_hash or "")[:12] or None,
            "model_version": self.model_version, "pool_size": len(self.pool),
            "enumeration_key": self.enumeration_key, "reference_key": self.reference_key,
            "chart_digest": self.chart_digest, "guard": "whole-hash (legacy meta)"
            if self.legacy else "enumeration_key",
            "n_sets": self.n_sets, "plan_cache_seed": (
                {"leader": self.plan_cache_seed[0], "members": list(self.plan_cache_seed[1])}
                if self.plan_cache_seed else None),
            "budget": self.budget, "uptime": self.uptime, "platform": self.platform,
            "started": self.started, "finished": self.finished, "seconds": self.seconds,
            "meta": _rel(self.meta_path), "scores": _rel(self.scores_path),
            "complete": self.complete, **({"problem": self.problem} if self.problem else {}),
            **({"derived_from": {k: self.derived_from.get(k) for k in
                                 ("inputs_hash", "n_sets", "pool_size", "finished", "reuse_basis")},
                "new_cards": list(self.new_cards), "sets_rescored": self.sets_rescored}
               if self.derived_from else {}),
        }


def _parse_meta(doc: Any, meta_path: Path) -> Enumeration | None:
    if not isinstance(doc, dict):
        return None
    song = str(doc.get("song") or "").strip()
    pool = doc.get("pool")
    if not song or not isinstance(pool, list) or not all(isinstance(p, str) for p in pool):
        return None
    try:
        n_sets = int(doc.get("n_sets") or 0)
    except (TypeError, ValueError):
        return None
    seed = doc.get("plan_cache_seed")
    pcs = None
    if isinstance(seed, dict) and isinstance(seed.get("leader"), str) \
            and isinstance(seed.get("members"), list):
        pcs = (seed["leader"], tuple(str(m) for m in seed["members"]))
    stem = meta_path.name[:-len(_META_SUFFIX)]
    scores_path = meta_path.with_name(stem + _SCORES_SUFFIX)
    proxy = str(doc.get("proxy") or stem.rsplit("_", 1)[-1])
    finished = doc.get("finished")
    problem = None
    complete = True
    if not finished:
        complete, problem = False, "enumeration not finished (meta has no `finished`)"
    else:
        try:
            size = scores_path.stat().st_size
        except OSError:
            size = -1
        if size != n_sets * 8:
            complete = False
            problem = (f"scores file holds {size // 8 if size >= 0 else 'no'} of "
                       f"{n_sets:,} doubles ({_rel(scores_path)})")
    if n_sets <= 0:
        complete, problem = False, "n_sets is 0"
    sec = doc.get("seconds")
    files = doc.get("inputs_hash_files")
    files = {str(k): str(v) for k, v in files.items()} if isinstance(files, dict) else None
    ekey = str(doc["enumeration_key"]) if doc.get("enumeration_key") else None
    rkey = str(doc["reference_key"]) if doc.get("reference_key") else None
    if ekey is not None and rkey is None and files and doc.get("model_version"):
        try:    # a meta stamped with the own key but not the reference one
            rkey = cert_mod.reference_key(files, str(doc["model_version"]))
        except KeyError:
            rkey = None
    derived = doc.get("derived_from")
    new_cards = doc.get("new_cards")
    rescored = doc.get("sets_rescored")
    return Enumeration(
        derived_from=dict(derived) if isinstance(derived, dict) else None,
        new_cards=tuple(str(x) for x in new_cards) if isinstance(new_cards, list) else (),
        sets_rescored=int(rescored) if isinstance(rescored, int) else None,
        song=song, proxy=proxy, proxy_card=doc.get("proxy_card"),
        proxy_holomem=doc.get("proxy_holomem"),
        inputs_hash=str(doc["inputs_hash"]) if doc.get("inputs_hash") else None,
        model_version=doc.get("model_version"), pool=tuple(pool), n_sets=n_sets,
        plan_cache_seed=pcs,
        budget=doc.get("budget"), uptime=doc.get("uptime"), platform=doc.get("platform"),
        started=doc.get("started"), finished=finished,
        seconds=float(sec) if isinstance(sec, (int, float)) else None,
        meta_path=meta_path, scores_path=scores_path, complete=complete, problem=problem,
        enumeration_key=ekey, reference_key=rkey,
        chart_digest=str(doc["chart_digest"]) if doc.get("chart_digest") else None,
        inputs_hash_files=files)


@dataclasses.dataclass(frozen=True)
class RunKeys:
    """The run's side of the guard (`MembersFirstIndex.run_keys`): the keys
    the enumerator stamps, computed from the run's per-file digests; `fallback`
    names why they could not be (then the whole `inputs_hash` guards)."""

    song: str
    inputs_hash: str | None
    enumeration_key: str | None
    reference_key: str | None
    chart_digest: str | None
    model_version: str
    inputs_hash_files: dict | None
    fallback: str | None

    def summary(self) -> dict:
        return {"song": self.song, "inputs_hash": self.inputs_hash,
                "enumeration_key": self.enumeration_key, "reference_key": self.reference_key,
                "chart_digest": self.chart_digest, "model_version": self.model_version,
                "guard": "enumeration_key" if self.reference_key else "whole-hash",
                **({"fallback": self.fallback} if self.fallback else {})}


@dataclasses.dataclass
class Loaded:
    """An enumeration in memory: the scores, the descending order, the
    per-set tables (shared per pool) — plus how long each step took."""

    enumeration: Enumeration
    scores: array.array
    order: array.array
    tables: mf.Tables
    seconds: dict[str, float]


class MembersFirstIndex:
    """Scans the enumeration directories lazily (each meta parsed once per
    mtime/size), loads an enumeration on first use and keeps it for the
    process. `best_for_leaders` never raises on a missing/incomplete file —
    it reports."""

    def __init__(self, dirs: Iterable[Path] | None = None, *, use_env: bool = True) -> None:
        base = tuple(Path(p) for p in (DEFAULT_DIRS if dirs is None else dirs))
        self.dirs = base + (env_dirs() if use_env else ())
        self._files: dict[Path, tuple[tuple[float, int], Enumeration | None]] = {}
        self._listed_at = 0.0
        self._paths: list[Path] = []
        self._loaded: dict[tuple, Loaded] = {}
        self._tables: dict[tuple[str, ...], tuple[mf.Tables, float]] = {}

    # -- scanning -----------------------------------------------------------
    def _list(self, force: bool = False) -> list[Path]:
        now = time.monotonic()
        if not force and self._paths and now - self._listed_at < _LISTING_TTL:
            return self._paths
        paths: list[Path] = []
        for d in self.dirs:
            if d.is_dir():
                paths.extend(sorted(d.glob("members_first_*" + _META_SUFFIX)))
        self._paths, self._listed_at = paths, now
        return paths

    def _entry(self, path: Path) -> Enumeration | None:
        try:
            st = os.stat(path)
            sp = path.with_name(path.name[:-len(_META_SUFFIX)] + _SCORES_SUFFIX)
            ssize = sp.stat().st_size if sp.is_file() else -1
        except OSError:
            self._files.pop(path, None)
            return None
        stamp = (st.st_mtime, st.st_size + ssize)   # re-parse when the scores grow too
        cached = self._files.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            doc = json.loads(path.read_bytes().decode("utf-8"))
            entry = _parse_meta(doc, path)
        except (OSError, ValueError, UnicodeDecodeError):
            entry = None
        self._files[path] = (stamp, entry)
        return entry

    def refresh(self) -> int:
        return sum(1 for p in self._list(force=True) if self._entry(p) is not None)

    def enumerations(self, song: str | None = None, *, complete_only: bool = False
                     ) -> list[Enumeration]:
        out = []
        for p in self._list():
            e = self._entry(p)
            if e is None or (song is not None and e.song != song):
                continue
            if complete_only and not e.complete:
                continue
            out.append(e)
        return out

    # -- the guard ----------------------------------------------------------
    def run_keys(self, song: str, inputs_hash: str | None,
                 inputs_hash_files: Mapping[str, str] | None = None,
                 model_version: str | None = None) -> "RunKeys":
        """The run's keys, computed the way the enumerator stamps them: the
        `reference_key` from the run's per-file digests (`inputs_hash_files`,
        the `per` dict of `certificate.inputs_hash`) and the `enumeration_key`
        from those + the run song's `chart_digest` (read from
        `certificate.CHARTS_PATH`). Without per-file digests both are None
        and the guard is the whole `inputs_hash` (`RunKeys.fallback` says
        why)."""
        mv = model_version or cert_mod.MODEL_VERSION
        files = dict(inputs_hash_files) if inputs_hash_files else None
        ek, rk, cd, err = cert_mod.try_enumeration_keys(files, song, mv)
        return RunKeys(song=song, inputs_hash=inputs_hash, enumeration_key=ek,
                       reference_key=rk, chart_digest=cd, model_version=mv,
                       inputs_hash_files=files, fallback=err)

    @staticmethod
    def _differing_inputs(e: Enumeration, run: "RunKeys", *, with_chart: bool) -> str:
        """Name WHAT differs between a keyed enumeration and the run — the
        file(s) whose digest differs (per `inputs_hash_files`), the chart of
        the song, the model version — so the retirement reason is actionable.
        Falls back to the key hashes when the per-file digests are absent."""
        parts = []
        if e.inputs_hash_files and run.inputs_hash_files:
            names = dict(cert_mod.INPUT_FILES)
            for label in cert_mod.REFERENCE_KEY_LINES:
                if e.inputs_hash_files.get(label) != run.inputs_hash_files.get(label):
                    parts.append(names.get(label, label))
        if e.model_version and run.model_version and e.model_version != run.model_version:
            parts.append(f"model_version {e.model_version} ≠ {run.model_version}")
        if with_chart and e.chart_digest and run.chart_digest and e.chart_digest != run.chart_digest:
            parts.append(f"the chart of {run.song} in charts.json")
        if parts:
            return "differs in " + ", ".join(parts)
        a = e.enumeration_key if with_chart else e.reference_key
        b = run.enumeration_key if with_chart else run.reference_key
        kind = "enumeration_key" if with_chart else "reference_key"
        return f"{kind} {(a or '')[:12]}… ≠ run {(b or '')[:12]}…"

    def _refuse(self, e: Enumeration, run: "RunKeys", *, as_reference: bool) -> str | None:
        """None when `e` passes the key guard for the run (OWN source when
        `as_reference` is False — `enumeration_key` equal; TRANSFER reference
        when True — `reference_key` equal); else the reason. A legacy meta
        (no key) or a run without per-file digests compares the whole
        `inputs_hash` and the reason says which guard refused."""
        if not e.legacy and run.reference_key is not None:
            if as_reference:
                if e.reference_key == run.reference_key:
                    return None
                return self._differing_inputs(e, run, with_chart=False)
            if run.enumeration_key is None:       # no chart for the run's song
                return f"run has no enumeration_key ({run.fallback})"
            if e.enumeration_key == run.enumeration_key:
                return None
            return self._differing_inputs(e, run, with_chart=True)
        # legacy whole-hash guard
        why = "legacy meta without enumeration_key" if e.legacy else (run.fallback or "")
        if run.inputs_hash is None:
            return f"this run carries no inputs_hash (a dump is missing); whole-hash guard ({why})"
        if e.inputs_hash != run.inputs_hash:
            return (f"hash {(e.inputs_hash or '')[:12]}… ≠ run {run.inputs_hash[:12]}… "
                    f"(whole-hash guard: {why})")
        return None

    def available(self, song: str, inputs_hash: str | None,
                  pool_ids: Iterable[str] | None = None, *,
                  budget: int | None = None,
                  inputs_hash_files: Mapping[str, str] | None = None,
                  model_version: str | None = None,
                  ) -> tuple[Enumeration | None, str | None]:
        """`(enumeration, None)` when a COMPLETE enumeration of `song` on the
        run's `enumeration_key` — computed from `inputs_hash_files` (the per-
        file digests of the run's `inputs_hash`) + the chart digest of `song`;
        legacy metas / a run without digests compare the whole `inputs_hash`
        — (and, when given, exactly the pool `pool_ids` as a set, and the
        same flat `budget` — the keys cover cards/board/charts/cfg, not the
        per-character point cap the bare scores were computed at) exists;
        else `(None, reason)` — the reason names the first difference (the
        file / chart that differs, pool, budget, incompleteness) or says
        there is none at all. With several candidates, the newest finished
        wins."""
        cands = self.enumerations(song)
        if not cands:
            return None, f"no members-first enumeration on file for {song}"
        run = self.run_keys(song, inputs_hash, inputs_hash_files, model_version)
        if inputs_hash is None and run.reference_key is None:
            return None, "this run carries no inputs_hash (a dump is missing)"
        want = frozenset(pool_ids) if pool_ids is not None else None
        ok, reasons = [], []
        for e in cands:
            why = self._refuse(e, run, as_reference=False)
            if why is not None:
                reasons.append(f"enumeration {why} ({_rel(e.meta_path)})")
                continue
            if want is not None and frozenset(e.pool) != want:
                extra = sorted(want - frozenset(e.pool))
                missing = sorted(frozenset(e.pool) - want)
                reasons.append(f"pool differs: run has {len(want)} cards, enumeration "
                               f"{len(e.pool)} (run-only {len(extra)}, enumeration-only "
                               f"{len(missing)}) ({_rel(e.meta_path)})")
                continue
            if budget is not None and e.budget is not None and int(e.budget) != int(budget):
                reasons.append(f"enumeration budget {e.budget} ≠ run {budget} "
                               f"({_rel(e.meta_path)})")
                continue
            if not e.complete:
                reasons.append(f"{e.problem} ({_rel(e.meta_path)})")
                continue
            ok.append(e)
        if ok:
            ok.sort(key=lambda e: str(e.finished or ""), reverse=True)
            return ok[0], None
        return None, "; ".join(reasons)

    def sources(self, song: str, inputs_hash: str | None,
                pool_ids: Iterable[str] | None = None, *, budget: int | None = None,
                inputs_hash_files: Mapping[str, str] | None = None,
                model_version: str | None = None,
                ) -> tuple[str | None, list[Enumeration], str | None]:
        """`("own", [e], None)` when the song's own enumeration passes
        `available`; else `("transfer", [refs...], None)` — every COMPLETE
        enumeration of another song on the same `reference_key` (the non-
        chart part of the key: same cards / collection / board / skilltree /
        master_stn / cfg / model; legacy metas: the same whole hash) / pool /
        budget, the largest group sharing one `plan_cache_seed` (newest
        first); else `(None, [], reason)` naming every candidate's first
        difference."""
        pool_ids = tuple(pool_ids) if pool_ids is not None else None   # may be a generator
        own, reason = self.available(song, inputs_hash, pool_ids, budget=budget,
                                     inputs_hash_files=inputs_hash_files,
                                     model_version=model_version)
        if own is not None:
            return "own", [own], None
        run = self.run_keys(song, inputs_hash, inputs_hash_files, model_version)
        if inputs_hash is None and run.reference_key is None:
            return None, [], reason
        want = frozenset(pool_ids) if pool_ids is not None else None
        refs, reasons = [], [reason] if reason else []
        for e in self.enumerations():
            if e.song == song:
                continue
            why = self._refuse(e, run, as_reference=True)
            if why is not None:
                reasons.append(f"reference {e.song}: {why} ({_rel(e.meta_path)})")
                continue
            if want is not None and frozenset(e.pool) != want:
                reasons.append(f"reference {e.song}: pool differs (run {len(want)} cards, "
                               f"enumeration {len(e.pool)}) ({_rel(e.meta_path)})")
                continue
            if budget is not None and e.budget is not None and int(e.budget) != int(budget):
                reasons.append(f"reference {e.song}: budget {e.budget} ≠ run {budget} "
                               f"({_rel(e.meta_path)})")
                continue
            if not e.complete:
                reasons.append(f"reference {e.song}: {e.problem} ({_rel(e.meta_path)})")
                continue
            refs.append(e)
        if not refs:
            return None, [], "; ".join(reasons) or f"no members-first enumeration on file for {song}"
        groups: dict[Any, list[Enumeration]] = {}
        for e in refs:
            groups.setdefault(e.plan_cache_seed, []).append(e)
        best = max(groups.values(),
                   key=lambda g: (len(g), max(str(e.finished or "") for e in g)))
        best.sort(key=lambda e: (str(e.finished or ""), e.song), reverse=True)
        return "transfer", best, None

    # -- the transfer reports (measured recall, read, never invented) --------
    def transfer_reports(self) -> list[dict]:
        """One summary per `members_first_<song>_transfer.json` in the scanned
        directories (the `transfer` mode: the target song's B&B rows
        vs the candidates transferred from other songs' enumerations)."""
        out = []
        for d in self.dirs:
            if not d.is_dir():
                continue
            for path in sorted(d.glob("members_first_*" + _TRANSFER_SUFFIX)):
                try:
                    doc = json.loads(path.read_bytes().decode("utf-8"))
                except (OSError, ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(doc, dict) or not isinstance(doc.get("summary"), dict):
                    continue
                by_k = {}
                for k, v in doc["summary"].items():
                    try:
                        by_k[int(k)] = {"rows": int(v.get("rows", 0)),
                                        "zero_loss": int(v.get("zero_loss", 0)),
                                        "max_loss": float(v.get("max_loss") or 0.0),
                                        "same_roster": int(v.get("same_roster", 0))}
                    except (TypeError, ValueError, AttributeError):
                        continue
                out.append({"song": str(doc.get("song") or ""),
                            "sources": [str(x) for x in (doc.get("sources") or [])],
                            "inputs_hash": doc.get("inputs_hash"),
                            "by_k": by_k, "file": _rel(path)})
        return out

    def measured_recall(self, k: int | None = None) -> dict[str, str]:
        """`{"<song> from <sources>": "<zero>/<rows> rows at K=<k>, <loss>"}`
        per report on file — at the K in use when the report measured it,
        else every K the report has. Empty when no report is on file."""
        out: dict[str, str] = {}
        for r in self.transfer_reports():
            if not r["by_k"]:
                continue
            ks = [k] if k is not None and k in r["by_k"] else sorted(r["by_k"])
            parts = []
            for kk in ks:
                v = r["by_k"][kk]
                loss = "0 loss" if v["zero_loss"] == v["rows"] else f"max loss {v['max_loss']:.2%}"
                parts.append(f"{v['zero_loss']}/{v['rows']} rows at K={kk}, {loss}")
            out[f"{r['song']} from {'+'.join(r['sources'])}"] = "; ".join(parts)
        return out

    # -- loading ------------------------------------------------------------
    def tables_for(self, pool_cards: Sequence[Any], pool_order: Sequence[str]) -> mf.Tables:
        """`members_first.tabulate` over `pool_cards` arranged in `pool_order`
        (the enumeration's index order); cached per pool-id tuple."""
        key = tuple(pool_order)
        hit = self._tables.get(key)
        if hit is not None:
            return hit[0]
        by_id = {c.id: c for c in pool_cards}
        missing = [pid for pid in key if pid not in by_id]
        if missing:
            raise KeyError(f"pool cards missing from the run's cards: {missing[:5]}")
        t0 = time.perf_counter()
        tables = mf.tabulate([by_id[pid] for pid in key])
        self._tables[key] = (tables, time.perf_counter() - t0)
        return tables

    def load(self, enumeration: Enumeration, pool_cards: Sequence[Any]) -> Loaded:
        """Read the scores, build the descending order and the tables (once per
        process for this (song, hash, pool))."""
        hit = self._loaded.get(enumeration.key)
        if hit is not None:
            return hit
        sec: dict[str, float] = {}
        t0 = time.perf_counter()
        scores = array.array("d")
        with open(enumeration.scores_path, "rb") as fh:
            scores.frombytes(fh.read())
        if len(scores) != enumeration.n_sets:
            raise ValueError(f"{_rel(enumeration.scores_path)}: {len(scores)} scores, "
                             f"meta says {enumeration.n_sets}")
        sec["read"] = time.perf_counter() - t0
        t0 = time.perf_counter()
        order = array.array("I", sorted(range(len(scores)), key=scores.__getitem__, reverse=True))
        sec["order"] = time.perf_counter() - t0
        tables = self.tables_for(pool_cards, enumeration.pool)
        sec["tables"] = self._tables[tuple(enumeration.pool)][1]
        if tables.n != enumeration.n_sets:
            raise ValueError(f"pool of {len(enumeration.pool)} cards yields {tables.n:,} legal "
                             f"sets, the enumeration holds {enumeration.n_sets:,}")
        loaded = Loaded(enumeration=enumeration, scores=scores, order=order, tables=tables,
                        seconds=sec)
        self._loaded[enumeration.key] = loaded
        return loaded

    # -- the answer ---------------------------------------------------------
    def best_for_leaders(self, song: str, inputs_hash: str | None, pool_cards: Sequence[Any],
                         leaders: Sequence[Any], k: int | None, batch_score: BatchScore, *,
                         budget: int | None = None,
                         inputs_hash_files: Mapping[str, str] | None = None,
                         model_version: str | None = None,
                         progress: Callable[[int, int, Any], None] | None = None,
                         ) -> tuple[dict[str, tuple[float, tuple[str, ...], int]], dict]:
        """For every leader card in `leaders`, the members-first best member
        set on `song`: `{leader_id: (score, member_ids, candidates_scored)}`
        plus a meta dict (`available`, `mode`, `k`, `sources`, `enumeration`,
        `measured_recall`, timings, or the reason nothing was produced).
        `inputs_hash_files` (the per-file digests of the run's hash) + the
        run's `model_version` feed the enumeration-key guard (`run_keys`);
        without them the whole `inputs_hash` guards.
        `pool_cards` is the run's candidate universe (must equal the
        enumeration's pool as a set — it supplies the card objects the tables
        are built from). `batch_score(leader, member_id_tuples)` is the run's
        exact leaf. `k`: None -> `DEFAULT_K` on the song's own enumeration,
        `DEFAULT_K_TRANSFER` on references; `k <= 0` disables. In transfer
        mode a leader's candidates are the UNION of its three tiers over every
        reference."""
        meta: dict[str, Any] = {"available": False, "k": None if k is None else int(k),
                                "song": song}
        if k is not None and k <= 0:
            meta["reason"] = "members_first_k is 0 (disabled)"
            return {}, meta
        run = self.run_keys(song, inputs_hash, inputs_hash_files, model_version)
        meta["run_keys"] = run.summary()
        mode, enums, reason = self.sources(song, inputs_hash, (c.id for c in pool_cards),
                                          budget=budget, inputs_hash_files=inputs_hash_files,
                                          model_version=model_version)
        if not enums:
            meta["reason"] = reason
            return {}, meta
        k = int(k) if k is not None else (DEFAULT_K if mode == "own" else DEFAULT_K_TRANSFER)
        meta["k"] = k
        loads: list[Loaded] = []
        for e in enums:
            try:
                loads.append(self.load(e, pool_cards))
            except (OSError, ValueError, KeyError) as exc:
                meta["reason"] = f"could not load {_rel(e.scores_path)}: {exc}"
                return {}, meta
        out: dict[str, tuple[float, tuple[str, ...], int]] = {}
        t_all = time.perf_counter()
        n_cands = 0
        for pos, leader in enumerate(leaders, 1):
            # union of the tiers over the sources, as member-id tuples (the
            # sources share the pool as a set; ids are robust to its order)
            sets: dict[tuple[str, ...], None] = {}
            for ld in loads:
                for i in mf.candidate_indices(ld.order, ld.tables, leader, k):
                    sets.setdefault(ld.tables.roster_ids(i), None)
            cands = list(sets)
            if not cands:
                continue
            scores = batch_score(leader, cands)
            if len(scores) != len(cands):
                raise ValueError("batch_score returned a different number of scores")
            i_best, s_best = mf.best_of(dict(enumerate(scores)), range(len(cands)))
            out[leader.id] = (float(s_best), cands[i_best], len(cands))
            n_cands += len(cands)
            if progress is not None:
                progress(pos, len(leaders), leader)
        primary = enums[0]
        recall = self.measured_recall(k)
        meta.update({
            "available": True, "mode": mode,
            "sources": [e.summary() for e in enums],
            "source_songs": [e.song for e in enums],
            "enumeration": primary.summary(), "enumeration_hash": primary.inputs_hash,
            "guard": ("whole-hash (legacy meta)" if primary.legacy else
                      "whole-hash (run without per-file digests)" if run.reference_key is None
                      else "enumeration_key" if mode == "own" else "reference_key"),
            "proxy": primary.proxy, "leaders": len(leaders), "answered": len(out),
            "candidates_scored": n_cands, "seconds": time.perf_counter() - t_all,
            "load_seconds": {e.song: dict(ld.seconds) for e, ld in zip(enums, loads)},
            "measured_recall": recall,
            "label": ("HEURISTIC — candidates from this song's own bare enumeration, "
                      f"top-K tiers at K={k} (exact on 66/66 rows of m0334 at K=2,000)"
                      if mode == "own" else
                      "HEURISTIC — candidates TRANSFERRED from the enumeration(s) of "
                      f"{', '.join(e.song for e in enums)}, re-scored on {song}; K={k} "
                      + (f"(transfer loss checked against exact rows on {len(recall)} chart(s), "
                         "see measured_recall; assumed similar on other charts)" if recall else
                         "(transfer loss not checked on any chart on file)")),
        })
        if not meta["measured_recall"]:
            meta["measured_recall_note"] = ("no members_first_*_transfer.json report on file in the "
                                            "scanned directories; see MEMBERS-FIRST.md §Cross-song TRANSFER")
        return out, meta


_INDEX: MembersFirstIndex | None = None


def default_index() -> MembersFirstIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = MembersFirstIndex()
    return _INDEX


__all__ = ["DEFAULT_DIRS", "DEFAULT_K", "DEFAULT_K_TRANSFER", "ENV_DIRS", "Enumeration",
           "Loaded", "MembersFirstIndex", "RunKeys", "default_index", "env_dirs"]
