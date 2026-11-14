"""A refresh logs in once: the shared session handed to the account steps, and the connection
`call` keeps open between calls. No network: the game is stood in for."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import account_snapshot as A  # noqa: E402
import refresh_all  # noqa: E402


@pytest.fixture
def no_session(monkeypatch):
    for k in (*A.SESSION_ENV, A.SESSION_USERDATA_ENV):
        monkeypatch.delenv(k, raising=False)


def _logins(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(A, "load_credential", lambda account: ("us.example", b"c" * 255))
    monkeypatch.setattr(A, "client_headers", lambda account: [("x-app-version", "1")])

    def master(host, ident):
        calls.append("Master/Get")
        return "mv-live"

    def login(host, cred, ident, mv):
        calls.append("Auth/Login")
        return "tok-live"

    def userdata(host, token, ident, mv):
        calls.append("User/Get")
        return b"live-userdata"

    monkeypatch.setattr(A, "fetch_master_version", master)
    monkeypatch.setattr(A, "login", login)
    monkeypatch.setattr(A, "fetch_userdata", userdata)
    return calls


def test_without_a_shared_session_a_step_logs_in_itself(monkeypatch, no_session):
    calls = _logins(monkeypatch)
    host, ident, mv, token = A.session("main")
    assert (host, mv, token) == ("us.example", "mv-live", "tok-live")
    assert A.userdata(host, token, ident, mv) == b"live-userdata"
    assert calls == ["Master/Get", "Auth/Login", "User/Get"]


def test_the_shared_session_is_used_for_the_own_account_only(monkeypatch, tmp_path, no_session):
    calls = _logins(monkeypatch)
    blob = tmp_path / "ud.bin"
    blob.write_bytes(b"shared-userdata")
    monkeypatch.setenv("HB_SESSION_HOST", "us.example")
    monkeypatch.setenv("HB_SESSION_TOKEN", "tok-shared")
    monkeypatch.setenv("HB_SESSION_MASTER_VERSION", "mv-shared")
    monkeypatch.setenv(A.SESSION_USERDATA_ENV, str(blob))

    host, ident, mv, token = A.session("main")
    assert (host, mv, token) == ("us.example", "mv-shared", "tok-shared")
    assert A.userdata(host, token, ident, mv) == b"shared-userdata"
    assert calls == []  # no Master/Get, no login, no User/Get

    # a guest never takes the user's session
    assert A.session("us")[3] == "tok-live"
    # a User/Get for another token is fetched, never read from the user's copy
    assert A.userdata(host, "tok-live", ident, mv) == b"live-userdata"


def test_a_missing_userdata_file_falls_back_to_a_fetch(monkeypatch, tmp_path, no_session):
    calls = _logins(monkeypatch)
    monkeypatch.setenv("HB_SESSION_TOKEN", "tok-shared")
    monkeypatch.setenv(A.SESSION_USERDATA_ENV, str(tmp_path / "gone.bin"))
    assert A.userdata("us.example", "tok-shared", [], "mv") == b"live-userdata"
    assert calls == ["User/Get"]


def test_refresh_all_opens_one_session_and_hands_it_down(monkeypatch, no_session):
    calls = _logins(monkeypatch)
    env, path = refresh_all.open_shared_session("main")
    try:
        assert calls == ["Master/Get", "Auth/Login", "User/Get"]
        assert env["HB_SESSION_HOST"] == "us.example"
        assert env["HB_SESSION_TOKEN"] == "tok-live"
        assert env["HB_SESSION_MASTER_VERSION"] == "mv-live"
        assert Path(env[A.SESSION_USERDATA_ENV]) == path
        assert path.read_bytes() == b"live-userdata"
    finally:
        path.unlink(missing_ok=True)


def test_refresh_all_without_a_login_leaves_the_steps_to_log_in(monkeypatch, no_session):
    def refused(account):
        raise RuntimeError("login rejected: grpc-status 16")

    monkeypatch.setattr(A, "session", refused)
    assert refresh_all.open_shared_session("main") == ({}, None)
    assert refresh_all.open_shared_session("other") == ({}, None)


class _FakeChannel:
    made: list[_FakeChannel] = []

    def __init__(self, host, *, timeout=30, guest_account=None):
        self.host, self.timeout, self.guest_account = host, timeout, guest_account
        self.sock, self.used_at, self.closed = None, 0.0, 0
        _FakeChannel.made.append(self)

    def call(self, path, body, **kw):
        self.sock, self.used_at = object(), A.time.time()
        return {}, {"grpc-status": "0"}, path.encode()

    def close(self):
        self.sock, self.closed = None, self.closed + 1


def test_call_keeps_one_connection_per_host(monkeypatch):
    _FakeChannel.made = []
    monkeypatch.setattr(A, "Channel", _FakeChannel)
    monkeypatch.setattr(A._POOL, "chans", {}, raising=False)
    for _ in range(3):
        A.call("us.game-hololive-dreams.com", "/rpc.api.User/Get", b"")
    A.call("jp.game-hololive-dreams.com", "/rpc.api.User/Get", b"")
    assert [c.host for c in _FakeChannel.made] == [
        "us.game-hololive-dreams.com",
        "jp.game-hololive-dreams.com",
    ]


def test_an_idle_connection_is_replaced_not_trusted(monkeypatch):
    _FakeChannel.made = []
    monkeypatch.setattr(A, "Channel", _FakeChannel)
    monkeypatch.setattr(A._POOL, "chans", {}, raising=False)
    A.call("us.game-hololive-dreams.com", "/rpc.api.User/Get", b"")
    ch = _FakeChannel.made[0]
    ch.used_at -= A.REUSE_IDLE_S + 1
    A.call("us.game-hololive-dreams.com", "/rpc.api.User/Get", b"")
    assert ch.closed == 1  # closed, then reopened by its next call


def test_call_still_refuses_paths_off_the_whitelist(monkeypatch):
    monkeypatch.setattr(A, "Channel", _FakeChannel)
    monkeypatch.setattr(A._POOL, "chans", {}, raising=False)
    with pytest.raises(PermissionError):
        A.call("us.game-hololive-dreams.com", "/rpc.api.Marathon/ReadTop", b"")


def test_own_account_event_reads_are_not_paced_guests_are(monkeypatch, tmp_path):
    import event_cutoffs as ec
    from test_event_cutoffs import _FakeSnap, event_info

    for account, shared, want in (("main", True, {0.0}), ("us", False, {ec.PACE_S})):
        fake = _FakeSnap(event_info())
        fake.logged_in, fake.shared = True, shared
        monkeypatch.setitem(sys.modules, "account_snapshot", fake)
        slept: list[float] = []
        monkeypatch.setattr(ec.time, "sleep", slept.append)
        monkeypatch.setattr(ec.time, "time", lambda: 1.5)
        ec.fetch_region("us", account=account, songs=False, keep_self=(account == "main"), store=tmp_path)
        assert set(slept) == want
        if account == "main":
            assert "Auth/Login" not in fake.calls and "Master/Get" not in fake.calls


def _h2_server(sock, seen_max: list[int]) -> None:
    """A minimal HTTP/2 server on `sock`: echoes each request body as its response, answering
    every finished request in REVERSE order of arrival, and notes the most streams open at once."""
    import h2.config
    import h2.connection
    import h2.events

    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False))
    conn.initiate_connection()
    sock.sendall(conn.data_to_send())
    bodies: dict[int, bytes] = {}
    ready: list[int] = []
    while True:
        data = sock.recv(65536)
        if not data:
            return
        for ev in conn.receive_data(data):
            if isinstance(ev, h2.events.RequestReceived):
                bodies[ev.stream_id] = b""
                seen_max[0] = max(seen_max[0], len(bodies))
            elif isinstance(ev, h2.events.DataReceived):
                bodies[ev.stream_id] += ev.data
                conn.acknowledge_received_data(ev.flow_controlled_length, ev.stream_id)
            elif isinstance(ev, h2.events.StreamEnded):
                ready.append(ev.stream_id)
        for sid in reversed(ready):
            conn.send_headers(sid, [(":status", "200"), ("content-type", "application/grpc")])
            conn.send_data(sid, bodies.pop(sid))
            conn.send_headers(sid, [("grpc-status", "0")], end_stream=True)
        ready.clear()
        sock.sendall(conn.data_to_send())


def test_call_many_answers_in_order_with_a_bounded_number_in_flight(monkeypatch):
    import socket
    import threading

    import h2.config
    import h2.connection

    client, server = socket.socketpair()
    seen_max = [0]
    threading.Thread(target=_h2_server, args=(server, seen_max), daemon=True).start()

    def open_h2(host, timeout):
        conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
        conn.initiate_connection()
        client.sendall(conn.data_to_send())
        return client, conn

    monkeypatch.setattr(A, "_open_h2", open_h2)
    monkeypatch.setattr(
        A,
        "_request_headers",
        lambda host, path, **kw: [
            (":method", "POST"),
            (":scheme", "https"),
            (":authority", host),
            (":path", path),
        ],
    )
    ch = A.Channel("us.game-hololive-dreams.com")
    reqs = [("/rpc.api.User/Get", f"body-{i}".encode()) for i in range(23)]
    out = ch.call_many(reqs, streams=4)
    assert [payload for _, _, payload in out] == [b for _, b in reqs]
    assert all(tr.get("grpc-status") == "0" for _, tr, _ in out)
    assert 1 <= seen_max[0] <= 4
    ch.close()
    server.close()


def test_a_border_read_refused_for_the_lock_is_retried_alone(monkeypatch):
    import holomem_rating as H

    monkeypatch.setattr(H.snap, "seal", lambda b: b)
    monkeypatch.setattr(H.snap, "unseal", lambda b: b)
    monkeypatch.setattr(H.snap, "frame", lambda b: b)
    monkeypatch.setattr(H.snap, "unframe", lambda d: [d])
    ok = {"grpc-status": "0"}
    locked = {"grpc-status": "13", "grpc-message": "LOCK_FAILED"}

    def call_many(host, reqs, *, streams, **kw):
        assert streams == H.BORDER_STREAMS
        return [({}, locked if b"chr-2" in body else ok, b"answer:" + body) for _, body in reqs]

    retried = []

    def call(host, path, body, **kw):
        retried.append(body)
        return {}, ok, b"retry:" + body

    monkeypatch.setattr(H.snap, "call_many", call_many)
    monkeypatch.setattr(H.snap, "call", call)
    out = H.read_borders("h", "t", [], "mv", ["chr-1", "chr-2", "chr-3"])
    assert len(retried) == 1 and b"chr-2" in retried[0]
    assert out[0].startswith(b"answer:") and out[1].startswith(b"retry:") and out[2].startswith(b"answer:")
