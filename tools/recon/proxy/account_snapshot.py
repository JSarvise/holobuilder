"""Headless account snapshot: Auth/Login -> User/Get -> decoded JSON, with no game client.

Why this exists
---------------
The same mechanism the ranking poller uses also reads the user's OWN account. A
`state/collection.json` of `card_default: "max"` with no per-card overrides makes the solver
assume every card owned and fully upgraded, while a real account owns fewer, most of them
nowhere near max. This shows that gap without typing the whole collection by hand.

What it does NOT do
-------------------
It never writes `data/cards.json` (authoritative, hand-edited) and writes
`state/collection.json` (the user's account) only on an explicit `--merge-collection`.
Otherwise it writes a snapshot to `dumps/` and prints a diff.

Safety
------
  * READ-ONLY WHITELIST enforced in code. The credential can reach write endpoints; this
    client refuses to call anything not on the list. Keep it that way.

    ⚠ Concretely, and this is why the whitelist is not ceremony: the User service is

          rpc Get    (google.protobuf.Empty) returns (UserGetResponse)
          rpc Delete (google.protobuf.Empty) returns (UserDeleteResponse)

    `User/Delete` takes the SAME empty request as `User/Get` and differs only in the path
    string. There is no confirmation field, no argument to get wrong, nothing to fail on --
    an empty message sent to the wrong path deletes the account. The `SkillTree` service is
    the same hazard in a different shape: it exposes ReleaseNode / ConnectNode / ResetNode
    and NO read method at all, so every path on it is destructive.

    Therefore: never build a path by string interpolation, never take a path from config or
    argv, and never widen ALLOWED to a prefix or a regex. Add exact literals only, one at a
    time, and only after checking the method is a read in the .proto.
  * The credential IS the account -- 255 opaque bytes, full access, no second factor, and unlike
    the guest ones it is not disposable. It lives in `state/` (gitignored). Never printed here,
    never logged, never committed.
  * Does not send `x-app-steam-id`: tying the user's Steam ID to a guest account is gratuitous,
    and the server does not miss it.

Usage
-----
    proxyenv/Scripts/python account_snapshot.py --import-from-capture keycap8_flows.mitm
    proxyenv/Scripts/python account_snapshot.py --account main --snapshot
    proxyenv/Scripts/python account_snapshot.py --account main --snapshot --diff

Wire format (all measured; see `grpc_decrypt.py`):
    gRPC frame  [flag:1][len:4 BE][payload]
    envelope    [hdr:2 LE][compress:1][keylen:1][nonce][AES-128-CBC(PKCS7)],  hdr == keylen+2
    key = MD5(SEED), IV = MD5(SEED || nonce)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import atomic_io
import os
import re
import socket
import ssl
import struct
import time
import sys
import uuid
import zlib
from pathlib import Path

from Crypto.Cipher import AES
import h2.config
import h2.connection
import h2.events
import h2.exceptions

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
STATE = REPO / "state"
DUMPS = HERE / "dumps"
CRED_FILE = STATE / "account_credential.json"
PROTO_DIR = HERE.parent / "protobufs"  # vendored from symphytum-server (MIT); see its README

import game_keys  # noqa: E402
SEED = game_keys.grpc_seed(required=False)          # None: looked up when used


def _seed(seed: bytes | None = None) -> bytes:
    """The payload cipher's build seed: the one given, else this PC's (GameKeyMissing if none)."""
    if seed is not None:
        return seed
    return SEED if SEED is not None else game_keys.grpc_seed()

# Read-only, enforced here rather than by the account.
#
# ⚠ /rpc.api.User/Delete takes the SAME empty request as /rpc.api.User/Get and differs only in
#   this string. One typo here deletes the account -- no confirmation, no argument to get wrong.
#   Exact literals only. Never a prefix, never a regex, never interpolated. See the module
#   docstring's Safety section before touching this line.
#   frozenset, not set, so no code path can ALLOWED.add(...) its way past this at runtime.
#
#   /rpc.api.Master/Get was added after checking the .proto per the rule above: the Master
#   service has exactly ONE method, `Get (Empty) returns (MasterGetResponse)`, and it declares
#   both `disabled_game_auth_token` and `disabled_master_version` -- it is an unauthenticated
#   read, and there is no write method on that service to typo into.
#   /rpc.api.Card/GetParameters was added the same way, and needs MORE care than the others
#   because the Card service is NOT read-only: it also carries LevelUp, LevelLimitBreak and
#   UpgradePotential. `GetParameters (Empty) returns (CardGetParametersResponse)` is the read.
#   This is exactly why the rule is "exact literals, never a prefix" -- a prefix on
#   /rpc.api.Card/ would expose three write methods.
#   AccountMigration is the same trap again. `List (Empty) returns (AccountMigrationListResponse)`
#   is a pure read -- it reports whether a transfer id/password has been set -- but its siblings
#   on the SAME service (SetPassword, PrepareMigrationPassword, Migrate, Unlink) all mutate who
#   owns the account. It is deliberately NOT listed here: nothing needs it yet, and an unused
#   entry on a safety rail is a liability. If a live migration-status check is ever wanted, add
#   the exact literal "/rpc.api.AccountMigration/List" and nothing broader.
#   /rpc.api.Live/GetDeck was added after checking live.gen.proto per the rule above:
#   `GetDeck (LiveGetDeckRequest) returns (LiveGetDeckResponse)` with enabled_response_cache=true
#   -- a pure read that returns the deck positions and the server's LiveDeckEvaluation (the Unit
#   Score decomposition oracle). Its SIBLING /rpc.api.Live/SaveDeck WRITES the deck and is the
#   trap the "exact literals, never a prefix" rule guards against -- so GetDeck is listed and
#   SaveDeck is not. GetDeck takes a real request (character_id + number), never an empty body,
#   so it cannot be confused with a destructive empty-request method.
#   /rpc.api.Live/GetDraftDeckInfo and /rpc.api.Live/GetDeckCandidateCardParameters were added
#   after checking live.gen.proto:
#   `GetDraftDeckInfo (LiveGetDraftDeckInfoRequest{character_id, costume_id, deck_positions[<=5],
#   music_id}) returns (LiveGetDraftDeckInfoResponse{live_deck_power, deck_positions})` -- it
#   EVALUATES an UNSAVED, caller-composed deck and returns POWER only; it persists NOTHING (no
#   `number` slot, unlike SaveDeck), so it is a pure read despite taking a rich request.
#   `GetDeckCandidateCardParameters (…{character_id, costume_id, music_id}) returns
#   (…{parameter_infos[]})` is a pure per-card stat lookup. Their mutating sibling SaveDeck stays
#   OFF the list. All three are exact literals, never a prefix.
#   /rpc.api.Live/ListRecommendCharacterDeck was added after checking its proto per the rule above:
#   `ListRecommendCharacterDeck (LiveListRecommendCharacterDeckRequest{musicId, currentCharacterId,
#   currentDeckNumber}) returns (LiveListRecommendCharacterDeckResponse{strongLiveDecks[],
#   highScoreRatingTargetLiveDecks[]})`. It is the game's OWN team optimizer: it returns two ranked
#   server-evaluated 5-card deck lists (max-power and max-score-rating) and persists NOTHING — no
#   `number` write slot, unlike SaveDeck. A pure read despite taking a rich request, exactly like
#   GetDraftDeckInfo. Its mutating siblings (SaveDeck) stay OFF the list. Exact literal, never a prefix.
ALLOWED = frozenset({
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
    # the open events and their bonus tables. merged.proto:
    # `rpc ListEventInfo (.google.protobuf.Empty) returns (EventListEventInfoResponse)` -- an
    # empty request, a pure read. Its sibling ListEventInfoForPortal stays off; exact literal.
    "/rpc.api.Event/ListEventInfo",
    # event cutoffs: the ranking BORDERS -- one rank_info per reward-tier boundary, plus the caller's own rank. merged.proto:
    # `ListMarathonScoreRankingGrade (…{marathon_chapter_id}) returns (…{MarathonScoreRankingResult})`
    # and the two Score-Challenge twins (total / per song, `{marathon_chapter_id[, music_id]}`).
    # Pure reads; the Marathon service's one mutator is `ReadTop` (marks the top screen seen) and it
    # stays OFF, as do the *Top / *AroundSelf lists nothing needs yet. Exact literals, never a prefix.
    "/rpc.api.Marathon/ListMarathonScoreRankingGrade",
    "/rpc.api.Marathon/ListTotalMusicHighestScoreRankingGrade",
    "/rpc.api.Marathon/ListMusicHighestScoreRankingGrade",
    # the event tracker: the chapter's TOP-100 board. merged.proto: `ListMarathonScoreRankingTop
    # (…{marathon_chapter_id}) returns (…{MarathonScoreRankingResult})` and its Score-Challenge
    # twin `ListTotalMusicHighestScoreRankingTop` -- same request as the Grade reads, pure reads.
    # ReadTop and the *AroundSelf lists stay OFF. Exact literals, never a prefix.
    "/rpc.api.Marathon/ListMarathonScoreRankingTop",
    "/rpc.api.Marathon/ListTotalMusicHighestScoreRankingTop",
    # a chapter's SONG ranking, top 100: merged.proto
    # `ListMusicHighestScoreRankingTop ({marathon_chapter_id=1, music_id=2})`, the Grade read's
    # song twin; same result message as the two above. Pure read.
    "/rpc.api.Marathon/ListMusicHighestScoreRankingTop",
    # event history: the account's own result per chapter.
    # merged.proto: `rpc Top (MarathonTopRequest{marathon_id}) returns (MarathonTopResponse{
    # marathon{chapters[{id, character_id, score, ...}], ...}, ranking_result{chapters[{
    # marathon_chapter_id, marathon_score_rank, ...}]}})` -- a pure read; the screen-seen mark is
    # its sibling `ReadTop`, which stays OFF. Exact literal, never a prefix.
    "/rpc.api.Marathon/Top",
})

# GUEST-ONLY. NOT read-only, therefore NOT in ALLOWED and never reachable for the user's own
# account:
#
#   rpc Login (.google.protobuf.Empty) returns (.rpc.api.HomeLoginResponse)   [merged.proto]
#
# `Home/Login` is the client's daily "home login" (Auth/Login -> User/Get -> Home/Login in all
# 13 startup captures). It stamps `UserTime.last_login_time`, settles expired resources and
# licence renewals. The server answers `grpc-status 3 DATE_CHANGED` (ERROR_CODE 2005) to an
# account whose last_login_time is from an earlier game day; the client handles that by an
# out-game reload (`DateChangedOutGameReloadTrace`, global-metadata strings) that runs the
# startup again. A guest whose last_login_time is from an earlier day gets DATE_CHANGED; a
# guest with NO last_login_time is never refused; User/Get does not clear it. Only the
# project's disposable GUEST accounts (state/ranking_accounts.json) may call
# it, through `guest_home_login`, which checks the account name and host. Its sibling
# Home/AgreeRule stays off. Exact literal, never a prefix.
GUEST_ONLY_ALLOWED = frozenset({
    "/rpc.api.Home/Login",
})

# Fallback only. The real value is fetched from Master/Get at startup, exactly as the client
# does (t+0.49s in the capture, before it logs in). Kept so the tool still runs if that call
# fails, but a stale value here is the thing most likely to break login after a game update.
MASTER_VERSION_FALLBACK = "24afa2c641f4a831c024ac2e912972b2accb695ad0dd3a63fc47a2ffaa69e121"

PROFILE_FILE = STATE / "client_profile.json"

# Generic client identity, used for guest accounts. When state/client_profile.json exists
# (written by --import-from-capture) its captured headers are used instead for 'main', which is
# both coherent and truthful for the user's own account.
#
# `x-app-store-type` is required: the server REQUIRES the store type and does NOT require
# `x-app-steam-id`. Against us.game-hololive-dreams.com, /rpc.api.Master/Get, same second, no
# credential on any of them:
#     generic                 -> grpc-status 3 INVALID_ARGUMENT
#     generic + store-type    -> grpc-status 0
#     full profile - identity -> grpc-status 0
# So store-type present and steam-id absent is exactly the combination the server accepts, and
# it is the one that leaks least. Without this line every guest login dies at the very first
# call.
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

# The order the game's client sends its headers in (captured traffic shows this one, minus the
# headers a call leaves out). HTTP/2 keeps the order on the wire, so a request in any other
# order is one no real client makes. A header not listed here
# goes just before the Steam ID.
CLIENT_ORDER = [
    "user-agent", "te", "grpc-accept-encoding", "x-app-auth-token", "x-app-master-version",
    "x-app-request-id", "x-app-version", "x-app-device-name", "x-app-os-version",
    "x-app-lang-type", "x-app-os-type", "x-app-store-type", "x-app-bundle-id", "x-app-cpu",
    "x-app-gpu", "x-app-operating-system-family", "x-app-device-type",
    "x-app-graphics-device-type", "x-app-graphics-device-version",
    "x-app-graphics-device-vendor-id", "x-app-graphics-device-memory-size",
    "x-app-graphics-shader-level", "x-app-processor-model", "x-app-processor-count",
    "x-app-system-memory-size", "x-app-screen-dpi", "x-app-screen-height", "x-app-screen-width",
    "x-app-steam-id", "grpc-timeout", "content-type",
]
# What the client leaves out, per call (from the same captures). The two calls before login
# carry no Steam ID; those two, the login, User/Get and the private-room poll (never called
# here) carry no request id. Every other call carries both -- a ranking read without a fresh
# request id is answered from the server's cache. `header_check.py` re-measures this on a capture.
NO_STEAM_ID = frozenset({"/rpc.api.Master/Get", "/rpc.api.System/GetSystemInfo"})
NO_REQUEST_ID = frozenset({"/rpc.api.Master/Get", "/rpc.api.System/GetSystemInfo",
                           "/rpc.api.Auth/Login", "/rpc.api.User/Get",
                           "/rpc.api.MultiGame/CheckInvitedPrivateRoom"})


def client_headers(account: str) -> list[tuple[str, str]]:
    """Identity headers. Captured profile for 'main'; generic set for guests.

    The profile carries the user's real device name, CPU, GPU and Steam ID. That is correct
    and coherent for the user's own account, and gratuitous linkage on a throwaway guest --
    so guests get the generic set and no store claim.
    """
    if account == "main" and PROFILE_FILE.exists():
        prof = json.loads(PROFILE_FILE.read_text("utf8"))
        return [(k, v) for k, v in prof.get("headers", {}).items()
                if k.lower() not in PROFILE_BLOCK]
    return list(GENERIC_HEADERS)


_APP_VERSION_RE = re.compile(rb"(?<![\d.])\d+\.\d+\.\d+(?![\d.])")
_app_version_cache: list[str | None] = []


def app_version() -> str | None:
    """The client version the server will accept, or None to keep the header as written.

    The server refuses a client older than its current minimum with `grpc-status 3
    OUTDATED_APP` (e.g. on Master/Get: 1.2.1 accepted, 1.2.0 refused). Both places a version
    is written down (GENERIC_HEADERS and the captured client_profile.json) are frozen at
    whatever the client was that day, so a hardcoded version breaks every call here after a
    game update.

    `HOLODREAMS_APP_VERSION` wins. Otherwise it is read off the installed client: Unity's
    PlayerSettings.bundleVersion in data.unity3d. UnityPy cannot read that object's type tree
    on this build, so the raw object is scanned; it holds exactly one X.Y.Z string,
    and anything other than exactly one is treated as unreadable rather than guessed at.

    On a PC without the game (the desktop app) the last fallback is
    `dumps/client_version.json`: written by `tools/app/publish.py` from the publisher's install
    and synced to the app, and rewritten here when `fetch_master_version` had to probe past it.
    """
    env = os.environ.get("HOLODREAMS_APP_VERSION", "").strip()
    if env:
        return env
    if not _app_version_cache:
        found: set[str] = set()
        try:
            if str(HERE) not in sys.path:
                sys.path.insert(0, str(HERE))
            import UnityPy
            from game_paths import game_root
            data = os.path.join(game_root(), "hololive-Dreams_Data", "data.unity3d")
            for obj in UnityPy.load(data).objects:
                if obj.type.name == "PlayerSettings":
                    found |= {m.group().decode() for m in _APP_VERSION_RE.finditer(obj.get_raw_data())}
        except (Exception, SystemExit):  # no install, no UnityPy: keep the header as written
            found = set()
        _app_version_cache.append(found.pop() if len(found) == 1 else _published_version())
    return _app_version_cache[0]


CLIENT_VERSION_FILE = HERE / "dumps" / "client_version.json"


def _published_version() -> str | None:
    try:
        v = json.loads(CLIENT_VERSION_FILE.read_text("utf8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, str) and _APP_VERSION_RE.fullmatch(v.encode()) else None


def next_versions(v: str) -> list[str]:
    """The versions a game update can move to from `v`: patch, minor, major."""
    try:
        a, b, c = (int(x) for x in v.split("."))
    except ValueError:
        return []
    return [f"{a}.{b}.{c + 1}", f"{a}.{b + 1}.0", f"{a + 1}.0.0"]


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
            yield fn, wt, b[i:i + ln]
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
    nonce, ct = msg[4:4 + nlen], msg[4 + nlen:]
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
        ln = struct.unpack(">I", data[pos + 1:pos + 5])[0]
        yield data[pos + 5:pos + 5 + ln]
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
    rh, tr, _data = call(host, "/rpc.api.Home/Login", frame(b""), token=token, ident=ident,
                         master_version=master_version, _guest_account=account)
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st not in (None, "0"):
        raise RuntimeError(f"Home/Login rejected: grpc-status {st} {tr.get('grpc-message', '')}")


# An HTTP CONNECT proxy for the game's API hosts only (`host:port`, IP-whitelisted, no
# credentials), for a host whose own egress the API edge refuses. From one datacenter VPS,
# direct calls were refused (grpc 7, Google's HTML 403) with a Chrome AND a libcurl TLS
# fingerprint alike and answered through a residential proxy -- for that host the refusal is
# on the IP, not the fingerprint; other hosts may behave differently. The CDN hosts
# (static./asset.) never go through it: they answer any IP and are the heavy part.
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
    hdrs.sort(key=lambda kv: rank.get(kv[0].lower(), unknown))      # stable: unknowns keep theirs
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
            f"malformed header value, not a bad endpoint -- see RANKING-TOOL-SPEC.md 11f."
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
        self.used_at = 0.0        # last call or ping on the open connection (time.time())

    def ping(self, timeout: float = 10) -> bool:
        """An HTTP/2 PING on the open connection, waiting for its ACK. False (and the
        connection closed) when there is none to ping or it has died; the next `call` opens
        a fresh one. Through the proxy an idle connection is closed after 50-60 s; with a
        PING every 20 s it survives a 330 s gap and is reused. A fresh connection costs ~6.1 KB on the wire, a PING round trip under 100 B."""
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
                acked = any(isinstance(ev, h2.events.PingAckReceived)
                            for ev in self.conn.receive_data(chunk))
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

    def call(self, path: str, body: bytes, *, token: str | None = None, ident=None,
             master_version: str | None = None):
        _check_allowed(path, self.host, self.guest_account)
        for attempt in (0, 1):
            fresh = self.sock is None
            try:
                if fresh:
                    self.sock, self.conn = _open_h2(self.host, self.timeout)
                sid = self.conn.get_next_available_stream_id()
                self.conn.send_headers(sid, _request_headers(
                    self.host, path, token=token, ident=ident, master_version=master_version))
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


def call(host: str, path: str, body: bytes, *, token: str | None = None,
         ident: list[tuple[str, str]] | None = None, master_version: str | None = None,
         timeout: int = 30, _guest_account: str | None = None):
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
            f"malformed header value, not a bad endpoint -- see RANKING-TOOL-SPEC.md 11f."
        )
    return rh, tr, payload


def fetch_master_version(host: str, ident) -> str:
    """Current master-data version, unauthenticated.

    `MasterGetResponse { string version = 1; repeated MasterTagPack master_tag_packs = 2 }`,
    and the Master service declares disabled_game_auth_token + disabled_master_version -- so
    this needs no credential and no prior version. It is what the real client does at t+0.49s,
    before it logs in. Fetching it beats hardcoding: the master version identifies which build
    of the game's master data the client claims to hold, and a stale one is the most likely cause of a
    login being refused after a game update.
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
        # The game updated and nothing here knows yet (no install to read, or the published
        # version is behind): try the next patch/minor/major once each, keep the one accepted
        # for this process and the next ones. The same climb the 24/7 tracker does.
        # The base itself first: the version SENT may be an older one (a captured profile is
        # frozen at its capture: e.g. a profile's 1.1.1 is OUTDATED_APP while the generic 1.2.1 is
        # accepted, and a version that does not exist yet is INVALID_ARGUMENT).
        current = app_version() or dict(GENERIC_HEADERS)["x-app-version"]
        sent = next((v for k, v in (ident or GENERIC_HEADERS) if k.lower() == "x-app-version"), None)
        before = os.environ.get("HOLODREAMS_APP_VERSION")
        for cand in [v for v in [current, *next_versions(current)] if v != sent]:
            os.environ["HOLODREAMS_APP_VERSION"] = cand
            rh, tr, data = call(host, "/rpc.api.Master/Get", frame(b""), ident=ident)
            st = tr.get("grpc-status", rh.get("grpc-status"))
            if st in (None, "0"):
                print(f"client version {current} is outdated; the server accepts {cand}")
                atomic_io.write_text(CLIENT_VERSION_FILE, json.dumps(
                    {"version": cand, "source": "probed past OUTDATED_APP"}), "utf8")
                break
        else:
            if before is None:
                os.environ.pop("HOLODREAMS_APP_VERSION", None)
            else:
                os.environ["HOLODREAMS_APP_VERSION"] = before
    if st not in (None, "0"):
        msg = tr.get("grpc-message", "")
        if "IN_MAINTENANCE" in msg:
            raise GameMaintenance(f"Master/Get rejected: grpc-status {st} {msg}")
        raise RuntimeError(f"Master/Get rejected: grpc-status {st} {msg}")
    return data


def login(host: str, credential: bytes, ident, master_version: str) -> str:
    rh, tr, data = call(host, "/rpc.api.Auth/Login", frame(seal(pb_bytes_field(1, credential))),
                        ident=ident, master_version=master_version)
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


EVENT_INFO_PATH = Path(__file__).resolve().parent / "dumps" / "event_info_live.bin"


def refresh_event_info(host: str, token: str, ident, master_version: str) -> Path:
    """`Event/ListEventInfo` -> the decrypted response, written to dumps/event_info_live.bin.

    It nests the open marathon's bonus tables (`MarathonLiveMarathonScoreBonus`, the event bonus
    the optimiser's Event Pt / Event Song Score modes need); `marathon_bonus.py` reads the file
    the same way it reads a capture. The optimiser must always work with the CURRENT event, so
    the refresh fetches it."""
    rh, tr, data = call(host, "/rpc.api.Event/ListEventInfo", frame(b""), token=token,
                        ident=ident, master_version=master_version)
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st not in (None, "0"):
        raise RuntimeError(f"Event/ListEventInfo rejected: grpc-status {st} "
                           f"{tr.get('grpc-message', '')}")
    for msg in unframe(data):
        if msg:
            atomic_io.write_bytes(EVENT_INFO_PATH, unseal(msg))
            print(f"event info: {EVENT_INFO_PATH.name} ({EVENT_INFO_PATH.stat().st_size} bytes)")
            return EVENT_INFO_PATH
    raise RuntimeError("empty Event/ListEventInfo response")


def fetch_userdata(host: str, token: str, ident, master_version: str) -> bytes:
    rh, tr, data = call(host, "/rpc.api.User/Get", frame(b""), token=token,
                        ident=ident, master_version=master_version)
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st not in (None, "0"):
        raise RuntimeError(f"User/Get rejected: grpc-status {st} {tr.get('grpc-message', '')}")
    for msg in unframe(data):
        if msg:
            return unseal(msg)
    raise RuntimeError("empty User/Get response")


# ---------------------------------------------------------------- decode
def load_userdata_schema() -> dict[int, str]:
    p = PROTO_DIR / "rpc" / "api" / "common" / "user_data.gen.proto"
    if not p.exists():
        return {}
    return {int(m.group(2)): m.group(1) for m in
            re.finditer(r"^\s*(?:repeated\s+)?\S+\s+(\w+)\s*=\s*(\d+);", p.read_text("utf8"), re.M)}


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
# is done; the 54 Pokajan! per-character 3-Card missions all have threshold 1.
MISSION = {2: "mission_id", 4: "progress", 5: "reward_received_thresholds"}
EMBLEM = {2: "emblem_id", 5: "last_activated_time"}

WANT = {
    "user_card_list": ("cards", CARD),
    "user_character_list": ("characters", CHAR),
    "user_character_skill_tree_list": ("boards", TREE),
    "user_skill_tree_point_list": ("board_points", POINT),
    "user_live_deck_list": ("decks", DECK),
    "user_live_deck_position_list": ("deck_positions", POS),
    # The poster COUNT drives the Memory Bonus rate (memory_bonus_permil,
    # PosterCollectEffect); user_poster_list makes it a live read instead of a
    # hand-entered value in state/collection.json's player block.
    "user_poster_list": ("posters", POSTER),
    # Read-only, and it costs nothing extra: User/Get already returns this collection
    # and decode_snapshot already walked past it. It answers "which of the 54 Pokajan!
    # achievements are done" from the ACCOUNT rather than from a log the bot keeps.
    "user_mission_list": ("missions", MISSION),
    # Event history: a marathon's ranking reward includes one emblem per chapter and tier
    # (`emblem-marathon_score_ranking_rank_reward-<chapter>-<grade>`), so the emblems held
    # say which tier the account reached in every past event -- the one record of events
    # older than the tool's own reads. Same User/Get response, nothing extra fetched.
    "user_emblem_list": ("emblems", EMBLEM),
}


def load_level_curves() -> tuple[dict, dict]:
    """(card_id -> cardLevelGroupId, group_id -> sorted [(cum_exp, level)]).

    The chain is `Card.card_level_group_id` -> `CardLevel.group_id`, and CardLevel rows are
    `{group_id, level, parameter_base_value, exp, live_deck_power_permyriad_up}` where `exp` is
    the CUMULATIVE exp needed to reach that level. 27 groups (9 per rarity), 60/70/80 rows each,
    matching the rarity caps. Resolving a level is a bisect: the highest level whose threshold
    the card's exp has passed.
    """
    holo = DUMPS / "holodori"
    cf, lf = holo / "main_Card.json", holo / "main_CardLevel.json"
    if not (cf.exists() and lf.exists()):
        return {}, {}
    groups = {r["id"]: r["data"]["cardLevelGroupId"]
              for r in json.loads(cf.read_text("utf8"))}
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
        print("     COLLECTION-MODEL.md says 'max' reproduces pre-file behaviour and is meant")
        print("     to be edited downward. This is the measurement that lets you edit it.")
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
        print(f"    most common levels                    : "
              + ", ".join(f"lvl {v} x{n}" for v, n in top))
    else:
        print("\n  NOTE: levels not resolved -- dumps/holodori/main_Card.json and")
        print("        main_CardLevel.json are needed to turn exp into a level.")


def propose_collection(snap: dict) -> None:
    """Write a CANDIDATE collection.json to dumps/ -- never to state/.

    `data/cards.json` is authoritative and hand-edited, and `state/collection.json` is the
    user's account; this path writes neither. It produces a
    file to read, check and copy across deliberately -- the review is the point, not a
    formality: every value here is derived from one snapshot at one moment.
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
    DUMPS.mkdir(exist_ok=True)
    path = DUMPS / "collection_proposed.json"
    atomic_io.write_text(path, json.dumps(out, indent=1, ensure_ascii=False), "utf8")
    print(f"\nproposed  : {path}")
    print(f"            {len(entries)} owned cards enumerated, card_default = 'unknown'")
    if missing:
        print(f"            ! {len(missing)} without a resolved level (left absent, not guessed)")
    print("            REVIEW before copying to state/collection.json -- nothing was written there.")


def refresh_card_params(host: str, token: str, ident, master_version: str) -> None:
    """Re-fetch `Card/GetParameters` live and rewrite dumps/card_params.json.

    Same records `parse_card_params.py` produces from a capture, but without needing one --
    which matters because that file is what mode B (`--real-stats`) ranks on, and it goes stale
    the moment a card is acquired: a card in owned.json with no entry here makes mode B refuse
    to run.

    `CardGetParametersResponse.ParameterInfo { card_id=1, parameter=2, performance=3,
    technique=4, sense=5 }` -- so the studio's own schema NAMES the fields:
    stat3/4/5 = Performance/Technique/Sense.
    """
    rh, tr, data = call(host, "/rpc.api.Card/GetParameters", frame(b""), token=token,
                        ident=ident, master_version=master_version)
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st not in (None, "0"):
        raise RuntimeError(f"Card/GetParameters rejected: grpc-status {st} "
                           f"{tr.get('grpc-message', '')}")
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
            rows.append({
                "id": cid,
                "card_num": parts[1] if len(parts) > 1 else "",
                "rarity": parts[3] if len(parts) > 3 else "",
                "total": total,
                "stat3": s3, "stat4": s4, "stat5": s5,
                "total_matches_sum": total == s3 + s4 + s5,
            })
    if not rows:
        raise RuntimeError("Card/GetParameters returned no parameter_infos")

    DUMPS.mkdir(exist_ok=True)
    out = DUMPS / "card_params.json"
    prev = len(json.loads(out.read_text("utf8"))) if out.exists() else 0
    atomic_io.write_text(out, json.dumps(rows, indent=1, ensure_ascii=False), "utf8")
    bad = [r["id"] for r in rows if not r["total_matches_sum"]]
    print(f"\ncard params: {out}")
    print(f"            {prev} -> {len(rows)} cards")
    print(f"            total == stat3+stat4+stat5 for {len(rows) - len(bad)}/{len(rows)}"
          + (f"  ! mismatches: {bad[:3]}" if bad else ""))


def refresh_board_state(snap: dict) -> None:
    """Write `dumps/board_state.json` from the live snapshot.

    That file is `build_board.py`'s input, and until now it only came from a hand-taken capture
    (`board_state.py`), so it aged the moment a node was unlocked. Its shape is exactly what
    `UserCharacterSkillTree` already carries:

        {"chr-00004": {"nodes": ["G-001", ...], "slots": {"S-001": "card-..."}}}

    `released_skill_tree_node_group_ids` -> `nodes`, and the connected node/card lists arrive
    positionally paired -> `slots`. No mapping table, no master lookup: the ids are already in
    `domain/board.py`'s vocabulary.

    This does NOT regenerate `board.json` -- run `build_board.py` after, which performs the
    three joins (character name, effect ids, Connect multiplier) that this file deliberately
    knows nothing about.
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
        # UNION, not just `released`. The server can report a socket as connected (it holds a
        # card) while omitting it from released_skill_tree_node_group_ids -- chr-04003 has
        # S-001 carded but unreleased, S-002 in both. A socket holding a card is unlocked by
        # definition, so taking released verbatim would silently drop it and shrink the board.
        nodes = sorted(set(released) | set(gids))
        out_doc[cid] = {"nodes": nodes, "slots": dict(zip(gids, cids))}

    DUMPS.mkdir(exist_ok=True)
    path = DUMPS / "board_state.json"
    prev = json.loads(path.read_text("utf8")) if path.exists() else {}
    # Write the backup ONCE. Overwriting it every run means the second run destroys the only
    # copy of the pre-tool file and "backup" silently becomes "the previous run's output".
    backup = DUMPS / "board_state_before_refresh.json"
    if prev and not backup.exists():
        atomic_io.write_text(backup, json.dumps(prev, indent=1, ensure_ascii=False), "utf8")

    atomic_io.write_text(path, json.dumps(dict(sorted(out_doc.items())), indent=1,
                               ensure_ascii=False), "utf8")

    def tot(d, key):
        return sum(len(v.get(key) or ()) for v in d.values())

    print(f"\nboard state: {path}")
    print(f"            characters {len(prev)} -> {len(out_doc)}; "
          f"nodes {tot(prev, 'nodes')} -> {tot(out_doc, 'nodes')}; "
          f"slots {tot(prev, 'slots')} -> {tot(out_doc, 'slots')}")
    print("            now run build_board.py to regenerate dumps/board.json")


def merge_collection(snap: dict) -> None:
    """MERGE the snapshot's cards into state/collection.json, preserving everything else.

    Explicitly opt-in (`--merge-collection`), never automatic: that file is the user's account.
    It merges rather than overwrites for a concrete reason -- collection.json
    carries hand-entered `player` data (level, power_up_permyriad) that a snapshot has no
    notion of, so writing the candidate over the top would silently destroy it. `cards` and
    `card_default` are replaced, and `player.posters` is set from the account's own
    `user_poster_list` count when the snapshot carries it (it drives the Memory Bonus permil;
    a hand-entered count one behind the account shows as a power residual on every deck). `player.level` and `player.power_up_permyriad` stay hand-entered.
    Every other key is left exactly as it was.

    `data/cards.json` is never touched.
    """
    state = STATE / "collection.json"
    cur = json.loads(state.read_text("utf8")) if state.exists() else {
        "schema_version": "collection-1"}

    DUMPS.mkdir(exist_ok=True)
    # Once only -- see refresh_board_state. Re-backing-up on every run replaces the pre-tool
    # file with the previous run's output, which is the opposite of what a backup is for.
    backup = DUMPS / "collection_before_import.json"
    if state.exists() and not backup.exists():
        atomic_io.write_text(backup, state.read_text("utf8"), "utf8")

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
    # A collection born from a snapshot (the desktop app's first refresh) has no
    # `power_up_permyriad`, and absent is "not told" -> no Member Power-Up Bonus at all
    # (a Unit Score several % below the game's). "auto" derives it
    # from the account's own cards (`collection.POWER_UP_AUTO`), so it guesses nothing;
    # a value already there, typed or "auto", is kept.
    # Cards TRIED as if owned (the Collection page's "Try as owned") end at the refresh: the
    # account is read again and it does not have them. One the account now owns is simply
    # owned.
    if merged.get("trial"):
        posters_note += ("; " if posters_note else "") + f"trial cards cleared ({len(merged['trial'])})"
    merged.pop("trial", None)
    if "power_up_permyriad" not in (merged.get("player") or {}):
        merged["player"] = {**(merged.get("player") or {}), "power_up_permyriad": "auto"}
        posters_note += ("; " if posters_note else "") + "player.power_up_permyriad absent -> 'auto'"
    atomic_io.write_text(state, json.dumps(merged, indent=1, ensure_ascii=False), "utf8")

    print(f"\nmerged    : {state}")
    if state.exists():
        print(f"            backup -> {backup}")
    print(f"            cards {before_n} -> {len(entries)}; "
          f"card_default {before_def!r} -> 'unknown'")
    if posters_note:
        print(f"            {posters_note} (the account's own user_poster_list count)")
    print(f"            preserved untouched: {', '.join(k for k in preserved if k != 'player') or '(nothing else present)'}"
          + ("; player.level / power_up_permyriad kept" if "player" in preserved else ""))
    print("            data/cards.json and data/owned.json not touched.")


# ---------------------------------------------------------------- credential handling
def import_from_capture(capture: str) -> None:
    """One-off: lift the account credential out of a capture into state/ (gitignored)."""
    sys.path.insert(0, str(HERE))
    from grpc_decrypt import decrypt_exact
    from mitmproxy import io as mio

    src = HERE / capture if not os.path.isabs(capture) else Path(capture)
    found = None
    with open(src, "rb") as f:
        for fl in mio.FlowReader(f).stream():
            if fl.type != "http" or fl.request.path != "/rpc.api.Auth/Login":
                continue
            got = decrypt_exact(fl.request.raw_content or b"")
            if not got:
                continue
            for fn, wt, v in fields(got[1]):
                if fn == 1 and wt == 2:
                    hdrs = {k: val for k, val in fl.request.headers.items()
                            if k.lower().startswith(("x-app-", "user-agent"))
                            and k.lower() not in PROFILE_BLOCK}
                    found = (v, fl.request.pretty_host, hdrs)
    if not found:
        sys.exit("no Auth/Login request with a credential found in that capture")
    cred, host, hdrs = found
    STATE.mkdir(exist_ok=True)
    atomic_io.write_text(CRED_FILE, json.dumps({
        "host": host,
        "credential_b64": base64.b64encode(cred).decode(),
        "source": f"extracted from {src.name}",
        "_warning": "This IS the account. Full access, no second factor. state/ is gitignored.",
    }, indent=1), "utf8")
    print(f"wrote {CRED_FILE}  (credential {len(cred)} B, host {host}; value not printed)")

    # The client's own identity headers, so a 'main' run is internally consistent: the captured
    # client sends x-app-store-type: Steam TOGETHER WITH x-app-steam-id, and claiming one
    # without the other is a request no real client would make. These carry the user's device
    # name, CPU, GPU and Steam ID -- true of the user's own account, which is why they live in
    # state/ (gitignored) and are never used for a guest.
    atomic_io.write_text(PROFILE_FILE, json.dumps({
        "host": host,
        "headers": dict(hdrs),
        "source": f"extracted from {src.name}",
        "_warning": "Contains the owner's device fingerprint and Steam ID. state/ is gitignored.",
    }, indent=1), "utf8")
    print(f"wrote {PROFILE_FILE}  ({len(hdrs)} identity headers)")


def import_from_localstore(path: str, host: str = "us.game-hololive-dreams.com") -> None:
    """Lift the persistent account credential out of the game's OWN LocalLow store.

    The WorldWide (Steam) client persists the login credential as a standalone BOM-prefixed
    file under %USERPROFILE%/AppData/LocalLow/QualiArts, Inc_/hololive-Dreams/. It is the SAME
    255-byte credential `import_from_capture` lifts from an Auth/Login request body, so no
    capture is needed to obtain it -- reading the game's own data on the user's own machine.

    The value is written to state/ (gitignored) and NEVER printed. The host defaults to the
    WorldWide region (US, lowest ping for a European player); a login that returns grpc-status
    16 means the account lives in another region -- rerun with --host jp or --host as.
    """
    raw = Path(path).read_bytes()
    cred = raw[3:] if raw[:3] == b"\xef\xbb\xbf" else raw           # strip a UTF-8 BOM if present
    if not (200 <= len(cred) <= 300):
        sys.exit(f"unexpected credential length {len(cred)} B (expected ~255) -- refusing to "
                 f"write a guess; is {path} really the credential file?")
    STATE.mkdir(exist_ok=True)
    atomic_io.write_text(CRED_FILE, json.dumps({
        "host": host,
        "credential_b64": base64.b64encode(cred).decode(),
        "source": f"extracted from game LocalLow {os.path.basename(path)} (BOM-stripped)",
        "_warning": "This IS the account. Full access, no second factor. state/ is gitignored.",
    }, indent=1), "utf8")
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
    except (Exception, SystemExit):  # noqa: BLE001 -- unknown is not maintenance
        return None
    return False


def load_credential(account: str) -> tuple[str, bytes]:
    if account == "main" and os.environ.get("HB_CREDENTIAL_FILE"):
        # The desktop app (tools/app/app_settings.py): the game's own LocalLow credential file,
        # read in place at every refresh and never copied (a path set in the app's Settings).
        raw = Path(os.environ["HB_CREDENTIAL_FILE"]).read_bytes()
        cred = raw[3:] if raw[:3] == b"\xef\xbb\xbf" else raw
        if not 200 <= len(cred) <= 300:
            sys.exit(f"unexpected credential length {len(cred)} B in HB_CREDENTIAL_FILE")
        return os.environ.get("HB_ACCOUNT_HOST", "us.game-hololive-dreams.com"), cred
    if account == "main":
        if not CRED_FILE.exists():
            sys.exit("no account file: set HB_CREDENTIAL_FILE to the game's own account file "
                     "(refresh.cmd finds it in the game's LocalLow folder; docs/run-locally.md), "
                     f"or write {CRED_FILE} with --import-from-localstore")
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
    ap.add_argument("--import-from-capture", metavar="MITM", help="lift the credential from a capture")
    ap.add_argument("--import-from-localstore", metavar="FILE",
                    help="lift the credential from the game's LocalLow store (no capture needed)")
    ap.add_argument("--host", default="us.game-hololive-dreams.com",
                    help="API region host (us/jp/as .game-hololive-dreams.com)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent; open no socket")
    ap.add_argument("--propose-collection", action="store_true",
                    help="write a CANDIDATE collection.json to dumps/ for the owner to review")
    ap.add_argument("--merge-collection", action="store_true",
                    help="MERGE cards into state/collection.json (backs up first, keeps 'player')")
    ap.add_argument("--refresh-events", action="store_true",
                    help="also fetch Event/ListEventInfo (the open event's bonus tables)")
    ap.add_argument("--refresh-card-params", action="store_true",
                    help="re-fetch Card/GetParameters into dumps/card_params.json (mode B input)")
    ap.add_argument("--refresh-board-state", action="store_true",
                    help="write dumps/board_state.json from the snapshot (build_board.py input)")
    ap.add_argument("--out", default=None, help="where to write the snapshot JSON")
    args = ap.parse_args()

    if args.import_from_capture:
        import_from_capture(args.import_from_capture)
        return
    if args.import_from_localstore:
        import_from_localstore(args.import_from_localstore, args.host)
        return
    if not args.snapshot:
        ap.error("nothing to do -- pass --snapshot (optionally with --diff)")

    host, credential = load_credential(args.account)
    ident = client_headers(args.account)
    profile = "this PC's client profile" if (args.account == "main" and PROFILE_FILE.exists()) \
        else "generic (no store claimed)"

    print(f"host      : {host}")
    print(f"account   : {args.account} (credential {len(credential)} B, not printed)")
    print(f"whitelist : {sorted(ALLOWED)}")
    print(f"identity  : {profile}, {len(ident)} headers")

    if args.dry_run:
        print("\n--dry-run: no socket will be opened. Requests that would be sent:")
        for path, what in (("/rpc.api.Master/Get", "no auth, no master-version"),
                           ("/rpc.api.Auth/Login", "credential in sealed envelope"),
                           ("/rpc.api.User/Get", "empty body + auth token")):
            print(f"    POST https://{host}{path}   ({what})")
        print("\n  identity headers (values shown; none are secret):")
        for k, v in ident:
            print(f"    {k}: {v}")
        print(f"\n  plus {[k for k, _ in WIRE_HEADERS]}, a fresh x-app-request-id,")
        print("  and x-app-master-version fetched at runtime from Master/Get.")
        return

    master_version = fetch_master_version(host, ident)
    stale = master_version != MASTER_VERSION_FALLBACK
    print(f"master    : {master_version[:16]}...  "
          f"({'CHANGED since the capture' if stale else 'same as the capture'})")

    token = login(host, credential, ident, master_version)
    print(f"login     : OK, token {len(token)} chars (not printed)")
    blob = fetch_userdata(host, token, ident, master_version)
    print(f"snapshot  : {len(blob):,} B plaintext")

    snap = decode_snapshot(blob)
    resolved = resolve_levels(snap)
    print(f"decoded   : {snap['_collections']} populated user_* collections"
          + (f"; {resolved} card levels resolved from exp" if resolved else ""))
    for key in ("cards", "characters", "boards", "board_points", "decks",
                "deck_positions", "posters", "emblems"):
        if key in snap:
            print(f"    {len(snap[key]):>4}  {key}")
    if "posters" in snap:
        pc = len(snap["posters"])
        print(f"    -> poster count = {pc} (drives Memory Bonus; state/collection.json "
              f"player.posters follows it on --merge-collection)")

    DUMPS.mkdir(exist_ok=True)
    out = Path(args.out) if args.out else DUMPS / f"account_snapshot_{args.account}.json"
    atomic_io.write_text(out, json.dumps(snap, indent=1, ensure_ascii=False), "utf8")
    print(f"written   : {out}")
    print("            (dumps/ is gitignored; data/cards.json is never written)"
          if args.merge_collection else
          "            (dumps/ is gitignored; cards.json and collection.json untouched)")

    if args.propose_collection:
        propose_collection(snap)

    # ⚠ ORDER MATTERS, and it is the opposite of the obvious one. Everything
    # here except `--refresh-card-params` is a pure function of the snapshot
    # ALREADY IN HAND, so none of it may be hostage to one more network call:
    # if `Card/GetParameters` comes back DATE_CHANGED, the exception unwinds
    # `main`, and `--merge-collection` would never run -- leaving the solver
    # scoring cards at stale levels while every counter reports success. So
    # the offline work runs FIRST.
    if args.refresh_board_state:
        refresh_board_state(snap)

    if args.merge_collection:
        merge_collection(snap)

    if args.diff:
        diff_against_collection(snap)

    # `User/Get` succeeds while `Event/ListEventInfo` and `Card/GetParameters` are
    # refused `grpc-status 3 DATE_CHANGED` once the in-game day has rolled over
    # (04:00 JST) and the CLIENT has not processed the reset yet. Not rate limiting,
    # not a stale credential: it clears as soon as the game itself is opened once.
    # Diagnosed rather than raised, because a traceback here reads as "the account
    # broke"; each call is still tried, so one that is answered is not lost.
    refused = []
    for wanted, rpc, read in ((args.refresh_events, "Event/ListEventInfo", refresh_event_info),
                              (args.refresh_card_params, "Card/GetParameters", refresh_card_params)):
        if not wanted:
            continue
        try:
            read(host, token, ident, master_version)
        except RuntimeError as exc:
            if "DATE_CHANGED" not in str(exc):
                raise
            refused.append(rpc)
    if refused:
        sys.stdout.flush()      # the report above first: a reader of the merged output sees this last
        for line in (
            "",
            f"! {' and '.join(refused)} refused: DATE_CHANGED.",
            "  The in-game day rolled over (04:00 JST) and this account has not",
            "  opened the game since. Nothing is wrong with the credential -- the",
            "  server wants the CLIENT to run its daily reset first.",
            "  FIX: open the game once, then re-run.",
            "  What those calls refresh (dumps/card_params.json, the open event) is",
            "  UNCHANGED; the snapshot, board state and collection above ARE current.",
        ):
            print(line, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
