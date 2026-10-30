"""`GET /cardart/<family>/<card id>`: a card whose art is not in the game-cache
directory (`dumps/card_images/`) is served from the art directory the refresh
fills (`<id>.webp`, `<id>.sq.webp`).

Without the fallback, every card the game ships after `card_images/` was last
filled answers 404 while its art sits in the explorer art directory. Both
directories here are fakes
(a real image copied in by the builder's writer is not needed: the route is
asked for the ORIGINAL bytes, no `?w=`)."""
from __future__ import annotations

import shutil
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import server as web_server

_REPO = Path(__file__).resolve().parents[2]
_REAL_ART = _REPO / "tools" / "explorer" / "cards_explorer_art"
OLD, NEW = "card-00001-5-uniq-0000-00", "card-04012-5-uniq-0087-00"


@pytest.fixture()
def web(tmp_path, tmp_path_factory, monkeypatch):
    sample = next(iter(sorted(_REAL_ART.glob("*.sq.webp"))), None) if _REAL_ART.is_dir() else None
    if sample is None:
        pytest.skip("no card art in this checkout to use as sample bytes")
    # two directories pytest makes: nothing under tools/web creates one (`test_no_writes.py`)
    images, art = tmp_path, tmp_path_factory.mktemp("art")
    shutil.copyfile(sample, images / f"img_card_vert_{OLD[5:]}.webp")       # the cache has the old card
    shutil.copyfile(sample, art / f"{NEW}.webp")                            # the refresh brought the new one
    shutil.copyfile(sample, art / f"{NEW}.sq.webp")
    monkeypatch.setattr(web_server, "CARD_IMAGES_DIR", images)
    monkeypatch.setattr(web_server.Handler, "art_dir", art, raising=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    httpd = web_server.ThreadingServer((web_server.BIND_HOST, port), web_server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}", sample.read_bytes()
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""


def test_a_card_missing_from_the_game_cache_is_served_from_the_refreshed_art(web):
    base, sample = web
    for family in ("vert", "full", "thumb"):
        status, body = _get(f"{base}/cardart/{family}/{NEW}")
        assert status == 200 and len(body) > 0, family
    assert _get(f"{base}/cardart/vert/{OLD}")[0] == 200                     # the cache still answers first
    # a card in NEITHER directory is still a 404, and so is a family the cache lacks
    assert _get(f"{base}/cardart/vert/card-09999-5-uniq-9999-00")[0] == 404
    assert _get(f"{base}/cardart/thumb/{OLD}")[0] == 404
    # the id is validated before either directory is touched
    assert _get(f"{base}/cardart/vert/..%2F..%2Fsecret")[0] in (400, 404)
