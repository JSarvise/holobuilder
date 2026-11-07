"""account_snapshot._request_headers: the game client's headers, order and per-call omissions
(as seen in 111 captured requests). Synthetic identity values only."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_snapshot as A  # noqa: E402

# a profile in the order a captured profile is stored in (the capture's own dict order is not
# the wire's: client_headers keeps whatever order the file has)
IDENT = [("x-app-steam-id", "76561190000000000"), ("x-app-screen-width", "1920"),
         ("user-agent", A.GENERIC_HEADERS[0][1]), ("x-app-version", "1.2.1"),
         ("x-app-device-name", "PC (Maker)"), ("x-app-os-version", "Windows 11  (10.0.26200)"),
         ("x-app-lang-type", "eng"), ("x-app-os-type", "Windows"), ("x-app-store-type", "Steam"),
         ("x-app-bundle-id", "game.qualiarts.hololive.dreams.com"), ("x-app-cpu", "CPU "),
         ("x-app-gpu", "GPU"), ("x-app-operating-system-family", "Windows"),
         ("x-app-device-type", "Desktop"), ("x-app-graphics-device-type", "Direct3D11"),
         ("x-app-graphics-device-version", "Direct3D 11.0 [level 11.1]"),
         ("x-app-graphics-device-vendor-id", "4318"), ("x-app-graphics-device-memory-size", "8192"),
         ("x-app-graphics-shader-level", "50"), ("x-app-processor-model", "CPU "),
         ("x-app-processor-count", "16"), ("x-app-system-memory-size", "32000"),
         ("x-app-screen-dpi", "96"), ("x-app-screen-height", "1080")]


def names(path, **kw):
    kw = {"token": "t", "master_version": "m", **kw}
    return [k for k, _ in A._request_headers("us.example", path, ident=IDENT, **kw)]


def test_an_authenticated_call_is_the_clients_order_exactly(monkeypatch):
    monkeypatch.setattr(A, "app_version", lambda: None)
    got = names("/rpc.api.Live/GetDeck")
    assert got[:4] == [":method", ":scheme", ":authority", ":path"]
    assert got[4:] == A.CLIENT_ORDER                 # the capture's 84-request order


def test_calls_leave_out_what_the_client_leaves_out(monkeypatch):
    monkeypatch.setattr(A, "app_version", lambda: None)
    pre = names("/rpc.api.Master/Get", token=None, master_version=None)
    assert not {"x-app-steam-id", "x-app-request-id", "x-app-auth-token", "x-app-master-version"} & set(pre)
    login = names("/rpc.api.Auth/Login", token=None)
    assert "x-app-steam-id" in login and "x-app-master-version" in login
    assert "x-app-request-id" not in login and "x-app-auth-token" not in login
    user = names("/rpc.api.User/Get")
    assert "x-app-request-id" not in user and "x-app-steam-id" in user
    assert "x-app-request-id" in names("/rpc.api.Marathon/Top")   # rankings must not be cached
    # the subsets keep the full order
    for got in (pre, login, user):
        assert got[4:] == [k for k in A.CLIENT_ORDER if k in got]


def test_the_wire_values_and_the_live_version(monkeypatch):
    monkeypatch.setattr(A, "app_version", lambda: "9.9.9")
    h = dict(A._request_headers("us.example", "/rpc.api.User/Get", token="t", ident=IDENT,
                                master_version="m"))
    assert h["grpc-timeout"] == "10S" and h["x-app-version"] == "9.9.9"
    guest = dict(A._request_headers("us.example", "/rpc.api.Marathon/Top", token="t", ident=None,
                                    master_version="m"))
    assert "x-app-steam-id" not in guest and guest["x-app-store-type"] == "Steam"
