"""The Park's Free Chats: the catalogue (master data) and the account's read history.

A Free Chat is a conversation between two to four holomems standing at one spot of the Park;
the game counts them per character in the achievement "Read {0} kind(s) of Free Chat"
(`mission-achievement-<chr>-absolute_park_talk_free_unique_count`, progress capped at the last
threshold, 10).

THE CATALOGUE (master)
    ParkTalkFreeGrouping   one site of an area: ratio, is_force_pick, condition
    ParkTalkFree           one row the site can show: target mapping, ratio, own condition
    ParkTargetMapping      who stands there (park_npc_group_id), what they say
                           (park_talk_group_id -> ParkTalk lines) and where (park_position_id)
Most rows are holomems standing in the background. The client gives a row the TALK action only
when its mapping names a talk group and that talk's first line is not a speech balloon
(ParkSpawnDataExtension.GetActionType: ParkTargetMapping.parkTalkGroupId non-empty, and
ParkTalk.isBalloon false), so only those rows are conversations the player can read.

THE ACCOUNT (User/Get, no extra request)
    UserParkTalkFreePickResult        (area, grouping): the sites the server picked for the
                                      area the account stands in; re-picked on every login
    UserParkTalkFreePickResultDetail  (grouping, park_talk_free_id, already_read_character_ids)
                                      every row of each picked site, with the characters it was
                                      already read with
`already_read_character_ids` is permanent (a chat that comes back carries the same marks months
later; reading credits the whole cast), but each login shows only the picked sites of the
current area. So the refresh merges every response into a log (`dumps/park_free_chat_log.json`)
and the page reads the log: a chat is READ once marks were seen, PENDING when it was last seen
without marks, and UNSEEN when no response has carried it yet.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

LOG_VERSION = 1
# The log keeps the last observations (when, which area, how many sites) for the page's
# provenance line; the per-chat state never ages out.
MAX_OBSERVATIONS = 200

# An interior area has no map of its own: its Free Chats are drawn on the building that leads
# into it, the facility of the same name in the parent area (area-a01-multiplay -> facility
# facility-a01-multiplay, which stands in area-a01).
_INTERIOR = re.compile(r"^area-(a\d+)-(\w+)$")


# --------------------------------------------------------------------------- catalogue


def _first_line(lines: list[Mapping]) -> Mapping | None:
    return min(lines, key=lambda d: d.get("number", 0)) if lines else None


def interactive_rows(
    talk_free: Iterable[Mapping], mappings: Mapping[str, Mapping], talks: Mapping[str, list[Mapping]]
) -> list[Mapping]:
    """The ParkTalkFree rows the client lets the player read: the mapping has a talk group and
    the talk's first line is not a balloon."""
    out = []
    for f in talk_free:
        tm = mappings.get(f.get("parkTargetMappingId") or "") or {}
        group = tm.get("parkTalkGroupId")
        first = _first_line(talks.get(group or "", []))
        if group and first is not None and not first.get("isBalloon"):
            out.append(f)
    return out


def cast_of(npc_group: list[Mapping], npc_character: Mapping[str, str]) -> list[str]:
    """Characters of a ParkNpcGroup, slot by slot (`number`), each slot's candidates in order."""
    out: list[str] = []
    for row in sorted(npc_group, key=lambda r: r.get("number", 0)):
        c = npc_character.get(row.get("parkNpcId") or "")
        if c and c not in out:
            out.append(c)
    return out


def site_of(position: Mapping, facilities: Mapping[str, Mapping]) -> tuple[str | None, bool]:
    """(facility id the site belongs to, inside a building?) for a ParkPosition row."""
    if position.get("facilityId"):
        return position["facilityId"], False
    m = _INTERIOR.match(position.get("areaId") or "")
    if m:
        fid = f"facility-{m.group(1)}-{m.group(2)}"
        if fid in facilities:
            return fid, True
    return None, False


def quest_label(quest_id: str) -> str:
    """quest-main-w1-03-10 -> 'Main quest 3-10'."""
    m = re.match(r"^quest-main-w\d+-(\d+)-(\d+)$", quest_id)
    return f"Main quest {int(m.group(1))}-{int(m.group(2))}" if m else quest_id


def condition_text(
    rows: list[Mapping],
    *,
    quest_titles: Mapping[str, str],
    facility_names: Mapping[str, str],
    all_rows: Mapping[str, list[Mapping]],
    depth: int = 0,
) -> str:
    """One condition group in words, in the game's terms (quest titles, facility names)."""
    parts = []
    for r in sorted(rows, key=lambda r: r.get("number", 0)):
        kind = (r.get("type") or "").replace("ConditionType_CONDITION_TYPE_", "")
        res = r.get("resourceId1") or ""
        if kind == "PARK_QUEST_CLEAR":
            title = quest_titles.get(res)
            step = int(r.get("min") or 0) if r.get("resourceId2") else 0
            text = quest_label(res) + (f" step {step}" if step > 1 else "") + " cleared"
            parts.append(text + (f" (“{title}”)" if title else ""))
        elif kind == "FACILITY_LEVEL":
            parts.append(f"{facility_names.get(res, res)} built")
        elif kind == "PARK_TIME_PERIOD":
            parts.append(f"{res.capitalize()} only")
        elif kind == "NEGATIVE_SET" and depth < 3 and res in all_rows:
            inner = condition_text(
                all_rows[res],
                quest_titles=quest_titles,
                facility_names=facility_names,
                all_rows=all_rows,
                depth=depth + 1,
            )
            parts.append("until " + inner.replace(" cleared", "", 1))
        else:
            parts.append(kind.replace("_", " ").lower() + (f" {res}" if res else ""))
    return ", ".join(parts)


def _render(tables: Iterable[str]) -> dict[str, list[dict]]:
    """Named master rows ({table: [row data]}) from the local master (master_json)."""
    import master_json  # noqa: PLC0415  (needs the master; refresh side only)

    renderer = master_json.Renderer(master_json.load_schema())
    return {t: [r["data"] for r in master_json.render(t, renderer)] for t in tables}


def built_appearance(rows: Iterable[Mapping]) -> dict[str, Mapping]:
    """facility id -> the FacilityAppearance the map draws for it once built: the
    highest-priority row that names a map image (`mapImageAssetId`, also the stem of the
    facility's map piece `mdl_map_facility_<id>` and photo `img_park_facility_map_thumb_<id>`)."""
    out: dict[str, Mapping] = {}
    for a in sorted(rows, key=lambda a: a.get("priority", 0)):
        if a.get("mapImageAssetId"):
            out[a["facilityId"]] = a
    return out


TABLES = (
    "ParkTalkFreeGrouping",
    "ParkTalkFree",
    "ParkTargetMapping",
    "ParkTalk",
    "ParkNpcGroup",
    "ParkNpc",
    "SdCostume",
    "ParkPosition",
    "Facility",
    "LangFacility_Eng",
    "FacilityAppearance",
    "Condition",
    "LangParkQuest_Eng",
    "Character",
)


def build_catalogue(m: Mapping[str, list[dict]] | None = None) -> dict[str, Any]:
    """The Free Chat catalogue from master: chats, the sites they stand at, every condition in
    words and the characters' names. No positions: park_export adds the map pixels."""
    m = m if m is not None else _render(TABLES)
    groupings = {g["groupId"]: g for g in m["ParkTalkFreeGrouping"]}
    mappings = {t["id"]: t for t in m["ParkTargetMapping"]}
    talks: dict[str, list[dict]] = {}
    for line in m["ParkTalk"]:
        talks.setdefault(line["groupId"], []).append(line)
    npc_groups: dict[str, list[dict]] = {}
    for row in m["ParkNpcGroup"]:
        npc_groups.setdefault(row["id"], []).append(row)
    sd_character = {s["id"]: s.get("characterId") for s in m["SdCostume"]}
    npc_character = {}
    for n in m["ParkNpc"]:
        sd = n.get("sdCostumeId") or ""
        found = re.match(r"^sdcos-(\d{5})", sd)
        npc_character[n["id"]] = sd_character.get(sd) or (f"chr-{found.group(1)}" if found else None)
    positions = {p["id"]: p for p in m["ParkPosition"]}
    lang_facility = {r["id"]: r.get("text") for r in m["LangFacility_Eng"]}
    facilities = {f["id"]: f for f in m["Facility"]}
    facility_names = {fid: lang_facility.get(f.get("nameLangId")) or fid for fid, f in facilities.items()}
    quest_titles = {
        r["id"][3:-6]: r.get("text")
        for r in m["LangParkQuest_Eng"]
        if r["id"].startswith("la-") and r["id"].endswith("-title")
    }
    cond_rows: dict[str, list[dict]] = {}
    for c in m["Condition"]:
        cond_rows.setdefault(c["groupId"], []).append(c)
    appearance = built_appearance(m["FacilityAppearance"])

    chats: dict[str, dict] = {}
    sites: dict[str, dict] = {}
    conditions: dict[str, str] = {}
    unplaced: list[str] = []
    for f in interactive_rows(m["ParkTalkFree"], mappings, talks):
        tm = mappings[f["parkTargetMappingId"]]
        pos = positions.get(tm.get("parkPositionId") or "")
        fid, inside = site_of(pos, facilities) if pos else (None, False)
        if fid is None:
            unplaced.append(f["id"])
            continue
        grouping = groupings.get(f["parkTalkFreeGroupingGroupId"]) or {}
        conds = [c for c in (grouping.get("conditionGroupId"), f.get("conditionGroupId")) if c]
        for c in conds:
            if c not in conditions:
                conditions[c] = (
                    condition_text(
                        cond_rows.get(c, []),
                        quest_titles=quest_titles,
                        facility_names=facility_names,
                        all_rows=cond_rows,
                    )
                    or c
                )
        chats[f["id"]] = {
            "site": fid,
            "area": pos.get("areaId"),
            "spot": pos.get("position"),
            "cast": cast_of(npc_groups.get(tm.get("parkNpcGroupId") or "", []), npc_character),
            "conditions": conds,
            "talk": tm["parkTalkGroupId"],
        }
        if fid not in sites:
            app = appearance.get(fid) or {}
            sites[fid] = {
                "name": facility_names.get(fid, fid),
                "area": facilities.get(fid, {}).get("areaId"),
                "inside": pos.get("areaId") if inside else None,
                "map_image": app.get("mapImageAssetId"),
                "icon": (app.get("iconAssetIds") or [None])[0],
            }
    names = {
        c["id"]: {
            "name": c.get("nameEng") or c["id"],
            "short": c.get("shortNameEng") or c.get("nameEng") or c["id"],
        }
        for c in m["Character"]
    }
    used = {c for ch in chats.values() for c in ch["cast"]}
    return {
        "chats": dict(sorted(chats.items())),
        "sites": dict(sorted(sites.items())),
        "conditions": dict(sorted(conditions.items())),
        "characters": {c: names.get(c, {"name": c, "short": c}) for c in sorted(used)},
        "unplaced": unplaced,
    }


# --------------------------------------------------------------------------- the account


def parse_userdata(fields_iter: Iterable[tuple[int, int, Any]], fields_fn) -> dict[str, Any]:
    """{"picks": [{area, grouping}], "details": [{grouping, talk_free, read}]} from UserData."""
    picks, details = [], []
    for fn, wt, v in fields_iter:
        if wt != 2:
            continue
        if fn == F_PICK:
            d = {f2: _text(v2) for f2, _w, v2 in fields_fn(v)}
            picks.append({"area": d.get(3), "grouping": d.get(2)})
        elif fn == F_DETAIL:
            grouping = talk_free = None
            read: list[str] = []
            for f2, _w, v2 in fields_fn(v):
                if f2 == 2:
                    grouping = _text(v2)
                elif f2 == 3:
                    talk_free = _text(v2)
                elif f2 == 4:
                    read.append(_text(v2))
            details.append({"grouping": grouping, "talk_free": talk_free, "read": read})
    return {"picks": picks, "details": details}


# Field numbers in rpc.api.common.UserData (the client's own .proto).
F_PICK = 103534744  # user_park_talk_free_pick_result_list
F_DETAIL = 498625501  # user_park_talk_free_pick_result_detail_list


def _text(b):
    return b.decode("utf-8", "replace") if isinstance(b, (bytes, bytearray)) else b


def merge_log(
    log: Mapping | None, free_chat: Mapping, at: str, keep: set[str] | None = None
) -> dict[str, Any]:
    """The log with one more User/Get folded in. Never forgets: read marks only grow, a chat
    once read stays read. `keep` (the catalogue's chat ids) drops the background rows; without
    it every row that carries marks or is new is kept.

    Per chat: `read` (characters), `seen` (last time a response carried it) and `read_seen`
    (the earliest response that carried marks on it). Responses may arrive in any order."""
    out = {"version": LOG_VERSION, "chats": {}, "observations": []}
    if log:
        # rows kept before a catalogue existed: background rows (never read) go once it does
        out["chats"] = {
            k: dict(v)
            for k, v in (log.get("chats") or {}).items()
            if keep is None or k in keep or v.get("read")
        }
        out["observations"] = list(log.get("observations") or [])
    chats = out["chats"]
    kept = 0
    for d in free_chat.get("details") or []:
        cid = d.get("talk_free")
        if not cid or (keep is not None and cid not in keep):
            continue
        kept += 1
        c = chats.setdefault(cid, {"read": [], "seen": None, "read_seen": None})
        marks = set(d.get("read") or [])
        c["read"] = sorted(set(c.get("read") or []) | marks)
        if marks and (not c.get("read_seen") or _ts(at) < _ts(c["read_seen"])):
            c["read_seen"] = at
        if _ts(at) > _ts(c.get("seen")):
            c["seen"] = at
    areas = sorted({p.get("area") for p in free_chat.get("picks") or [] if p.get("area")})
    out["observations"].append(
        {"at": at, "areas": areas, "sites": len(free_chat.get("picks") or []), "chats": kept}
    )
    out["observations"] = sorted(out["observations"], key=lambda o: _ts(o["at"]))[-MAX_OBSERVATIONS:]
    out["chats"] = dict(sorted(chats.items()))
    return out


def _ts(iso: str | None) -> float:
    """An ISO time with its offset as epoch seconds (offsets differ across DST); None is -inf."""
    if not iso:
        return float("-inf")
    return datetime.datetime.fromisoformat(iso).timestamp()


def catalogue_ids(spots_path: Path) -> set[str] | None:
    """The chat ids in the exported catalogue (park_spots.json), or None when it is absent."""
    try:
        doc = json.loads(Path(spots_path).read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    chats = (doc.get("free_chat") or {}).get("chats")
    return set(chats) if chats else None


def update_log(path: Path, free_chat: Mapping, at: str, keep: set[str] | None) -> dict:
    """Merge one response into the log file at `path` (written atomically, bytes)."""
    path = Path(path)
    try:
        old = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        old = None
    new = merge_log(old, free_chat, at, keep)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes(json.dumps(new, ensure_ascii=False, indent=1).encode("utf-8"))
    os.replace(tmp, path)
    return new


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")
