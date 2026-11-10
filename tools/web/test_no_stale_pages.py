"""No stale views after an update: the pages and their scripts and styles are never cached by the
app's window (Cache-Control: no-store), so a new version's page is what the next load shows. The
launcher's own screens are covered by `launcher.clear_webview_cache_after_update`."""
from __future__ import annotations

import urllib.request

import pytest


@pytest.mark.parametrize("path", ["/parkview", "/optimizer", "/pk-app.js", "/pk-chat.js",
                                  "/hb-shell.js", "/hb.css", "/parkview.css"])
def test_pages_scripts_and_styles_are_never_cached(web_base, path):
    with urllib.request.urlopen(f"{web_base}{path}", timeout=30) as r:
        assert r.status == 200
        assert r.headers.get("Cache-Control") == "no-store", path
