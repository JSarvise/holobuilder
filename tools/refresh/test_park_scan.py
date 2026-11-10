"""park_scan: the client's area-move quest gate, the request bytes, the guards, and the whole scan
against a scripted server (no network, no game keys: a test seed seals the replies)."""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import park_scan as S  # noqa: E402

snap = S.snap
F_PICK, F_DETAIL = 103534744, 498625501


def _s(fn: int, s) -> bytes:
    return snap.pb_bytes_field(fn, s if isinstance(s, bytes) else s.encode())


def _v(fn: int, n: int) -> bytes:
    return snap.write_varint(fn << 3) + snap.write_varint(n)


def userdata_blob(area="area-a01", quests=(), steps=(), actions=(), last_login=None) -> bytes:
    blob = _s(S.F_USER_PARK, _v(2, 1) + _s(3, area))
    for q, start in quests:
        blob += _s(S.F_QUEST, _s(2, q) + (_v(3, start) if start else b"") + _v(4, 1))
    for q, group, cur, all_clear in steps:
        blob += _s(
            S.F_QUEST_STEP, _s(2, q) + _v(3, group) + _v(4, cur) + (_v(5, all_clear) if all_clear else b"")
        )
    for q, group, step, ptm, clear in actions:
        blob += _s(
            S.F_QUEST_STEP_ACTION,
            _s(2, q) + _v(3, group) + _v(4, step) + _s(5, ptm) + (_v(6, clear) if clear else b""),
        )
    if last_login is not None:
        blob += _s(S.F_USER_TIME, _v(2, 1) + _v(3, last_login))
    return blob


def userdata(**kw) -> list:
    return list(snap.fields(userdata_blob(**kw)))


MOVE = {
    "parkQuestId": "q",
    "stepGroupNumber": 1,
    "step": 2,
    "parkTargetMappingId": "tm-q-1-2",
    "actionNumber": 1,
    "type": "ParkActionType_PARK_ACTION_TYPE_AREA_MOVE",
    "areaId": "area-a03",
}
TALK = dict(MOVE, parkTargetMappingId="tm-talk", type="ParkActionType_PARK_ACTION_TYPE_QUEST_TALK")
MASTER = [{"park_quest_id": "q", "data": MOVE}, {"park_quest_id": "q", "data": TALK}]


def gate(**kw):
    return S.area_move_actions("area-a03", S.parse_quest_state(userdata(**kw)), MASTER)


# ------------------------------------------------------------------ the gate
def test_parse_reads_area_quests_steps_and_actions():
    st = S.parse_quest_state(
        userdata(quests=[("q", 100)], steps=[("q", 1, 2, 0)], actions=[("q", 1, 2, "tm-q-1-2", 150)])
    )
    assert st.current_area_id == "area-a01"
    assert st.quest_start == {"q": 100}
    assert st.steps == [("q", 1, 2, 0)]
    assert st.action_clear == {("q", 1, 2, "tm-q-1-2"): 150}


def test_open_step_with_an_uncleared_move_gates_the_area():
    assert gate(quests=[("q", 100)], steps=[("q", 1, 2, 0)]) == [MOVE]


def test_move_cleared_in_this_run_of_the_quest_does_not_gate():
    assert gate(quests=[("q", 100)], steps=[("q", 1, 2, 0)], actions=[("q", 1, 2, "tm-q-1-2", 150)]) == []


def test_move_cleared_in_an_earlier_run_still_gates():
    # the quest restarted at 200; the action's clear at 150 belongs to the previous run
    assert gate(quests=[("q", 200)], steps=[("q", 1, 2, 0)], actions=[("q", 1, 2, "tm-q-1-2", 150)]) == [MOVE]


def test_all_cleared_step_does_not_gate():
    assert gate(quests=[("q", 100)], steps=[("q", 1, 2, 300)]) == []


def test_all_clear_from_an_earlier_run_keeps_the_step_open():
    assert gate(quests=[("q", 400)], steps=[("q", 1, 2, 300)]) == [MOVE]


def test_only_the_current_step_counts():
    assert gate(quests=[("q", 100)], steps=[("q", 1, 1, 0)]) == []
    assert gate(quests=[("q", 100)], steps=[("q", 2, 2, 0)]) == []


def test_other_area_and_other_types_do_not_gate():
    st = S.parse_quest_state(userdata(quests=[("q", 100)], steps=[("q", 1, 2, 0)]))
    assert S.area_move_actions("area-a01", st, MASTER) == []
    assert S.area_move_actions("area-a03", st, [{"data": TALK}]) == []


def test_quest_the_account_does_not_hold_is_treated_as_open():
    assert gate(quests=[], steps=[("q", 1, 2, 0)], actions=[("q", 1, 2, "tm-q-1-2", 150)]) == [MOVE]


def test_gate_detail_explains_each_area_move_row():
    st = S.parse_quest_state(
        userdata(quests=[("q", 100)], steps=[("q", 1, 2, 300)], actions=[("q", 1, 2, "tm-q-1-2", 150)])
    )
    (row,) = S.gate_detail(st, MASTER)
    assert row == {
        "quest": "q",
        "group": 1,
        "step": 2,
        "area": "area-a03",
        "quest_start": 100,
        "current_step": 2,
        "all_clear_time": 300,
        "action_clear_time": 150,
        "step_all_cleared": True,
        "action_cleared": True,
    }


def test_enum_as_number_and_unknown_shapes():
    assert S.is_area_move(2008) and S.is_area_move("PARK_ACTION_TYPE_AREA_MOVE")
    assert not S.is_area_move(2007)
    assert not S.is_area_move("ParkActionType_PARK_ACTION_TYPE_QUEST_AREA_SELECT")
    with pytest.raises(ValueError):
        S.is_area_move(None)


# ------------------------------------------------------------------ requests and whitelist
def test_refresh_body_matches_the_game_client():
    # the plaintext the game sends for Harmony Garden: 0a08 "area-a01"
    assert S.refresh_body("area-a01") == bytes.fromhex("0a08617265612d613031")
    with pytest.raises(ValueError):
        S.refresh_body("")


def test_wire_bodies_frame_like_the_client(seed):
    assert S.wire_body(b"") == bytes(5)  # Park/Enter and User/Get: 00 00000000
    env = S.wire_body(S.refresh_body("area-a03"))
    assert env[:5] == b"\x00" + (len(env) - 5).to_bytes(4, "big")
    assert env[5:9] == bytes.fromhex("0a000008")  # envelope header, 8-byte nonce
    assert snap.unseal(env[5:]) == b"\x0a\x08area-a03"


def test_whitelist_gains_exactly_four_literals_in_the_scan_process_only(monkeypatch):
    assert not (S.ADDED & snap.ALLOWED)
    monkeypatch.setattr(snap, "ALLOWED", snap.ALLOWED)
    before = set(snap.ALLOWED)
    S.widen_whitelist()
    assert set(snap.ALLOWED) - before == {
        "/rpc.api.Park/Enter",
        "/rpc.api.Park/Refresh",
        "/rpc.api.Home/Login",
        "/rpc.api.MultiGame/ListPingServer",
    }
    for write in (
        "/rpc.api.Park/SelectArea",
        "/rpc.api.Park/ReadTalkFree",
        "/rpc.api.Park/CollectRandomSymbol",
        "/rpc.api.ParkQuest/MoveArea",
        "/rpc.api.Home/AgreeRule",
        "/rpc.api.MultiGame/GeneratePrivateRoomId",
        "/rpc.api.LoginBonus/Check",
    ):
        with pytest.raises(PermissionError):
            snap._check_allowed(write, "us.game-hololive-dreams.com", None)


def _utc(*a):
    return datetime.datetime(*a, tzinfo=datetime.UTC)


def _ms(d):
    return int(d.timestamp() * 1000)


def test_home_login_only_as_a_repeat_login_of_the_game_day():
    now = _utc(2026, 11, 8, 23, 2)  # 05:00 JST = 20:00 UTC the day before
    assert S.logged_in_this_game_day(_ms(_utc(2026, 11, 8, 20, 50)), now, 5)
    assert not S.logged_in_this_game_day(_ms(_utc(2026, 11, 8, 19, 30)), now, 5)
    assert not S.logged_in_this_game_day(None, now, 5)


def test_last_login_read_from_user_time():
    assert S.last_login_ms(userdata(last_login=1792978472356)) == 1792978472356
    assert S.last_login_ms(userdata()) is None


def test_once_per_game_day_counts_only_scans_that_moved():
    now = _utc(2026, 11, 8, 23, 2)
    start = S.game_day.day_start_ms(now, 5)
    assert S.scanned_this_game_day({"counted": True, "game_day_start": start}, now, 5)
    assert not S.scanned_this_game_day({"counted": False, "game_day_start": start}, now, 5)
    assert not S.scanned_this_game_day({"counted": True, "game_day_start": start - 86_400_000}, now, 5)
    assert not S.scanned_this_game_day(None, now, 5)


# ------------------------------------------------------------------ the Park quest data
def test_build_moves_keeps_area_moves_and_the_source_hashes():
    doc = S.build_moves(
        MASTER,
        [{"data": {"id": "area-a03"}}, {"data": {"id": "area-a01"}}],
        {"ParkQuestStepAction": {"hash": "h1"}, "Area": {"hash": "h2"}},
    )
    assert doc["source"] == {"ParkQuestStepAction": "h1", "Area": "h2"}
    assert doc["areas"] == ["area-a01", "area-a03"]
    assert [m["parkTargetMappingId"] for m in doc["area_move_actions"]] == ["tm-q-1-2"]
    assert S.stale_tables(doc, {"ParkQuestStepAction": {"hash": "h1"}, "Area": {"hash": "h2"}}) == []
    assert S.stale_tables(doc, {"ParkQuestStepAction": {"hash": "NEW"}, "Area": {"hash": "h2"}}) == [
        "ParkQuestStepAction"
    ]
    with pytest.raises(S.MissingInput):
        S.build_moves(MASTER, [], {"Area": {"hash": "h2"}})


def test_chat_areas_include_interiors():
    spots = {
        "free_chat": {
            "chats": {
                "a": {"area": "area-a03"},
                "b": {"area": "area-a01-multiplay"},
                "c": {"area": "area-a01"},
                "d": {"area": "area-a03"},
            }
        }
    }
    assert S.chat_areas(spots) == ["area-a01", "area-a01-multiplay", "area-a03"]


# ------------------------------------------------------------------ the scan, end to end
NOW = _utc(2026, 11, 8, 23, 2)  # 08:02 JST: day started 20:00 UTC
LOGGED_IN = _ms(_utc(2026, 11, 8, 20, 50))
SPOTS = {
    "free_chat": {
        "chats": {
            "chat-a01": {"area": "area-a01"},
            "chat-int": {"area": "area-a01-multiplay"},
            "chat-a03-1": {"area": "area-a03"},
            "chat-a03-2": {"area": "area-a03"},
        }
    }
}
MOVES = {
    "source": {"ParkQuestStepAction": "h1", "Area": "h2"},
    "areas": ["area-a01", "area-a01-multiplay", "area-a03"],
    "area_move_actions": [dict(MOVE, type=2008)],
}
CHATS_IN = {
    "area-a01": ["chat-a01"],
    "area-a01-multiplay": ["chat-int"],
    "area-a03": ["chat-a03-1", "chat-a03-2"],
}


@pytest.fixture
def seed(monkeypatch):
    monkeypatch.setattr(snap, "SEED", b"park-scan-test-seed")


class Server:
    """A scripted game server: the account's area moves with each Park/Refresh."""

    def __init__(self, area="area-a01", fail=None, quest_steps=(), last_login=LOGGED_IN, manifest_hash="h1"):
        self.area, self.fail, self.calls = area, fail or {}, []
        self.quest_steps, self.last_login, self.manifest_hash = quest_steps, last_login, manifest_hash
        self.logins = 0

    def master(self, host, ident):
        return "v", {"ParkQuestStepAction": {"hash": self.manifest_hash}, "Area": {"hash": "h2"}}

    def login(self, host, credential, ident, version):
        self.logins += 1
        return "token"

    def send(self, host, path, body, *, token, ident, master_version):
        plain = snap.unseal(body[5:]) if len(body) > 5 else b""
        label = path if path != S.PARK_REFRESH else f"{path} {plain[2:].decode()}"
        self.calls.append(label)
        if label in self.fail:
            return {}, {"grpc-status": "13", "grpc-message": self.fail[label]}, b""
        if path == S.USER_GET:
            out = userdata_blob(
                self.area, quests=[("q", 100)], steps=self.quest_steps, last_login=self.last_login
            )
        elif path == S.PARK_REFRESH:
            self.area = plain[2:].decode()
            chats = CHATS_IN.get(self.area, [])
            ud = _s(S.F_USER_PARK, _s(3, self.area)) + _s(F_PICK, _s(2, "g1") + _s(3, self.area))
            for i, c in enumerate(chats):
                ud += _s(F_DETAIL, _s(2, "g1") + _s(3, c) + (_s(4, "chr-00001") if i == 0 else b""))
            out = _s(S.F_COMMON_RESPONSE, _s(1, ud))
        else:
            out = b""
        return {}, {"grpc-status": "0"}, snap.frame(snap.seal(out)) if out else b""


@pytest.fixture
def scan_env(monkeypatch, tmp_path, seed):
    (tmp_path / "moves.json").write_text(json.dumps(MOVES), "utf8")
    (tmp_path / "spots.json").write_text(json.dumps(SPOTS), "utf8")
    monkeypatch.setattr(S, "MOVES_PATH", tmp_path / "moves.json")
    monkeypatch.setattr(S, "SPOTS_PATH", tmp_path / "spots.json")
    monkeypatch.setattr(S, "LOG_PATH", tmp_path / "log.json")
    monkeypatch.setattr(S, "LAST_PATH", tmp_path / "last.json")
    monkeypatch.setattr(S.game_day, "reset_hour", lambda *a: 5)
    monkeypatch.setattr(snap, "load_credential", lambda account: ("host", b"cred"))
    monkeypatch.setattr(snap, "client_headers", lambda account: [])
    monkeypatch.setattr(snap, "ALLOWED", snap.ALLOWED)
    written = []
    monkeypatch.setattr(snap, "write_park_state", lambda blob, account: written.append(blob))
    return tmp_path, written


def run(server, **kw):
    slept = []
    tick = iter(range(0, 10_000))
    wire = S.Wire(
        master=server.master,
        login=server.login,
        send=server.send,
        sleep=slept.append,
        clock=lambda: float(next(tick)),
    )
    rec = S.scan(wire=wire, now=kw.pop("now", NOW), running=kw.pop("running", lambda: False), **kw)
    return rec, slept


def test_scan_visits_every_chat_area_in_the_client_order_and_goes_back(scan_env):
    tmp, written = scan_env
    server = Server()
    rec, slept = run(server)
    assert server.calls == [
        S.SYSTEM_INFO,
        S.USER_GET,
        S.HOME_LOGIN,
        S.PING_SERVERS,
        S.PARK_ENTER,
        S.EVENT_INFO,
        f"{S.PARK_REFRESH} area-a01-multiplay",
        f"{S.PARK_REFRESH} area-a03",
        f"{S.PARK_REFRESH} area-a01",
        S.USER_GET,
    ]
    assert rec["stopped"] is None and rec["restored"] is True and rec["counted"] is True
    assert server.area == "area-a01" and len(written) == 1
    assert [(a["area"], a["chats"], a["read"]) for a in rec["areas"]] == [
        ("area-a01-multiplay", 1, 1),
        ("area-a03", 2, 1),
        ("area-a01", 1, 1),
    ]
    log = json.loads((tmp / "log.json").read_text("utf8"))["chats"]
    assert set(log) == {"chat-a01", "chat-int", "chat-a03-1", "chat-a03-2"}
    assert json.loads((tmp / "last.json").read_text("utf8"))["counted"] is True
    # paced: every call but the first waits for the gap (the clock ticks 1 s per reading)
    assert slept and all(s > 0 for s in slept)


def test_an_area_with_an_open_move_step_is_skipped(scan_env):
    server = Server(quest_steps=[("q", 1, 2, 0)])  # MOVE to area-a03 is open
    rec, _ = run(server)
    assert f"{S.PARK_REFRESH} area-a03" not in server.calls
    assert [s["area"] for s in rec["skipped"]] == ["area-a03"]
    assert rec["restored"] is True


def test_an_original_area_with_an_open_move_step_stops_before_any_write(scan_env):
    server = Server(area="area-a03", quest_steps=[("q", 1, 2, 0)])
    rec, _ = run(server)
    assert server.calls == [S.SYSTEM_INFO, S.USER_GET]
    assert "could not come back" in rec["stopped"] and rec["counted"] is False


def test_first_login_of_the_game_day_stops_before_home_login(scan_env):
    server = Server(last_login=_ms(_utc(2026, 11, 8, 19, 30)))  # before 05:00 JST
    rec, _ = run(server)
    assert server.calls == [S.SYSTEM_INFO, S.USER_GET]
    assert "first one" in rec["stopped"] and rec["restored"] is None


def test_changed_master_stops_before_the_login(scan_env):
    server = Server(manifest_hash="NEW")
    rec, _ = run(server)
    assert server.logins == 0 and server.calls == [S.SYSTEM_INFO]
    assert "ParkQuestStepAction" in rec["stopped"]


def test_a_failure_mid_scan_still_goes_back(scan_env):
    server = Server(fail={f"{S.PARK_REFRESH} area-a03": "boom"})
    rec, _ = run(server)
    assert server.calls[-2:] == [f"{S.PARK_REFRESH} area-a01", S.USER_GET]
    assert "boom" in rec["stopped"] and rec["restored"] is True and server.area == "area-a01"


def test_once_per_game_day(scan_env):
    run(Server())
    with pytest.raises(S.ScanStop, match="already scanned"):
        run(Server())
    later = _utc(2026, 11, 9, 20, 30)  # the next game day
    rec, _ = run(Server(last_login=_ms(_utc(2026, 11, 9, 20, 10))), now=later)
    assert rec["stopped"] is None


@pytest.mark.parametrize("running,match", [(lambda: True, "running"), (lambda: None, "cannot tell")])
def test_game_running_or_unknown_refuses_before_any_call(scan_env, running, match):
    server = Server()
    with pytest.raises(S.ScanStop, match=match):
        run(server, running=running)
    assert server.calls == []


def test_close_to_the_rollover_refuses(scan_env):
    with pytest.raises(S.ScanStop, match="rolls over"):
        run(Server(), now=_utc(2026, 11, 9, 19, 55))
