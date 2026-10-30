"""Read-only lookup of the EXACT / BOUNDED certificates written by the offline
per-leader and branch-and-bound searches, and the row-level certificate object
every song-mode optimize response carries.

Sources scanned (cheap: directory listings on every lookup, each file parsed
once per mtime/size):
  RESEARCH-2026-08-18/PHASE-6/per_leader/<song>/per_leader_table_<song>.json
      rows keyed by `leader`, table-level basis fields (budget, uptime,
      platform, universe, basis, budget_kind, inputs_hash) apply to every row
  RESEARCH-2026-08-18/PHASE-5/certificate_bnb_*.json
  RESEARCH-2026-08-18/PHASE-6/certificate_bnb_*.json
      standalone single-row certificates (same fields, one file per row)

⛔ The "same instant, same basis" GUARD (`attach`): a certificate is attached to
a response row ONLY when its `inputs_hash` equals the response's and its
budget / budget_kind / uptime_model / platform / universe equal the response's
`certificate_basis`. Anything else is HEURISTIC with a `certificate_note`
saying which field differed — never a gap borrowed from another basis. A row
whose score exceeds the certified maximum for the same leader on the same
hash is a BUG (`refined > exact` on the same hash) and is
reported as status "ERROR", never hidden — UNLESS the row says it was scored on
the per-team develop leaf (`certificate_basis.leaf == "develop"`, the labelled
what-if): that is a different objective than the one the certificates certify
(the fixed plan-cache leaf), so such a row is HEURISTIC
with a note, never ERROR.

Nothing here writes a file.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[2]
RESEARCH = REPO / "tools/recon/RESEARCH-2026-08-18"
DEFAULT_TABLE_ROOTS = (RESEARCH / "PHASE-6" / "per_leader",)
DEFAULT_CERT_ROOTS = (RESEARCH / "PHASE-5", RESEARCH / "PHASE-6")

# A row's (rounded) score may exceed the certified float by the rounding alone
# (`song_est: round(est)`); one point of a ~3.4 M score is far beyond float
# noise (the fast leaf is float-identical to the plain assembly) and far below
# any real improvement.
SCORE_TOLERANCE = 1.0
_LISTING_TTL = 2.0   # seconds between directory listings


def _rel(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return str(path)


def _entry_from_cert(cert: dict, path: Path) -> dict | None:
    if not isinstance(cert, dict) or cert.get("status") not in ("EXACT", "BOUNDED"):
        return None
    best = cert.get("best") or {}
    git = cert.get("git") or {}
    return {
        "song": cert.get("song"), "leader": cert.get("leader"),
        "leader_name": cert.get("leader_name"),
        "status": cert["status"], "score": best.get("score"),
        "ids": list(best.get("ids") or []), "members": best.get("members"),
        "leader_also_member": best.get("leader_also_member"),
        "upper_bound": cert.get("upper_bound"), "gap": cert.get("gap"),
        "leaves_evaluated": cert.get("leaves_evaluated"),
        "n_rosters": cert.get("n_rosters"), "seconds": cert.get("seconds"),
        "inputs_hash": cert.get("inputs_hash"), "model_version": cert.get("model_version"),
        "git_head": git.get("head"), "git_dirty": git.get("dirty"),
        "written": cert.get("written"),
        "universe": cert.get("universe"), "basis": cert.get("basis"),
        "budget_kind": cert.get("budget_kind"), "budget": cert.get("budget"),
        "uptime_model": cert.get("uptime_model"), "platform": cert.get("platform"),
        "difficulty": cert.get("difficulty"),
        "certificate": _rel(path), "source": _rel(path), "source_kind": "certificate",
    }


def _entries_from_table(table: dict, path: Path) -> list[dict]:
    if not isinstance(table, dict) or not isinstance(table.get("rows"), list):
        return []
    seed = table.get("plan_cache_seed")
    if not (isinstance(seed, dict) and seed.get("leader")
            and isinstance(seed.get("members"), list)):
        seed = None
    common = {
        "song": table.get("song"), "universe": table.get("universe"),
        "basis": table.get("basis"), "budget_kind": table.get("budget_kind"),
        "budget": table.get("budget"), "uptime_model": table.get("uptime_model"),
        "platform": table.get("platform"), "difficulty": table.get("difficulty"),
        # the ONE PlanCache seed the whole table was scored on: a web run on
        # the same hash + basis seeds its objective from it
        # so its rows and these certificates are the same objective
        "plan_cache_seed": ({"leader": str(seed["leader"]),
                             "members": [str(m) for m in seed["members"]]}
                            if seed else None),
    }
    out = []
    for row in table["rows"]:
        if not isinstance(row, dict) or row.get("status") not in ("EXACT", "BOUNDED"):
            continue
        cert_rel = row.get("certificate")
        out.append({
            **common,
            "leader": row.get("leader"), "leader_name": row.get("leader_name"),
            "status": row["status"], "score": row.get("score"),
            "ids": list(row.get("ids") or []), "members": row.get("members"),
            "leader_also_member": row.get("leader_also_member"),
            "upper_bound": row.get("upper_bound"), "gap": row.get("gap"),
            "leaves_evaluated": row.get("leaves_evaluated"),
            "n_rosters": row.get("n_rosters"), "seconds": row.get("seconds"),
            # a row may carry its own hash (resumed tables); the table's is the fallback
            "inputs_hash": row.get("inputs_hash") or table.get("inputs_hash"),
            "model_version": row.get("model_version") or table.get("model_version"),
            "git_head": row.get("git_head"), "git_dirty": row.get("git_dirty"),
            "written": row.get("written") or table.get("written"),
            "certificate": cert_rel, "source": _rel(path), "source_kind": "table",
        })
    return out


class CertificateIndex:
    """Scans the certificate roots lazily; every file is parsed once per
    (mtime, size). `lookup` never raises on a bad file — it is skipped."""

    def __init__(self, table_roots: Iterable[Path] = DEFAULT_TABLE_ROOTS,
                 cert_roots: Iterable[Path] = DEFAULT_CERT_ROOTS) -> None:
        self.table_roots = tuple(Path(p) for p in table_roots)
        self.cert_roots = tuple(Path(p) for p in cert_roots)
        self._files: dict[Path, tuple[tuple[float, int], list[dict]]] = {}
        self._listed_at = 0.0
        self._paths: list[Path] = []

    # -- scanning -----------------------------------------------------------
    def _list(self, force: bool = False) -> list[Path]:
        now = time.monotonic()
        if not force and self._paths and now - self._listed_at < _LISTING_TTL:
            return self._paths
        paths: list[Path] = []
        for root in self.table_roots:
            if root.is_dir():
                paths.extend(sorted(root.glob("*/per_leader_table_*.json")))
                paths.extend(sorted(root.glob("per_leader_table_*.json")))
        for root in self.cert_roots:
            if root.is_dir():
                paths.extend(sorted(root.glob("certificate_bnb_*.json")))
        self._paths, self._listed_at = paths, now
        return paths

    def _entries(self, path: Path) -> list[dict]:
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
        if path.name.startswith("per_leader_table_"):
            entries = _entries_from_table(doc, path)
        else:
            e = _entry_from_cert(doc, path)
            entries = [e] if e else []
        self._files[path] = (stamp, entries)
        return entries

    def refresh(self) -> int:
        """Force a re-list; returns the number of certified rows on file."""
        return sum(len(self._entries(p)) for p in self._list(force=True))

    def entries(self, song: str | None = None, leader: str | None = None) -> list[dict]:
        out = []
        for path in self._list():
            for e in self._entries(path):
                if song is not None and e.get("song") != song:
                    continue
                if leader is not None and e.get("leader") != leader:
                    continue
                out.append(e)
        return out

    # -- the guard ----------------------------------------------------------
    @staticmethod
    def _mismatch(entry: dict, inputs_hash: str | None, basis: dict) -> str | None:
        """First field that differs, in the order that matters most."""
        if inputs_hash is None:
            return "this response carries no inputs_hash (a dump is missing)"
        if entry.get("inputs_hash") != inputs_hash:
            return (f"inputs_hash {str(entry.get('inputs_hash'))[:8]}…, this run is "
                    f"{inputs_hash[:8]}… — different basis (cards/collection/board/"
                    f"charts/skilltree/cfg/model_version)")
        for key in ("universe", "budget_kind", "budget", "uptime_model", "platform"):
            if entry.get(key) != basis.get(key):
                return f"{key} {entry.get(key)!r}, this run is {basis.get(key)!r}"
        # every certificate on file was scored on EXPERT and carries no field;
        # a run on another difficulty is a different chart, never a match
        theirs = str(entry.get("difficulty") or "EXPERT").upper()
        ours = str(basis.get("difficulty") or "EXPERT").upper()
        if theirs != ours:
            return f"difficulty {theirs!r}, this run is {ours!r}"
        return None

    def lookup(self, song: str, leader: str, inputs_hash: str | None,
               basis: dict) -> tuple[dict | None, str | None]:
        """(matching entry, note). `basis` = `certificate.certificate_basis(...)`.
        No entry for (song, leader) at all -> (None, None). Entries that fail
        the guard -> (None, note naming the first difference)."""
        candidates = self.entries(song, leader)
        if not candidates:
            return None, None
        ok = [e for e in candidates if self._mismatch(e, inputs_hash, basis) is None]
        if ok:
            # EXACT beats BOUNDED; then the smallest gap; then the newest.
            ok.sort(key=lambda e: (e["status"] != "EXACT", e.get("gap") if e.get("gap") is not None else 1e9,
                                   -(_ts(e.get("written")))))
            return ok[0], None
        # prefer to explain the table over a stray standalone file
        candidates.sort(key=lambda e: (e["source_kind"] != "table", -(_ts(e.get("written")))))
        first = candidates[0]
        kind = "table" if first["source_kind"] == "table" else "certificate"
        return None, (f"{kind} exists for this song and leader ({first['source']}) but on "
                      f"{self._mismatch(first, inputs_hash, basis)}")


    def plan_cache_seed_for(self, song: str, inputs_hash: str | None,
                            basis: dict, *, ignore: tuple[str, ...] = ()
                            ) -> tuple[str, tuple[str, ...], str] | None:
        """The PlanCache seed (leader id, member ids, source path) of a
        per-leader TABLE for `song` that passes the guard (same inputs_hash,
        same basis tuple) — so a web run can score its rows on the very
        objective the table's certificates certify. None when no such table,
        or the table carries no seed. The guard ignores `basis["leaf"]`
        exactly as `lookup` does; `ignore` names further basis fields to skip
        (the Compare view passes ("universe",): it searches nothing, so the
        universe a table was enumerated over does not bear on which roster
        seeds its plans — the hash, budget, uptime and platform still must)."""
        best: tuple[float, dict] | None = None
        for e in self.entries(song):
            if e.get("source_kind") != "table" or not e.get("plan_cache_seed"):
                continue
            probe = {**basis, **{k: e.get(k) for k in ignore}}
            if self._mismatch(e, inputs_hash, probe) is not None:
                continue
            ts = _ts(e.get("written"))
            if best is None or ts > best[0]:
                best = (ts, e)
        if best is None:
            return None
        seed = best[1]["plan_cache_seed"]
        return seed["leader"], tuple(seed["members"]), best[1]["source"]


def _ts(written: Any) -> float:
    if not written:
        return 0.0
    try:
        return time.mktime(time.strptime(str(written)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return 0.0


# ---------------------------------------------------------------------------
# The per-row certificate object
# ---------------------------------------------------------------------------
_INDEX: CertificateIndex | None = None


def default_index() -> CertificateIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = CertificateIndex()
    return _INDEX


def heuristic_certificate(*, model_version: str, inputs_hash: str | None, basis: dict,
                          generator: str, stages: list[str],
                          candidates_evaluated: int | None = None,
                          seed_team: list[str] | None = None,
                          seed_score: float | None = None,
                          note: str | None = None, extra: dict | None = None) -> dict:
    cert = {
        "status": "HEURISTIC",
        "model_version": model_version,
        "inputs_hash": inputs_hash,
        "inputs_hash_short": (inputs_hash or "")[:12] or None,
        "basis": dict(basis),
        "candidate_generator": generator,
        "stages": list(stages),
        "candidates_evaluated": candidates_evaluated,
        "seed_team": seed_team,
        "seed_score": seed_score,
        "meaning": ("best of the candidates this pipeline evaluated; no bound over "
                    "the rosters it did not evaluate"),
    }
    if note:
        cert["certificate_note"] = note
    if extra:
        cert.update(extra)
    return cert


def attach(row_cert: dict, *, song: str, leader: str, member_ids: list[str],
           row_score: float | None, index: CertificateIndex | None = None) -> dict:
    """Upgrade a HEURISTIC row certificate to EXACT / BOUNDED (or ERROR) when a
    certificate on file passes the guard. Returns the (new) certificate dict;
    `row_cert` is not mutated."""
    index = index or default_index()
    cert = dict(row_cert)
    entry, note = index.lookup(song, leader, cert.get("inputs_hash"), cert.get("basis") or {})
    if entry is None:
        if note:
            cert["certificate_note"] = note
        return cert
    same_roster = sorted(entry.get("ids") or []) == sorted(member_ids or [])
    certified = {
        "score": entry.get("score"), "gap": entry.get("gap"),
        "upper_bound": entry.get("upper_bound"),
        "leaves_evaluated": entry.get("leaves_evaluated"),
        "n_rosters": entry.get("n_rosters"), "seconds": entry.get("seconds"),
        "model_version": entry.get("model_version"),
        "inputs_hash": entry.get("inputs_hash"),
        "inputs_hash_short": (entry.get("inputs_hash") or "")[:12] or None,
        "leader": entry.get("leader"), "leader_name": entry.get("leader_name"),
        "ids": list(entry.get("ids") or []), "members": entry.get("members"),
        "leader_also_member": entry.get("leader_also_member"),
        "git_head": entry.get("git_head"), "git_dirty": entry.get("git_dirty"),
        "written": entry.get("written"),
        "file": entry.get("certificate"), "source": entry.get("source"),
        "universe": entry.get("universe"),
    }
    cert.update({
        "status": entry["status"],
        "certified": certified,
        "is_certified_argmax": same_roster,
        "row_score": row_score,
        # convenience copies of the headline numbers at the top level
        "score": entry.get("score"), "gap": entry.get("gap"),
        "upper_bound": entry.get("upper_bound"),
        "leaves_evaluated": entry.get("leaves_evaluated"),
        "n_rosters": entry.get("n_rosters"), "seconds": entry.get("seconds"),
        "certificate_file": entry.get("certificate"),
    })
    n = entry.get("n_rosters")
    if entry["status"] == "EXACT":
        cert["meaning"] = (f"every one of {n:,} rosters for this leader accounted for, on this "
                           f"exact basis (inputs_hash {(entry.get('inputs_hash') or '')[:12]}); "
                           f"the certified best is the argmax" if isinstance(n, int) else
                           "every roster for this leader accounted for on this exact basis")
    else:
        g = entry.get("gap")
        cert["meaning"] = ("an incumbent plus a valid upper bound over the rosters not yet "
                           "explored; gap = (upper_bound − score) / score"
                           + (f" = {g:.4%}" if isinstance(g, (int, float)) else ""))
    # The row's LEAF (`certificate_basis.leaf`): the certificates certify the
    # FIXED-plan objective (plan-cache leaf). A row scored on the per-team
    # greedy re-develop ("develop", the labelled what-if) is a DIFFERENT
    # objective — above the certified maximum it is HEURISTIC with a note,
    # never ERROR (re-develop can legitimately exceed a certified argmax by
    # +0.2-0.7 %; 8 of 66 certified rows would otherwise render ERROR).
    leaf = (cert.get("basis") or {}).get("leaf")
    develop_leaf = leaf == "develop"
    develop_note = ("scored on the per-team develop leaf (what-if), not the "
                    "certified objective")
    cs, rs = entry.get("score"), row_score
    if isinstance(cs, (int, float)) and isinstance(rs, (int, float)):
        if rs > cs + SCORE_TOLERANCE and entry["status"] == "EXACT":
            if develop_leaf:
                cert["status"] = "HEURISTIC"
                cert["note"] = develop_note
                cert["meaning"] = ("best of the candidates this pipeline evaluated, on the "
                                   "per-team develop leaf; the EXACT certificate on file "
                                   "is about the fixed-plan objective")
            else:
                cert["status"] = "ERROR"
                cert["error"] = (f"this row scores {rs:,.0f} but the EXACT certificate says the "
                                 f"maximum for this leader on the same inputs_hash is {cs:,.0f} "
                                 f"({entry.get('certificate')}) — refined > exact on the same "
                                 f"hash is a bug (OBJECTIVE-v2 §5), not a better team")
        elif rs > cs + SCORE_TOLERANCE and entry["status"] == "BOUNDED":
            ub = entry.get("upper_bound")
            if isinstance(ub, (int, float)) and rs > ub + SCORE_TOLERANCE:
                if develop_leaf:
                    cert["status"] = "HEURISTIC"
                    cert["note"] = develop_note
                else:
                    cert["status"] = "ERROR"
                    cert["error"] = (f"this row scores {rs:,.0f}, above the certificate's valid "
                                     f"upper bound {ub:,.0f} for this leader on the same "
                                     f"inputs_hash — a bug, not a better team")
            else:
                cert["note"] = (f"this row ({rs:,.0f}) beats the BOUNDED incumbent "
                                f"({cs:,.0f}) but stays under its upper bound "
                                f"{ub:,.0f} — consistent; the incumbent was not the argmax")
        elif not same_roster:
            names = entry.get("members") or entry.get("ids")
            cert["note"] = (f"certified best for this leader is {names} at score {cs:,.0f}; "
                            f"this row is a different roster at {rs:,.0f}")
    elif not same_roster:
        cert["note"] = "certified best for this leader is a different roster"
    if develop_leaf and cert["status"] != "HEURISTIC":
        cert["leaf_note"] = (f"row number is on the per-team develop leaf (what-if); the "
                             f"certificate is about the fixed-plan objective "
                             f"({cs:,.0f} certified)" if isinstance(cs, (int, float))
                             else "row number is on the per-team develop leaf (what-if)")
    return cert


__all__ = ["CertificateIndex", "default_index", "heuristic_certificate", "attach",
           "SCORE_TOLERANCE"]
