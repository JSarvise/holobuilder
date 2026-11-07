"""`GET /ui/<category>/<sprite>` serves the game's official UI sprites
(`dumps/ui_official/`) to the Night
Deck shell and components (`docs/redesign/IDENTITY.md`).

Pinned: a real sprite is a PNG, a missing one is a first-class 404 (never a
placeholder), and nothing outside the folder is reachable: no traversal, no
second level, no non-image (the `views/` text dumps)."""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from hbweb import config


def _get(url: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, b"", ""


@pytest.mark.skipif(not (config.UI_OFFICIAL_DIR / "grade" / "ui_999_txt_score_s.png").is_file(),
                    reason="dumps/ui_official not extracted on this machine")
def test_a_real_sprite_is_served_as_png(web_base):
    code, body, ctype = _get(f"{web_base}/ui/grade/ui_999_txt_score_s.png")
    assert code == 200 and ctype == "image/png" and body[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_missing_sprite_is_404_not_a_placeholder(web_base):
    assert _get(f"{web_base}/ui/grade/ui_999_txt_score_z.png")[0] == 404


@pytest.mark.parametrize("name", [
    "../card_chrome/x.png",          # traversal
    "grade/../../cards.png",         # traversal inside the name
    "a/b/c.png",                     # second level
    "views/sprite_usage.json",       # not an image
    "manifest.json",                 # no category
    "Grade/ui_999_ico_plus.png",     # uppercase category
])
def test_names_outside_the_contract_are_refused(web_base, name):
    assert _get(f"{web_base}/ui/{name}")[0] in (400, 404)
