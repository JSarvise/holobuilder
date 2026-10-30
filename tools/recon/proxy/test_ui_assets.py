"""ui_assets.json: every picture the pages draw from /chrome/ and /ui/ has a source."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ui_assets  # noqa: E402

REPO = HERE.parents[2]
SPEC = json.loads((HERE / "ui_assets.json").read_text("utf-8"))


def test_spec_shape():
    assert len(SPEC["sprites"]) > 200 and SPEC["copies"]
    for out, sprite in SPEC["sprites"].items():
        assert out.startswith(("card_chrome/", "ui_official/")) and out.endswith(".png"), out
        assert sprite and "/" not in sprite and os.path.splitext(os.path.basename(out))[0] == sprite
    for out, src in SPEC["copies"].items():
        assert out.startswith("ui_official/items/") and src.startswith("item_icons/")


def test_every_static_ui_path_the_pages_name_has_a_source():
    """A literal `/ui/<dir>/<file>` or `/chrome/<dir>/<file>` in the pages must be producible."""
    have = {out.split("/", 1)[1] for out in [*SPEC["sprites"], *SPEC["copies"]]}
    static = REPO / "tools" / "web" / "static"
    wanted = set()
    for f in static.iterdir():
        if f.suffix in (".js", ".html", ".css"):
            text = f.read_text("utf-8", errors="replace")
            text = re.sub(r"/\*.*?\*/|<!--.*?-->", "", text, flags=re.S)   # examples in comments
            wanted |= set(re.findall(r"/(?:ui|chrome)/([a-z_]+/[A-Za-z0-9_.-]+\.(?:png|webp))", text))
    missing = sorted(w for w in wanted if w not in have)
    assert not missing, missing


def test_plan_skips_files_already_there(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_assets, "DUMPS", str(tmp_path))
    spec = {"sprites": {"card_chrome/a/x.png": "x", "card_chrome/a/y.png": "y"}, "copies": {}}
    (tmp_path / "card_chrome" / "a").mkdir(parents=True)
    (tmp_path / "card_chrome" / "a" / "x.png").write_bytes(b"png")
    want, _ = ui_assets.plan(spec, everything=False)
    assert want == {"y": ["card_chrome/a/y.png"]}
    assert set(ui_assets.plan(spec, everything=True)[0]) == {"x", "y"}
