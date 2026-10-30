from __future__ import annotations
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gacha_art as ga  # noqa: E402


def test_asset_names_cover_measured_variants():
    n = ga.asset_names("gacha-pickup-normal-260807")
    assert "img_banner_full_gacha-pickup-260807_lang-eng" in n["banner"]
    assert "img_banner_full_gacha-pickup-normal-260807" in n["banner"]
    assert n["logo"][0] == "img_gacha_logo_pickup-normal-260807_lang-eng"
    assert n["icon"][-1] == "img_gacha_group_icon_pickup-normal-260807"

    # `-select-` is dropped exactly like `-normal-`, and the `_lang-eng` variants come first.
    s = ga.asset_names("gacha-pickup-select-260817")
    assert s["banner"] == [
        "img_banner_full_gacha-pickup-select-260817_lang-eng",
        "img_banner_full_gacha-pickup-260817_lang-eng",
        "img_banner_full_gacha-pickup-select-260817",
        "img_banner_full_gacha-pickup-260817",
    ]
    assert s["logo"][0] == "img_gacha_logo_pickup-select-260817_lang-eng"


def test_missing_accepts_either_extension(tmp_path):
    """A committed `.png` master and a new `.webp` both count as present -- the switch to
    WEBP must not re-fetch every banner already on disk."""
    (tmp_path / "gacha-a").mkdir()
    (tmp_path / "gacha-a" / "banner.png").write_bytes(b"x")
    (tmp_path / "gacha-c").mkdir()
    (tmp_path / "gacha-c" / "banner.webp").write_bytes(b"x")
    assert ga.missing(["gacha-a", "gacha-b", "gacha-c"], tmp_path) == ["gacha-b"]


def test_ensure_art_uses_first_available_name_and_writes_webps(tmp_path):
    fetched = []
    from PIL import Image
    def fake_fetch(names): fetched.extend(names)
    def fake_extract(bundle_path, name):
        return Image.new("RGB", (4, 2)) if name.endswith("_lang-eng") else None
    out = ga.ensure_art(["gacha-pickup-normal-260807"], tmp_path, fetch=fake_fetch, extract=fake_extract)
    assert out == {"gacha-pickup-normal-260807": ["banner", "logo", "icon"]}
    d = tmp_path / "gacha-pickup-normal-260807"
    assert (d / "banner.webp").is_file() and (d / "logo.webp").is_file() and (d / "icon.webp").is_file()
    assert not (d / "banner.png").exists()
    with Image.open(d / "banner.webp") as im:
        assert im.format == "WEBP"
    assert fetched  # it asked the CDN for the names it tried


def test_ensure_art_downscales_to_the_max_width(tmp_path):
    """1280 px WEBP q85 is ~242 KB against ~1.5 MB for the native PNG."""
    from PIL import Image
    def fake_extract(bundle_path, name):
        return Image.new("RGB", (2833, 1417))
    ga.ensure_art(["gacha-a"], tmp_path, fetch=lambda n: None, extract=fake_extract)
    with Image.open(tmp_path / "gacha-a" / "banner.webp") as im:
        assert im.width == ga.MAX_W and im.height == round(1417 * ga.MAX_W / 2833)


def test_ensure_art_survives_extract_exception(tmp_path):
    from PIL import Image
    def fake_fetch(names): pass
    def fake_extract(bundle_path, name):
        if name.endswith("_lang-eng"):
            raise RuntimeError("corrupt bundle")
        return Image.new("RGB", (4, 2))
    out = ga.ensure_art(["gacha-pickup-normal-260807"], tmp_path, fetch=fake_fetch, extract=fake_extract)
    assert out == {"gacha-pickup-normal-260807": ["banner", "logo", "icon"]}
    d = tmp_path / "gacha-pickup-normal-260807"
    assert (d / "banner.webp").is_file() and (d / "logo.webp").is_file() and (d / "icon.webp").is_file()


def test_ensure_art_survives_fetch_exception(tmp_path):
    """A fresh clone has no decrypted Octo catalogue and `octo_fetch.catalog()` raises
    SystemExit for it. That must skip the DOWNLOAD only: an already-cached bundle still
    extracts, and the art step never takes the sync down with it."""
    from PIL import Image
    def fake_fetch(names): raise SystemExit("no catalogue")
    def fake_extract(bundle_path, name):
        return Image.new("RGB", (4, 2)) if name.endswith("_lang-eng") else None
    out = ga.ensure_art(["gacha-pickup-normal-260807"], tmp_path, fetch=fake_fetch, extract=fake_extract)
    assert out == {"gacha-pickup-normal-260807": ["banner", "logo", "icon"]}
    d = tmp_path / "gacha-pickup-normal-260807"
    assert (d / "banner.webp").is_file() and (d / "logo.webp").is_file() and (d / "icon.webp").is_file()
