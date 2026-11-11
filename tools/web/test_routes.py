"""Every page of the rail, and every ES module and stylesheet it reaches, is served.

If the static route were an allowlist, a module that falls off it would not be a loud error:
an ES module import that 404s aborts the whole module graph and leaves a blank page. So the
test reads each page's own `<script src>` / `<link href>` and walks the `import ... from`
statements of every module it reaches, demanding a 200 for each, rather than restating the
file list a second time.

Fast by construction: it starts the server and fetches static files; no search runs.

    python -m pytest tools/web/test_routes.py
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from hbweb import config

from hbuilder.inputs import load_inputs

STATIC = Path(__file__).resolve().parent / "static"
IMPORT_RE = re.compile(r"""^\s*import\s.*?from\s+["'](/[^"']+)["']""", re.M)
ASSET_RE = re.compile(r'(?:src|href)="(/[a-z0-9-]+\.(?:js|css))"')
PAGES = sorted(p.stem for p in STATIC.glob("*.html"))


def _get(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def test_root_opens_the_optimizer(web_base):
    status, ctype, body = _get(web_base + "/")  # urllib follows the 302
    assert status == 200 and "text/html" in ctype
    assert b'data-page="optimizer"' in body


@pytest.mark.parametrize("page", PAGES)
def test_every_page_and_every_module_it_reaches_is_served(web_base, page):
    status, ctype, body = _get(f"{web_base}/{page}")
    assert status == 200 and "text/html" in ctype, page
    todo = ASSET_RE.findall(body.decode("utf-8"))
    assert todo, f"{page} loads no script -- this test would be vacuous"
    seen: set[str] = set()
    while todo:
        asset = todo.pop()
        if asset in seen:
            continue
        seen.add(asset)
        status, ctype, served = _get(web_base + asset)
        assert status == 200 and served, f"{asset} (reached from /{page}) is not served"
        assert ("javascript" if asset.endswith(".js") else "text/css") in ctype, asset
        if asset.endswith(".js"):
            todo.extend(IMPORT_RE.findall(served.decode("utf-8")))


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/nope.js", 404),
        ("/nope.css", 404),
        ("/deeper/opt-app.js", 404),  # one level only
        ("/server.py", 404),  # the extension gate, not an allowlist accident
        ("/classic", 404),
        ("/index.html", 404),
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

from hbuilder.inputs import apply_collection  # noqa: E402


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
    monkeypatch.setattr(config, "COLLECTION_PATH", written)

    document = {"schema_version": "collection-1", "card_default": "max", "cards": {}}
    status, saved = _post(web_base + "/api/collection", document)
    assert status == 200, saved
    assert saved["saved"] is True
    assert written.is_file(), "POST reported success and wrote nothing"
    assert json.loads(written.read_text(encoding="utf-8")) == document

    cards, _owned, _cfg, _prov = load_inputs(config.CARDS_PATH, config.OWNED_PATH)
    _restated, basis = apply_collection(tuple(cards), str(written))
    assert basis.applied == saved["cards_restated"]
    assert basis.unchanged == saved["cards_unchanged"]


def test_a_malformed_document_is_rejected_and_not_written(web_base, tmp_path, monkeypatch):
    """A document the solver cannot read must not reach disk. The negative
    control for the test above, which would otherwise pass on an endpoint that
    wrote anything it was handed."""
    written = tmp_path / "collection.json"
    monkeypatch.setattr(config, "COLLECTION_PATH", written)

    status, data = _post(web_base + "/api/collection", {"cards": "not an object"})

    assert status == 400
    assert "error" in data
    assert not written.exists(), "a rejected document was written anyway"


def test_a_document_the_solver_refuses_never_replaces_the_saved_one(web_base, tmp_path, monkeypatch):
    """The solver's own loader judges the document before it reaches the disk: a collection it
    refuses (here a `player` that is not an object) would otherwise sit in state/ and stop every
    account search. The saved file stays byte for byte, and no temp file is left beside it."""
    written = tmp_path / "collection.json"
    written.write_text('{"cards": {}}', encoding="utf-8")
    monkeypatch.setattr(config, "COLLECTION_PATH", written)

    status, data = _post(web_base + "/api/collection", {"cards": {}, "player": "not an object"})

    assert status == 400 and "nothing was saved" in data["error"], data
    assert written.read_text(encoding="utf-8") == '{"cards": {}}'
    status, saved = _post(web_base + "/api/collection", {"cards": {}, "player": {"level": 31}})
    assert status == 200 and saved["saved"] is True
    assert json.loads(written.read_text(encoding="utf-8")) == {"cards": {}, "player": {"level": 31}}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["collection.json", "collection.previous.json"]
    assert (tmp_path / "collection.previous.json").read_text(encoding="utf-8") == '{"cards": {}}'


def test_a_save_never_empties_a_collection_that_holds_cards(web_base, tmp_path, monkeypatch):
    written = tmp_path / "collection.json"
    saved = '{"cards": {"card-00001-3-nrml-0000-00": {"level": 10}}}'
    written.write_text(saved, encoding="utf-8")
    monkeypatch.setattr(config, "COLLECTION_PATH", written)
    for document in ({}, {"cards": {}}, {"cards": {}, "player": {"level": 31}}):
        status, data = _post(web_base + "/api/collection", document)
        assert status == 400 and "no cards" in data["error"], data
    assert written.read_text(encoding="utf-8") == saved
