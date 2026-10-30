"""`GET /api/gacha`, `GET /gachaart/<gacha_id>/<file>.png`, `POST /api/gacha/sync`.

⚠ This file writes NO files: `tools/web/test_no_writes.py` walks every `.py`
under `tools/web/` and would reject a `write_text`/`mkdir`/`open(...,"w")`
here too. The store fixture is an in-memory dict injected via monkeypatching
`gacha_view.load_store`, never on disk. The art test instead reads the real,
committed, read-only `data/gacha/art/gacha-pickup-normal-260807/` directory
(banner.png/logo.png/icon.png) rather than writing a fixture PNG.
"""
from __future__ import annotations
import json, socket, threading, urllib.request, urllib.error
import pytest
import server as web_server


STORE = {
    "banners": [
        {
            "gacha_id": "gacha-a", "name": "A", "type": 1,
            "start_time": 1, "end_time": 2, "pickup_card_ids": [], "seen_at": 5,
            "raw": {},
        }
    ],
    "pulls": [
        {
            "draw_time": 1000, "gacha_name": "A", "gacha_id": "gacha-a",
            "card_id": "card-00001-5-x", "first_seen": 5,
        }
    ],
}


def _free_port():
    with socket.socket() as s:
        s.bind((web_server.BIND_HOST, 0)); return s.getsockname()[1]


@pytest.fixture
def web_base(monkeypatch):
    monkeypatch.setattr(web_server.gacha_view, "load_store", lambda _d: STORE)
    port = _free_port()
    srv = web_server.ThreadingServer((web_server.BIND_HOST, port), web_server.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        srv.shutdown(); srv.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def test_api_gacha_returns_view_model(web_base):
    st, ct, body = _get(web_base + "/api/gacha")
    assert st == 200 and "json" in ct
    v = json.loads(body)
    assert v["totals"]["pulls"] == 1 and v["banners"][0]["gacha_id"] == "gacha-a"
    assert v["banners"][0]["art"]["banner"] is False and v["banners"][0]["has_rates"] is False


def test_gachaart_serves_png_and_rejects_bad_paths(web_base):
    st, ct, body = _get(web_base + "/gachaart/gacha-pickup-normal-260807/banner.png")
    assert st == 200 and ct == "image/png" and body.startswith(b"\x89PNG")
    assert _get(web_base + "/gachaart/gacha-a/logo.png")[0] == 404
    assert _get(web_base + "/gachaart/gacha-a/other.png")[0] == 400
    assert _get(web_base + "/gachaart/../x/banner.png")[0] == 400
    assert _get(web_base + "/gachaart/GACHA-A/banner.png")[0] == 400
    assert _get(web_base + "/gachaart/gacha-a/banner.gif")[0] == 400   # not one of ours


def test_gachaart_extensionless_serves_whichever_file_exists(web_base):
    """What the page and the export ask for. New banners are saved as WEBP and the older
    masters are PNG; one URL has to work for both, and it must NOT invent the missing one."""
    st, ct, body = _get(web_base + "/gachaart/gacha-pickup-normal-260807/banner")
    assert st == 200 and ct == "image/png" and body.startswith(b"\x89PNG")
    # the same banner asked for BY the extension it does not have on disk
    assert _get(web_base + "/gachaart/gacha-pickup-normal-260807/banner.webp")[0] == 404
    assert _get(web_base + "/gachaart/gacha-a/banner")[0] == 404       # no art for this one at all


def test_gachaart_scales_to_webp_when_a_width_is_asked_for(web_base):
    """`?w=N` must downscale AND say so in the mime: the gallery asks for 640 px and
    would otherwise paint a 2048 px master scaled by the browser (the reported blur).
    The master stays untouched on disk -- this asserts the response, not the file."""
    pytest.importorskip("PIL.Image")
    url = web_base + "/gachaart/gacha-pickup-normal-260807/banner.png"
    st, ct, small = _get(url + "?w=320")
    assert st == 200 and ct == "image/webp" and small[:4] == b"RIFF"
    assert len(small) < len(_get(url)[2])


def _post(url, data=b""):
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_gacha_sync_refuses_a_second_run_while_one_holds_the_lock(web_base, monkeypatch):
    """The sync SPENDS a login and appends to the store; two at once would do both twice.
    The lock is held here by the test, so NO subprocess is spawned -- that is the point:
    the refusal happens before `_run_recon_script` is ever reached."""
    held = threading.Lock()
    assert held.acquire(blocking=False)
    monkeypatch.setattr(web_server, "_GACHA_SYNC_LOCK", held)
    monkeypatch.setattr(web_server.Handler, "_run_recon_script",
                        lambda self, script, tag: pytest.fail("a subprocess was started"))
    st, body = _post(web_base + "/api/gacha/sync")
    assert st == 409 and json.loads(body) == {"error": "a gacha sync is already running"}
    held.release()


def test_parse_sync_summary_takes_last_summary_line():
    out = "login OK\nSUMMARY {\"pulls\": 1}\nnoise\nSUMMARY {\"pulls\": 2, \"banners\": 0}\n"
    assert web_server._parse_sync_summary(out) == {"pulls": 2, "banners": 0}
    assert web_server._parse_sync_summary("nothing") is None


def test_parse_wrote_takes_last_wrote_line():
    out = "3 banners\nWROTE C:\\a\\old.html\nnoise\nWROTE C:\\a\\gacha_export_1.html\n"
    assert web_server._parse_wrote(out) == "C:\\a\\gacha_export_1.html"
    assert web_server._parse_wrote("nothing written") is None
    assert web_server._parse_wrote("WROTE   \n") is None


def test_refresh_refuses_a_second_run_while_one_is_running(web_base, monkeypatch):
    """A page reloaded mid-refresh and the button pressed again must not start a
    SECOND refresh_all.py beside the first. The lock is held by
    the test, so no subprocess is started; the lock is free again afterwards."""
    held = threading.Lock()
    assert held.acquire(blocking=False)
    monkeypatch.setattr(web_server, "_REFRESH_LOCK", held)
    monkeypatch.setattr(web_server.Handler, "_refresh",
                        lambda self: pytest.fail("a refresh was started"))
    st, body = _post(web_base + "/api/refresh")
    assert st == 409 and "already running" in json.loads(body)["error"]
    held.release()
    monkeypatch.setattr(web_server.Handler, "_refresh",
                        lambda self: self._json(200, {"returncode": 0}))
    assert _post(web_base + "/api/refresh")[0] == 200
    assert not held.locked()
