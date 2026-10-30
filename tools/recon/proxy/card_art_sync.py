"""Art for the cards `data/cards.json` has and the web/explorer art dir lacks.

For every card id without `tools/explorer/cards_explorer_art/<id>.webp` (portrait,
the game's roster image `img_card_vert_*`) or `<id>.sq.webp` (square,
`img_card_thumb_*`), fetch the two bundles from the Octo CDN by name
(`octo_fetch.fetch`: production catalogue, md5-verified, 1 req/s, cached under
dumps/octo_fetched), decrypt the 256-byte name-derived header (`octo_crypto`),
take the largest Texture2D and write the WEBP the page serves — the same
encode `build_explorer.export_art` uses (quality 80). The native PNG is kept
under dumps/card_art/<family>/<WxH>/ so a later full `export_art` finds it.

Why not `build_explorer.export_art`: it PURGES the art dir of anything it cannot
source from dumps/card_art natives, and on a machine whose dumps/ were lost that
is 340 committed files. This adds and never removes.

Why not `card_images.py --from-cache`: that reads the game's own on-disk Octo
cache, which only holds what the game has already shown the player. A card the
game just shipped may not be there yet; the CDN has it.

    proxyenv/Scripts/python card_art_sync.py            # fetch + render what is missing
    proxyenv/Scripts/python card_art_sync.py --status   # list what is missing, no network
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parents[2]
DUMPS = HERE / "dumps"
CARDS = REPO / "data" / "cards.json"
ART_DIR = REPO / "tools" / "explorer" / "cards_explorer_art"
FETCHED = DUMPS / "octo_fetched"
ASSETS = DUMPS / "card_assets"
NATIVE = DUMPS / "card_art"
QUALITY = 80

# (asset family, output suffix): the portrait and the square the page serves.
FAMILIES = (("img_card_vert", ".webp"), ("img_card_thumb", ".sq.webp"))


def missing_ids() -> list[str]:
    raw = json.loads(CARDS.read_text(encoding="utf-8"))
    cards = raw["cards"] if isinstance(raw, dict) else raw
    have = set(os.listdir(ART_DIR)) if ART_DIR.is_dir() else set()
    return sorted(c["id"] for c in cards
                  if any(c["id"] + suffix not in have for _fam, suffix in FAMILIES))


def asset_names(cid: str) -> list[str]:
    tail = cid[len("card-"):] if cid.startswith("card-") else cid
    return [f"{fam}_{tail}" for fam, _s in FAMILIES]


def _texture(name: str):
    import warnings
    warnings.filterwarnings("ignore")
    import UnityPy
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"
    import octo_crypto
    src = FETCHED / (name + ".bundle")
    if not src.exists():
        return None, "bundle not fetched"
    raw = src.read_bytes()
    env = UnityPy.load(io.BytesIO(octo_crypto.decrypt_bundle(raw, name)))
    best = None
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        img = obj.read().image
        if img and (best is None or img.width * img.height > best.width * best.height):
            best = img
    return best, None if best is not None else "no Texture2D"


def sync(ids: list[str]) -> int:
    import octo_fetch
    names = [n for cid in ids for n in asset_names(cid)]
    todo = [n for n in names if not (FETCHED / (n + ".bundle")).exists()]
    if todo:
        print(f"fetching {len(todo)} bundle(s) for {len(ids)} card(s) at 1 req/s")
        octo_fetch.fetch(todo)
    ART_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    written, failed = 0, []
    for cid in ids:
        for (fam, suffix), name in zip(FAMILIES, asset_names(cid)):
            dst = ART_DIR / (cid + suffix)
            if dst.exists():
                continue
            img, why = _texture(name)
            if img is None:
                failed.append((name, why))
                continue
            # keep the native beside card_images' output, for a later export_art
            native_dir = NATIVE / fam / f"{img.width}x{img.height}"
            native_dir.mkdir(parents=True, exist_ok=True)
            img.save(native_dir / (name + ".png"))
            src = FETCHED / (name + ".bundle")
            if not (ASSETS / src.name).exists():
                shutil.copyfile(src, ASSETS / src.name)
            img.convert("RGBA").save(dst, "WEBP", quality=QUALITY)
            written += 1
    print(f"{written} image(s) written -> {ART_DIR}")
    for name, why in failed:
        print(f"  FAILED {name}: {why}")
    return 1 if failed else 0


def main() -> int:
    ids = missing_ids()
    if "--status" in sys.argv[1:] or not ids:
        print(f"{len(ids)} card(s) without art in {ART_DIR}"
              + (": " + ", ".join(ids) if ids else " -- nothing to do"))
        return 0
    print(f"{len(ids)} card(s) without art: {', '.join(ids)}")
    return sync(ids)


if __name__ == "__main__":
    raise SystemExit(main())
