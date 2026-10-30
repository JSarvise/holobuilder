"""Exhaustive-with-dominance, fixed-leader, per-song certified team search.

An independent second route (besides branch-and-bound) to the best 5-member
roster for one leader on one chart, boards developed at a flat point budget. Method:

  1. load the catalogue exactly as tools/web/server.py `_optimize_rerank` does
     (load_inputs -> apply_collection maxed/assume_* -> skilltree/node_master ->
     node_effect_ids_from_master -> tree_for/budget_for -> optimize_and_rerank
     -> load_charts + score_song_detailed);
  2. build the candidate member pool (5-star cards; one card per holomem in a
     roster; the leader card may also be a member) and print each candidate's
     solo feature vector; collapse candidates that are dominance-identical
     (same holomem, type, generations and every skill slot equal) to one
     representative;
  3. enumerate EVERY legal 5-roster of the reduced pool with a CHEAP surrogate
     (board-off `score_fast`, in-run uptime model), sharded across processes,
     keeping the global top-N by surrogate;
  4. evaluate the top-N EXACTLY (develop the board with optimize_and_rerank at
     the flat budget, then score_song_detailed on the chart) through
     hbuilder.solver.parallel_refine.RefinePool, in batches, and report the
     best exact team plus the Spearman correlation surrogate-vs-exact.

Nothing here is imported by src/hbuilder; the web's search loads it by path. Run it
with PYTHONPATH="src;tools/web;tools/recon/proxy".

    python tools/recon/probes/exhaustive_song_leader.py --song m0334 \
        --leader card-04001-5-uniq-0042-00 --budget 453 --top 4000 --stage all
"""
from __future__ import annotations

import argparse
import dataclasses
import heapq
import importlib.util
import itertools
import json
import multiprocessing
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hbuilder.cli import (  # noqa: E402
    DEFAULT_BOARD_PATH, DEFAULT_CHARTS_PATH, apply_collection, load_inputs)
from hbuilder.domain.board_plan import (  # noqa: E402
    model_for_character, node_effect_ids_from_master, optimize_and_rerank,
    skill_tree_from_dumps)
from hbuilder.domain.scoring import score_fast, score_with_breakdown  # noqa: E402
from hbuilder.domain.scoring_config import load_scoring_config  # noqa: E402
from hbuilder.domain.song import load_charts, score_song_detailed  # noqa: E402
from hbuilder.domain.team import Team, TeamError  # noqa: E402
from hbuilder.solver.parallel_refine import RefineContext, RefinePool  # noqa: E402

CARDS_PATH = REPO_ROOT / "data" / "cards.json"
OWNED_PATH = REPO_ROOT / "data" / "owned.json"
DUMPS = REPO_ROOT / "tools" / "recon" / "proxy" / "dumps"
SKILLTREE_PATH = DUMPS / "skilltree.json"
NODE_MASTER_PATH = DUMPS / "master_SkillTreeNode.json"
CHARTS_PATH = REPO_ROOT / DEFAULT_CHARTS_PATH
# EASY / NORMAL / HARD live only here (`build_charts.py --all-out`)
CHARTS_ALL_PATH = CHARTS_PATH.with_name("charts_all.json")
PROXY_DIR = REPO_ROOT / "tools" / "recon" / "proxy"
OUT_DIR = REPO_ROOT / "tools" / "recon" / "RESEARCH-2026-08-18"


# ---------------------------------------------------------------------------
# Loading — mirrors server._optimize_rerank (basis=maxed, assume_*=max,
# uptime_model=in-run, flat budget).
# ---------------------------------------------------------------------------
def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _load_precompute():
    spec = importlib.util.spec_from_file_location(
        "precompute_optimisations", PROXY_DIR / "precompute_optimisations.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_context(uptime_model: str, flat_budget: int, song: str, difficulty: str = "EXPERT"):
    cards, owned, cfg, _prov = load_inputs(CARDS_PATH, OWNED_PATH, False)
    # `load_inputs` reads the CLI's cwd-relative `config.json`; run from another folder it
    # would silently fall back to DEFAULT_CONFIG (the on-demand engine's maxed in-general
    # #1 then scores 2,993,105 instead of 3,339,089). Anchored like the rest
    cfg = load_scoring_config(REPO_ROOT / "config.json")
    cards, basis = apply_collection(
        cards, None, owned_ids=[c.id for c in owned], maxed=True,
        assume_level="max", assume_bloom="max")
    if basis.bonuses is not None:
        cfg = dataclasses.replace(cfg, account=basis.bonuses)
    cfg = dataclasses.replace(cfg, active_uptime_model=uptime_model)
    skilltree = _read_json(SKILLTREE_PATH)
    node_master = _read_json(NODE_MASTER_PATH)
    effects = node_effect_ids_from_master(node_master)
    try:
        connect_cards = _load_precompute().connect_cards_for("full-collection-maxed")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! connect_cards unavailable ({exc}); sockets idealised", flush=True)
        connect_cards = None
    bench = (_read_json(REPO_ROOT / DEFAULT_BOARD_PATH) or {}).get("characters") or None
    name_by_chr = {"chr-" + c.id.split("-")[1]: c.member for c in cards}
    difficulty = (difficulty or "EXPERT").upper()
    library = load_charts(str(CHARTS_PATH if difficulty == "EXPERT" else CHARTS_ALL_PATH))
    chart = library.get(song, difficulty) if library is not None else None
    if chart is None:
        raise SystemExit(f"no {difficulty} chart for {song}")
    ctx = RefineContext(
        cards=tuple(cards), cfg=dataclasses.replace(cfg, board=None),
        charts=(chart,), skilltree=skilltree, node_master=node_master,
        node_effect_ids=effects, connect_cards=connect_cards,
        bench_characters=bench, name_by_chr=name_by_chr,
        flat_budget=flat_budget, granted={})
    return cards, cfg, ctx, chart


# ---------------------------------------------------------------------------
# Candidate features + dominance grouping
# ---------------------------------------------------------------------------
def _slot_key(slot):
    if slot is None:
        return None
    return (slot.kind, slot.family, slot.effect_type, repr(slot.duration),
            repr(slot.interval), repr(slot.probability_tier), repr(slot.magnitude),
            repr(slot.condition), repr(slot.target), repr(slot.override),
            repr(slot.rider))


def feature_key(card):
    """Everything a member contributes: stats, type, generations, the three
    normal skill slots (SPECIAL/ACTIVE/PASSIVE with magnitude, interval,
    duration, prob tier, condition, target, override, rider). The costume skill
    is NOT included: a member's costume never fires (team.py docstring)."""
    return (card.member, card.type, tuple(sorted(card.generations)),
            (card.stats.performance, card.stats.technique, card.stats.sense),
            tuple(_slot_key(s) for s in card.skills))


def feature_row(card):
    return {"id": card.id, "member": card.member, "type": card.type,
            "gens": sorted(card.generations), "costume": card.costume_name,
            "stats": [card.stats.performance, card.stats.technique, card.stats.sense],
            "skills": [None if s is None else {
                "kind": s.kind, "family": s.family, "effect": s.effect_type,
                "mag": repr(s.magnitude), "int": repr(s.interval),
                "dur": repr(s.duration), "prob": repr(s.probability_tier),
                "cond": repr(s.condition), "target": repr(s.target)}
                for s in card.skills]}


def build_pool(cards, leader_id: str):
    five = [c for c in cards if c.rarity.startswith("5")]
    groups: dict[tuple, list] = {}
    for c in five:
        groups.setdefault(feature_key(c), []).append(c)
    reps, dominated = [], []
    for key, members in groups.items():
        # keep the leader card itself as representative when it is in the group
        members = sorted(members, key=lambda c: (c.id != leader_id, c.id))
        reps.append(members[0])
        for d in members[1:]:
            dominated.append((d.id, members[0].id))
    reps.sort(key=lambda c: c.id)
    return five, reps, dominated


# ---------------------------------------------------------------------------
# Stage 3: surrogate enumeration, sharded by the first member index
# ---------------------------------------------------------------------------
_W: dict = {}


def _init_surrogate(pool, leader, cfg):
    _W["pool"] = pool
    _W["leader"] = leader
    _W["cfg"] = cfg


def _surrogate_shard(args):
    i, top = args
    pool, leader, cfg = _W["pool"], _W["leader"], _W["cfg"]
    n = len(pool)
    first = pool[i]
    heap: list = []
    count = 0
    for rest in itertools.combinations(range(i + 1, n), 4):
        members = (first, pool[rest[0]], pool[rest[1]], pool[rest[2]], pool[rest[3]])
        names = {m.member for m in members}
        if len(names) != 5:
            continue
        team = Team(leader=leader, members=members)
        s = score_fast(team, cfg)
        count += 1
        item = (s, (i,) + rest)
        if len(heap) < top:
            heapq.heappush(heap, item)
        elif s > heap[0][0]:
            heapq.heapreplace(heap, item)
    return count, heap


def stage_surrogate(pool, leader, cfg, top: int, workers: int):
    n = len(pool)
    t0 = time.perf_counter()
    tasks = [(i, top) for i in range(n - 4)]
    ctx = multiprocessing.get_context("spawn")
    total = 0
    heap: list = []
    with ctx.Pool(processes=workers, initializer=_init_surrogate,
                  initargs=(pool, leader, cfg)) as p:
        for count, part in p.imap_unordered(_surrogate_shard, tasks):
            total += count
            for item in part:
                if len(heap) < top:
                    heapq.heappush(heap, item)
                elif item[0] > heap[0][0]:
                    heapq.heapreplace(heap, item)
    ranked = sorted(heap, key=lambda t: -t[0])
    return total, ranked, time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Stage 4: exact evaluation of the top-N surrogate rosters
# ---------------------------------------------------------------------------
def stage_exact(ctx, leader, pool, ranked, batch: int, workers: int, out_path: Path,
                deadline_s: float | None):
    by_id = {c.id: c for c in ctx.cards}
    results: list[dict] = []
    if out_path.is_file():
        results = json.loads(out_path.read_text(encoding="utf-8"))
    done = {tuple(r["idx"]) for r in results}
    todo = [(rank, s, idx) for rank, (s, idx) in enumerate(ranked) if idx not in done]
    t0 = time.perf_counter()
    with RefinePool(ctx, workers=workers,
                    on_fallback=lambda line: print("  !", line, flush=True)) as rp:
        for b in range(0, len(todo), batch):
            if deadline_s is not None and time.perf_counter() - t0 > deadline_s:
                print(f"  deadline reached after {len(results)} exact evals", flush=True)
                break
            chunk = todo[b:b + batch]
            teams = [Team(leader=leader, members=tuple(pool[k] for k in idx))
                     for _r, _s, idx in chunk]
            tb = time.perf_counter()
            scores = rp.batch_score(teams)
            dt = time.perf_counter() - tb
            for (rank, s, idx), ex in zip(chunk, scores):
                results.append({"rank": rank, "surrogate": s, "exact": ex,
                                "idx": list(idx),
                                "ids": [pool[k].id for k in idx]})
            out_path.write_bytes(json.dumps(results, indent=0).encode("utf-8"))
            best = max(results, key=lambda r: r["exact"])
            print(f"  exact batch {b // batch + 1}: {len(chunk)} teams in {dt:.1f}s "
                  f"({dt / len(chunk) * 1000:.0f} ms/team); evaluated {len(results)}; "
                  f"best exact {best['exact']:,.0f} @surrogate-rank {best['rank']}",
                  flush=True)
    return results, time.perf_counter() - t0


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    return cov / (vx * vy) if vx and vy else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--song", default="m0334")
    ap.add_argument("--leader", default="card-04001-5-uniq-0042-00")
    ap.add_argument("--budget", type=int, default=453)
    ap.add_argument("--uptime", default="in-run")
    ap.add_argument("--top", type=int, default=4000)
    ap.add_argument("--exact-n", type=int, default=None,
                    help="how many of the top surrogate rosters to evaluate exactly")
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--stage", default="all",
                    choices=["explore", "surrogate", "exact", "all", "report", "sample"])
    ap.add_argument("--deadline", type=float, default=None,
                    help="seconds allowed for the exact stage (resumable)")
    ap.add_argument("--tag", default="")
    ap.add_argument("--sample", type=int, default=0,
                    help="ALSO score this many uniformly random legal rosters "
                         "(surrogate + exact) for a whole-space correlation")
    ap.add_argument("--seed", type=int, default=20260818)
    args = ap.parse_args()

    tag = args.tag or f"{args.song}_{args.leader.split('-')[-2]}"
    surrogate_path = OUT_DIR / f"exhaustive_{tag}_surrogate.json"
    exact_path = OUT_DIR / f"exhaustive_{tag}_exact.json"

    t0 = time.perf_counter()
    cards, cfg, ctx, chart = load_context(args.uptime, args.budget, args.song)
    by_id = {c.id: c for c in cards}
    leader = by_id[args.leader]
    print(f"loaded {len(cards)} cards in {time.perf_counter() - t0:.1f}s; "
          f"uptime={cfg.active_uptime_model} budget={args.budget} song={args.song}",
          flush=True)

    five, reps, dominated = build_pool(cards, args.leader)
    holomems = {c.member for c in five}
    print(f"5-star pool: {len(five)} cards, {len(holomems)} holomems; "
          f"after dominance-collapse: {len(reps)} representatives "
          f"({len(dominated)} dominated)", flush=True)
    for d, r in dominated:
        print(f"    dominated {d} == {r}")

    if args.stage in ("explore", "all"):
        # single-process timings of surrogate vs exact
        team = Team(leader=leader, members=tuple(reps[:5]) if len({c.member for c in reps[:5]}) == 5
                    else tuple(c for c in reps if c.member != leader.member)[:5])
        n = 200
        t = time.perf_counter()
        for _ in range(n):
            score_fast(team, cfg)
        dt_fast = (time.perf_counter() - t) / n
        t = time.perf_counter()
        for _ in range(20):
            score_with_breakdown(team, cfg)
        dt_bd = (time.perf_counter() - t) / 20
        t = time.perf_counter()
        from hbuilder.solver.parallel_refine import evaluate  # noqa: PLC0415
        pool_ = RefinePool(ctx, workers=1)
        ex = evaluate(ctx, by_id, pool_._tree_for, pool_._budget_for,
                      (leader.id, tuple(m.id for m in team.members)))
        dt_exact = time.perf_counter() - t
        t = time.perf_counter()
        ex = evaluate(ctx, by_id, pool_._tree_for, pool_._budget_for,
                      (leader.id, tuple(m.id for m in team.members)))
        dt_exact2 = time.perf_counter() - t
        print(f"timings: score_fast {dt_fast * 1000:.2f} ms; score_with_breakdown "
              f"{dt_bd * 1000:.2f} ms; exact (develop+chart) {dt_exact * 1000:.0f} ms "
              f"first / {dt_exact2 * 1000:.0f} ms warm -> {ex:,.0f}", flush=True)
        # legal roster count
        legal = 0
        nrep = len(reps)
        for combo in itertools.combinations(range(nrep), 5):
            if len({reps[k].member for k in combo}) == 5:
                legal += 1
        print(f"legal 5-rosters over {nrep} reps: {legal:,} (raw C={len(list(itertools.combinations(range(nrep), 5))) if nrep <= 70 else 'n/a'})",
              flush=True)
        (OUT_DIR / f"exhaustive_{tag}_features.json").write_bytes(json.dumps(
            {"leader": args.leader, "pool": [feature_row(c) for c in five],
             "representatives": [c.id for c in reps], "dominated": dominated,
             "legal_rosters": legal,
             "timings_ms": {"score_fast": dt_fast * 1000,
                            "score_with_breakdown": dt_bd * 1000,
                            "exact_first": dt_exact * 1000,
                            "exact_warm": dt_exact2 * 1000}},
            indent=1).encode("utf-8"))
        if args.stage == "explore":
            return

    ranked = None
    if args.stage in ("surrogate", "all"):
        total, ranked, dt = stage_surrogate(reps, leader, cfg, args.top, args.workers)
        print(f"surrogate stage: {total:,} legal rosters scored in {dt:.1f}s "
              f"({args.workers} workers); top-{len(ranked)} kept; "
              f"best surrogate {ranked[0][0]:,.0f}", flush=True)
        surrogate_path.write_bytes(json.dumps(
            {"total": total, "seconds": dt, "workers": args.workers,
             "pool": [c.id for c in reps],
             "ranked": [[s, list(idx)] for s, idx in ranked]}, indent=0).encode("utf-8"))
        if args.stage == "surrogate":
            return

    if ranked is None:
        doc = json.loads(surrogate_path.read_text(encoding="utf-8"))
        assert doc["pool"] == [c.id for c in reps], "pool drift vs saved surrogate"
        ranked = [(s, tuple(idx)) for s, idx in doc["ranked"]]

    if args.stage in ("exact", "all"):
        n_exact = args.exact_n or len(ranked)
        results, dt = stage_exact(ctx, leader, reps, ranked[:n_exact], args.batch,
                                  args.workers, exact_path, args.deadline)
        print(f"exact stage: {len(results)} evaluated in {dt:.0f}s", flush=True)

    if args.sample:
        import random  # noqa: PLC0415
        rng = random.Random(args.seed)
        n = len(reps)
        picks = set()
        while len(picks) < args.sample:
            idx = tuple(sorted(rng.sample(range(n), 5)))
            if len({reps[k].member for k in idx}) == 5:
                picks.add(idx)
        picks = sorted(picks)
        teams = [Team(leader=leader, members=tuple(reps[k] for k in idx)) for idx in picks]
        sur = [score_fast(t, cfg) for t in teams]
        t = time.perf_counter()
        with RefinePool(ctx, workers=args.workers) as rp:
            ex = rp.batch_score(teams)
        dt = time.perf_counter() - t
        rows = [{"idx": list(i), "surrogate": s_, "exact": e_}
                for i, s_, e_ in zip(picks, sur, ex)]
        (OUT_DIR / f"exhaustive_{tag}_sample.json").write_bytes(
            json.dumps(rows, indent=0).encode("utf-8"))
        print(f"RANDOM SAMPLE: {len(rows)} legal rosters in {dt:.0f}s; "
              f"Spearman(surrogate, exact) = {spearman(sur, ex):.4f}; "
              f"exact range {min(ex):,.0f}..{max(ex):,.0f}; "
              f"surrogate range {min(sur):,.0f}..{max(sur):,.0f}", flush=True)
        # Pearson too, and the ratio exact/surrogate spread
        ratios = [e_ / s_ for s_, e_ in zip(sur, ex)]
        print(f"  exact/surrogate ratio: min {min(ratios):.3f} median "
              f"{sorted(ratios)[len(ratios) // 2]:.3f} max {max(ratios):.3f}", flush=True)
        if args.stage == "sample":
            return

    # report
    results = json.loads(exact_path.read_text(encoding="utf-8"))
    results.sort(key=lambda r: r["rank"])
    xs = [r["surrogate"] for r in results]
    ys = [r["exact"] for r in results]
    rho = spearman(xs, ys)
    best = max(results, key=lambda r: r["exact"])
    print(f"REPORT: {len(results)} exact evals; Spearman(surrogate, exact) = {rho:.4f}")
    print(f"  best exact {best['exact']:,.0f} at surrogate rank {best['rank']} "
          f"(surrogate {best['surrogate']:,.0f})")
    print("  members: " + ", ".join(
        f"{by_id[i].member} '{by_id[i].costume_name}' ({i})" for i in best["ids"]))
    top10 = sorted(results, key=lambda r: -r["exact"])[:10]
    for r in top10:
        print(f"    exact {r['exact']:,.0f}  surrogate-rank {r['rank']:5d}  "
              f"surrogate {r['surrogate']:,.0f}  " +
              ", ".join(by_id[i].member.split()[0] + " " + by_id[i].costume_name
                        for i in r["ids"]))
    # windowed Spearman + how deep the exact top-k sits in the surrogate order
    for w in (100, 500, 1000, 2000, len(results)):
        sub = [r for r in results if r["rank"] < w]
        if len(sub) > 5:
            print(f"  Spearman over surrogate top-{w}: "
                  f"{spearman([r['surrogate'] for r in sub], [r['exact'] for r in sub]):.4f}")
    order_exact = sorted(results, key=lambda r: -r["exact"])
    for k in (1, 5, 10, 25, 50, 100):
        ranks = [r["rank"] for r in order_exact[:k]]
        print(f"  exact top-{k}: deepest surrogate rank {max(ranks)}")


if __name__ == "__main__":
    main()
