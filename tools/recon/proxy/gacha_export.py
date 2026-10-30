"""One self-contained HTML export of the gacha tracker.

    proxyenv/Scripts/python gacha_export.py            # -> dumps/gacha_export_<stamp>.html

Reads the append-only store (data/gacha/*.jsonl) through the WEB layer's own
aggregator, `tools/web/gacha_view.py`, imported here by path: the export and the
Gacha tab must show the same numbers, and the only way to guarantee that is to
run the same code rather than a second copy of the arithmetic.

SELF-CONTAINED IS THE POINT. The file is meant to survive being mailed, copied to
a phone, or opened years from now with this repo gone:
  * banner art (`data/gacha/art/<id>/banner.png`) and card thumbs
    (`dumps/card_images/img_card_thumb_<key>.webp`) are embedded as base64 `data:`
    URIs -- a missing thumb degrades to the card id as text, never a broken image;
  * the banner heroes are DOWNSCALED to <=1200px wide and re-encoded WEBP first
    (Pillow, the same LANCZOS->WEBP path `tools/web/server.py::_scaled_art` uses),
    because a raw 2833x1417 PNG costs ~3.8 MB of base64 EACH and the file size is
    otherwise unbounded in the number of banners the store has ever seen. Any
    failure -- Pillow absent, a file Pillow cannot decode -- embeds the raw PNG;
  * the two charts are inline SVG, drawn server-side, so nothing has to run;
  * the stylesheet is inline; there is no `http(s)://` in any `src`/`href`;
  * every string that came from the store goes through `html.escape`.

WRITES: only `dumps/gacha_export_<YYYYmmdd_HHMMSS>.html` -- a fresh timestamped name
each run, opened "wb". This is NOT the append-only store and nothing here touches it.
The web server never writes; it shells out to this file and parses the `WROTE <path>`
line this prints last.
"""
from __future__ import annotations

import base64
import datetime as _dt
import html
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
STORE_DIR = REPO_ROOT / "data" / "gacha"
ART_DIR = STORE_DIR / "art"
CARDS_PATH = REPO_ROOT / "data" / "cards.json"
CARD_IMAGES_DIR = HERE / "dumps" / "card_images"
OUT_DIR = HERE / "dumps"
# The game's own star sprite, the same file the web server hands out as
# `/chrome/rarity/ui_999_img_card_star.png`. Embedded ONCE as a CSS background (see
# `star_css`), because the page prints a rarity row a few hundred times.
STAR_PNG = HERE / "dumps" / "card_chrome" / "rarity" / "ui_999_img_card_star.png"
SNAPSHOT_PATH = HERE / "dumps" / "account_snapshot_main.json"

# The Gacha tab's aggregator, by path. `tools/web` is not a package and must not be
# put on sys.path wholesale from a recon script -- one named module is the whole need.
_spec = importlib.util.spec_from_file_location(
    "gacha_view", REPO_ROOT / "tools" / "web" / "gacha_view.py")
assert _spec and _spec.loader
gacha_view = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gacha_view)

RAR = ("5★", "4★", "3★")
# The same three hues the tab uses, and for the same reason -- see the header of
# tools/web/static/gacha.js for the validator run that chose them.
COL = {"5★": "#d07e00", "4★": "#8840c6", "3★": "#0098ff"}
DORI = "#ff5c8a"

# --- small formatters ---------------------------------------------------------


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def num(n) -> str:
    return f"{n:,}" if isinstance(n, (int, float)) and not isinstance(n, bool) else "—"


def pct(a: float, b: float) -> str:
    return f"{100 * a / b:.2f}%" if b else "—"


def _secs(t):
    """The wire mixes units: the tracker's own stamps are epoch seconds, the game's
    start/end times are milliseconds. One helper, same rule as the tab."""
    if not isinstance(t, (int, float)) or isinstance(t, bool):
        return None
    return t / 1000 if abs(t) > 1e12 else t


def day(t) -> str:
    s = _secs(t)
    if s is None:
        return "—"
    return _dt.datetime.fromtimestamp(s, _dt.timezone.utc).strftime("%Y-%m-%d")


# A permanent banner is not dated. On the live wire the holodori Gacha
# (`gacha-common-normal-001`) arrives with NO start_time, NO end_time and
# `is_end_time_hidden: 1` -- that flag is the game saying "this one does not close".
# `— → —` would say nothing; "permanent" says the thing. A 0 or a sentinel end far past any
# real schedule is read the same way. An open END alone is "since <date>", because that
# banner IS running. Two nulls and no flag stay unknown: a closed banner whose dates were
# never captured must not be relabelled permanent. The tab's `period()` is the twin.
FAR_FUTURE = _dt.datetime(2100, 1, 1, tzinfo=_dt.timezone.utc).timestamp()


def period(b: dict) -> str | None:
    s, e = _secs(b.get("start_time")), _secs(b.get("end_time"))
    hidden = bool(b.get("is_end_time_hidden"))
    if s is None and e is None and not hidden:
        return None                       # simply unknown -- say nothing
    s_open = s is None or s == 0
    e_open = e is None or e == 0 or hidden or e >= FAR_FUTURE
    if s_open and e_open:
        return "permanent"
    if e_open:
        return f'since {day(b.get("start_time"))}'
    if s_open:
        return f'until {day(b.get("end_time"))}'
    return f'{day(b.get("start_time"))} → {day(b.get("end_time"))}'


def stamp(t) -> str:
    s = _secs(t)
    if s is None:
        return "never"
    return _dt.datetime.fromtimestamp(s, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _data_uri(raw: bytes | None, mime: str) -> str | None:
    if not raw:
        return None
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


# --- the compared subset ------------------------------------------------------
#
# THE DENOMINATOR, and the SERVER's answer to it. `gacha_view.build_view` splits every
# pull into covered + excluded (ticket/tutorial, bought with no rate roll) + uncovered
# (no saved rate table), so observed and expected are always the same population. This
# reads the block the tab reads rather than keeping a second copy of that arithmetic,
# which would not see the exclusion rule.


def comparison(view: dict) -> dict:
    c = dict(view.get("comparison") or {})
    return {"covered_pulls": c.get("covered_pulls", 0),
            "observed": c.get("observed_by_rarity") or {},
            "expected": c.get("expected_by_rarity") or {},
            "excluded_pulls": c.get("excluded_pulls", 0),
            "uncovered_pulls": c.get("uncovered_pulls", 0),
            "excluded": c.get("excluded") or []}


def stars(r: str) -> str:
    """N star sprites and the rarity token beside them -- the tab's rule, in HTML.
    The token stays because the sprite may be absent from this machine's dumps."""
    n = 0
    if r and r[0].isdigit():
        n = int(r[0])
    return (f'<span class="rar"><span class="stars">{"<i></i>" * n}</span>'
            f'<span class="rarn">{esc(r)}</span></span>')


def star_css(uri: str | None) -> str:
    """The sprite, embedded once. The caller hands in the data URI it already built for
    the SVG `<defs>`, so the 40 KB PNG is read and base64'd ONCE per export, not twice.
    Absent -> the `<i>` collapses and only the token shows: a missing dump degrades the
    page, it never breaks it."""
    if not uri:
        return ".stars i{display:none}"
    return (".stars{display:inline-flex;gap:1px;align-items:center}"
            f".stars i{{width:12.6px;height:12px;background:url({uri}) center/contain no-repeat}}")


# --- chart 1: observed vs expected, per rarity (bullet) -----------------------


def rates_chart(view: dict, cov: dict, star_uri: str | None) -> str:
    if not cov["covered_pulls"]:
        return ('<p class="note">No banner in the store has a saved rate table, so there is '
                'nothing to compare these pulls against.</p>')
    rows = [(r, cov["observed"].get(r, 0), cov["expected"].get(r)) for r in RAR]
    top = max([1.0] + [float(g) for _, g, _ in rows] + [float(e or 0) for _, _, e in rows]) * 1.12
    W, ROW, PADL, PADR, TOP, BAR = 540, 34, 88, 56, 8, 18
    H = TOP + len(rows) * ROW + 6
    S, GAP = 11, 1.5

    def x(v: float) -> float:
        return PADL + (W - PADL - PADR) * v / top

    parts = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" '
             f'aria-label="Pulls observed per rarity against the rate tables expectation">']
    if star_uri:
        # ONCE, in <defs>, and <use>d 12 times. Inlining the href on each <image> put
        # twelve copies of a 40 KB base64 blob in the file for one 11 px sprite.
        parts.append(f'<defs><image id="star" width="{S}" height="{S}" href="{star_uri}"/></defs>')
    for i, (r, got, exp) in enumerate(rows):
        cy = TOP + i * ROW + ROW / 2
        # The row label: the digit, then the game's star sprites (the same <image> the
        # tab draws). Without the sprite on disk the digit alone still names the row.
        n = int(r[0]) if r and r[0].isdigit() else 0
        sx = PADL - 10 - n * (S + GAP) + GAP
        parts.append(f'<text x="{sx - 6:.1f}" y="{cy + 4}" text-anchor="end" class="ax">{n}</text>')
        if star_uri:
            for k in range(n):
                parts.append(f'<use href="#star" x="{sx + k * (S + GAP):.1f}" '
                             f'y="{cy - S / 2:.1f}"/>')
        parts.append(f'<rect x="{PADL}" y="{cy - BAR / 2}" width="{W - PADL - PADR}" '
                     f'height="{BAR}" rx="4" class="track"/>')
        w = max(3 if got > 0 else 0, x(got) - PADL)
        if w > 0:
            parts.append(f'<rect x="{PADL}" y="{cy - BAR / 2}" width="{w:.1f}" height="{BAR}" '
                         f'rx="4" fill="{COL[r]}"/>')
        if exp is not None:
            tx = x(float(exp))
            parts.append(f'<line x1="{tx:.1f}" x2="{tx:.1f}" y1="{cy - BAR / 2 - 4}" '
                         f'y2="{cy + BAR / 2 + 4}" class="target"/>')
        label = str(got) if exp is None else f"{got} / {float(exp):.1f}"
        parts.append(f'<text x="{W - PADR + 8}" y="{cy + 4}" class="val">{esc(label)}</text>')
    parts.append("</svg>")

    legend = ('<div class="legend">'
              f'<span class="key"><i class="key-bar" style="background:linear-gradient(90deg,'
              f'{COL["5★"]} 0 33%,{COL["4★"]} 33% 66%,{COL["3★"]} 66% 100%)"></i>'
              "<span>observed pulls</span></span>"
              '<span class="key"><i class="key-tick"></i>'
              "<span>expected by the saved rate tables</span></span></div>")

    total = (view.get("totals") or {}).get("pulls") or 0
    cap = (f'compared: {num(cov["covered_pulls"])} pulls from banners with a saved rate table'
           + (f' · excluded: ticket/tutorial pulls ({num(cov["excluded_pulls"])})'
              if cov["excluded_pulls"] else "")
           + (f' · no saved rates: {num(cov["uncovered_pulls"])}'
              if cov["uncovered_pulls"] else ""))
    left = ""
    if cov["excluded"]:
        names = " · ".join(f'{x.get("gacha_name") or "(no name)"} ({x.get("kind")}, '
                           f'{num(x.get("pulls"))})' for x in cov["excluded"])
        left = f'<p class="cap">left out: {esc(names)}</p>'
    return (legend + "".join(parts) + f'<p class="cap">{esc(cap)}</p>' + left
            + rates_table(view, cov))


def rates_table(view: dict, cov: dict) -> str:
    total = (view.get("totals") or {}).get("pulls") or 0
    out = [f'<details class="table"><summary>Show the numbers ({num(cov["covered_pulls"])} '
           "compared pulls)</summary><table><tr><th>Rarity</th><th>Observed</th><th>Share</th>"
           "<th>Expected</th><th>Difference</th></tr>"]
    for r in RAR:
        got = cov["observed"].get(r, 0)
        exp = cov["expected"].get(r)
        diff = "—" if exp is None else ("+" if got - exp >= 0 else "") + f"{got - exp:.1f}"
        out.append(f'<tr><td class="td-r"><i class="dot" style="background:{COL[r]}"></i>'
                   f"{stars(r)}</td>"
                   f'<td class="td-n">{num(got)}</td>'
                   f'<td class="td-n">{pct(got, cov["covered_pulls"])}</td>'
                   f'<td class="td-n">{"—" if exp is None else f"{exp:.1f}"}</td>'
                   f'<td class="td-n">{esc(diff)}</td></tr>')
    out.append("</table>")
    out.append(f'<p class="cap">Observed here counts only the {num(cov["covered_pulls"])} pulls '
               "the server compared — banners with a saved rate table, minus the ticket and "
               "tutorial pulls, which were not bought with a rate roll. The store holds "
               f"{num(total)} pulls in all.</p></details>")
    return "".join(out)


# --- chart 2: pulls per day ---------------------------------------------------


def fill_days(rows: list[dict]) -> list[dict]:
    """The view sends only the days that HAVE pulls; a time axis with the empty days
    squeezed out lies about pace, so the gaps are filled and drawn as zero."""
    if not rows:
        return []
    at = {r["day"]: r for r in rows}
    fmt = "%Y-%m-%d"
    cur = _dt.datetime.strptime(rows[0]["day"], fmt).replace(tzinfo=_dt.timezone.utc)
    last = _dt.datetime.strptime(rows[-1]["day"], fmt).replace(tzinfo=_dt.timezone.utc)
    out = []
    while cur <= last and len(out) < 4000:  # a guard, not a formality: a bad date would spin
        key = cur.strftime(fmt)
        row = at.get(key) or {}
        out.append({"day": key, "pulls": row.get("pulls", 0), "five_star": row.get("five_star", 0)})
        cur += _dt.timedelta(days=1)
    return out


def timeline_chart(view: dict) -> str:
    rows = fill_days([r for r in (view.get("timeline") or []) if r.get("day")])
    if not rows:
        return '<p class="note">No dated pulls in the store yet.</p>'
    W, H, PADL, PADR, TOP, BASE = 720, 200, 34, 10, 22, 166
    top = max(1, *[r["pulls"] for r in rows]) if len(rows) > 1 else max(1, rows[0]["pulls"])
    slot = (W - PADL - PADR) / len(rows)
    bw = max(2.0, min(24.0, slot - 2))

    def y(v: float) -> float:
        return BASE - (BASE - TOP) * v / top

    parts = [f'<svg viewBox="0 0 {W} {H}" class="chart wide" role="img" '
             f'aria-label="Pulls per day, with the days that produced a five-star marked">']
    for v in (0, top):
        parts.append(f'<line x1="{PADL}" x2="{W - PADR}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>')
        parts.append(f'<text x="{PADL - 8}" y="{y(v) + 4:.1f}" text-anchor="end" class="ax">{num(v)}</text>')
    # x labels: the last day is ALWAYS labelled (the caption quotes it), so it is placed
    # first and any periodic label that would collide with it is dropped rather than
    # overprinted; the last is right-aligned so it cannot run past the plot edge either.
    step = max(1, -(-len(rows) // 7))
    last_i = len(rows) - 1

    def cx_of(i: int) -> float:
        return PADL + i * slot + slot / 2

    LBL_GAP = 42.0          # ~ the width of "08-17" plus breathing room
    marks = list(range(0, last_i, step))
    while marks and cx_of(last_i) - cx_of(marks[-1]) < LBL_GAP:
        marks.pop()
    marks.append(last_i)
    label_at = set(marks)
    for i, row in enumerate(rows):
        cx = PADL + i * slot + slot / 2
        if row["pulls"] > 0:
            h = max(2.0, BASE - y(row["pulls"]))
            parts.append(f'<rect x="{cx - bw / 2:.1f}" y="{BASE - h:.1f}" width="{bw:.1f}" '
                         f'height="{h:.1f}" rx="4" fill="{DORI}"/>')
        if row["five_star"] > 0:
            dy = (y(row["pulls"]) - 10) if row["pulls"] > 0 else BASE - 10
            parts.append(f'<circle cx="{cx:.1f}" cy="{dy:.1f}" r="5" fill="{COL["5★"]}" class="dotmark"/>')
            if row["five_star"] > 1:
                parts.append(f'<text x="{cx:.1f}" y="{dy - 9:.1f}" text-anchor="middle" '
                             f'class="val">×{row["five_star"]}</text>')
        if i in label_at:
            last = i == last_i
            lx = min(cx + 12, W - PADR) if last else cx
            anchor = "end" if last else "middle"
            parts.append(f'<text x="{lx:.1f}" y="{H - 12}" text-anchor="{anchor}" class="ax">'
                         f'{esc(row["day"][5:])}</text>')
    parts.append("</svg>")

    legend = ('<div class="legend">'
              f'<span class="key"><i class="key-bar" style="background:{DORI}"></i>'
              "<span>pulls that day</span></span>"
              f'<span class="key"><i class="key-dot" style="background:{COL["5★"]}"></i>'
              "<span>a ★5 came out (dot = how many)</span></span></div>")
    tot = sum(r["pulls"] for r in rows)
    stars = sum(r["five_star"] for r in rows)
    cap = (f'{len(rows)} days, {rows[0]["day"]} to {rows[-1]["day"]} · {num(tot)} pulls · '
           f"{num(stars)} ★5")
    return legend + "".join(parts) + f'<p class="cap">{esc(cap)}</p>'


# --- tiles, banners, cards ----------------------------------------------------


def tiles(view: dict, cov: dict) -> str:
    totals = view.get("totals") or {}
    out = ['<div class="tiles">',
           f'<div class="tile"><div class="tl">Pulls</div><div class="tb">{num(totals.get("pulls"))}</div></div>']
    for r in RAR:
        got = (totals.get("by_rarity") or {}).get(r, 0)
        exp = cov["expected"].get(r)
        # The big number is EVERY pull; the small line is the expectation for the
        # compared subset only, and says so.
        small = ("no saved rates" if exp is None or not cov["covered_pulls"]
                 else f'expected {exp:.1f} of {num(cov["covered_pulls"])} compared')
        out.append(f'<div class="tile" style="border-top-color:{COL[r]}">'
                   f'<div class="tl">{stars(r)}</div>'
                   f'<div class="tb">{num(got)} · {pct(got, totals.get("pulls") or 0)}</div>'
                   f'<div class="ts">{esc(small)}</div></div>')
    out.append("</div>")
    return "".join(out)


def thumb_css(thumbs: dict[str, bytes]) -> tuple[str, dict[str, str]]:
    """One CSS rule per DISTINCT thumb, and the class name to use for each card.

    Why not just repeat the `data:` URI in every `<img src>`: a card pulled from two
    banners would be embedded twice, and at ~50 KB a thumb the duplicates alone can cost
    a megabyte. The bytes are emitted once here and referenced by class.
    """
    rules, cls = [], {}
    for i, cid in enumerate(sorted(c for c in thumbs if thumbs[c])):
        uri = _data_uri(thumbs[cid], "image/webp")
        cls[cid] = f"th{i}"
        rules.append(f".th{i}{{background-image:url({uri})}}")
    return "\n".join(rules), cls


def card_grid(cards: list[dict], thumbs: dict[str, bytes], cls: dict[str, str] | None = None) -> str:
    out = []
    for r in RAR:
        rows = [c for c in cards if c.get("rarity") == r]
        if not rows:
            continue
        rows.sort(key=lambda c: (not c.get("is_pickup"), -(c.get("count") or 0), c.get("card_id") or ""))
        n = sum(c.get("count") or 0 for c in rows)
        out.append(f'<div class="sec"><h4 class="sec-h"><i class="dot" style="background:{COL[r]}">'
                   f'</i>{stars(r)}<span>· {num(n)}</span></h4><div class="grid">')
        for c in rows:
            cid = c.get("card_id") or ""
            rate = (f' · rate {c["rate_ppm"] / 1e5:.3f}%' if c.get("rate_ppm") is not None
                    else " · no saved rate")
            title = esc(cid + rate + (" · pickup" if c.get("is_pickup") else ""))
            # A missing thumb is named, never a broken image box.
            klass = (cls or {}).get(cid)
            if klass:
                img = f'<div class="thumb {klass}" role="img" aria-label="{esc(cid)}"></div>'
            elif thumbs.get(cid):
                img = f'<img src="{_data_uri(thumbs[cid], "image/webp")}" alt="{esc(cid)}"/>'
            else:
                img = f'<div class="noimg">{esc(cid)}</div>'
            pk = '<span class="pk">pickup</span>' if c.get("is_pickup") else ""
            box = "card pickup" if c.get("is_pickup") else "card"
            out.append(f'<div class="{box}" title="{title}">'
                       f'{img}<span class="count">×{num(c.get("count"))}</span>{pk}</div>')
        out.append("</div></div>")
    return "".join(out) or '<p class="note">No cards recorded here yet.</p>'


def _hero_bytes(path: Path, max_w: int = 1200) -> tuple[bytes, str]:
    """(bytes, mime) for a banner hero: downscaled to <= `max_w` wide and re-encoded
    WEBP q85, else the file untouched.

    THE SIZE BOUND LIVES HERE. The game's banner PNGs are ~2-3 MB each and base64
    inflates them by a third; three of them make about 10 MB of base64, and a handful of
    banners would push the file past 16 MB. Downscaling is never an
    upscale and NEVER a failure path: any exception -- Pillow missing, a byte string
    that is not really a PNG -- returns the raw bytes, so the hero is always shown.
    """
    raw = path.read_bytes()
    try:
        import io  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415 -- proxyenv ships Pillow (see server._scaled_art)
        with Image.open(io.BytesIO(raw)) as im:
            if im.width > max_w:
                height = max(1, round(im.height * max_w / im.width))
                im = im.convert("RGBA").resize((max_w, height), Image.LANCZOS)
            else:
                im = im.convert("RGBA")
            buf = io.BytesIO()
            im.save(buf, format="WEBP", quality=85, method=4)
        return buf.getvalue(), "image/webp"
    except Exception:  # noqa: BLE001 -- any failure embeds the master
        # The mime follows the BYTES, not the extension: a banner saved as WEBP that
        # Pillow cannot reopen would otherwise be embedded labelled `image/png`.
        return raw, ("image/webp" if raw[:4] == b"RIFF" else "image/png")


def banner_block(b: dict, art_dir: Path, thumbs: dict[str, bytes],
                 cls: dict[str, str] | None = None) -> str:
    gid = b.get("gacha_id") or ""
    art = None
    if gid and (b.get("art") or {}).get("banner"):
        # `art_ext` names WHICH file the view found (png master or the webp new banners
        # are saved as); the fallback tries both so an older view model still works.
        ext = (b.get("art_ext") or {}).get("banner")
        for e in ([ext] if ext else ["png", "webp"]):
            p = art_dir / gid / f"banner.{e}"
            if p.is_file():
                art = _data_uri(*_hero_bytes(p))
                break
    head = (f'<img class="hero" src="{art}" alt="{esc(b.get("name"))}"/>' if art
            else f'<div class="noart">{esc(b.get("name"))}</div>')
    pills = [f'<span class="pill">{num(b.get("pulls"))} pulls</span>']
    for r in RAR:
        got = (b.get("by_rarity") or {}).get(r, 0)
        exp = (b.get("expected_by_rarity") or {}).get(r)
        txt = f"{num(got)}" + ("" if exp is None else f" / {exp:.1f}")
        pills.append(f'<span class="pill" style="border-color:{COL[r]}">'
                     f'<i class="dot" style="background:{COL[r]}"></i>{stars(r)}{txt}</span>')
    if b.get("is_alias"):
        pills.append('<span class="flag">closed · rates not saved</span>')
    elif not b.get("has_rates"):
        pills.append('<span class="flag">no rate table saved</span>')
    if b.get("excluded_from_rates"):
        pills.append(f'<span class="pill dim">{esc(b.get("kind"))} · not in the comparison</span>')
    counters = b.get("counters") or {}
    if counters.get("gacha_point") is not None:
        pills.append(f'<span class="pill dim">points {num(counters["gacha_point"])}</span>')
    if counters.get("drawn_count_total") is not None:
        pills.append(f'<span class="pill dim">{num(counters["drawn_count_total"])} draws counted '
                     "by the game</span>")
    dates = (period(b) or "—") + (
        " · live at last sync" if b.get("is_live_last_sync") else "")
    return (f'<section class="banner">{head}'
            f'<div class="bmeta"><div class="bname">{esc(b.get("name"))}</div>'
            f'<div class="bdates">{esc(dates)} · {esc(gid)}</div>'
            f'<div class="pills">{"".join(pills)}</div></div>'
            f'{card_grid(b.get("cards") or [], thumbs, cls)}</section>')


def unidentified_block(u: dict, thumbs: dict[str, bytes],
                       cls: dict[str, str] | None = None) -> str:
    pills = [f'<span class="pill">{num(u.get("pulls"))} pulls</span>']
    for r in RAR:
        got = (u.get("by_rarity") or {}).get(r, 0)
        if not got:
            continue
        pills.append(f'<span class="pill" style="border-color:{COL[r]}">'
                     f'<i class="dot" style="background:{COL[r]}"></i>{stars(r)}{num(got)}</span>')
    pills.append('<span class="flag">no banner matched — no rates to compare</span>')
    return (f'<section class="banner"><div class="bmeta">'
            f'<div class="bname">{esc(u.get("gacha_name") or "(no name)")}</div>'
            f'<div class="pills">{"".join(pills)}</div></div>'
            f'{card_grid(u.get("cards") or [], thumbs, cls)}</section>')


def owned_block(view: dict, thumbs: dict[str, bytes], cls: dict[str, str] | None = None) -> str:
    """The cards the account holds that the pull history never mentions -- first copies
    from before the tracked window, and the tutorial's guaranteed pull. `None` (no
    account snapshot on this machine) says so rather than showing an empty list."""
    tut = view.get("tutorial")
    if not tut:
        return ('<p class="note">This needs the account snapshot '
                '(dumps/account_snapshot_main.json), which was not present when this file '
                "was written.</p>")
    rows = tut.get("owned_not_tracked") or []
    if not rows:
        return '<p class="cap">Every owned card appears in the tracked pulls.</p>'
    n_tut = sum(1 for r in rows if r.get("is_tutorial"))
    out = [f'<p class="cap">{num(len(rows))} owned cards are not in the tracked pulls — '
           "acquired before the window, or from a source the history does not report"
           + (f". {num(n_tut)} came from the tutorial's guaranteed pull and count for "
              "nothing here.</p>" if n_tut else ".</p>"),
           '<div class="grid owned">']
    for c in rows:
        cid = c.get("card_id") or ""
        klass = (cls or {}).get(cid)
        if klass:
            img = f'<div class="thumb {klass}" role="img" aria-label="{esc(cid)}"></div>'
        elif thumbs.get(cid):
            img = f'<img src="{_data_uri(thumbs[cid], "image/webp")}" alt="{esc(cid)}"/>'
        else:
            img = f'<div class="noimg">{esc(cid)}</div>'
        tag = '<span class="pk tut">tutorial · not counted</span>' if c.get("is_tutorial") else ""
        out.append(f'<div class="card{" tutcard" if c.get("is_tutorial") else ""}" '
                   f'title="{esc(cid)}">{img}{tag}'
                   f'<span class="count">{stars(c.get("rarity") or "")} '
                   f'{esc(day(c.get("acquired")))}</span></div>')
    out.append("</div>")
    return "".join(out)


CSS = """
:root{--bg:#0b1730;--panel:#12244f;--ink:#e8eeff;--dim:#9fb0d6;--line:#24386b;--dori:#ff5c8a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif}
main{max-width:1040px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:34px 0 10px}
h4{margin:0 0 8px;font-size:13px}
.sub{color:var(--dim);margin:0 0 22px}
.note{color:var(--dim);background:var(--panel);border:1px solid var(--line);
  border-radius:10px;padding:12px}
.cap{color:var(--dim);font-size:12px;margin:8px 0 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{background:var(--panel);border:1px solid var(--line);border-top:3px solid var(--line);
  border-radius:10px;padding:12px}
.tl{color:var(--dim);font-size:12px}.tb{font-size:20px;font-weight:600}
.ts{color:var(--dim);font-size:11px;margin-top:2px}
.chartbox{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:14px;margin-top:14px}
.chart{width:100%;height:auto;display:block;overflow:visible}
.chart .ax{fill:var(--dim);font-size:11px}
.chart .val{fill:var(--ink);font-size:11px}
.chart .track{fill:#1b2f5f}
.chart .target{stroke:var(--ink);stroke-width:2}
.chart .grid{stroke:var(--line);stroke-width:1}
.chart .dotmark{stroke:var(--panel);stroke-width:2}
.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--dim);font-size:12px;margin-bottom:6px}
.key{display:inline-flex;align-items:center;gap:6px}
.key-bar{width:22px;height:10px;border-radius:3px;display:inline-block}
.key-tick{width:2px;height:14px;background:var(--ink);display:inline-block}
.key-dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.table{margin-top:10px}
.table summary{cursor:pointer;color:var(--dim)}
table{border-collapse:collapse;margin-top:8px;width:100%}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;font-size:12px}
th{color:var(--dim);font-weight:600}
.td-n{text-align:right;font-variant-numeric:tabular-nums}
.td-r{display:flex;align-items:center;gap:6px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex:0 0 auto}
.banner{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  overflow:hidden;margin-top:16px}
.hero{width:100%;display:block}
.noart{padding:26px;text-align:center;color:var(--dim);background:#16294f}
.bmeta{padding:12px 14px}
.bname{font-weight:600}
.bdates{color:var(--dim);font-size:12px;margin-bottom:8px}
.pills{display:flex;gap:6px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);
  border-radius:999px;padding:2px 9px;font-size:12px}
.pill.dim{color:var(--dim)}
.flag{border:1px dashed #b26a00;color:#e8a33d;border-radius:999px;padding:2px 9px;font-size:12px}
.sec{padding:0 14px 12px}
.sec-h{display:flex;align-items:center;gap:6px;color:var(--dim)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(88px,1fr));gap:8px}
.card{position:relative;background:#16294f;border:1px solid var(--line);border-radius:8px;
  padding:4px;text-align:center}
.card.pickup{border-color:#d07e00}
.card img,.thumb{width:100%;aspect-ratio:1/1;display:block;border-radius:6px;
  background-size:cover;background-position:center}
.noimg{font-size:10px;color:var(--dim);word-break:break-all;padding:10px 2px}
.count{display:block;font-size:11px;color:var(--dim)}
.pk{position:absolute;top:4px;left:4px;background:#d07e00;color:#1a1200;font-size:9px;
  border-radius:4px;padding:0 4px}
.rar{display:inline-flex;align-items:center;gap:4px}
.rarn{color:var(--dim);font-size:11px}
.owned{padding:0 0 4px}
.tutcard{border-color:#d07e00}
.pk.tut{left:4px;right:4px;text-align:center}
"""


def render_html(view: dict, art_dir: Path, thumbs: dict[str, bytes]) -> str:
    """The whole page, as one string. Pure: every byte it embeds was handed in."""
    cov = comparison(view)
    thumb_rules, cls = thumb_css(thumbs)
    star_uri = _data_uri(STAR_PNG.read_bytes(), "image/png") if STAR_PNG.is_file() else None
    totals = view.get("totals") or {}
    banners = view.get("banners") or []
    unid = view.get("unidentified") or []
    body = [f"<h1>Gacha — {num(totals.get('pulls'))} pulls</h1>",
            f'<p class="sub">Exported {esc(_dt.datetime.now().strftime("%Y-%m-%d %H:%M"))} · '
            f"last sync {esc(stamp(view.get('last_sync')))} · "
            f"{num(len(banners))} banners · self-contained (no network)</p>"]
    # The tab's twin (see gacha.js): if the ONE hand-maintained file did not parse, every
    # alias silently stopped working and the export must say so, not just show fewer banners.
    if view.get("aliases_error"):
        body.append('<p class="flag">banner_aliases.json could not be read: '
                    f'{esc(view["aliases_error"])}</p>')
    if not totals.get("pulls"):
        body.append('<p class="note">No pulls in the store yet.</p>')
    else:
        body.append(tiles(view, cov))
        body.append('<div class="chartbox"><h4>Observed vs expected, by rarity</h4>'
                    + rates_chart(view, cov, star_uri) + "</div>")
        body.append('<div class="chartbox"><h4>Pulls per day</h4>' + timeline_chart(view) + "</div>")
        body.append("<h2>Banners</h2>")
        if banners:
            body.extend(banner_block(b, art_dir, thumbs, cls) for b in banners)
        else:
            body.append('<p class="note">No banner was identified for any pull yet.</p>')
        if unid:
            body.append("<h2>Pulls whose banner was not identified</h2>")
            body.append('<p class="cap">A pull whose banner has closed can still be attributed '
                        "by hand: add its name to data/gacha/banner_aliases.json.</p>")
            body.extend(unidentified_block(u, thumbs, cls) for u in unid)
        body.append("<h2>Owned before tracking started</h2>")
        body.append('<section class="banner"><div class="sec">'
                    + owned_block(view, thumbs, cls) + "</div></section>")
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"/>"
            '<meta name="viewport" content="width=device-width,initial-scale=1"/>'
            f"<title>Gacha export — {esc(day(view.get('last_sync')))}</title>"
            f"<style>{CSS}{star_css(star_uri)}{thumb_rules}</style></head>"
            f"<body><main>{''.join(body)}</main></body></html>\n")


# --- gathering the bytes ------------------------------------------------------


def collect_thumbs(view: dict, images_dir: Path) -> dict[str, bytes]:
    """`card-00001-5-uniq-0000-00` -> dumps/card_images/img_card_thumb_00001-5-....webp.
    A card with no rendered thumb is simply absent; `render_html` names it instead."""
    ids = set()
    for b in (view.get("banners") or []) + (view.get("unidentified") or []):
        for c in b.get("cards") or []:
            if c.get("card_id"):
                ids.add(c["card_id"])
    for c in ((view.get("tutorial") or {}).get("owned_not_tracked") or []):
        if c.get("card_id"):
            ids.add(c["card_id"])
    out: dict[str, bytes] = {}
    for cid in sorted(ids):
        key = cid[len("card-"):] if cid.startswith("card-") else cid
        p = images_dir / f"img_card_thumb_{key}.webp"
        # `st_size` as well as `is_file`: one master in the dump is a 0-byte truncated
        # write, and an empty `data:` URI is a broken image rather than a named absence.
        if p.is_file() and p.stat().st_size:
            out[cid] = p.read_bytes()
    return out


def build_view() -> dict:
    doc = json.loads(CARDS_PATH.read_text(encoding="utf8")) if CARDS_PATH.is_file() else {}
    cards_by_id = {c["id"]: c for c in (doc.get("cards") or []) if isinstance(c, dict) and "id" in c}
    store = gacha_view.load_store(STORE_DIR)
    # The account snapshot is optional exactly as it is on the tab: absent -> the
    # view's `tutorial` block is None and the export says so instead of guessing.
    snapshot = None
    if SNAPSHOT_PATH.is_file():
        try:
            snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf8"))
        except (OSError, ValueError):
            snapshot = None
    return gacha_view.build_view(store, cards_by_id, ART_DIR, snapshot=snapshot)


def main() -> None:
    view = build_view()
    thumbs = collect_thumbs(view, CARD_IMAGES_DIR)
    page = render_html(view, ART_DIR, thumbs)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"gacha_export_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    with open(out, "wb") as fh:  # a fresh timestamped name; NOT the append-only store
        fh.write(page.encode("utf8"))
    # ASCII on purpose: this goes to whatever console runs it, including the web
    # server's cp1252 one when the button shells out.
    print(f"{len(view.get('banners') or [])} banners | "
          f"{(view.get('totals') or {}).get('pulls')} pulls | {len(thumbs)} thumbs embedded | "
          f"{out.stat().st_size / 1e6:.1f} MB")
    print(f"WROTE {out}")


if __name__ == "__main__":
    sys.exit(main())
