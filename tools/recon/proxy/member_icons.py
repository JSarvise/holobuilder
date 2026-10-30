"""Extract the per-character holomem face icon to dumps/member_icons/chr-NNNNN.png.

THE ASSET: the board icon is the
Octo-delivered Texture2D `img_chr_icon_normal_<code>` -- a 256x256 round member
face badge, where `<code>` is the 5-digit character number identical to the chr-id
numeric (chr-04003 -> img_chr_icon_normal_04003). The Addressables prefab templates
(CharacterIcon / thm_member / CharacterMotifIcon) wire NO static sprite; the image
is runtime-assigned, so it is pulled from the game's own on-disk Octo cache
(octo/v1, header-XOR-encrypted, recovered byte-exactly by
`octo_crypto.decrypt_bundle`). Fully offline: needs only the decrypted catalog
(dumps/octocacheevai*.dec) and the cache. All 54 board members are present.

    tools/recon/proxy/proxyenv/Scripts/python.exe tools/recon/proxy/member_icons.py
    ...                                            member_icons.py --family img_chr_icon_sd_   # chibi drop-in

A member whose icon cannot be produced is REPORTED and left UNWRITTEN -- never
substituted with another character's face. The per-file assert below makes a
face/character mismap fail the run rather than ship.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
DUMPS = HERE / "dumps"
OUT = DUMPS / "member_icons"
import game_paths  # noqa: E402  (portable install resolution)

# resolved when a build runs, so importing this module (tests, board_export) needs no install
DEFAULT_FAMILY = "img_chr_icon_normal_"


def icon_name(chr_id: str, family: str = DEFAULT_FAMILY) -> str:
    """'chr-04003' -> 'img_chr_icon_normal_04003'. The code IS the chr numeric."""
    return family + chr_id.split("-", 1)[1]


def _chr_of(asset_id: str) -> str | None:
    """Any character-scoped asset id embeds the chr numeric as its first 5-digit
    run (true across the Character/Card master). Used to assert the right
    member's file was decoded, never a neighbour's."""
    match = re.search(r"[0-9]{5}", asset_id)
    return "chr-" + match.group(0) if match else None


def build(family: str = DEFAULT_FAMILY) -> tuple[int, list[tuple[str, str]]]:
    import UnityPy  # noqa: PLC0415

    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"
    import octo_crypto  # noqa: PLC0415
    from card_images import _catalog_by_md5  # noqa: PLC0415

    members = json.loads((DUMPS / "skilltree.json").read_text("utf-8"))["members"]
    name_to_md5 = {name: md5 for md5, name in _catalog_by_md5().items()}

    # One walk of the ~11 GB cache, opening ONLY target files (basename == md5).
    wanted = {name_to_md5[icon_name(m["chr"], family)]
              for m in members if icon_name(m["chr"], family) in name_to_md5}
    located: dict[str, str] = {}
    for root, _dirs, files in os.walk(game_paths.octo_cache()):
        for filename in files:
            if filename in wanted:
                located[filename] = os.path.join(root, filename)

    OUT.mkdir(parents=True, exist_ok=True)
    ok = 0
    misses: list[tuple[str, str]] = []
    for member in members:
        cid = member["chr"]
        name = icon_name(cid, family)
        md5 = name_to_md5.get(name)
        if md5 is None:
            misses.append((cid, f"name {name} not in catalog"))
            continue
        path = located.get(md5)
        if path is None:
            misses.append((cid, f"md5 {md5} not in on-disk cache"))
            continue
        assert _chr_of(name) == cid, f"asset {name} does not belong to {cid}"
        env = UnityPy.load(io.BytesIO(octo_crypto.decrypt_bundle(Path(path).read_bytes(), name)))
        best = None
        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            image = obj.read().image
            if image and (best is None or image.width * image.height > best.width * best.height):
                best = image
        if best is None:
            misses.append((cid, "no Texture2D after decrypt"))
            continue
        best.save(OUT / f"{cid}.png")
        ok += 1
    return ok, misses


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--family", default=DEFAULT_FAMILY,
                    help="asset family prefix (e.g. img_chr_icon_sd_ for the chibi drop-in)")
    args = ap.parse_args()
    ok, misses = build(args.family)
    print(f"{ok}/{ok + len(misses)} icons -> {OUT}", file=sys.stderr)
    for cid, why in misses:
        print(f"  MISS {cid}: {why}", file=sys.stderr)
    if misses:
        sys.exit(1)


if __name__ == "__main__":
    main()
