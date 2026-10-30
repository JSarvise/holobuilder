"""event_tracker.py (the 24/7 reader) and tracker_pull.py (its local side). No network: protobuf
messages are built here, the tracker's API is a dict, sockets are faked."""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import account_snapshot as A  # noqa: E402
import event_cutoffs as ec  # noqa: E402
import event_tracker as et  # noqa: E402
import tracker_pull as tp  # noqa: E402


def vint(fn: int, v: int) -> bytes:
    return ec._write_varint(fn << 3) + ec._write_varint(v)


def ld(fn: int, b: bytes | str) -> bytes:
    b = b.encode() if isinstance(b, str) else b
    return ec._write_varint((fn << 3) | 2) + ec._write_varint(len(b)) + b


def top_response() -> bytes:
    row = lambda r, s, uid, name: ld(4, vint(1, r) + vint(2, s)  # noqa: E731
                                     + ld(3, ld(1, uid) + ld(2, ld(1, name) + vint(2, 80))))
    return ld(1, vint(1, 0) + row(2, 900, "BBBB", "ぺこ") + row(1, 1000, "AAAA", "Ann"))


def test_parse_top_keeps_rank_score_id_and_name_in_rank_order():
    assert et.parse_top(top_response()) == [[1, 1000, "AAAA", "Ann"], [2, 900, "BBBB", "ぺこ"]]


def test_next_versions_climbs_patch_minor_major():
    assert et.next_versions("1.2.1") == ["1.2.2", "1.3.0", "2.0.0"]
    assert et.next_versions("weird") == []


def test_top_board_is_not_on_the_whitelist_by_prefix():
    # exact literals: the Top lists are readable, ReadTop (marks the screen seen) is not
    assert "/rpc.api.Marathon/ListMarathonScoreRankingTop" in A.ALLOWED
    assert "/rpc.api.Marathon/ListTotalMusicHighestScoreRankingTop" in A.ALLOWED
    with pytest.raises(PermissionError):
        A.Channel("us.game-hololive-dreams.com").call("/rpc.api.Marathon/ReadTop", b"")


def test_proxy_is_only_for_api_hosts(monkeypatch):
    seen = []

    class Fake:
        def sendall(self, b):
            seen.append(("send", b))

        def recv(self, n):
            return b"HTTP/1.1 200 Connection established\r\n\r\n"

    def create_connection(addr, timeout=None):
        seen.append(("connect", addr))
        return Fake()

    monkeypatch.setattr(A, "API_PROXY", "proxy.example:12321")
    monkeypatch.setattr(socket, "create_connection", create_connection)
    A._connect("us.game-hololive-dreams.com", 5)
    A._connect("static.game-hololive-dreams.com", 5)
    assert seen[0] == ("connect", ("proxy.example", 12321))
    assert seen[1][1].startswith(b"CONNECT us.game-hololive-dreams.com:443 ")
    assert seen[2] == ("connect", ("static.game-hololive-dreams.com", 443))  # the CDN goes direct


def rec(at: int, kind: str = "points") -> bytes:
    return json.dumps({"captured_at": at, "kind": kind, "region": "us",
                       "chapter_id": "c1", "borders": []}).encode()


def test_merge_lines_skips_reads_the_store_already_has(tmp_path):
    local = tmp_path / "us" / "c1.jsonl"
    local.parent.mkdir()
    local.write_bytes(rec(1) + b"\n")
    assert tp.merge_lines(local, [rec(1), rec(2), rec(2), rec(1, "total")]) == 2
    assert [json.loads(x)["captured_at"] for x in local.read_bytes().splitlines()] == [1, 2, 1]


def test_merge_meta_takes_only_a_newer_read(tmp_path):
    p = tmp_path / "us" / "c1.meta.json"
    assert tp.merge_meta(p, b'{"fetched_at": 5}')
    assert not tp.merge_meta(p, b'{"fetched_at": 4}')
    assert tp.merge_meta(p, b'{"fetched_at": 6}')
    assert json.loads(p.read_text())["fetched_at"] == 6


def test_pull_fetches_from_the_last_offset_and_waits_for_a_torn_line(tmp_path):
    remote = {"us/c1.jsonl": rec(1) + b"\n" + rec(2)[:10],      # second line still being written
              "status.json": b'{"regions": []}'}
    asked = []

    def get(route, **q):
        if route == "/files":
            return json.dumps({"files": [{"path": p, "size": len(b), "mtime": 1}
                                         for p, b in remote.items()]}).encode()
        asked.append((q["path"], int(q.get("offset", 0))))
        return remote[q["path"]][int(q.get("offset", 0)):]

    store = tmp_path / "cutoffs"
    assert tp.pull(get, store)["appended"] == 1
    remote["us/c1.jsonl"] = rec(1) + b"\n" + rec(2) + b"\n"
    assert tp.pull(get, store)["appended"] == 1
    first = len(rec(1)) + 1
    assert ("us/c1.jsonl", first) in asked                        # resumed, not re-read
    assert len((store / "us" / "c1.jsonl").read_bytes().splitlines()) == 2
    assert (store / "tracker_status.json").is_file()
    assert tp.pull(get, store)["appended"] == 0                   # nothing new, nothing fetched


def test_api_serves_files_inside_the_store_only(tmp_path):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    (tmp_path / "us").mkdir()
    (tmp_path / "us" / "c1.jsonl").write_bytes(b"abcdef\n")
    (tmp_path.parent / "secret.json").write_text("{}")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), et.make_handler(tmp_path, lambda: {"ok": 1}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        files = json.loads(urllib.request.urlopen(base + "/files").read())["files"]
        assert [f["path"] for f in files] == ["us/c1.jsonl"]
        assert urllib.request.urlopen(base + "/file?path=us/c1.jsonl&offset=3").read() == b"def\n"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/file?path=../secret.json")
        assert json.loads(urllib.request.urlopen(base + "/status").read()) == {"ok": 1}
    finally:
        srv.shutdown()


class _Plain(et.Region):
    """A Region without credentials or network: only the schedule is under test."""

    def __init__(self, plan):  # noqa: D107 -- deliberately skips Region.__init__
        self.plan, self.watch_until, self.next_top, self.wake = plan, 0.0, 0.0, 1e18
        self.stat = {}


def test_top_is_dense_only_while_watched():
    g = _Plain({"top": 1800, "top_watch": 120})
    assert g.top_every(0) == 1800
    g.watch(100, 300)
    assert g.top_every(150) == 120 and g.top_every(399) == 120
    assert g.top_every(401) == 1800                     # the lease ran out: background again
    assert _Plain({"top": 0, "top_watch": 120}).top_every(0) == 0   # as/jp: nothing unwatched


def test_a_new_viewer_gets_a_board_now_and_a_lease_is_capped():
    g = _Plain({"top": 1800, "top_watch": 120})
    g.next_top = 1500                                  # the background read is far away
    g.watch(100, 10**6)
    assert g.next_top == 100 and g.wake == 100
    assert g.watch_until == 100 + et.WATCH_MAX_S
    g.next_top = 200
    g.watch(150, 300)                                  # a renewal does not force an extra read
    assert g.next_top == 200


def test_watch_waits_for_the_read_it_triggers():
    calls = {"status": 0}

    class T:
        def get(self, route, **q):
            calls["status"] += 1
            n = 3 if calls["status"] >= 3 else 2
            return json.dumps({"regions": [{"region": "us", "top_reads": n}]}).encode()

        def post(self, route, **q):
            assert route == "/watch" and q["region"] == "us"
            return json.dumps({"regions": [{"region": "us", "top_reads": 2}]}).encode()

    import time as _t
    sleep = _t.sleep
    _t.sleep = lambda s: None
    try:
        assert tp.watch(T(), "us", wait_s=5)
    finally:
        _t.sleep = sleep


def test_the_event_list_is_reread_at_the_next_date_it_lists():
    ms = 1000
    listed = [{"chapter_id": "c1", "start_time": 100 * ms, "reveal_time": 500 * ms},
              {"chapter_id": "c2", "start_time": 900 * ms, "chapter_end_time": 1200 * ms}]
    assert et.next_boundary(listed, 50) == 105          # c1 starts
    assert et.next_boundary(listed, 200) == 505         # c1's final read
    assert et.next_boundary(listed, 600) == 905         # c2 starts
    assert et.next_boundary(listed, 2000) == float("inf")


class _Chan:
    def __init__(self, ok=True):
        self.sock, self.used_at, self.ok, self.pinged = object(), 0.0, ok, 0

    def ping(self):
        self.pinged += 1
        if not self.ok:
            self.sock = None
        return self.ok


def _region(chan, wake):
    g = object.__new__(et.Region)
    g.chan, g.wake = chan, wake
    g.stat = {"pings": 0, "ping_fails": 0}
    return g


def test_keepalive_pings_only_a_quiet_connection_with_a_read_due_soon():
    now = 1000.0
    c = _Chan()
    _region(c, now + 200).keepalive(now)                 # quiet since 0, read in 200 s: ping
    assert c.pinged == 1
    c.used_at = now - 5
    _region(c, now + 200).keepalive(now)                 # used 5 s ago: no ping
    assert c.pinged == 1
    c.used_at = 0.0
    _region(c, now + et.KEEPALIVE_AHEAD + 1).keepalive(now)   # nothing due soon: let it close
    assert c.pinged == 1
    dead = _Chan(ok=False)
    g = _region(dead, now + 100)
    g.keepalive(now)
    assert g.stat["ping_fails"] == 1 and dead.sock is None
    _region(dead, now + 100).keepalive(now)              # no connection: nothing to ping
    assert dead.pinged == 1


def test_an_ended_chapters_song_board_is_read_until_its_final(tmp_path):
    # an ended, still-listed chapter stays on the top list until each kind's board
    # (Event Pt and every song) has a final read
    ch = {"chapter_id": "c1", "start_time": 1, "chapter_end_time": 100,
          "tiers": {"points": [{"rank": 100}], "total": [], "music": {"m1": [{"rank": 100}]}}}
    d = tmp_path / "us"
    d.mkdir()
    (d / "c1.top.jsonl").write_text(json.dumps({"captured_at": 9, "final": True, "kind": "points", "rows": []}) + "\n")
    assert et.top_chapters([ch], [], tmp_path, "us", 500) == [ch]
    with (d / "c1.top.jsonl").open("a") as fh:
        fh.write(json.dumps({"captured_at": 10, "final": True, "kind": "music:m1", "rows": []}) + "\n")
    assert et.top_chapters([ch], [], tmp_path, "us", 500) == []


def test_a_song_board_is_read_only_under_its_own_lease():
    # the song top 100 is served like the Event Pt one, only when asked for;
    # watching the Event Pt board must not read every song's board too
    g = et.Region.__new__(et.Region)
    g.plan, g.watch_until, g.song_watch, g.next_top, g.wake = {"top": 0, "top_watch": 300}, 0.0, {}, 1e18, 1e18
    g.stat = {}
    g.watch(100.0, 600)
    assert g.kind_watched("points", 101) and not g.kind_watched("music:m1", 101)
    g.watch(100.0, 600, "music:m1")
    assert g.kind_watched("music:m1", 101) and not g.kind_watched("music:m2", 101)
    assert not g.kind_watched("music:m1", 800)


def test_maintenance_is_a_short_fixed_wait_not_a_growing_backoff():
    class _C:
        def close(self):
            pass
    g = object.__new__(et.Region)
    g.region, g.chan, g.wake, g.backoff, g.stat = "us", _C(), 0.0, 480, {}
    g.events_at, g.next_boundary = 0.0, 0.0

    def refused(now):
        raise et.GameError("/rpc.api.Event/ListEventInfo", "3", "IN_MAINTENANCE", "2006")
    g.refresh_events = refused
    g.tick(1000.0)
    assert g.wake == 1000.0 + et.MAINTENANCE_RETRY and g.backoff == 480
    assert g.stat["maintenance"] is True and g.stat["error"] == "IN_MAINTENANCE"
    g._ok(2000.0)
    assert g.stat["maintenance"] is False and g.backoff == 0
