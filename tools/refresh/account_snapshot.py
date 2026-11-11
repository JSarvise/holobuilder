"""Headless account snapshot: Auth/Login -> User/Get -> decoded JSON, with no game client.

Reads the account's real cards, levels and boards, so the solver need not assume every card
owned and maxed. It never writes `data/cards.json`, and writes `state/collection.json` only on
an explicit `--merge-collection`; otherwise it writes a snapshot to `dumps/` and prints a diff.

Safety
------
  * READ-ONLY WHITELIST enforced in code (`ALLOWED`). The credential can reach write
    endpoints, and `User/Delete` takes the SAME empty request as `User/Get`, differing only in
    the path string: an empty message sent to the wrong path deletes the account. The
    `SkillTree` service has no read method at all. So: never build a path by interpolation,
    never take one from config or argv, never widen ALLOWED to a prefix or a regex; add exact
    literals only, after checking the method is a read in the .proto.
  * The credential IS the account (255 opaque bytes, full access, no second factor). It lives
    in `state/` (gitignored) and is never printed, logged or committed.
  * Guests do not send `x-app-steam-id`: the server does not need it.

Usage
-----
    python account_snapshot.py --import-from-localstore <file>
    python account_snapshot.py --account main --snapshot
    python account_snapshot.py --account main --snapshot --diff

Wire format (`frame`, `unframe`, `seal`, `unseal` below):
    gRPC frame  [flag:1][len:4 BE][payload]
    envelope    [hdr:2 LE][compress:1][keylen:1][nonce][AES-128-CBC(PKCS7)],  hdr == keylen+2
    key = MD5(SEED), IV = MD5(SEED || nonce)
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import sys
import time
import uuid
import zlib
from pathlib import Path

import atomic_io
import h2.config
import h2.connection
import h2.events
import h2.exceptions
from Crypto.Cipher import AES

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402
from hbuilder.errors import MissingInput, run_cli  # noqa: E402

STATE = paths.STATE
DUMPS = paths.DUMPS
CRED_FILE = STATE / "account_credential.json"
PROTO_DIR = paths.PROTOBUFS  # vendored from symphytum-server (MIT); see its README

import game_keys  # noqa: E402

SEED = game_keys.grpc_seed(required=False)  # None: looked up when used


def _seed(seed: bytes | None = None) -> bytes:
    """The payload cipher's build seed: the one given, else this PC's (GameKeyMissing if none)."""
    if seed is not None:
        return seed
    return SEED if SEED is not None else game_keys.grpc_seed()


# Read-only, enforced here rather than by the account.
#
# ⚠ /rpc.api.User/Delete takes the SAME empty request as /rpc.api.User/Get and differs only in
#   this string. One typo here deletes the account. Exact literals only: never a prefix, a
#   regex or an interpolation. A frozenset, so nothing can ALLOWED.add(...) past it at runtime.
#   Each entry is a read in the .proto; the services' write siblings stay off:
#   - Master/Get: the service's only method, unauthenticated.
#   - Card/GetParameters: Card also carries LevelUp, LevelLimitBreak and UpgradePotential, so a
#     /rpc.api.Card/ prefix would expose three writes.
#   - Live/GetDeck (the server's LiveDeckEvaluation), GetDraftDeckInfo (evaluates an unsaved
#     deck, power only), GetDeckCandidateCardParameters (per-card stats) and
#     ListRecommendCharacterDeck (the game's own two ranked deck lists): none has the `number`
#     slot their sibling SaveDeck writes, and SaveDeck stays off.
#   AccountMigration/List is a read too, but its siblings change who owns the account and
#   nothing needs it, so it is not listed.
ALLOWED = frozenset(
    {
        "/rpc.api.Master/Get",
        "/rpc.api.Auth/Login",
        "/rpc.api.User/Get",
        "/rpc.api.Card/GetParameters",
        "/rpc.api.Live/GetDeck",
        "/rpc.api.Live/GetDraftDeckInfo",
        "/rpc.api.Live/GetDeckCandidateCardParameters",
        "/rpc.api.Live/ListRecommendCharacterDeck",
        "/rpc.api.System/GetSystemInfo",  # read-only, unauth, single-method service; server-info probe
        "/rpc.api.ParkQuest/GetLiveFixedCharacterDeck",  # quest deck evaluation read; exact literal, never a prefix
        # the open events and their bonus tables (empty request); ListEventInfoForPortal stays off
        "/rpc.api.Event/ListEventInfo",
        # Marathon reads. The service's one mutator, ReadTop (marks the top screen seen), stays
        # off, as do the *AroundSelf lists.
        # The ranking borders (one rank_info per reward tier, plus the caller's own rank) and
        # the two Score-Challenge twins (total / per song):
        "/rpc.api.Marathon/ListMarathonScoreRankingGrade",
        "/rpc.api.Marathon/ListTotalMusicHighestScoreRankingGrade",
        "/rpc.api.Marathon/ListMusicHighestScoreRankingGrade",
        # the event tracker's top-100 boards (same requests as the Grade reads):
        "/rpc.api.Marathon/ListMarathonScoreRankingTop",
        "/rpc.api.Marathon/ListTotalMusicHighestScoreRankingTop",
        "/rpc.api.Marathon/ListMusicHighestScoreRankingTop",
        # event history: the account's own score and rank per chapter
        "/rpc.api.Marathon/Top",
    }
)

# GUEST-ONLY. NOT read-only, therefore NOT in ALLOWED and never reachable for the user's own
# account:
#
#   rpc Login (.google.protobuf.Empty) returns (.rpc.api.HomeLoginResponse)   [merged.proto]
#
# `Home/Login` is the client's daily "home login" (Auth/Login -> User/Get -> Home/Login at
# startup). It stamps `UserTime.last_login_time` and settles expired resources and licence
# renewals. The server answers `grpc-status 3 DATE_CHANGED` (ERROR_CODE 2005) to an account
# whose last_login_time is from an earlier game day (a guest with none is never refused); the
# client then reloads and runs the startup again. Only the disposable guest accounts
# (state/ranking_accounts.json) may call it, through `guest_home_login`, which checks the
# account name and host. Its sibling Home/AgreeRule stays off.
GUEST_ONLY_ALLOWED = frozenset(
    {
        "/rpc.api.Home/Login",
    }
)

# Fallback only: the real value comes from Master/Get before login, as the client does. A
# stale value here is the likeliest thing to break login after a game update.
MASTER_VERSION_FALLBACK = "24afa2c641f4a831c024ac2e912972b2accb695ad0dd3a63fc47a2ffaa69e121"

PROFILE_FILE = STATE / "client_profile.json"

# Generic client identity, used for guest accounts. When state/client_profile.json exists
# (written by --import-from-capture) its captured headers are used instead for 'main', which is
# both coherent and truthful for the user's own account.
#
# The server requires `x-app-store-type` (without it Master/Get answers grpc-status 3
# INVALID_ARGUMENT) and does not require `x-app-steam-id`: store type present, Steam ID absent
# is accepted and leaks least.
GENERIC_HEADERS = [
    ("user-agent", "grpc-dotnet/2.76.0 (Mono Unity; CLR 4.0.30319.17020; netstandard2.1; windows; x64)"),
    ("x-app-version", "1.2.1"),  # fallback only -- see app_version()
    ("x-app-lang-type", "eng"),
    ("x-app-os-type", "Windows"),
    ("x-app-store-type", "Steam"),
    ("x-app-bundle-id", "game.qualiarts.hololive.dreams.com"),
    ("x-app-os-version", "Windows 11  (10.0.26200)"),
    ("x-app-device-type", "Desktop"),
    ("x-app-operating-system-family", "Windows"),
]

# Never forwarded from a captured profile: identity not to attach to a guest account.
PROFILE_BLOCK = {"x-app-auth-token", "x-app-master-version", "x-app-request-id"}

# Fixed transport headers, independent of the identity claimed. `grpc-timeout` is the client's
# own 10S (as seen in captured client traffic).
WIRE_HEADERS = [
    ("te", "trailers"),
    ("grpc-accept-encoding", "identity,gzip"),
    ("grpc-timeout", "10S"),
    ("content-type", "application/grpc+proto-enc"),
]

# The order the game's client sends its headers in. HTTP/2 keeps the order on the wire, so any
# other order is one no real client makes. A header not listed goes just before the Steam ID.
CLIENT_ORDER = [
    "user-agent",
    "te",
    "grpc-accept-encoding",
    "x-app-auth-token",
    "x-app-master-version",
    "x-app-request-id",
    "x-app-version",
    "x-app-device-name",
    "x-app-os-version",
    "x-app-lang-type",
    "x-app-os-type",
    "x-app-store-type",
    "x-app-bundle-id",
    "x-app-cpu",
    "x-app-gpu",
    "x-app-operating-system-family",
    "x-app-device-type",
    "x-app-graphics-device-type",
    "x-app-graphics-device-version",
    "x-app-graphics-device-vendor-id",
    "x-app-graphics-device-memory-size",
    "x-app-graphics-shader-level",
    "x-app-processor-model",
    "x-app-processor-count",
    "x-app-system-memory-size",
    "x-app-screen-dpi",
    "x-app-screen-height",
    "x-app-screen-width",
    "x-app-steam-id",
    "grpc-timeout",
    "content-type",
]
# What the client leaves out, per call (from the same captures). The two calls before login
# carry no Steam ID; those two, the login, User/Get and the private-room poll (never called
# here) carry no request id. Every other call carries both -- a ranking read without a fresh
# request id is answered from the server's cache. `header_check.py` re-measures this on a capture.
NO_STEAM_ID = frozenset({"/rpc.api.Master/Get", "/rpc.api.System/GetSystemInfo"})
NO_REQUEST_ID = frozenset(
    {
        "/rpc.api.Master/Get",
        "/rpc.api.System/GetSystemInfo",
        "/rpc.api.Auth/Login",
        "/rpc.api.User/Get",
        "/rpc.api.MultiGame/CheckInvitedPrivateRoom",
    }
)


def client_headers(account: str) -> list[tuple[str, str]]:
    """Identity headers. Captured profile for 'main'; generic set for guests.

    The profile carries the user's real device name, CPU, GPU and Steam ID. That is correct
    and coherent for the user's own account, and gratuitous linkage on a throwaway guest --
    so guests get the generic set and no store claim.
    """
    if account == "main" and PROFILE_FILE.exists():
        prof = json.loads(PROFILE_FILE.read_text("utf8"))
        return [(k, v) for k, v in prof.get("headers", {}).items() if k.lower() not in PROFILE_BLOCK]
    return list(GENERIC_HEADERS)


_APP_VERSION_RE = re.compile(rb"(?<![\d.])\d+\.\d+\.\d+(?![\d.])")
_app_version_cache: list[str | None] = []


def app_version() -> str | None:
    """The client version the server will accept, or None to keep the header as written.

    The server refuses a client older than its current minimum with `grpc-status 3
    OUTDATED_APP`, so a hardcoded version breaks every call after a game update.

    `HOLODREAMS_APP_VERSION` wins. Otherwise Unity's PlayerSettings.bundleVersion is read off
    the installed client's data.unity3d (the raw object is scanned, since UnityPy cannot read
    its type tree; anything but exactly one X.Y.Z string counts as unreadable). Without the
    game (the desktop app) the fallback is `dumps/client_version.json`, written by
    `tools/app/publish.py`.
    """
    env = os.environ.get("HOLODREAMS_APP_VERSION", "").strip()
    if env:
        return env
    if not _app_version_cache:
        found: set[str] = set()
        try:
            import UnityPy

            data = os.path.join(paths.game_root(), "hololive-Dreams_Data", "data.unity3d")
            for obj in UnityPy.load(data).objects:
                if obj.type.name == "PlayerSettings":
                    found |= {m.group().decode() for m in _APP_VERSION_RE.finditer(obj.get_raw_data())}
        except Exception:  # noqa: BLE001 -- no install, no UnityPy: keep the header as written
            found = set()
        _app_version_cache.append(found.pop() if len(found) == 1 else _published_version())
    return _app_version_cache[0]


CLIENT_VERSION_FILE = DUMPS / "client_version.json"


def _published_version() -> str | None:
    try:
        v = json.loads(CLIENT_VERSION_FILE.read_text("utf8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, str) and _APP_VERSION_RE.fullmatch(v.encode()) else None


# ---------------------------------------------------------------- protobuf primitives
def read_varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while i < len(b):
        r |= (b[i] & 0x7F) << s
        s += 7
        i += 1
        if not (b[i - 1] & 0x80):
            return r, i
        if s > 70:
            break
    raise ValueError("bad varint")


def write_varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def fields(b: bytes):
    """Yield (field_no, wire_type, value). Raises on anything malformed."""
    i = 0
    while i < len(b):
        tag, i = read_varint(b, i)
        fn, wt = tag >> 3, tag & 7
        if fn == 0:
            raise ValueError("field 0")
        if wt == 0:
            v, i = read_varint(b, i)
            yield fn, wt, v
        elif wt == 1:
            i += 8
            yield fn, wt, None
        elif wt == 2:
            ln, i = read_varint(b, i)
            if i + ln > len(b):
                raise ValueError("overrun")
            yield fn, wt, b[i : i + ln]
            i += ln
        elif wt == 5:
            i += 4
            yield fn, wt, None
        else:
            raise ValueError(f"wire type {wt}")
    if i != len(b):
        raise ValueError("trailing bytes")


def pb_bytes_field(fn: int, s: bytes) -> bytes:
    return write_varint((fn << 3) | 2) + write_varint(len(s)) + s


# ---------------------------------------------------------------- envelope
def seal(plain: bytes, seed: bytes | None = None) -> bytes:
    seed = _seed(seed)
    nonce = os.urandom(8)
    iv = hashlib.md5(seed + nonce).digest()
    pad = 16 - (len(plain) % 16)
    ct = AES.new(hashlib.md5(seed).digest(), AES.MODE_CBC, iv).encrypt(plain + bytes([pad]) * pad)
    return struct.pack("<H", len(nonce) + 2) + bytes([0, len(nonce)]) + nonce + ct


def unseal(msg: bytes, seed: bytes | None = None) -> bytes:
    if len(msg) < 4:
        return b""
    seed = _seed(seed)
    nlen = msg[3]
    nonce, ct = msg[4 : 4 + nlen], msg[4 + nlen :]
    iv = hashlib.md5(seed + nonce).digest()
    pt = AES.new(hashlib.md5(seed).digest(), AES.MODE_CBC, iv).decrypt(ct)
    n = pt[-1]
    if 1 <= n <= 16 and all(b == n for b in pt[-n:]):
        pt = pt[:-n]
    return zlib.decompress(pt, 31) if msg[2] == 1 else pt


def frame(payload: bytes) -> bytes:
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def unframe(data: bytes):
    pos = 0
    while pos + 5 <= len(data):
        ln = struct.unpack(">I", data[pos + 1 : pos + 5])[0]
        yield data[pos + 5 : pos + 5 + ln]
        pos += 5 + ln


# ---------------------------------------------------------------- transport
def _guest_ok(account: str | None, host: str) -> bool:
    """True only for a named GUEST account of state/ranking_accounts.json on its own host.
    'main' (the user's own account) is refused by name before anything is read."""
    if not account or account == "main":
        return False
    try:
        guests = json.loads((STATE / "ranking_accounts.json").read_text("utf8"))
    except (OSError, ValueError):
        return False
    entry = guests.get(account) if isinstance(guests, dict) else None
    return isinstance(entry, dict) and entry.get("host") == host


def guest_home_login(account: str, host: str, token: str, ident, master_version: str) -> None:
    """`Home/Login` for a GUEST account -- the client's daily home login, which clears
    DATE_CHANGED (see GUEST_ONLY_ALLOWED). Refuses the user's own account outright."""
    if account == "main" or not _guest_ok(account, host):
        raise PermissionError(f"Home/Login is guest-only; refused for account {account!r}")
    rh, tr, _data = call(
        host,
        "/rpc.api.Home/Login",
        frame(b""),
        token=token,
        ident=ident,
        master_version=master_version,
        _guest_account=account,
    )
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st not in (None, "0"):
        raise RuntimeError(f"Home/Login rejected: grpc-status {st} {tr.get('grpc-message', '')}")


# An HTTP CONNECT proxy for the game's API hosts only (`host:port`, IP-whitelisted, no
# credentials), for a host whose own egress the API edge refuses (on one datacenter VPS the
# refusal was on the IP, not the TLS fingerprint; other hosts may differ). The CDN hosts
# (static./asset.) answer any IP and never go through it.
API_PROXY = os.environ.get("HB_API_PROXY", "").strip()
_DIRECT_HOSTS = ("static.game-hololive-dreams.com", "asset.game-hololive-dreams.com")


def _connect(host: str, timeout: float) -> socket.socket:
    """A TCP connection to `host:443` -- through API_PROXY when set and `host` is an API host."""
    if not (API_PROXY and host.endswith("game-hololive-dreams.com") and host not in _DIRECT_HOSTS):
        return socket.create_connection((host, 443), timeout=timeout)
    ph, pp = API_PROXY.rsplit(":", 1)
    s = socket.create_connection((ph, int(pp)), timeout=timeout)
    s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(4096)
        if not c:
            break
        buf += c
    status = buf.split(b"\r\n", 1)[0]
    if b" 200" not in status:
        s.close()
        raise OSError(f"proxy CONNECT {host} refused: {status!r}")
    return s


def _check_allowed(path: str, host: str, guest_account: str | None) -> None:
    if path not in ALLOWED:
        if not (path in GUEST_ONLY_ALLOWED and _guest_ok(guest_account, host)):
            raise PermissionError(f"{path} is not on the read-only whitelist")


def _request_headers(host: str, path: str, *, token: str | None, ident, master_version: str | None):
    """The request's headers, as the game's client sends them on `path`: its headers, its
    order, and per call the ones it leaves out (CLIENT_ORDER, NO_STEAM_ID, NO_REQUEST_ID)."""
    hdrs = list(ident if ident is not None else GENERIC_HEADERS)
    live_version = app_version()
    if live_version:
        hdrs = [(k, live_version if k.lower() == "x-app-version" else v) for k, v in hdrs]
    hdrs += WIRE_HEADERS
    if path not in NO_REQUEST_ID:
        hdrs.append(("x-app-request-id", str(uuid.uuid4())))
    # Master/Get declares disabled_master_version and the real client sends neither this
    # header nor a token on it -- so only attach them when they are actually known.
    if master_version:
        hdrs.append(("x-app-master-version", master_version))
    if token:
        hdrs.append(("x-app-auth-token", token))
    if path in NO_STEAM_ID:
        hdrs = [(k, v) for k, v in hdrs if k.lower() != "x-app-steam-id"]
    rank = {k: i for i, k in enumerate(CLIENT_ORDER)}
    unknown = rank["x-app-steam-id"] - 0.5
    hdrs.sort(key=lambda kv: rank.get(kv[0].lower(), unknown))  # stable: unknowns keep theirs
    return [(":method", "POST"), (":scheme", "https"), (":authority", host), (":path", path)] + hdrs


def _open_h2(host: str, timeout: float):
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["h2"])
    sock = ctx.wrap_socket(_connect(host, timeout), server_hostname=host)
    if sock.selected_alpn_protocol() != "h2":
        raise RuntimeError(f"ALPN negotiated {sock.selected_alpn_protocol()!r}, not h2")
    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    sock.sendall(conn.data_to_send())
    return sock, conn


def _exchange(sock, conn, sid: int):
    """Read until stream `sid` ends -> (response headers, trailers, payload)."""
    payload, rh, tr, reset, done = b"", {}, {}, None, False
    while not done:
        chunk = sock.recv(262144)
        if not chunk:
            raise ConnectionError("connection closed mid-response")
        for ev in conn.receive_data(chunk):
            if getattr(ev, "stream_id", sid) not in (sid, 0):
                continue
            if isinstance(ev, h2.events.ResponseReceived):
                rh = {k.decode(): v.decode() for k, v in ev.headers}
            elif isinstance(ev, h2.events.DataReceived):
                payload += ev.data
                conn.acknowledge_received_data(ev.flow_controlled_length, ev.stream_id)
            elif isinstance(ev, h2.events.TrailersReceived):
                tr = {k.decode(): v.decode() for k, v in ev.headers}
            elif isinstance(ev, h2.events.StreamReset):
                reset, done = ev.error_code, True
            elif isinstance(ev, h2.events.StreamEnded):
                done = True
            elif isinstance(ev, h2.events.ConnectionTerminated):
                raise ConnectionError(f"GOAWAY error_code={ev.error_code}")
        out = conn.data_to_send()
        if out:
            sock.sendall(out)
    if reset is not None:
        raise RuntimeError(
            f"stream reset, error_code={reset}. A PROTOCOL_ERROR (1) here usually means a "
            f"malformed header value (one the server's HTTP/2 layer refuses), not a bad endpoint."
        )
    return rh, tr, payload


class Channel:
    """ONE HTTP/2 connection to one API host, reused across calls -- for a long-running reader
    (`event_tracker.py`). Through a paid proxy the TLS handshake (~6 KB) costs as much as a
    border read, so a fresh connection per call doubles the bill. Same whitelist and headers as
    `call`; reconnects once on a dropped connection."""

    def __init__(self, host: str, *, timeout: float = 30, guest_account: str | None = None):
        self.host, self.timeout, self.guest_account = host, timeout, guest_account
        self.sock = self.conn = None
        self.used_at = 0.0  # last call or ping on the open connection (time.time())

    def ping(self, timeout: float = 10) -> bool:
        """An HTTP/2 PING on the open connection, waiting for its ACK. False (and the
        connection closed) when there is none to ping or it has died; the next `call` opens
        a fresh one. The proxy closes an idle connection after 50-60 s; a PING (under 100 B)
        every 20 s keeps it, against ~6.1 KB for a fresh connection."""
        if self.sock is None:
            return False
        try:
            self.conn.ping(os.urandom(8))
            self.sock.sendall(self.conn.data_to_send())
            self.sock.settimeout(timeout)
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionError("closed")
                acked = any(isinstance(ev, h2.events.PingAckReceived) for ev in self.conn.receive_data(chunk))
                out = self.conn.data_to_send()
                if out:
                    self.sock.sendall(out)
                if acked:
                    self.sock.settimeout(self.timeout)
                    self.used_at = time.time()
                    return True
        except (OSError, ConnectionError, h2.exceptions.ProtocolError):
            self.close()
            return False

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = self.conn = None

    def call(
        self,
        path: str,
        body: bytes,
        *,
        token: str | None = None,
        ident=None,
        master_version: str | None = None,
    ):
        _check_allowed(path, self.host, self.guest_account)
        for attempt in (0, 1):
            fresh = self.sock is None
            try:
                if fresh:
                    self.sock, self.conn = _open_h2(self.host, self.timeout)
                sid = self.conn.get_next_available_stream_id()
                self.conn.send_headers(
                    sid,
                    _request_headers(
                        self.host, path, token=token, ident=ident, master_version=master_version
                    ),
                )
                self.conn.send_data(sid, body, end_stream=True)
                self.sock.sendall(self.conn.data_to_send())
                out = _exchange(self.sock, self.conn, sid)
                self.used_at = time.time()
                return out
            except (OSError, ConnectionError, h2.exceptions.ProtocolError):
                self.close()
                if fresh or attempt:
                    raise
        raise AssertionError("unreachable")


def call(
    host: str,
    path: str,
    body: bytes,
    *,
    token: str | None = None,
    ident: list[tuple[str, str]] | None = None,
    master_version: str | None = None,
    timeout: int = 30,
    _guest_account: str | None = None,
):
    _check_allowed(path, host, _guest_account)

    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["h2"])
    sock = ctx.wrap_socket(_connect(host, timeout), server_hostname=host)
    if sock.selected_alpn_protocol() != "h2":
        raise RuntimeError(f"ALPN negotiated {sock.selected_alpn_protocol()!r}, not h2")

    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
    conn.initiate_connection()
    sock.sendall(conn.data_to_send())

    hdrs = _request_headers(host, path, token=token, ident=ident, master_version=master_version)

    sid = conn.get_next_available_stream_id()
    conn.send_headers(sid, hdrs)
    conn.send_data(sid, body, end_stream=True)
    sock.sendall(conn.data_to_send())

    payload, rh, tr, reset, done = b"", {}, {}, None, False
    while not done:
        chunk = sock.recv(262144)
        if not chunk:
            break
        for ev in conn.receive_data(chunk):
            if isinstance(ev, h2.events.ResponseReceived):
                rh = {k.decode(): v.decode() for k, v in ev.headers}
            elif isinstance(ev, h2.events.DataReceived):
                payload += ev.data
                conn.acknowledge_received_data(ev.flow_controlled_length, ev.stream_id)
            elif isinstance(ev, h2.events.TrailersReceived):
                tr = {k.decode(): v.decode() for k, v in ev.headers}
            elif isinstance(ev, h2.events.StreamReset):
                reset, done = ev.error_code, True
            elif isinstance(ev, h2.events.StreamEnded):
                done = True
        out = conn.data_to_send()
        if out:
            sock.sendall(out)
    sock.close()

    if reset is not None:
        raise RuntimeError(
            f"stream reset, error_code={reset}. A PROTOCOL_ERROR (1) here usually means a "
            f"malformed header value (one the server's HTTP/2 layer refuses), not a bad endpoint."
        )
    return rh, tr, payload


def fetch_master_version(host: str, ident) -> str:
    """Current master-data version, unauthenticated.

    `MasterGetResponse { string version = 1; ... }`; the service needs no credential and no
    prior version. The real client calls it before logging in; a stale master version is the
    likeliest cause of a refused login after a game update.
    """
    data = master_get(host, ident)
    for msg in unframe(data):
        if not msg:
            continue
        for fn, wt, v in fields(unseal(msg)):
            if fn == 1 and wt == 2:
                return v.decode()
            break
    raise RuntimeError("no version field in Master/Get response")


def master_get(host: str, ident) -> bytes:
    """One anonymous `Master/Get` (see `fetch_master_version`), past an outdated client
    version: the response body as it arrives, its messages still sealed. Raises
    GameMaintenance or RuntimeError when the server refuses it."""
    rh, tr, data = call(host, "/rpc.api.Master/Get", frame(b""), ident=ident)
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st == "3" and "OUTDATED_APP" in tr.get("grpc-message", ""):
        # A captured client profile is frozen at its capture's version. Retry once with
        # `app_version()` (else the generic header's). No version is guessed: game versions
        # jump (1.2.1 -> 1.2.101).
        current = app_version() or dict(GENERIC_HEADERS)["x-app-version"]
        sent = next((v for k, v in (ident or GENERIC_HEADERS) if k.lower() == "x-app-version"), None)
        if current != sent:
            before = os.environ.get("HOLODREAMS_APP_VERSION")
            os.environ["HOLODREAMS_APP_VERSION"] = current
            rh, tr, data = call(host, "/rpc.api.Master/Get", frame(b""), ident=ident)
            st = tr.get("grpc-status", rh.get("grpc-status"))
            if st in (None, "0"):
                CLIENT_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)  # a fresh clone
                atomic_io.write_text(
                    CLIENT_VERSION_FILE,
                    json.dumps({"version": current, "source": "accepted on Master/Get"}),
                    "utf8",
                )
            else:
                if before is None:
                    os.environ.pop("HOLODREAMS_APP_VERSION", None)
                else:
                    os.environ["HOLODREAMS_APP_VERSION"] = before
    if st not in (None, "0"):
        msg = tr.get("grpc-message", "")
        if "OUTDATED_APP" in msg:
            raise RuntimeError(
                f"the game refused client version {app_version() or sent} (OUTDATED_APP): the game "
                "has updated. On the publisher's PC, run tools/app/publish.py from the updated "
                "install; the desktop app picks the new version up with its next content sync."
            )
        if "IN_MAINTENANCE" in msg:
            raise GameMaintenance(f"Master/Get rejected: grpc-status {st} {msg}")
        raise RuntimeError(f"Master/Get rejected: grpc-status {st} {msg}")
    return data


def login(host: str, credential: bytes, ident, master_version: str) -> str:
    rh, tr, data = call(
        host,
        "/rpc.api.Auth/Login",
        frame(seal(pb_bytes_field(1, credential))),
        ident=ident,
        master_version=master_version,
    )
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st not in (None, "0"):
        raise RuntimeError(f"login rejected: grpc-status {st} {tr.get('grpc-message', '')}")
    for msg in unframe(data):
        if not msg:
            continue
        for fn, wt, v in fields(unseal(msg)):
            if fn == 1 and wt == 2:
                return v.decode()
            if fn == 2 and wt == 0 and v:
                raise RuntimeError("server demands a Play Integrity attestation")
    raise RuntimeError("no game_auth_token in login response")


EVENT_INFO_PATH = DUMPS / "event_info_live.bin"


def refresh_event_info(host: str, token: str, ident, master_version: str) -> Path:
    """`Event/ListEventInfo` -> the decrypted response, written to dumps/event_info_live.bin.

    It nests the open marathon's bonus tables (`MarathonLiveMarathonScoreBonus`) that the
    optimiser's Event Pt / Event Song Score modes need; `marathon_bonus.py` reads the file."""
    rh, tr, data = call(
        host,
        "/rpc.api.Event/ListEventInfo",
        frame(b""),
        token=token,
        ident=ident,
        master_version=master_version,
    )
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st not in (None, "0"):
        raise RuntimeError(f"Event/ListEventInfo rejected: grpc-status {st} {tr.get('grpc-message', '')}")
    for msg in unframe(data):
        if msg:
            atomic_io.write_bytes(EVENT_INFO_PATH, unseal(msg))
            print(f"event info: {EVENT_INFO_PATH.name} ({EVENT_INFO_PATH.stat().st_size} bytes)")
            return EVENT_INFO_PATH
    raise RuntimeError("empty Event/ListEventInfo response")


def fetch_userdata(host: str, token: str, ident, master_version: str) -> bytes:
    rh, tr, data = call(
        host, "/rpc.api.User/Get", frame(b""), token=token, ident=ident, master_version=master_version
    )
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st not in (None, "0"):
        raise RuntimeError(f"User/Get rejected: grpc-status {st} {tr.get('grpc-message', '')}")
    for msg in unframe(data):
        if msg:
            return unseal(msg)
    raise RuntimeError("empty User/Get response")


# ---------------------------------------------------------------- decode
def write_park_state(blob: bytes, account: str) -> None:
    """`dumps/park_state[_<account>].json`: today's Park pick and opened treasures, read from
    the same `User/Get` as the snapshot, so the Park page never logs in on its own (a login
    from the app ends the game's session on that account)."""
    import park_treasure  # noqa: PLC0415 -- it imports this module

    got = locate_userdata(blob, load_userdata_schema())
    if got is None:
        print("park      : no parseable UserData -- park_state not written")
        return
    state = park_treasure.park_state(got)
    state["_captured_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    # Free Chat read marks are permanent but each login shows only a slice (the picked sites
    # of the current area), so they are folded into a log that only ever grows; park_state
    # keeps the picks of this response, not its 2,000+ detail rows.
    free_chat = state.pop("free_chat", None) or {"picks": [], "details": []}
    state["free_chat_picks"] = free_chat["picks"]
    suffix = "" if account == "main" else f"_{account}"
    name = f"park_state{suffix}.json"
    atomic_io.write_text(DUMPS / name, json.dumps(state, indent=1, ensure_ascii=False), "utf8")
    print(f"park      : {len(state['random'])} areas -> {name}")
    import park_free_chat  # noqa: PLC0415

    keep = park_free_chat.catalogue_ids(DUMPS / "park_export" / "park_spots.json")
    log = park_free_chat.update_log(
        DUMPS / f"park_free_chat_log{suffix}.json", free_chat, state["_captured_at"], keep
    )
    print(
        f"free chat : {len(free_chat['picks'])} sites picked, "
        f"{sum(1 for c in log['chats'].values() if c['read'])} chats known read "
        f"-> park_free_chat_log{suffix}.json"
    )


def load_userdata_schema() -> dict[int, str]:
    p = PROTO_DIR / "rpc" / "api" / "common" / "user_data.gen.proto"
    if not p.exists():
        return {}
    return {
        int(m.group(2)): m.group(1)
        for m in re.finditer(r"^\s*(?:repeated\s+)?\S+\s+(\w+)\s*=\s*(\d+);", p.read_text("utf8"), re.M)
    }


def locate_userdata(blob: bytes, schema: dict[int, str], depth: int = 0):
    """The UserData parse level. Requires >=3 hash-derived field numbers co-occurring --
    a single one at any depth false-positives badly, since arbitrary bytes often parse."""
    if depth > 6:
        return None
    try:
        fs = list(fields(blob))
    except Exception:
        return None
    if sum(1 for fn, _, _ in fs if fn in schema and fn > 1000) >= 3:
        return fs
    for fn, wt, payload in fs:
        if wt == 2 and payload:
            got = locate_userdata(payload, schema, depth + 1)
            if got:
                return got
    return None


def sub(payload: bytes, names: dict[int, str]) -> dict:
    out: dict = {}
    for fn, wt, v in fields(payload):
        key = names.get(fn, fn)
        val = v.decode("utf8", "replace") if isinstance(v, bytes) else v
        if key in out:
            if not isinstance(out[key], list):
                out[key] = [out[key]]
            out[key].append(val)
        else:
            out[key] = [val] if names.get(fn, "").endswith("_ids") else val
    return out


CARD = {2: "card_id", 3: "exp", 4: "limit_break", 5: "bloom", 6: "bloom_points", 7: "acquired"}
CHAR = {2: "character_id", 3: "costume_id", 6: "exp", 7: "highest_deck_eval", 8: "acquired"}
TREE = {2: "character_id", 3: "released_ids", 4: "connected_node_ids", 5: "connected_card_ids"}
POINT = {2: "point_id", 3: "quantity"}
DECK = {2: "character_id", 3: "number", 4: "name", 5: "costume_id"}
POS = {2: "character_id", 3: "number", 4: "position", 5: "card_id"}
POSTER = {2: "poster_id"}
# UserMission. `progress` against MissionProgress.threshold is what says an achievement
# is done (other tools read this list from the snapshot).
MISSION = {2: "mission_id", 4: "progress", 5: "reward_received_thresholds"}
EMBLEM = {2: "emblem_id", 5: "last_activated_time"}

WANT = {
    "user_card_list": ("cards", CARD),
    "user_character_list": ("characters", CHAR),
    "user_character_skill_tree_list": ("boards", TREE),
    "user_skill_tree_point_list": ("board_points", POINT),
    "user_live_deck_list": ("decks", DECK),
    "user_live_deck_position_list": ("deck_positions", POS),
    # The poster count drives the Memory Bonus rate (memory_bonus_permil, PosterCollectEffect).
    "user_poster_list": ("posters", POSTER),
    # Which achievements are done (other tools read it from the snapshot).
    "user_mission_list": ("missions", MISSION),
    # A marathon's ranking reward includes one emblem per chapter and tier
    # (`emblem-marathon_score_ranking_rank_reward-<chapter>-<grade>`): the tier reached in
    # every past event, including events older than the tool's own reads.
    "user_emblem_list": ("emblems", EMBLEM),
}


def load_level_curves() -> tuple[dict, dict]:
    """(card_id -> cardLevelGroupId, group_id -> sorted [(cum_exp, level)]).

    `Card.card_level_group_id` -> `CardLevel.group_id`; a CardLevel row's `exp` is the
    CUMULATIVE exp to reach its level, so a card's level is the highest threshold its exp has
    passed (a bisect).
    """
    holo = DUMPS / "holodori"
    cf, lf = holo / "main_Card.json", holo / "main_CardLevel.json"
    if not (cf.exists() and lf.exists()):
        return {}, {}
    groups = {r["id"]: r["data"]["cardLevelGroupId"] for r in json.loads(cf.read_text("utf8"))}
    curves: dict[str, list[tuple[int, int]]] = {}
    for r in json.loads(lf.read_text("utf8")):
        d = r["data"]
        curves.setdefault(d["groupId"], []).append((int(d.get("exp") or 0), int(d["level"])))
    for g in curves:
        curves[g].sort()
    return groups, curves


def resolve_levels(snap: dict) -> int:
    """Annotate each card with `level` derived from exp. Returns how many resolved."""
    import bisect

    groups, curves = load_level_curves()
    if not groups:
        return 0
    n = 0
    for c in snap.get("cards", []):
        rows = curves.get(groups.get(c.get("card_id"), ""), None)
        if not rows:
            continue
        i = bisect.bisect_right([e for e, _ in rows], int(c.get("exp") or 0)) - 1
        c["level"] = rows[max(i, 0)][1]
        n += 1
    return n


def decode_snapshot(blob: bytes) -> dict:
    schema = load_userdata_schema()
    if not schema:
        raise RuntimeError(f"UserData schema not found at {PROTO_DIR}")
    lvl = locate_userdata(blob, schema)
    if not lvl:
        raise RuntimeError("no UserData message located in the response")

    out: dict = {"_bytes": len(blob), "_collections": 0}
    counts: dict[str, int] = {}
    for fn, wt, payload in lvl:
        if wt != 2 or fn not in schema:
            continue
        name = schema[fn]
        counts[name] = counts.get(name, 0) + 1
        if name in WANT:
            key, names = WANT[name]
            out.setdefault(key, []).append(sub(payload, names))
    out["_collections"] = len(counts)
    return out


# ---------------------------------------------------------------- diff
def diff_against_collection(snap: dict) -> None:
    path = STATE / "collection.json"
    if not path.exists():
        print("\n(no state/collection.json to diff against)")
        return
    coll = json.loads(path.read_text("utf8"))
    default = coll.get("card_default")
    overrides = coll.get("cards") or {}
    cards = snap.get("cards", [])

    print(f"\n--- diff vs state/collection.json (schema {coll.get('schema_version')}) ---")
    print(f"  model assumes      : card_default = {default!r}, {len(overrides)} per-card override(s)")
    print(f"  account really has : {len(cards)} cards")

    if default == "max" and not overrides:
        maxed = [c for c in cards if c.get("bloom") == 5 and c.get("limit_break") == 4]
        no_bloom = [c for c in cards if not c.get("bloom")]
        no_lb = [c for c in cards if not c.get("limit_break")]
        no_exp = [c for c in cards if not c.get("exp")]
        print("\n  Every owned card is being treated as fully upgraded. Measured:")
        print(f"    bloom 5 AND limit_break 4 (truly max) : {len(maxed):>3} / {len(cards)}")
        print(f"    bloom 0                               : {len(no_bloom):>3} / {len(cards)}")
        print(f"    limit_break 0                         : {len(no_lb):>3} / {len(cards)}")
        print(f"    exp 0 (still level 1)                 : {len(no_exp):>3} / {len(cards)}")
        print("\n  => the solver is overstating this collection. Layer B is unfilled, not wrong:")
        print("     'max' is the default for a card with no entry and is meant to be edited")
        print("     downward. This is the measurement that lets you edit it.")
    else:
        print("  (collection has real entries; per-card comparison not implemented yet)")

    lvl = [c["level"] for c in cards if "level" in c]
    if lvl:
        import collections

        at1 = sum(1 for v in lvl if v == 1)
        print(f"\n  levels resolved via CardLevel: {len(lvl)}/{len(cards)}")
        print(f"    still level 1                         : {at1:>3} / {len(cards)}")
        print(f"    above level 1                         : {len(lvl) - at1:>3} / {len(cards)}")
        print(f"    highest level owned                   : {max(lvl)}")
        top = collections.Counter(lvl).most_common(3)
        print("    most common levels                    : " + ", ".join(f"lvl {v} x{n}" for v, n in top))
    else:
        print("\n  NOTE: levels not resolved -- dumps/holodori/main_Card.json and")
        print("        main_CardLevel.json are needed to turn exp into a level.")


def propose_collection(snap: dict) -> None:
    """Write a CANDIDATE collection.json to dumps/ -- never to state/.

    It writes neither `data/cards.json` nor `state/collection.json`: the file is for review,
    since every value comes from one snapshot at one moment.
    """
    cards = snap.get("cards", [])
    entries = {}
    for c in cards:
        cid = c.get("card_id")
        if not cid:
            continue
        e = {}
        if "level" in c:
            e["level"] = c["level"]
        e["bloom"] = int(c.get("bloom") or 0)
        e["limit_break"] = int(c.get("limit_break") or 0)
        entries[cid] = e

    missing = [c.get("card_id") for c in cards if "level" not in c]
    out = {
        "_note": (
            "CANDIDATE, generated by account_snapshot.py from a live User/Get snapshot. "
            "NOT authoritative. Review before merging into state/collection.json. "
            "card_default is 'unknown', which in domain/collection.py means a card not listed "
            "here falls back to data/cards.json's maxed value -- it does NOT mean unowned. "
            "Ownership lives in data/owned.json. "
            "level is derived from exp via the master CardLevel curve, not reported by the "
            "server. "
            "NOTE: this file carries only cards. state/collection.json also holds hand-entered "
            "'player' data -- MERGE, do not overwrite."
        ),
        "schema_version": "collection-1",
        "card_default": "unknown",
        "cards": dict(sorted(entries.items())),
    }
    DUMPS.mkdir(parents=True, exist_ok=True)
    path = DUMPS / "collection_proposed.json"
    atomic_io.write_text(path, json.dumps(out, indent=1, ensure_ascii=False), "utf8")
    print(f"\nproposed  : {path}")
    print(f"            {len(entries)} owned cards enumerated, card_default = 'unknown'")
    if missing:
        print(f"            ! {len(missing)} without a resolved level (left absent, not guessed)")
    print("            REVIEW before copying to state/collection.json -- nothing was written there.")


def refresh_card_params(host: str, token: str, ident, master_version: str) -> None:
    """Re-fetch `Card/GetParameters` live and rewrite dumps/card_params.json.

    The refresh takes the account's card ids from this file (`merge_owned`) and
    `build_cards_dataset.py` its stats. `ParameterInfo { card_id=1, parameter=2,
    performance=3, technique=4, sense=5 }`: stat3/4/5 = Performance/Technique/Sense.
    """
    rh, tr, data = call(
        host,
        "/rpc.api.Card/GetParameters",
        frame(b""),
        token=token,
        ident=ident,
        master_version=master_version,
    )
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st not in (None, "0"):
        raise RuntimeError(f"Card/GetParameters rejected: grpc-status {st} {tr.get('grpc-message', '')}")
    rows = []
    for msg in unframe(data):
        if not msg:
            continue
        for fn, wt, payload in fields(unseal(msg)):
            if fn != 1 or wt != 2:
                continue
            rec = {}
            for f2, w2, v2 in fields(payload):
                rec[f2] = v2.decode("utf8", "replace") if isinstance(v2, bytes) else v2
            cid = rec.get(1)
            if not cid:
                continue
            parts = cid.split("-")
            s3, s4, s5 = int(rec.get(3, 0)), int(rec.get(4, 0)), int(rec.get(5, 0))
            total = int(rec.get(2, 0))
            rows.append(
                {
                    "id": cid,
                    "card_num": parts[1] if len(parts) > 1 else "",
                    "rarity": parts[3] if len(parts) > 3 else "",
                    "total": total,
                    "stat3": s3,
                    "stat4": s4,
                    "stat5": s5,
                    "total_matches_sum": total == s3 + s4 + s5,
                }
            )
    if not rows:
        raise RuntimeError("Card/GetParameters returned no parameter_infos")

    DUMPS.mkdir(parents=True, exist_ok=True)
    out = DUMPS / "card_params.json"
    prev = len(json.loads(out.read_text("utf8"))) if out.exists() else 0
    atomic_io.write_text(out, json.dumps(rows, indent=1, ensure_ascii=False), "utf8")
    bad = [r["id"] for r in rows if not r["total_matches_sum"]]
    print(f"\ncard params: {out}")
    print(f"            {prev} -> {len(rows)} cards")
    print(
        f"            total == stat3+stat4+stat5 for {len(rows) - len(bad)}/{len(rows)}"
        + (f"  ! mismatches: {bad[:3]}" if bad else "")
    )


def refresh_board_state(snap: dict) -> None:
    """Write `dumps/board_state.json` from the live snapshot.

    `build_board.py`'s input, in the shape `UserCharacterSkillTree` carries:

        {"chr-00004": {"nodes": ["G-001", ...], "slots": {"S-001": "card-..."}}}

    `released_skill_tree_node_group_ids` -> `nodes`; the connected node/card lists arrive
    positionally paired -> `slots`. The ids are already `domain/board.py`'s vocabulary.
    Run `build_board.py` after to regenerate `board.json`.
    """
    boards = snap.get("boards", [])
    if not boards:
        raise RuntimeError("snapshot carries no boards")

    out_doc = {}
    for b in boards:
        cid = b.get("character_id")
        if not cid:
            continue
        released = list(b.get("released_ids") or [])
        gids = list(b.get("connected_node_ids") or [])
        cids = list(b.get("connected_card_ids") or [])
        if len(gids) != len(cids):
            raise RuntimeError(
                f"{cid}: {len(gids)} connected nodes vs {len(cids)} connected cards -- the "
                f"lists are positionally paired, so a length mismatch means the decode is wrong"
            )
        # UNION, not just `released`: the server can report a socket as connected (it holds a
        # card) while omitting it from released_skill_tree_node_group_ids. A socket holding a
        # card is unlocked by definition.
        nodes = sorted(set(released) | set(gids))
        out_doc[cid] = {"nodes": nodes, "slots": dict(zip(gids, cids))}

    DUMPS.mkdir(parents=True, exist_ok=True)
    path = DUMPS / "board_state.json"
    prev = json.loads(path.read_text("utf8")) if path.exists() else {}
    # Back up ONCE: overwriting it every run would replace the pre-tool file with the previous
    # run's output.
    backup = DUMPS / "board_state_before_refresh.json"
    if prev and not backup.exists():
        atomic_io.write_text(backup, json.dumps(prev, indent=1, ensure_ascii=False), "utf8")

    atomic_io.write_text(
        path, json.dumps(dict(sorted(out_doc.items())), indent=1, ensure_ascii=False), "utf8"
    )

    def tot(d, key):
        return sum(len(v.get(key) or ()) for v in d.values())

    print(f"\nboard state: {path}")
    print(
        f"            characters {len(prev)} -> {len(out_doc)}; "
        f"nodes {tot(prev, 'nodes')} -> {tot(out_doc, 'nodes')}; "
        f"slots {tot(prev, 'slots')} -> {tot(out_doc, 'slots')}"
    )
    print("            now run build_board.py to regenerate dumps/board.json")


# Every merge first saves the file it is about to change, into its own timestamped backup, and
# keeps the newest COLLECTION_BACKUPS_KEPT: a backup per merge, none overwritten by the next.
# The first state ever merged also stays in `collection_before_import.json`.
COLLECTION_BACKUPS_KEPT = 20


def backup_collection(state: Path, dumps: Path) -> Path | None:
    """Copy `state` (the collection about to be merged) to
    `dumps/collection_backups/collection-<UTC time>.json`; None when there is nothing yet."""
    if not state.exists():
        return None
    folder = dumps / "collection_backups"
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)) + f"-{int(now * 1e6) % 1_000_000:06d}"
    backup = folder / f"collection-{stamp}.json"
    atomic_io.write_bytes(backup, state.read_bytes())
    kept = sorted(folder.glob("collection-*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    for old in kept[:-COLLECTION_BACKUPS_KEPT]:
        old.unlink()
    return backup


def merge_collection(snap: dict) -> bool:
    """MERGE the snapshot's cards into state/collection.json, preserving everything else.

    Opt-in (`--merge-collection`). It merges because the file carries hand-entered `player`
    data a snapshot has no notion of. `cards` and `card_default` are replaced, and
    `player.posters` is set from the account's `user_poster_list` count (it drives the Memory
    Bonus permil). `player.level` and `player.power_up_permyriad` stay hand-entered; every
    other key is kept as it was. `data/cards.json` is never touched.

    A snapshot without cards is refused (False, nothing written): an account always owns cards,
    so an empty list is a failed read, and merging it would empty the collection. Every merge
    backs up the file first (`backup_collection`).
    """
    state = STATE / "collection.json"
    if not snap.get("cards"):
        print(
            f"the snapshot carries no 'cards' -- refusing to merge an empty collection; {state} unchanged",
            file=sys.stderr,
        )
        return False
    cur = json.loads(state.read_text("utf8")) if state.exists() else {"schema_version": "collection-1"}

    DUMPS.mkdir(parents=True, exist_ok=True)
    # The first state ever merged, once, as in refresh_board_state; and a backup per merge.
    first = DUMPS / "collection_before_import.json"
    if state.exists() and not first.exists():
        atomic_io.write_text(first, state.read_text("utf8"), "utf8")
    backup = backup_collection(state, DUMPS)

    entries = {}
    for c in snap.get("cards", []):
        cid = c.get("card_id")
        if not cid:
            continue
        e = {}
        if "level" in c:
            e["level"] = c["level"]
        e["bloom"] = int(c.get("bloom") or 0)
        e["limit_break"] = int(c.get("limit_break") or 0)
        entries[cid] = e

    before_n, before_def = len(cur.get("cards") or {}), cur.get("card_default")
    preserved = [k for k in cur if k not in ("cards", "card_default")]

    merged = dict(cur)
    merged["card_default"] = "unknown"
    merged["cards"] = dict(sorted(entries.items()))
    posters_note = ""
    if isinstance(snap.get("posters"), list):
        player = dict(merged.get("player") or {})
        before_p = player.get("posters")
        player["posters"] = len(snap["posters"])
        merged["player"] = player
        posters_note = f"player.posters {before_p!r} -> {player['posters']}"
    # Cards tried as if owned (the Collection page's "Try as owned") end at the refresh.
    # A collection born from a snapshot has no `power_up_permyriad`, which would mean no
    # Member Power-Up Bonus at all; "auto" derives it from the account's own cards
    # (`collection.POWER_UP_AUTO`). A value already there is kept.
    if merged.get("trial"):
        posters_note += ("; " if posters_note else "") + f"trial cards cleared ({len(merged['trial'])})"
    merged.pop("trial", None)
    if "power_up_permyriad" not in (merged.get("player") or {}):
        merged["player"] = {**(merged.get("player") or {}), "power_up_permyriad": "auto"}
        posters_note += ("; " if posters_note else "") + "player.power_up_permyriad absent -> 'auto'"
    atomic_io.write_text(state, json.dumps(merged, indent=1, ensure_ascii=False), "utf8")

    print(f"\nmerged    : {state}")
    if backup is not None:
        print(f"            backup -> {backup}")
    print(f"            cards {before_n} -> {len(entries)}; card_default {before_def!r} -> 'unknown'")
    if posters_note:
        print(f"            {posters_note} (the account's own user_poster_list count)")
    print(
        f"            preserved untouched: {', '.join(k for k in preserved if k != 'player') or '(nothing else present)'}"
        + ("; player.level / power_up_permyriad kept" if "player" in preserved else "")
    )
    print("            data/cards.json and data/owned.json not touched.")
    return True


# ---------------------------------------------------------------- credential handling
def import_from_localstore(path: str, host: str = "us.game-hololive-dreams.com") -> None:
    """Lift the persistent account credential out of the game's OWN LocalLow store.

    The Steam client keeps the 255-byte credential it sends in Auth/Login as a BOM-prefixed
    file under %USERPROFILE%/AppData/LocalLow/QualiArts, Inc_/hololive-Dreams/. The value is
    written to state/ (gitignored) and never printed. A login answering grpc-status 16 means
    the account lives in another region: rerun with --host jp or --host as.
    """
    raw = Path(path).read_bytes()
    cred = raw[3:] if raw[:3] == b"\xef\xbb\xbf" else raw  # strip a UTF-8 BOM if present
    if not (200 <= len(cred) <= 300):
        sys.exit(
            f"unexpected credential length {len(cred)} B (expected ~255) -- refusing to "
            f"write a guess; is {path} really the credential file?"
        )
    STATE.mkdir(parents=True, exist_ok=True)
    atomic_io.write_text(
        CRED_FILE,
        json.dumps(
            {
                "host": host,
                "credential_b64": base64.b64encode(cred).decode(),
                "source": f"extracted from game LocalLow {os.path.basename(path)} (BOM-stripped)",
                "_warning": "This IS the account. Full access, no second factor. state/ is gitignored.",
            },
            indent=1,
        ),
        "utf8",
        private=True,
    )
    print(f"wrote {CRED_FILE}  (credential {len(cred)} B, host {host}; value not printed)")


class GameMaintenance(RuntimeError):
    """The game server is in maintenance (grpc-status 3 IN_MAINTENANCE, x-app-error-code 2006,
    seen on Master/Get and Event/ListEventInfo, in all three regions): every call
    is refused until it ends. Nothing to heal; try again later."""


def in_maintenance(account: str = "main") -> bool | None:
    """One anonymous Master/Get (no login, no token): True when the server says
    IN_MAINTENANCE, False when it answers, None when it could not be asked (no credential,
    no network) -- a caller then goes on and lets its own steps report."""
    try:
        host, _cred = load_credential(account)
        fetch_master_version(host, client_headers(account))
    except GameMaintenance:
        return True
    except Exception:  # noqa: BLE001 -- unknown is not maintenance
        return None
    return False


def load_credential(account: str) -> tuple[str, bytes]:
    if account == "main" and os.environ.get("HB_CREDENTIAL_FILE"):
        # The desktop app (tools/app/app_settings.py): the game's own LocalLow credential file,
        # read in place at every refresh and never copied (a path set in the app's Settings).
        raw = Path(os.environ["HB_CREDENTIAL_FILE"]).read_bytes()
        cred = raw[3:] if raw[:3] == b"\xef\xbb\xbf" else raw
        if not 200 <= len(cred) <= 300:
            raise MissingInput(f"unexpected credential length {len(cred)} B in HB_CREDENTIAL_FILE")
        return os.environ.get("HB_ACCOUNT_HOST", "us.game-hololive-dreams.com"), cred
    if account == "main":
        if not CRED_FILE.exists():
            raise MissingInput(
                "no account file: set HB_CREDENTIAL_FILE to the game's own account file "
                "(refresh.cmd / refresh.sh finds it in the game's LocalLow folder; docs/run-locally.md), "
                f"or write {CRED_FILE} with --import-from-localstore"
            )
        d = json.loads(CRED_FILE.read_text("utf8"))
    else:
        d = json.loads((STATE / "ranking_accounts.json").read_text("utf8"))[account]
    cred = base64.b64decode(d["credential_b64"])
    # docs/self-hosting.md has a fork base64 the game's own credential file, BOM and all
    return d["host"], cred[3:] if cred[:3] == b"\xef\xbb\xbf" else cred


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--account", default="main", help="'main', or a guest region key (us/jp/as)")
    ap.add_argument("--snapshot", action="store_true", help="log in and fetch User/Get")
    ap.add_argument("--diff", action="store_true", help="compare against state/collection.json")
    ap.add_argument(
        "--import-from-localstore", metavar="FILE", help="lift the credential from the game's LocalLow store"
    )
    ap.add_argument(
        "--host",
        default="us.game-hololive-dreams.com",
        help="API region host (us/jp/as .game-hololive-dreams.com)",
    )
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent; open no socket")
    ap.add_argument(
        "--propose-collection",
        action="store_true",
        help="write a CANDIDATE collection.json to dumps/ to review by hand",
    )
    ap.add_argument(
        "--merge-collection",
        action="store_true",
        help="MERGE cards into state/collection.json (backs up first, keeps 'player')",
    )
    ap.add_argument(
        "--refresh-events",
        action="store_true",
        help="also fetch Event/ListEventInfo (the open event's bonus tables)",
    )
    ap.add_argument(
        "--refresh-card-params",
        action="store_true",
        help="re-fetch Card/GetParameters into dumps/card_params.json (mode B input)",
    )
    ap.add_argument(
        "--refresh-board-state",
        action="store_true",
        help="write dumps/board_state.json from the snapshot (build_board.py input)",
    )
    ap.add_argument("--out", default=None, help="where to write the snapshot JSON")
    args = ap.parse_args()

    if args.import_from_localstore:
        import_from_localstore(args.import_from_localstore, args.host)
        return
    if not args.snapshot:
        ap.error("nothing to do -- pass --snapshot (optionally with --diff)")

    host, credential = load_credential(args.account)
    ident = client_headers(args.account)
    profile = (
        "this PC's client profile"
        if (args.account == "main" and PROFILE_FILE.exists())
        else "generic (no store claimed)"
    )

    print(f"host      : {host}")
    print(f"account   : {args.account} (credential {len(credential)} B, not printed)")
    print(f"whitelist : {sorted(ALLOWED)}")
    print(f"identity  : {profile}, {len(ident)} headers")

    if args.dry_run:
        print("\n--dry-run: no socket will be opened. Requests that would be sent:")
        for path, what in (
            ("/rpc.api.Master/Get", "no auth, no master-version"),
            ("/rpc.api.Auth/Login", "credential in sealed envelope"),
            ("/rpc.api.User/Get", "empty body + auth token"),
        ):
            print(f"    POST https://{host}{path}   ({what})")
        print("\n  identity headers (values shown; none are secret):")
        for k, v in ident:
            print(f"    {k}: {v}")
        print(f"\n  plus {[k for k, _ in WIRE_HEADERS]}, a fresh x-app-request-id,")
        print("  and x-app-master-version fetched at runtime from Master/Get.")
        return

    master_version = fetch_master_version(host, ident)
    stale = master_version != MASTER_VERSION_FALLBACK
    print(
        f"master    : {master_version[:16]}...  "
        f"({'CHANGED since the capture' if stale else 'same as the capture'})"
    )

    token = login(host, credential, ident, master_version)
    print(f"login     : OK, token {len(token)} chars (not printed)")
    blob = fetch_userdata(host, token, ident, master_version)
    print(f"snapshot  : {len(blob):,} B plaintext")

    snap = decode_snapshot(blob)
    write_park_state(blob, args.account)
    resolved = resolve_levels(snap)
    print(
        f"decoded   : {snap['_collections']} populated user_* collections"
        + (f"; {resolved} card levels resolved from exp" if resolved else "")
    )
    for key in (
        "cards",
        "characters",
        "boards",
        "board_points",
        "decks",
        "deck_positions",
        "posters",
        "emblems",
    ):
        if key in snap:
            print(f"    {len(snap[key]):>4}  {key}")
    if "posters" in snap:
        pc = len(snap["posters"])
        print(
            f"    -> poster count = {pc} (drives Memory Bonus; state/collection.json "
            f"player.posters follows it on --merge-collection)"
        )

    DUMPS.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else DUMPS / f"account_snapshot_{args.account}.json"
    atomic_io.write_text(out, json.dumps(snap, indent=1, ensure_ascii=False), "utf8")
    print(f"written   : {out}")
    print(
        "            (dumps/ is gitignored; data/cards.json is never written)"
        if args.merge_collection
        else "            (dumps/ is gitignored; cards.json and collection.json untouched)"
    )

    if args.propose_collection:
        propose_collection(snap)

    # ⚠ The offline work runs FIRST: it depends only on the snapshot in hand, and must not be
    # lost if a later network call (Card/GetParameters on DATE_CHANGED) ends `main`.
    if args.refresh_board_state:
        refresh_board_state(snap)

    merge_refused = bool(args.merge_collection) and not merge_collection(snap)

    if args.diff:
        diff_against_collection(snap)

    # Once the in-game day rolls over (`hbuilder.game_day`, Setting.resetTimingHour JST),
    # `Event/ListEventInfo` and `Card/GetParameters` are refused `grpc-status 3 DATE_CHANGED`
    # until the game client processes the reset (opened once); `User/Get` still succeeds.
    # Reported as a diagnosis, not a traceback; each call is still tried.
    refused = []
    for wanted, rpc, read in (
        (args.refresh_events, "Event/ListEventInfo", refresh_event_info),
        (args.refresh_card_params, "Card/GetParameters", refresh_card_params),
    ):
        if not wanted:
            continue
        try:
            read(host, token, ident, master_version)
        except RuntimeError as exc:
            if "DATE_CHANGED" not in str(exc):
                raise
            refused.append(rpc)
    if refused:
        from hbuilder import game_day  # noqa: PLC0415

        sys.stdout.flush()  # the report above first: a reader of the merged output sees this last
        for line in (
            "",
            f"! {' and '.join(refused)} refused: DATE_CHANGED.",
            f"  The in-game day rolled over ({game_day.reset_hour():02d}:00 JST) and this account has not",
            "  opened the game since. Nothing is wrong with the credential -- the",
            "  server wants the CLIENT to run its daily reset first.",
            "  FIX: open the game once, then re-run.",
            "  What those calls refresh (dumps/card_params.json, the open event) is",
            "  UNCHANGED; the snapshot, board state and collection above ARE current.",
        ):
            print(line, file=sys.stderr)
        sys.exit(1)
    if merge_refused:
        sys.exit(2)


if __name__ == "__main__":
    run_cli(main)
