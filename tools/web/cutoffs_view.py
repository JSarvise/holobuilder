"""Event ranking cutoffs: read the stored border series and predict where each tier ends.

READ-ONLY. The store (`tools/recon/proxy/dumps/cutoffs/<region>/<chapter_id>.jsonl` plus
`<chapter_id>.meta.json`) is written ONLY by `tools/recon/proxy/event_cutoffs.py`; this module
reads it and computes the prediction on the fly. API contract: `tools/web/EVENT-API.md`
"Cutoffs".

The prediction, in one paragraph. Every border starts at 0 when the chapter opens, so each
tier's series is anchored at (start, 0). With no finished chapter stored on disk the model
is PACE: the border keeps rising at the pace it has kept, measured two ways -- since the start
(average pace) and over the trailing window (recent pace). The prediction is their mean and the
band is the two of them. With ONE pace (a single snapshot, or no snapshot old enough to
measure a recent pace) there is no band: one number cannot bound itself. Once a finished
chapter of the same region and ranking kind is stored, the model becomes TEMPLATE: each past
chapter's border curve, normalised to its own final (x = elapsed fraction, y = score / final),
says what fraction of the final is usually in by now; final = current / that fraction, the
band is the spread over past chapters. Pace extrapolation knows nothing of an end-of-event
sprint, so in that mode the band is a floor-ish estimate and is labelled as such.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

REGIONS = ("us", "as", "jp")
RECENT_WINDOW_MS = 12 * 3600 * 1000   # trailing window for the recent pace
MIN_RECENT_SPAN_MS = 3600 * 1000      # a recent pace over less than an hour is noise
EARLY_FRACTION = 0.05                 # before 5 % of the chapter, a pace means little
CURVE_POINTS = 24                     # prediction path resolution (to the chapter end)
FINAL_SLACK_MS = 3600 * 1000          # a chapter whose last read is this close to its end is "final"
# a template must have been read from early in its chapter: a curve whose first read came late
# is anchored (0,0) -> (first read) by a straight line it never measured, and its exponent
# comes out of that gap (a history first read at 73 % of a chapter can fit a = 2.28 where the
# full record gives 1.04, and forecast Top 500 an order of magnitude too high)
MAX_TEMPLATE_START = 0.25
# the template band's least half-width at the chapter end, as a share of the forecast, grown
# from 0 now in proportion to the time left: one past chapter cannot bound itself, and how far a
# same-tier curve carries from one event to the next is not known (an estimated floor; a
# cross-REGION transfer has missed by up to 9 % on the top tiers, 50 % on low ones)
BAND_FLOOR = 0.15


# ------------------------------------------------------------------ store (read only)
def read_chapter(store: Path, region: str, chapter_id: str) -> tuple[dict | None, list[dict]]:
    """(meta, records) for one chapter. Missing files read as (None, [])."""
    d = store / region
    meta_path = d / f"{chapter_id}.meta.json"
    meta = json.loads(meta_path.read_text("utf-8")) if meta_path.is_file() else None
    recs: list[dict] = []
    path = d / f"{chapter_id}.jsonl"
    if path.is_file():
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue  # a torn last line from an interrupted append: skip, never fail
    recs.sort(key=lambda r: r.get("captured_at") or 0)
    return meta, recs


def list_chapters(store: Path, region: str) -> list[dict]:
    """Every chapter of a region that has metadata, newest start first."""
    d = store / region
    if not d.is_dir():
        return []
    metas = []
    for p in d.glob("*.meta.json"):
        try:
            metas.append(json.loads(p.read_text("utf-8")))
        except ValueError:
            continue
    # a chapter starts when the one before it ends (a Spotlight event's chapters run in turn;
    # older metas carry the EVENT's start on every chapter)
    by_event: dict = {}
    for m in metas:
        by_event.setdefault(m.get("event_id"), []).append(m)
    for chs in by_event.values():
        chs.sort(key=lambda m: m.get("chapter_number") or 0)
        for before, after in zip(chs, chs[1:]):
            end = before.get("chapter_end_time")
            if end and int(after.get("start_time") or 0) < int(end):
                after["start_time"] = int(end) + 1000
    metas.sort(key=lambda m: (m.get("start_time") or 0, m.get("chapter_id") or ""), reverse=True)
    return metas


# ------------------------------------------------------------------ series helpers (pure)
def chapter_end(meta: dict) -> int:
    return int(meta.get("chapter_end_time") or meta.get("end_time") or 0)


def series_for(records: Iterable[dict], kind: str, rank: int) -> list[tuple[int, int]]:
    """[(captured_at, score)] of one border, oldest first, one point per read."""
    out = []
    for r in records:
        if r.get("kind") != kind:
            continue
        for b in r.get("borders") or []:
            if b.get("rank") == rank and b.get("score") is not None:
                out.append((int(r["captured_at"]), int(b["score"])))
    out.sort()
    return out


def _interp(xs: list[float], ys: list[float], x: float) -> float:
    if x <= xs[0]:
        return ys[0]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


def normalised_curve(series: list[tuple[int, int]], start: int, end: int) -> tuple[list, list] | None:
    """A FINISHED chapter's border as (x = elapsed fraction, y = score / final), anchored (0,0)
    and ending at (1,1). None when the series does not reach the end or the final is 0."""
    if not series or end <= start:
        return None
    t_last, final = series[-1]
    if final <= 0 or t_last < end - FINAL_SLACK_MS:
        return None
    if (series[0][0] - start) / (end - start) > MAX_TEMPLATE_START:
        return None
    xs, ys = [0.0], [0.0]
    for t, s in series:
        x = min(1.0, max(0.0, (t - start) / (end - start)))
        if x <= xs[-1]:
            continue
        xs.append(x)
        ys.append(min(1.0, s / final))
    if xs[-1] < 1.0:
        xs.append(1.0)
        ys.append(1.0)
    return xs, ys


def fit_exponent(xs: list, ys: list) -> float | None:
    """The `a` of `y = x**a` closest (in logs) to a normalised curve's inner points: one
    number per past chapter, so a forecast built from it is a smooth curve however few reads
    the chapter had (two reads and its final drew a kinked line). >1 = an end sprint (the
    border gains faster late), <1 = it fades. None without an inner point."""
    num = den = 0.0
    for x, y in zip(xs, ys):
        if 0.01 < x < 0.99 and 0.0 < y < 1.0:
            lx = math.log(x)
            num += math.log(y) * lx
            den += lx * lx
    return num / den if den > 0 else None


def _median(v: list[float]) -> float:
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _path(now: int, end: int, at: callable) -> list[list[int]]:
    """CURVE_POINTS + 1 evenly spaced points from now to the chapter end."""
    pts = []
    for k in range(CURVE_POINTS + 1):
        t = now + (end - now) * k // CURVE_POINTS
        pts.append([t, int(round(at(t)))])
    return pts


def predict(series: list[tuple[int, int]], *, start: int, end: int, now: int,
            templates: list[tuple[list, list]] | None = None) -> dict | None:
    """Predict a border's final score. `series` = this chapter's reads (oldest first).

    Returns None with no reads. After the chapter end, the last read IS the final (method
    "final"). See the module docstring for "pace" and "template"."""
    if not series:
        return None
    t_now, s_now = series[-1]
    span = end - start
    if span <= 0:
        return None
    if t_now >= end - 60_000:
        return {"method": "final", "final": s_now, "low": s_now, "high": s_now,
                "plus_minus": 0, "points": [], "band": [], "confidence": "final",
                "note": "the chapter has ended; this is the last border read"}
    x_now = (t_now - start) / span
    remaining = end - t_now
    elapsed = max(1, t_now - start)

    usable = []
    for xs, ys in templates or []:
        f = _interp(xs, ys, x_now)
        a = fit_exponent(xs, ys)
        if f > 0.01 and a is not None:
            usable.append((a, s_now / x_now ** a))
    if usable:
        finals = [fin for _a, fin in usable]
        mid, low, high = _median(finals), min(finals), max(finals)
        # the path: s_now * (x / x_now) ** a, `a` chosen so that it ends on the median final
        a_mid = math.log(s_now / mid) / math.log(x_now) if 0 < x_now < 1 and mid > 0 else 1.0
        a_lo = math.log(s_now / low) / math.log(x_now) if low > 0 else a_mid
        a_hi = math.log(s_now / high) / math.log(x_now) if high > 0 else a_mid

        def at(t: int, a: float) -> float:
            return s_now * (min(1.0, (t - start) / span) / x_now) ** a

        def band_at(t: int) -> list[int]:
            grown = BAND_FLOOR * min(1.0, max(0.0, (t - t_now) / remaining))
            m = at(t, a_mid)
            return [t, int(round(min(at(t, a_lo), m * (1 - grown)))),
                    int(round(max(at(t, a_hi), m * (1 + grown))))]
        points = _path(t_now, end, lambda t: at(t, a_mid))
        band = [band_at(t) for t, _ in points]
        low, high = band[-1][1], band[-1][2]
        return {
            "method": "template", "final": int(round(mid)),
            "low": int(low), "high": int(high),
            "plus_minus": int(round((high - low) / 2)),
            "points": points, "band": band,
            "confidence": "medium" if len(usable) >= 3 else "low",
            "inputs": {"elapsed_fraction": round(x_now, 4), "templates": len(usable),
                       "exponents": [round(a, 3) for a, _f in usable]},
            "note": (f"scaled from {len(usable)} finished chapter(s) of our own record, same "
                     "region and tier; each one's curve read as y = x^a, so the path bends "
                     "(an end-of-event sprint where a > 1); band = their spread, at least "
                     f"±{round(BAND_FLOOR * 100)} % by the end"),
        }

    rate_avg = s_now / elapsed
    rate_recent = None
    for t, s in series:
        if t_now - t <= RECENT_WINDOW_MS and t_now - t >= MIN_RECENT_SPAN_MS:
            rate_recent = (s_now - s) / (t_now - t)
            break
    rates = [rate_avg] + ([rate_recent] if rate_recent is not None else [])
    finals = [s_now + r * remaining for r in rates]
    final = sum(finals) / len(finals)
    low, high = min(finals), max(finals)
    r_mid = (final - s_now) / remaining
    r_lo, r_hi = (low - s_now) / remaining, (high - s_now) / remaining
    points = _path(t_now, end, lambda t: s_now + r_mid * (t - t_now))
    band = ([[t, int(round(s_now + r_lo * (t - t_now))), int(round(s_now + r_hi * (t - t_now)))]
             for t, _ in points] if len(rates) > 1 else [])
    confidence = "low" if len(rates) == 1 or x_now < EARLY_FRACTION else "rough"
    return {
        "method": "pace", "final": int(round(final)),
        "low": int(round(low)), "high": int(round(high)),
        "plus_minus": int(round((high - low) / 2)),
        "points": points, "band": band, "confidence": confidence,
        "inputs": {"elapsed_fraction": round(x_now, 4),
                   "rate_avg_per_hour": int(round(rate_avg * 3_600_000)),
                   "rate_recent_per_hour": (None if rate_recent is None
                                            else int(round(rate_recent * 3_600_000)))},
        "note": ("pace extrapolation (no finished chapter on record yet): mean of the average "
                 "and recent pace, band = the two" if len(rates) > 1 else
                 "pace extrapolation from the average pace only (no read old enough for a "
                 "recent pace): no band")
                + "; it knows nothing of an end-of-event sprint, so expect the final above it",
    }


def templates_for(store: Path, region: str, kind: str, rank: int, exclude_chapter: str) -> list:
    """Normalised curves of every FINISHED chapter of this region, same ranking kind and rank.
    Song rankings ('music:<id>') template each other regardless of the song."""
    family = kind.split(":", 1)[0]
    out, same_kind = [], []
    this = next((m for m in list_chapters(store, region) if m.get("chapter_id") == exclude_chapter), None)
    this_kind = event_kind(this.get("event_id") if this else None)
    for meta in list_chapters(store, region):
        cid = meta.get("chapter_id")
        if not cid or cid == exclude_chapter:
            continue
        _m, recs = read_chapter(store, region, cid)
        kinds = {r.get("kind") for r in recs if (r.get("kind") or "").split(":", 1)[0] == family}
        for k in sorted(kinds):
            curve = normalised_curve(series_for(recs, k, rank), int(meta.get("start_time") or 0),
                                     chapter_end(meta))
            if curve:
                out.append(curve)
                if this_kind and event_kind(meta.get("event_id")) == this_kind:
                    same_kind.append(curve)
    # a relay chapter learns from relays when it can: the kinds differ in length and pace
    return same_kind or out


def event_kind(event_id: str | None) -> str | None:
    """`marathon_event-normal-007` -> "normal", `marathon_event-relay-003` -> "relay"."""
    if not event_id or "-" not in event_id:
        return None
    parts = event_id.split("-")
    return parts[1] if len(parts) > 2 else None


# ------------------------------------------------------------------ grades
def load_grades(grade_rows: list | None, lang_rows: list | None) -> dict[str, dict]:
    """{grade_id: {name, color1, color2}} from the game's MarathonRankingGrade master + Eng lang."""
    names = {}
    for row in lang_rows or []:
        data = row.get("data") if isinstance(row, dict) else None
        if isinstance(data, dict) and data.get("id"):
            names[data["id"]] = data.get("text")
    out = {}
    for row in grade_rows or []:
        data = row.get("data") if isinstance(row, dict) else None
        if isinstance(data, dict) and data.get("id"):
            out[data["id"]] = {"name": names.get(data.get("nameLangId")),
                               "color1": data.get("color1"), "color2": data.get("color2")}
    return out


# ------------------------------------------------------------------ the view
def tier_rows(meta: dict, kind: str) -> list[dict]:
    t = meta.get("tiers") or {}
    if kind.startswith("music:"):
        return list((t.get("music") or {}).get(kind.split(":", 1)[1]) or [])
    return list(t.get(kind) or [])


def build_view(store: Path, region: str, *, now: int, chapter_id: str | None = None,
               kind: str | None = None, grades: dict | None = None) -> dict:
    """The `/api/event/cutoffs` payload. Raises LookupError when nothing is stored."""
    if region not in REGIONS:
        raise ValueError(f"region must be one of {', '.join(REGIONS)}")
    chapters = list_chapters(store, region)
    if chapter_id:
        chapters = [m for m in chapters if m.get("chapter_id") == chapter_id]
    if not chapter_id:
        # the chapter being played: started and newest (a Spotlight event stores a chapter per
        # holomem, and the one starting last is not the one running)
        begun = [m for m in chapters if int(m.get("start_time") or 0) <= now]
        chapters = begun + [m for m in chapters if m not in begun]
    if not chapters:
        raise LookupError(f"no cutoffs stored for region {region!r}"
                          + (f" chapter {chapter_id!r}" if chapter_id else "")
                          + " -- run the sync (POST /api/event/cutoffs/sync)")
    meta = chapters[0]
    cid = meta["chapter_id"]
    _m, recs = read_chapter(store, region, cid)
    # the kinds READ, plus every song ranking the chapter has: its Song Score tab shows before
    # its first read
    read = {r.get("kind") for r in recs if r.get("kind")}
    listed = {f"music:{m}" for m in ((meta.get("tiers") or {}).get("music") or {})}
    kinds = sorted(read | listed, key=lambda k: (k != "points", k != "total", k))
    kind = kind or next((k for k in kinds if k in read), kinds[0] if kinds else "points")
    start, end = int(meta.get("start_time") or 0), chapter_end(meta)
    of_kind = [r for r in recs if r.get("kind") == kind]
    last = of_kind[-1] if of_kind else None
    grades = grades or {}

    tiers = []
    for row in tier_rows(meta, kind):
        rank = row.get("end_rank")
        series = series_for(recs, kind, rank)
        g = grades.get(row.get("grade_id") or "", {})
        cur = series[-1] if series else None
        prev = series[-2] if len(series) > 1 else None
        rate = None
        if cur and prev and cur[0] > prev[0]:
            rate = int(round((cur[1] - prev[1]) * 3_600_000 / (cur[0] - prev[0])))
        in_last = bool(last and any(b.get("rank") == rank for b in last.get("borders") or []))
        tiers.append({
            "rank": rank,
            "grade_id": row.get("grade_id"),
            "grade_name": g.get("name"),
            "color1": g.get("color1"),
            "color2": g.get("color2"),
            "score": cur[1] if cur and in_last else None,
            "captured_at": cur[0] if cur and in_last else None,
            "filled": in_last,
            "previous": ({"score": prev[1], "captured_at": prev[0]} if prev else None),
            "rate_per_hour": rate,
            "series": [[t, s] for t, s in series],
            "prediction": (predict(series, start=start, end=end, now=now,
                                   templates=templates_for(store, region, kind, rank, cid))
                           if in_last else None),
        })

    own = next((r.get("self") for r in reversed(of_kind) if r.get("self")), None)
    own_at = next((r.get("captured_at") for r in reversed(of_kind) if r.get("self")), None)
    captured_at = last.get("captured_at") if last else None
    return {
        "available": True,
        "region": region,
        "regions": {r: bool(list_chapters(store, r)) for r in REGIONS},
        "event_id": meta.get("event_id"),
        "event_name": meta.get("event_name"),
        "chapter_id": cid,
        "chapters": [m.get("chapter_id") for m in list_chapters(store, region)],
        "kind": kind,
        "kinds": kinds,
        "start_time": start,
        "end_time": end,
        "reveal_time": meta.get("reveal_time"),
        "now": now,
        "ended": now >= end,
        "elapsed_fraction": (round(min(1.0, max(0.0, (now - start) / (end - start))), 4)
                             if end > start else None),
        "captured_at": captured_at,
        "age_s": (now - captured_at) // 1000 if captured_at else None,
        "snapshots": len(of_kind),
        "self": (dict(own, captured_at=own_at) if own else None),
        # every read of the player's own score in this ranking: the chart's "Show you" line and
        # heatmap (the tier's own line and heatmap are the border as the tracker read it)
        "self_series": own_series(store, region, meta.get("event_id"), cid, of_kind, kind,
                                  start=start, end=end),
        # the player's Event Pt for this chapter as Marathon/Top last gave it: after the end it is
        # final even when no rank was read since (a chapter in its tally refuses the rank read)
        "own_points": own_points(store, region, meta.get("event_id"), cid),
        "tiers": tiers,
    }


# ------------------------------------------------------------------ event history
def own_points(store, region: str, event_id: str | None, chapter_id: str | None) -> dict | None:
    """{value, captured_at} -- the latest own Event Pt of this chapter in
    `<region>/<event_id>.self_points.jsonl` (event_cutoffs.append_points), or None."""
    if not (event_id and chapter_id):
        return None
    try:
        path = Path(store) / region / f"{event_id}.self_points.jsonl"
        lines = path.read_text("utf-8").splitlines() if path.is_file() else []
    except OSError:
        return None
    best = None
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        v = (rec.get("chapters") or {}).get(chapter_id)
        if v is not None and (best is None or rec.get("captured_at", 0) >= best["captured_at"]):
            best = {"value": v, "captured_at": rec.get("captured_at", 0)}
    return best


def own_series(store, region: str, event_id: str | None, chapter_id: str | None,
               of_kind: list[dict], kind: str, *, start: int, end: int) -> list[list[int]]:
    """[[t, score], ...] -- every time the player's own score in this ranking was read: the rank
    reads of the cutoff store and, for Event Pt, each refresh's Marathon/Top points
    (`<event_id>.self_points.jsonl`). A score cannot move after the chapter ends, so a value
    read after the end IS the value at the end: it is placed at `end`, so the line reaches
    the end of the chapter."""
    pts = [(r["captured_at"], r["self"]["score"]) for r in of_kind
           if (r.get("self") or {}).get("score") is not None and r.get("captured_at")]
    if kind == "points" and event_id and chapter_id:
        path = Path(store) / region / f"{event_id}.self_points.jsonl"
        try:
            lines = path.read_text("utf-8").splitlines() if path.is_file() else []
        except OSError:
            lines = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            v = (rec.get("chapters") or {}).get(chapter_id)
            if v is not None and rec.get("captured_at"):
                pts.append((int(rec["captured_at"]), int(v)))
    before = sorted({(t, v) for t, v in pts if start <= t < end})
    after = [v for t, v in pts if t >= end]
    out: list[list[int]] = []
    for t, v in before:   # two reads of one refresh land seconds apart: keep one
        if out and v == out[-1][1] and t - out[-1][0] < 60_000:
            continue
        out.append([t, v])
    if after and end > start:
        out.append([end, max(after)])
    return out


def song_view(mid: str, sg: dict, grade, title: str | None) -> dict:
    """One song ranking of a chapter for the page: its final borders (with tiers), your read."""
    tiers = sorted(sg.get("tiers") or [], key=lambda t: t.get("rank") or 0)
    final = {b.get("rank"): b.get("score") for b in ((sg.get("final_borders") or {}).get("borders") or [])}
    me = sg.get("self") or {}
    reached = next((t.get("grade_id") for t in tiers if me.get("rank") and (t.get("rank") or 0) >= me["rank"]), None)
    return {"music_id": mid, "title": title, "rank": me.get("rank"), "score": me.get("score"),
            "outside": bool(me.get("out_of_range")) and not me.get("rank"), "final": bool(me.get("final")),
            "reached": grade(reached) if reached else None,
            "borders": [dict(grade(t.get("grade_id")), rank=t.get("rank"), score=final.get(t.get("rank")))
                        for t in tiers],
            "borders_final": bool(final)}


def standing_from_points(points: int | None, borders: list[dict]) -> dict | None:
    """Where a final Event Pt total lands among a chapter's FINAL borders: the tier it is inside
    and the rank range that implies. {"best": the tightest Top N it reaches (None: below them
    all), "worse": the next Top N up from it (None: it reaches the very top one), "next":
    {"rank", "need"} -- the next border up and the Pt short of it}. A border equal to the
    total counts as reached: the boards order ties by time, so this is the optimistic side
    of a tie. None without a total or borders."""
    rows = sorted((b for b in borders if b.get("score") is not None and b.get("rank")),
                  key=lambda b: b["rank"])
    if points is None or not rows:
        return None
    inside = [b for b in rows if points >= b["score"]]
    best = inside[0] if inside else None
    above = [b for b in rows if b["rank"] < (best["rank"] if best else float("inf"))]
    nxt = above[-1] if above else None
    return {"best": best["rank"] if best else None,
            "from_rank": (nxt["rank"] + 1) if (best and nxt) else (1 if best else (rows[-1]["rank"] + 1)),
            "outside": best is None,
            "next": {"rank": nxt["rank"], "need": nxt["score"] - points} if nxt else None}


def history_view(doc: dict, grades: dict | None = None, titles: dict | None = None) -> list[dict]:
    """`dumps/event_history.json` (written by `tools/recon/proxy/event_history.py`) as the page's
    list, newest event first: per chapter the player's result (rank, tier reached, Event Pt, or
    only the tier from an emblem for an event older than the stored reads) and the final borders."""
    grades = grades or {}

    def grade(gid: str | None) -> dict:
        g = grades.get(gid or "", {})
        return {"grade_id": gid, "grade_name": g.get("name"), "color1": g.get("color1")}

    # the tier ladder (grade -> its Top N), as every event on record lists it; an event older
    # than the stored reads has none of its own, and its emblem names only the grade
    rank_of: dict[str, int] = {}
    for e in (doc.get("events") or {}).values():
        for c in (e.get("chapters") or {}).values():
            for t in c.get("tiers") or []:
                if t.get("grade_id") and t.get("rank"):
                    rank_of.setdefault(t["grade_id"], t["rank"])
    grade_of = {r: g for g, r in rank_of.items()}

    out = []
    for eid, e in (doc.get("events") or {}).items():
        chapters = []
        for cid, ch in sorted((e.get("chapters") or {}).items(), key=lambda kv: kv[1].get("number") or 0):
            tiers = sorted(ch.get("tiers") or [], key=lambda t: t.get("rank") or 0)
            me = ch.get("self") or {}
            reached = None
            if me.get("rank"):
                reached = next((t.get("grade_id") for t in tiers if (t.get("rank") or 0) >= me["rank"]), None)
            emblems = ch.get("emblems") or {}
            em = emblems.get(ch.get("ranking") or "points") or next(iter(emblems.values()), None)
            if reached is None and em and not me.get("rank"):
                reached = f"marathon_ranking_grade-{em['grade']}"
            final = {b.get("rank"): b.get("score") for b in ((ch.get("final_borders") or {}).get("borders") or [])}
            # the player's Event Pt is FINAL when read after the chapter closed (Marathon/Top); with
            # the final borders it places the player in each chapter even when no rank was read
            # after the end
            ep = ch.get("event_pt") or {}
            pt_final = bool(ep.get("value") is not None and ch.get("end_time")
                            and (ep.get("captured_at") or 0) >= ch["end_time"])
            standing = None
            if me.get("final") and me.get("rank"):
                standing = {"rank": me["rank"], "how": "final rank read after the end"}
            elif (ch.get("ranking") or "points") == "points" and pt_final and final:
                st = standing_from_points(ep["value"], [{"rank": r, "score": v} for r, v in final.items()])
                if st:
                    standing = dict(st, how="your final Event Pt against the final borders")
                    if st["best"]:
                        reached = next((t.get("grade_id") for t in tiers if t.get("rank") == st["best"]), reached)
            elif not me and em and reached in rank_of:
                # the emblem names the tier: inside its Top N, outside the next one up -- and
                # the final borders of those two bound the ranking score it took
                best = rank_of[reached]
                ups = sorted(r for r in rank_of.values() if r < best)
                up = ups[-1] if ups else None
                standing = {"best": best, "from_rank": (up + 1) if up else 1, "outside": False,
                            "next": None, "how": "the emblem you hold",
                            "score_range": [final.get(best), final.get(up) if up else None]}
            chapters.append({
                "chapter_id": cid, "number": ch.get("number"), "title": ch.get("title"),
                "ranking": ch.get("ranking"),
                "rank": me.get("rank"), "outside": bool(me.get("out_of_range")) and not me.get("rank"),
                "final": bool(me.get("final")), "read_at": me.get("captured_at"),
                "event_pt": (ch.get("event_pt") or {}).get("value"),
                "event_pt_final": pt_final,
                "standing": standing,
                "reached": grade(reached) if reached else None,
                "from_emblem": bool(em) and not me,
                # an event older than the stored reads has no tiers on record: its recorded finals' ranks
                "borders": ([dict(grade(t.get("grade_id")), rank=t.get("rank"), score=final.get(t.get("rank")))
                             for t in tiers] if tiers else
                            [dict(grade(grade_of.get(r)), rank=r, score=s) for r, s in sorted(final.items())]),
                "borders_final": bool(final),
                # the chapter's song ranking(s): final borders, your result, the tier you reached
                "songs": [song_view(mid, sg, grade, (titles or {}).get(mid))
                          for mid, sg in sorted((ch.get("songs") or {}).items())],
            })
        out.append({"event_id": eid, "title": e.get("title"), "kind_label": e.get("kind_label"),
                    "starts_at": e.get("starts_at"), "ends_at": e.get("ends_at"),
                    "banner": (e.get("art") or {}).get("banner"), "chapters": chapters})
    # an event older than the stored reads has no start date: the day its recorded final was taken
    ended = {eid: max([(c.get("final_borders") or {}).get("updated") or "" for c in (e.get("chapters") or {}).values()]
                      + [""]) for eid, e in (doc.get("events") or {}).items()}
    out.sort(key=lambda e: (e.get("starts_at") or ended.get(e["event_id"]) or "", e["event_id"]), reverse=True)
    return out
