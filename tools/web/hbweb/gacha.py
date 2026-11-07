"""The gacha tracker: `GET /api/gacha` (the view over data/gacha/*.jsonl),
`GET /gachaart/<gacha_id>/<kind>` (banner art), `POST /api/gacha/sync` (pull the
account's history) and `POST /api/gacha/export` (one self-contained HTML file).

The store is written only by `gacha_sync.py` (append-only), in a child process;
this module reads it.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import gacha_view

from hbweb import config, scripts, static

_GACHA_ID_RE = re.compile(r"^gacha-[0-9a-z_-]+$")
# `<kind>` (extension-less, what the page asks for), `<kind>.png` (the committed masters)
# and `<kind>.webp` (what gacha_art saves). The route serves whichever
# file is actually there, so the page never has to know which format a banner was saved in.
ART_KINDS = ("banner", "logo", "icon")
ART_EXTS = ("png", "webp")
# The sync SPENDS the account credential (a login). Two in flight would double-log-in and
# race two appenders on the same append-only store, so the second is refused, not queued:
# the user pressed a button and deserves an answer, not a silent wait behind a 60 s login.
SYNC_LOCK = threading.Lock()


def parse_sync_summary(stdout: str) -> dict | None:
    """The last `SUMMARY {json}` line of the sync's output (a rerun inside one
    subprocess would otherwise hand back a stale summary)."""
    last = None
    for line in stdout.splitlines():
        if line.startswith("SUMMARY "):
            try:
                last = json.loads(line[len("SUMMARY "):])
            except ValueError:
                pass
    return last


def parse_wrote(stdout: str) -> str | None:
    """The last `WROTE <path>` line's remainder -- the export's own report of where it
    landed. Last, not first, for the same reason as SUMMARY: a rerun inside one
    subprocess would otherwise hand back a stale path."""
    last = None
    for line in stdout.splitlines():
        if line.startswith("WROTE "):
            last = line[len("WROTE "):].strip() or None
    return last


def serve_gacha(h) -> None:
    """`GET /api/gacha`: the tracker's view of the store (`gacha_view.build_view`)."""
    cards_doc = config.read_json(config.CARDS_PATH) or {}
    cards_by_id = {c["id"]: c for c in (cards_doc.get("cards") or []) if isinstance(c, dict) and "id" in c}
    store = gacha_view.load_store(config.GACHA_STORE_DIR)
    # The account snapshot ("Refresh account") is optional: without it the view's
    # `tutorial` block is null rather than guessed.
    snapshot = config.read_json(config.SNAPSHOT_PATH)
    view = gacha_view.build_view(store, cards_by_id, config.GACHA_ART_DIR, snapshot=snapshot)
    # art the app has not downloaded yet but the server holds counts as there
    for b in view.get("banners") or []:
        for k, e in (b.get("art_ext") or {}).items():
            if e is None and b.get("gacha_id"):
                e = next((x for x in ART_EXTS if static.asset_known(
                    config.GACHA_ART_DIR, config.GACHA_ART_DIR / b["gacha_id"] / f"{k}.{x}")), None)
                b["art_ext"][k] = e
                b.setdefault("art", {})[k] = e is not None
    view["store_dir"] = str(config.GACHA_STORE_DIR)
    h.reply_json(200, view)


def _art_file(h, gacha_id: str, kind: str, wanted: list[str]) -> Path | None:
    """The banner file on disk, else the server's copy as a cached Path. With the app's
    cache off the server's bytes are sent here and the answer is None (nothing left to do)."""
    art_dir = config.GACHA_ART_DIR
    path = next((p for p in (art_dir / gacha_id / f"{kind}.{e}" for e in wanted)
                 if p.is_file()), art_dir / gacha_id / f"{kind}.{wanted[0]}")
    if not path.is_file():
        # the desktop app has no game install to extract it from: the server's copy
        for p in (art_dir / gacha_id / f"{kind}.{e}" for e in wanted):
            got = static.asset(art_dir, p)
            if isinstance(got, Path):
                path = got
                break
            if got:
                h.reply(200, got, "image/png" if got[:4] == b"\x89PNG" else "image/webp")
                return None
    return path


def serve_gachaart(h, rest: str) -> None:
    """`GET /gachaart/<gacha_id>/<kind>[.png|.webp][?w=N]`: a banner's art, downscaled on
    request. `banner`, `banner.png` and `banner.webp` are all accepted; the extension-less
    form is what the page and the export ask for: it works whether a banner was saved as
    a PNG master or as a WEBP."""
    parts = rest.split("/")
    if len(parts) != 2 or not _GACHA_ID_RE.match(parts[0]):
        h.reply(400, b"bad gacha art path", "text/plain; charset=utf-8")
        return
    kind, _, ext = parts[1].partition(".")
    if kind not in ART_KINDS or (ext and ext not in ART_EXTS):
        h.reply(400, b"bad gacha art path", "text/plain; charset=utf-8")
        return
    path = _art_file(h, parts[0], kind, [ext] if ext else list(ART_EXTS))
    if path is None:
        return
    if not path.is_file() or (config.GACHA_ART_DIR.resolve() not in path.resolve().parents
                              and static.CONTENT.store.resolve() not in path.resolve().parents):
        h.reply(404, b"no gacha art", "text/plain; charset=utf-8")
        return
    # `?w=N` downscales (LANCZOS -> WEBP, cached) exactly as `/cardart/` does. The
    # masters are 2048x1024 PNGs of 2-3 MB; a 320 px gallery card that downloads the
    # master and lets the browser shrink it comes out blurred. A width
    # at or above the master's own returns the master, so the mime follows the BYTES
    # (`scaled_art` only re-encodes when it actually resized), never the request.
    width = h.query_width()
    body = static.scaled_art(path, width) if width else None
    if not body:
        body = path.read_bytes()
    if not body:
        h.reply(404, b"no gacha art", "text/plain; charset=utf-8")
        return
    ctype = "image/png" if body[:8] == b"\x89PNG\r\n\x1a\n" else "image/webp"
    h.reply(200, body, ctype, cache="max-age=3600")


def gacha_sync(h) -> None:
    """`POST /api/gacha/sync`: the page's sync button. Spends the account credential (a login).

    One at a time (`SYNC_LOCK`): this is a threading server, and a double click
    or a second tab would otherwise log in twice and run two appenders over the same
    store. The second caller is REFUSED (409) rather than queued -- gacha.js prints the
    `error` field, so the user is told why instead of watching a button hang."""
    if not SYNC_LOCK.acquire(blocking=False):
        h.reply_json(409, {"error": "a gacha sync is already running"})
        return
    try:
        ran = scripts.run_recon_script(h, config.GACHA_SYNC_SCRIPT, "gacha")
        if ran is None:
            return
        code, out = ran
        h.reply_json(200, {"returncode": code, "stdout": out[-20000:],
                           "summary": parse_sync_summary(out)})
    finally:
        SYNC_LOCK.release()


def sync_after_refresh(h, refresh_code: int) -> dict | None:
    """The gacha block of `POST /api/refresh`: a refresh also syncs the pulls, because
    Gacha/ListHistory serves at most the newest 100 entries and every refresh without a
    sync is a chance for pulls to age out of that window unrecorded. Same lock as the
    button: if it is mid-sync the refresh reports that instead of queueing behind it.
    None when the sync script could not start (500 already answered)."""
    if refresh_code == 3:   # refresh_all.EXIT_MAINTENANCE: the game refuses every call, so would this
        return {"error": "the game is in maintenance; pulls not synced this refresh"}
    if refresh_code == 4:   # refresh_all.EXIT_NO_KEYS: the API cannot be read on this PC yet
        return {"error": "the game client's build constants are not on this PC; pulls not synced"}
    if not SYNC_LOCK.acquire(blocking=False):
        return {"error": "a gacha sync is already running; pulls not synced this refresh"}
    try:
        ran = scripts.run_recon_script(h, config.GACHA_SYNC_SCRIPT, "gacha")
    finally:
        SYNC_LOCK.release()
    if ran is None:
        return None
    gcode, gout = ran
    return {"returncode": gcode, "summary": parse_sync_summary(gout)}


def gacha_export(h) -> None:
    """`POST /api/gacha/export`: one self-contained HTML file under dumps/. No login: it
    reads the store."""
    ran = scripts.run_recon_script(h, config.GACHA_EXPORT_SCRIPT, "gacha-export")
    if ran is None:
        return
    code, out = ran
    h.reply_json(200, {"returncode": code, "stdout": out[-20000:], "path": parse_wrote(out)})
