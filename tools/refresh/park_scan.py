"""Scan every Park area that has Free Chats: enter each one as the game does, read its chats,
and go back to the area the account started in.

`Park/Refresh {area_id}` is the call the game makes when the player walks into an area or a
building (`ParkUtility.CallChangeAreaApiAsync`). The server answers it with that area's picked
sites and the read marks of every chat there, moves the account into the area
(`UserPark.current_area_id`) and keeps the picks of that area only. So the scan refreshes each
area with chats in turn and ends with a refresh of the area it started in: that puts the account
back and draws that area's sites again, as walking back into it does. Symbols and quests do not
change.

Guards, all checked before the first write (`Home/Login`):
  * the game is not running (a login ends its session);
  * the Park quest data on this PC (`park_export/park_moves.json`, written by the refresh)
    matches the live master;
  * an area where an open quest step wants the move is skipped: there the game sends
    `ParkQuest/MoveArea`, never `Park/Refresh` (`area_move_actions`, the client's
    `ParkQuestManager.FindChangeAreaQuest`); if that is the area the account stands in, the scan
    stops, since it could not go back;
  * the account already logged in this game day, so `Home/Login` is the repeat login the game
    itself makes at every start, not the day's first one (which settles expirations and
    renewals);
  * at most one scan per game day, and none in the 10 minutes before the rollover.
About 5 s between calls, in the game's own startup order. The area it started in is refreshed
last whatever failed before.

Writes `dumps/park_free_chat_log.json` (every area's chats, merged: read marks only grow),
`dumps/park_state.json` (the park after the scan, as the refresh writes it) and
`dumps/park_scan_last.json` (what the scan did; the once-per-day rule reads it).

The four calls the account refresh never makes (Park/Enter, Park/Refresh, Home/Login,
MultiGame/ListPingServer) join account_snapshot's whitelist in memory, in this process only,
as exact literals (`widen_whitelist`).

    python tools/refresh/park_scan.py                 # the scan
    python tools/refresh/park_scan.py --dry-run       # what it would do; opens no socket
    python tools/refresh/park_scan.py --write-moves   # park_moves.json from this PC's master
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
import time
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import game_day, paths  # noqa: E402
from hbuilder.errors import MissingInput, run_cli  # noqa: E402
sys.path.insert(0, str(paths.REFRESH_DIR))

import account_snapshot as snap  # noqa: E402
import atomic_io  # noqa: E402
import park_free_chat  # noqa: E402

DUMPS = paths.DUMPS
MOVES_PATH = DUMPS / "park_export" / "park_moves.json"
SPOTS_PATH = DUMPS / "park_export" / "park_spots.json"
LOG_PATH = DUMPS / "park_free_chat_log.json"
LAST_PATH = DUMPS / "park_scan_last.json"
INDEX_PATH = DUMPS / "master_index.json"

USER_GET = "/rpc.api.User/Get"
SYSTEM_INFO = "/rpc.api.System/GetSystemInfo"
HOME_LOGIN = "/rpc.api.Home/Login"
PING_SERVERS = "/rpc.api.MultiGame/ListPingServer"
PARK_ENTER = "/rpc.api.Park/Enter"
EVENT_INFO = "/rpc.api.Event/ListEventInfo"
PARK_REFRESH = "/rpc.api.Park/Refresh"
# Home/Login writes (last_login_time and the profile on a repeat login; the day's first one also
# settles expired resources and licence renewals), so it is sent only as a repeat login
# (`logged_in_this_game_day`). ListPingServer is a read (Empty -> the multiplayer ping servers);
# its siblings on MultiGame (GeneratePrivateRoomId, EnterPrivateRoom, CancelMatch...) and the
# Park service's other calls (SelectArea, ReadTalkFree, CollectRandomSymbol...) are not added.
ADDED = frozenset({PARK_ENTER, PARK_REFRESH, HOME_LOGIN, PING_SERVERS})

# ParkActionType.PARK_ACTION_TYPE_AREA_MOVE: the only step-action type the client puts in
# ParkQuestManager._areaMoveStepActionList, the set FindChangeAreaQuest reads.
AREA_MOVE = 2008
AREA_MOVE_NAME = "PARK_ACTION_TYPE_AREA_MOVE"
MOVE_TABLES = ("ParkQuestStepAction", "Area")

# rpc.api.common.UserData field numbers (user_data.gen.proto)
F_USER_PARK = 227737460            # UserPark: 3 current_area_id
F_QUEST = 296076812                # UserParkQuest: 2 park_quest_id, 3 start_time
F_QUEST_STEP = 228511112           # UserParkQuestStep: 2 id, 3 group, 4 current_step, 5 all_clear_time
F_QUEST_STEP_ACTION = 439898815    # UserParkQuestStepAction: 2 id, 3 group, 4 step,
#                                    5 park_target_mapping_id, 6 clear_time
F_USER_TIME = 165033958            # UserTime: 3 last_login_time (unix ms)
F_COMMON_RESPONSE = 9999           # a write's common_response {1 updated_data, 2 deleted_data}

GAME_EXE = "hololive-Dreams.exe"
GAP_S = 5.0
ROLLOVER_MARGIN_MIN = 10


class ScanStop(RuntimeError):
    """The scan stops here; the area is restored if it had moved, and the reason recorded."""


# ---------------------------------------------------------------- the client's quest gate
@dataclass
class QuestState:
    current_area_id: str | None = None
    quest_start: dict[str, int] = field(default_factory=dict)        # quest -> start_time
    steps: list[tuple[str, int, int, int]] = field(default_factory=list)
    action_clear: dict[tuple[str, int, int, str], int] = field(default_factory=dict)


def _text(v: Any) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)


def parse_quest_state(userdata_fields: Iterable[tuple[int, int, Any]]) -> QuestState:
    """UserData fields -> the account's area and quest progress. proto3 leaves zero values
    out, so an absent int is 0 and an absent string is ''."""
    st = QuestState()
    for fn, wt, v in userdata_fields:
        if wt != 2:
            continue
        if fn == F_USER_PARK:
            for f2, _w, v2 in snap.fields(v):
                if f2 == 3:
                    st.current_area_id = _text(v2)
        elif fn == F_QUEST:
            d = {f2: v2 for f2, _w, v2 in snap.fields(v)}
            st.quest_start[_text(d.get(2, b""))] = int(d.get(3, 0))
        elif fn == F_QUEST_STEP:
            d = {f2: v2 for f2, _w, v2 in snap.fields(v)}
            st.steps.append((_text(d.get(2, b"")), int(d.get(3, 0)), int(d.get(4, 0)),
                             int(d.get(5, 0))))
        elif fn == F_QUEST_STEP_ACTION:
            d = {f2: v2 for f2, _w, v2 in snap.fields(v)}
            key = (_text(d.get(2, b"")), int(d.get(3, 0)), int(d.get(4, 0)), _text(d.get(5, b"")))
            st.action_clear[key] = int(d.get(6, 0))
    return st


def step_all_cleared(step: tuple[str, int, int, int], quest_start: dict[str, int]) -> bool:
    """`UserParkQuestStep.IsAllCleared`: the quest's start_time and the step's all_clear_time are
    both set and start_time <= all_clear_time. A step whose quest the account does not hold
    counts as open (the client never meets that case), which keeps its actions in the gate."""
    quest, _group, _cur, all_clear = step
    start = quest_start.get(quest)
    if start is None:
        return False
    return start != 0 and all_clear != 0 and start <= all_clear


def action_cleared(key: tuple[str, int, int, str], action_clear: dict, quest_start: dict) -> bool:
    """`ParkQuestStepAction.IsCleared`: the account's own record of the action has a clear_time,
    and that clear belongs to the current run of the quest (start_time <= clear_time)."""
    clear = action_clear.get(key, 0)
    if not clear:
        return False
    start = quest_start.get(key[0])
    if start is None:
        return False
    return start <= clear


def is_area_move(action_type: Any) -> bool:
    if isinstance(action_type, int):
        return action_type == AREA_MOVE
    if isinstance(action_type, str):
        return action_type == AREA_MOVE_NAME or action_type.endswith("_" + AREA_MOVE_NAME)
    raise ValueError(f"unreadable ParkActionType {action_type!r}")


def area_move_actions(area_id: str, state: QuestState, step_actions: list[dict]) -> list[dict]:
    """The quest step actions that would make the client send `ParkQuest/MoveArea` instead of
    `Park/Refresh` for a move to `area_id`.

    The client builds `_areaMoveStepActionList` from every UserParkQuestStep that is not
    all-cleared: the master's ParkQuestStepAction rows of that step's CURRENT step (key: quest,
    step group, current step) of type AREA_MOVE and not yet cleared; `FindChangeAreaQuest(area)`
    then yields those whose areaId is the area and that are still not cleared. The client also
    drops, before that, actions whose clear condition is already satisfied or whose mission is
    already cleared (it clears them itself); this keeps them, so the result is the client's set
    or a superset of it: it never misses a MoveArea.

    `step_actions` are master rows, `{data: record}` or the record itself (camelCase)."""
    by_step: dict[tuple[str, int, int], list[dict]] = defaultdict(list)
    for row in step_actions:
        d = row.get("data", row)
        by_step[(d.get("parkQuestId", ""), int(d.get("stepGroupNumber", 0)),
                 int(d.get("step", 0)))].append(d)
    out = []
    for step in state.steps:
        if step_all_cleared(step, state.quest_start):
            continue
        quest, group, current, _all_clear = step
        for d in by_step.get((quest, group, current), []):
            if not is_area_move(d.get("type")) or d.get("areaId") != area_id:
                continue
            key = (quest, group, current, d.get("parkTargetMappingId", ""))
            if action_cleared(key, state.action_clear, state.quest_start):
                continue
            out.append(d)
    return out


def gate_detail(state: QuestState, step_actions: list[dict]) -> list[dict]:
    """Every AREA_MOVE row with the account's side of it: why each one gates its area or not."""
    steps = {(q, g): (cur, allc) for q, g, cur, allc in state.steps}
    out = []
    for row in step_actions:
        d = row.get("data", row)
        if not is_area_move(d.get("type")):
            continue
        q, g, s = d.get("parkQuestId", ""), int(d.get("stepGroupNumber", 0)), int(d.get("step", 0))
        cur, allc = steps.get((q, g), (None, None))
        key = (q, g, s, d.get("parkTargetMappingId", ""))
        out.append({"quest": q, "group": g, "step": s, "area": d.get("areaId"),
                    "quest_start": state.quest_start.get(q), "current_step": cur,
                    "all_clear_time": allc, "action_clear_time": state.action_clear.get(key),
                    "step_all_cleared": cur is not None and step_all_cleared((q, g, cur, allc),
                                                                             state.quest_start),
                    "action_cleared": action_cleared(key, state.action_clear, state.quest_start)})
    return out


# ---------------------------------------------------------------- the Park quest data
def build_moves(step_rows: list[dict], area_rows: list[dict], index: dict) -> dict:
    """`park_moves.json`: the master's AREA_MOVE step actions and its area ids, with the hashes
    of the two tables they come from (the live manifest must still carry those hashes)."""
    source = {t: (index.get(t) or {}).get("hash") for t in MOVE_TABLES}
    missing = [t for t, h in source.items() if not h]
    if missing:
        raise MissingInput(f"master_index.json has no hash for {missing}: refresh first")
    moves = []
    for row in step_rows:
        d = row.get("data", row)
        if is_area_move(d.get("type")):
            moves.append({"parkQuestId": d.get("parkQuestId", ""),
                          "stepGroupNumber": int(d.get("stepGroupNumber", 0)),
                          "step": int(d.get("step", 0)),
                          "parkTargetMappingId": d.get("parkTargetMappingId", ""),
                          "type": AREA_MOVE, "areaId": d.get("areaId")})
    moves.sort(key=lambda m: (m["parkQuestId"], m["stepGroupNumber"], m["step"],
                              m["parkTargetMappingId"]))
    return {"source": source, "areas": sorted(r.get("data", r)["id"] for r in area_rows),
            "area_move_actions": moves}


def write_moves(path: Path = MOVES_PATH) -> dict:
    """Render the two tables from this PC's master and write `park_moves.json`."""
    import master_json  # noqa: PLC0415  (needs SQLCipher)

    r = master_json.Renderer(master_json.load_schema())
    doc = build_moves(master_json.render("ParkQuestStepAction", r), master_json.render("Area", r),
                      json.loads(INDEX_PATH.read_bytes().decode("utf-8")))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_io.write_bytes(path, json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8"))
    return doc


def stale_tables(moves: dict, manifest: dict) -> list[str]:
    """The tables whose live hash is not the one `park_moves.json` was built from."""
    return [t for t in MOVE_TABLES
            if (manifest.get(t) or {}).get("hash") != (moves.get("source") or {}).get(t)]


def chat_areas(spots: dict) -> list[str]:
    """The areas that hold Free Chats (interiors included), from the exported catalogue."""
    chats = ((spots or {}).get("free_chat") or {}).get("chats") or {}
    return sorted({c["area"] for c in chats.values() if c.get("area")})


# ---------------------------------------------------------------- requests and replies
def refresh_body(area_id: str) -> bytes:
    """`ParkRefreshRequest {string area_id = 1}` (the client's descriptor), unsealed."""
    if not area_id:
        raise ValueError("empty area_id (the request validates min_len 1)")
    return snap.pb_bytes_field(1, area_id.encode("utf-8"))


def wire_body(plain: bytes) -> bytes:
    """As the game frames it: an empty request is five zero bytes, unsealed; anything else goes
    in the AES envelope with a fresh random nonce."""
    return snap.frame(b"") if not plain else snap.frame(snap.seal(plain))


def widen_whitelist() -> None:
    """The `ADDED` literals, exact, this process only."""
    snap.ALLOWED = frozenset(set(snap.ALLOWED) | ADDED)


def last_login_ms(userdata_fields: Iterable[tuple[int, int, Any]]) -> int | None:
    for fn, wt, v in userdata_fields:
        if fn == F_USER_TIME and wt == 2:
            for f2, _w, v2 in snap.fields(v):
                if f2 == 3:
                    return int(v2)
    return None


def logged_in_this_game_day(last_ms: int | None, now: datetime.datetime,
                            hour: int | None = None) -> bool:
    return bool(last_ms) and last_ms >= game_day.day_start_ms(now, hour)


def scanned_this_game_day(last: dict | None, now: datetime.datetime,
                          hour: int | None = None) -> bool:
    """A scan that sent any area refresh in the game day `now` falls in."""
    return bool(last and last.get("counted")
                and last.get("game_day_start") == game_day.day_start_ms(now, hour))


def userdata_of(plain: bytes) -> list | None:
    return snap.locate_userdata(plain, snap.load_userdata_schema())


def updated_userdata(plain: bytes) -> list | None:
    """A write's `common_response.updated_data`, as UserData fields."""
    for fn, wt, v in snap.fields(plain):
        if fn == F_COMMON_RESPONSE and wt == 2:
            for f2, w2, v2 in snap.fields(v):
                if f2 == 1 and w2 == 2:
                    return userdata_of(v2)
    return None


def game_running() -> bool | None:
    """True when the game's process is up; None where it cannot be told."""
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}", "/NH"],
                             capture_output=True, text=True, check=False).stdout
        return GAME_EXE.lower() in out.lower()
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    for p in proc.iterdir():
        if p.name.isdigit():
            try:
                if GAME_EXE.encode() in (p / "cmdline").read_bytes():
                    return True
            except OSError:
                continue
    return False


def master_live(host: str, ident) -> tuple[str, dict]:
    """One unauthenticated Master/Get: (master version, manifest)."""
    import master_index_live  # noqa: PLC0415

    data = snap.master_get(host, ident)
    for msg in snap.unframe(data):
        if not msg:
            continue
        pt = snap.unseal(msg)
        for wbits in (15, -15, 47):
            try:
                pt = zlib.decompress(pt, wbits)
                break
            except zlib.error:
                pass
        version = None
        for fn, wt, v in snap.fields(pt):
            if fn == 1 and wt == 2:
                version = v.decode()
            break
        if version is None:
            raise ScanStop("no version in Master/Get")
        return version, master_index_live.parse(pt)
    raise ScanStop("empty Master/Get response")


# ---------------------------------------------------------------- one login's calls
@dataclass
class Wire:
    """What the scan needs from the network, replaceable in tests."""
    master: Callable[[str, Any], tuple[str, dict]] = master_live
    login: Callable[..., str] = snap.login
    send: Callable[..., tuple[dict, dict, bytes]] = snap.call
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


class Session:
    """The calls of one login, paced `gap` seconds apart, each recorded in `calls`."""

    def __init__(self, host: str, ident, gap: float, wire: Wire, calls: list):
        self.host, self.ident, self.gap, self.wire, self.calls = host, ident, gap, wire, calls
        self.token = self.master_version = None
        self._last: float | None = None

    def pace(self) -> None:
        if self._last is not None:
            wait = self._last + self.gap - self.wire.clock()
            if wait > 0:
                self.wire.sleep(wait)
        self._last = self.wire.clock()

    def call(self, path: str, plain: bytes, label: str) -> bytes:
        self.pace()
        rec = {"label": label, "path": path, "at": now_iso()}
        self.calls.append(rec)
        print(f"  {label:38s} ", end="", flush=True)
        try:
            rh, tr, data = self.wire.send(self.host, path, wire_body(plain), token=self.token,
                                          ident=self.ident, master_version=self.master_version)
        except Exception as e:                  # noqa: BLE001  recorded, then stops the scan
            rec["error"] = f"{type(e).__name__}: {e}"
            print(rec["error"])
            raise ScanStop(f"{label}: {rec['error']}") from e
        status = tr.get("grpc-status", rh.get("grpc-status"))
        rec["grpc_status"], rec["grpc_message"] = status, tr.get("grpc-message", "")
        msgs = [m for m in snap.unframe(data) if m]
        resp = snap.unseal(msgs[0]) if msgs else b""
        rec["bytes"] = len(resp)
        print(f"ok ({len(resp):,} B)" if status in (None, "0") else f"grpc-status {status} "
              f"{rec['grpc_message']}")
        if status not in (None, "0"):
            hint = (": the game day changed, open the game once and close it"
                    if "DATE_CHANGED" in rec["grpc_message"] else "")
            raise ScanStop(f"{label} refused, grpc-status {status} {rec['grpc_message']}{hint}")
        return resp


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- the scan
def _merge_area(plain: bytes, area: str, at: str, spots: dict, keep: set | None) -> dict:
    """Fold one area refresh's chats into the read log; what it carried for that area."""
    fields = updated_userdata(plain) or []
    fc = park_free_chat.parse_userdata(fields, snap.fields)
    try:
        before = json.loads(LOG_PATH.read_bytes().decode("utf-8")).get("chats") or {}
    except (OSError, ValueError):
        before = {}
    park_free_chat.update_log(LOG_PATH, fc, at, keep)
    cat = ((spots.get("free_chat") or {}).get("chats") or {})
    mine = {d["talk_free"]: d["read"] for d in fc["details"]
            if (cat.get(d["talk_free"]) or {}).get("area") == area}
    return {"area": area, "sites": sum(1 for p in fc["picks"] if p.get("area") == area),
            "chats": len(mine), "read": sum(1 for r in mine.values() if r),
            "new_read": sum(1 for c, r in mine.items() if r and not (before.get(c) or {}).get("read"))}


def _write_last(record: dict) -> None:
    LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_io.write_bytes(LAST_PATH, json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8"))


def scan(*, wire: Wire | None = None, now: datetime.datetime | None = None,
         gap: float = GAP_S, running: Callable[[], bool | None] = game_running) -> dict:
    """The whole scan; returns its record (also written to `park_scan_last.json` once anything
    has been sent). Raises ScanStop for a refusal before any network call."""
    wire = wire or Wire()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    hour = game_day.reset_hour()
    day_start = game_day.day_start_ms(now, hour)
    if scanned_this_game_day(read_json(LAST_PATH), now, hour):
        raise ScanStop("already scanned this game day; the next scan opens at "
                       + _local(game_day.next_day_start_ms(now, hour)))
    if game_day.minutes_to_rollover(now, hour) < ROLLOVER_MARGIN_MIN:
        raise ScanStop("the game day rolls over in under 10 minutes: scan after it")
    state = running()
    if state is None:
        raise ScanStop("cannot tell whether the game is running on this PC")
    if state:
        raise ScanStop("the game is running: close it first (a login ends its session)")
    moves, spots = read_json(MOVES_PATH), read_json(SPOTS_PATH)
    if not moves or not spots:
        raise ScanStop("the Park data is missing on this PC (park_export/park_moves.json, "
                       "park_spots.json): refresh first")
    areas = chat_areas(spots)
    keep = park_free_chat.catalogue_ids(SPOTS_PATH)
    host, credential = snap.load_credential("main")
    ident = snap.client_headers("main")

    record: dict = {"started_at": now_iso(), "game_day_start": day_start, "counted": False,
                    "original": None, "areas": [], "skipped": [], "restored": None,
                    "current_area_id_after": None, "stopped": None, "calls": []}
    s = Session(host, ident, gap, wire, record["calls"])
    moved = False
    try:
        s.call(SYSTEM_INFO, b"", "System/GetSystemInfo")
        s.pace()
        version, manifest = wire.master(host, ident)
        stale = stale_tables(moves, manifest)
        if stale:
            raise ScanStop(f"the game's Park quest data changed ({', '.join(stale)}) since this "
                           "PC's Park data was built: refresh first (in the app: wait for the next "
                           "data update)")
        s.master_version = version
        s.pace()
        s.token = wire.login(host, credential, ident, version)
        print("  login ok")
        widen_whitelist()

        before = userdata_of(s.call(USER_GET, b"", "User/Get")) or []
        state = parse_quest_state(before)
        original = state.current_area_id
        record["original"] = original
        known = set(moves.get("areas") or [])
        if not original or original not in known:
            raise ScanStop(f"the account's area {original!r} is not an area of the master")
        rows = moves.get("area_move_actions") or []
        if area_move_actions(original, state, rows):
            raise ScanStop(f"an open quest step moves the account into {original}, where it "
                           "stands: the game would send ParkQuest/MoveArea there, so the scan "
                           "could not come back")
        order = []
        for area in areas:
            if area == original:
                continue
            if area not in known:
                record["skipped"].append({"area": area, "reason": "not an area of the master"})
            elif area_move_actions(area, state, rows):
                record["skipped"].append({"area": area, "reason": "an open quest step moves "
                                          "there (the game would send ParkQuest/MoveArea)"})
            else:
                order.append(area)
        last = last_login_ms(before)
        if not logged_in_this_game_day(last, now, hour):
            raise ScanStop("the account has not opened the game this game day: the scan's login "
                           "would be the day's first one. Open the game once, close it, scan again")

        s.call(HOME_LOGIN, b"", "Home/Login")
        s.call(PING_SERVERS, b"", "MultiGame/ListPingServer")
        s.call(PARK_ENTER, b"", "Park/Enter")
        s.call(EVENT_INFO, b"", "Event/ListEventInfo")
        for area in order:
            moved = True
            record["counted"] = True
            plain = s.call(PARK_REFRESH, refresh_body(area), f"Park/Refresh {area}")
            record["areas"].append(_merge_area(plain, area, now_iso(), spots, keep))
    except Exception as e:                      # noqa: BLE001  recorded; the restore still runs
        record["stopped"] = str(e) if isinstance(e, ScanStop) else f"{type(e).__name__}: {e}"
        print(f"  stopped: {record['stopped']}")
    finally:
        try:
            if moved:
                original = record["original"]
                plain = s.call(PARK_REFRESH, refresh_body(original), f"Park/Refresh {original} (back)")
                record["areas"].append(_merge_area(plain, original, now_iso(), spots, keep))
                blob = s.call(USER_GET, b"", "User/Get")
                snap.write_park_state(blob, "main")
                after = parse_quest_state(userdata_of(blob) or []).current_area_id
                record["current_area_id_after"] = after
                record["restored"] = after == original
                if not record["restored"]:
                    record["stopped"] = (record["stopped"] or "") + \
                        f" | the account is in {after}, not back in {original}"
        except Exception as e:                  # noqa: BLE001
            record["restored"] = False
            record["stopped"] = (record["stopped"] or "") + f" | going back failed: {e}"
            print(f"  GOING BACK FAILED: {e}")
        finally:
            record["finished_at"] = now_iso()
            _write_last(record)
    return record


def read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None


def _local(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000).astimezone().strftime("%H:%M")


def report(record: dict) -> str:
    lines = []
    for a in record.get("areas") or []:
        lines.append(f"  {a['area']:22s} {a['chats']:3d} chats, {a['read']:3d} read"
                     + (f" ({a['new_read']} newly)" if a.get("new_read") else "")
                     + f", {a['sites']} sites")
    for sk in record.get("skipped") or []:
        lines.append(f"  {sk['area']:22s} skipped: {sk['reason']}")
    if record.get("restored") is not None:
        lines.append(f"  back in {record.get('current_area_id_after')}: "
                     f"{'yes' if record['restored'] else 'NO'}")
    lines.append("  done" if not record.get("stopped") else f"  stopped: {record['stopped']}")
    return "\n".join(lines)


def dry_run(now: datetime.datetime | None = None) -> int:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    hour = game_day.reset_hour()
    moves, spots = read_json(MOVES_PATH), read_json(SPOTS_PATH)
    print(f"dry run, no socket. Game day starts at {hour:02d}:00 JST "
          f"({_local(game_day.day_start_ms(now, hour))} here).")
    print(f"scanned this game day: {scanned_this_game_day(read_json(LAST_PATH), now, hour)}")
    print(f"game running: {game_running()}")
    print(f"Park quest data: {'missing' if not moves else moves['source']}")
    if moves:
        for m in moves["area_move_actions"]:
            print(f"    AREA_MOVE {m['parkQuestId']} {m['stepGroupNumber']}-{m['step']} -> {m['areaId']}")
    print(f"areas with Free Chats: {chat_areas(spots) if spots else 'catalogue missing'}")
    print("calls: System/GetSystemInfo, Master/Get, Auth/Login, User/Get, Home/Login, "
          "MultiGame/ListPingServer, Park/Enter, Event/ListEventInfo, Park/Refresh per area, "
          "Park/Refresh <the area it started in>, User/Get")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="what the scan would do; no socket")
    g.add_argument("--write-moves", action="store_true",
                   help="write park_export/park_moves.json from this PC's master")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.write_moves:
        doc = write_moves()
        print(f"park_moves.json: {len(doc['area_move_actions'])} AREA_MOVE actions, "
              f"{len(doc['areas'])} areas")
        return 0
    if args.dry_run:
        return dry_run()
    try:
        record = scan()
    except ScanStop as e:
        print(f"not started: {e}")
        return 2
    print(report(record))
    return 0 if not record["stopped"] else 1


if __name__ == "__main__":
    run_cli(main)
