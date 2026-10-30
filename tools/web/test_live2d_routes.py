"""The leaders' Live2D figure routes: `GET /api/live2d/index`, `/live2d/core.js` and
`/live2d/<converted file>` (written by tools/recon/proxy/live2d_build.py into a gitignored
folder). The index must always answer (the page falls back to the card art when it says
nothing is available); the file route must refuse anything outside its own name pattern.
    python -m pytest tools/web/test_live2d_routes.py
"""
from __future__ import annotations

import json
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
def web_base():
    port = _free_port()
    server = web_server.ThreadingServer((web_server.BIND_HOST, port), web_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, r.read(), r.headers.get("Content-Type")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type")


def test_index_always_answers_with_its_shape(web_base):
    status, body, kind = _get(web_base + "/api/live2d/index")
    assert status == 200 and kind.startswith("application/json")
    doc = json.loads(body)
    assert set(doc) >= {"available", "core", "by_card", "models"}
    # every card points at a model the index lists, with a URL under /live2d/
    for card, asset in doc["by_card"].items():
        assert card.startswith("card-") and asset in doc["models"]
        assert doc["models"][asset]["model"].startswith("/live2d/models/")


@pytest.mark.parametrize("path", [
    "/live2d/../server.py", "/live2d/models/../../index.json", "/live2d/index.json",
    "/live2d/models/00022-uniq-0063-00/../../../x.json", "/live2d/motions/evil.js",
    "/live2d/models/00022-uniq-0063-00/model3.exe",
])
def test_paths_outside_the_pattern_are_refused(web_base, path):
    status, _body, _kind = _get(web_base + path)
    assert status in (400, 404)


def test_a_missing_model_is_a_404_not_a_substitute(web_base):
    status, _body, _kind = _get(web_base + "/live2d/models/99999-uniq-9999-00/model3.json")
    assert status == 404


def test_served_model_files_resolve_their_relative_references(web_base):
    doc = json.loads(_get(web_base + "/api/live2d/index")[1])
    if not doc["models"]:
        pytest.skip("no converted Live2D model on disk (run tools/recon/proxy/live2d_build.py)")
    url = next(iter(doc["models"].values()))["model"]
    status, body, kind = _get(web_base + url)
    assert status == 200 and kind == "application/json"
    model3 = json.loads(body)
    base = url.rsplit("/", 1)[0]
    refs = model3["FileReferences"]
    files = [refs["Moc"], *refs["Textures"], refs.get("Physics"), refs.get("Pose")]
    files += [e["File"] for e in refs.get("Expressions") or []]
    files += [m["File"] for group in (refs.get("Motions") or {}).values() for m in group]
    for rel in filter(None, files):
        parts = (base + "/" + rel).split("/")
        resolved = []
        for p in parts:
            if p == "..":
                resolved.pop()
            else:
                resolved.append(p)
        assert _get(web_base + "/".join(resolved))[0] == 200, rel
