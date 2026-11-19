"""Event ranking cutoffs: read the stored border series and predict where each tier ends.

READ-ONLY. The store (`tools/recon/proxy/dumps/cutoffs/<region>/<chapter_id>.jsonl` plus
`<chapter_id>.meta.json`) is written ONLY by `tools/refresh/event_cutoffs.py`; this module
reads it and computes the prediction on the fly. API contract: `tools/web/EVENT-API.md`
"Cutoffs".

The prediction. Every border starts at 0 when the chapter opens, so each tier's series is
anchored at (start, 0).

TEMPLATE (once a finished chapter of the same region and rank is stored): each past chapter's
border curve, normalised to its own final (x = elapsed fraction, y = score / final), says what
share of the final is usually in by now; that chapter's forecast is current / that share, and
the forecast is the weighted median over the past chapters (a chapter read with long gaps is a
straight line across them and weighs less). Event Pt rankings learn from Event Pt rankings;
the best-score rankings (Total Score, Song Score) learn from each other, since a best score
saturates early where Event Pt keeps climbing. A relay chapter learns from relays when it can.
The band is how far earlier forecasts made at the same point of a chapter missed (the middle
90 % of their misses, over every finished chapter, `calibration`); before enough of them are on
record, a floor that narrows as the chapter runs. Re-made on every finished border at 10, 50 and
75 % of its chapter, each time with only the chapters that had ended by then, the Event Pt
forecast missed the final by a median 17, 9 and 5 % (the earlier `y = x^a` reading of the
templates, on the same borders: 24, 21 and 18 %), and its band held the final 79-97 % of the
time (that one's: 32-48 %).

PACE (no template): the border keeps rising at the pace it has kept, measured since the start
(average pace) and over the trailing window (recent pace); the prediction is their mean and the
band is the two of them. With ONE pace there is no band: one number cannot bound itself. Pace
knows nothing of an end-of-event sprint, so expect the final above it. A best-score ranking
never gets a pace: its border jumps early and flattens, and a pace from 0 overshoots it by
an order of magnitude; it has no forecast until a template exists.
"""

from __future__ import annotations

import bisect
import json
import math
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

REGIONS = ("us", "as", "jp")
RECENT_WINDOW_MS = 12 * 3600 * 1000  # trailing window for the recent pace
MIN_RECENT_SPAN_MS = 3600 * 1000  # a recent pace over less than an hour is noise
EARLY_FRACTION = 0.05  # before 5 % of the chapter, a pace means little
CURVE_POINTS = 24  # prediction path resolution (to the chapter end)
FINAL_SLACK_MS = 3600 * 1000  # a chapter whose last read is this close to its end is "final"
# a template must have been read from early in its chapter: a curve whose first read came late
# is a straight line from (0, 0) to that read, a shape it never measured (a history first read
# at 73 % of a chapter forecast Top 500 an order of magnitude too high)
MAX_TEMPLATE_START = 0.25
# the band's half-width at the chapter end before enough earlier misses are on record, as a
# share of the forecast and times the share of the chapter still to run (25 % at the start,
# nothing at the end). A best score moves far less (its forecasts on record missed by a median
# 0.2-3 %), so its floor is narrower
BAND_FLOOR = 0.25
BAND_FLOOR_BEST = 0.10
# a template drawn across a gap wider than this share of its chapter weighs TEMPLATE_GAP_WEIGHT
TEMPLATE_GAP = 0.2
TEMPLATE_GAP_WEIGHT = 0.2
# the calibration: forecasts re-made at these shares of every finished chapter; a band needs
# CALIBRATION_MIN misses within CALIBRATION_NEAR of the current share, and keeps the middle
# CALIBRATION_KEEP of them
CALIBRATION_AT = (0.05, 0.1, 0.25, 0.5, 0.75, 0.9)
CALIBRATION_NEAR = 0.15
CALIBRATION_MIN = 12
CALIBRATION_KEEP = 0.9


def family(kind: str | None) -> str:
    """The ranking's family: `points` for Event Pt, `best` for the best-score rankings (total,
    music:<id>)."""
    return "points" if (kind or "points").split(":", 1)[0] == "points" else "best"


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
    """The curve (xs ascending) at x, linear between its points, flat beyond its ends."""
    if x <= xs[0]:
        return ys[0]
    i = bisect.bisect_left(xs, x)
    if i >= len(xs):
        return ys[-1]
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


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


def max_gap(xs: list[float]) -> float:
    """The widest stretch of a normalised curve between two reads, as a share of its chapter."""
    return max((b - a for a, b in zip(xs, xs[1:])), default=1.0)


def _median(v: list[float]) -> float:
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _weighted_median(values: list[float], weights: list[float]) -> float:
    """The value at half the total weight (equal weights: the plain median)."""
    if len(set(weights)) <= 1:
        return _median(values)
    pairs = sorted(zip(values, weights))
    half, acc = sum(weights) / 2, 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def _quantile(sorted_v: list[float], q: float) -> float:
    return sorted_v[min(len(sorted_v) - 1, max(0, int(round(q * (len(sorted_v) - 1)))))]


def _path(now: int, end: int, at: Callable[[int], float]) -> list[list[int]]:
    """CURVE_POINTS + 1 evenly spaced points from now to the chapter end."""
    pts = []
    for k in range(CURVE_POINTS + 1):
        t = now + (end - now) * k // CURVE_POINTS
        pts.append([t, int(round(at(t)))])
    return pts


def _template(t: tuple) -> tuple[list, list, float]:
    """(xs, ys, weight) of a template given as (xs, ys) or (xs, ys, weight)."""
    return t[0], t[1], (t[2] if len(t) > 2 else 1.0)


def predict(
    series: list[tuple[int, int]],
    *,
    start: int,
    end: int,
    now: int,
    templates: list[tuple] | None = None,
    misses: list[float] | None = None,
    kind: str | None = None,
) -> dict | None:
    """Predict a border's final score. `series` = this chapter's reads (oldest first).
    `templates`: past chapters' normalised curves, (xs, ys[, weight]) (`templates_for`).
    `misses`: log(final / forecast) of earlier forecasts made near this point of a chapter
    (`calibration`), which set the band. `kind`: the ranking; a best-score one gets no pace.

    Returns None with no reads, and for a best-score ranking with no template. After the
    chapter end, the last read IS the final (method "final"). See the module docstring."""
    if not series:
        return None
    t_now, s_now = series[-1]
    span = end - start
    if span <= 0:
        return None
    if t_now >= end - 60_000:
        return {
            "method": "final",
            "final": s_now,
            "low": s_now,
            "high": s_now,
            "plus_minus": 0,
            "points": [],
            "band": [],
            "confidence": "final",
            "note": "the chapter has ended; this is the last border read",
        }
    x_now = (t_now - start) / span
    remaining = end - t_now
    elapsed = max(1, t_now - start)

    used = []
    for tpl in templates or []:
        xs, ys, w = _template(tpl)
        f = _interp(xs, ys, x_now)
        if f > 0.01:
            used.append((xs, ys, w, s_now / f, f))
    if used:
        mid = _weighted_median([u[3] for u in used], [u[2] for u in used])
        low, high = min(u[3] for u in used), max(u[3] for u in used)
        calibrated = len(misses or []) >= CALIBRATION_MIN
        if calibrated:
            m = sorted(misses or [])
            cut = (1 - CALIBRATION_KEEP) / 2
            low = min(low, mid * math.exp(_quantile(m, cut)))
            high = max(high, mid * math.exp(_quantile(m, 1 - cut)))
        else:
            floor = (BAND_FLOOR_BEST if family(kind) == "best" else BAND_FLOOR) * (1 - x_now)
            low, high = min(low, mid * (1 - floor)), max(high, mid * (1 + floor))
        low = max(low, s_now)  # a border never falls
        high = max(high, mid)

        def share(t: int) -> float:
            """How much of what is still to come is in by `t` (0 now, 1 at the end): the
            weighted mean of the templates' own shapes from here."""
            x = min(1.0, (t - start) / span)
            num = den = 0.0
            for xs, ys, w, _fin, f in used:
                if f < 1.0:
                    num += w * (_interp(xs, ys, x) - f) / (1.0 - f)
                    den += w
            if not den:
                return min(1.0, max(0.0, (t - t_now) / remaining))
            return min(1.0, max(0.0, num / den))

        points = _path(t_now, end, lambda t: s_now + (mid - s_now) * share(t))
        band = [
            [t, int(round(s_now + (low - s_now) * share(t))), int(round(s_now + (high - s_now) * share(t)))]
            for t, _ in points
        ]
        points[-1][1] = int(round(mid))
        band[-1][1:] = [int(round(low)), int(round(high))]
        n_miss = len(misses or [])
        return {
            "method": "template",
            "final": int(round(mid)),
            "low": int(round(low)),
            "high": int(round(high)),
            "plus_minus": int(round((high - low) / 2)),
            "points": points,
            "band": band,
            "confidence": "medium" if calibrated and len(used) >= 3 else "low",
            "inputs": {
                "elapsed_fraction": round(x_now, 4),
                "templates": len(used),
                "band_from": "misses" if calibrated else "floor",
                "misses": n_miss,
            },
            "note": (
                f"scaled from {len(used)} finished chapter(s) of our own record, same region and "
                "tier: the share of its final each had in by now; band = "
                + (
                    f"how far {n_miss} earlier forecasts at this point of a chapter missed"
                    if calibrated
                    else f"at least ±{round((BAND_FLOOR_BEST if family(kind) == 'best' else BAND_FLOOR) * (1 - x_now) * 100)} % "
                    "(too few earlier forecasts on record to measure it)"
                )
            ),
        }

    if family(kind) == "best":
        return None  # a best score saturates early: a pace from 0 would overshoot it

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
    band = (
        [
            [t, int(round(s_now + r_lo * (t - t_now))), int(round(s_now + r_hi * (t - t_now)))]
            for t, _ in points
        ]
        if len(rates) > 1
        else []
    )
    confidence = "low" if len(rates) == 1 or x_now < EARLY_FRACTION else "rough"
    return {
        "method": "pace",
        "final": int(round(final)),
        "low": int(round(low)),
        "high": int(round(high)),
        "plus_minus": int(round((high - low) / 2)),
        "points": points,
        "band": band,
        "confidence": confidence,
        "inputs": {
            "elapsed_fraction": round(x_now, 4),
            "rate_avg_per_hour": int(round(rate_avg * 3_600_000)),
            "rate_recent_per_hour": (None if rate_recent is None else int(round(rate_recent * 3_600_000))),
        },
        "note": (
            "pace extrapolation (no finished chapter on record yet): mean of the average "
            "and recent pace, band = the two"
            if len(rates) > 1
            else "pace extrapolation from the average pace only (no read old enough for a "
            "recent pace): no band"
        )
        + "; it knows nothing of an end-of-event sprint, so expect the final above it",
    }


# per chapter file, every border's series by (kind, rank), kept per version of the file: the
# template and calibration passes read every chapter on record at each request, and the parsed
# records themselves (a Point Rally chapter is ~12,000 reads) are not kept
_SERIES_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}


def _series_of(recs: Iterable[dict]) -> dict[tuple[str, int], list[tuple[int, int]]]:
    out: dict[tuple[str, int], list[tuple[int, int]]] = {}
    for r in recs:
        k = r.get("kind")
        if not k:
            continue
        t = int(r["captured_at"])
        for b in r.get("borders") or []:
            if b.get("score") is not None and b.get("rank") is not None:
                out.setdefault((k, b["rank"]), []).append((t, int(b["score"])))
    for s in out.values():
        s.sort()
    return out


def chapter_series(store: Path, region: str, chapter_id: str) -> dict[tuple[str, int], list[tuple[int, int]]]:
    """`{(kind, rank): [(captured_at, score), ...]}` of one chapter (`series_for` of each)."""
    path = Path(store or ".") / region / f"{chapter_id}.jsonl"
    try:
        st = path.stat()
        stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    hit = _SERIES_CACHE.get(str(path)) if stamp else None
    if hit and hit[0] == stamp:
        return hit[1]
    out = _series_of(read_chapter(store, region, chapter_id)[1])
    if stamp:
        _SERIES_CACHE[str(path)] = (stamp, out)
    return out


def _finished_curves(store: Path, region: str) -> list[dict]:
    """Every chapter of a region with each finished border's normalised curve, weighted."""
    out = []
    for meta in list_chapters(store, region):
        cid = meta.get("chapter_id")
        if not cid:
            continue
        start, end = int(meta.get("start_time") or 0), chapter_end(meta)
        curves = {}
        for (k, rank), s in chapter_series(store, region, cid).items():
            c = normalised_curve(s, start, end)
            if c:
                w = TEMPLATE_GAP_WEIGHT if max_gap(c[0]) > TEMPLATE_GAP else 1.0
                curves[(k, rank)] = (c[0], c[1], w)
        out.append(
            {"meta": meta, "id": cid, "end": end, "kind": event_kind(meta.get("event_id")), "curves": curves}
        )
    return out


def _pick_templates(
    chapters: list[dict], kind: str, rank: int, this_id: str, this_kind: str | None, before: int | None
) -> list:
    """One border's templates out of `_finished_curves`: same family and rank, the chapter
    itself left out, and only chapters ended by `before` when given; a relay learns from
    relays when it can (the kinds differ in length and pace)."""
    fam = family(kind)
    out, same_kind = [], []
    for ch in chapters:
        if ch["id"] == this_id or (before is not None and ch["end"] > before):
            continue
        for (k, r), curve in sorted(ch["curves"].items()):
            if r != rank or family(k) != fam:
                continue
            out.append(curve)
            if this_kind and ch["kind"] == this_kind:
                same_kind.append(curve)
    return same_kind or out


def templates_for(
    store: Path, region: str, kind: str, rank: int, exclude_chapter: str, before: int | None = None
) -> list:
    """Normalised curves (xs, ys, weight) of every FINISHED border of this region with the same
    rank and ranking family (Event Pt; or the best-score rankings, Total Score and every song,
    together). `before`: only the chapters ended by then (a forecast as it stood then)."""
    this = next((m for m in list_chapters(store, region) if m.get("chapter_id") == exclude_chapter), None)
    this_kind = event_kind(this.get("event_id") if this else None)
    return _pick_templates(_finished_curves(store, region), kind, rank, exclude_chapter, this_kind, before)


# the misses of the re-made forecasts, per store, kept while its finished chapters stay the same
_CALIBRATION_CACHE: dict[str, tuple[tuple, list]] = {}


def calibration(store: Path, now: int | None = None) -> list[tuple[str, float, int, float]]:
    """`[(family, share, chapter end, log(final / forecast))]`: every finished border of every
    region forecast again at each share in CALIBRATION_AT, with only the chapters that had
    ended by then (what the page would have shown). A live forecast's band is the spread of
    these misses near its own share; they pool over the three regions. Only chapters ended by
    `now` (default: the clock) count: a chapter in its last hour has no final yet."""
    now = int(time.time() * 1000) if now is None else now
    per_region = {r: [ch for ch in _finished_curves(store, r) if ch["end"] <= now] for r in REGIONS}
    stamp = tuple(
        (r, ch["id"], ch["end"], len(ch["curves"]))
        + tuple(sorted((k[0], k[1], len(c[0])) for k, c in ch["curves"].items()))
        for r, chs in per_region.items()
        for ch in chs
        if ch["curves"]
    )
    hit = _CALIBRATION_CACHE.get(str(store))
    if hit and hit[0] == stamp:
        return hit[1]
    out = []
    for region, chapters in per_region.items():
        for ch in chapters:
            start, end = int(ch["meta"].get("start_time") or 0), ch["end"]
            every = chapter_series(store, region, ch["id"])
            for k, rank in ch["curves"]:
                series = [p for p in every[(k, rank)] if p[0] >= start]
                final = series[-1][1] if series else 0
                if final <= 0:
                    continue
                for share in CALIBRATION_AT:
                    t_cut = start + int(share * (end - start))
                    cut = [p for p in series if p[0] <= t_cut]
                    if len(cut) < 2 or t_cut - cut[-1][0] > 2 * 3600 * 1000 or cut[-1][1] <= 0:
                        continue
                    tpl = _pick_templates(chapters, k, rank, ch["id"], ch["kind"], t_cut)
                    p = predict(cut, start=start, end=end, now=cut[-1][0], templates=tpl, kind=k)
                    if p and p["method"] == "template" and p["final"] > 0:
                        out.append((family(k), share, end, math.log(final / p["final"])))
    _CALIBRATION_CACHE[str(store)] = (stamp, out)
    return out


def misses_near(cal: list, kind: str, x_now: float, before: int | None = None) -> list[float]:
    """The calibration misses for a forecast of `kind` at share `x_now`, from chapters ended
    by `before` when given."""
    fam = family(kind)
    return [
        m
        for f, share, end, m in cal
        if f == fam and abs(share - x_now) <= CALIBRATION_NEAR and (before is None or end <= before)
    ]


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
            out[data["id"]] = {
                "name": names.get(data.get("nameLangId")),
                "color1": data.get("color1"),
                "color2": data.get("color2"),
            }
    return out


# ------------------------------------------------------------------ the view
def tier_rows(meta: dict, kind: str) -> list[dict]:
    t = meta.get("tiers") or {}
    if kind.startswith("music:"):
        return list((t.get("music") or {}).get(kind.split(":", 1)[1]) or [])
    return list(t.get(kind) or [])


def build_view(
    store: Path,
    region: str,
    *,
    now: int,
    chapter_id: str | None = None,
    kind: str | None = None,
    grades: dict | None = None,
    at: int | None = None,
) -> dict:
    """The `/api/event/cutoffs` payload. Raises LookupError when nothing is stored.
    `at` (ms): the chapter as it stood then -- only the reads made by then, the forecast with
    only the chapters ended by then (a past event replayed, or how a forecast fared)."""
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
        raise LookupError(
            f"no cutoffs stored for region {region!r}"
            + (f" chapter {chapter_id!r}" if chapter_id else "")
            + " -- not synced yet (the server's cutoffs poller or"
            + " event_cutoffs.py writes the store)"
        )
    meta = chapters[0]
    cid = meta["chapter_id"]
    _m, recs = read_chapter(store, region, cid)
    if at is not None:
        recs = [r for r in recs if int(r.get("captured_at") or 0) <= at]
        now = at
    # the kinds READ, plus every song ranking the chapter has: its Song Score tab shows before
    # its first read
    read: set[Any] = {r.get("kind") for r in recs if r.get("kind")}
    listed = {f"music:{m}" for m in ((meta.get("tiers") or {}).get("music") or {})}
    kinds = sorted(read | listed, key=lambda k: (k != "points", k != "total", k))
    kind = kind or next((k for k in kinds if k in read), kinds[0] if kinds else "points")
    start, end = int(meta.get("start_time") or 0), chapter_end(meta)
    of_kind = [r for r in recs if r.get("kind") == kind]
    last = of_kind[-1] if of_kind else None
    grades = grades or {}
    # the finished chapters' curves and the calibration misses: read once for every tier
    finished = _finished_curves(store, region)
    this_kind = event_kind(meta.get("event_id"))
    cal: list | None = None

    tiers = []
    for row in tier_rows(meta, kind):
        rank = row.get("end_rank")
        series = series_for(recs, kind, rank)  # type: ignore[arg-type]
        g = grades.get(row.get("grade_id") or "", {})
        cur = series[-1] if series else None
        prev = series[-2] if len(series) > 1 else None
        rate = None
        if cur and prev and cur[0] > prev[0]:
            rate = int(round((cur[1] - prev[1]) * 3_600_000 / (cur[0] - prev[0])))
        in_last = bool(last and any(b.get("rank") == rank for b in last.get("borders") or []))
        prediction = None
        if in_last and series:
            # only chapters that had ended by then (a chapter in its last hour has no final yet)
            tpl = _pick_templates(finished, kind, rank, cid, this_kind, now)  # type: ignore[arg-type]
            x_now = (series[-1][0] - start) / (end - start) if end > start else 1.0
            misses = None
            if tpl and x_now < 1.0:
                cal = calibration(store) if cal is None else cal
                misses = misses_near(cal, kind, x_now, now)
            prediction = predict(
                series, start=start, end=end, now=now, templates=tpl, misses=misses, kind=kind
            )
        tiers.append(
            {
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
                "prediction": prediction,
            }
        )

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
        "elapsed_fraction": (
            round(min(1.0, max(0.0, (now - start) / (end - start))), 4) if end > start else None
        ),
        "captured_at": captured_at,
        "age_s": (now - captured_at) // 1000 if captured_at else None,
        "snapshots": len(of_kind),
        "self": (dict(own, captured_at=own_at) if own else None),
        # every read of the player's own score in this ranking: the chart's "Show you" line and
        # heatmap (the tier's own line and heatmap are the border as the tracker read it)
        "self_series": own_series(
            store, region, meta.get("event_id"), cid, of_kind, kind, start=start, end=end
        ),
        # the player's Event Pt for this chapter as Marathon/Top last gave it: after the end it is
        # final even when no rank was read since (a chapter in its tally refuses the rank read)
        "own_points": (
            own_points(store, region, meta.get("event_id"), cid) if at is None or at >= end else None
        ),
        "at": at,
        "tiers": tiers,
    }


# ------------------------------------------------------------------ event history
def own_points(store: str | Path, region: str, event_id: str | None, chapter_id: str | None) -> dict | None:
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


def own_series(
    store: str | Path,
    region: str,
    event_id: str | None,
    chapter_id: str | None,
    of_kind: list[dict],
    kind: str,
    *,
    start: int,
    end: int,
) -> list[list[int]]:
    """[[t, score], ...] -- every time the player's own score in this ranking was read: the rank
    reads of the cutoff store and, for Event Pt, each refresh's Marathon/Top points
    (`<event_id>.self_points.jsonl`). A score cannot move after the chapter ends, so a value
    read after the end IS the value at the end: it is placed at `end`, so the line reaches
    the end of the chapter."""
    pts = [
        (r["captured_at"], r["self"]["score"])
        for r in of_kind
        if (r.get("self") or {}).get("score") is not None and r.get("captured_at")
    ]
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
    for t, v in before:  # two reads of one refresh land seconds apart: keep one
        if out and v == out[-1][1] and t - out[-1][0] < 60_000:
            continue
        out.append([t, v])
    if after and end > start:
        out.append([end, max(after)])
    return out


def song_view(mid: str, sg: dict, grade: Callable[[str | None], dict], title: str | None) -> dict:
    """One song ranking of a chapter for the page: its final borders (with tiers), your read."""
    tiers = sorted(sg.get("tiers") or [], key=lambda t: t.get("rank") or 0)
    final = {b.get("rank"): b.get("score") for b in ((sg.get("final_borders") or {}).get("borders") or [])}
    me = sg.get("self") or {}
    reached = next(
        (t.get("grade_id") for t in tiers if me.get("rank") and (t.get("rank") or 0) >= me["rank"]), None
    )
    return {
        "music_id": mid,
        "title": title,
        "rank": me.get("rank"),
        "score": me.get("score"),
        "outside": bool(me.get("out_of_range")) and not me.get("rank"),
        "final": bool(me.get("final")),
        "reached": grade(reached) if reached else None,
        "borders": [
            dict(grade(t.get("grade_id")), rank=t.get("rank"), score=final.get(t.get("rank"))) for t in tiers
        ],
        "borders_final": bool(final),
    }


def standing_from_points(points: int | None, borders: list[dict]) -> dict | None:
    """Where a final Event Pt total lands among a chapter's FINAL borders: the tier it is inside
    and the rank range that implies. {"best": the tightest Top N it reaches (None: below them
    all), "worse": the next Top N up from it (None: it reaches the very top one), "next":
    {"rank", "need"} -- the next border up and the Pt short of it}. A border equal to the
    total counts as reached: the boards order ties by time, so this is the optimistic side
    of a tie. None without a total or borders."""
    rows = sorted(
        (b for b in borders if b.get("score") is not None and b.get("rank")), key=lambda b: b["rank"]
    )
    if points is None or not rows:
        return None
    inside = [b for b in rows if points >= b["score"]]
    best = inside[0] if inside else None
    above = [b for b in rows if b["rank"] < (best["rank"] if best else float("inf"))]
    nxt = above[-1] if above else None
    return {
        "best": best["rank"] if best else None,
        "from_rank": (nxt["rank"] + 1) if (best and nxt) else (1 if best else (rows[-1]["rank"] + 1)),
        "outside": best is None,
        "next": {"rank": nxt["rank"], "need": nxt["score"] - points} if nxt else None,
    }


def history_view(doc: dict, grades: dict | None = None, titles: dict | None = None) -> list[dict]:
    """`dumps/event_history.json` (written by `tools/refresh/event_history.py`) as the page's
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
            final = {
                b.get("rank"): b.get("score") for b in ((ch.get("final_borders") or {}).get("borders") or [])
            }
            # the player's Event Pt is FINAL when read after the chapter closed (Marathon/Top); with
            # the final borders it places the player in each chapter even when no rank was read
            # after the end
            ep = ch.get("event_pt") or {}
            pt_final = bool(
                ep.get("value") is not None
                and ch.get("end_time")
                and (ep.get("captured_at") or 0) >= ch["end_time"]
            )
            standing = None
            if me.get("final") and me.get("rank"):
                standing = {"rank": me["rank"], "how": "final rank read after the end"}
            elif (ch.get("ranking") or "points") == "points" and pt_final and final:
                st = standing_from_points(ep["value"], [{"rank": r, "score": v} for r, v in final.items()])
                if st:
                    standing = dict(st, how="your final Event Pt against the final borders")
                    if st["best"]:
                        reached = next(
                            (t.get("grade_id") for t in tiers if t.get("rank") == st["best"]), reached
                        )
            elif not me and em and reached in rank_of:
                # the emblem names the tier: inside its Top N, outside the next one up -- and
                # the final borders of those two bound the ranking score it took
                best = rank_of[reached]
                ups = sorted(r for r in rank_of.values() if r < best)
                up = ups[-1] if ups else None
                standing = {
                    "best": best,
                    "from_rank": (up + 1) if up else 1,
                    "outside": False,
                    "next": None,
                    "how": "the emblem you hold",
                    "score_range": [final.get(best), final.get(up) if up else None],
                }
            chapters.append(
                {
                    "chapter_id": cid,
                    "number": ch.get("number"),
                    "title": ch.get("title"),
                    "ranking": ch.get("ranking"),
                    "rank": me.get("rank"),
                    "outside": bool(me.get("out_of_range")) and not me.get("rank"),
                    "final": bool(me.get("final")),
                    "read_at": me.get("captured_at"),
                    "event_pt": (ch.get("event_pt") or {}).get("value"),
                    "event_pt_final": pt_final,
                    "standing": standing,
                    "reached": grade(reached) if reached else None,
                    "from_emblem": bool(em) and not me,
                    # an event older than the stored reads has no tiers on record: its recorded finals' ranks
                    "borders": (
                        [
                            dict(grade(t.get("grade_id")), rank=t.get("rank"), score=final.get(t.get("rank")))
                            for t in tiers
                        ]
                        if tiers
                        else [dict(grade(grade_of.get(r)), rank=r, score=s) for r, s in sorted(final.items())]
                    ),
                    "borders_final": bool(final),
                    # the chapter's song ranking(s): final borders, your result, the tier you reached
                    "songs": [
                        song_view(mid, sg, grade, (titles or {}).get(mid))
                        for mid, sg in sorted((ch.get("songs") or {}).items())
                    ],
                }
            )
        out.append(
            {
                "event_id": eid,
                "title": e.get("title"),
                "kind_label": e.get("kind_label"),
                "starts_at": e.get("starts_at"),
                "ends_at": e.get("ends_at"),
                "banner": (e.get("art") or {}).get("banner"),
                "chapters": chapters,
            }
        )
    # an event older than the stored reads has no start date: the day its recorded final was taken
    ended = {
        eid: max(
            [(c.get("final_borders") or {}).get("updated") or "" for c in (e.get("chapters") or {}).values()]
            + [""]
        )
        for eid, e in (doc.get("events") or {}).items()
    }
    out.sort(
        key=lambda e: (e.get("starts_at") or ended.get(e["event_id"]) or "", e["event_id"]), reverse=True
    )
    return out
