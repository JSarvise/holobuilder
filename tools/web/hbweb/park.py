"""The Park view: `GET /api/park` (today's hidden collectables joined to the offline
export) and `GET /park/<area>.png` (the area maps).

Two halves: the geometry (`park_spots.json` + the maps, the offline export of
`park_export.py`) and the day's pick (`dumps/park_state.json`, written by the account
refresh from its own `User/Get`, `account_snapshot.write_park_state`).
Both absences are first-class `available: false` states with the command that fixes
them, never a 500 and never a silent empty list.

The page never logs in itself: a login from the app ends the game's own session on the
account. The symbol pick is frozen for the whole game-day once rolled (Park/Refresh does not
re-roll it), so the last refresh is right about the day; symbols collected since then show
after the next Refresh. The one login the page can start is "Scan all areas" (`scan_status`,
`admin.park_scan_route`): off unless turned on in Settings, run by `park_scan.py` in its own
process.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hbuilder import game_day
from hbweb import config

if TYPE_CHECKING:
    from hbweb.router import Handler

# `GET /park/<area>.png` -- the area id is lowercase letters, digits and
# hyphens only (`area-a01`, `area-a02-casino`). No `.` but the literal
# extension, no `/`, no `_`, so there is no traversal surface; the served path
# is re-checked for containment under `PARK_EXPORT_DIR` after resolving anyway.
# `chest.png` is the game's own chest map icon and `symbol.png` its random-symbol picture,
# exported beside the maps; `site-*.jpg` are
# the Free Chat sites' photos (the game's facility thumbnails) and `icon-*.png` their icons.
PARK_IMAGE_NAME_RE = re.compile(
    r"^((area-[a-z0-9-]+|chest|symbol|icon-[a-z_]+)\.png|site-area-[a-z0-9-]+\.jpg)$"
)

# The game's per-character Free Chat achievement: its progress is the number of distinct Free
# Chats read with that character in the cast, capped at the last threshold.
FREE_CHAT_MISSION = re.compile(r"^mission-achievement-(chr-\d+)-absolute_park_talk_free_unique_count$")
FREE_CHAT_COUNTER_CAP = 10


def park_saved_state() -> dict | None:
    """The pick the last account refresh saved (`dumps/park_state.json`), or None."""
    try:
        return json.loads(config.PARK_STATE_PATH.read_text("utf8"))
    except (OSError, ValueError):
        return None


def park_export() -> dict | None:
    """The offline export (`park_spots.json`), or None when it is absent.

    A separate seam from the live read on purpose: the export is what a fresh
    checkout lacks, and the test suite stubs this to develop the join against a
    fixture without touching disk."""
    return config.read_json(config.PARK_SPOTS_PATH)


def resolve_spot(
    mapping: str, mapping_to_spot: Mapping[str, Any], spots: Mapping[str, Any]
) -> tuple[Any, Any, Any]:
    """`(area, spot_name, {x,y})` for a mapping id.

    ⚠ ALWAYS via `mapping_to_spot`, never by synthesising a spot name from the
    mapping id: the mapping suffix is 3-digit (`...a01-082`) while the spot name
    is 2-digit (`reward_random_82`), and areas spell differently inside the two
    (`area-a02-casino` vs `...a02_casino-033`). `(None, None, None)` when the
    mapping is unknown; `(area, spot, None)` when the spot carries no pixel --
    both are REPORTED by the caller, never dropped."""
    m = mapping_to_spot.get(mapping)
    if not m:
        return None, None, None
    area = m.get("area")
    spot = m.get("spot")
    px = spots.get(spot) if spot else None
    return area, spot, px


def _join_area(
    area_id: str, ainfo: Mapping, live: Mapping | None, export: Mapping, opened: set
) -> dict[str, Any]:
    """One area of the join: its picked symbols placed on the map (or reported in
    `unresolved`), its candidate spots and its fixed chests."""
    mapping_to_spot = export.get("mapping_to_spot") or {}
    spots = ainfo.get("spots") or {}
    # Straight from THIS area's own row -- None when the area has no live
    # state (not entered since the reset, or absent from the blob).
    rolled_at = live.get("last_refreshed_time") if live else None
    picked_ids = list(live.get("picked") or []) if live else []
    collected_ids = set(live.get("collected") or []) if live else set()

    picked_out: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for mapping in picked_ids:
        m_area, spot, px = resolve_spot(mapping, mapping_to_spot, spots)
        if px is None:
            unresolved.append(
                {
                    "mapping": mapping,
                    "reason": (
                        "no entry in the export's mapping_to_spot"
                        if m_area is None
                        else f"spot {spot!r} has no pixel in {area_id}"
                    ),
                }
            )
            continue
        picked_out.append(
            {
                "mapping": mapping,
                "spot": spot,
                "x": px.get("x"),
                "y": px.get("y"),
                # `collected` is per-symbol and comes from THIS area's
                # collected list, so it is a strict subset of `picked`.
                "collected": mapping in collected_ids,
            }
        )

    candidates_out: list[dict[str, Any]] = []
    for mapping in (export.get("candidates") or {}).get(area_id) or []:
        _a, spot, px = resolve_spot(mapping, mapping_to_spot, spots)
        if px is not None:
            candidates_out.append({"spot": spot, "x": px.get("x"), "y": px.get("y")})

    treasures_out: list[dict[str, Any]] = []
    for tid, tinfo in sorted((export.get("treasures") or {}).items()):
        if (tinfo or {}).get("area") != area_id:
            continue
        spot = (tinfo or {}).get("spot")
        px = spots.get(spot) if spot else None
        treasures_out.append(
            {
                "id": tid,
                "spot": spot,
                "x": (px or {}).get("x"),
                "y": (px or {}).get("y"),
                "opened": tid in opened,
            }
        )

    return {
        "id": area_id,
        "name": ainfo.get("name") or area_id,
        "image": f"/park/{area_id}.png",
        "width": ainfo.get("width"),
        "height": ainfo.get("height"),
        "rolled_at": rolled_at,
        "picked": picked_out,
        "unresolved": unresolved,
        "candidates": candidates_out,
        "treasures": treasures_out,
    }


def join_park(export: Mapping[str, Any], state: Mapping[str, Any]) -> dict:
    """The export's geometry joined to today's live pick.

    ⚠ Each area carries ITS OWN `rolled_at` (`last_refreshed_time`), never a
    park-wide one. A capture caught the casino a full day behind the
    other two, so a single broadcast timestamp would be a lie on exactly the day
    it matters. An area with no live row gets `rolled_at: null` -- it is NOT
    filled from another area.

    ⚠ A picked symbol that cannot be placed is REPORTED in the area's
    `unresolved` list (and counted at the top), never dropped: a silently
    missing marker is a treasure the player walks past. Every picked id is drawn
    from the candidate pool `mapping_to_spot` holds, so a miss should not happen
    -- this path is the guard that makes it loud if that assumption ever breaks.

    ⚠ Three of the fixed chests (`-008/-018/-014`, in a01-house/-restaurant/-theater; 70
    chests in all since Soda Pop Lagoon) have NO map asset and no `areas` entry, so they
    cannot be placed.
    They are skipped explicitly and counted in `unplaceable_treasures` rather
    than falling out of the join unnoticed."""
    export_areas = export.get("areas") or {}
    all_treasures = export.get("treasures") or {}
    random_state = state.get("random") or {}
    opened = set(state.get("opened_treasures") or [])

    areas_out = [
        _join_area(area_id, export_areas[area_id] or {}, random_state.get(area_id), export, opened)
        for area_id in sorted(export_areas)
    ]
    unresolved_total = sum(len(a["unresolved"]) for a in areas_out)

    # The three chests in areas with no map (see docstring) -- counted so a
    # treasure list of 39 is explained rather than mysteriously short.
    unplaceable_treasures = sum(
        1 for t in all_treasures.values() if (t or {}).get("area") not in export_areas
    )
    # A live area the export does not know about must not vanish silently.
    unmapped_areas = sorted(a for a in random_state if a not in export_areas)

    return {
        "available": True,
        "note": export.get("note") or "",
        "generated_at": export.get("generated_at"),
        "areas": areas_out,
        "unresolved_count": unresolved_total,
        "unplaceable_treasures": unplaceable_treasures,
        "unmapped_areas": unmapped_areas,
    }


def free_chat_counters(snapshot: Mapping | None) -> dict[str, int]:
    """chr id -> the game's Free Chat achievement progress, from the account snapshot."""
    out: dict[str, int] = {}
    for m in (snapshot or {}).get("missions") or []:
        hit = FREE_CHAT_MISSION.match(m.get("mission_id") or "")
        if hit:
            out[hit.group(1)] = int(m.get("progress") or 0)
    return out


def join_free_chat(fc: Mapping | None, log: Mapping | None, counters: Mapping[str, int]) -> dict[str, Any]:
    """The exported Free Chat catalogue joined to the account's read log.

    Status per chat: `read` once any response carried read marks on it (marks are permanent),
    `pending` when the latest response that carried it had no marks, `unseen` when no
    response has carried it yet (each login shows only the picked sites of the area the
    account stands in, so a site can go unobserved for a while)."""
    if not fc or not fc.get("chats"):
        return {
            "available": False,
            "reason": (
                f"the Free Chat catalogue is not in {config.PARK_SPOTS_PATH.name}: rebuild it "
                "with tools/refresh/park_export.py"
            ),
        }
    seen = (log or {}).get("chats") or {}
    chats = []
    for cid, c in fc["chats"].items():
        s = seen.get(cid) or {}
        status = "read" if s.get("read") else "pending" if s.get("seen") else "unseen"
        chats.append(
            {
                "id": cid,
                "site": c["site"],
                "x": c["x"],
                "y": c["y"],
                "cast": c["cast"],
                "conditions": [fc.get("conditions", {}).get(k, k) for k in c.get("conditions", [])],
                "status": status,
                "seen": s.get("seen"),
                "read_seen": s.get("read_seen"),
            }
        )

    def tally(rows):
        out = {"total": len(rows), "read": 0, "pending": 0, "unseen": 0}
        for r in rows:
            out[r["status"]] += 1
        return out

    sites = []
    for fid, s in fc.get("sites", {}).items():
        mine = [c for c in chats if c["site"] == fid]
        sites.append(
            {
                "id": fid,
                "name": s["name"],
                "map": s["map"],
                "x": s["x"],
                "y": s["y"],
                "inside": bool(s.get("inside")),
                "photo": f"/park/{s['photo'][len('park-') :]}" if s.get("photo") else None,
                "icon": f"/park/{s['icon_image'][len('park-') :]}" if s.get("icon_image") else None,
                **tally(mine),
            }
        )
    sites.sort(key=lambda s: (-(s["pending"] + s["unseen"]), s["name"]))
    characters = []
    for cid, names in fc.get("characters", {}).items():
        mine = [c for c in chats if cid in c["cast"]]
        counter = counters.get(cid)
        characters.append(
            {
                "id": cid,
                "name": names.get("name"),
                "short": names.get("short"),
                **tally(mine),
                # the game's own count; None for a character the game gives no achievement
                "game_read": counter,
                "game_capped": counter is not None and counter >= FREE_CHAT_COUNTER_CAP,
            }
        )
    obs = (log or {}).get("observations") or []
    return {
        "available": True,
        **tally(chats),
        "sites": sites,
        "chats": chats,
        "characters": characters,
        "observations": len(obs),
        "first_seen": obs[0]["at"] if obs else None,
        "last_seen": obs[-1]["at"] if obs else None,
        "areas_seen": sorted({a for o in obs for a in o.get("areas") or []}),
    }


def park_free_chat() -> dict[str, Any]:
    """`join_free_chat` of what is on disk (export, log, account snapshot)."""
    export = park_export() or {}
    return join_free_chat(
        export.get("free_chat"),
        config.read_json(config.PARK_FREE_CHAT_LOG_PATH),
        free_chat_counters(config.read_json(config.SNAPSHOT_PATH)),
    )


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


SCAN_LAST_KEYS = ("finished_at", "counted", "original", "areas", "skipped", "restored", "stopped")


def scan_status(now: datetime.datetime | None = None) -> dict[str, Any]:
    """What the page needs for "Scan all areas": whether it is on, whether this game day's
    scan is used (a scan counts once it entered an area, as `park_scan` rules), when the next
    one opens, and what the last one did (`park_scan_last.json`)."""
    now = now or datetime.datetime.now(datetime.UTC)
    hour = game_day.reset_hour()
    last = config.read_json(config.PARK_SCAN_LAST_PATH)
    done = bool(
        last and last.get("counted") and last.get("game_day_start") == game_day.day_start_ms(now, hour)
    )
    return {
        "enabled": os.environ.get(config.PARK_SCAN_ENV) == "1",
        "done_today": done,
        "next_at": game_day.next_day_start_ms(now, hour),
        "last": {k: last.get(k) for k in SCAN_LAST_KEYS} if isinstance(last, dict) else None,
    }


def serve_park(h: Handler) -> None:
    """`GET /api/park`: today's hidden collectables, joined to the offline export.
    The pick is the last account refresh's (`park_saved_state`); this route never logs in."""
    export = park_export()
    if export is None:
        h.reply_json(
            200,
            {
                "available": False,
                "reason": (
                    f"{config.PARK_SPOTS_PATH.name} is not on disk (it lives under "
                    f"gitignored dumps/). Build it with "
                    f"tools/refresh/park_export.py."
                ),
                "areas": [],
                "scan": scan_status(),
                "game_day_start": game_day.day_start_ms(_now()),
            },
        )
        return
    state = park_saved_state()
    if state is None:
        h.reply_json(
            200,
            {
                "available": False,
                "reason": (
                    "today's park pick is read by the account refresh: press Refresh "
                    f"(it writes {config.PARK_STATE_PATH.name})."
                ),
                # the maps still travel: the Free Chat section draws on them from its own log
                "areas": join_park(export, {})["areas"],
                "free_chat": park_free_chat(),
                "scan": scan_status(),
                "game_day_start": game_day.day_start_ms(_now()),
            },
        )
        return
    out = join_park(export, state)
    out["captured_at"] = state.get("_captured_at")
    out["free_chat"] = park_free_chat()
    out["scan"] = scan_status()
    # the page marks a symbol roll from before this as stale; the hour is the master's
    out["game_day_start"] = game_day.day_start_ms(_now())
    h.reply_json(200, out)


def serve_park_image(h: Handler, name: str) -> None:
    """`GET /park/<area>.png`: a RAW area map, from `dumps/park_export/park-<area>.png`.

    Guarded twice, like the other image routes: the regex forbids `..`, `/` and any
    `.` but the extension, AND the resolved path must still live under
    `PARK_EXPORT_DIR`. A missing file is a 404, never a substituted placeholder."""
    if not PARK_IMAGE_NAME_RE.match(name):
        h.reply(400, b"bad park image name", "text/plain; charset=utf-8")
        return
    # `park-` prefix: the URL carries the area id (`area-a01.png`), the file
    # is `park-area-a01.png` -- the export's own filename convention.
    path = config.PARK_EXPORT_DIR / f"park-{name}"
    if not path.is_file() or config.PARK_EXPORT_DIR.resolve() not in path.resolve().parents:
        h.reply(404, b"no park image", "text/plain; charset=utf-8")
        return
    ctype = "image/jpeg" if name.endswith(".jpg") else "image/png"
    h.reply(200, path.read_bytes(), ctype, cache="max-age=3600")
