"""Banner art for the gacha tracker: Octo catalogue -> data/gacha/art/<gacha_id>/*.webp.

The decrypted production catalogue names them like `img_banner_full_gacha-pickup-260807`
(note: no `-normal`), `img_gacha_logo_pickup-normal-260807`, `img_gacha_group_icon_pickup-normal-260807`,
each also as `_lang-{eng,chs,cht,kor,ind}`. Past and upcoming banners are both listed; how long
they stay is not known, hence the copy into the repo the first time a banner is seen.

Fetch = octo_fetch.fetch (md5-checked, 1 req/s, cached in dumps/octo_fetched). Extract = UnityPy on
octo_crypto.decrypt_bundle. Both are injectable so the logic is testable offline.

NEW banners are saved as WEBP, downscaled to <= `MAX_W` px wide: 1280 px WEBP q85 is ~242 KB
against ~1.5 MB for the native PNG, and the gallery never paints one wider than 1280 anyway.
The `.png` masters already committed STAY -- `missing()` accepts either extension, and the web
layer serves both -- so nothing that exists is rewritten.

NOTHING here is fatal. A banner whose bundle will not fetch (a fresh clone has no decrypted
Octo catalogue, and `octo_fetch.catalog()` raises SystemExit for it) is REPORTED and skipped;
the sync's own summary is printed either way.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

KINDS = ("banner", "logo", "icon")
EXTS = (".png", ".webp")      # .png = already-committed masters; .webp = everything new
MAX_W = 1280                  # the widest the gallery or the popup ever paints one


def asset_names(gacha_id: str) -> dict[str, list[str]]:
    short = gacha_id[len("gacha-"):] if gacha_id.startswith("gacha-") else gacha_id
    banner_ids = [gacha_id]
    # The catalogue drops the banner's KIND segment from the banner_full name --
    # `gacha-pickup-normal-260807` -> `img_banner_full_gacha-pickup-260807`, and the same for
    # `-select-` (`gacha-pickup-select-260817` -> `img_banner_full_gacha-pickup-260817`).
    # The logo/icon names keep the segment, so only the banner list gets the fallback.
    for seg in ("-normal-", "-select-"):
        if seg in gacha_id:
            banner_ids.append(gacha_id.replace(seg, "-"))
    banners = [f"img_banner_full_{b}_lang-eng" for b in banner_ids] + [f"img_banner_full_{b}" for b in banner_ids]
    return {"banner": banners,
            "logo": [f"img_gacha_logo_{short}_lang-eng", f"img_gacha_logo_{short}"],
            "icon": [f"img_gacha_group_icon_{short}_lang-eng", f"img_gacha_group_icon_{short}"]}


def missing(gacha_ids: list[str], art_dir: Path) -> list[str]:
    """Either extension counts as present: the committed `.png` masters are not re-fetched
    just because new banners are saved as `.webp`."""
    return [g for g in gacha_ids
            if not any((art_dir / g / f"banner{e}").is_file() for e in EXTS)]


def _default_fetch(names: list[str]) -> None:
    import octo_fetch  # noqa: PLC0415
    octo_fetch.fetch(names)


def _default_extract(bundle_path: Path, name: str):
    if not bundle_path.is_file():
        return None
    import UnityPy  # noqa: PLC0415
    import octo_crypto  # noqa: PLC0415
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"
    env = UnityPy.load(io.BytesIO(octo_crypto.decrypt_bundle(bundle_path.read_bytes(), name)))
    best = None
    for obj in env.objects:
        if obj.type.name not in ("Sprite", "Texture2D"):
            continue
        try:
            img = obj.read().image
        except Exception:  # noqa: BLE001 -- one bad object must not kill the run
            continue
        if img is not None and (best is None or img.size[0] * img.size[1] > best.size[0] * best.size[1]):
            best = img
    return best


def _save_webp(img, dst: Path) -> None:
    """<= MAX_W wide, WEBP q85. A resize is never an upscale."""
    from PIL import Image  # noqa: PLC0415 -- proxyenv ships Pillow
    if img.width > MAX_W:
        height = max(1, round(img.height * MAX_W / img.width))
        img = img.resize((MAX_W, height), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGBA").save(dst, format="WEBP", quality=85, method=4)


def ensure_art(gacha_ids: list[str], art_dir: Path, fetch=None, extract=None) -> dict[str, list[str]]:
    fetch = fetch or _default_fetch
    extract = extract or _default_extract
    from_dumps = HERE / "dumps" / "octo_fetched"
    saved: dict[str, list[str]] = {}
    for gid in missing(gacha_ids, art_dir):
        names = asset_names(gid)
        try:
            fetch([n for kind in KINDS for n in names[kind]])
        except (Exception, SystemExit) as exc:  # noqa: BLE001 -- reported, never fatal
            # `octo_fetch.catalog()` raises SystemExit when the decrypted Octo catalogue is
            # absent, which is the NORMAL state of a fresh clone. Extraction may still
            # succeed from an already-cached bundle, so this only skips the download.
            print(f"art {gid}: fetch failed: {type(exc).__name__}: {exc}")
        got: list[str] = []
        for kind in KINDS:
            for n in names[kind]:
                try:
                    img = extract(from_dumps / f"{n}.bundle", n)
                except Exception as exc:  # noqa: BLE001 -- reported, never fatal
                    print(f"art {gid}: {n}: {type(exc).__name__}: {exc}")
                    img = None
                if img is None:
                    continue
                # a NEW file per banner; `missing()` skipped any kind already on disk
                _save_webp(img, art_dir / gid / f"{kind}.webp")
                got.append(kind)
                break
        saved[gid] = got
        print(f"art {gid}: {got or 'NONE FOUND'}")
    return saved
