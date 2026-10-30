"""One command to bring every derived artefact up to date -- and say what moved.

    python tools/recon/proxy/refresh_all.py            # the whole chain
    python tools/recon/proxy/refresh_all.py --offline  # local rebuilds only
    python tools/recon/proxy/refresh_all.py --dry-run  # plan + current state, run nothing

The chain, in dependency order:

    1. master        hololive-dreams-master's own dump (scripts\\run-local.ps1)
    2. master_index  dumps/master_index.json from a live Master/Get
    3. account       Auth/Login -> User/Get -> card_params + board_state
    4. cards         dumps/cards_enriched.json from the masters + card_params
    5. import        dumps/cards_import_draft.json, the schema-valid proposal
    6. add_cards     APPEND-ONLY additions to data/cards.json
    7. catalog       dumps/octocacheevai_prod.dec from the client's own install
    8. charts        download the .sus charts the catalogue names and disk lacks
    9. songs         dumps/songs.json from the masters + the charts on disk
   10. song_meta     Music (casts) + LangMusic_Eng (titles) + runtime tables, from the master
   11. chart_lib     lattice/music_coef caches if stale -> dumps/charts.json --verify
   12. jackets       song jackets the catalogue names and disk lacks + render
   13. board         dumps/board.json from board_state
   (6b. card_art     portrait + square WEBP for cards the art dir lacks, after add_cards)
   (6c. event_art    open events' title/kind/times + banner -> dumps/event_meta.json, event_art/)
   (6d. event_cutoffs tier borders, all regions + the account's rank -> dumps/cutoffs/; soft)
   (6e. event_history every event seen + the account's result -> dumps/event_history.json; soft)
   (7b. item_icons   item/resource thumbs from the LOCAL Octo cache -> dumps/item_icons/; soft)
   (12b. member_icons, skill_icons  holomem faces + board node glyphs, LOCAL Octo cache; soft)
   (14. park_export  Parkview maps + spot positions -> dumps/park_export/; soft)

Why a Python script and not a .ps1 beside run_tap.ps1: the whole point of this
tool is the *diff*. It has to load six JSON artefacts, take set differences over
card ids and table names, and count nodes and slots. That is a page of Python
and a small unmaintainable horror in PowerShell. The one PowerShell thing in the
chain -- run-local.ps1 -- is invoked as a subprocess.

What this tool may and may not write
------------------------------------
The two authoritative files answer two different questions, and they have two
different rules:

`data/owned.json` is the USER'S COLLECTION. This tool MAY
APPEND to it: a card the account owns and the file lacks is added, with the ids
taken from `card_params.json`, which is the account's own `User/Get` read back
from the server -- so nothing is guessed. It may never modify or remove an
existing entry, `owned_guard` verifies that after the run, and the writer refuses
to touch the file at all unless re-serialising the unchanged data reproduces it
byte for byte.

`data/cards.json` is the GAME'S CATALOGUE, which is not the user's data. The
`add_cards` step MAY ADD a card the game ships and the file lacks; it may never
modify or remove an existing entry, and a card it cannot translate cleanly is
reported by id with its reason rather than guessed. The guard here is stronger
than a digest because "append-only" is not a statement a digest can make: every
record present before the run is compared for deep equality afterwards, and
only whole new ids may appear.

A blanket "never write either" would let new cards sit unnoticed -- the tool
would answer "your best team" without them and the user would never learn they
existed. `--dry-run-adds` writes nothing, for a run where you want to look first.

Reporting rule
--------------
"Step 4 OK" proves nothing. Every step is judged by comparing the artefact on
disk before and after, and the report names what changed: table counts, card
counts, node counts, slots, and the specific ids that appeared or vanished.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import atomic_io
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
DUMPS = HERE / "dumps"
REPO = HERE.parents[2]
STATE = REPO / "state"
DATA = REPO / "data"
# the master-data dump repo: HB_MASTER_REPO, else the sibling folder of this repo
MASTER_REPO = Path(os.environ.get("HB_MASTER_REPO") or REPO.parent / "hololive-dreams-master")

# The user's own account.
#
# `data/owned.json` may be APPENDED TO by this tool when the account has a card
# the file lacks: the refresh is what should notice a new card and add it.
#
# A pipeline must not be able to replace hand-entered truth with a guess. So
# this is append-only, on the
# same contract as `data/cards.json` -- a whole new id may appear, an existing
# entry may never be modified or removed -- and it is enforced after the run by
# `owned_guard`, not merely intended.
SACRED: tuple[Path, ...] = ()

# Append-only, and verified as such after the run. `cards.json` is the game's
# catalogue and `owned.json` is the user's collection; they are different things
# with the same write contract.
APPEND_ONLY_OWNED = DATA / "owned.json"

# The game's CATALOGUE, which is a different thing. The `add_cards` step may ADD a card the game ships and
# this file lacks; it may never modify or remove an existing entry. A digest
# check cannot express that, so this file gets a stronger guard instead: every
# record present before the run is compared for deep equality after it, and only
# whole new ids may appear. See `catalogue_guard`.
APPEND_ONLY = DATA / "cards.json"

# The four `m9999` tutorial difficulties carry two SPECIAL cues instead of five,
# so `active_lattice.build_chart_cache` skips them by design. The parsed cache is
# therefore expected to sit exactly this far below the .sus file count.
TUTORIAL_CHART_COUNT = 4


# --------------------------------------------------------------------------
# probes: read an artefact off disk and reduce it to facts worth diffing
# --------------------------------------------------------------------------


@dataclass
class Snap:
    """What one artefact looked like at one moment."""

    label: str
    path: Path
    exists: bool
    facts: dict[str, Any] = field(default_factory=dict)
    ids: dict[str, set[str]] = field(default_factory=dict)
    error: str = ""


def _load(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _probe(label: str, path: Path, extract: Callable[[Any], tuple[dict, dict]]) -> Snap:
    if not path.exists():
        return Snap(label, path, exists=False)
    try:
        facts, ids = extract(_load(path))
    except Exception as exc:  # a half-written or hand-broken artefact is a finding
        return Snap(label, path, exists=True, error=f"{type(exc).__name__}: {exc}")
    facts["mtime"] = _dt.datetime.fromtimestamp(path.stat().st_mtime).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return Snap(label, path, exists=True, facts=facts, ids=ids)


def probe_master_tables() -> Snap:
    def extract(doc: Any) -> tuple[dict, dict]:
        tables = doc.get("tables") or {}
        facts = {"version": str(doc.get("version", ""))[:12], "tables": len(tables)}
        on_disk = MASTER_REPO / "data" / "tables"
        if on_disk.is_dir():
            facts["db_files"] = sum(1 for _ in on_disk.iterdir() if _.is_file())
        dumped = MASTER_REPO / "data" / "dumped.json"
        if dumped.exists():
            try:
                d = _load(dumped)
                rows = d.values() if isinstance(d, dict) else d
                bad = [r for r in rows if isinstance(r, dict) and r.get("error")]
                if bad:
                    facts["failed_tables"] = len(bad)
            except Exception:
                pass
        return facts, {"table": set(tables)}

    return _probe("master data", MASTER_REPO / "data" / "index.json", extract)


def probe_master_index() -> Snap:
    def extract(doc: Any) -> tuple[dict, dict]:
        return {"tables": len(doc)}, {"table": set(doc)}

    return _probe("master_index.json", DUMPS / "master_index.json", extract)


def probe_card_params() -> Snap:
    def extract(doc: Any) -> tuple[dict, dict]:
        ids = {str(r.get("id")) for r in doc if isinstance(r, dict)}
        return {"rows": len(doc)}, {"card": ids}

    return _probe("card_params.json", DUMPS / "card_params.json", extract)


def probe_songs() -> Snap:
    def extract(doc: Any) -> tuple[dict, dict]:
        songs = doc["songs"] if isinstance(doc, dict) else doc
        ids = {str(s.get("id")) for s in songs}
        complete = sum(
            1 for s in songs if all(d.get("chart_file") for d in s.get("difficulties", []))
        )
        return {"songs": len(songs), "all charts on disk": complete}, {"card": ids}

    return _probe("songs.json", DUMPS / "songs.json", extract)


def probe_chart_library() -> Snap:
    """`charts.json` -- the EXPERT chart library `--song`/`--across-songs` score on.

    Reported as its own artefact rather than folded into the songs probe because
    it is TWO rebuilds downstream of a chart landing on disk (the parsed-note
    cache, then this), and each of those is a place the chain can silently stop.
    """

    def extract(doc: Any) -> tuple[dict, dict]:
        # `charts` is keyed `"<music id>/<DIFFICULTY>"`, not a list of records.
        charts = doc["charts"] if isinstance(doc, dict) else doc
        names = set(charts) if isinstance(charts, dict) else {str(c) for c in charts}
        return {"charts": len(names)}, {"card": names}

    return _probe("charts.json", DUMPS / "charts.json", extract)


def probe_song_titles() -> Snap:
    """LangMusic_Eng (master_json.py) -- the song picker's titles (display only)."""

    def extract(doc: Any) -> tuple[dict, dict]:
        rows = doc if isinstance(doc, list) else []
        titles = {str(r.get("id"))[len("la-music_title-"):] for r in rows
                  if str(r.get("id", "")).startswith("la-music_title-")}
        return {"titles": len(titles)}, {"card": titles}

    return _probe("main_LangMusic_Eng.json", DUMPS / "holodori" / "main_LangMusic_Eng.json",
                  extract)


def probe_song_casts() -> Snap:
    """Music (master_json.py) -- the casts build_charts.py stamps on every chart."""

    def extract(doc: Any) -> tuple[dict, dict]:
        rows = doc if isinstance(doc, list) else []
        ids = {str(r.get("id")) for r in rows}
        return {"music rows": len(ids)}, {"card": ids}

    return _probe("main_Music.json", DUMPS / "holodori" / "main_Music.json", extract)


def _probe_dir(label: str, path: Path, suffix: str) -> Snap:
    if not path.is_dir():
        return Snap(label, path, False)
    names = {n[:-len(suffix)] for n in os.listdir(path) if n.endswith(suffix)}
    return Snap(label, path, True, {"files": len(names)}, {"card": names})


def probe_jackets() -> Snap:
    return _probe_dir("song_jackets/", DUMPS / "song_jackets", ".webp")


def probe_card_art() -> Snap:
    return _probe_dir("cards_explorer_art/", REPO / "tools" / "explorer" / "cards_explorer_art",
                      ".sq.webp")


def chart_cache_state() -> list[str]:
    """Whether the two parsed-chart caches have caught up with the .sus files.

    `active_lattice.build_chart_cache` rebuilds ONLY when its cache file is
    absent, so downloading twenty new charts leaves the solver quietly scoring
    the old set — the artefact reports success and the number it produces is
    computed over content that no longer matches the disk. That is exactly the
    "verify artefacts, not counters" failure, so it is named here rather than
    left to be noticed.
    """
    sus_dir = DUMPS / "charts_cdn"
    if not sus_dir.is_dir():
        return ["  charts_cdn/ missing -- no charts downloaded yet."]
    on_disk = sum(1 for p in sus_dir.iterdir() if p.suffix == ".sus")
    lines = [f"  charts_cdn/ holds {on_disk} .sus files"]
    cache = DUMPS / "lattice_charts.json"
    if not cache.exists():
        lines.append("  lattice_charts.json MISSING -- run active_lattice.py --charts 1")
        return lines
    try:
        parsed = len(json.loads(cache.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        return lines + [f"  lattice_charts.json unreadable: {exc}"]
    # The four `m9999` tutorial difficulties carry two SPECIAL cues instead of
    # five and are skipped by design, so the cache is expected to sit exactly
    # four below the file count -- not equal to it.
    expected = on_disk - TUTORIAL_CHART_COUNT
    if parsed < expected:
        lines.append(
            f"  ⚠ lattice_charts.json is STALE: {parsed} parsed against {expected} expected "
            f"({on_disk} .sus minus the {TUTORIAL_CHART_COUNT} tutorial charts). The solver is "
            "scoring the OLD chart set. Rebuild: delete it, run `active_lattice.py --charts 1`, "
            "then `build_charts.py --verify`."
        )
    else:
        lines.append(f"  lattice_charts.json parsed {parsed} charts -- current.")
    return lines


def probe_cards_enriched() -> Snap:
    def extract(doc: Any) -> tuple[dict, dict]:
        facts = {
            "cards": len(doc),
            "with_grpc_stats": sum(1 for c in doc if c.get("stats")),
        }
        return facts, {"card": {str(c.get("id")) for c in doc}}

    return _probe("cards_enriched.json", DUMPS / "cards_enriched.json", extract)


def probe_board_state() -> Snap:
    def extract(doc: Any) -> tuple[dict, dict]:
        slots: set[str] = set()
        nodes = 0
        for chr_id, entry in doc.items():
            nodes += len(entry.get("nodes") or ())
            for slot in (entry.get("slots") or {}):
                slots.add(f"{chr_id}:{slot}")
        facts = {"characters": len(doc), "filled_slots": len(slots), "nodes": nodes}
        return facts, {"slot": slots, "character": set(doc)}

    return _probe("board_state.json", DUMPS / "board_state.json", extract)


def probe_inventory() -> Snap:
    """dumps/inventory.json -- the ITEM balances.

    It holds how many Red Cubes (and other materials) the account has, which is
    half of what a board node costs. The balance is in `User/Get`, readable only
    once the reader inflates the body."""

    def extract(doc: Any) -> tuple[dict, dict]:
        tree = doc.get("skill_tree") or {}
        items = doc.get("items") or {}
        facts = {"items": len(items), "board_currencies": len(tree)}
        facts.update({k: v for k, v in sorted(tree.items())})
        return facts, {"item": set(items)}

    return _probe("inventory.json", DUMPS / "inventory.json", extract)


def probe_board() -> Snap:
    def extract(doc: Any) -> tuple[dict, dict]:
        chars = doc.get("characters") or {}
        facts = {
            "characters": len(chars),
            "node_entries": sum(len(v) for v in chars.values()),
            "connect_level": doc.get("connect_level"),
            "basis": doc.get("basis"),
        }
        return facts, {"character": set(chars)}

    return _probe("board.json", DUMPS / "board.json", extract)


def probe_board_export() -> Snap:
    """`dumps/board_export.json` — the web frontend's view of the board.

    ⚠ It must be part of this pipeline: if the file on disk falls behind
    `board.json`, `tools/web/server.py` diffs its buy/sell proposal against a
    stale "current board" and budgets plans against stale `points.granted`, and
    nothing says so unless something is watching it."""

    def extract(doc: Any) -> tuple[dict, dict]:
        chars = doc.get("characters") or []
        nodes = [n for c in chars for n in (c.get("nodes") or [])]
        facts = {
            "characters": len(chars),
            "nodes": len(nodes),
            "owned_nodes": sum(1 for n in nodes if n.get("state") == "owned"),
            "buyable_nodes": sum(1 for n in nodes if n.get("state") == "buyable"),
            "scenario": doc.get("scenario"),
        }
        return facts, {"character": {str(c.get("chr")) for c in chars}}

    return _probe("board_export.json", DUMPS / "board_export.json", extract)


def probe_collection() -> Snap:
    def extract(doc: Any) -> tuple[dict, dict]:
        cards = (doc.get("cards") if isinstance(doc, dict) else doc) or {}
        # collection.json keys its `cards` map BY card id; be tolerant of a list too.
        if isinstance(cards, dict):
            ids = {str(k) for k in cards}
        else:
            ids = {str(c.get("id")) for c in cards if isinstance(c, dict)}
        return {"cards": len(ids)}, {"card": ids}

    return _probe("state/collection.json", STATE / "collection.json", extract)


def derived_artefact_drift() -> list[tuple[str, str, float]]:
    """`(derived, source, hours_behind)` for every derived file OLDER than the
    file it is built from.

    ⚠ This exists because a derived artefact going stale is SILENT: if
    `account_snapshot.py` refreshes `dumps/board_state.json` and nobody runs
    build_board.py, `dumps/board.json` keeps describing the previous board, and
    the solver can score a card WITHOUT her board's cooldown shorten with
    nothing saying a word.

    The full chain rebuilds these in order, so this is a guard on the paths that
    are NOT the full chain — a direct `account_snapshot.py`, an interrupted run.
    """
    pairs = [
        (DUMPS / "board.json", DUMPS / "board_state.json"),
        (DUMPS / "cards_enriched.json", DUMPS / "card_params.json"),
        (DUMPS / "charts.json", DUMPS / "lattice_charts.json"),
        (DUMPS / "charts_all.json", DUMPS / "lattice_charts.json"),
    ]
    out = []
    for derived, source in pairs:
        if not (derived.exists() and source.exists()):
            continue
        behind = source.stat().st_mtime - derived.stat().st_mtime
        if behind > 60:                      # a minute of slack for one chain run
            out.append((derived.name, source.name, behind / 3600.0))
    return out


def collection_level_drift(
    collection_path: Path, snapshot_path: Path
) -> list[tuple[str, str, object, object]]:
    """`(card_id, field, in_collection, on_account)` for every card the two
    disagree about. Empty when they agree, or when either file is absent.

    ⚠ **The id-set comparison below is not enough.** It can report
    "state/collection.json matches the live account" while a card sits at level
    1 in the collection and level 70 on the account: she is in BOTH sets, so
    nothing is missing, and the solver goes on scoring her at a fraction of her
    real stats — a card that cannot be recommended, with no line anywhere
    saying why.

    No level curve and no derivation: `account_snapshot_*.json` carries `level`,
    `bloom` and `limit_break` verbatim, so this compares the two numbers that
    are supposed to be the same number. A field the snapshot OMITS is not drift
    — absent is not evidence of zero, and treating it as drift would flag most
    of the collection and train the operator to ignore the warning. Cards only
    one side knows are the id probe's business, not this one's."""
    try:
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    mine = collection.get("cards") if isinstance(collection, dict) else None
    if not isinstance(mine, dict):
        return []

    drift: list[tuple[str, str, object, object]] = []
    for entry in (snapshot.get("cards") or []) if isinstance(snapshot, dict) else []:
        if not isinstance(entry, dict):
            continue
        card_id = entry.get("card_id")
        held = mine.get(card_id)
        if not isinstance(held, dict):
            continue
        for field in ("level", "bloom", "limit_break"):
            if field not in entry or field not in held:
                continue
            if entry[field] != held[field]:
                drift.append((str(card_id), field, held[field], entry[field]))
    return sorted(drift)


def probe_snapshot(account: str) -> Callable[[], Snap]:
    path = DUMPS / f"account_snapshot_{account}.json"

    def go() -> Snap:
        def extract(doc: Any) -> tuple[dict, dict]:
            facts: dict[str, Any] = {"bytes": path.stat().st_size}
            user = doc.get("user") if isinstance(doc, dict) else None
            if isinstance(user, dict):
                for key in ("name", "level", "rank"):
                    if key in user:
                        facts[key] = user[key]
            return facts, {}

        return _probe(f"account_snapshot_{account}.json", path, extract)

    return go


def probe_cards_json() -> Snap:
    def extract(doc: Any) -> tuple[dict, dict]:
        cards = doc.get("cards") if isinstance(doc, dict) else doc
        return {"cards": len(cards)}, {"card": {str(c.get("id")) for c in cards}}

    return _probe("data/cards.json", DATA / "cards.json", extract)


# --------------------------------------------------------------------------
# delta rendering -- the actual product
# --------------------------------------------------------------------------


def _fmt_ids(ids: set[str], limit: int = 12) -> str:
    shown = sorted(ids)[:limit]
    tail = f" (+{len(ids) - limit} more)" if len(ids) > limit else ""
    return ", ".join(shown) + tail


def delta_lines(before: Snap, after: Snap) -> list[str]:
    """What moved in this artefact. Empty list means genuinely nothing."""
    out: list[str] = []
    if after.error:
        out.append(f"{after.label}: UNREADABLE -- {after.error}")
        return out
    if not after.exists:
        out.append(f"{after.label}: MISSING at {after.path}")
        return out
    if not before.exists:
        facts = ", ".join(f"{k} {v}" for k, v in after.facts.items() if k != "mtime")
        out.append(f"{after.label}: CREATED -- {facts}")
        return out
    if before.error:
        out.append(f"{after.label}: was unreadable before, now reads OK")

    for key in after.facts:
        if key == "mtime":
            continue
        old, new = before.facts.get(key), after.facts[key]
        if old != new:
            out.append(f"{after.label}: {key} {old} -> {new}")

    for kind, new_ids in after.ids.items():
        old_ids = before.ids.get(kind, set())
        gained, lost = new_ids - old_ids, old_ids - new_ids
        if gained:
            out.append(f"{after.label}: new {kind} ({len(gained)}): {_fmt_ids(gained)}")
        if lost:
            out.append(f"{after.label}: gone {kind} ({len(lost)}): {_fmt_ids(lost)}")

    if not out:
        rewritten = before.facts.get("mtime") != after.facts.get("mtime")
        note = "rewritten, content identical" if rewritten else "untouched"
        counts = ", ".join(
            f"{k} {v}" for k, v in after.facts.items() if k != "mtime"
        )
        out.append(f"{after.label}: no change ({note}) -- {counts}")
    return out


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------


EXIT_MAINTENANCE = 3   # the game server is in maintenance: nothing ran
EXIT_NO_KEYS = 4       # the game client's build constants are not on this PC: nothing ran

# The steps that talk to the game's API, whose messages are sealed with the client's payload
# seed (game_keys.py). The CDN downloads and the local builds do not need it.
API_STEPS = ("master_index", "account", "inventory", "event_cutoffs")


def missing_keys(step_names) -> bool:
    """Whether a run of these steps needs the client's payload seed and this PC has none."""
    if not set(step_names) & set(API_STEPS):
        return False
    import game_keys  # noqa: PLC0415
    return game_keys.grpc_seed(required=False) is None


@dataclass
class Step:
    name: str
    what: str
    network: bool
    probes: list[str]
    build: Callable[[argparse.Namespace], tuple[list[str], dict[str, str], Path]]
    # A soft step's failure is printed as a WARN and never fails the refresh: a
    # convenience artefact (an icon) must not turn the daily command red.
    soft: bool = False


def _pyexe(args: argparse.Namespace) -> str:
    return args.python or sys.executable


def start_two_stage_build(pyexe: str) -> str:
    """Start `two_stage_build.py` DETACHED and return one line saying so.

    The per-song and in-general answers (best leader + five members) are a
    function of the inputs this refresh just rewrote, and recomputing the ones
    whose key changed is minutes of every core — not something to make the
    refresh wait for. The builder computes only what is stale, holds a lock so
    two refreshes do not start two builders, and starts over by itself if the
    inputs move under it. It writes under `dumps/two_stage/` only; nothing the
    data guards watch. A failure to start is reported, never raised: the
    refresh itself has already succeeded."""
    script = HERE / "two_stage_build.py"
    out_dir = HERE / "dumps" / "two_stage"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = os.pathsep.join(
            str(REPO / part) for part in ("src", "tools/web", "tools/recon/proxy"))
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        with open(out_dir / "build.stdout.log", "ab") as sink:
            proc = subprocess.Popen(
                [pyexe, str(script)], cwd=str(REPO), env=env, stdin=subprocess.DEVNULL,
                stdout=sink, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
    except Exception as exc:  # noqa: BLE001 - never fail a finished refresh over this
        return f"two-stage answers: could NOT start the background build ({exc!r})"
    return (f"two-stage answers: background build started (pid {proc.pid}); "
            f"progress in {out_dir / 'status.json'}")


PICTURES_LOCK_MAX_S = 6 * 3600     # a picture run's lock older than this is a stopped run's leftover


def _pictures_lock() -> Path:
    """While a refresh fetches pictures, its {"pid", "started"} is here: a first background run
    takes about an hour, and a second refresh meanwhile would fetch and write the same files."""
    return DUMPS / "pictures_refresh.lock"


def _alive(pid: int) -> bool:
    """Whether process `pid` is running. On Windows `os.kill(pid, 0)` would END it, so the
    process is opened and asked for its exit code (259 = STILL_ACTIVE)."""
    if sys.platform == "win32":
        import ctypes  # noqa: PLC0415

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)       # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def pictures_busy() -> int | None:
    """The pid of another refresh fetching pictures right now, else None."""
    try:
        doc = json.loads(_pictures_lock().read_text("utf8"))
        pid, began = int(doc["pid"]), float(doc["started"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if pid == os.getpid() or time.time() - began > PICTURES_LOCK_MAX_S or not _alive(pid):
        return None
    return pid


def hold_pictures_lock() -> None:
    """Mark this process as the one fetching pictures, until it exits."""
    import atexit  # noqa: PLC0415

    lock = _pictures_lock()
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": os.getpid(), "started": time.time()}), "utf8")
    except OSError:
        return

    def release() -> None:
        try:
            if json.loads(lock.read_text("utf8")).get("pid") == os.getpid():
                lock.unlink()
        except (OSError, ValueError):
            pass

    atexit.register(release)


def start_pictures(pyexe: str, steps: list[str], offline: bool) -> str:
    """Start `refresh_all.py --only <picture steps>` DETACHED, after the rest of the refresh:
    the app is usable now and the pictures fill in as they arrive. They need only the install's
    catalogue and the asset CDN, so a refused account read does not hold them back. Its output
    goes to dumps/pictures_refresh.log. A failure to start is reported, never raised."""
    log = HERE / "dumps" / "pictures_refresh.log"
    cmd = [pyexe, str(Path(__file__).resolve()), "--only", ",".join(steps)]
    if offline:
        cmd.append("--offline")
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        with open(log, "ab") as sink:
            proc = subprocess.Popen(cmd, cwd=str(REPO), env=env, stdin=subprocess.DEVNULL,
                                    stdout=sink, stderr=subprocess.STDOUT, creationflags=flags,
                                    close_fds=True)
    except Exception as exc:  # noqa: BLE001 - never fail a finished refresh over this
        return f"pictures: could NOT start the background run ({exc!r}); run them with --only {','.join(steps)}"
    return (f"pictures: {', '.join(steps)} running in the background (pid {proc.pid}); "
            f"the app works meanwhile; progress in {log}")


def cmd_master(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    script = MASTER_REPO / "scripts" / "run-local.ps1"
    cmd = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-NoProfile",
        "-File",
        str(script),
    ]
    env = {"FORCE_RUN": "1"} if args.force else {}
    return cmd, env, MASTER_REPO


def cmd_master_index(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # NO `--account`. `master_index_live.py` takes only `--dry-run`: it calls
    # `Master/Get`, which declares `disabled_game_auth_token`, so it needs no
    # credential and has no account to choose. Passing one makes argparse exit 2
    # and the step fail on every run.
    return [_pyexe(args), str(HERE / "master_index_live.py")], {}, HERE


def cmd_event_bonus(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "marathon_bonus.py")], {}, HERE


def cmd_inventory(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "inventory_snapshot.py")], {}, HERE


def cmd_account(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    cmd = [
        _pyexe(args),
        str(HERE / "account_snapshot.py"),
        "--account",
        args.account,
        "--snapshot",
        "--refresh-card-params",
        "--refresh-board-state",
        "--diff",
    ]
    # The desktop app gets the open event from the publisher, and
    # ListEventInfo is the call a not-yet-reset game day refuses (DATE_CHANGED).
    if not getattr(args, "account_only", False):
        cmd.insert(-1, "--refresh-events")
    if args.merge_collection:
        cmd.append("--merge-collection")
    return cmd, {}, HERE


def cmd_cards(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "build_cards_dataset.py")], {}, HERE


def cmd_level_curve(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "card_level_curve.py")], {}, HERE


def cmd_caps(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "card_caps.py")], {}, HERE


# The master tables the runtime reads as dumps/holodori/main_<T>.json, written from the local
# master by master_json.py (the directory name only reflects the export format it mirrors).
# Writing them locally means they never lag the game.
LEVEL_TABLES = ("Card", "CardLevel", "LiveNote", "SkillTreeNode")
# the open event's title (event_art.py), written with the level tables so that the FIRST refresh's
# event_art step has them: song_meta runs after it
EVENT_TITLE_TABLES = ("Poster", "LangPoster_Eng", "LangEventStoryChapter_Eng")
SONG_META_TABLES = (
    "Music", "LangMusic_Eng", "Poster",
    # read by the web/app/pokajan
    "Setting", "LangItem_Eng", "LangMarathonRankingGrade_Eng", "MarathonRankingGrade",
    "LiveDeckEvaluationRank", "LiveScoreEvaluationRank", "SkillTreeEffectValueLimit",
    "Character", "CharacterGrouping", "SdCostume", "Mission", "MissionProgress",
    "ComboCardGameCard", "ComboCardGameDeck", "ComboCardGameRoleScore", "ComboCardGameSetting",
)


def cmd_level_tables(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "master_json.py"), *LEVEL_TABLES, *EVENT_TITLE_TABLES], {}, HERE


def cmd_import(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "import_to_cards.py")], {}, HERE


def cmd_add_cards(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    cmd = [_pyexe(args), str(HERE / "card_append.py")]
    if args.dry_run_adds:
        cmd.append("--dry-run")
    return cmd, {}, HERE


def cmd_catalog(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "octo_catalog.py")], {}, HERE


def cmd_charts(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "chart_download.py")], {}, HERE


def cmd_songs(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "build_songs.py")], {}, HERE


def cmd_song_meta(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # casts (Music) + titles (LangMusic_Eng), from the master (master_json.py).
    # ...and Poster: `maxed_account_bonuses` counts its rows as "every poster the
    # game has shipped", so it must follow the game as posters are added.
    return [_pyexe(args), str(HERE / "master_json.py"), *SONG_META_TABLES], {}, HERE


def cmd_chart_lib(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "build_charts.py"), "--rebuild-caches",
            "--all-out", str(DUMPS / "charts_all.json"), "--verify"], {}, HERE


def cmd_jackets(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "song_jackets.py"), "--sync"], {}, HERE


def cmd_card_art(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "card_art_sync.py")], {}, HERE


def cmd_event_art(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # Exits 0 whatever is missing: a banner the CDN will not serve is a printed line
    # and a null title/art on the Event page, never a failed refresh.
    return [_pyexe(args), str(HERE / "event_art.py")], {}, HERE


def cmd_event_cutoffs(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # Every region's tier borders (guests) plus the account's own rank in its region (the
    # account this refresh already logged in with). An ended chapter the server still lists
    # is read once more for its finals. Reading them on every refresh keeps the Event page
    # from sitting on an old read and missing the event's end and the final rank.
    own = ["--region", "own"] if getattr(args, "account_only", False) else []
    return [_pyexe(args), str(HERE / "event_cutoffs.py"), "--self", "--songs", *own], {}, HERE


def cmd_event_emblems(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # after event_history: it names the chapters on record (each one's exact medal)
    return [_pyexe(args), str(HERE / "event_emblems.py")], {}, HERE


def cmd_event_history(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # Offline: folds event_meta (archived before the event leaves the list), the cutoff store
    # (the account's rank, Event Pt, final borders) and the emblems held into event_history.json.
    return [_pyexe(args), str(HERE / "event_history.py")], {}, HERE


def cmd_item_icons(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # Reads the client's own Octo cache; downloads nothing. A new item's icon arrives
    # with the refresh rather than waiting for a hand run.
    return [_pyexe(args), str(HERE / "item_icons.py")], {}, HERE


def cmd_ui_assets(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "ui_assets.py")], {}, HERE


def cmd_node_master(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "master_export.py"), "SkillTreeNode",
            "SkillTreeEffect"], {}, HERE


def cmd_skilltree(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "build_skilltree.py")], {}, HERE


def cmd_member_icons(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "member_icons.py")], {}, HERE


def cmd_skill_icons(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "skill_icons.py")], {}, HERE


def cmd_park_export(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    return [_pyexe(args), str(HERE / "park_export.py")], {}, HERE


def cmd_board(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    cmd = [
        _pyexe(args),
        str(HERE / "build_board.py"),
        "--connect-level",
        args.connect_level,
    ]
    return cmd, {}, HERE


def cmd_board_export(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # board_export.py must run FROM THE REPO ROOT so hbuilder's collection resolves
    # (its own docstring says so) — hence REPO rather than HERE as the cwd.
    return [_pyexe(args), str(HERE / "board_export.py")], {}, REPO


def cmd_live2d(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # The leaders' Live2D figures for the Optimizer (live2d_build.py): every outfit of the
    # game's catalogue that is not converted yet, so a NEW card's leader figure appears without
    # a hand run, owned or not -- "Maxed account" and the pulls page lead with any card,
    # unowned ones included. Built models are skipped, so a
    # refresh with nothing new downloads nothing; a new outfit is ~4.5 MB.
    return [_pyexe(args), str(HERE / "live2d_build.py"), "--all"], {}, HERE


def cmd_chibi(args: argparse.Namespace) -> tuple[list[str], dict[str, str], Path]:
    # The Optimizer's progress-bar runners (chibi_build.py): only the SD
    # outfits of the game's current master SdCostume with no model yet, so a new outfit runs
    # without a hand run; a refresh with nothing new downloads nothing.
    return [_pyexe(args), str(HERE / "chibi_build.py"), "--new"], {}, HERE


# What the desktop app refreshes (`--account-only`): the steps that read the account itself,
# and the two boards built from its board state. Everything else is shared content the
# publisher's full refresh builds and tools/app/publish.py serves.
# event_cutoffs reads only the account's own region there (`--region own`): the app has no guest
# accounts, and the borders come from the tracker; what only this account can read is its own
# rank and Event Pt (without this step the app's Event page shows no points after a refresh).
ACCOUNT_STEPS = ("account", "inventory", "board", "board_export", "event_cutoffs", "event_history")

# Steps that only bring pictures: nothing the optimizer scores reads them, and the pages draw
# without them (a missing picture is a 404). On a first refresh they are most of the time
# (chibis ~50 min and card art / jackets / Live2D from the CDN without a game cache), so
# `--pictures-later` runs everything else first and these in the background afterwards.
PICTURE_STEPS = ("card_art", "event_emblems", "jackets", "live2d", "chibi", "park_export")

STEPS: list[Step] = [
    Step(
        "master",
        "hololive-dreams-master dump (fetch + decrypt + commit + push)",
        network=True,
        probes=["master_tables"],
        build=cmd_master,
    ),
    Step(
        "master_index",
        "dumps/master_index.json from a live Master/Get",
        network=True,
        probes=["master_index"],
        build=cmd_master_index,
    ),
    # The account step turns each card's exp into a LEVEL through the Card +
    # CardLevel tables (main_*.json). A card newer than those tables has no row
    # there, so it would merge into state/collection.json with NO level -- and
    # `power_up_permyriad: "auto"` sums over every owned card's level, so one
    # missing level moves the Member Power-Up line of every deck. LiveNote and
    # SkillTreeNode are the other two tables live code reads. Written from the
    # local master (master_json.py), after master_index so a table the game just
    # changed is read at its new seed.
    Step(
        "level_tables",
        "Card + CardLevel (exp -> level) + LiveNote + SkillTreeNode from our master",
        network=True,
        probes=[],
        build=cmd_level_tables,
    ),
    Step(
        "account",
        "Auth/Login -> User/Get -> card_params.json + board_state.json",
        network=True,
        probes=["snapshot", "card_params", "board_state", "collection"],
        build=cmd_account,
    ),
    Step(
        "event_bonus",
        "dumps/marathon_bonus.json -- the open event's bonus tables (Event/ListEventInfo)",
        network=False,
        probes=["event_bonus"],
        build=cmd_event_bonus,
    ),
    Step(
        "inventory",
        "dumps/inventory.json -- every item balance, incl. the 8 board currencies",
        network=True,
        probes=["inventory"],
        build=cmd_inventory,
    ),
    Step(
        "cards",
        "dumps/cards_enriched.json from the masters + card_params",
        network=False,
        probes=["cards_enriched"],
        build=cmd_cards,
    ),
    # Two more files read off the masters. If they fall behind the game, newer
    # cards cannot be restated at their real level and the account basis scores
    # them at their MAXED stats -- a wrong ranking, with nothing saying so.
    Step(
        "level_curve",
        "dumps/card_level_curve.json -- per-level stat multipliers and each card's group",
        network=False,
        probes=[],
        build=cmd_level_curve,
    ),
    Step(
        "caps",
        "dumps/card_caps.json -- level / bloom / limit-break caps by rarity",
        network=False,
        probes=[],
        build=cmd_caps,
    ),
    Step(
        "import",
        "dumps/cards_import_draft.json -- the schema-valid proposal",
        network=False,
        probes=["cards_json"],
        build=cmd_import,
    ),
    Step(
        "add_cards",
        "APPEND-ONLY: add cards the game ships and data/cards.json lacks",
        network=False,
        probes=["cards_json"],
        build=cmd_add_cards,
    ),
    # `catalog` precedes everything that asks the Octo CDN for an asset BY NAME.
    # After `card_art`, the first refresh after an update would look the new cards'
    # art up in the previous catalogue and fail ("NOT IN CAT").
    Step(
        "catalog",
        "dumps/octocacheevai_prod.dec from the client's own install (no network)",
        network=False,
        probes=[],
        build=cmd_catalog,
    ),
    # Item/resource thumbs for the Resources view. Needs `catalog` (md5 -> asset
    # name). Local only, and soft: a missing cache or a bad bundle is a WARN line.
    Step(
        "item_icons",
        "dumps/item_icons/ -- item + resource thumbs from the local Octo cache "
        "(no network; never fails the refresh)",
        network=False,
        probes=[],
        build=cmd_item_icons,
        soft=True,
    ),
    # The card chrome and the official UI sprites the pages draw (ui_assets.json says which);
    # after item_icons, whose thumbs it copies. Local install only, soft.
    Step(
        "ui_assets",
        "dumps/card_chrome/ + dumps/ui_official/ -- the game's own UI sprites the pages draw "
        "(local install, no network; never fails the refresh)",
        network=False,
        probes=[],
        build=cmd_ui_assets,
        soft=True,
    ),
    Step(
        "card_art",
        "portrait + square WEBP for cards the art dir lacks (Octo CDN by name, 1 req/s)",
        network=True,
        probes=["card_art"],
        build=cmd_card_art,
    ),
    # The open events' title (master Poster name), kind, times and banner/logo art.
    # Needs `account` (event_info_live.bin), `master` (the Poster tables) and
    # `catalog` (asset names) before it.
    Step(
        "event_art",
        "dumps/event_meta.json + dumps/event_art/ -- event title, kind, times, banner "
        "(Octo CDN by name, 1 req/s; never fails the refresh)",
        network=True,
        probes=[],
        build=cmd_event_art,
    ),
    Step(
        "event_cutoffs",
        "dumps/cutoffs/<region>/ -- every region's tier borders + the owner's own rank "
        "(appended; an ended chapter read once more for its finals; never fails the refresh)",
        network=True,
        probes=[],
        build=cmd_event_cutoffs,
        soft=True,
    ),
    Step(
        "event_history",
        "dumps/event_history.json -- every event seen, the owner's result and the final borders "
        "(additive: an event once seen is never dropped; offline, never fails the refresh)",
        network=False,
        probes=[],
        build=cmd_event_history,
        soft=True,
    ),
    Step(
        "event_emblems",
        "dumps/event_art/emblem-<chapter>-<grade>.webp -- every chapter's ranking medal as the game "
        "draws it (Octo CDN, new ones only; published with the event art; never fails the refresh)",
        network=True,
        probes=[],
        build=cmd_event_emblems,
        soft=True,
    ),
    # The song chain, and it is three steps because a song shipping is three
    # separate things going stale: a stale catalogue does not name a new song's
    # charts, so the downloader cannot ask for them, so songs.json has no chart to
    # point at. Nothing reports a problem — `songs.json` gives the old count and
    # means it.
    Step(
        "charts",
        "download .sus charts the catalogue names and disk lacks (1 req/s, md5-verified)",
        network=True,
        probes=[],
        build=cmd_charts,
    ),
    Step(
        "songs",
        "dumps/songs.json from the masters + the charts on disk",
        network=False,
        probes=["songs"],
        build=cmd_songs,
    ),
    # ...and a NEW SONG is three more things going stale after songs.json: its
    # cast + title (Music, LangMusic_Eng), the parsed-chart caches the solver scores
    # from (charts.json is TWO rebuilds downstream of a .sus landing), and its
    # jacket. With all three stale a song has no cast (so every singer node is
    # inert on it), "(no title on file)" and no jacket.
    Step(
        "song_meta",
        "Music (casts) + LangMusic_Eng (titles) + the other runtime tables from our master",
        network=True,
        probes=["song_casts", "song_titles"],
        build=cmd_song_meta,
    ),
    Step(
        "chart_lib",
        "lattice/music_coef caches if stale, then dumps/charts.json + "
        "dumps/charts_all.json (every difficulty, --verify)",
        network=False,
        probes=["chart_library"],
        build=cmd_chart_lib,
    ),
    Step(
        "jackets",
        "song jackets the catalogue names and disk lacks (Octo CDN, 1 req/s) + render",
        network=True,
        probes=["jackets"],
        build=cmd_jackets,
    ),
    # The Connect effects build_board resolves sockets against. A stale file has
    # no Connect effect for a newer socketed card and `board` fails outright.
    # Offline, from the masters, a second or two.
    # build_board, tree_optimise, the CLI and the web server all read the node table
    # as dumps/master_SkillTreeNode.json, which must follow the game when it adds
    # nodes to the trees.
    Step(
        "node_master",
        "dumps/master_SkillTreeNode.json + master_SkillTreeEffect.json from the masters",
        network=False,
        probes=[],
        build=cmd_node_master,
    ),
    Step(
        "skilltree",
        "dumps/skilltree.json (board graph + card Connect effects) from the masters",
        network=False,
        probes=[],
        build=cmd_skilltree,
    ),
    # The web's pictures (holomem faces, board glyphs), so a clean checkout gets them without a
    # hand run. Local Octo cache, no network; soft, like item_icons.
    Step(
        "member_icons",
        "dumps/member_icons/ -- the round holomem faces (/chricon, the board export) from the "
        "local Octo cache (never fails the refresh)",
        network=False,
        probes=[],
        build=cmd_member_icons,
        soft=True,
    ),
    Step(
        "skill_icons",
        "dumps/skill_icons/ -- the board node glyphs (/icon) from the local Octo cache, plus "
        "what octo_fetch pulled (never fails the refresh)",
        network=False,
        probes=[],
        build=cmd_skill_icons,
        soft=True,
    ),
    Step(
        "board",
        "dumps/board.json from board_state.json",
        network=False,
        probes=["board"],
        build=cmd_board,
    ),
    Step(
        "board_export",
        "dumps/board_export.json -- the web frontend's board (nodes, costs, "
        "sockets, points.granted). MUST follow `board`.",
        network=False,
        probes=["board_export"],
        build=cmd_board_export,
    ),
    Step(
        "live2d",
        "dumps/live2d/ -- the leaders' Live2D figures (every new outfit of the game)",
        network=True,
        probes=[],
        build=cmd_live2d,
    ),
    Step(
        "chibi",
        "dumps/chibi/ -- the Optimizer's running chibis (new SD outfits of the game only)",
        network=True,
        probes=[],
        build=cmd_chibi,
    ),
    # Parkview's raw maps + every spot's pixel position (park_export.py). The map bundles come
    # from the Octo CDN once, then the export is offline. Soft.
    Step(
        "park_export",
        "dumps/park_export/ -- Parkview's maps and spot positions (Octo CDN once; never fails "
        "the refresh)",
        network=True,
        probes=[],
        build=cmd_park_export,
        soft=True,
    ),
]

STEP_NAMES = [s.name for s in STEPS]


def probe_event_bonus() -> Snap:
    """dumps/marathon_bonus.json -- the event bonus tables, and which groups are open NOW.

    The optimiser must always work with the current event, so the tables are read on every
    refresh rather than taken from hand-made captures."""

    def extract(doc: Any) -> tuple[dict, dict]:
        groups = doc.get("groups") or []
        current = doc.get("current_group_ids") or []
        facts = {"groups": len(groups), "open_now": len(current)}
        facts.update({f"open {g}": "yes" for g in current})
        # each open chapter's songs against its song-score-bonus rows: a merge that lost
        # songs (e.g. a "normal" event keeping 2 of its 5-6) shows here
        with_row: dict[str, set] = {}
        for g in groups:
            if g.get("kind") == "music":
                with_row.setdefault(str(g.get("group_id", "")).split("-", 1)[-1], set()).update(
                    r.get("music_id") for r in g.get("rows") or [])
        for chapter, songs in sorted((doc.get("event_songs") or {}).items()):
            have = with_row.get(chapter, set())
            facts[f"songs {chapter}"] = (f"{len(songs)}, {sum(m in have for m in songs)} "
                                         "with a song bonus")
        return facts, {"group": {g.get("group_id") for g in groups}}

    return _probe("marathon_bonus.json", DUMPS / "marathon_bonus.json", extract)


def probe_table(args: argparse.Namespace) -> dict[str, Callable[[], Snap]]:
    return {
        "master_tables": probe_master_tables,
        "master_index": probe_master_index,
        "card_params": probe_card_params,
        "cards_enriched": probe_cards_enriched,
        "board_state": probe_board_state,
        "board": probe_board,
        "board_export": probe_board_export,
        "inventory": probe_inventory,
        "event_bonus": probe_event_bonus,
        "collection": probe_collection,
        "snapshot": probe_snapshot(args.account),
        "cards_json": probe_cards_json,
        "songs": probe_songs,
        "chart_library": probe_chart_library,
        "song_titles": probe_song_titles,
        "song_casts": probe_song_casts,
        "jackets": probe_jackets,
        "card_art": probe_card_art,
    }


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


def _digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(cmd: list[str], extra_env: dict[str, str], cwd: Path) -> tuple[int, str]:
    env = dict(os.environ)
    env.update(extra_env)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout or ""


def _tail(text: str, n: int) -> list[str]:
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return lines[-n:]


def master_guard_state(force: bool) -> str:
    stamp = MASTER_REPO / ".last-run"
    today = _dt.date.today().isoformat()
    if not stamp.exists():
        return "never run"
    last = stamp.read_text(encoding="utf-8", errors="replace").strip()
    if last == today and not force:
        return f"already ran today ({today}); run-local.ps1 will no-op -- use --force"
    return f"last run {last}"


def owned_ids() -> list[str] | None:
    """The user's collection as it stands on disk, or `None` if unreadable."""
    try:
        raw = json.loads(APPEND_ONLY_OWNED.read_bytes().decode("utf-8"))
    except Exception:
        return None
    ids = raw.get("owned")
    return list(ids) if isinstance(ids, list) else None


def _owned_bytes(payload: dict[str, Any]) -> bytes:
    """Serialise owned.json the way it is already written on disk: 2-space
    indent, sorted keys, CRLF, trailing newline."""
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    return (text + "\n").replace("\n", "\r\n").encode("utf-8")


def merge_owned(account: str) -> tuple[list[str], bool]:
    """APPEND cards the account owns and `data/owned.json` lacks. Never anything else.

    Three things make this safe enough to be automatic:

    1. The ids come from `card_params.json`, which is the account's own `User/Get`
       read back from the server -- nothing is guessed.
    2. It is append-only: existing entries are carried across untouched, and
       `owned_guard` verifies that after the run rather than trusting it.
    3. It refuses to write unless re-serialising the UNCHANGED data reproduces the
       file byte for byte, so a formatting drift can never be smuggled in with an
       addition. A 26-line append must not become a whole-file reformat.
    """
    created = False
    if not APPEND_ONLY_OWNED.exists():
        # A fresh desktop-app install ships no account: the file is
        # born empty in this tool's own format and filled below -- appending to nothing.
        # Where the file exists this never runs.
        atomic_io.write_bytes(APPEND_ONLY_OWNED, _owned_bytes({"owned": [], "schema_version": "2"}))
        created = True
    have = owned_ids()
    if have is None:
        return ["data/owned.json unreadable -- not merging"], True
    params = probe_card_params()
    if params.exists and not params.error:
        ids, source = params.ids["card"], "card_params.json"
    else:
        # Card/GetParameters is the call a not-yet-reset game day refuses (DATE_CHANGED); the
        # collection merged a moment ago is the same account's own User/Get, so its card ids
        # are just as much the account's word. Nothing guessed either way.
        try:
            ids = set(json.loads((STATE / "collection.json").read_text("utf-8"))["cards"])
            source = "state/collection.json"
        except (OSError, ValueError, KeyError, TypeError):
            return [f"card_params.json unavailable ({params.error}) and no collection -- "
                    "not merging"], True

    missing = sorted(set(ids) - set(have))
    # only cards the catalogue knows: an owned id that data/cards.json lacks makes the whole
    # app refuse to load ("_LoadFailure: 3" on the page, e.g. a new event's card pulled
    # before the app's synced catalogue has it). Such a card is held back and appended
    # by the first refresh after the catalogue gains it (append-only either way).
    catalogue = catalogue_records()
    later = [cid for cid in missing if catalogue is not None and cid not in catalogue]
    missing = [cid for cid in missing if cid not in later]
    held_back = ([f"   held back until the card catalogue has it (update the app's data): {cid}"
                  for cid in later])
    original = APPEND_ONLY_OWNED.read_bytes()
    raw = json.loads(original.decode("utf-8"))
    if _owned_bytes(raw) != original:
        return ["data/owned.json is not in this tool's own format -- refusing to"
                " write it; merge by hand"], True
    if not missing:
        return (["data/owned.json already has every card the account owns"
                 + (" that the catalogue knows" if held_back else "")] + held_back), False

    backup = DUMPS / "owned_before_merge.json"
    if not backup.exists():
        atomic_io.write_bytes(backup, original)
    raw["owned"] = sorted(set(have) | set(missing))
    atomic_io.write_bytes(APPEND_ONLY_OWNED, _owned_bytes(raw))
    return ([f"data/owned.json: {'CREATED and ' if created else ''}APPENDED {len(missing)} "
             f"card(s) the account owns (ids from {source})"]
            + [f"   + {cid}" for cid in missing]
            + [f"   backup of the previous file: {backup}" if not backup.exists()
               else f"   first-run backup kept at {backup}"] + held_back, False)


def owned_guard(before: list[str] | None) -> tuple[list[str], bool]:
    """Enforce append-only on `data/owned.json` after the run."""
    after = owned_ids()
    if before is None or after is None:
        state = "before" if before is None else "after"
        return [f"data/owned.json was unreadable {state} the run -- cannot verify"], True
    removed = sorted(set(before) - set(after))
    if removed:
        lines = ["!! data/owned.json IS APPEND-ONLY AND A CARD DISAPPEARED !!"]
        lines += [f"   REMOVED  {cid}" for cid in removed]
        return lines, True
    added = sorted(set(after) - set(before))
    if added:
        return [f"data/owned.json append-only guard: {len(added)} added, none removed"], False
    return ["data/owned.json append-only guard: unchanged"], False


def catalogue_records() -> dict[str, Any] | None:
    """`data/cards.json` as {id: record}, or None if it cannot be read.

    Deliberately the whole record and not a digest: the guard has to distinguish
    "two cards were appended" from "one card's magnitude changed", and a sha256
    says only that the bytes moved.
    """
    try:
        doc = json.loads(APPEND_ONLY.read_bytes().decode("utf-8"))
        return {c["id"]: c for c in doc["cards"]}
    except Exception:  # noqa: BLE001 -- unreadable is a state, not a crash
        return None


def catalogue_guard(before: dict[str, Any] | None) -> tuple[list[str], bool]:
    """Enforce append-only on `data/cards.json`, by comparing records.

    The rule allows exactly one kind of change: whole new ids appearing. A
    modified record, a vanished record, or a file that stopped parsing are all
    failures, and each is named rather than summarised -- if this ever fires,
    the useful output is WHICH card moved.
    """
    after = catalogue_records()
    if before is None or after is None:
        state = "before" if before is None else "after"
        return [f"data/cards.json was unreadable {state} the run -- cannot verify"], True

    removed = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    changed = sorted(cid for cid in set(before) & set(after) if before[cid] != after[cid])

    if removed or changed:
        lines = ["!! data/cards.json IS APPEND-ONLY AND SOMETHING ELSE HAPPENED !!"]
        for cid in removed:
            lines.append(f"   REMOVED  {cid}")
        for cid in changed:
            lines.append(f"   MODIFIED {cid}")
        return lines, True
    if added:
        return [
            f"data/cards.json grew by {len(added)} card(s), every existing entry "
            "unchanged -- verified record by record:",
            *[f"   + {cid}" for cid in added],
        ], False
    return ["data/cards.json unchanged -- verified record by record"], False


def probe_owned_json() -> Snap:
    def extract(doc: Any) -> tuple[dict, dict]:
        ids = set(doc["owned"]) if isinstance(doc, dict) else set(doc)
        return {"owned": len(ids)}, {"card": {str(i) for i in ids}}

    return _probe("data/owned.json", DATA / "owned.json", extract)


def _compare(
    live: Snap,
    hand: Snap,
    live_kind: str,
    hand_kind: str,
    hint: str,
    policy: str = "is authoritative and hand-edited; this tool will not",
) -> tuple[list[str], bool]:
    if not live.exists or not hand.exists:
        return [f"{live.label} / {hand.label}: one is missing -- cannot compare"], False
    if live.error or hand.error:
        return [f"{live.label} / {hand.label}: {live.error or hand.error}"], False

    live_ids, hand_ids = live.ids["card"], hand.ids["card"]
    missing, extra = live_ids - hand_ids, hand_ids - live_ids

    lines = [
        f"{live.label} {len(live_ids)} {live_kind} "
        f"| {hand.label} {len(hand_ids)} {hand_kind}"
    ]
    if missing:
        lines.append("")
        lines.append(
            f"  ** HAND MERGE OWED: {len(missing)} card(s) in {live.label} "
            f"and NOT in {hand.label} **"
        )
        for cid in sorted(missing):
            lines.append(f"     + {cid}")
        lines.append(f"  {hand.label} {policy}")
        lines.append(f"  {hint}")
    if extra:
        lines.append(
            f"  note: {len(extra)} id(s) in {hand.label} are not in {live.label}: "
            f"{_fmt_ids(extra)}"
        )
    if not missing and not extra:
        lines.append("  in sync -- no hand merge owed.")
    return lines, bool(missing)


def hand_merge_report(account: str = "main") -> tuple[list[str], bool]:
    """Everything the derived artefacts know that the hand-edited files do not.

    Two independent comparisons, because the two authoritative files answer two
    different questions:

      data/cards.json  -- the card CATALOGUE, and NOT the user's data. Compared
                          against cards_enriched.json, which is rebuilt from the
                          game's own Card master. A gap here means the game shipped
                          a card the catalogue has not heard of. The `add_cards` step closes
                          such a gap automatically -- append-only -- so a gap
                          surviving to this report means the add REFUSED the card
                          and said why, or the step was skipped.
      data/owned.json  -- the user's COLLECTION. Compared against card_params.json,
                          which is a live Card/GetParameters of the real account. A
                          gap here means the account acquired a card and the
                          append has not happened. It also fails
                          tests/test_mode_b.py, which asserts the two agree.
    """
    lines, need_a = _compare(
        probe_cards_enriched(),
        probe_cards_json(),
        "ids",
        "ids",
        "Run the add_cards step, or add by hand if it refused them by name above.",
        policy="is the game's catalogue; this tool may ADD to it but never",
    )
    lines.append("")
    more, need_b = _compare(
        probe_card_params(),
        probe_owned_json(),
        "captured",
        "owned",
        'Add the ids to the "owned" list by hand. '
        "tests/test_mode_b.py asserts these two match.",
        policy="is the owner's account and is hand-edited; this tool will not",
    )
    lines += more

    # state/collection.json is not hand-only -- the tool CAN write it, but only
    # when asked. Say when it has fallen behind the live account rather than
    # merging silently.
    params, coll = probe_card_params(), probe_collection()
    if params.exists and coll.exists and not (params.error or coll.error):
        behind = params.ids["card"] - coll.ids["card"]
        ahead = coll.ids["card"] - params.ids["card"]
        lines.append("")
        if behind or ahead:
            lines.append(
                f"  state/collection.json is STALE: {len(behind)} card(s) live but "
                f"not in it{f', {len(ahead)} in it but not live' if ahead else ''} "
                "-- re-run with --merge-collection"
            )
            if behind:
                lines.append(f"     + {_fmt_ids(behind)}")
        # ...and the half the id sets cannot see: a card that IS in both but at
        # the wrong level. Reported whether or not the sets agree, because the
        # two failures are independent and the "matches" line below must not
        # hide this one.
        drift = collection_level_drift(
            STATE / "collection.json", DUMPS / f"account_snapshot_{account}.json"
        )
        if drift:
            lines.append(
                f"  state/collection.json is STALE: {len(drift)} card field(s) "
                f"disagree with the live account -- re-run with --merge-collection"
            )
            for card_id, field, mine, live in drift[:8]:
                lines.append(f"     ~ {card_id} {field} {mine} -> {live}")
            if len(drift) > 8:
                lines.append(f"     ~ ...and {len(drift) - 8} more")
        elif not (behind or ahead):
            lines.append("  state/collection.json matches the live account.")

    stale = derived_artefact_drift()
    lines.append("")
    if stale:
        lines.append("  DERIVED FILES ARE STALE -- rebuilt by the full chain, "
                     "silent if you ran a single step by hand:")
        for derived, source, hours in stale:
            lines.append(f"     ~ {derived} is {hours:.1f} h older than {source}")
    else:
        lines.append("  derived files are newer than their sources.")

    # The song chain has no hand-edited authority to compare against, so its
    # staleness is reported here rather than as a merge: the failure it guards
    # is the solver scoring a chart set that no longer matches the disk.
    lines.append("")
    lines += chart_cache_state()
    library = probe_chart_library()
    if library.exists and not library.error:
        lines.append(f"  charts.json holds {library.facts.get('charts')} EXPERT charts")

    return lines, need_a or need_b


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Refresh every derived artefact and report what actually changed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="steps: " + ", ".join(STEP_NAMES),
    )
    ap.add_argument("--account", default="main", help="account key (default: main)")
    ap.add_argument(
        "--offline",
        action="store_true",
        help="skip every step that touches the network (master, master_index, account)",
    )
    ap.add_argument("--only", default="", help="comma-separated step names to run")
    ap.add_argument(
        "--account-only",
        action="store_true",
        help="the desktop app's refresh: only the steps that read THIS account (%s), "
        "with the collection merge; everything shared comes from the server's content"
        % ", ".join(ACCOUNT_STEPS),
    )
    ap.add_argument("--skip", default="", help="comma-separated step names to skip")
    ap.add_argument(
        "--pictures-later",
        action="store_true",
        help="run the picture-only steps (%s) in the background after the rest, so a first "
        "refresh is usable sooner" % ", ".join(PICTURE_STEPS),
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="override the master dump's once-a-day guard (FORCE_RUN=1)",
    )
    ap.add_argument(
        "--merge-collection",
        action="store_true",
        help="also MERGE the snapshot into state/collection.json (off by default)",
    )
    ap.add_argument(
        "--dry-run-adds",
        action="store_true",
        help="run the add_cards step in report-only mode: name the cards it would "
        "add to data/cards.json, write nothing",
    )
    ap.add_argument(
        "--connect-level",
        # "auto": each socket's level from its card's bloom in state/collection.json
        # (bloom 5 on a 4/5-star = level 2). The collection holds every owned card's
        # bloom from User/Get, so auto is the account. On an account with 10 bloom-5
        # socket cards, "1" reads Overall Power 335,354 and its board 93,999 against
        # the game's 335,758 / 94,389; "auto" reproduces both exactly.
        default="auto",
        choices=("auto", "1", "2"),
        help="passed through to build_board.py (default: auto, from each socket card's bloom)",
    )
    ap.add_argument("--python", default="", help="interpreter for the Python steps")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and the current artefact state; run nothing",
    )
    ap.add_argument(
        "--verbose", action="store_true", help="print full output of every step"
    )
    ap.add_argument(
        "--no-two-stage",
        action="store_true",
        help="(the default since 2026-10-16; kept so older callers still parse)",
    )
    # The optimiser answers ON DEMAND (tools/web/ondemand.py), so a refresh does not
    # start the hours-long two-stage cache build unless asked to.
    ap.add_argument(
        "--two-stage",
        action="store_true",
        help="also start the background two-stage answers cache build when the refresh ends",
    )
    args = ap.parse_args(argv)

    # Print in UTF-8 whatever the console/pipe claims to be.
    # Redirected to a file, stdout can come up as cp1252, and the FIRST step whose output
    # carries a U+FFFD (e.g. build_charts' progress line) would kill the whole refresh with
    # UnicodeEncodeError mid-report. A refresh must not die over how a subprocess spelled a
    # character; `errors="replace"` keeps the line, mangled at worst.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # not a reconfigurable text stream
            pass

    # --offline must mean offline all the way down: `master_cache` self-heals a master
    # table by re-downloading it from the CDN, and that is a network request inside steps
    # this script declares `network=False`. The children inherit this.
    if args.offline:
        os.environ["HB_MASTER_NO_FETCH"] = "1"

    if args.account_only:
        if args.only:
            ap.error("--account-only and --only are exclusive")
        args.only = ",".join(ACCOUNT_STEPS)
        args.merge_collection = True
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    deferred: list[str] = []
    if args.pictures_later:
        deferred = [n for n in PICTURE_STEPS if n not in skip and (not only or n in only)]
        skip |= set(deferred)
    for name in only | skip:
        if name not in STEP_NAMES:
            ap.error(f"unknown step {name!r}; known: {', '.join(STEP_NAMES)}")
    busy = pictures_busy()          # another refresh is fetching pictures: leave them to it
    if busy:
        deferred = []

    def selected(step: Step) -> tuple[bool, str]:
        if only and step.name not in only:
            return False, "not in --only"
        if step.name in skip:
            return False, "--skip"
        if busy and step.name in PICTURE_STEPS:
            return False, f"another refresh is fetching the pictures (pid {busy})"
        if args.offline and step.network:
            return False, "--offline"
        return True, ""

    probes = probe_table(args)
    started = time.time()

    print("=" * 74)
    print("refresh_all -- bring every derived artefact up to date")
    print(f"repo         {REPO}")
    print(f"master repo  {MASTER_REPO}{'' if MASTER_REPO.is_dir() else '   (MISSING)'}")
    print(f"python       {_pyexe(args)}")
    print(f"master guard {master_guard_state(args.force)}")
    print("=" * 74)

    if args.dry_run:
        print("\n-- plan --")
        for step in STEPS:
            ok, why = selected(step)
            mark = "run " if ok else "skip"
            print(f"  [{mark}] {step.name:<13} {step.what}" + (f"   ({why})" if why else ""))
            cmd, env, cwd = step.build(args)
            envs = " ".join(f"{k}={v}" for k, v in env.items())
            print(f"           $ {envs + ' ' if envs else ''}{subprocess.list2cmdline(cmd)}")
        print("\n-- artefacts as they stand --")
        for key, fn in sorted(probes.items()):
            snap = fn()
            if not snap.exists:
                print(f"  {snap.label}: MISSING")
            elif snap.error:
                print(f"  {snap.label}: UNREADABLE -- {snap.error}")
            else:
                facts = ", ".join(f"{k} {v}" for k, v in snap.facts.items())
                print(f"  {snap.label}: {facts}")
        print("\n-- hand-merge check --")
        for line in hand_merge_report(args.account)[0]:
            print("  " + line)
        return 0

    # The game in maintenance refuses every call (each network step would fail on its own
    # and the gacha sync would die in a traceback). One
    # anonymous Master/Get first: in maintenance nothing runs, nothing is written, and the
    # exit code says why (EXIT_MAINTENANCE), so the page can say so instead of "partial".
    if any(selected(s)[0] and s.network for s in STEPS):
        import account_snapshot  # noqa: PLC0415
        if account_snapshot.in_maintenance(args.account):
            print("\nGAME IN MAINTENANCE -- the server refuses every call (IN_MAINTENANCE).")
            print("Nothing was refreshed and nothing was written; refresh again once it ends.")
            return EXIT_MAINTENANCE

    # Without the client's payload seed every API step would fail on its own, one traceback
    # after another, while the downloads around them ran for nothing: say what is missing
    # once, and run nothing. (The maintenance probe above needs no seed: a refusal carries no
    # sealed body, and without a seed it answers "unknown".)
    if missing_keys(s.name for s in STEPS if selected(s)[0]):
        print("\nTHE GAME CLIENT'S BUILD CONSTANTS ARE NOT ON THIS PC -- the game's API cannot be read.")
        print("Find them in your own game install (one anonymous request, nothing printed):")
        print("    python tools/recon/proxy/game_keys.py --find")
        print("refresh.cmd does this by itself; docs/dev/game-keys.md explains it. Nothing ran.")
        return EXIT_NO_KEYS

    if any(selected(s)[0] for s in STEPS if s.name in PICTURE_STEPS):
        hold_pictures_lock()

    sacred_before = {p: _digest(p) for p in SACRED}
    catalogue_before = catalogue_records()
    # absent (a fresh app install, before merge_owned creates it) is an empty list, not a fault
    owned_before = owned_ids() if APPEND_ONLY_OWNED.exists() else []

    results: list[tuple[str, str, list[str]]] = []  # name, verdict, delta lines
    failed: list[str] = []

    for step in STEPS:
        ok, why = selected(step)
        print()
        print("-" * 74)
        if not ok:
            print(f"[skip] {step.name} -- {why}")
            results.append((step.name, f"skipped ({why})", []))
            continue

        before = {key: probes[key]() for key in step.probes}
        cmd, env, cwd = step.build(args)
        # A step whose working directory is absent (e.g. the `master` step's
        # hololive-dreams-master repo on a machine that has not cloned it) cannot
        # run -- and launching a subprocess with a missing cwd raises
        # NotADirectoryError, which would crash the WHOLE refresh on the first
        # step so nothing after it ran. Skip it as a first-class "not here" state,
        # exactly like a deselected step, and let the rest of the refresh proceed.
        if not Path(cwd).is_dir():
            print(f"[skip] {step.name} -- working dir not here: {cwd}")
            results.append((step.name, f"skipped (dir missing: {cwd})", []))
            continue
        print(f"[run ] {step.name} -- {step.what}")
        print(f"       $ {subprocess.list2cmdline(cmd)}")
        sys.stdout.flush()

        t0 = time.time()
        try:
            code, out = _run(cmd, env, cwd)
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            code, out = 127, f"could not launch: {exc}"
        elapsed = time.time() - t0

        after = {key: probes[key]() for key in step.probes}
        deltas: list[str] = []
        for key in step.probes:
            deltas.extend(delta_lines(before[key], after[key]))

        if code == 0:
            verdict = f"ok ({elapsed:.0f}s)"
            for line in _tail(out, 40 if args.verbose else 6):
                print(f"       | {line}")
        elif step.soft:
            verdict = f"WARN exit {code} ({elapsed:.0f}s; soft step, the refresh goes on)"
            print(f"       ~~ exit {code} (soft)")
            for line in _tail(out, 12):
                print(f"       | {line}")
        else:
            verdict = f"FAILED exit {code} ({elapsed:.0f}s)"
            failed.append(step.name)
            print(f"       !! exit {code}")
            for line in _tail(out, 40):
                print(f"       | {line}")

        print(f"       -> {verdict}")
        for line in deltas:
            print(f"       ~ {line}")
        results.append((step.name, verdict, deltas))

    # ---- the authoritative files must not have moved -------------------
    print()
    print("=" * 74)
    tampered = [p for p in SACRED if _digest(p) != sacred_before[p]]
    if tampered:
        print("!! THE OWNER'S OWN DATA WAS WRITTEN BY THE REFRESH -- THIS IS A BUG !!")
        for p in tampered:
            print(f"   {p}")
        failed.append("authoritative-file-guard")
    else:
        names = ", ".join(p.name for p in SACRED)
        print(f"the owner's own files untouched ({names}) -- verified by sha256")

    catalogue_lines, catalogue_failed = catalogue_guard(catalogue_before)
    for line in catalogue_lines:
        print(line)
    if catalogue_failed:
        failed.append("append-only-guard")

    # The user's collection: append what the account owns, then verify nothing else moved.
    for line in merge_owned(args.account)[0]:
        print(line)
    owned_lines, owned_failed = owned_guard(owned_before)
    for line in owned_lines:
        print(line)
    if owned_failed:
        failed.append("owned-append-only-guard")

    # ---- summary -------------------------------------------------------
    print()
    print("SUMMARY")
    for name, verdict, deltas in results:
        print(f"  {name:<13} {verdict}")
        for line in deltas:
            print(f"      ~ {line}")

    print()
    print(
        "HAND-MERGE CHECK  (both data/cards.json and data/owned.json are APPEND-ONLY:"
        " a whole new id may appear, an existing entry may never move)"
    )
    merge_lines, needs_merge = hand_merge_report(args.account)
    for line in merge_lines:
        print("  " + line)

    print()
    if failed:
        print(f"FAILED -- {len(failed)} step(s): {', '.join(failed)}")
        print(f"({time.time() - started:.0f}s total)")
        if deferred:
            print(start_pictures(_pyexe(args), deferred, args.offline))
        return 1
    if needs_merge:
        print("all steps ok, but a hand merge into data/cards.json is owed (see above)")
    else:
        print("all steps ok")
    print(f"({time.time() - started:.0f}s total)")
    if args.two_stage and not args.no_two_stage and not args.only:
        print(start_two_stage_build(_pyexe(args)))
    if deferred:
        print(start_pictures(_pyexe(args), deferred, args.offline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
