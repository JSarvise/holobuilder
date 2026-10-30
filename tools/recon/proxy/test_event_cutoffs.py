"""event_cutoffs.py: the protobuf parsers, the chapter filter and the append-only store,
read back through tools/web/cutoffs_view.py. No network: every message is built here."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2] / "tools" / "web"))
import event_cutoffs as ec  # noqa: E402
import cutoffs_view as cv  # noqa: E402


def vint(fn: int, v: int) -> bytes:
    return ec._write_varint(fn << 3) + ec._write_varint(v)


def ld(fn: int, b: bytes | str) -> bytes:
    b = b.encode() if isinstance(b, str) else b
    return ec._write_varint((fn << 3) | 2) + ec._write_varint(len(b)) + b


def event_info() -> bytes:
    tier = lambda r, g: ld(16, vint(2, r) + ld(4, g))  # noqa: E731
    music = ld(17, ld(2, "m0001") + vint(3, 100) + ld(5, "grade-dream"))
    chapter = (ld(1, "ev-1-chapter-01") + vint(2, 1) + vint(4, 2_000) + vint(15, 3_000)
               + tier(1000, "grade-diamond") + tier(500, "grade-dream") + music)
    marathon = vint(2, 2_500) + vint(3, 3_000) + ld(4, chapter)
    ev = ld(1, "ev-1") + vint(2, 1) + ld(3, "Point Rally") + vint(4, 1_000) + vint(5, 2_000) \
        + ld(14, marathon)
    mission = ld(1, "event_mission-x") + vint(2, 3)
    return ld(1, ev) + ld(1, mission)


def grade_response() -> bytes:
    info = lambda r, s: ld(4, vint(1, r) + vint(2, s) + ld(3, ld(1, "PUBLIC-ID")))  # noqa: E731
    result = vint(1, 1385) + vint(2, 415_427) + info(1000, 558_196) + info(500, 961_903)
    return ld(1, result)


def test_parse_event_info_keeps_marathon_chapters_and_sorted_tiers():
    (ch,) = ec.parse_event_info(event_info())
    assert ch["event_id"] == "ev-1" and ch["chapter_id"] == "ev-1-chapter-01"
    assert ch["start_time"] == 1_000 and ch["chapter_end_time"] == 2_000
    assert ch["reveal_time"] == 3_000 and ch["points_ranking_disabled"] is False
    assert [t["end_rank"] for t in ch["tiers"]["points"]] == [500, 1000]
    assert ch["tiers"]["music"] == {"m0001": [{"end_rank": 100, "grade_id": "grade-dream"}]}


def test_parse_ranking_result_drops_user_info():
    r = ec.parse_ranking_result(grade_response())
    assert r == {"self_rank": 1385, "self_score": 415_427, "self_out_of_range": False,
                 "borders": [{"rank": 500, "score": 961_903}, {"rank": 1000, "score": 558_196}]}


def test_request_body_is_chapter_then_music():
    assert ec.msg(ec.request_body("ch", "m1")) == {1: [b"ch"], 2: [b"m1"]}
    assert ec.msg(ec.request_body("ch")) == {1: [b"ch"]}


def test_live_chapters_window_runs_to_the_reveal():
    (ch,) = ec.parse_event_info(event_info())
    assert ec.live_chapters([ch], 999) == []
    assert ec.live_chapters([ch], 2_500) == [ch]   # ended, final not revealed: still read
    assert ec.live_chapters([ch], 3_001) == []


def test_an_ended_chapter_is_read_once_more_for_its_final(tmp_path):
    (ch,) = ec.parse_event_info(event_info())
    # past the reveal with nothing final stored: read it
    assert ec.live_chapters([ch], 3_001, store=tmp_path, region="us") == [ch]
    early = ec.make_record(region="us", ch=ch, kind="points", account="guest", captured_at=2_500,
                           result={"borders": [], "self_rank": None, "self_score": None,
                                   "self_out_of_range": False}, keep_self=False)
    assert early["final"] is False
    ec.append_record(tmp_path, early)
    assert ec.live_chapters([ch], 3_001, store=tmp_path, region="us") == [ch]
    late = dict(early, captured_at=3_001, final=True)
    assert ec.make_record(region="us", ch=ch, kind="points", account="guest", captured_at=3_001,
                          result={"borders": [], "self_rank": None, "self_score": None,
                                  "self_out_of_range": False}, keep_self=False)["final"] is True
    ec.append_record(tmp_path, late)
    # stored: never again
    assert ec.live_chapters([ch], 9_999, store=tmp_path, region="us") == []


def test_kinds_for_point_rally_and_score_challenge():
    (ch,) = ec.parse_event_info(event_info())
    assert [k for k, _, _ in ec.kinds_for(ch, songs=False)] == ["points"]
    assert ec.kinds_for(ch, songs=True)[1] == ("music:m0001", ec.RPC_MUSIC, "m0001")
    sc = dict(ch, points_ranking_disabled=True,
              tiers=dict(ch["tiers"], total=[{"end_rank": 500, "grade_id": "g"}]))
    assert [k for k, _, _ in ec.kinds_for(sc, songs=False)] == ["total"]


def test_store_round_trip_through_the_view(tmp_path):
    (ch,) = ec.parse_event_info(event_info())
    ch = dict(ch, start_time=0, chapter_end_time=100 * 3_600_000, end_time=100 * 3_600_000,
              reveal_time=101 * 3_600_000)
    ec.write_meta(tmp_path, "us", ch, 0)
    res = ec.parse_ranking_result(grade_response())
    for t, scale in ((10, 1), (20, 2)):
        rec = ec.make_record(region="us", ch=ch, kind="points", account="main",
                             captured_at=t * 3_600_000,
                             result=dict(res, borders=[dict(b, score=b["score"] * scale)
                                                       for b in res["borders"]]),
                             keep_self=True)
        ec.append_record(tmp_path, rec)
    lines = (tmp_path / "us" / "ev-1-chapter-01.jsonl").read_bytes().split(b"\n")
    assert len(lines) == 3 and lines[-1] == b"" and b"\r" not in lines[0]
    assert b"PUBLIC-ID" not in lines[0]
    v = cv.build_view(tmp_path, "us", now=20 * 3_600_000)
    t500 = v["tiers"][0]
    assert t500["score"] == 961_903 * 2 and t500["previous"]["score"] == 961_903
    assert v["self"]["rank"] == 1385 and v["snapshots"] == 2
    assert t500["prediction"]["method"] == "pace" and t500["prediction"]["band"]


# ------------------------------------------------------------------ DATE_CHANGED
def test_daily_login_is_for_guests_only():
    err = RuntimeError("grpc-status 3 DATE_CHANGED")
    assert ec.needs_daily_login(err, "us") is True
    assert ec.needs_daily_login(err, "main") is False
    assert ec.needs_daily_login(RuntimeError("grpc-status 16 UNAUTHENTICATED"), "us") is False
    assert ec.owner_fallback(err, "main") is True and ec.owner_fallback(err, "us") is False


class _FakeSnap:
    """Stands in for account_snapshot: ListEventInfo is refused DATE_CHANGED until the
    guest's Home/Login has run. Records every call; no network."""

    def __init__(self, blob: bytes, host: str = "us.example"):
        self.blob, self.host, self.calls, self.logged_in = blob, host, [], False

    def load_credential(self, account):
        return self.host, b"cred"

    def client_headers(self, account):
        return []

    def fetch_master_version(self, host, ident):
        self.calls.append("Master/Get")
        return "mv"

    def login(self, host, cred, ident, mv):
        self.calls.append("Auth/Login")
        return "tok"

    def guest_home_login(self, account, host, token, ident, mv):
        assert account != "main"
        self.calls.append(f"Home/Login:{account}")
        self.logged_in = True

    frame = staticmethod(lambda b: b)
    seal = staticmethod(lambda b: b)
    unseal = staticmethod(lambda b: b)
    unframe = staticmethod(lambda d: [d])

    def call(self, host, path, body, **kw):
        self.calls.append(path.rsplit("/", 1)[1])
        if path.endswith("ListEventInfo"):
            if not self.logged_in:
                return {}, {"grpc-status": "3", "grpc-message": "DATE_CHANGED"}, b""
            return {}, {"grpc-status": "0"}, self.blob
        return {}, {"grpc-status": "0"}, b""


def _run_fetch(monkeypatch, tmp_path, account):
    fake = _FakeSnap(event_info())
    monkeypatch.setitem(sys.modules, "account_snapshot", fake)
    monkeypatch.setattr(ec.time, "sleep", lambda s: None)
    monkeypatch.setattr(ec.time, "time", lambda: 1.5)  # inside the test chapter (ms 1500)
    return fake, ec.fetch_region("us", account=account, songs=False, keep_self=False,
                                 store=tmp_path)


def test_guest_date_changed_does_home_login_once_then_reads(monkeypatch, tmp_path):
    fake, recs = _run_fetch(monkeypatch, tmp_path, "us")
    assert fake.calls[:5] == ["Master/Get", "Auth/Login", "ListEventInfo", "Home/Login:us",
                              "ListEventInfo"]
    assert fake.calls.count("Home/Login:us") == 1
    assert recs and recs[0]["account"] == "guest"


def test_owner_date_changed_is_raised_never_home_login(monkeypatch, tmp_path):
    import pytest
    fake = _FakeSnap(event_info())
    monkeypatch.setitem(sys.modules, "account_snapshot", fake)
    monkeypatch.setattr(ec.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="DATE_CHANGED"):
        ec.fetch_region("us", account="main", songs=False, keep_self=True, store=tmp_path)
    assert not any(c.startswith("Home/Login") for c in fake.calls)


def test_home_login_is_guest_only_in_the_whitelist(monkeypatch, tmp_path):
    import json
    import pytest
    import account_snapshot as snap
    assert "/rpc.api.Home/Login" not in snap.ALLOWED
    assert snap.GUEST_ONLY_ALLOWED == frozenset({"/rpc.api.Home/Login"})
    (tmp_path / "ranking_accounts.json").write_text(json.dumps(
        {"us": {"host": "us.example", "credential_b64": ""}}), "utf8")
    monkeypatch.setattr(snap, "STATE", tmp_path)
    assert snap._guest_ok("us", "us.example") is True
    assert snap._guest_ok("us", "jp.example") is False      # a guest only on its own host
    assert snap._guest_ok("main", "us.example") is False
    assert snap._guest_ok(None, "us.example") is False
    # refused before any socket is opened
    with pytest.raises(PermissionError):
        snap.call("us.example", "/rpc.api.Home/Login", b"")
    with pytest.raises(PermissionError):
        snap.call("us.example", "/rpc.api.Home/Login", b"", _guest_account="main")
    with pytest.raises(PermissionError):
        snap.guest_home_login("main", "us.example", "tok", [], "mv")
    with pytest.raises(PermissionError):
        snap.call("us.example", "/rpc.api.Home/AgreeRule", b"", _guest_account="us")


def test_the_refresh_reads_the_cutoffs_and_the_owners_rank_softly():
    """The refresh reads the cutoffs, so the page is never left on a stale read: the step
    reads every region plus the account's own rank, and never fails a refresh."""
    import argparse

    import refresh_all as ra
    step = next(s for s in ra.STEPS if s.name == "event_cutoffs")
    order = [s.name for s in ra.STEPS]
    assert order.index("account") < order.index("event_cutoffs")
    assert step.soft and step.network
    cmd, _env, _cwd = step.build(argparse.Namespace(python=None))
    assert cmd[-3].endswith("event_cutoffs.py") and cmd[-2:] == ["--self", "--songs"]


def test_a_chapter_out_of_term_is_skipped_and_the_others_still_read(monkeypatch, tmp_path):
    """In a relay event, a chapter that has passed its chapter_end_time answers the Grade RPC
    with EVENT_OUT_OF_TERM until its reveal. That one refusal must not fail the whole region,
    or the running next chapter is never read."""
    tier = lambda r, g: ld(16, vint(2, r) + ld(4, g))  # noqa: E731
    # chapter-01 ended at 1.4 s (reveal 5 s); chapter-02 runs from 2.4 s (its predecessor's
    # end + 1 s) to 4 s; now = 2.5 s
    chapter = lambda cid, n, end: (ld(1, cid) + vint(2, n) + vint(4, end)  # noqa: E731
                                   + vint(15, 5_000) + tier(500, "grade-dream"))
    marathon = vint(2, 4_500) + vint(3, 5_000) + ld(4, chapter("ev-1-chapter-01", 1, 1_400)) \
        + ld(4, chapter("ev-1-chapter-02", 2, 4_000))
    blob = ld(1, ld(1, "ev-1") + vint(2, 1) + ld(3, "Relay") + vint(4, 1_000)
              + vint(5, 2_000) + ld(14, marathon))

    class Snap(_FakeSnap):
        def call(self, host, path, body, **kw):
            if path.endswith("RankingGrade"):
                self.calls.append(path.rsplit("/", 1)[1])
                if b"chapter-01" in body:
                    return {}, {"grpc-status": "3", "grpc-message": "EVENT_OUT_OF_TERM"}, b""
                return {}, {"grpc-status": "0"}, grade_response()
            return super().call(host, path, body, **kw)

    fake = Snap(blob)
    fake.logged_in = True
    monkeypatch.setitem(sys.modules, "account_snapshot", fake)
    monkeypatch.setattr(ec.time, "sleep", lambda s: None)
    monkeypatch.setattr(ec.time, "time", lambda: 2.5)
    recs =ec.fetch_region("us", account="us", songs=False, keep_self=False, store=tmp_path)
    assert [r["chapter_id"] for r in recs] == ["ev-1-chapter-02"]
    assert fake.calls.count("ListMarathonScoreRankingGrade") == 2
    assert not (tmp_path / "us" / "ev-1-chapter-01.jsonl").exists()


def test_the_owner_rereads_an_ended_chapter_until_his_own_final_rank_is_stored(tmp_path):
    import json as _json
    ch = {"chapter_id": "c1", "start_time": 1, "chapter_end_time": 100}
    d = tmp_path / "us"
    d.mkdir()
    (d / "c1.jsonl").write_bytes((_json.dumps({"final": True, "self": None}) + "\n").encode())
    assert ec.live_chapters([ch], 500, store=tmp_path, region="us") == []            # tracker: done
    assert ec.live_chapters([ch], 500, store=tmp_path, region="us", need_self=True) == [ch]
    with (d / "c1.jsonl").open("ab") as fh:
        fh.write((_json.dumps({"final": True, "self": {"rank": 1362}}) + "\n").encode())
    assert ec.live_chapters([ch], 500, store=tmp_path, region="us", need_self=True) == []


def test_an_ended_chapter_is_reread_until_every_song_ranking_has_its_final(tmp_path):
    # holding a chapter's points final must not stop the tracker reading its song finals;
    # with songs=True a missing kind keeps the chapter listed
    import json as _json
    ch = {"chapter_id": "c1", "start_time": 1, "chapter_end_time": 100,
          "tiers": {"points": [{"rank": 100}], "total": [], "music": {"m0365": [{"rank": 100}]}}}
    d = tmp_path / "us"
    d.mkdir()
    (d / "c1.jsonl").write_bytes((_json.dumps({"final": True, "kind": "points"}) + "\n").encode())
    assert ec.live_chapters([ch], 500, store=tmp_path, region="us") == []
    assert ec.live_chapters([ch], 500, store=tmp_path, region="us", songs=True) == [ch]
    with (d / "c1.jsonl").open("ab") as fh:
        fh.write((_json.dumps({"final": True, "kind": "music:m0365"}) + "\n").encode())
    assert ec.live_chapters([ch], 500, store=tmp_path, region="us", songs=True) == []
