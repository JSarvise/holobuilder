"""The exact leaf's inputs and the keys that tell fresh inputs from stale ones.

The web's search scores teams on an exact leaf: a `RefineContext` over the
pool's representative cards, the song's chart and every character's board.
This module builds those inputs on each basis the web offers:

* `build_maxed` — the five-star catalogue at max level and bloom, every board
  planned at a flat point budget (by default what a max-level holomem holds,
  `max_level_board_points`), a bare proxy leader for the members-first
  enumeration (`hbuilder.solver.members_first`).
* `build_account` — every owned card at its real level, bloom and limit break
  (`state/collection.json`), each character's board at the points it has
  earned, the account's own Connect cards and its Memory and Power-Up bonuses.
* `leaf_inputs` — the dispatcher over `BASES`, which also gives the `maxed`
  basis its developed bench.

`keys_for` / `today_keys` digest the scoring inputs, the chart of each song,
`SETTINGS` and the maxed board budget, so a reader compares keys instead of
trusting a file's age.
`global_ranking`, `alternatives`, `_slot_orders` and `_refine_rows` turn the
leaf's per-leader rows into the ranked teams the pages show.

Nothing here writes data/ or state/.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Sequence

_SRC = str(Path(__file__).resolve().parents[2] / "src")   # this checkout's hbuilder
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
from hbuilder import paths  # noqa: E402
from hbuilder.errors import MissingInput  # noqa: E402

REPO = paths.REPO
for _p in (paths.WEB_DIR, paths.REFRESH_DIR, paths.WEB_DIR / "kernel"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hbuilder.inputs import (  # noqa: E402
    DEFAULT_BOARD_PATH, DEFAULT_CHARTS_PATH, apply_collection, load_inputs,
    max_level_board_points)
from hbuilder.domain.board_plan import node_effect_ids_from_master  # noqa: E402
from hbuilder.domain.song import load_charts  # noqa: E402
from hbuilder.solver import members_first as mf  # noqa: E402
from hbuilder.solver.parallel_refine import RefineContext  # noqa: E402
import certificate as cert_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CARDS_PATH = REPO / "data" / "cards.json"
OWNED_PATH = REPO / "data" / "owned.json"
DUMPS = paths.DUMPS
SKILLTREE_PATH = DUMPS / "skilltree.json"
NODE_MASTER_PATH = DUMPS / "master_SkillTreeNode.json"
CHARTS_PATH = REPO / DEFAULT_CHARTS_PATH
# EASY / NORMAL / HARD live only here (`build_charts.py --all-out`)
CHARTS_ALL_PATH = CHARTS_PATH.with_name("charts_all.json")
REFRESH_DIR = paths.REFRESH_DIR


# ---------------------------------------------------------------------------
# Settings and bases
# ---------------------------------------------------------------------------
# Part of every key: bump it when the search or the leaf changes, and every key changes with it.
VERSION = "two-stage-build-4"
# The maxed basis' flat board budget is not here: it is read from the master
# (`maxed_budget`) and added to every key by `keys_for`.
SETTINGS = {"uptime": "finite", "platform": "mobile", "proxy": "nonsinger",
            "margin": 0.04, "max_searched": 12, "triage_top": 300, "tier": 100,
            "general": {"sample": 16, "keep": 100, "triage_top": 150, "tier": 50},
            "seed": 20261013,
            # The short list re-scored with the team's activation nodes chosen jointly
            # (`RefineContext.refine_activation`): every row within `margin` of the best,
            # at most `cap` of them, then re-sorted. The search itself stays plain
            # (the refinement costs ~0.1-0.2 s a team against ~0.5 ms).
            "refine": {"margin": 0.04, "cap": 30},
            # per song row, the next best member sets under the same leader
            # (`alternatives[]`, plain leaf, not refined)
            "alternatives": 4,
            # the GLOBAL top teams of the song (the 10 best teams may share one
            # leader): every leader's ranking merged, best `size` kept
            "ranking": {"size": 50}}
# the bases the leaf is built on (basis -> its short tag). The maxed basis keys
# carry no basis line.
BASES = {"maxed": "", "account": "account_", "account-max": "accountmax_",
         # every owned card at its max level (blooms real): "Upgraded cards, level Max" on
         # the page; with -max also the boards at max points and unlimited materials
         # (on demand only)
         "account-levelmax": "accountlvmax_", "account-levelmax-max": "accountlvmaxmax_"}
# The other characters' boards on each account basis. On the max-level what-if
# ("max board points + infinite cubes/hexas") their idle points go to
# account-wide green without limit: +12..16 % on three measured winners. On the
# account as it is they stay as captured: spending the idle points within the
# account's own wallet moves the same winners by only -0.3..+0.35 %.
BENCH = {"account": "captured", "account-max": "infinite",
         "account-levelmax": "captured", "account-levelmax-max": "infinite"}
# the account bases whose cards are restated at their max level
LEVEL_MAX = ("account-levelmax", "account-levelmax-max")
# `{points}`: the max-level board points (`basis_text`)
BASIS_TEXT = {
    "maxed": "maxed five-star catalogue, every holomem at max level ({points} board points), "
             "boards rebuilt per song",
    "account": "your cards at their real level and bloom, your real board points, "
               "boards rebuilt per song",
    "account-max": "your cards at their real level and bloom, every character at max level "
                   "({points} board points), unlimited cubes/hexas (the other characters' green too), boards rebuilt per song",
    "account-levelmax": "your cards at their MAX level (blooms real), your real board points, "
                        "boards rebuilt per song",
    "account-levelmax-max": "your cards at their MAX level (blooms real), every character at max level "
                            "({points} board points), unlimited cubes/hexas: holodori.best's default assumptions",
}
DEFAULT_DIFFICULTY = "EXPERT"


def maxed_budget() -> int:
    """The flat board budget of every team holomem on the maxed basis and the max-level
    what-ifs: the points a max-level holomem holds (`max_level_board_points`, from the
    master's `CharacterLevel`). The whole tree costs more, so no account can complete a
    board. Raises `MissingInput` when the master export is absent."""
    return max_level_board_points()


def basis_text(basis: str) -> str:
    """`BASIS_TEXT[basis]` with the max-level board points filled in."""
    text = BASIS_TEXT[basis]
    if "{points}" not in text:
        return text
    try:
        points = str(maxed_budget())
    except MissingInput:
        points = "max-level"
    return text.replace("{points}", points)


# ---------------------------------------------------------------------------
# The maxed basis
# ---------------------------------------------------------------------------
def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _load_precompute():
    spec = importlib.util.spec_from_file_location(
        "precompute_optimisations", REFRESH_DIR / "precompute_optimisations.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_context(uptime_model: str, flat_budget: int, song: str, difficulty: str = "EXPERT"):
    cards, owned, cfg, _prov = load_inputs(CARDS_PATH, OWNED_PATH)
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
    bench = (_read_json(REPO / DEFAULT_BOARD_PATH) or {}).get("characters") or None
    name_by_chr = {"chr-" + c.id.split("-")[1]: c.member for c in cards}
    difficulty = (difficulty or "EXPERT").upper()
    library = load_charts(str(CHARTS_PATH if difficulty == "EXPERT" else CHARTS_ALL_PATH))
    chart = library.get(song, difficulty) if library is not None else None
    if chart is None:
        raise MissingInput(f"no {difficulty} chart for {song}")
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


def build_pool(cards, leader_id: str):
    five = [c for c in cards if c.rarity.startswith("5")]
    groups: dict[tuple, list] = {}
    for c in five:
        groups.setdefault(feature_key(c), []).append(c)
    reps, dominated = [], []
    for members in groups.values():
        # keep the leader card itself as representative when it is in the group
        members = sorted(members, key=lambda c: (c.id != leader_id, c.id))
        reps.append(members[0])
        for d in members[1:]:
            dominated.append((d.id, members[0].id))
    reps.sort(key=lambda c: c.id)
    return five, reps, dominated


TABLE_SEED = ("card-04001-5-uniq-0042-00",
              ("card-04001-5-uniq-0042-00", "card-00032-5-uniq-0026-00",
               "card-04003-5-uniq-0044-00", "card-00007-5-uniq-0008-00",
               "card-00006-5-uniq-0007-00"))


# bare proxies: a non-singer of m0334 (Hakos Baelz RC) and the singer (Calli PDW)
PROXIES = {"nonsinger": "card-04012-5-uniq-0051-00", "singer": "card-04001-5-uniq-0042-00"}


def build_maxed(args, proxy_card_id: str | None):
    """The maxed basis' leaf inputs. `args.budget` is the flat board budget of every
    team holomem; None (or absent) = `maxed_budget()`."""
    difficulty = cert_mod.normalize_difficulty(getattr(args, "difficulty", None))
    budget = getattr(args, "budget", None)
    budget = maxed_budget() if budget is None else int(budget)
    cards, cfg, ctx, chart = load_context(args.uptime, budget, args.song, difficulty)
    ctx = dataclasses.replace(ctx, cfg=dataclasses.replace(ctx.cfg, platform=args.platform))
    by_id = {c.id: c for c in cards}
    proxy = None
    if proxy_card_id:
        proxy = mf.bare_leader(by_id[proxy_card_id])
        cards = list(cards) + [proxy]
        by_id[proxy.id] = proxy
        ctx = dataclasses.replace(ctx, cards=tuple(cards))
    five, reps, dominated = build_pool([c for c in cards if not mf.is_bare(c)], TABLE_SEED[0])
    ctx = dataclasses.replace(ctx, use_plan_cache=True, plan_cache_seed=TABLE_SEED)
    ih, per = cert_mod.inputs_hash(ctx.cfg, "maxed", cert_mod.MODEL_VERSION)
    # EXPERT reads charts.json exactly as before (its enumeration keys do not
    # move); another difficulty digests ITS chart in charts_all.json, so an
    # enumeration on it can never pass for the EXPERT one
    cd = cert_mod.chart_digest(args.song, charts_path=cert_mod.charts_path_for(difficulty),
                               difficulty=difficulty)
    if cd is None:
        raise MissingInput(f"no {difficulty} chart for {args.song} in "
                           f"{cert_mod.charts_path_for(difficulty).name}")
    return dict(cards=cards, by_id=by_id, ctx=ctx, chart=chart, reps=reps,
                pool_ids=[c.id for c in reps], hol=tuple(c.member for c in reps),
                proxy=proxy, inputs_hash=ih, inputs_hash_files=per, dominated=dominated,
                chart_digest=cd,
                enumeration_key=cert_mod.enumeration_key(per, cd, cert_mod.MODEL_VERSION),
                reference_key=cert_mod.reference_key(per, cert_mod.MODEL_VERSION),
                pool_features={c.id: feature_digest(c) for c in reps},
                proxy_feature=feature_digest(proxy) if proxy else None)


def feature_digest(card) -> str:
    """sha256 of `feature_key(card)` — everything a
    member contributes (the outfit excluded). Recorded per pool card so a
    reader can verify that a card is unchanged (the whole-file `inputs_hash`
    cannot tell an addition from an edit)."""
    return hashlib.sha256(repr(feature_key(card)).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The account basis
# ---------------------------------------------------------------------------
COLLECTION_PATH = paths.STATE / "collection.json"


def _total(card) -> int:
    return card.stats.performance + card.stats.technique + card.stats.sense


def account_cards_and_cfg(args):
    """(cards restated from the collection, owned ids, config) — everything the
    account's `reference_key` depends on, and cheap (no board export)."""
    from hbuilder.inputs import apply_collection, load_inputs  # noqa: PLC0415

    cards, owned, cfg, _prov = load_inputs(CARDS_PATH, OWNED_PATH)
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


INVENTORY_PATH = DUMPS / "inventory.json"


BENCH_MODES = ("captured", "inventory", "infinite")


def _bench_key(bench: str) -> str:
    """What the developed bench depends on beyond the reference key: the mode,
    and with `inventory` the account's cube/hexa balance (a refresh rewrites it)."""
    import hashlib  # noqa: PLC0415

    if bench != "inventory":
        return f"bench={bench}"
    digest = hashlib.sha256(INVENTORY_PATH.read_bytes()).hexdigest() if INVENTORY_PATH.exists() else "none"
    return f"bench=inventory:{digest}"


def account_reference_key(args, bench: str = "captured") -> str:
    """The account basis's current key, without building the leaf context —
    what a reader needs to tell fresh inputs from stale ones."""
    import hashlib  # noqa: PLC0415

    import certificate as cert_mod  # noqa: PLC0415

    _cards, _owned, cfg = account_cards_and_cfg(args)
    _hash, per = cert_mod.inputs_hash(cfg, "account", cert_mod.MODEL_VERSION)
    key = cert_mod.reference_key(per, cert_mod.MODEL_VERSION)
    if bench == "captured":
        return key
    return hashlib.sha256(f"{key}\n{_bench_key(bench)}".encode("utf-8")).hexdigest()


def developed_bench(seed_ids: list[str], *, max_level: bool, resources: str) -> dict:
    """`precompute_optimisations.developed_bench_characters` (moved there so the
    web server shares it)."""
    spec = importlib.util.spec_from_file_location(
        "precompute_optimisations", REFRESH_DIR / "precompute_optimisations.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.developed_bench_characters(seed_ids, max_level=max_level, resources=resources)


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


def build_account(args, proxy_card_id: str | None = None, flat_budget: int | None = None,
          bench: str = "captured", split_key=None, board_state=None):
    """The account basis as `build_maxed` returns the maxed
    one: `reps` = the owned cards (one per distinct feature set, EVERY rarity),
    `ctx` = the leaf's context with per-character real budgets — or, with
    `flat_budget`, every character's board planned with that many points (the
    "characters at max level" what-if: `maxed_budget()`). `bench`: the boards
    of the characters off the team as `captured`, or with their idle points spent
    on green (`developed_bench`) within the account's `inventory` or `infinite`.

    `split_key(card)` is added to the collapse key: cards the leaf cannot tell
    apart but something else can (an event bonus by card id or bloom) stay
    separate pool entries instead of one representative standing for both.

    `board_state` (`{chr: {nodes, slots}}`, e.g. board_state.json): the team's
    boards AS THEY ARE instead of planned (`RefineContext.board_state`)."""
    if bench not in BENCH_MODES:
        raise ValueError(f"bench must be one of {BENCH_MODES}, got {bench!r}")
    from hbuilder.inputs import DEFAULT_BOARD_PATH  # noqa: PLC0415
    from hbuilder.domain.board_plan import (  # noqa: PLC0415
        bench_sockets_from_state, node_effect_ids_from_master)
    from hbuilder.domain.song import load_charts  # noqa: PLC0415
    from hbuilder.solver.parallel_refine import RefineContext  # noqa: PLC0415

    cards, owned_ids, cfg = account_cards_and_cfg(args)
    skilltree = _read_json(SKILLTREE_PATH)
    node_master = _read_json(NODE_MASTER_PATH)
    precompute = _load_precompute()
    export = precompute.board_export.build(gain=False, pool=1, scenario="account")
    granted = {c["chr"]: int((c.get("points") or {}).get("granted") or 0)
               for c in export.get("characters", [])}
    granted = {k: v for k, v in granted.items() if v}
    # the Connect levels follow an assumed bloom too (bloom 5 = level 2), as the stats do
    connect_cards = precompute.connect_cards_for(
        "account", assume_bloom=getattr(args, "assume_bloom", None),
        keep_higher_bloom=bool(getattr(args, "keep_higher_bloom", False)))
    bench_captured = (_read_json(REPO / DEFAULT_BOARD_PATH) or {}).get("characters") or None
    bench_mode = bench
    difficulty = cert_mod.normalize_difficulty(getattr(args, "difficulty", None))
    library = load_charts(str(cert_mod.charts_path_for(
        difficulty, charts_path=CHARTS_PATH, charts_all_path=CHARTS_ALL_PATH)))
    chart = library.get(args.song, difficulty) if library is not None else None
    if chart is None:
        raise MissingInput(f"no {difficulty} chart for {args.song}")

    by_id = {c.id: c for c in cards}
    mine = [by_id[i] for i in owned_ids if i in by_id]
    reps, dominated = collapse_pool(mine, feature_key, split_key)
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
        bench_sockets=bench_sockets_from_state(_read_json(
            DUMPS / "board_state.json")))
    ih, per = cert_mod.inputs_hash(ctx.cfg, "account", cert_mod.MODEL_VERSION)
    return dict(cards=cards, by_id=by_id, ctx=ctx, chart=chart, reps=reps,
                pool_ids=[c.id for c in reps], hol=tuple(c.member for c in reps), proxy=proxy,
                inputs_hash=ih, inputs_hash_files=per, dominated=dominated, granted=granted,
                reference_key=account_reference_key(args, bench_mode), bench=bench_mode)


# ---------------------------------------------------------------------------
# Keys and the leaf's inputs
# ---------------------------------------------------------------------------
def _difficulty(difficulty: str | None) -> str:
    """Upper-cased, EXPERT for None / ""; ValueError on an unknown name."""
    import certificate as cert_mod  # noqa: PLC0415

    return cert_mod.normalize_difficulty(difficulty)


def charts_path(difficulty: str = DEFAULT_DIFFICULTY) -> Path:
    """charts.json for EXPERT (its digests unchanged), charts_all.json else."""
    import certificate as cert_mod  # noqa: PLC0415

    return cert_mod.charts_path_for(difficulty)


def _namespace(song: str = "m0334", difficulty: str = DEFAULT_DIFFICULTY,
               basis: str = "") -> argparse.Namespace:
    # budget None: `build_maxed` reads the max-level board points (the account bases
    # take theirs from `build_account`'s `flat_budget`)
    ns = argparse.Namespace(song=song, budget=None, uptime=SETTINGS["uptime"],
                            platform=SETTINGS["platform"], difficulty=_difficulty(difficulty))
    if basis in LEVEL_MAX:
        ns.assume_level = "max"
    return ns


def today_keys(basis: str = "maxed",
               difficulty: str = DEFAULT_DIFFICULTY) -> tuple[dict[str, str], str]:
    """`(key per song, general key)` of today's inputs on `basis` at
    `difficulty`, WITHOUT building the leaf — for a reader (the web) telling
    fresh from stale."""
    import certificate as cert_mod  # noqa: PLC0415

    if basis in BENCH:
        reference = account_reference_key(_namespace(basis=basis), BENCH[basis])
    else:
        # `ctx.cfg`, as `members_first.build` hashes it — not the raw config
        _cards, _cfg, ctx, _chart = load_context(SETTINGS["uptime"], maxed_budget(), "m0334")
        cfg = dataclasses.replace(ctx.cfg, platform=SETTINGS["platform"])
        _hash, per = cert_mod.inputs_hash(cfg, "maxed", cert_mod.MODEL_VERSION)
        reference = cert_mod.reference_key(per, cert_mod.MODEL_VERSION)
    name = _difficulty(difficulty)
    return keys_for(reference, chart_digests(charts_path(name), name), basis, name)


def chart_digests(charts_path: Path, difficulty: str = DEFAULT_DIFFICULTY) -> dict[str, str]:
    """music id -> sha256 of the canonical JSON of its chart entry at
    `difficulty` (EXPERT by default)."""
    wanted = _difficulty(difficulty)
    raw = json.loads(charts_path.read_text("utf-8"))
    out = {}
    entries = raw["charts"]
    for entry in (entries.values() if isinstance(entries, dict) else entries):
        if str(entry.get("difficulty", "")).upper() == wanted:
            out[entry["music_id"]] = hashlib.sha256(
                json.dumps(entry, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return out


def keys_for(reference_key: str, digests: dict[str, str],
             basis: str = "maxed",
             difficulty: str = DEFAULT_DIFFICULTY) -> tuple[dict[str, str], str]:
    """`(key per song, key of the general answer)` for today's inputs. EXPERT
    adds nothing to the key (EXPERT keys carry no difficulty line); any other difficulty
    adds a `difficulty=` line, so its answers can never pass for EXPERT ones. The
    max-level board points (`maxed_budget`, None when the master export is absent)
    sit with `SETTINGS`, so a game update that moves them moves every key."""
    try:
        budget = maxed_budget()
    except MissingInput:
        budget = None
    settings = json.dumps({**SETTINGS, "budget": budget}, sort_keys=True)
    base = f"{VERSION}\n{settings}\n{reference_key}\n"
    if basis != "maxed":
        base += f"basis={basis}\n"
    name = _difficulty(difficulty)
    if name != DEFAULT_DIFFICULTY:
        base += f"difficulty={name}\n"
    songs = {song: hashlib.sha256((base + digest).encode("utf-8")).hexdigest()
             for song, digest in digests.items()}
    everything = "\n".join(f"{song}:{digests[song]}" for song in sorted(digests))
    return songs, hashlib.sha256((base + "general\n" + everything).encode("utf-8")).hexdigest()


def leaf_inputs(basis: str = "maxed", difficulty: str = DEFAULT_DIFFICULTY,
                song: str = "m0334") -> dict:
    """The exact leaf's inputs on `basis` (`reps`, `ctx`, `by_id`, `chart` of
    `song`, `reference_key`, ...) — what the search scores with, and what the
    web's row detail (`tools/web/row_detail.py`) re-develops a row on."""
    difficulty = _difficulty(difficulty)
    if basis in ("account", "account-levelmax"):
        return build_account(_namespace(song, difficulty, basis), bench=BENCH[basis])
    if basis in ("account-max", "account-levelmax-max"):
        return build_account(_namespace(song, difficulty, basis), flat_budget=maxed_budget(),
                          bench=BENCH[basis])
    built = build_maxed(_namespace(song, difficulty), PROXIES[SETTINGS["proxy"]])
    return _with_maxed_bench(built) if basis == "maxed" else built


# ---------------------------------------------------------------------------
# The maxed bench
# ---------------------------------------------------------------------------
# The bench (the holomems off the team) of the `maxed` basis = the web's "Maxed account"
# (every card at max level and bloom, every holomem at max level with her board developed).
# `load_context` alone gives it the account's CAPTURED boards
# (`dumps/board.json`: a few nodes a holomem, Connect cards at level 1), which would max
# only the six team boards. Instead every bench holomem spends her max-level points on
# what of her board reaches a team from the bench (greens, yellows, sockets), with
# level-2 Connect cards from the whole catalogue, placed by the domain's exact assignment
# (`precompute_optimisations.maxed_account_bench`), valued at a FIXED nominal team so the
# bench never depends on the team being scored: the roster the community tool
# holodori.best uses (v3.1.1), the same as `hbweb.basis.MAXED_ACCOUNT_NOMINAL_TEAM`.
MAXED_NOMINAL_TEAM = (
    "card-00022-5-uniq-0063-00",
    ("card-00007-5-uniq-0008-00", "card-00022-5-uniq-0063-00",
     "card-00021-5-uniq-0017-00", "card-00006-5-uniq-0007-00",
     "card-06005-5-uniq-0061-00"),
)


_MAXED_BENCH: dict = {}          # inputs key -> (bench characters, bench as a board state)


def _with_maxed_bench(built: dict) -> dict:
    """`built` with `ctx.bench_characters` = the maxed bench, every holomem at max level
    (built once per inputs: ~seconds, one exact socket assignment over 54 boards)."""
    import precompute_optimisations as pc  # noqa: PLC0415

    ctx = built["ctx"]
    points = maxed_budget()
    key = (built.get("reference_key") or built.get("inputs_hash"), points)
    hit = _MAXED_BENCH.get(key)
    if hit is None:
        state: dict = {}
        bench = pc.maxed_account_bench(list(ctx.cards), dataclasses.replace(ctx.cfg, board=None),
                                       MAXED_NOMINAL_TEAM, points=points, state_out=state)
        _MAXED_BENCH.clear()
        hit = _MAXED_BENCH[key] = (bench, state, {c.id: c for c in ctx.cards})
    return {**built, "ctx": dataclasses.replace(ctx, bench_characters=hit[0])}


def maxed_bench_state(difficulty: str = DEFAULT_DIFFICULTY) -> dict:
    """The `maxed` basis' bench as a board state, `{chr: {"nodes": [her bench board],
    "slots": {socket: card}}}`: every holomem's target on a maxed account, its Connect
    cards where the exact assignment put them. Builds the leaf when not built yet."""
    if not _MAXED_BENCH:
        leaf_inputs("maxed", difficulty)
    return next(iter(_MAXED_BENCH.values()))[1]


def maxed_bench_targets(team: Sequence[str] = (), difficulty: str = DEFAULT_DIFFICULTY) -> dict:
    """What of the maxed boards SCORES for `team` (card ids, leader first), as `{chr:
    {"nodes": [...], "slots": {...}}}` for every holomem: its nodes with an effect the
    scorer counts (not `ignored`: mini-game / work / live rewards, EXP, Hologold, LIFE,
    leader-active additions) that can reach this team --
      * greens that reach one of the team's cards (`board._reaches`: a "to All" one
        always, a "to Advent" one only with an Advent member);
      * yellows (a holomem's per-song bonus counts on every song she sings, on the
        team or not);
      * on a TEAM holomem, the reds that fire only when she sings (the song decides);
    and the sockets holding a Connect card. A team holomem's other reds and blues are
    the row's own plan (the caller's); a bench holomem's reach no team. No unlock paths
    (they depend on the account's boards; the caller adds them). Measured over 9 teams
    x 24 charts: the bench pruned to its scoring greens + yellows + paths scores
    IDENTICALLY to the complete one; without the yellows up to -6.5 %."""
    from hbuilder.domain.board import BoardError, _reaches, parse_effect_id  # noqa: PLC0415

    state = maxed_bench_state(difficulty)
    bench, _state, by_id = next(iter(_MAXED_BENCH.values()))
    cards = [by_id[c] for c in team if c in by_id]
    team_chrs = {"chr-" + c.split("-")[1] for c in team}
    skilltree = json.loads(SKILLTREE_PATH.read_text(encoding="utf-8"))
    name_of = {m["chr"]: m["name"] for m in skilltree["members"]}

    def counts(effect, holomem: str, chr_id: str, branch: str) -> bool:
        if effect.kind == "ignored":
            return False
        if branch == "Y":
            return True
        if branch == "G":
            if not cards:
                return True
            try:
                return any(_reaches(effect, holomem, c) for c in cards)
            except BoardError:                      # a leader-scoped effect: keep it
                return True
        return chr_id in team_chrs and effect.conditional

    out = {}
    for chr_id, entry in state.items():
        holomem = name_of.get(chr_id, "")
        scoring = {e["node"] for e in bench.get(holomem, ())
                   if any(counts(parse_effect_id(x, branch=e["node"][0]), holomem, chr_id, e["node"][0])
                          for x in e["effects"])}
        out[chr_id] = {"nodes": sorted(scoring | set(entry["slots"])), "slots": dict(entry["slots"])}
    return out


# rows of a song answer that carry their slot order and quantiles (the page shows this many)
ORDER_ROWS = 8


# ---------------------------------------------------------------------------
# Rankings over leaf rows
# ---------------------------------------------------------------------------
def _team_key(row: dict) -> tuple:
    return row["leader"], frozenset(row["members"])


def global_ranking(rows: list[dict], pool_ids, reps, name, singing, size: int) -> list[dict]:
    """The song's top `size` teams ACROSS leaders: every row's `ranking` (a
    searched leader's search ranking, a triaged leader's short list) merged and
    sorted by score, so a leader's 2nd and 3rd sets appear whenever they beat
    another leader's best. Plain-leaf scores; `_refine_rows` then refines the
    top within its margin. `status` is the leader's (searched / triage)."""
    merged = []
    for r in rows:
        leader = r["leader"]
        for value, members in (r.get("ranking") or [(r["best"], r["members"])]):
            merged.append((float(value), leader.id, tuple(members), r))
    merged.sort(key=lambda m: (-m[0], m[1], m[2]))
    out, seen = [], set()
    for value, _lid, members, r in merged:
        key = (r["leader"].id, members)
        if key in seen:
            continue
        seen.add(key)
        out.append({"leader": r["leader"].id, "leader_name": name(r["leader"]),
                    "sings": r["leader"] in singing, "status": r["status"], "best": value,
                    "members": [pool_ids[i] for i in members],
                    "member_names": [reps[i].member for i in members]})
        if len(out) >= size:
            break
    return out


def alternatives(row: dict, pool_ids, reps, count: int | None = None) -> list[dict]:
    """The next best member sets under `row`'s leader, from its `ranking` (the
    search's, or the triage short list's): `{members, member_names, best,
    refined: False}`, best first, the row's own set left out. Plain-leaf
    scores — the row's `best` may be the REFINED figure, so compare an
    alternative with the row's `best_plain` when the row is `refined`."""
    count = SETTINGS["alternatives"] if count is None else count
    own = tuple(row["members"])
    out = []
    for value, members in row.get("ranking") or ():
        if tuple(members) == own:
            continue
        out.append({"members": [pool_ids[i] for i in members],
                    "member_names": [reps[i].member for i in members],
                    "best": float(value), "refined": False})
        if len(out) >= count:
            break
    return out


def _slot_orders(rows: list[dict], built: dict, chart, log=None, known: dict | None = None) -> None:
    """`member_order` (the slot order) and the run-score quantiles
    (`row_detail.quantile_fields`) of the first `ORDER_ROWS` rows: each row re-developed
    on the leaf that scored it (`row_detail.leaf_detail`, refined when the row
    was), so the order and the law are the ones behind its `best`. Additive:
    an older file simply lacks the fields; a failure leaves them out and logs.
    `known` (team key -> fields) is shared between calls so a team present in
    both the global ranking and the per-leader rows is developed once."""
    known = {} if known is None else known
    try:
        import row_detail  # noqa: PLC0415
        from hbuilder.domain.team import Team  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"  ! slot orders unavailable: {exc}")
        return
    by_id = built["by_id"]
    for row in rows[:ORDER_ROWS]:
        held = known.get((_team_key(row), bool(row.get("refined"))))
        if held is not None:
            row.update(held)
            continue
        try:
            team = Team(leader=by_id[row["leader"]], members=tuple(by_id[m] for m in row["members"]))
            got = row_detail.leaf_detail(built, team, refine=bool(row.get("refined")),
                                         panel=False, chart=chart, want_distribution=True)
            fields = {"member_order": got["member_order"], **row_detail.quantile_fields(got)}
            known[(_team_key(row), bool(row.get("refined")))] = fields
            row.update(fields)
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"  ! slot order of {row.get('leader')}: {type(exc).__name__}: {exc}")


def _refine_rows(rows: list[dict], chart_indices, pool, by_id, known: dict | None = None) -> int:
    """Re-score the rows within `SETTINGS["refine"]["margin"]` of the best with the
    activation refinement and re-sort them. A refined row keeps its plain figure as
    `best_plain`; its `best` is the refined score (never below the plain one: the
    refinement only keeps moves that raise it). Returns how many rows it touched.
    `known` (team key -> refined value), shared between calls, scores a team
    once when it is both a global-ranking entry and a per-leader row."""
    from hbuilder.domain.team import Team  # noqa: PLC0415

    if not rows:
        return 0
    settings = SETTINGS["refine"]
    top = max(r["best"] for r in rows)
    chosen = [r for r in sorted(rows, key=lambda r: -r["best"])
              if r["best"] >= (1.0 - settings["margin"]) * top][:settings["cap"]]
    known = {} if known is None else known
    todo = [r for r in chosen if _team_key(r) not in known]
    teams = [Team(leader=by_id[r["leader"]], members=tuple(by_id[m] for m in r["members"]))
             for r in todo]
    if teams:
        for row, value in zip(todo, pool.batch_score(teams, chart_indices=chart_indices,
                                                      refine_activation=True)):
            known[_team_key(row)] = float(value)
    for row in chosen:
        value = known[_team_key(row)]
        row["best_plain"] = row["best"]
        row["best"] = max(float(value), row["best"])
        row["refined"] = True
    rows.sort(key=lambda r: -r["best"])
    return len(chosen)
