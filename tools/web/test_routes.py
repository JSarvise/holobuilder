"""Every ES module the page imports must actually be served.

If the static route were an allowlist, a module that falls off it would not
be a loud error -- it would be a **blank page** with one line in a console
nobody has open, because an ES module import that 404s aborts the whole
module graph.
So the test walks `app.js`'s own import statements and demands a 200 for
each, rather than restating the allowlist a second time (which would pass
for exactly as long as someone remembered to edit both).

Fast by construction: it starts the server and fetches static files. No
solve runs here -- the solve is ~10 s and is covered elsewhere.

`pyproject.toml`'s `testpaths` is `["tests", "tools/web", "tools/explorer"]`,
so this runs in the root suite; `test_no_writes.py`'s AST checks are
parametrised over the `.py` files in this directory and scan this file too.

    python -m pytest tools/web/test_routes.py
"""

from __future__ import annotations

import re
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import server as web_server

STATIC = Path(__file__).resolve().parent / "static"
IMPORT_RE = re.compile(r"""^\s*import\s.*?from\s+["'](/[^"']+)["']""", re.M)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def web_base():
    port = _free_port()
    server = web_server.ThreadingServer(
        (web_server.BIND_HOST, port), web_server.Handler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def test_root_opens_the_redesign_and_the_previous_ui_stays_at_classic(web_base):
    """`/` redirects to the Optimizer page; `/classic` is the previous page."""
    status, ctype, body = _get(web_base + "/")          # urllib follows the 302
    assert status == 200 and "text/html" in ctype
    assert b'data-page="optimizer"' in body


def test_page_and_its_modules_are_served(web_base):
    """`/classic` (the previous UI), plus every module `app.js` imports, by reading app.js."""
    status, ctype, body = _get(web_base + "/classic")
    assert status == 200 and "text/html" in ctype
    assert b'src="/app.js"' in body

    imports = IMPORT_RE.findall((STATIC / "app.js").read_text(encoding="utf-8"))
    assert imports, "app.js imports nothing -- this test would be vacuous"
    assert "/ranking.js" in imports
    for module in imports:
        status, ctype, body = _get(web_base + module)
        assert status == 200, f"{module} is imported by app.js but 404s"
        assert "javascript" in ctype
        assert body, f"{module} served empty"


def test_stylesheet_is_served(web_base):
    status, ctype, _ = _get(web_base + "/style.css")
    assert status == 200 and "text/css" in ctype


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/nope.js", 404),
        ("/nope.css", 404),
        ("/deeper/app.js", 404),  # one level only
        ("/server.py", 404),  # the extension gate, not an allowlist accident
        ("/art/notacard.webp", 400),  # _ART_NAME_RE
        ("/art/../../../data/cards.json", 400),
    ],
)
def test_routes_that_must_not_serve(web_base, path, expected):
    status, _, _ = _get(web_base + path)
    assert status == expected



# ---------------------------------------------------------------------------
# The collection editor's two endpoints, and the acceptance that matters:
# what the page saves must produce the same score the CLI produces from the
# same document. Without that, the UI would only ever be checked against
# itself.
# ---------------------------------------------------------------------------

import json  # noqa: E402
import dataclasses  # noqa: E402

from hbuilder.cli import apply_collection  # noqa: E402


def _post(url: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_collection_endpoint_serves_bounds_from_the_domain_not_its_own(web_base):
    """`GET /api/collection` must hand the page the game's real ceilings.

    A UI that clamped to its own idea of the cap would let the user type a
    level the solver then silently changes. Every bound here comes from
    `domain.collection`, so this asserts the shape reaches the client rather
    than re-deriving it."""
    status, ctype, body = _get(web_base + "/api/collection")
    assert status == 200 and "json" in ctype
    data = json.loads(body)

    assert data["cards"], "no cards served"
    assert data["fully_scorable_bloom"] == 4
    assert data["skill_bloom_step"] == {"ACTIVE": 1, "SPECIAL": 3, "PASSIVE": 4}
    sample = data["cards"][0]
    assert {"id", "level", "bloom", "level_cap", "max_bloom", "overstated"} <= set(sample)


def test_what_the_page_saves_scores_the_same_as_the_cli(web_base, tmp_path, monkeypatch):
    """The acceptance. `POST /api/collection` writes the document, and the
    CLI's own `apply_collection` must make exactly the same cards out of the
    file on disk as the server reported making.

    Both sides call the same function on purpose -- that is the design, not a
    weakness of the test. What it proves is that the bytes the page WRITES are
    bytes the solver can read and agrees with: a document that round-trips
    through JSON, the HTTP body and the file and then produces a different
    restatement is exactly the failure this endpoint could otherwise ship."""
    written = tmp_path / "collection.json"
    monkeypatch.setattr(web_server, "COLLECTION_PATH", written)

    document = {"schema_version": "collection-1", "card_default": "max", "cards": {}}
    status, saved = _post(web_base + "/api/collection", document)
    assert status == 200, saved
    assert saved["saved"] is True
    assert written.is_file(), "POST reported success and wrote nothing"
    assert json.loads(written.read_text(encoding="utf-8")) == document

    cards, _owned, _cfg, _prov = web_server.load_inputs(
        web_server.CARDS_PATH, web_server.OWNED_PATH, None
    )
    _restated, basis = apply_collection(tuple(cards), str(written))
    assert basis.applied == saved["cards_restated"]
    assert basis.unchanged == saved["cards_unchanged"]


def test_a_malformed_document_is_rejected_and_not_written(web_base, tmp_path, monkeypatch):
    """A document the solver cannot read must not reach disk. The negative
    control for the test above, which would otherwise pass on an endpoint that
    wrote anything it was handed."""
    written = tmp_path / "collection.json"
    monkeypatch.setattr(web_server, "COLLECTION_PATH", written)

    status, data = _post(web_base + "/api/collection", {"cards": "not an object"})

    assert status == 400
    assert "error" in data
    assert not written.exists(), "a rejected document was written anyway"
