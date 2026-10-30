"""Read-only aggregation of data/gacha/*.jsonl into the Gacha tab's view model.

READS ONLY (tools/web/test_no_writes.py). The store's writer is tools/recon/proxy/gacha_store.py;
this module deliberately has its own tiny reader so the web package imports nothing that can write.
Expected counts use the LATEST saved rate table per banner; a banner without one contributes
nothing to `expected_by_rarity` and is flagged `has_rates: false` -- absence stays visible.

Two rules the raw store does not state, both read off the game's own banner data:

* **9+1.** Every 10-pull button carries `fixed_reward_pick_count = 1` and the rate row carries a
  second table, `normal.fixed` (100% 5★ on gacha-fixed-260807). Draws sharing a `draw_time` are
  one multi-draw, so a group of n contributes `(n - n//10)` draws from `normal` plus `n//10` from
  `fixed`. Counting n x normal would be biased low on every 10-pull. A banner whose `fixed` table is
  empty or absent keeps the plain n x normal -- no phantom guaranteed slot is invented.
* **Exclusions.** A banner's `kind` comes from the hand-maintained `banner_aliases.json`, else
  from its id (`-fixed-` / `ticket` / `pickup` / `common`). Pulls of kind `ticket` or `tutorial`
  are TRACKED but never enter the observed-vs-expected `comparison`: they were not paid for with
  a rate roll, so scoring luck against them would be comparing two different populations.
  `comparison` splits every pull into covered + excluded + uncovered, so none can go missing.

Also:

* **Attribution by name and time.** A pull drawn on a banner that had closed by the next sync
  reaches the store with no `gacha_id` (the sync can only resolve LIVE banners). When exactly
  one stored banner carries the pull's name and its window holds the draw time, the pull is
  that banner's (otherwise about 5 % of pulls end up 'without a banner').
* **`rate_facts`** per banner, read off its saved table: the stated 5/4/3 rates, the 10th
  slot's 5★ rate, each featured card's rate. On the saved tables every banner is 5 / 10 / 85
  with the 10th card of a 10-pull 5 % 5★ / 95 % 4★ (100 % 5★ on a guaranteed banner); a
  pickup's featured cards share 2 % of the 5 %; a 'Take your pick!' banner with a member chosen
  is 5 % (the chosen one 1 %), and WITHOUT one chosen 4 % -- that 1 % is missing.
* **`curve`** (observed vs expected ★5, draw group by draw group, with the binomial variance)
  and **`gaps`** (pulls between consecutive ★5 groups) for the page's charts.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

_FILES = ("pulls", "banners", "rates", "counters")
_RAR_RE = re.compile(r"^card-\d+-(\d)-")
ALIASES_FILE = "banner_aliases.json"
_TUTORIAL_WINDOW_MS = 600_000          # the account's first batch arrives within 10 min
_EXCLUDED_KINDS = ("ticket", "tutorial")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with open(path, "r", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def parse_aliases(text: str) -> tuple[dict, str | None]:
    """(aliases, error). The `_doc` key is documentation, not a banner.

    Split out from the file read so the parsing rule is testable without a file: the
    web package writes nothing, tests included."""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(doc, dict):
        return {}, "top level is not an object"
    return {k: v for k, v in doc.items() if k != "_doc" and isinstance(v, dict)}, None


def _read_aliases(path: Path) -> tuple[dict, str | None]:
    """The ONE hand-maintained file in the store: gacha_name -> {gacha_id, kind}.
    Absent is first-class ({}, no error). Hand-maintained means hand-breakable, so a
    malformed file is NAMED on the page rather than 500-ing the whole tab."""
    if not path.is_file():
        return {}, None
    try:
        text = path.read_text(encoding="utf8")
    except (OSError, ValueError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    return parse_aliases(text)


def load_store(store_dir: Path) -> dict:
    store = {k: _read_jsonl(store_dir / f"{k}.jsonl") for k in _FILES}
    store["aliases"], store["aliases_error"] = _read_aliases(store_dir / ALIASES_FILE)
    return store


def rarity_of(card_id: str, cards_by_id: dict) -> str:
    c = cards_by_id.get(card_id)
    if c and c.get("rarity"):
        return str(c["rarity"])
    m = _RAR_RE.match(card_id or "")
    return f"{m.group(1)}★" if m else "?"


def _latest_by_id(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        out[r["gacha_id"]] = r
    return out


def _table_index(table: list[dict]) -> tuple[dict[str, float], dict[str, int]]:
    by_rar = {f"{t['rarity']}★": t["ppm"] / 1e7 for t in (table or []) if "rarity" in t}
    by_card = {c["card_id"]: c["ppm"] for t in (table or []) for c in t.get("cards", [])}
    return by_rar, by_card


def _rate_index(rate_row: dict | None) -> tuple[dict[str, float], dict[str, int]]:
    """({rarity: probability}, {card_id: ppm}) from the latest normal table; empty if none."""
    if not rate_row:
        return {}, {}
    return _table_index((rate_row.get("normal") or {}).get("normal") or [])


def _fixed_index(rate_row: dict | None) -> dict[str, float]:
    """{rarity: probability} of the guaranteed slot; `{}` when the banner has none.

    Every 10-pull button carries `fixed_reward_pick_count = 1`, so a
    10-draw group is 9 draws from `normal` + 1 from this table (100% 5★ on gacha-fixed-260807).
    """
    if not rate_row:
        return {}
    return _table_index((rate_row.get("normal") or {}).get("fixed") or [])[0]


def _expected(pulls: list[dict], by_rar: dict[str, float], fixed_rar: dict[str, float]) -> dict[str, float]:
    """Draws sharing a `draw_time` are one multi-draw. Every 10 of them buys one
    guaranteed-slot draw, so a group of n is (n - n//10) normal + (n//10) fixed."""
    exp: Counter = Counter()
    groups: Counter = Counter(p["draw_time"] for p in pulls)
    for n in groups.values():
        n_fixed = (n // 10) if fixed_rar else 0
        for k, v in by_rar.items():
            exp[k] += (n - n_fixed) * v
        for k, v in fixed_rar.items():
            exp[k] += n_fixed * v
    return dict(exp)


def _since_last_five(pulls: list[dict], cards_by_id: dict) -> int:
    """Pulls drawn after the last multi-draw group that produced a 5★; every pull when
    none ever did. The GROUP is the unit -- cards inside one multi-draw carry the same
    `draw_time` and their order is unknown, so a group with a 5★ resets the count to 0."""
    groups: dict[int, list[dict]] = defaultdict(list)
    for p in pulls:
        groups[p["draw_time"]].append(p)
    since = 0
    for dt in sorted(groups, reverse=True):
        if any(rarity_of(p["card_id"], cards_by_id).startswith("5") for p in groups[dt]):
            return since
        since += len(groups[dt])
    return since


def _kind_of(gacha_id: str | None, alias: dict | None) -> str:
    if alias and alias.get("kind"):
        return str(alias["kind"])
    g = gacha_id or ""
    if "-fixed-" in g:
        return "fixed"
    if "ticket" in g:
        return "ticket"
    if "pickup" in g:
        return "pickup"
    return "common"


def _rate_facts(rate_row: dict | None, kind: str) -> dict | None:
    """What a banner's saved table states, in percent; None without a table."""
    if not rate_row:
        return None
    tag = "card_select" if isinstance(rate_row.get("card_select"), dict) and \
        (rate_row["card_select"].get("normal")) else "normal"
    tables = rate_row.get(tag) or {}
    normal, fixed = tables.get("normal") or [], tables.get("fixed") or []
    if not normal:
        return None
    by_r = {t["rarity"]: t for t in normal if "rarity" in t}
    five = by_r.get(5) or {}
    cards = five.get("cards") or []
    base = min((c["ppm"] for c in cards), default=0)
    featured = sorted(({"card_id": c["card_id"], "pct": c["ppm"] / 1e5} for c in cards if c["ppm"] > base),
                      key=lambda c: -c["pct"])
    slot = {t["rarity"]: t["ppm"] / 1e5 for t in fixed if "rarity" in t}
    picked = None
    if tag == "card_select":
        # a chosen member carries the 1 % on top of the others (see the module notes)
        picked = any(abs(c["pct"] - 1.0) < 1e-9 for c in featured)
    return {"table": tag,
            "rates": {f"{r}★": by_r[r]["ppm"] / 1e5 for r in sorted(by_r, reverse=True)},
            "slot10": {f"{r}★": v for r, v in sorted(slot.items(), reverse=True)},
            "featured": featured, "featured_total": sum(c["pct"] for c in featured),
            "other_5_each": base / 1e5, "other_5_count": len(cards) - len(featured),
            "picked": picked, "kind": kind}


def _curve(groups: list[tuple[int, list[dict], dict, dict]], cards_by_id: dict) -> list[list]:
    """[[draws so far, ★5 so far, expected ★5 so far, variance so far], ...] per draw group, in
    time order, over the compared pulls. A group of n is (n - n//10) normal draws plus n//10
    from the 10th-slot table, as in `_expected`."""
    out, n, obs, exp, var = [], 0, 0, 0.0, 0.0
    for _dt, ps, by_rar, fixed_rar in groups:
        k = len(ps)
        n_fixed = (k // 10) if fixed_rar else 0
        for cnt, p5 in ((k - n_fixed, by_rar.get("5★", 0.0)), (n_fixed, fixed_rar.get("5★", 0.0))):
            exp += cnt * p5
            var += cnt * p5 * (1 - p5)
        n += k
        obs += sum(1 for p in ps if rarity_of(p["card_id"], cards_by_id).startswith("5"))
        out.append([n, obs, round(exp, 4), round(var, 4)])
    return out


def _gaps(pulls: list[dict], cards_by_id: dict) -> dict:
    """Pulls from one ★5 draw group to the next, over every pull in time order. Inside a group
    the order is unknown, so a group holding a ★5 closes the gap with all its draws."""
    groups: dict[int, list[dict]] = defaultdict(list)
    for p in pulls:
        groups[p["draw_time"]].append(p)
    gaps, run = [], 0
    for dt in sorted(groups):
        run += len(groups[dt])
        if any(rarity_of(p["card_id"], cards_by_id).startswith("5") for p in groups[dt]):
            gaps.append(run)
            run = 0
    return {"gaps": gaps, "open": run}


def _cards_block(pulls: list[dict], cards_by_id: dict, pickups: set[str], by_card_rate: dict) -> list[dict]:
    cnt = Counter(p["card_id"] for p in pulls)
    rows = [{"card_id": cid, "count": n, "rarity": rarity_of(cid, cards_by_id),
             "is_pickup": cid in pickups, "rate_ppm": by_card_rate.get(cid)} for cid, n in cnt.items()]
    rows.sort(key=lambda r: (-int(r["rarity"][0]) if r["rarity"][0].isdigit() else 0, -r["count"], r["card_id"]))
    return rows


_ART_EXTS = ("png", "webp")   # png = the masters already committed, webp = everything new


def _art_ext(art_dir: Path, gid: str) -> dict[str, str | None]:
    """{kind: "png"|"webp"|None} -- WHICH file is on disk. `png` is checked first so an
    existing master keeps winning; new banners are saved as `webp` (gacha_art.MAX_W)."""
    out: dict[str, str | None] = {}
    for k in ("banner", "logo", "icon"):
        out[k] = next((e for e in _ART_EXTS if (art_dir / gid / f"{k}.{e}").is_file()), None)
    return out


def _tutorial_block(snapshot: dict | None, pulls: list[dict], cards_by_id: dict) -> dict | None:
    """Owned cards whose id never appears in the tracked pulls -- acquired before the
    window or from a source the history does not report (the tutorial's guaranteed pull).
    `None` when no snapshot was given: absence stays first-class, nothing is guessed."""
    if not snapshot:
        return None
    owned = [c for c in (snapshot.get("cards") or []) if c.get("card_id")]
    # `earliest` over the cards that actually HAVE a timestamp: one `acquired: null` would
    # otherwise collapse the origin to 0 and flag the null card while un-flagging the real
    # first batch. A card without `acquired` gets `is_tutorial: None` -- unknown, not False.
    stamps = [c["acquired"] for c in owned if isinstance(c.get("acquired"), (int, float))]
    earliest = min(stamps) if stamps else None
    pulled = {p["card_id"] for p in pulls}
    rows = [{"card_id": c["card_id"], "rarity": rarity_of(c["card_id"], cards_by_id),
             "acquired": c.get("acquired"),
             "is_tutorial": None if (earliest is None or not isinstance(c.get("acquired"), (int, float)))
             else c["acquired"] - earliest <= _TUTORIAL_WINDOW_MS}
            for c in owned if c["card_id"] not in pulled]
    rows.sort(key=lambda r: (r["acquired"] or 0, r["card_id"]))
    return {"owned_not_tracked": rows}


def build_view(store: dict, cards_by_id: dict, art_dir: Path, snapshot: dict | None = None) -> dict:
    pulls, banners, rates, counters = (store.get(k, []) for k in _FILES)
    aliases = {k: v for k, v in (store.get("aliases") or {}).items() if k != "_doc"}
    alias_by_id = {v["gacha_id"]: (k, v) for k, v in aliases.items() if v.get("gacha_id")}
    latest_banner = _latest_by_id(banners)
    first_seen = {}
    for b in banners:
        first_seen.setdefault(b["gacha_id"], b["seen_at"])
    latest_rate = _latest_by_id(rates)
    latest_counter = _latest_by_id(counters)
    last_sync = max([r.get("synced_at", 0) for r in rates + counters] + [b.get("seen_at", 0) for b in banners]
                    + [p.get("first_seen", 0) for p in pulls] + [0])
    last_counter_sync = max([c.get("synced_at", 0) for c in counters] + [0])

    # A pull the server left unattributed is attributed to the hand-maintained alias id,
    # so a CLOSED banner still shows as a banner (with its art) instead of "unidentified".
    by_banner: dict[str | None, list[dict]] = defaultdict(list)
    alias_pulls: dict[str, list[dict]] = defaultdict(list)
    by_time = 0
    for p in pulls:
        gid = p.get("gacha_id")
        if gid is None:
            # a banner closed by the next sync: its name and window, when exactly one fits
            fit = [g for g, b in latest_banner.items()
                   if b.get("name") == p.get("gacha_name") and b.get("start_time")
                   and b["start_time"] <= p["draw_time"] <= (b.get("end_time") or float("inf"))]
            if len(fit) == 1:
                gid = fit[0]
                by_time += 1
        if gid is None and p.get("gacha_name") in aliases:
            gid = aliases[p["gacha_name"]]["gacha_id"]
            alias_pulls[gid].append(p)
        by_banner[gid].append(p)

    total_rar = Counter(rarity_of(p["card_id"], cards_by_id) for p in pulls)
    out_banners = []
    for gid in list(latest_banner) + [g for g in alias_pulls if g not in latest_banner]:
        b = latest_banner.get(gid)
        alias_name, alias = alias_by_id.get(gid, (None, None))
        if b is None:                                  # alias-only: the store never saw it live
            b = {"name": alias_name, "pickup_card_ids": []}
        elif aliases.get(b.get("name"), {}).get("gacha_id") == gid:
            # By NAME only when the alias points at THIS id: two banners can share a name
            # across seasons, and inheriting a `ticket` kind from a namesake would silently
            # drop a live banner's pulls out of the comparison.
            alias = aliases[b["name"]]
        bp = by_banner.get(gid, [])
        rate_row = latest_rate.get(gid)
        by_rar, by_card = _rate_index(rate_row)
        exp = _expected(bp, by_rar, _fixed_index(rate_row))
        kind = _kind_of(gid, alias)
        c = latest_counter.get(gid) or {}
        art_ext = _art_ext(art_dir, gid)
        out_banners.append({
            "gacha_id": gid, "name": b.get("name"), "type": b.get("type"),
            "start_time": b.get("start_time"), "end_time": b.get("end_time"),
            # The permanent pool (`gacha-common-normal-001`) is
            # sent with NO start_time and NO end_time, and `is_end_time_hidden: 1` instead
            # -- that flag is the game's own way of saying "this one does not close". The
            # tab needs it to tell a permanent banner from a closed one whose dates were
            # simply never captured; both arrive here as a pair of nulls otherwise.
            "is_end_time_hidden": bool((b.get("raw") or {}).get("is_end_time_hidden")),
            "pickup_card_ids": list(b.get("pickup_card_ids") or []),
            "art": {k: v is not None for k, v in art_ext.items()},
            "art_ext": art_ext,
            "pulls": len(bp),
            "by_rarity": dict(Counter(rarity_of(p["card_id"], cards_by_id) for p in bp)),
            "expected_by_rarity": exp,
            "pulls_since_last_5": _since_last_five(bp, cards_by_id),
            "cards": _cards_block(bp, cards_by_id, set(b.get("pickup_card_ids") or []), by_card),
            "has_rates": bool(by_rar), "first_seen": first_seen.get(gid),
            "rate_facts": _rate_facts(rate_row, kind),
            # the 1-pull button's price as the game sent it (Blue/Red Diamonds)
            "cost_per_pull": next((c.get("quantity") for x in ((b.get("raw") or {}).get("gacha_buttons") or [])
                                   if x.get("total_reward_pick_count") == 1
                                   for c in (x.get("consumptions") or [])[:1]), None),
            "is_live_last_sync": c.get("synced_at") == last_counter_sync and bool(c),
            "kind": kind, "excluded_from_rates": kind in _EXCLUDED_KINDS,
            "is_alias": gid in alias_pulls and gid not in latest_banner,
            "counters": {"gacha_point": ((c.get("gacha_point") or {}).get("quantity")),
                         "drawn_count_total": sum((x.get("drawn_count") or 0) for x in c.get("buttons", []))},
        })
    out_banners.sort(key=lambda b: -(b["start_time"] or 0))

    # The observed-vs-expected comparison: only banners whose rate table was actually saved,
    # and never the ticket/tutorial pulls -- those are tracked but bought with no rate roll.
    covered = [b for b in out_banners if b["has_rates"] and not b["excluded_from_rates"]]
    expected_total: Counter = Counter()
    observed: Counter = Counter()
    for b in covered:
        expected_total.update(b["expected_by_rarity"])
        observed.update(b["by_rarity"])
    excluded = [{"gacha_name": b["name"], "kind": b["kind"], "pulls": b["pulls"]}
                for b in out_banners if b["excluded_from_rates"]]
    # Everything else: banners with no saved rate table (a closed banner the server
    # will not serve rates for) plus pulls still not attributed to any banner. Reported so
    # covered + excluded + uncovered == totals.pulls and no pull can go missing unnoticed.
    uncovered = sum(b["pulls"] for b in out_banners
                    if not b["excluded_from_rates"] and not b["has_rates"])
    uncovered += len(by_banner.get(None, []))
    comparison = {"covered_pulls": sum(b["pulls"] for b in covered),
                  "observed_by_rarity": dict(observed),
                  "expected_by_rarity": dict(expected_total),
                  "excluded_pulls": sum(x["pulls"] for x in excluded),
                  "uncovered_pulls": uncovered,
                  "excluded": excluded}

    # the compared pulls, draw group by draw group in time order, each with its banner's rates
    rated = {b["gacha_id"]: _rate_index(latest_rate.get(b["gacha_id"]))[0] for b in covered}
    fixed_of = {b["gacha_id"]: _fixed_index(latest_rate.get(b["gacha_id"])) for b in covered}
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for b in covered:
        for p in by_banner.get(b["gacha_id"], []):
            grouped[(p["draw_time"], b["gacha_id"])].append(p)
    curve = _curve([(dt, ps, rated[g], fixed_of[g]) for (dt, g), ps in sorted(grouped.items())],
                   cards_by_id)

    unidentified = []
    by_name: dict[str, list[dict]] = defaultdict(list)
    for p in by_banner.get(None, []):
        by_name[p["gacha_name"]].append(p)
    for name, ps in by_name.items():
        unidentified.append({"gacha_name": name, "pulls": len(ps),
                             "by_rarity": dict(Counter(rarity_of(p["card_id"], cards_by_id) for p in ps)),
                             "cards": _cards_block(ps, cards_by_id, set(), {})})
    unidentified.sort(key=lambda u: -u["pulls"])

    days: dict[str, dict] = {}
    for p in pulls:
        day = _dt.datetime.fromtimestamp(p["draw_time"] / 1000, _dt.timezone.utc).strftime("%Y-%m-%d")
        d = days.setdefault(day, {"day": day, "pulls": 0, "five_star": 0})
        d["pulls"] += 1
        if rarity_of(p["card_id"], cards_by_id).startswith("5"):
            d["five_star"] += 1

    return {"last_sync": last_sync,
            # Absent when the file parsed (or was absent, which is not an error): the tab
            # only shows the note when there IS something the user has to go fix.
            "aliases_error": store.get("aliases_error"),
            "totals": {"pulls": len(pulls), "by_rarity": dict(total_rar)},
            "expected_by_rarity": comparison["expected_by_rarity"],   # kept for older readers of the view
            "comparison": comparison,
            "tutorial": _tutorial_block(snapshot, pulls, cards_by_id),
            "banners": out_banners, "unidentified": unidentified,
            "attributed_by_time": by_time,
            "curve": curve, "gaps": _gaps(pulls, cards_by_id),
            "timeline": [days[k] for k in sorted(days)]}
