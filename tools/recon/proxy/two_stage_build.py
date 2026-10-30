"""Build / update the TWO-STAGE answers cache: per song the best (leader, five
members), and the best in general — ready right after a refresh instead of a
multi-hour search.

    dumps/two_stage/song_<music id>.json    `two_stage.search_song` for that song
    dumps/two_stage/general.json            `two_stage.search_general` over every chart,
                                            the board rebuilt per song
    dumps/two_stage/singers.json            per character: the best team with HER as
                                            leader over the songs she sings
    dumps/two_stage/status.json             what the builder is doing / did

EVERY FILE CARRIES THE KEY OF THE INPUTS IT WAS COMPUTED FROM, and a file is
trusted only while its key equals the key of today's inputs. A song's key
digests the scoring inputs (cards, collection, board, skilltree, master nodes,
config, model version — `certificate.reference_key`), THAT song's chart, and
this builder's version and settings; the general key digests every chart. So:

    a new song           -> only that song is computed (~10 s), and `general`
    a new / changed card -> every key changes, everything is recomputed
    nothing changed      -> the builder exits in a second

`refresh_all.py` starts this detached when it finishes, so pressing refresh is
all it takes. One builder at a time (a lock file); a builder that finds the
inputs changed under it starts over.

Two BASES, each with its own files and its own keys:

    maxed     the maxed five-star catalogue at the flat 453 budget.
                                                  song_<id>.json, general.json
    account   the account's cards at their real level / bloom, every rarity, each
              character's board at the points it has earned, the account's Connect
              cards (checked against complete enumerations of 60-card sub-pools).
                                    account_song_<id>.json, account_general.json

The scores are the exact leaf's; they are SEARCHED, not certified.

DIFFICULTY: every answer is on EXPERT unless `--difficulty` says otherwise.
EXPERT uses the unsuffixed file names and keys; another difficulty is scored on charts_all.json, its key gains a `difficulty=` line and
its files a `_<difficulty>` suffix (`song_m0334_hard.json`, `general_hard.json`),
so the two never overwrite or vouch for each other.

Nothing here writes data/ or state/. Run from the repo root:
    set PYTHONIOENCODING=utf-8
    set PYTHONPATH=src;tools/web;tools/recon/proxy
    python tools/recon/proxy/two_stage_build.py [--songs m0334,m0144] [--force] [--status]
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Sequence

REPO = Path(__file__).resolve().parents[3]
for _p in ("src", "tools/web", "tools/recon/proxy", "tools/web/kernel"):
    _sp = str(REPO / _p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

CACHE = REPO / "tools" / "recon" / "proxy" / "dumps" / "two_stage"
LOCK = CACHE / "builder.lock"
STATUS = CACHE / "status.json"
# Bump when the search or the files' shape changes: every key changes with it.
VERSION = "two-stage-build-4"     # 2: the short list re-scored with the activation refinement
                                  # 3: scored on the full Overall Power (Memory Bonus + Member Power-Up)
                                  # 4: song answers carry a GLOBAL `ranking[]` (top teams across
                                  #    leaders), rows carry `alternatives[]`, and the first
                                  #    ORDER_ROWS of both carry the run-score quantiles (p10/p50/p90)
SETTINGS = {"budget": 453, "uptime": "finite", "platform": "mobile", "proxy": "nonsinger",
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
# basis -> the file prefix of its answers. The maxed basis has no prefix, and its
# keys carry no basis line.
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
BASIS_TEXT = {
    "maxed": "maxed five-star catalogue, board budget 453, rebuilt per song",
    "account": "your cards at their real level and bloom, your real board points, "
               "boards rebuilt per song",
    "account-max": "your cards at their real level and bloom, every character at max level "
                   "(361 board points), unlimited cubes/hexas (the other characters' green too), boards rebuilt per song",
    "account-levelmax": "your cards at their MAX level (blooms real), your real board points, "
                        "boards rebuilt per song",
    "account-levelmax-max": "your cards at their MAX level (blooms real), every character at max level "
                            "(361 board points), unlimited cubes/hexas: holodori.best's default assumptions",
}


DEFAULT_DIFFICULTY = "EXPERT"


def _difficulty(difficulty: str | None) -> str:
    """Upper-cased, EXPERT for None / ""; ValueError on an unknown name."""
    import certificate as cert_mod  # noqa: PLC0415

    return cert_mod.normalize_difficulty(difficulty)


def _suffix(difficulty: str | None) -> str:
    name = _difficulty(difficulty)
    return "" if name == DEFAULT_DIFFICULTY else f"_{name.lower()}"


def song_file(song: str, basis: str = "maxed", difficulty: str = DEFAULT_DIFFICULTY) -> str:
    return f"{BASES[basis]}song_{song}{_suffix(difficulty)}.json"


def general_file(basis: str = "maxed", difficulty: str = DEFAULT_DIFFICULTY) -> str:
    return f"{BASES[basis]}general{_suffix(difficulty)}.json"


def singers_file(basis: str = "maxed", difficulty: str = DEFAULT_DIFFICULTY) -> str:
    return f"{BASES[basis]}singers{_suffix(difficulty)}.json"


def charts_path(difficulty: str = DEFAULT_DIFFICULTY) -> Path:
    """charts.json for EXPERT (its digests unchanged), charts_all.json else."""
    import certificate as cert_mod  # noqa: PLC0415

    return cert_mod.charts_path_for(difficulty)


def singers_key(general_key: str) -> str:
    """The per-singer answers depend on everything the general answer does
    (every chart, the scoring inputs) and on their own procedure."""
    return hashlib.sha256(f"singers-1\n{general_key}".encode("utf-8")).hexdigest()


def _namespace(song: str = "m0334", difficulty: str = DEFAULT_DIFFICULTY,
               basis: str = "") -> argparse.Namespace:
    ns = argparse.Namespace(song=song, budget=SETTINGS["budget"], uptime=SETTINGS["uptime"],
                            platform=SETTINGS["platform"], difficulty=_difficulty(difficulty))
    if basis in LEVEL_MAX:
        ns.assume_level = "max"
    return ns


def _account():
    return _load("_two_stage_account", "tools/recon/probes/two_stage_account.py")


def today_keys(basis: str = "maxed",
               difficulty: str = DEFAULT_DIFFICULTY) -> tuple[dict[str, str], str]:
    """`(key per song, general key)` of today's inputs on `basis` at
    `difficulty`, WITHOUT building the leaf — for a reader (the web) telling
    fresh from stale."""
    import certificate as cert_mod  # noqa: PLC0415

    ex = _load("_ex_two_stage_read", "tools/recon/probes/exhaustive_song_leader.py")
    if basis in BENCH:
        reference = _account().reference_key(_namespace(basis=basis), BENCH[basis])
    else:
        # `ctx.cfg`, as `members_first.build` hashes it — not the raw config
        _cards, _cfg, ctx, _chart = ex.load_context(SETTINGS["uptime"], SETTINGS["budget"], "m0334")
        cfg = dataclasses.replace(ctx.cfg, platform=SETTINGS["platform"])
        _hash, per = cert_mod.inputs_hash(cfg, "maxed", cert_mod.MODEL_VERSION)
        reference = cert_mod.reference_key(per, cert_mod.MODEL_VERSION)
    name = _difficulty(difficulty)
    return keys_for(reference, chart_digests(charts_path(name), name), basis, name)


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, payload: dict) -> None:
    """Atomically: a reader never sees half a file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(json.dumps(payload, indent=1, ensure_ascii=False, default=str).encode("utf-8"))
    os.replace(tmp, path)


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


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
    adds a `difficulty=` line, so its answers can never pass for EXPERT ones."""
    base = f"{VERSION}\n{json.dumps(SETTINGS, sort_keys=True)}\n{reference_key}\n"
    if basis != "maxed":
        base += f"basis={basis}\n"
    name = _difficulty(difficulty)
    if name != DEFAULT_DIFFICULTY:
        base += f"difficulty={name}\n"
    songs = {song: hashlib.sha256((base + digest).encode("utf-8")).hexdigest()
             for song, digest in digests.items()}
    everything = "\n".join(f"{song}:{digests[song]}" for song in sorted(digests))
    return songs, hashlib.sha256((base + "general\n" + everything).encode("utf-8")).hexdigest()


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259                              # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_lock() -> bool:
    CACHE.mkdir(parents=True, exist_ok=True)
    held = _read(LOCK)
    if held and _alive(int(held.get("pid", 0))) and int(held.get("pid", 0)) != os.getpid():
        return False
    _write(LOCK, {"pid": os.getpid(), "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
    return True


def read_song(song: str, basis: str = "maxed",
              difficulty: str = DEFAULT_DIFFICULTY) -> dict | None:
    """A song's cached answer with `fresh` = its key is today's key — for a
    reader (the web) that must not show a stale answer as current. `None` when
    there is no file. Computing today's key reads the input files (~0.3 s)."""
    cached = _read(CACHE / song_file(song, basis, difficulty))
    if cached is None:
        return None
    try:
        songs, _general = today_keys(basis, difficulty)
        cached["fresh"] = songs.get(song) == cached.get("key")
    except Exception as exc:  # noqa: BLE001 - an unreadable input is "cannot vouch", not a crash
        cached["fresh"] = False
        cached["fresh_error"] = repr(exc)
    return cached


def build(args) -> int:
    import connect  # noqa: PLC0415
    import notewalk  # noqa: PLC0415
    import two_stage  # noqa: PLC0415

    if not os.environ.get("HBUILDER_NO_KERNEL"):
        notewalk.enable()
        connect.enable()
    mfp = _load("_mfp_two_stage_build", "tools/recon/probes/members_first_2026_08_19.py")
    mf = mfp.mf
    log = mfp.Log(CACHE / "build.log")
    started = time.strftime("%Y-%m-%dT%H:%M:%S")

    def status(**fields) -> None:
        _write(STATUS, {"pid": os.getpid(), "started": started,
                        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": VERSION, **fields})

    bases = [b for b in args.basis.split(",") if b in BASES]
    for basis in bases:
        code = _build_basis(basis, args, mfp, two_stage, log, status)
        if code:
            return code
    status(state="done", bases=bases)
    log("done")
    return 0


def leaf_inputs(basis: str = "maxed", difficulty: str = DEFAULT_DIFFICULTY,
                song: str = "m0334", *, mfp=None) -> dict:
    """The exact leaf's inputs on `basis` (`reps`, `ctx`, `by_id`, `chart` of
    `song`, `reference_key`, ...) — what the builder scores with, and what the
    web's row detail (`tools/web/row_detail.py`) re-develops a cached row on."""
    difficulty = _difficulty(difficulty)
    if basis in ("account", "account-levelmax"):
        return _account().build(_namespace(song, difficulty, basis), bench=BENCH[basis])
    if basis in ("account-max", "account-levelmax-max"):
        acct = _account()
        return acct.build(_namespace(song, difficulty, basis), flat_budget=acct.MAX_LEVEL_POINTS,
                          bench=BENCH[basis])
    if mfp is None:
        mfp = _load("_mfp_two_stage_leaf", "tools/recon/probes/members_first_2026_08_19.py")
    built = mfp.build(_namespace(song, difficulty), mfp.PROXIES[SETTINGS["proxy"]])
    return _with_maxed_bench(built) if basis == "maxed" else built


# The bench (the holomems off the team) of the `maxed` basis = the web's "Maxed account"
# (every card at max level and bloom, every holomem at max level with a COMPLETE board).
# `exhaustive_song_leader.load_context` alone gives it the account's CAPTURED boards
# (`dumps/board.json`: a few nodes a holomem, Connect cards at level 1), which would max
# only the six team boards. Instead every bench board is the full 453-point tree with
# level-2 Connect cards from the whole catalogue, placed by the domain's exact assignment
# (`precompute_optimisations.maxed_account_bench`), valued at a FIXED nominal team so the
# bench never depends on the team being scored: the roster the community tool
# holodori.best uses (v3.1.1), the same as `server.MAXED_ACCOUNT_NOMINAL_TEAM`.
MAXED_NOMINAL_TEAM = (
    "card-00022-5-uniq-0063-00",
    ("card-00007-5-uniq-0008-00", "card-00022-5-uniq-0063-00",
     "card-00021-5-uniq-0017-00", "card-00006-5-uniq-0007-00",
     "card-06005-5-uniq-0061-00"),
)
_MAXED_BENCH: dict = {}          # inputs key -> (bench characters, bench as a board state)


def _with_maxed_bench(built: dict) -> dict:
    """`built` with `ctx.bench_characters` = the full maxed bench (built once per
    inputs: ~seconds, one exact socket assignment over 54 boards)."""
    import precompute_optimisations as pc  # noqa: PLC0415

    ctx = built["ctx"]
    key = built.get("reference_key") or built.get("inputs_hash")
    hit = _MAXED_BENCH.get(key)
    if hit is None:
        state: dict = {}
        bench = pc.maxed_account_bench(list(ctx.cards), dataclasses.replace(ctx.cfg, board=None),
                                       MAXED_NOMINAL_TEAM, state_out=state)
        _MAXED_BENCH.clear()
        hit = _MAXED_BENCH[key] = (bench, state, {c.id: c for c in ctx.cards})
    return {**built, "ctx": dataclasses.replace(ctx, bench_characters=hit[0])}


def maxed_bench_state(difficulty: str = DEFAULT_DIFFICULTY) -> dict:
    """The `maxed` basis' bench as a board state, `{chr: {"nodes": [whole tree],
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
    skilltree = json.loads((REPO / "tools/recon/proxy/dumps/skilltree.json").read_text(encoding="utf-8"))
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


def _build_basis(basis, args, mfp, two_stage, log, status) -> int:
    mf = mfp.mf
    difficulty = _difficulty(getattr(args, "difficulty", None))

    def sfile(song):
        return song_file(song, basis, difficulty)

    def inputs():
        return leaf_inputs(basis, difficulty, mfp=mfp)

    for attempt in range(1, 4):
        built = inputs()
        ex = built["ex"]
        path = charts_path(difficulty)
        digests = chart_digests(path, difficulty)
        song_keys, general_key = keys_for(built["reference_key"], digests, basis, difficulty)
        library = ex.load_charts(str(path))
        charts = library.at(difficulty)
        index = {chart.music_id: k for k, chart in enumerate(charts)}
        wanted = [s for s in (args.songs.split(",") if args.songs else sorted(index)) if s in index]
        stale = [s for s in wanted
                 if args.force or (_read(CACHE / sfile(s)) or {}).get("key") != song_keys[s]]
        general_stale = not args.songs and (
            args.force or (_read(CACHE / general_file(basis, difficulty)) or {}).get("key") != general_key)
        log(f"[{basis}] attempt {attempt}: {len(charts)} {difficulty} charts, pool {len(built['reps'])}; "
            f"{len(stale)} of {len(wanted)} songs to compute; general "
            f"{'to compute' if general_stale else 'up to date or not asked'}; "
            f"reference_key {built['reference_key'][:16]}")
        singers_stale = not args.songs and (
            args.force or (_read(CACHE / singers_file(basis, difficulty)) or {}).get("key")
            != singers_key(general_key))
        if not stale and not general_stale and not singers_stale:
            return 0
        reps, proxy, pool_ids, hol = built["reps"], built["proxy"], built["pool_ids"], built["hol"]
        ctx = dataclasses.replace(built["ctx"], charts=tuple(charts), board_per_chart=True)
        leaders = list(reps)
        workers = args.workers or mfp.default_workers()

        def name(card) -> str:
            return f"{card.member} / {card.costume_name}"

        with mfp.pr.RefinePool(ctx, workers=workers,
                               on_fallback=lambda line: log(f"  ! {line}")) as pool:
            def scorer(chart_indices):
                def score(leader, sets):
                    return mf.score_sets(
                        leader, reps, sets,
                        lambda teams: pool.batch_score(teams, chart_indices=chart_indices))
                return score

            scorer((0,))(proxy, two_stage.random_legal_sets(hol, 4 * workers, random.Random(0)))
            t_all = time.perf_counter()
            for number, song in enumerate(stale, 1):
                chart = charts[index[song]]
                singers = tuple(chart.singers or ())
                singing = [c for c in leaders if "chr-" + c.id.split("-")[1] in singers]
                t0 = time.perf_counter()
                found = two_stage.search_song(
                    reps, leaders, scorer((index[song],)), base_leader=proxy, always=singing,
                    gate_of=mf.gate_of, margin=SETTINGS["margin"],
                    max_searched=SETTINGS["max_searched"], triage_top=SETTINGS["triage_top"],
                    tier=SETTINGS["tier"], seed=SETTINGS["seed"])
                song_rows = [{"leader": r["leader"].id, "leader_name": name(r["leader"]),
                              "sings": r["leader"] in singing, "status": r["status"],
                              "best": r["best"], "members": [pool_ids[i] for i in r["members"]],
                              "member_names": [reps[i].member for i in r["members"]],
                              "alternatives": alternatives(r, pool_ids, reps)}
                             for r in found["rows"]]
                ranking = global_ranking(found["rows"], pool_ids, reps, name, singing,
                                         SETTINGS["ranking"]["size"])
                known: dict = {}
                refined_ranking = _refine_rows(ranking, (index[song],), pool, built["by_id"],
                                               known=known)
                refined = _refine_rows(song_rows, (index[song],), pool, built["by_id"], known=known)
                figures_known: dict = {}
                _slot_orders(ranking, built, chart, log, known=figures_known)
                _slot_orders(song_rows, built, chart, log, known=figures_known)
                seconds = time.perf_counter() - t0
                _write(CACHE / sfile(song), {
                    "key": song_keys[song], "song": song, "singers": list(singers),
                    "singer_type": chart.singer_type, "basis": basis, "difficulty": difficulty,
                    "method": "two-stage search; the short list re-scored with the activation refinement",
                    "certified": False, "version": VERSION, "settings": SETTINGS,
                    "reference_key": built["reference_key"], "chart_digest": digests[song],
                    "pool": len(reps), "seconds": seconds,
                    "stats": {**found["stats"], "refined": refined, "ranking_refined": refined_ranking},
                    "written": time.strftime("%Y-%m-%dT%H:%M:%S"), "rows": song_rows,
                    "ranking": ranking})
                best = {"leader": built["by_id"][song_rows[0]["leader"]], "best": song_rows[0]["best"]}
                elapsed = time.perf_counter() - t_all
                log(f"[{basis} {number}/{len(stale)}] {song}: {name(best['leader'])} {best['best']:,.0f} "
                    f"({found['stats']['searched']} leaders searched, {seconds:.1f}s; "
                    f"eta {elapsed / number * (len(stale) - number) / 60:.1f} min)")
                status(state="running", basis=basis, stage="songs", done=number, total=len(stale),
                       song=song, difficulty=difficulty, reference_key=built["reference_key"])
            if general_stale:
                status(state="running", basis=basis, stage="general",
                       reference_key=built["reference_key"])
                g = SETTINGS["general"]
                sample = two_stage.chart_sample(charts, g["sample"])
                # every song's own winner is a candidate for the general answer too
                extra = []
                by_id = {c.id: c for c in leaders}
                position = {card_id: k for k, card_id in enumerate(pool_ids)}
                for song in sorted(index):
                    cached = _read(CACHE / sfile(song))
                    if cached and cached.get("key") == song_keys[song] and cached["rows"]:
                        row = cached["rows"][0]
                        extra.append((by_id[row["leader"]],
                                      tuple(sorted(position[m] for m in row["members"]))))
                t0 = time.perf_counter()
                found = two_stage.search_general(
                    reps, leaders, scorer(tuple(index[c.music_id] for c in sample)), scorer(None),
                    base_leader=proxy, gate_of=mf.gate_of, keep=g["keep"], extra=extra,
                    max_searched=SETTINGS["max_searched"], triage_top=g["triage_top"],
                    tier=g["tier"], seed=SETTINGS["seed"])
                general_rows = [{"leader": r["leader"].id, "leader_name": name(r["leader"]),
                                 "status": r["status"], "best": r["best"], "candidates": r["candidates"],
                                 "members": [pool_ids[i] for i in r["members"]],
                                 "member_names": [reps[i].member for i in r["members"]]}
                                for r in found["rows"]]
                refined = _refine_rows(general_rows, None, pool, built["by_id"])
                seconds = time.perf_counter() - t0
                _write(CACHE / general_file(basis, difficulty), {
                    "key": general_key, "basis": basis, "difficulty": difficulty,
                    "method": "two-stage search on a chart sample, "
                    "every candidate re-scored on all charts", "certified": False,
                    "objective": f"mean score over every {difficulty} chart, the board rebuilt per song",
                    "charts": len(charts), "sample": [c.music_id for c in sample], "version": VERSION,
                    "settings": SETTINGS, "reference_key": built["reference_key"], "pool": len(reps),
                    "seconds": seconds, "stats": {**found["stats"], "refined": refined},
                    "written": time.strftime("%Y-%m-%dT%H:%M:%S"), "rows": general_rows})
                best = {"leader": built["by_id"][general_rows[0]["leader"]], "best": general_rows[0]["best"]}
                log(f"[{basis}] general: {name(best['leader'])} {best['best']:,.0f} mean over {len(charts)} charts "
                    f"({seconds:.0f}s, {found['stats']['full_evals']:,} candidates re-scored)")
            if singers_stale:
                status(state="running", basis=basis, stage="singers",
                       reference_key=built["reference_key"])
                t0 = time.perf_counter()
                rows = _singers(charts, index, leaders, reps, pool_ids, scorer, two_stage,
                                lambda song: _read(CACHE / sfile(song)), song_keys, log, basis)
                from hbuilder.domain.team import Team  # noqa: PLC0415
                by_id = built["by_id"]
                for row in rows:
                    team = Team(leader=by_id[row["leader"]], members=tuple(by_id[m] for m in row["members"]))
                    value = pool.batch_score([team], chart_indices=tuple(index[s] for s in row["songs"]),
                                             refine_activation=True)[0]
                    row["best_plain"], row["best"], row["refined"] = row["best"], max(float(value), row["best"]), True
                rows.sort(key=lambda r: -r["best"])
                _write(CACHE / singers_file(basis, difficulty), {
                    "key": singers_key(general_key), "basis": basis, "difficulty": difficulty,
                    "certified": False,
                    "method": "per character: one two-stage search per card of hers as leader, "
                              "scored on the mean over the songs she sings, the board rebuilt per song",
                    "version": VERSION, "settings": SETTINGS, "reference_key": built["reference_key"],
                    "pool": len(reps), "seconds": time.perf_counter() - t0,
                    "written": time.strftime("%Y-%m-%dT%H:%M:%S"), "rows": rows})
                log(f"[{basis}] singers: {len(rows)} characters ({time.perf_counter() - t0:.0f}s)")
        # did the inputs move while this ran (a refresh landed)? then go again
        if today_keys(basis, difficulty) == (song_keys, general_key):
            return 0
        log(f"[{basis}] the inputs changed while building; starting over")
    status(state="failed", basis=basis, error="the inputs kept changing")
    return 1


# rows of a song answer that carry their slot order and quantiles (the page shows this many)
ORDER_ROWS = 8


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


def _singers(charts, index, leaders, reps, pool_ids, scorer, two_stage, read_song_file,
             song_keys, log, basis, only=None) -> list[dict]:
    """For every character, the best team
    with one of HER cards as leader over the songs SHE sings — the mean of the
    team's score on each of those charts, the board rebuilt per song. One search
    per leader card of hers, seeded with what each of her songs' own answers
    found under that leader. Every score is the exact leaf's mean; the per-song
    figures of the winning team are scored one chart at a time and their mean
    is the reported number (checked here with ==)."""
    position = {card_id: k for k, card_id in enumerate(pool_ids)}
    sung: dict[str, list[str]] = {}
    for chart in charts:
        for singer in chart.singers or ():
            sung.setdefault(singer, []).append(chart.music_id)
    by_chr: dict[str, list] = {}
    for card in leaders:
        by_chr.setdefault("chr-" + card.id.split("-")[1], []).append(card)
    rows = []
    for number, chr_id in enumerate(sorted(sung), 1):
        cards = by_chr.get(chr_id)
        if not cards or (only is not None and chr_id not in only):
            continue                      # she sings, but no card of hers is in this pool
        songs = sorted(sung[chr_id])
        chosen = tuple(index[s] for s in songs)
        score = scorer(chosen)
        best = None
        for leader in cards:
            seeds = []
            for song in songs:
                cached = read_song_file(song)
                if cached and cached.get("key") == song_keys.get(song):
                    seeds += [tuple(sorted(position[m] for m in r["members"]))
                              for r in cached["rows"] if r["leader"] == leader.id
                              and all(m in position for m in r["members"])]
            found = two_stage.search(reps, lambda sets, leader=leader: score(leader, sets),
                                     seed=SETTINGS["seed"], seeds=list(dict.fromkeys(seeds)))
            value, members = found["ranking"][0]
            if best is None or value > best[0]:
                best = (value, members, leader, found["stats"])
        value, members, leader, stats = best
        per_song = [float(scorer((index[s],))(leader, [members])[0]) for s in songs]
        mean = sum(per_song) / len(per_song)
        if mean != value:
            raise RuntimeError(f"{chr_id}: the per-song mean {mean!r} is not the searched score {value!r}")
        rows.append({"chr": chr_id, "name": leader.member, "songs": songs,
                     "leader": leader.id, "leader_name": f"{leader.member} / {leader.costume_name}",
                     "leader_cards": len(cards), "best": value,
                     "members": [pool_ids[i] for i in members],
                     "member_names": [reps[i].member for i in members],
                     "per_song": dict(zip(songs, per_song)),
                     "exact_evals": stats["exact_evals"], "legal_sets": stats["legal_sets"]})
        log(f"[{basis} singer {number}/{len(sung)}] {leader.member}: {leader.costume_name} "
            f"{value:,.0f} mean over {len(songs)} songs")
    rows.sort(key=lambda r: -r["best"])
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--songs", default="", help="comma-separated music ids (default: all; skips `general`)")
    ap.add_argument("--force", action="store_true", help="recompute even when the key matches")
    ap.add_argument("--basis", default="maxed,account,account-max",
                    help="comma-separated bases to build, in order (maxed, account, account-max)")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--difficulty", default=DEFAULT_DIFFICULTY,
                    help="chart difficulty: EASY, NORMAL, HARD or EXPERT (default EXPERT; "
                         "the others read charts_all.json and write *_<difficulty>.json)")
    ap.add_argument("--status", action="store_true", help="print the status file and exit")
    args = ap.parse_args(argv)
    try:
        args.difficulty = _difficulty(args.difficulty)
    except ValueError as exc:
        ap.error(str(exc))
    if args.status:
        print(json.dumps(_read(STATUS) or {"state": "never run"}, indent=1))
        return 0
    if not acquire_lock():
        print(f"another builder is running (pid {(_read(LOCK) or {}).get('pid')}); nothing started")
        return 0
    try:
        return build(args)
    except BaseException as exc:
        _write(STATUS, {"pid": os.getpid(), "state": "failed", "error": repr(exc),
                        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": VERSION})
        raise
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
