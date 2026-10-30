"""`GET /ui/<category>/<sprite>` serves the game's official UI sprites
(`dumps/ui_official/`, `tools/recon/UI-OFFICIAL-ASSETS.md` §5) to the Night
Deck shell and components (`docs/redesign/IDENTITY.md`).

Pinned: a real sprite is a PNG, a missing one is a first-class 404 (never a
placeholder), and nothing outside the folder is reachable: no traversal, no
second level, no non-image (the `views/` text dumps)."""

from __future__ import annotations

import socket
import threading
import urllib.error
import urllib.request

import pytest

import server as web_server


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def base():
    port = _free_port()
    srv = web_server.ThreadingServer((web_server.BIND_HOST, port), web_server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def _get(url: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, b"", ""


@pytest.mark.skipif(not (web_server.UI_OFFICIAL_DIR / "grade" / "ui_999_txt_score_s.png").is_file(),
                    reason="dumps/ui_official not extracted on this machine")
def test_a_real_sprite_is_served_as_png(base):
    code, body, ctype = _get(f"{base}/ui/grade/ui_999_txt_score_s.png")
    assert code == 200 and ctype == "image/png" and body[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_missing_sprite_is_404_not_a_placeholder(base):
    assert _get(f"{base}/ui/grade/ui_999_txt_score_z.png")[0] == 404


@pytest.mark.parametrize("name", [
    "../card_chrome/x.png",          # traversal
    "grade/../../cards.png",         # traversal inside the name
    "a/b/c.png",                     # second level
    "views/sprite_usage.json",       # not an image
    "manifest.json",                 # no category
    "Grade/ui_999_ico_plus.png",     # uppercase category
])
def test_names_outside_the_contract_are_refused(base, name):
    assert _get(f"{base}/ui/{name}")[0] in (400, 404)
