"""The 3D chibi routes for the Optimizer's progress runner: `GET /api/chibi/index` and
`/chibi/models/<SdCostume id>.glb` (written by tools/recon/proxy/chibi_build.py into a
gitignored folder). The index must always answer (the page shows a plain bar when nothing is
available); the file route must refuse anything outside its own name pattern.
    python -m pytest tools/web/test_chibi_routes.py
"""
from __future__ import annotations

import json
import socket
import struct
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
    status, body, kind = _get(web_base + "/api/chibi/index")
    assert status == 200 and kind.startswith("application/json")
    doc = json.loads(body)
    assert set(doc) >= {"available", "outfits"}
    for sd, o in doc["outfits"].items():
        assert sd.startswith("sdcos-") and o["model"] == f"/chibi/models/{sd}.glb"
        assert o["typ"] in (0, 1, 2) and o["chr"].startswith("chr-")


@pytest.mark.parametrize("path", [
    "/chibi/../server.py", "/chibi/index.json", "/chibi/models/../index.json",
    "/chibi/models/sdcos-00001-cmmn-0000-00.glb.exe", "/chibi/models/evil.js",
])
def test_paths_outside_the_pattern_are_refused(web_base, path):
    status, _body, _kind = _get(web_base + path)
    assert status in (400, 404)


def test_a_missing_model_is_a_404_not_a_substitute(web_base):
    status, _body, _kind = _get(web_base + "/chibi/models/sdcos-99999-uniq-9999-00.glb")
    assert status == 404


def test_a_listed_model_is_a_binary_gltf(web_base):
    doc = json.loads(_get(web_base + "/api/chibi/index")[1])
    if not doc["outfits"]:
        pytest.skip("no converted chibi on disk (run tools/recon/proxy/chibi_build.py)")
    status, body, kind = _get(web_base + next(iter(doc["outfits"].values()))["model"])
    assert status == 200 and kind == "model/gltf-binary"
    magic, version, length = struct.unpack_from("<III", body)
    assert magic == 0x46546C67 and version == 2 and length == len(body)
