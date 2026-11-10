"""Pages and assets: the redesign's pages (`/<name>` -> `static/<name>.html`), their
ES modules and stylesheets, and every image, figure and font they draw.

Each asset route is guarded twice: a strict name pattern (no `..`, no separators, no `.`
but the extension) AND the resolved path must stay under its folder. A missing file is a
404 and the page falls back to its named placeholder; never a substitute, because a wrong
badge or glyph is a factual claim about the game.

The installed app keeps the Live2D figures, chibis and card art on api.holobuilder.dev
and fetches each the first time a page asks (`tools/app/content_store.py`); on a PC with
every file local, `asset()` returns it and no request leaves the machine. The store
writes its cache itself: this package opens nothing for writing.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote

from hbuilder import paths
from hbweb import config

sys.path.insert(0, str(paths.APP_DIR))
import content_store as _content_store  # noqa: E402

if TYPE_CHECKING:
    from hbweb.router import Handler

CONTENT = _content_store.ContentStore()
LIVE2D_CORE_URL = "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js"

PAGE_RE = re.compile(r"/[a-z][a-z-]{0,40}")  # a redesign page: /<name> -> static/<name>.html
_ART_NAME_RE = re.compile(r"^card-[0-9A-Za-z_-]+\.webp$")
# A music id is `m` + digits in every chart on disk; this allowlist is also the
# path-traversal guard for the jacket route (no dots, no slashes).
_MUSIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_EVENT_ART_RE = re.compile(r"^[A-Za-z0-9_-]+\.webp$")
# Node glyphs. The names come from `skilltree.json`'s own `e.icon`.
#
# ⚠ The HYPHEN is required: three Y-branch assets are suffixed by song scope
# (`…musicskilltreecharacterandmusicsingertype-solo` / `-group` / `-all`) and a
# `[a-z0-9_]+` pattern would 400 every one of them — **18 nodes**, the whole
# score-bonus family, silently glyph-less.
_ICON_NAME_RE = re.compile(r"^[a-z0-9_-]+\.png$")
# `<subdir>/<file>.png`, both from the same restricted alphabet. One level only:
# `card_chrome/` is exactly two deep and nothing legitimate needs to go further.
_CHROME_NAME_RE = re.compile(r"^[a-z0-9_]+/[a-z0-9_]+\.png$")
# `<category>/<sprite>.(png|webp)`: the category is a folder of `ui_official/` (letters
# and `_`), the file the game's own sprite name (item thumbs carry hyphens). `views/`
# holds node-tree text dumps, not images, and the extension rule keeps it out.
_UI_NAME_RE = re.compile(r"^[a-z_]+/[a-z0-9_-]+\.(png|webp)$")
_LIVE2D_PATH_RE = re.compile(
    r"^(models/[0-9]{5}-[a-z]+-[0-9]{4}-[0-9]{2}/[a-z0-9_]+\.(json|moc3|webp|png)"
    r"|motions/live2d_mot_[a-z0-9_-]+\.motion3\.json"
    r"|expressions/live2d_exp_[a-z0-9_-]+\.exp3\.json)$"
)
_LIVE2D_TYPES = {
    "json": "application/json",
    "moc3": "application/octet-stream",
    "webp": "image/webp",
    "png": "image/png",
}
_CHIBI_PATH_RE = re.compile(r"^models/sdcos-[0-9]{5}-[a-z]+-[0-9]{4}-[0-9]{2}\.glb$")
_CARD_ART_FAMILIES = {"full": "img_card_full", "vert": "img_card_vert", "thumb": "img_card_thumb"}
_NOT_FOUND = b"not found"
_TEXT = "text/plain; charset=utf-8"


def asset(base: Path, path: Path) -> Path | bytes | None:
    """`path`, which must stay under `base`: the local file, else the server's copy (a cached
    Path, or the bytes when the cache is off), else None. A path escaping `base` is None."""
    try:
        if base.resolve() not in path.resolve().parents:
            return None
    except OSError:
        return None
    if path.is_file():
        return path
    try:
        rel = path.relative_to(config.REPO_ROOT).as_posix()
    except ValueError:
        return None  # outside this checkout (another --art-dir): local only
    return CONTENT.lazy(rel)


def asset_known(base: Path, path: Path) -> bool:
    """Whether a page may ask for `path`: on disk, or on the server. Never downloads."""
    if path.is_file():
        return True
    try:
        return CONTENT.entry(path.relative_to(config.REPO_ROOT).as_posix()) is not None
    except ValueError:
        return False


def asset_bytes(base: Path, path: Path) -> bytes | None:
    got = asset(base, path)
    return got.read_bytes() if isinstance(got, Path) else got


# In-memory cache of downscaled art. The masters on disk stay at NATIVE resolution
# (lossless), which is the right archive; the page asks for the exact size it shows
# via `?w=`, so it never downloads a 2 MB texture to paint a 56 px thumbnail. Keyed
# by (path, mtime, width) so a re-render invalidates it.
_ART_SCALE_CACHE: dict = {}


def scaled_art(path: Path, width: int) -> bytes | None:
    """The bytes of `path`, downscaled to `width` px wide (aspect kept) and cached.
    `width <= 0`, an unreadable master, or a master already narrower returns the
    master untouched — a resize is never an upscale and never a failure path."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, width)
    cached = _ART_SCALE_CACHE.get(key)
    if cached is not None:
        return cached
    raw = path.read_bytes()
    body = raw
    if width > 0:
        try:
            import io  # noqa: PLC0415

            from PIL import Image  # noqa: PLC0415 — Pillow is in requirements.txt

            with Image.open(io.BytesIO(raw)) as im:
                if im.width > width:
                    height = max(1, round(im.height * width / im.width))
                    im = im.convert("RGBA").resize((width, height), Image.LANCZOS)  # type: ignore[assignment,attr-defined]
                    buf = io.BytesIO()
                    im.save(buf, format="WEBP", quality=90, method=4)
                    body = buf.getvalue()
        except Exception:  # noqa: BLE001 — any failure serves the master
            body = raw
    if len(_ART_SCALE_CACHE) > 3000:
        _ART_SCALE_CACHE.clear()
    _ART_SCALE_CACHE[key] = body
    return body


_ITEM_ICON_MAP: dict = {"mtime": None, "map": {}}


def item_icon_map() -> dict[str, str]:
    """`item_id -> icon asset name` from `item_icons.py`'s index.json (`by_item`,
    master `Item` field 8). Reloaded when the file changes; `{}` when absent — the
    filename conventions in `serve_itemicon` still apply."""
    path = config.ITEM_ICONS_DIR / "index.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _ITEM_ICON_MAP["mtime"] != mtime:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            _ITEM_ICON_MAP["map"] = dict(raw.get("by_item") or {})
            _ITEM_ICON_MAP["mtime"] = mtime
        except (OSError, ValueError):
            return _ITEM_ICON_MAP["map"]
    return _ITEM_ICON_MAP["map"]


def live2d_core() -> bool:
    """True once the Cubism Core is on this PC: the app's file cache downloads it the first
    time from Live2D's own CDN, so the holobuilder server never redistributes it
    (`content_store.fetch_live2d_core`)."""
    return _content_store.fetch_live2d_core(config.LIVE2D_CORE, LIVE2D_CORE_URL)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def serve_static(h: Handler, name: str) -> None:
    """A file of `static/`, re-checked for containment."""
    path = (config.STATIC_DIR / name).resolve()
    if not str(path).startswith(str(config.STATIC_DIR.resolve())) or not path.is_file():
        h.reply(404, _NOT_FOUND, _TEXT)
        return
    ctype = {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".woff2": "font/woff2",
    }.get(path.suffix, "application/octet-stream")
    h.reply(200, path.read_bytes(), ctype)


def is_module_path(path: str) -> bool:
    """The pages are ES modules that import each other, so `.js` / `.css` are routed by
    extension rather than by an allowlist a new module would fall off. One level deep,
    no `..`, and `serve_static` re-checks containment."""
    return path.endswith((".js", ".css")) and path.count("/") == 1 and ".." not in path


def is_page(path: str) -> bool:
    """A redesign page (`static/<name>.html`): lowercase letters and dashes only, one
    level, so a new page needs no server change. The router checks it last, so no API
    route can be shadowed."""
    return bool(PAGE_RE.fullmatch(path)) and (config.STATIC_DIR / f"{path[1:]}.html").is_file()


def redirect_home(h: Handler) -> None:
    """`GET /` -> the Optimizer."""
    h.send_response(302)
    h.send_header("Location", "/optimizer")
    h.send_header("Content-Length", "0")
    h.send_header("Cache-Control", "no-store")
    h.end_headers()


# ---------------------------------------------------------------------------
# Figures: the leaders' Live2D outfits and the 3D chibis
# ---------------------------------------------------------------------------
def serve_live2d_index(h: Handler) -> None:
    """`GET /api/live2d/index` -> `{available, core, by_card: {card id: asset},
    models: {asset: {model, motion, expression}}}`: which leader cards have a
    converted Live2D figure, so the page never asks for one that is not there."""
    index = config.read_json(config.LIVE2D_DIR / "index.json") or {}
    models = {
        a: {"model": f"/live2d/{m['model']}", "motion": m.get("motion"), "expression": m.get("expression")}
        for a, m in (index.get("models") or {}).items()
    }
    core = bool(models) and live2d_core()
    h.reply_json(
        200,
        {
            "available": core,
            "core": core,
            "by_card": index.get("by_card") or {},
            "models": models,
            "written": index.get("written"),
        },
    )


def serve_live2d_core(h: Handler) -> None:
    """`GET /live2d/core.js`: the Cubism Core runtime, fetched once per PC."""
    live2d_core()
    _send_asset(
        h,
        config.LIVE2D_CORE.parent,
        config.LIVE2D_CORE,
        "text/javascript; charset=utf-8",
        b"no such live2d file",
    )


def serve_live2d_file(h: Handler, rel: str) -> None:
    """`GET /live2d/<rel>`: a converted Live2D file under `LIVE2D_DIR`."""
    if not _LIVE2D_PATH_RE.match(rel):
        h.reply(400, b"bad live2d path", _TEXT)
        return
    _send_asset(
        h,
        config.LIVE2D_DIR,
        config.LIVE2D_DIR / rel,
        _LIVE2D_TYPES[rel.rsplit(".", 1)[1]],
        b"no such live2d file",
    )


def serve_chibi_index(h: Handler) -> None:
    """`GET /api/chibi/index` -> `{available, outfits: {SdCostume id: {chr, name, typ,
    model}}}`: the converted 3D chibis, so the page only ever asks for one that exists."""
    chibi_dir = config.CHIBI_DIR
    index = config.read_json(chibi_dir / "index.json") or {}
    outfits = {
        k: {**{f: v.get(f) for f in ("chr", "name", "typ")}, "model": f"/chibi/{v['model']}"}
        for k, v in (index.get("outfits") or {}).items()
        if v.get("model") and asset_known(chibi_dir, chibi_dir / v["model"])
    }
    h.reply_json(200, {"available": bool(outfits), "outfits": outfits})


def serve_chibi_file(h: Handler, rel: str) -> None:
    """`GET /chibi/<rel>`: a converted chibi `.glb` under `CHIBI_DIR`."""
    if not _CHIBI_PATH_RE.match(rel):
        h.reply(400, b"bad chibi path", _TEXT)
        return
    _send_asset(h, config.CHIBI_DIR, config.CHIBI_DIR / rel, "model/gltf-binary", b"no such chibi file")


def _send_asset(h: Handler, base: Path, path: Path, ctype: str, missing: bytes) -> None:
    """`path` (local, or the server's copy) with an hour of browser cache, else a 404."""
    body = asset_bytes(base, path)
    if body is None:
        h.reply(404, missing, _TEXT)
        return
    h.reply(200, body, ctype, cache="max-age=3600")


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------
def _served_file(base: Path, path: Path) -> bytes | None:
    """The bytes of `path` when it is a file under `base`, else None."""
    if not path.is_file() or base.resolve() not in path.resolve().parents:
        return None
    return path.read_bytes()


def serve_jacket(h: Handler, rest: str) -> None:
    """`GET /jacket/<music id>[?w=N]`: a song's jacket from
    dumps/song_jackets/img_music_jacket_<id>.webp. Absent is a 404 -- the picker shows
    the title alone. A truncated (0-byte) master is a 404 too: served as a 200 with an
    empty body it reads to the page as a broken image with no cause named."""
    music_id = rest.split("?", 1)[0].strip("/")
    if not _MUSIC_ID_RE.match(music_id or ""):
        h.reply(404, b"bad id", _TEXT)
        return
    jackets = config.JACKETS_DIR
    path = jackets / f"img_music_jacket_{music_id}.webp"
    if not path.is_file() or jackets.resolve() not in path.resolve().parents:
        h.reply(404, b"no jacket", _TEXT)
        return
    width = h.query_width()
    body = scaled_art(path, width) if width else path.read_bytes()
    if not body:
        body = path.read_bytes()
    if not body:
        h.reply(404, b"no jacket", _TEXT)
        return
    h.reply(200, body, "image/webp")


def serve_eventart(h: Handler, rest: str) -> None:
    """`GET /eventart/<name>.webp[?w=N]`: an event banner / logo from dumps/event_art/
    (`event_art.py`). Absent is a 404 -- the page keeps its placeholder."""
    name = rest.split("?", 1)[0].strip("/")
    if not _EVENT_ART_RE.match(name or ""):
        h.reply(404, b"bad name", _TEXT)
        return
    art_dir = config.EVENT_ART_DIR
    path = art_dir / name
    if not path.is_file() or art_dir.resolve() not in path.resolve().parents:
        h.reply(404, b"no event art", _TEXT)
        return
    width = h.query_width()
    body = (scaled_art(path, width) if width else None) or path.read_bytes()
    if not body:
        h.reply(404, b"no event art", _TEXT)
        return
    h.reply(200, body, "image/webp")


def serve_cardart(h: Handler, rest: str) -> None:
    """`GET /cardart/[full|vert|thumb/]<card-id>[?w=N]`: card art from
    `dumps/card_images/img_card_<family>_<key>.webp`, at NATIVE resolution on disk.

    No family is the FULL art (2048x1024 landscape); `vert` is the portrait (the
    ranking face), `thumb` the square (the tree sockets). `?w=N` downscales to N px
    wide (cached). A miss is a 404 and the caller falls back (name / gradient / dim
    socket), and so is a 0-byte master in the dump (a truncated write)."""
    parts = rest.split("/")
    if len(parts) == 2 and parts[0] in _CARD_ART_FAMILIES:
        family, card_id = parts
    else:
        family, card_id = "full", parts[-1]
    if not _ART_NAME_RE.match(card_id + ".webp"):
        h.reply(400, b"bad card id", _TEXT)
        return
    key = card_id[len("card-") :] if card_id.startswith("card-") else card_id
    images = config.CARD_IMAGES_DIR
    got = asset(images, images / f"{_CARD_ART_FAMILIES[family]}_{key}.webp")
    if got is None:
        # `dumps/card_images/` is filled from the game's own on-disk cache and
        # can predate the newest cards; the refresh puts THEIR art in the
        # explorer art dir (`card_art_sync.py`: `<id>.webp` is the same
        # `img_card_vert`, `<id>.sq.webp` the same thumb), so look there too.
        art_dir = Path(h.art_dir)
        got = asset(art_dir, art_dir / (f"{card_id}.sq.webp" if family == "thumb" else f"{card_id}.webp"))
        if got is None:
            h.reply(404, b"no card art", _TEXT)
            return
    # (bytes = the app's asset cache is off: served unscaled rather than not at all)
    body = scaled_art(got, h.query_width()) if isinstance(got, Path) else got
    if not body:
        h.reply(404, b"no card art", _TEXT)
        return
    h.reply(200, body, "image/webp", cache="max-age=3600")


def serve_itemicon(h: Handler, key: str) -> None:
    """`GET /itemicon/<item_id>`: the game's item icon (Resources view). Tries the
    item_id filename first, then the game's own item->asset map (master `Item` field 8,
    where reward boxes and friends name their icon), then the octo asset-name form
    (`img_item_thumb_<id>` with `item-` dropped and `_`->`-`). A miss is a 404 — the
    view falls back to a coloured glyph."""
    key = unquote(key)
    # item_ids are [A-Za-z0-9_-] only; a dot or slash is a path-escape attempt.
    if not re.match(r"^[0-9A-Za-z_-]+$", key):
        h.reply(400, b"bad item id", _TEXT)
        return
    icons = config.ITEM_ICONS_DIR
    candidates = [icons / f"{key}.webp"]
    mapped = item_icon_map().get(key)
    if mapped:
        candidates.append(icons / f"{mapped}.webp")
    if key.startswith("item-"):
        candidates.append(icons / f"img_item_thumb_{key[len('item-') :].replace('_', '-')}.webp")
    for path in candidates:
        body = _served_file(icons, path)
        if body is not None:
            h.reply(200, body, "image/webp", cache="max-age=3600")
            return
    h.reply(404, b"no item icon", _TEXT)


def serve_chricon(h: Handler, chr_id: str) -> None:
    """`GET /chricon/chr-XXXXX`: a character's round face icon. Prefers the game's
    512px `img_chr_icon_mini_<num>.webp` (once rendered into card_images), else
    the 256px `member_icons/<chr>.png`. A miss is a 404 — the caller shows the
    name alone."""
    if not re.match(r"^chr-[0-9A-Za-z_-]+$", chr_id):
        h.reply(400, b"bad chr id", _TEXT)
        return
    num = chr_id.split("-", 1)[1]
    images = config.CARD_IMAGES_DIR
    mini = asset_bytes(images, images / f"img_chr_icon_mini_{num}.webp")
    png = config.MEMBER_ICONS_DIR / f"{chr_id}.png"
    if mini:
        body, ctype = mini, "image/webp"
    elif png.is_file() and config.MEMBER_ICONS_DIR.resolve() in png.resolve().parents:
        body, ctype = png.read_bytes(), "image/png"
    else:
        h.reply(404, b"no chr icon", _TEXT)
        return
    h.reply(200, body, ctype, cache="max-age=3600")


def serve_icon(h: Handler, name: str) -> None:
    """`GET /icon/<name>.png`: a skill-tree node glyph, from `dumps/skill_icons/` in place.

    The mapping from a node's `e.icon` to a file is the identity plus `.png`. There are
    22 distinct names across the 152 nodes; 21 resolve, and exactly one
    (`img_skill_icon_tree_livedeckleaderactiveskilladdition`, used by R-024
    and R-027) is in the decrypted catalogue but is not extracted. A placeholder glyph
    would put a WRONG symbol on those two real nodes, so a miss is a 404."""
    if not _ICON_NAME_RE.match(name):
        h.reply(400, b"bad icon name", _TEXT)
        return
    body = _served_file(config.ICONS_DIR, config.ICONS_DIR / name)
    if body is None:
        h.reply(404, b"no icon", _TEXT)
        return
    h.reply(200, body, "image/png", cache="max-age=3600")


def serve_chrome(h: Handler, name: str) -> None:
    """`GET /chrome/<kind>/<file>.png`: one card-chrome sprite, from `dumps/card_chrome/`.
    A wrong element badge or rarity band is a factual claim about the card, so a miss
    is a 404 and the face draws without that piece."""
    if not _CHROME_NAME_RE.match(name):
        h.reply(400, b"bad chrome name", _TEXT)
        return
    body = _served_file(config.CHROME_DIR, config.CHROME_DIR / name)
    if body is None:
        h.reply(404, b"no chrome", _TEXT)
        return
    h.reply(200, body, "image/png", cache="max-age=86400")


def serve_ui_sprite(h: Handler, name: str) -> None:
    """`GET /ui/<category>/<sprite>.(png|webp)`: one official UI sprite, from
    `dumps/ui_official/`. A wrong grade letter or skill plate is a factual claim about
    the game, so a miss is a 404 and the page draws without it."""
    if not _UI_NAME_RE.match(name):
        h.reply(400, b"bad ui sprite name", _TEXT)
        return
    body = _served_file(config.UI_OFFICIAL_DIR, config.UI_OFFICIAL_DIR / name)
    if body is None:
        h.reply(404, b"no ui sprite", _TEXT)
        return
    h.reply(200, body, "image/webp" if name.endswith(".webp") else "image/png", cache="max-age=86400")


def serve_font(h: Handler) -> None:
    """`GET /font/line-seed-sans-xbd-latin.woff2`: the game's own face. One fixed path, so
    no traversal surface. A missing font is a 404 and `font-display:swap` keeps the
    fallback stack: it must degrade, never block paint."""
    if not config.FONT_PATH.is_file():
        h.reply(404, b"no font", _TEXT)
        return
    h.reply(200, config.FONT_PATH.read_bytes(), "font/woff2", cache="max-age=86400")
