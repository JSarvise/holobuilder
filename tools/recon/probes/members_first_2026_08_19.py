"""MEMBERS FIRST, LEADER SECOND: one leader-agnostic enumeration of the member 5-sets
in place of one search per leader. Many leaders share the same optimal member set (on
m0334, 22 of 66 leaders share one, and there are 25 distinct sets in all).

Idea. `S(T, C)` depends on the leader only through (a) its outfit skill (a
team-wide stat_up / score_support with a gate on the FIVE members' type or
generation counts, plus the rider), (b) its own character's plan role
(leader / leader+member) and singer context, and nothing else. So every legal
member set is scored ONCE under a BARE proxy leader (a real 5★ card with its
`costume_skill` removed), every score is kept, and then, per real leader, only
the sets that can matter for THAT leader are re-scored: the top-K by bare score
overall, plus the top-K among the sets that satisfy the leader's outfit gate (a
gated outfit can only win on a set that fires it), plus the top-K among the sets
that contain the leader's own holomem (role b). The best of those under the real
leader is the members-first answer for that leader.

The check is the exact per-leader table (`per_leader/<song>/per_leader_table_<song>.json`,
every row certified by branch-and-bound): recall loss per leader =
(certified − members_first) / certified, for one enumeration plus one cheap re-score
per leader instead of a full search per leader.

Modes:
  enumerate   score every legal 5-set under the bare proxy → members_first_<song>_<proxy>.scores.bin
              (float64, lexicographic `legal_rosters` order over the pool sorted by id, chunked,
              resumable) + .meta.json
  leaders     per real leader: candidates ∪ over the K tiers → exact re-score → best; compare with
              the table → members_first_<song>_leaders.json (+ .log)
  enumerate --songs m0334,m0335,...   the batch builder: one enumeration per song in order,
              resumable per song (complete songs are skipped, a partial one resumes) — the
              web's members_first_index.py serves seeds from every complete enumeration here
  transfer    candidates from OTHER songs' enumerations (--sources), re-scored on --song with each
              real leader against --song's B&B rows → members_first_<song>_transfer.json
  update      a card landed: recompute the enumeration of --song for the CURRENT pool from the
              older .scores.bin on disk (its meta `pool` must be a subset of the current pool, or
              differ only by representative changes), REUSING every old score and scoring ONLY
              the sets that contain a new card (`members_first.merge_scores`) → a new
              .scores.bin + meta (derived_from, new_cards, sets_rescored, ...). Minutes, not
              the hour of `enumerate`. --from-dir / --out-dir default to this module's OUT_DIR.
  rekey       stamp the enumeration's OWN validity key into an EXISTING meta (`chart_digest`,
              `enumeration_key`, `reference_key`, `inputs_hash_files`; `certificate.enumeration_key`)
              — only when the current files' `inputs_hash` equals the meta's (so the current
              charts.json IS the one it was scored on); writes the meta only, never the scores.

The enumeration's key. `inputs_hash` digests charts.json AS A WHOLE, so a new
song landing in charts.json would retire every enumeration — but the bare scores depend on
ONE chart (the song's own). Every meta therefore also carries `chart_digest` (sha256 of the
canonical JSON of the song's entry in charts.json), `enumeration_key` (the per-file digests of
cards / collection / board / skilltree / master_stn / cfg + model_version + the chart digest)
and `reference_key` (the same without the chart — what a cross-song TRANSFER reference must
share). The web index (`tools/web/members_first_index.py`) guards by these; `inputs_hash` is
kept for the record and for legacy metas.
Nothing here writes data/ or state/. Run from the repo root:
    set PYTHONIOENCODING=utf-8
    set PYTHONPATH=src;tools/web;tools/recon/proxy
    python tools/recon/probes/members_first_2026_08_19.py enumerate --song m0334 --workers 14
    python tools/recon/probes/members_first_2026_08_19.py leaders --song m0334 --k 100,500,2000
    python tools/recon/probes/members_first_2026_08_19.py update --song m0334 --proxy nonsinger --workers 14
    python tools/recon/probes/members_first_2026_08_19.py rekey --songs m0334,m0139 --proxy nonsinger
"""
from __future__ import annotations

import argparse
import array
import dataclasses
import hashlib
import importlib.util
import itertools
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for _p in ("src", "tools/web", "tools/recon/proxy"):
    _sp = str(REPO / _p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

from hbuilder.solver import exact_row as er  # noqa: E402
from hbuilder.solver import members_first as mf  # noqa: E402  (the file-free core)
from hbuilder.solver import parallel_refine as pr  # noqa: E402
from hbuilder.solver.parallel import default_workers  # noqa: E402
from hbuilder.domain.team import Team  # noqa: E402
import certificate as cert_mod  # noqa: E402

RESEARCH = REPO / "tools/recon/RESEARCH-2026-08-18"
OUT_DIR = RESEARCH / "PHASE-6" / "members_first"
TABLE_SEED = ("card-04001-5-uniq-0042-00",
              ("card-04001-5-uniq-0042-00", "card-00032-5-uniq-0026-00",
               "card-04003-5-uniq-0044-00", "card-00007-5-uniq-0008-00",
               "card-00006-5-uniq-0007-00"))
# bare proxies: a non-singer of m0334 (Hakos Baelz RC) and the singer (Calli PDW)
PROXIES = {"nonsinger": "card-04012-5-uniq-0051-00", "singer": "card-04001-5-uniq-0042-00"}


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Log:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.t0 = time.perf_counter()

    def __call__(self, line: str) -> None:
        msg = f"[{time.perf_counter() - self.t0:8.1f}s] {line}"
        print(msg, flush=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")


# the proxy leader, the gate reader, the per-set tables and the candidate
# tiers live in `hbuilder.solver.members_first` (stdlib, no file I/O); this
# module keeps the CLI, the on-disk formats and the comparison with the table
bare_leader = mf.bare_leader
_gate_of = mf.gate_of


def build(args, proxy_card_id: str | None):
    ex = _load("exhaustive_song_leader", "tools/recon/probes/exhaustive_song_leader.py")
    difficulty = cert_mod.normalize_difficulty(getattr(args, "difficulty", None))
    cards, cfg, ctx, chart = ex.load_context(args.uptime, args.budget, args.song, difficulty)
    ctx = dataclasses.replace(ctx, cfg=dataclasses.replace(ctx.cfg, platform=args.platform))
    by_id = {c.id: c for c in cards}
    proxy = None
    if proxy_card_id:
        proxy = bare_leader(by_id[proxy_card_id])
        cards = list(cards) + [proxy]
        by_id[proxy.id] = proxy
        ctx = dataclasses.replace(ctx, cards=tuple(cards))
    five, reps, dominated = ex.build_pool([c for c in cards if not mf.is_bare(c)], TABLE_SEED[0])
    ctx = dataclasses.replace(ctx, use_plan_cache=True, plan_cache_seed=TABLE_SEED)
    ih, per = cert_mod.inputs_hash(ctx.cfg, "maxed", cert_mod.MODEL_VERSION)
    # EXPERT reads charts.json exactly as before (its enumeration keys do not
    # move); another difficulty digests ITS chart in charts_all.json, so an
    # enumeration on it can never pass for the EXPERT one
    cd = cert_mod.chart_digest(args.song, charts_path=cert_mod.charts_path_for(difficulty),
                               difficulty=difficulty)
    if cd is None:
        raise SystemExit(f"no {difficulty} chart for {args.song} in "
                         f"{cert_mod.charts_path_for(difficulty).name}")
    return dict(ex=ex, cards=cards, by_id=by_id, ctx=ctx, chart=chart, reps=reps,
                pool_ids=[c.id for c in reps], hol=tuple(c.member for c in reps),
                proxy=proxy, inputs_hash=ih, inputs_hash_files=per, dominated=dominated,
                chart_digest=cd,
                enumeration_key=cert_mod.enumeration_key(per, cd, cert_mod.MODEL_VERSION),
                reference_key=cert_mod.reference_key(per, cert_mod.MODEL_VERSION),
                pool_features={c.id: feature_digest(ex, c) for c in reps},
                proxy_feature=feature_digest(ex, proxy) if proxy else None)


def feature_digest(ex, card) -> str:
    """sha256 of `exhaustive_song_leader.feature_key(card)` — everything a
    member contributes (the outfit excluded). Recorded per pool card so a
    later `update` can VERIFY that the old cards are unchanged instead of
    assuming it (the whole-file `inputs_hash` cannot tell an addition from an
    edit)."""
    return hashlib.sha256(repr(ex.feature_key(card)).encode("utf-8")).hexdigest()


def cmd_enumerate(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log = Log(OUT_DIR / f"members_first_{args.song}_{args.proxy}.log")
    S = build(args, PROXIES[args.proxy])
    reps, hol, proxy = S["reps"], S["hol"], S["proxy"]
    workers = args.workers or default_workers()
    n = sum(1 for _ in er.legal_rosters(hol, 5))
    scores_path = OUT_DIR / f"members_first_{args.song}_{args.proxy}.scores.bin"
    meta_path = OUT_DIR / f"members_first_{args.song}_{args.proxy}.meta.json"
    done = 0
    if scores_path.is_file() and not args.fresh:
        done = scores_path.stat().st_size // 8
        done -= done % args.chunk          # re-do a partial chunk
    else:
        scores_path.write_bytes(b"")
    log(f"song {args.song} proxy {args.proxy} = {proxy.id} ({proxy.member}, no outfit); pool {len(reps)}; "
        f"{n:,} legal sets; chunk {args.chunk}; workers {workers}; inputs_hash {S['inputs_hash'][:16]}; "
        f"model {cert_mod.MODEL_VERSION}; resuming at {done:,}")
    meta = {"song": args.song, "proxy": args.proxy, "proxy_card": proxy.id, "proxy_holomem": proxy.member,
            "pool": S["pool_ids"], "n_sets": n, "order": "exact_row.legal_rosters(pool holomems, 5) — "
            "lexicographic 5-combinations of pool indices with distinct holomems",
            "chunk": args.chunk, "inputs_hash": S["inputs_hash"], "model_version": cert_mod.MODEL_VERSION,
            "plan_cache_seed": {"leader": TABLE_SEED[0], "members": list(TABLE_SEED[1])},
            "budget": args.budget, "uptime": args.uptime, "platform": args.platform,
            "inputs_hash_files": S["inputs_hash_files"], "pool_features": S["pool_features"],
            "proxy_feature": S["proxy_feature"],
            "chart_digest": S["chart_digest"], "enumeration_key": S["enumeration_key"],
            "reference_key": S["reference_key"],
            "git": cert_mod.git_state(), "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    meta_path.write_bytes(json.dumps(meta, indent=1).encode())
    t0 = time.perf_counter()
    with pr.RefinePool(S["ctx"], workers=workers, on_fallback=lambda l: log(f"  ! {l}")) as rp, \
            open(scores_path, "r+b") as fh:
        fh.seek(done * 8)
        fh.truncate()
        i = done
        gen = er.legal_rosters(hol, 5)
        for _ in range(done):
            next(gen)
        while i < n:
            chunk = list(itertools.islice(gen, args.chunk))
            if not chunk:
                break
            sc = mf.score_sets(proxy, reps, chunk, rp.batch_score)
            arr = array.array("d", sc)
            fh.write(arr.tobytes())
            fh.flush()
            i += len(chunk)
            if (i // args.chunk) % 20 == 0 or i >= n:
                el = time.perf_counter() - t0
                rate = (i - done) / el if el else 0
                log(f"  {i:,}/{n:,} scored; {el:.0f}s; {rate:,.0f} sets/s; eta {(n - i) / rate / 60 if rate else 0:.1f} min")
    meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta["seconds"] = time.perf_counter() - t0
    meta_path.write_bytes(json.dumps(meta, indent=1).encode())
    log(f"done -> {scores_path} ({n:,} × float64)")


def cmd_leaders(args):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log = Log(OUT_DIR / f"members_first_{args.song}_leaders.log")
    S = build(args, None)
    reps, hol, by_id, ctx = S["reps"], S["hol"], S["by_id"], S["ctx"]
    t0 = time.perf_counter()
    tables = mf.tabulate(reps)
    h_idx = tables.h_idx
    n = tables.n
    log(f"{n:,} sets tabulated in {time.perf_counter() - t0:.0f}s")
    roster = tables.roster
    ks = [int(k) for k in args.k.split(",")]
    kmax = max(ks)
    # bare scores per proxy
    proxies = {}
    for name in args.proxies.split(","):
        p = OUT_DIR / f"members_first_{args.song}_{name}.scores.bin"
        if not p.is_file():
            log(f"! no scores for proxy {name} ({p}); skip")
            continue
        arr = array.array("d")
        arr.frombytes(p.read_bytes())
        if len(arr) != n:
            raise SystemExit(f"{p}: {len(arr)} scores, expected {n}")
        proxies[name] = arr
    log(f"song {args.song}: {n:,} sets; proxies {list(proxies)}; K tiers {ks}; inputs_hash {S['inputs_hash'][:16]}")
    # ranking by bare score per proxy (indices sorted desc)
    t0 = time.perf_counter()
    order = {name: sorted(range(n), key=arr.__getitem__, reverse=True) for name, arr in proxies.items()}
    log(f"orders built in {time.perf_counter() - t0:.0f}s")
    table_path = RESEARCH / "PHASE-6" / "per_leader" / args.song / f"per_leader_table_{args.song}.json"
    table = json.loads(table_path.read_text("utf-8")) if table_path.is_file() else None
    trows = {r["leader"]: r for r in table["rows"]} if table else {}
    if table and table.get("inputs_hash") != S["inputs_hash"]:
        log(f"! table hash {table['inputs_hash'][:16]} != this run {S['inputs_hash'][:16]} — losses are NOT on the same basis")
    leaders = [by_id[r["leader"]] for r in table["rows"]] if table else \
        [c for c in reps]
    workers = args.workers or default_workers()
    results = []
    t_all = time.perf_counter()
    with pr.RefinePool(ctx, workers=workers, on_fallback=lambda l: log(f"  ! {l}")) as rp:
        for li, leader in enumerate(leaders, 1):
            gate = _gate_of(leader)
            # candidate tiers per proxy: top-K overall, top-K gate-satisfying, top-K containing own holomem
            cand_by_k = {k: set() for k in ks}
            for name, ordr in order.items():
                for k, tier in mf.candidate_tiers(ordr, tables, leader, ks).items():
                    cand_by_k[k].update(tier)
            all_c = sorted(cand_by_k[kmax])
            t0 = time.perf_counter()
            sc = mf.score_sets(leader, reps, [roster(i) for i in all_c], rp.batch_score)
            t_leader = time.perf_counter() - t0
            score_of = dict(zip(all_c, sc))
            row = {"leader": leader.id, "leader_name": f"{leader.member} / {leader.costume_name}",
                   "gate": gate, "candidates": len(all_c), "seconds": t_leader, "by_k": {}}
            cert = trows.get(leader.id)
            for k in ks:
                best_i = max(cand_by_k[k], key=lambda i: score_of[i])
                best = score_of[best_i]
                ent = {"candidates": len(cand_by_k[k]), "best": best, "idx": list(roster(best_i)),
                       "ids": [S["pool_ids"][j] for j in roster(best_i)]}
                if cert:
                    ent["certified"] = cert["score"]
                    ent["recall_loss"] = (cert["score"] - best) / cert["score"]
                    ent["same_roster"] = sorted(ent["ids"]) == sorted(cert["ids"])
                row["by_k"][k] = ent
            results.append(row)
            e = row["by_k"][kmax]
            log(f"[{li}/{len(leaders)}] {row['leader_name']}: gate {gate}; {len(all_c)} cands {t_leader:.1f}s; "
                + " ".join(f"K{k}:{row['by_k'][k]['best']:,.0f}" + (f"({row['by_k'][k]['recall_loss']:.2%})" if cert else "") for k in ks))
    total = time.perf_counter() - t_all
    summary = {}
    for k in ks:
        losses = [r["by_k"][k].get("recall_loss") for r in results if "recall_loss" in r["by_k"][k]]
        summary[k] = {"leaders": len(losses), "zero_loss": sum(1 for x in losses if x <= 1e-12),
                      "max_loss": max(losses) if losses else None,
                      "mean_loss": sum(losses) / len(losses) if losses else None,
                      "same_roster": sum(1 for r in results if r["by_k"][k].get("same_roster"))}
    out = {"song": args.song, "proxies": list(proxies), "k_tiers": ks, "n_sets": n,
           "inputs_hash": S["inputs_hash"], "model_version": cert_mod.MODEL_VERSION,
           "table": str(table_path.relative_to(REPO)) if table else None,
           "table_inputs_hash": table.get("inputs_hash") if table else None,
           "seconds_leaders_stage": total, "workers": workers,
           "enumeration_seconds": {name: json.loads((OUT_DIR / f"members_first_{args.song}_{name}.meta.json").read_text()).get("seconds") for name in proxies},
           "summary": summary, "rows": results, "git": cert_mod.git_state(),
           "written": time.strftime("%Y-%m-%dT%H:%M:%S")}
    path = OUT_DIR / f"members_first_{args.song}_leaders.json"
    path.write_bytes(json.dumps(out, indent=1, ensure_ascii=False, default=str).encode())
    for k in ks:
        s = summary[k]
        log(f"K={k}: zero-loss {s['zero_loss']}/{s['leaders']} leaders, same roster {s['same_roster']}, "
            f"max loss {s['max_loss']:.3%} mean {s['mean_loss']:.4%}")
    log(f"leaders stage {total:.0f}s ×{workers}; -> {path}")


def cmd_transfer(args):
    """Cross-song TRANSFER: candidates from the enumerations of OTHER songs
    (`--sources`), re-scored on `--song` with each real leader, against the
    B&B rows of `--song` on disk. Seconds per leader; no enumeration of the
    target song. Writes members_first_<song>_transfer.json."""
    from hbuilder.solver import members_first as core
    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    log = Log(out_dir / f"members_first_{args.song}_transfer.log")
    S = build(args, None)
    reps, by_id, ctx = S["reps"], S["by_id"], S["ctx"]
    tables = core.tabulate(reps)
    n = tables.n if hasattr(tables, "n") else len(tables.tcount)
    sources = {}
    for name in args.sources.split(","):
        p = OUT_DIR / f"members_first_{name}_{args.proxy}.scores.bin"
        meta = OUT_DIR / f"members_first_{name}_{args.proxy}.meta.json"
        if not p.is_file() or not meta.is_file():
            log(f"! no enumeration for {name}; skip")
            continue
        m = json.loads(meta.read_text("utf-8"))
        if m.get("pool") != S["pool_ids"]:
            log(f"! {name}: pool differs; skip")
            continue
        arr = array.array("d")
        arr.frombytes(p.read_bytes())
        if len(arr) != n:
            log(f"! {name}: {len(arr)} scores != {n} sets; skip")
            continue
        t0 = time.perf_counter()
        sources[name] = sorted(range(n), key=arr.__getitem__, reverse=True)
        log(f"source {name}: order built in {time.perf_counter() - t0:.0f}s (enumeration hash {m.get('inputs_hash','')[:12]})")
    table_path = RESEARCH / "PHASE-6" / "per_leader" / args.song / f"per_leader_table_{args.song}.json"
    if not table_path.is_file():
        raise SystemExit(f"no B&B rows for {args.song} ({table_path}); certify some rows first")
    table = json.loads(table_path.read_text("utf-8"))
    if table.get("inputs_hash") != S["inputs_hash"]:
        log(f"! table hash {table['inputs_hash'][:16]} != run {S['inputs_hash'][:16]}")
    ks = [int(k) for k in args.k.split(",")]
    workers = args.workers or default_workers()
    rows = []
    with pr.RefinePool(ctx, workers=workers, on_fallback=lambda l: log(f"  ! {l}")) as rp:
        for r in table["rows"]:
            L = by_id[r["leader"]]
            row = {"leader": L.id, "leader_name": r["leader_name"], "certified": r["score"], "by_k": {}}
            for k in ks:
                cands = set()
                for name, order in sources.items():
                    cands.update(core.candidate_indices(order, tables, L, k))
                cands = sorted(cands)
                t0 = time.perf_counter()
                sc = rp.batch_score([Team(leader=L, members=tuple(reps[j] for j in tables.roster(i))) for i in cands])
                best_i = max(range(len(cands)), key=sc.__getitem__)
                best = sc[best_i]
                ids = [S["pool_ids"][j] for j in tables.roster(cands[best_i])]
                row["by_k"][k] = {"best": best, "candidates": len(cands), "seconds": time.perf_counter() - t0,
                                  "recall_loss": (r["score"] - best) / r["score"], "ids": ids,
                                  "same_roster": sorted(ids) == sorted(r["ids"])}
            rows.append(row)
            log(f"{row['leader_name']}: EXACT {r['score']:,.0f} | " + " ".join(
                f"K{k}:{row['by_k'][k]['best']:,.0f}({row['by_k'][k]['recall_loss']:.2%},{row['by_k'][k]['candidates']}c,{row['by_k'][k]['seconds']:.0f}s)" for k in ks))
    summary = {k: {"rows": len(rows), "zero_loss": sum(1 for x in rows if x["by_k"][k]["recall_loss"] <= 1e-12),
                   "max_loss": max(x["by_k"][k]["recall_loss"] for x in rows),
                   "same_roster": sum(1 for x in rows if x["by_k"][k]["same_roster"])} for k in ks}
    out = {"song": args.song, "sources": list(sources), "proxy": args.proxy, "k_tiers": ks,
           "inputs_hash": S["inputs_hash"], "table_inputs_hash": table.get("inputs_hash"),
           "model_version": cert_mod.MODEL_VERSION, "summary": summary, "rows": rows,
           "git": cert_mod.git_state(), "written": time.strftime("%Y-%m-%dT%H:%M:%S")}
    path = out_dir / f"members_first_{args.song}_transfer.json"
    path.write_bytes(json.dumps(out, indent=1, ensure_ascii=False, default=str).encode())
    for k in ks:
        log(f"K={k}: zero-loss {summary[k]['zero_loss']}/{len(rows)}, same roster {summary[k]['same_roster']}, max loss {summary[k]['max_loss']:.3%}")
    log(f"-> {path}")


def cmd_update(args):
    """A card landed (or a representative changed): the enumeration of
    `--song` for the CURRENT pool from the older one on disk, reusing every
    old score by position and scoring ONLY the sets that contain a new card
    (`members_first.merge_scores`). Writes `members_first_<song>_<proxy>.
    {scores.bin,meta.json}` into `--out-dir` (the meta carries `derived_from`,
    `new_cards`, `sets_rescored`, `representative_changes`, the per-file
    digests and per-card feature digests the NEXT update verifies against).
    The old pool must be a subset of the current one, or differ only by
    representative changes (an old card now dominated: its sets are DROPPED
    and the new representative's sets are scored — the fallback, never a
    guess)."""
    from_dir = Path(args.from_dir) if args.from_dir else OUT_DIR
    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"members_first_{args.song}_{args.proxy}"
    old_meta_path, old_scores_path = from_dir / f"{stem}.meta.json", from_dir / f"{stem}.scores.bin"
    if not (old_meta_path.is_file() and old_scores_path.is_file()):
        raise SystemExit(f"no old enumeration {stem} under {from_dir}")
    old_meta = json.loads(old_meta_path.read_text("utf-8"))
    if not old_meta.get("finished"):
        raise SystemExit(f"{old_meta_path}: the old enumeration is not finished; resume `enumerate` first")
    log = Log(out_dir / f"{stem}.update.log")
    S = build(args, PROXIES[args.proxy])
    reps, proxy, by_id = S["reps"], S["proxy"], S["by_id"]
    pool_ids = S["pool_ids"]
    # the same proxy, the same flat budget / uptime / platform, else the old scores are another objective
    for key, mine in (("proxy_card", proxy.id), ("budget", args.budget), ("uptime", args.uptime),
                      ("platform", args.platform), ("model_version", cert_mod.MODEL_VERSION)):
        if old_meta.get(key) is not None and old_meta.get(key) != mine:
            raise SystemExit(f"old enumeration {key}={old_meta.get(key)!r} != this run {mine!r}; not derivable")
    old_pool = list(old_meta["pool"])
    old_scores = array.array("d")
    old_scores.frombytes(old_scores_path.read_bytes())
    if len(old_scores) != int(old_meta["n_sets"]):
        raise SystemExit(f"{old_scores_path}: {len(old_scores)} scores, meta says {old_meta['n_sets']}")
    new_set, old_set = set(pool_ids), set(old_pool)
    new_cards = [pid for pid in pool_ids if pid not in old_set]
    gone = [pid for pid in old_pool if pid not in new_set]
    # representative changes: an old pool card now dominated by another (its sets are dropped,
    # the representative's scored); an old card no longer in the catalogue at all is fatal
    rep_of = {d: r for d, r in S["dominated"]}
    rep_changes = []
    old_hol = []
    for pid in old_pool:
        c = by_id.get(pid)
        if c is None:
            raise SystemExit(f"old pool card {pid} is not in the catalogue any more; re-enumerate")
        old_hol.append(c.member)
        if pid not in new_set:
            rep_changes.append({"old_card": pid, "now_represented_by": rep_of.get(pid),
                                "action": "old sets dropped; the representative's sets scored"})
    # can the old scores be reused at all? same hash -> trivially; else verify what the old meta lets us
    if old_meta.get("inputs_hash") == S["inputs_hash"]:
        reuse_basis = "VERIFIED: same inputs_hash as this run"
    elif old_meta.get("inputs_hash_files") and old_meta.get("pool_features"):
        bad = [k for k in ("board", "charts", "skilltree", "master_stn", "cfg", "collection")
               if old_meta["inputs_hash_files"].get(k) != S["inputs_hash_files"].get(k)]
        changed = [pid for pid in old_pool if pid in new_set
                   and old_meta["pool_features"].get(pid) != S["pool_features"].get(pid)]
        if old_meta.get("proxy_feature") and old_meta["proxy_feature"] != S["proxy_feature"]:
            changed.append(proxy.id)
        if bad or changed:
            raise SystemExit(f"the old scores are NOT reusable: per-file digests differ for {bad}; "
                             f"cards with changed features {changed[:5]}; re-enumerate")
        reuse_basis = ("VERIFIED: board/charts/skilltree/master_stn/cfg/collection digests and every "
                       "old pool card's feature digest equal this run's (cards.json differs only by additions)")
    else:
        reuse_basis = ("UNVERIFIED: the old meta carries neither per-file digests nor pool feature "
                       "digests (written before 2026-08-19); the old scores are reused on the "
                       "ASSUMPTION that only cards were added since")
    workers = args.workers or default_workers()
    log(f"song {args.song} proxy {args.proxy} = {proxy.id}; old pool {len(old_pool)} ({len(old_scores):,} sets, "
        f"hash {str(old_meta.get('inputs_hash', ''))[:12]}) -> new pool {len(pool_ids)}; new cards {new_cards}; "
        f"gone {gone}; workers {workers}; inputs_hash {S['inputs_hash'][:16]}")
    log(f"reuse basis: {reuse_basis}")
    n_new = sum(1 for _ in er.legal_rosters(S["hol"], 5))
    t0 = time.perf_counter()
    last = [time.perf_counter()]

    def progress(rescored, reused):
        now = time.perf_counter()
        if now - last[0] >= 30:
            last[0] = now
            el = now - t0
            log(f"  {rescored:,} rescored, {reused:,} reused of {n_new:,}; {el:.0f}s; {rescored / el:,.0f} new sets/s")

    with pr.RefinePool(S["ctx"], workers=workers, on_fallback=lambda l: log(f"  ! {l}")) as rp:
        merged = mf.merge_scores(
            old_scores, old_pool, reps,
            lambda rosters: mf.score_sets(proxy, reps, rosters, rp.batch_score),
            old_holomems=old_hol, chunk=args.chunk, progress=progress)
    seconds = time.perf_counter() - t0
    if merged.n_sets != n_new:
        raise SystemExit(f"merged {merged.n_sets:,} sets, the new pool has {n_new:,}")
    scores_path, meta_path = out_dir / f"{stem}.scores.bin", out_dir / f"{stem}.meta.json"
    scores_path.write_bytes(merged.scores.tobytes())
    meta = {"song": args.song, "proxy": args.proxy, "proxy_card": proxy.id, "proxy_holomem": proxy.member,
            "pool": pool_ids, "n_sets": merged.n_sets, "order": old_meta.get("order") or
            "exact_row.legal_rosters(pool holomems, 5) — lexicographic 5-combinations of pool indices with distinct holomems",
            "chunk": args.chunk, "inputs_hash": S["inputs_hash"], "model_version": cert_mod.MODEL_VERSION,
            "plan_cache_seed": old_meta.get("plan_cache_seed") or
            {"leader": TABLE_SEED[0], "members": list(TABLE_SEED[1])},
            "budget": args.budget, "uptime": args.uptime, "platform": args.platform,
            "inputs_hash_files": S["inputs_hash_files"], "pool_features": S["pool_features"],
            "proxy_feature": S["proxy_feature"],
            "chart_digest": S["chart_digest"], "enumeration_key": S["enumeration_key"],
            "reference_key": S["reference_key"],
            "derived_from": {"meta": str(old_meta_path), "scores": str(old_scores_path),
                             "inputs_hash": old_meta.get("inputs_hash"), "n_sets": len(old_scores),
                             "pool_size": len(old_pool), "finished": old_meta.get("finished"),
                             "reuse_basis": reuse_basis},
            "new_cards": new_cards, "sets_rescored": merged.sets_rescored, "sets_reused": merged.reused,
            "sets_dropped": merged.dropped, "representative_changes": rep_changes,
            "git": cert_mod.git_state(),
            "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - seconds)),
            "finished": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": seconds, "workers": workers}
    meta_path.write_bytes(json.dumps(meta, indent=1, ensure_ascii=False).encode("utf-8"))
    log(f"done: {merged.sets_rescored:,} sets rescored, {merged.reused:,} reused, {merged.dropped:,} dropped; "
        f"{seconds:.0f}s x{workers} ({merged.sets_rescored / seconds if seconds else 0:,.0f} new sets/s) "
        f"-> {scores_path} ({merged.n_sets:,} x float64)")


def cmd_rekey(args):
    """Stamp `chart_digest` / `enumeration_key` / `reference_key` (+
    `inputs_hash_files` when absent) into an existing meta — the key the
    web index guards by. Only when the CURRENT files' `inputs_hash` equals
    the meta's: then the current charts.json is the one the scores were
    computed on and the key is computed on the same basis (anything else is
    refused by name — re-enumerate or `update`). Rewrites the meta only,
    never the scores. `--songs a,b` rekeys several; `--out-dir` = where the
    metas live (default OUT_DIR)."""
    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    songs = [x.strip() for x in (args.songs or args.song).split(",") if x.strip()]
    for song in songs:
        args.song = song
        meta_path = out_dir / f"members_first_{song}_{args.proxy}.meta.json"
        if not meta_path.is_file():
            print(f"{song}: no meta at {meta_path}; skip", flush=True)
            continue
        meta = json.loads(meta_path.read_bytes().decode("utf-8"))
        S = build(args, PROXIES[args.proxy])
        if meta.get("inputs_hash") != S["inputs_hash"]:
            print(f"{song}: REFUSED — meta inputs_hash {str(meta.get('inputs_hash'))[:16]} ≠ current "
                  f"{S['inputs_hash'][:16]}: the current files are not the ones it was scored on "
                  f"(re-enumerate or `update`)", flush=True)
            continue
        if list(meta.get("pool") or []) != S["pool_ids"]:
            print(f"{song}: REFUSED — meta pool ({len(meta.get('pool') or [])}) ≠ current pool "
                  f"({len(S['pool_ids'])})", flush=True)
            continue
        if meta.get("model_version") not in (None, cert_mod.MODEL_VERSION):
            print(f"{song}: REFUSED — meta model_version {meta.get('model_version')} ≠ "
                  f"{cert_mod.MODEL_VERSION}", flush=True)
            continue
        before = {k: meta.get(k) for k in ("chart_digest", "enumeration_key", "reference_key")}
        meta["inputs_hash_files"] = meta.get("inputs_hash_files") or S["inputs_hash_files"]
        meta["chart_digest"] = S["chart_digest"]
        meta["enumeration_key"] = S["enumeration_key"]
        meta["reference_key"] = S["reference_key"]
        meta["keys_stamped"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "inputs_hash": S["inputs_hash"], "by": "rekey"}
        # the same serialisation `enumerate` used (ASCII-escaped), so the diff is the added keys only
        meta_path.write_bytes(json.dumps(meta, indent=1).encode("utf-8"))
        print(f"{song}: stamped chart_digest {S['chart_digest'][:16]} enumeration_key "
              f"{S['enumeration_key'][:16]} reference_key {S['reference_key'][:16]} "
              f"(was {'unset' if not before['enumeration_key'] else before['enumeration_key'][:16]}) "
              f"-> {meta_path}", flush=True)


def main(argv=None):
    # The compiled note walk (tools/web/kernel): bit-identical to the Python path
    # and ~4x faster per song score. `enable()` sets it for this process and, via
    # the environment, for every worker spawned from here; it returns False and
    # changes nothing if there is no compiler. HBUILDER_NO_KERNEL=1 opts out.
    if not os.environ.get("HBUILDER_NO_KERNEL"):
        sys.path.insert(0, str(REPO / "tools" / "web" / "kernel"))
        import notewalk  # noqa: PLC0415

        notewalk.enable()
        # ...and the compiled Connect socket assignment, the same contract
        # (`board_plan.CONNECT_KERNEL`): ~280 -> ~40 us per developed team.
        import connect  # noqa: PLC0415

        connect.enable()
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["enumerate", "leaders", "transfer", "update", "rekey"])
    ap.add_argument("--song", default="m0334")
    ap.add_argument("--proxy", default="nonsinger", choices=list(PROXIES))
    ap.add_argument("--proxies", default="nonsinger,singer")
    ap.add_argument("--sources", default="m0334,m0139", help="transfer: songs whose enumerations feed the candidates")
    ap.add_argument("--budget", type=int, default=453)
    ap.add_argument("--uptime", default="finite")
    ap.add_argument("--platform", default="mobile", choices=["mobile", "pc"])
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--chunk", type=int, default=20000)
    ap.add_argument("--k", default="100,500,2000")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--from-dir", default=None,
                    help="update: directory holding the OLD enumeration (default: this probe's OUT_DIR)")
    ap.add_argument("--out-dir", default=None,
                    help="update/transfer: where the result is written (default: this probe's OUT_DIR)")
    ap.add_argument("--songs", default=None,
                    help="enumerate: a comma-separated list of songs to enumerate one after the "
                         "other (overrides --song); a song whose enumeration is already complete "
                         "on disk is skipped, a partial one resumes — so the loop can be "
                         "interrupted and restarted (the overnight batch builder)")
    args = ap.parse_args(argv)
    if args.mode == "enumerate" and args.songs:
        cmd_enumerate_many(args)
    elif args.mode == "enumerate":
        cmd_enumerate(args)
    elif args.mode == "transfer":
        cmd_transfer(args)
    elif args.mode == "update":
        cmd_update(args)
    elif args.mode == "rekey":
        cmd_rekey(args)
    else:
        cmd_leaders(args)


def _enumeration_complete(song: str, proxy: str) -> bool:
    """A finished meta AND a scores file of exactly n_sets doubles."""
    meta_path = OUT_DIR / f"members_first_{song}_{proxy}.meta.json"
    scores_path = OUT_DIR / f"members_first_{song}_{proxy}.scores.bin"
    if not (meta_path.is_file() and scores_path.is_file()):
        return False
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
    except ValueError:
        return False
    return bool(meta.get("finished")) and scores_path.stat().st_size == int(meta.get("n_sets") or 0) * 8


def cmd_enumerate_many(args):
    """`enumerate --songs a,b,c`: one enumeration per song, in order, each
    resumable (`cmd_enumerate` picks a partial scores file up at its last
    whole chunk); a song already complete on disk is skipped; a song that
    fails (no chart, ...) is reported and the loop goes on."""
    songs = [x.strip() for x in args.songs.split(",") if x.strip()]
    t_all = time.perf_counter()
    done, skipped, failed = [], [], []
    for i, song in enumerate(songs, 1):
        if _enumeration_complete(song, args.proxy) and not args.fresh:
            print(f"[{i}/{len(songs)}] {song}: complete on disk; skip", flush=True)
            skipped.append(song)
            continue
        print(f"[{i}/{len(songs)}] {song}: enumerating ({time.strftime('%H:%M:%S')})", flush=True)
        args.song = song
        try:
            cmd_enumerate(args)
            done.append(song)
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 — one song must not end the night
            print(f"[{i}/{len(songs)}] {song}: FAILED ({type(exc).__name__}: {exc}); continuing",
                  flush=True)
            failed.append(song)
    print(f"songs: {len(done)} enumerated, {len(skipped)} already complete, {len(failed)} failed "
          f"{failed if failed else ''}; {(time.perf_counter() - t_all) / 60:.1f} min total", flush=True)


if __name__ == "__main__":
    main()
