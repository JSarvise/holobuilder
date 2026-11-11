"""The local server answers its own pages only (hbweb/session.py): a request that names another
host is refused, and so is every POST that does not come from one of its pages (another origin,
a cross-site fetch, no session key). Requests here are sent by hand (http.client), with exactly
the headers a hostile page's browser would send."""

from __future__ import annotations

import http.client
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from hbweb import config, router, session

STATIC = Path(config.STATIC_DIR)


def _send(
    base: str, method: str, path: str, headers: dict[str, str], body: bytes = b"{}"
) -> tuple[int, bytes]:
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=30)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in {"Host": parts.netloc, **headers}.items():
            if value is not None:
                conn.putheader(name, value)
        data = body if method == "POST" else b""
        conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _own(base: str) -> dict[str, str]:
    """The headers of this server's own page posting to it."""
    return {"Origin": base, "Sec-Fetch-Site": "same-origin", session.HEADER: session.TOKEN}


def _post_paths() -> list[str]:
    return sorted(router.POST_ROUTES) + [prefix + "x" for prefix, _route in router.POST_PREFIXES]


def test_every_post_route_refuses_a_request_without_the_session_key(web_base):
    for path in _post_paths():
        status, body = _send(web_base, "POST", path, {"Content-Type": "application/json"})
        assert status == 403, (path, status)
        assert "session key" in json.loads(body)["error"]


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},  # another site's page (a form, a fetch)
        {"Origin": "null"},  # a sandboxed or file:// page
        {"Origin": "http://127.0.0.1:1"},  # another local server's page
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},  # another port of localhost is the same site
    ],
)
def test_a_post_from_another_origin_changes_nothing_even_with_the_key(
    web_base, headers, tmp_path, monkeypatch
):
    written = tmp_path / "collection.json"
    monkeypatch.setattr(config, "COLLECTION_PATH", written)
    status, body = _send(
        web_base, "POST", "/api/collection", {**_own(web_base), **headers}, json.dumps({"cards": {}}).encode()
    )
    assert status == 403, body
    assert not written.exists()


def test_the_servers_own_page_is_answered(web_base, tmp_path, monkeypatch):
    status, body = _send(web_base, "POST", "/cancel/no-such-job", _own(web_base))
    assert status == 404 and json.loads(body) == {"error": "unknown job"}  # reached the route
    host = urlsplit(web_base).port
    status, _ = _send(
        web_base,
        "POST",
        "/cancel/no-such-job",
        {**_own(web_base), "Host": f"localhost:{host}", "Origin": f"http://localhost:{host}"},
    )
    assert status == 404


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:{port}", "127.0.0.1.nip.io:{port}", "127.0.0.1:1", None]
)
def test_a_request_naming_another_host_is_refused_even_a_read(web_base, host):
    """DNS rebinding: a hostile name pointed at 127.0.0.1 reaches this server, but its requests
    still name that host, and none is answered, so no page of it reads this server's data."""
    port = urlsplit(web_base).port
    headers = {"Host": host.format(port=port) if host else None}
    for method, path in (("GET", "/api/collection"), ("GET", "/optimizer"), ("POST", "/api/collection")):
        status, _ = _send(web_base, method, path, {**(_own(web_base) if method == "POST" else {}), **headers})
        assert status == 403, (host, method, path)


def test_every_page_carries_the_key_before_any_script(web_base):
    status, _ = _send(web_base, "GET", "/hb-session.js", {})
    assert status == 200
    for page in sorted(STATIC.glob("*.html")):
        status, body = _send(web_base, "GET", "/" + page.stem, {})
        assert status == 200, page.name
        text = body.decode("utf-8")
        assert f'<meta name="hb-session" content="{session.TOKEN}">' in text, page.name
        first_script = re.search(r"<script[^>]*>", text)
        assert first_script and first_script.group(0) == '<script src="/hb-session.js">', page.name


def test_every_cdn_module_is_held_to_its_hash():
    """Each module the pages load from a CDN (the import map's own, the ones hb-live2d.js and
    hb-runner.js import by URL, and what those import in turn) has its sha384 in the import
    map's `integrity`, so a changed file on the CDN is refused rather than run beside the
    pages' session key."""
    page = (STATIC / "optimizer.html").read_text("utf-8")
    importmap = json.loads(re.search(r'<script type="importmap">(.*?)</script>', page, re.S).group(1))
    held = importmap["integrity"]
    urls = set(importmap["imports"].values())
    for script in STATIC.glob("*.js"):
        urls |= set(re.findall(r"https://cdn\.jsdelivr\.net/npm/[^\"'\s]+\.m?js", script.read_text("utf-8")))
    # imported by URL from inside GLTFLoader.js ('../utils/BufferGeometryUtils.js')
    urls.add("https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/utils/BufferGeometryUtils.js")
    assert urls and set(held) == urls
    assert all(re.fullmatch(r"sha384-[A-Za-z0-9+/]{64}", v) for v in held.values())
