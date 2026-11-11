"""One command to bring every derived artefact up to date -- and say what moved.

    python tools/refresh/refresh_all.py            # the whole chain
    python tools/refresh/refresh_all.py --offline  # local rebuilds only
    python tools/refresh/refresh_all.py --dry-run  # plan + current state, run nothing

The chain is the `STEPS` table in `refresh_steps.py`, in dependency order, one line per
step saying what it builds; `--dry-run` prints it with each step's current state. By kind:

    master tables  master, master_index, level_tables, node_master, song_meta, park_moves
    the account    account, event_bonus, inventory, board, board_export
    the catalogue  cards, level_curve, caps, import, add_cards (APPEND-ONLY), skilltree
    songs          catalog, charts, songs, chart_lib
    events         event_art, event_cutoffs, event_history, event_emblems
    pictures       card_art, item_icons, ui_assets, jackets, member_icons, skill_icons,
                   live2d, chibi, park_export

A `soft` step's failure is printed as a warning and the chain goes on. The `--dry-run`
plan marks the steps a run would skip, and why.

The point of this tool is the *diff*: every step is judged by comparing its artefacts on
disk before and after, and the report names what changed (table, card, node and slot counts,
and the ids that appeared or vanished). The one step that is not Python is `master`, the
sibling hololive-dreams-master repo's Windows script; it is skipped elsewhere
(`master_index` and the synced export cover what the refresh reads).

What this tool may write
------------------------
Both authoritative files are APPEND-ONLY: a whole new id may appear, an existing entry may
never be modified or removed.

`data/owned.json` is the account's collection. A card the account owns and the file lacks is
appended, its id taken from `card_params.json` (the account's own `User/Get`), so nothing is
guessed. `owned_guard` verifies the rule after the run, and the writer refuses to write unless
re-serialising the unchanged data reproduces the file byte for byte.

`data/cards.json` is the game's catalogue. `add_cards` adds a card the game ships and the file
lacks; a card it cannot translate cleanly is reported with its reason, never guessed.
`catalogue_guard` compares every pre-existing record for deep equality after the run.

Writing neither would let a new card sit unnoticed: the tool would answer "your best team"
without it. `--dry-run-adds` writes nothing.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import atomic_io
from refresh_steps import ACCOUNT_STEPS, PICTURE_STEPS, STEP_NAMES, STEPS, Step, _pyexe

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402

HERE = paths.REFRESH_DIR
DUMPS = paths.DUMPS
REPO = paths.REPO
STATE = paths.STATE
DATA = paths.DATA
MASTER_REPO = paths.MASTER_REPO

# The account's collection: append-only, enforced after the run by `owned_guard`.
APPEND_ONLY_OWNED = DATA / "owned.json"

# The game's catalogue: append-only, enforced record by record by `catalogue_guard`.
APPEND_ONLY = DATA / "cards.json"

# The four `m9999` tutorial difficulties carry two SPECIAL cues instead of five,
# so `build_charts.build_chart_cache` skips them by design. The parsed cache is
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
    facts["mtime"] = _dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
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
        complete = sum(1 for s in songs if all(d.get("chart_file") for d in s.get("difficulties", [])))
        return {"songs": len(songs), "all charts on disk": complete}, {"card": ids}

    return _probe("songs.json", DUMPS / "songs.json", extract)


def probe_chart_library() -> Snap:
    """`charts.json` -- the EXPERT chart library `--song`/`--across-songs` score on.

    Its own probe because it is two rebuilds downstream of a chart landing on disk, and the
    chain can silently stop at either.
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
        titles = {
            str(r.get("id"))[len("la-music_title-") :]
            for r in rows
            if str(r.get("id", "")).startswith("la-music_title-")
        }
        return {"titles": len(titles)}, {"card": titles}

    return _probe("main_LangMusic_Eng.json", DUMPS / "holodori" / "main_LangMusic_Eng.json", extract)


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
    names = {n[: -len(suffix)] for n in os.listdir(path) if n.endswith(suffix)}
    return Snap(label, path, True, {"files": len(names)}, {"card": names})


def probe_jackets() -> Snap:
    return _probe_dir("song_jackets/", DUMPS / "song_jackets", ".webp")


def probe_card_art() -> Snap:
    return _probe_dir("cards_explorer_art/", paths.TOOLS / "explorer" / "cards_explorer_art", ".sq.webp")


def chart_cache_state() -> list[str]:
    """Whether the parsed-chart caches have caught up with the .sus files.

    A plain `build_charts.py` run does not rebuild the parsed cache, so new charts on disk
    leave the solver scoring the old set while every step reports success.
    """
    sus_dir = DUMPS / "charts_cdn"
    if not sus_dir.is_dir():
        return ["  charts_cdn/ missing -- no charts downloaded yet."]
    on_disk = sum(1 for p in sus_dir.iterdir() if p.suffix == ".sus")
    lines = [f"  charts_cdn/ holds {on_disk} .sus files"]
    cache = DUMPS / "lattice_charts.json"
    if not cache.exists():
        lines.append("  lattice_charts.json MISSING -- run build_charts.py --rebuild-caches")
        return lines
    try:
        parsed = len(json.loads(cache.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        return lines + [f"  lattice_charts.json unreadable: {exc}"]
    expected = on_disk - TUTORIAL_CHART_COUNT
    if parsed < expected:
        lines.append(
            f"  ⚠ lattice_charts.json is STALE: {parsed} parsed against {expected} expected "
            f"({on_disk} .sus minus the {TUTORIAL_CHART_COUNT} tutorial charts). The solver is "
            "scoring the OLD chart set. Rebuild: run `build_charts.py --rebuild-caches --verify`."
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
            for slot in entry.get("slots") or {}:
                slots.add(f"{chr_id}:{slot}")
        facts = {"characters": len(doc), "filled_slots": len(slots), "nodes": nodes}
        return facts, {"slot": slots, "character": set(doc)}

    return _probe("board_state.json", DUMPS / "board_state.json", extract)


def probe_inventory() -> Snap:
    """dumps/inventory.json -- the item balances, incl. the materials a board node costs."""

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

    Behind `board.json`, it makes the web server diff its buy/sell proposal against a stale
    current board and budget plans against stale `points.granted`, silently."""

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

    A stale derived file is silent (e.g. `board.json` still describing the previous board
    after a direct `account_snapshot.py`). The full chain rebuilds them in order; this guards
    the other paths: a single step run by hand, an interrupted run.
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
        if behind > 60:  # a minute of slack for one chain run
            out.append((derived.name, source.name, behind / 3600.0))
    return out


def collection_level_drift(
    collection_path: Path, snapshot_path: Path
) -> list[tuple[str, str, object, object]]:
    """`(card_id, field, in_collection, on_account)` for every card the two
    disagree about. Empty when they agree, or when either file is absent.

    The id-set comparison cannot see a card held in both at different levels, which the
    solver would score at a fraction of her real stats. The snapshot carries `level`, `bloom`
    and `limit_break` verbatim, so the numbers are compared directly. A field the snapshot
    omits is not drift (absent is not zero). Cards only one side knows are the id probe's
    business."""
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
        for key in ("level", "bloom", "limit_break"):
            if key not in entry or key not in held:
                continue
            if entry[key] != held[key]:
                drift.append((str(card_id), key, held[key], entry[key]))
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
        counts = ", ".join(f"{k} {v}" for k, v in after.facts.items() if k != "mtime")
        out.append(f"{after.label}: no change ({note}) -- {counts}")
    return out


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------


EXIT_MAINTENANCE = 3  # the game server is in maintenance: nothing ran
EXIT_NO_KEYS = 4  # the game client's build constants are not on this PC: nothing ran

# The steps that talk to the game's API, whose messages are sealed with the client's payload
# seed (game_keys.py). The CDN downloads and the local builds do not need it.
API_STEPS = ("master_index", "account", "inventory", "event_cutoffs")


def missing_keys(step_names) -> bool:
    """Whether a run of these steps needs the client's payload seed and this PC has none."""
    if not set(step_names) & set(API_STEPS):
        return False
    import game_keys  # noqa: PLC0415

    return game_keys.grpc_seed(required=False) is None


PICTURES_LOCK_MAX_S = 6 * 3600  # a picture run's lock older than this is a stopped run's leftover


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
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
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
    log = DUMPS / "pictures_refresh.log"
    cmd = [pyexe, str(Path(__file__).resolve()), "--only", ",".join(steps)]
    if offline:
        cmd.append("--offline")
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        with open(log, "ab") as sink:
            # its own session on POSIX: closing the terminal (or Ctrl+C) does not stop it
            proc = subprocess.Popen(
                cmd,
                cwd=str(REPO),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                close_fds=True,
                start_new_session=sys.platform != "win32",
            )
    except Exception as exc:  # noqa: BLE001 - never fail a finished refresh over this
        return (
            f"pictures: could NOT start the background run ({exc!r}); run them with --only {','.join(steps)}"
        )
    return (
        f"pictures: {', '.join(steps)} running in the background (pid {proc.pid}); "
        f"the app works meanwhile; progress in {log}"
    )


def probe_event_bonus() -> Snap:
    """dumps/marathon_bonus.json -- the event bonus tables, and which groups are open NOW.

    Read on every refresh so the optimiser always works with the current event."""

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
                    r.get("music_id") for r in g.get("rows") or []
                )
        for chapter, songs in sorted((doc.get("event_songs") or {}).items()):
            have = with_row.get(chapter, set())
            facts[f"songs {chapter}"] = f"{len(songs)}, {sum(m in have for m in songs)} with a song bonus"
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

    The ids come from the account's own `User/Get`; existing entries are carried across
    untouched (`owned_guard` checks it); and it refuses to write unless re-serialising the
    unchanged data reproduces the file byte for byte, so an append never becomes a reformat.
    """
    created = False
    if not APPEND_ONLY_OWNED.exists():
        # A fresh desktop-app install ships no account: start an empty file in this format.
        atomic_io.write_bytes(APPEND_ONLY_OWNED, _owned_bytes({"owned": [], "schema_version": "2"}))
        created = True
    have = owned_ids()
    if have is None:
        return ["data/owned.json unreadable -- not merging"], True
    params = probe_card_params()
    if params.exists and not params.error:
        ids, source = params.ids["card"], "card_params.json"
    else:
        # Card/GetParameters is refused on a not-yet-reset game day (DATE_CHANGED); the
        # collection just merged from the same account's User/Get holds the same card ids.
        try:
            ids = set(json.loads((STATE / "collection.json").read_text("utf-8"))["cards"])
            source = "state/collection.json"
        except (OSError, ValueError, KeyError, TypeError):
            return [f"card_params.json unavailable ({params.error}) and no collection -- not merging"], True

    missing = sorted(set(ids) - set(have))
    # Only cards the catalogue knows: an owned id that data/cards.json lacks makes the app
    # refuse to load ("_LoadFailure: 3"). Such a card is held back until the catalogue has it.
    catalogue = catalogue_records()
    later = [cid for cid in missing if catalogue is not None and cid not in catalogue]
    missing = [cid for cid in missing if cid not in later]
    held_back = [
        f"   held back until the card catalogue has it (update the app's data): {cid}" for cid in later
    ]
    original = APPEND_ONLY_OWNED.read_bytes()
    raw = json.loads(original.decode("utf-8"))
    if _owned_bytes(raw) != original:
        return [
            "data/owned.json is not in this tool's own format -- refusing to write it; merge by hand"
        ], True
    if not missing:
        return (
            [
                "data/owned.json already has every card the account owns"
                + (" that the catalogue knows" if held_back else "")
            ]
            + held_back
        ), False

    backup = DUMPS / "owned_before_merge.json"
    if not backup.exists():
        atomic_io.write_bytes(backup, original)
    raw["owned"] = sorted(set(have) | set(missing))
    atomic_io.write_bytes(APPEND_ONLY_OWNED, _owned_bytes(raw))
    return (
        [
            f"data/owned.json: {'CREATED and ' if created else ''}APPENDED {len(missing)} "
            f"card(s) the account owns (ids from {source})"
        ]
        + [f"   + {cid}" for cid in missing]
        + [
            f"   backup of the previous file: {backup}"
            if not backup.exists()
            else f"   first-run backup kept at {backup}"
        ]
        + held_back,
        False,
    )


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

    Whole records, not a digest: the guard must tell an append from a modified record.
    """
    try:
        doc = json.loads(APPEND_ONLY.read_bytes().decode("utf-8"))
        return {c["id"]: c for c in doc["cards"]}
    except Exception:  # noqa: BLE001 -- unreadable is a state, not a crash
        return None


def catalogue_guard(before: dict[str, Any] | None) -> tuple[list[str], bool]:
    """Enforce append-only on `data/cards.json`, by comparing records.

    Only whole new ids may appear. A modified or vanished record, or an unparseable file, is
    a failure, and each offending card is named.
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
    policy: str,
) -> tuple[list[str], bool]:
    if not live.exists or not hand.exists:
        return [f"{live.label} / {hand.label}: one is missing -- cannot compare"], False
    if live.error or hand.error:
        return [f"{live.label} / {hand.label}: {live.error or hand.error}"], False

    live_ids, hand_ids = live.ids["card"], hand.ids["card"]
    missing, extra = live_ids - hand_ids, hand_ids - live_ids

    lines = [f"{live.label} {len(live_ids)} {live_kind} | {hand.label} {len(hand_ids)} {hand_kind}"]
    if missing:
        lines.append("")
        lines.append(f"  ** {len(missing)} card(s) in {live.label} and NOT in {hand.label} **")
        for cid in sorted(missing):
            lines.append(f"     + {cid}")
        lines.append(f"  {hand.label} {policy}")
        lines.append(f"  {hint}")
    if extra:
        lines.append(f"  note: {len(extra)} id(s) in {hand.label} are not in {live.label}: {_fmt_ids(extra)}")
    if not missing and not extra:
        lines.append("  in sync.")
    return lines, bool(missing)


def hand_merge_report(account: str = "main") -> tuple[list[str], bool]:
    """Everything the derived artefacts know that the hand-edited files do not.

    data/cards.json  -- the catalogue, against cards_enriched.json (the game's Card
                        master). A gap surviving `add_cards` means it refused the card
                        and said why, or the step was skipped.
    data/owned.json  -- the collection, against card_params.json (the account's live
                        Card/GetParameters). A gap means `merge_owned` has not run.
    """
    lines, need_a = _compare(
        probe_cards_enriched(),
        probe_cards_json(),
        "ids",
        "ids",
        "Run the add_cards step, or add by hand if it refused them by name above.",
        policy="is the game's catalogue: the refresh may add a card, never modify or remove one.",
    )
    lines.append("")
    more, need_b = _compare(
        probe_card_params(),
        probe_owned_json(),
        "captured",
        "owned",
        "The merge_owned step appends them (append-only, guarded by owned_guard).",
        policy="is the account itself: the refresh may append a card, never modify or remove one.",
    )
    lines += more

    # state/collection.json is written only on --merge-collection: report when it is behind.
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
        # A card in both at a different level: independent of the id sets, so always checked.
        drift = collection_level_drift(STATE / "collection.json", DUMPS / f"account_snapshot_{account}.json")
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
        lines.append(
            "  DERIVED FILES ARE STALE -- rebuilt by the full chain, silent if you ran a single step by hand:"
        )
        for derived, source, hours in stale:
            lines.append(f"     ~ {derived} is {hours:.1f} h older than {source}")
    else:
        lines.append("  derived files are newer than their sources.")

    # The song chain has no authority file to compare against: report the cache staleness.
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
        # (bloom 5 on a 4/5-star = level 2). With 10 bloom-5 socket cards, "1" reads Overall
        # Power 335,354 / board 93,999 against the game's 335,758 / 94,389; "auto" matches.
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
    ap.add_argument("--verbose", action="store_true", help="print full output of every step")
    args = ap.parse_args(argv)

    # Print in UTF-8 whatever the console/pipe claims to be: redirected, stdout can be cp1252,
    # and one U+FFFD in a step's output would kill the refresh with UnicodeEncodeError.
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
    busy = pictures_busy()  # another refresh is fetching pictures: leave them to it
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
        if step.name == "master" and sys.platform != "win32":
            return False, "its runner (hololive-dreams-master's run-local.ps1) is Windows-only"
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

    # In maintenance the game refuses every call. One anonymous Master/Get first: if it says
    # maintenance, nothing runs and the exit code (EXIT_MAINTENANCE) lets the page say so.
    if any(selected(s)[0] and s.network for s in STEPS):
        import account_snapshot  # noqa: PLC0415

        if account_snapshot.in_maintenance(args.account):
            print("\nGAME IN MAINTENANCE -- the server refuses every call (IN_MAINTENANCE).")
            print("Nothing was refreshed and nothing was written; refresh again once it ends.")
            return EXIT_MAINTENANCE

    # Without the client's payload seed every API step would fail: say so once and run
    # nothing. (The maintenance probe needs no seed; without one it answers "unknown".)
    if missing_keys(s.name for s in STEPS if selected(s)[0]):
        print("\nTHE GAME CLIENT'S BUILD CONSTANTS ARE NOT ON THIS PC -- the game's API cannot be read.")
        print("Find them in your own game install (one anonymous request, nothing printed):")
        print("    python tools/refresh/game_keys.py --find")
        print("refresh.cmd / refresh.sh does this by itself; docs/dev/game-keys.md explains it. Nothing ran.")
        return EXIT_NO_KEYS

    if any(selected(s)[0] for s in STEPS if s.name in PICTURE_STEPS):
        hold_pictures_lock()

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
        # A step whose working directory is absent (the `master` repo not cloned here) is
        # skipped like a deselected one: a missing cwd would raise and end the whole refresh.
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

    # ---- the append-only files may only have grown ---------------------
    print()
    print("=" * 74)
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
        "CATALOGUE AND COLLECTION CHECK  (both data/cards.json and data/owned.json are APPEND-ONLY:"
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
        print("all steps ok, but a card the account or the game has is missing (see above)")
    else:
        print("all steps ok")
    print(f"({time.time() - started:.0f}s total)")
    if deferred:
        print(start_pictures(_pyexe(args), deferred, args.offline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
