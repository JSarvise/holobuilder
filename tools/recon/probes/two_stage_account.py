"""The two-stage search on the ACCOUNT, checked against a complete enumeration of
sub-pools small enough to enumerate.

The maxed basis is the five-star catalogue at max level on a flat board budget. The
account is another landscape: every rarity it owns, each card at its real level /
bloom / limit break (`state/collection.json`), each character's board at the points
it has really earned (`board_export` -> `points.granted`), the account's own Connect
cards at their own level, and its Memory and Power-Up bonuses.

`build` is the account twin of `members_first_2026_08_19.build` — same keys, so
`two_stage_build.py` can cache this basis with the code that caches the maxed
one. The leaf is the same one (`RefinePool.batch_score` on the plan cache); the
plan cache's nominal team is the owned cards with the highest stat totals.

The check: the full account pool has ~10^8 legal member sets, which nobody
enumerates in a sitting, so the oracle is a SUB-POOL — `--sub N` cards, either
the N with the highest stat totals (`--pick top`: where the real answer lives,
and the hard case, because strong cards are close to one another) or N drawn
at random (`--pick random`) — every legal set of which is scored on the exact
leaf, then the search is run on that same sub-pool with the oracle as its
scorer and must find the enumeration's best.

    python tools/recon/probes/two_stage_account.py --song m0334 --sub 40
    python tools/recon/probes/two_stage_account.py --song m0334 --full   (the 114-card search, no oracle)
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import os
import random
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for _p in ("src", "tools/web", "tools/web/kernel", "tools/recon/proxy"):
    _sp = str(REPO / _p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

OUT_DIR = REPO / "tools/recon/RESEARCH-2026-08-18/PHASE-6/two_stage"
COLLECTION_PATH = REPO / "state" / "collection.json"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _total(card) -> int:
    return card.stats.performance + card.stats.technique + card.stats.sense


def _cards_and_cfg(args, ex):
    """(cards restated from the collection, owned ids, config) — everything the
    account's `reference_key` depends on, and cheap (no board export)."""
    from hbuilder.cli import apply_collection, load_inputs  # noqa: PLC0415

    cards, owned, cfg, _prov = load_inputs(ex.CARDS_PATH, ex.OWNED_PATH, False)
    owned_ids = [c.id for c in owned]
    # cards the user is TRYING as if owned (the Collection page's "Try as owned", so the
    # optimizer considers a card before pulling it): `trial` in state/collection.json, never
    # data/owned.json; the next account refresh clears it (account_snapshot.merge_collection)
    try:
        trial = json.loads(COLLECTION_PATH.read_text("utf-8")).get("trial") or []
    except (OSError, ValueError):
        trial = []
    known = {c.id for c in cards}
    owned_ids += [t for t in trial if t in known and t not in owned_ids]
    # `args.assume_level` (e.g. "max", 60) / `assume_bloom`: the what-if "my cards at
    # that level / bloom" (the other stays real); `keep_higher_*`: holodori's "do not
    # delevel / debloom" -- a card already above the assumption keeps its own
    cards, basis = apply_collection(cards, str(COLLECTION_PATH), owned_ids=owned_ids, maxed=False,
                                    assume_level=getattr(args, "assume_level", None),
                                    assume_bloom=getattr(args, "assume_bloom", None),
                                    keep_higher_level=bool(getattr(args, "keep_higher_level", False)),
                                    keep_higher_bloom=bool(getattr(args, "keep_higher_bloom", False)),
                                    assume_limit_break=getattr(args, "assume_limit_break", None))
    if basis.bonuses is not None:
        cfg = dataclasses.replace(cfg, account=basis.bonuses)
    cfg = dataclasses.replace(cfg, active_uptime_model=args.uptime, platform=args.platform, board=None)
    return cards, owned_ids, cfg


INVENTORY_PATH = REPO / "tools/recon/proxy/dumps/inventory.json"
BENCH_MODES = ("captured", "inventory", "infinite")


def _bench_key(bench: str) -> str:
    """What the developed bench depends on beyond the reference key: the mode,
    and with `inventory` the account's cube/hexa balance (a refresh rewrites it)."""
    import hashlib  # noqa: PLC0415

    if bench != "inventory":
        return f"bench={bench}"
    digest = hashlib.sha256(INVENTORY_PATH.read_bytes()).hexdigest() if INVENTORY_PATH.exists() else "none"
    return f"bench=inventory:{digest}"


def reference_key(args, bench: str = "captured") -> str:
    """The account basis's current key, without building the leaf context —
    what a reader needs to tell a fresh cached answer from a stale one."""
    import hashlib  # noqa: PLC0415

    import certificate as cert_mod  # noqa: PLC0415

    ex = _load("exhaustive_song_leader", "tools/recon/probes/exhaustive_song_leader.py")
    _cards, _owned, cfg = _cards_and_cfg(args, ex)
    _hash, per = cert_mod.inputs_hash(cfg, "account", cert_mod.MODEL_VERSION)
    key = cert_mod.reference_key(per, cert_mod.MODEL_VERSION)
    if bench == "captured":
        return key
    return hashlib.sha256(f"{key}\n{_bench_key(bench)}".encode("utf-8")).hexdigest()


def developed_bench(seed_ids: list[str], *, max_level: bool, resources: str) -> dict:
    """`precompute_optimisations.developed_bench_characters` (moved there so the
    web server shares it)."""
    spec = importlib.util.spec_from_file_location(
        "precompute_optimisations", REPO / "tools/recon/proxy/precompute_optimisations.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.developed_bench_characters(seed_ids, max_level=max_level, resources=resources)


# Board points a character has earned at the maximum character level (50): the sum of
# the `CharacterLevel` grants. The whole board costs 447-453, so even a level-50
# character cannot buy all of it.
MAX_LEVEL_POINTS = 361


def collapse_pool(cards, feature_key, split_key=None):
    """One representative (lowest id) per group of cards with the same
    `feature_key` and, when given, the same `split_key`. Returns `(reps sorted by
    id, [(dropped id, its representative's id), ...])`."""
    groups: dict[tuple, list] = {}
    for card in cards:
        key = feature_key(card)
        groups.setdefault(key if split_key is None else (key, split_key(card)), []).append(card)
    reps = sorted((sorted(g, key=lambda c: c.id)[0] for g in groups.values()), key=lambda c: c.id)
    dominated = [(d.id, sorted(g, key=lambda c: c.id)[0].id)
                 for g in groups.values() for d in sorted(g, key=lambda c: c.id)[1:]]
    return reps, dominated


def build(args, proxy_card_id: str | None = None, flat_budget: int | None = None,
          bench: str = "captured", split_key=None, board_state=None):
    """The account basis as `members_first_2026_08_19.build` returns the maxed
    one: `reps` = the owned cards (one per distinct feature set, EVERY rarity),
    `ctx` = the leaf's context with per-character real budgets — or, with
    `flat_budget`, every character's board planned with that many points (the
    "characters at max level" what-if: `MAX_LEVEL_POINTS`). `bench`: the boards
    of the characters off the team as `captured`, or with their idle points spent
    on green (`developed_bench`) within the account's `inventory` or `infinite`.

    `split_key(card)` is added to the collapse key: cards the leaf cannot tell
    apart but something else can (an event bonus by card id or bloom) stay
    separate pool entries instead of one representative standing for both.

    `board_state` (`{chr: {nodes, slots}}`, e.g. board_state.json): the team's
    boards AS THEY ARE instead of planned (`RefineContext.board_state`)."""
    if bench not in BENCH_MODES:
        raise ValueError(f"bench must be one of {BENCH_MODES}, got {bench!r}")
    mfp = _load("_mfp_two_stage_account", "tools/recon/probes/members_first_2026_08_19.py")
    mf, cert_mod = mfp.mf, mfp.cert_mod
    ex = _load("exhaustive_song_leader", "tools/recon/probes/exhaustive_song_leader.py")
    from hbuilder.cli import DEFAULT_BOARD_PATH  # noqa: PLC0415
    from hbuilder.domain.board_plan import (  # noqa: PLC0415
        bench_sockets_from_state, node_effect_ids_from_master)
    from hbuilder.domain.song import load_charts  # noqa: PLC0415
    from hbuilder.solver.parallel_refine import RefineContext  # noqa: PLC0415

    cards, owned_ids, cfg = _cards_and_cfg(args, ex)
    skilltree = ex._read_json(ex.SKILLTREE_PATH)
    node_master = ex._read_json(ex.NODE_MASTER_PATH)
    precompute = ex._load_precompute()
    export = precompute.board_export.build(gain=False, pool=1, scenario="account")
    granted = {c["chr"]: int((c.get("points") or {}).get("granted") or 0)
               for c in export.get("characters", [])}
    granted = {k: v for k, v in granted.items() if v}
    # the Connect levels follow an assumed bloom too (bloom 5 = level 2), as the stats do
    connect_cards = precompute.connect_cards_for(
        "account", assume_bloom=getattr(args, "assume_bloom", None),
        keep_higher_bloom=bool(getattr(args, "keep_higher_bloom", False)))
    bench_captured = (ex._read_json(REPO / DEFAULT_BOARD_PATH) or {}).get("characters") or None
    bench_mode = bench
    difficulty = cert_mod.normalize_difficulty(getattr(args, "difficulty", None))
    library = load_charts(str(cert_mod.charts_path_for(
        difficulty, charts_path=ex.CHARTS_PATH, charts_all_path=ex.CHARTS_ALL_PATH)))
    chart = library.get(args.song, difficulty) if library is not None else None
    if chart is None:
        raise SystemExit(f"no {difficulty} chart for {args.song}")

    by_id = {c.id: c for c in cards}
    mine = [by_id[i] for i in owned_ids if i in by_id]
    reps, dominated = collapse_pool(mine, ex.feature_key, split_key)
    # the plan cache's nominal team: the six strongest owned cards, six holomems
    seed, names = [], set()
    for card in sorted(reps, key=lambda c: (-_total(c), c.id)):
        if card.member not in names:
            seed.append(card)
            names.add(card.member)
        if len(seed) == 6:
            break
    proxy = None
    cards = list(cards)
    if proxy_card_id is None:
        singers = set(chart.singers or ())
        proxy_card_id = next(c.id for c in sorted(reps, key=lambda c: (-_total(c), c.id))
                             if "chr-" + c.id.split("-")[1] not in singers)
    if bench != "captured":
        bench = developed_bench([c.id for c in seed], max_level=flat_budget is not None,
                                resources=bench) or bench_captured
    else:
        bench = bench_captured
    proxy = mf.bare_leader(by_id[proxy_card_id])
    cards.append(proxy)
    by_id[proxy.id] = proxy
    ctx = RefineContext(
        cards=tuple(cards), cfg=dataclasses.replace(cfg, board=None), charts=(chart,),
        skilltree=skilltree, node_master=node_master,
        node_effect_ids=node_effect_ids_from_master(node_master), connect_cards=connect_cards,
        bench_characters=bench, name_by_chr={"chr-" + c.id.split("-")[1]: c.member for c in cards},
        flat_budget=flat_budget, granted=granted, use_plan_cache=True,
        plan_cache_seed=(seed[0].id, tuple(c.id for c in seed[1:])), board_state=board_state,
        # what the off-team sockets hold: the team may not also socket a card a bench
        # board keeps lifting (`board_plan.held_bench_cards`)
        bench_sockets=bench_sockets_from_state(ex._read_json(
            REPO / "tools/recon/proxy/dumps/board_state.json")))
    ih, per = cert_mod.inputs_hash(ctx.cfg, "account", cert_mod.MODEL_VERSION)
    return dict(ex=ex, mfp=mfp, cards=cards, by_id=by_id, ctx=ctx, chart=chart, reps=reps,
                pool_ids=[c.id for c in reps], hol=tuple(c.member for c in reps), proxy=proxy,
                inputs_hash=ih, inputs_hash_files=per, dominated=dominated, granted=granted,
                reference_key=reference_key(args, bench_mode), bench=bench_mode)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--song", default="m0334")
    ap.add_argument("--difficulty", default="EXPERT",
                    help="chart difficulty: EASY, NORMAL, HARD or EXPERT (default)")
    ap.add_argument("--uptime", default="finite")
    ap.add_argument("--platform", default="mobile")
    ap.add_argument("--leader", default="", help="a real leader card id (default: the bare proxy)")
    ap.add_argument("--sub", type=int, default=40, help="sub-pool size to enumerate")
    ap.add_argument("--pick", choices=("top", "random"), default="top")
    ap.add_argument("--pools", type=int, default=1, help="how many random sub-pools (--pick random)")
    ap.add_argument("--seeds", type=int, default=10, help="search runs per sub-pool")
    ap.add_argument("--full", action="store_true", help="search the whole account pool (no oracle)")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--tag", default="")
    args = ap.parse_args(argv)

    import connect  # noqa: PLC0415
    import notewalk  # noqa: PLC0415
    import two_stage  # noqa: PLC0415
    from hbuilder.solver import exact_row as er  # noqa: PLC0415

    if not os.environ.get("HBUILDER_NO_KERNEL"):
        notewalk.enable()
        connect.enable()
    built = build(args)
    mfp, reps, by_id = built["mfp"], built["reps"], built["by_id"]
    mf = mfp.mf
    leader = by_id[args.leader] if args.leader else built["proxy"]
    workers = args.workers or mfp.default_workers()
    hard = "" if built["chart"].difficulty == "EXPERT" else f"_{built['chart'].difficulty.lower()}"
    log = mfp.Log(OUT_DIR / f"two_stage_account_{args.song}{hard}{args.tag}.log")
    rarities = {}
    for card in reps:
        rarities[card.rarity[:1]] = rarities.get(card.rarity[:1], 0) + 1
    log(f"account basis: {len(reps)} owned cards after de-duplication ({len(built['dominated'])} "
        f"identical-feature copies dropped), rarities {dict(sorted(rarities.items()))}; "
        f"{len(built['granted'])} characters with board points, "
        f"{min(built['granted'].values())}-{max(built['granted'].values())} each; "
        f"leader {leader.member} / {leader.costume_name}{' (bare proxy)' if not args.leader else ''}; "
        f"song {args.song} ({built['chart'].difficulty}); reference_key {built['reference_key'][:16]}")
    report = {"song": args.song, "difficulty": built["chart"].difficulty, "leader": leader.id, "bare_proxy": not args.leader,
              "pool": len(reps), "reference_key": built["reference_key"], "runs": []}

    with mfp.pr.RefinePool(built["ctx"], workers=workers,
                           on_fallback=lambda line: log(f"  ! {line}")) as pool:
        def scorer(cards):
            return lambda sets: mf.score_sets(leader, cards, sets, pool.batch_score)

        if args.full:
            t0 = time.perf_counter()
            found = two_stage.search(reps, scorer(reps))
            best, members = found["ranking"][0]
            log(f"FULL pool: best {best:,.0f} = {[reps[i].member for i in members]} "
                f"({[reps[i].rarity[:1] for i in members]}); {found['stats']['exact_evals']:,} exact "
                f"of {found['stats']['legal_sets']:,} legal sets, stop={found['stats']['stop']}, "
                f"{time.perf_counter() - t0:.1f}s")
            report["full"] = {"best": best, "members": [reps[i].id for i in members],
                              "stats": found["stats"]}
        else:
            ranked = sorted(range(len(reps)), key=lambda i: (-_total(reps[i]), reps[i].id))
            rng = random.Random(20261014)
            pools = ([sorted(ranked[:args.sub])] if args.pick == "top"
                     else [sorted(rng.sample(range(len(reps)), args.sub)) for _ in range(args.pools)])
            for number, chosen in enumerate(pools, 1):
                sub = [reps[i] for i in chosen]
                hol = [c.member for c in sub]
                legal = list(er.legal_rosters(hol, 5))
                t0 = time.perf_counter()
                truth = {}
                for start in range(0, len(legal), 20_000):
                    part = legal[start:start + 20_000]
                    truth.update(zip(part, scorer(sub)(part)))
                seconds = time.perf_counter() - t0
                order = sorted(truth, key=lambda s: (-truth[s], s))
                best = order[0]
                log(f"sub-pool {number}/{len(pools)} ({args.pick}, {len(sub)} cards): {len(legal):,} legal "
                    f"sets enumerated in {seconds:.1f}s ({seconds / len(legal) * 1e6 * workers:.0f} "
                    f"us/set/worker); best {truth[best]:,.0f} = {[sub[i].member for i in best]}; "
                    f"#2 {truth[order[1]] / truth[best] - 1:+.3%}, #10 {truth[order[9]] / truth[best] - 1:+.3%}")
                top10, top100 = set(order[:10]), set(order[:100])
                hits = 0
                rows = []
                for seed in range(args.seeds):
                    asked = []

                    def oracle(sets):
                        asked.extend(sets)
                        return [truth[s] for s in sets]     # a KeyError here = an illegal set proposed

                    found = two_stage.search(sub, oracle, seed=20261013 + seed)
                    got_score, got = found["ranking"][0]
                    scored = {s for _v, s in found["ranking"]}
                    ok = got == best and got_score == truth[best]
                    hits += ok
                    rows.append({"seed": seed, "found_best": ok, "exact_evals": len(scored),
                                 "top10": len(top10 & scored), "top100": len(top100 & scored),
                                 "stop": found["stats"]["stop"],
                                 "loss": got_score / truth[best] - 1})
                evals = [r["exact_evals"] for r in rows]
                log(f"  search x{args.seeds}: true best found {hits}/{args.seeds}; exact evals "
                    f"{min(evals):,}-{max(evals):,} of {len(legal):,} "
                    f"({max(evals) / len(legal):.1%} at most); top 10 found min "
                    f"{min(r['top10'] for r in rows)}, top 100 min {min(r['top100'] for r in rows)}; "
                    f"worst loss {min(r['loss'] for r in rows):+.3%}")
                report["runs"].append({"pick": args.pick, "cards": [c.id for c in sub],
                                       "legal_sets": len(legal), "enumeration_seconds": seconds,
                                       "best": truth[best], "best_members": [sub[i].id for i in best],
                                       "second_gap": truth[order[1]] / truth[best] - 1,
                                       "searches": rows})
    out = OUT_DIR / f"two_stage_account_{args.song}{hard}{args.tag}.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    log(f"wrote {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
