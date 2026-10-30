"""The web's official UI pictures, straight from the game install: card chrome and UI sprites.

    python ui_assets.py            # write what is missing
    python ui_assets.py --all      # write every file again

`ui_assets.json` (next to this file) maps every output file to the game sprite it is:

  * "sprites": dumps/card_chrome/... and dumps/ui_official/... -> a Sprite name in the game's
    Addressables bundles (<install>/.../StreamingAssets/aa, plain UnityFS). UnityPy crops each
    Sprite out of its atlas, so one PNG per element.
  * "copies":  dumps/ui_official/items/*.webp -> the same item thumbnail item_icons.py writes.

The pages draw these (`/chrome/...` on the card face, `/ui/...` for navigation, grades, board
glyphs, skill plates); a missing file is a 404 and the page draws without it.

Only the bundles that hold a wanted sprite are decoded: the first pass reads object names only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DUMPS = os.path.join(HERE, "dumps")
SPEC = os.path.join(HERE, "ui_assets.json")


def plan(spec: dict, everything: bool) -> tuple[dict[str, list[str]], dict[str, str]]:
    """({sprite name: [output paths]}, {output: source}) still to write."""
    want: dict[str, list[str]] = {}
    for out, sprite in spec["sprites"].items():
        if everything or not os.path.isfile(os.path.join(DUMPS, out)):
            want.setdefault(sprite, []).append(out)
    copies = {out: src for out, src in spec["copies"].items()
              if everything or not os.path.isfile(os.path.join(DUMPS, out))}
    return want, copies


def extract(want: dict[str, list[str]]) -> set[str]:
    """Write every wanted sprite; return the names found."""
    warnings.filterwarnings("ignore")
    import UnityPy  # noqa: PLC0415
    import game_paths  # noqa: PLC0415
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"
    aa = game_paths.aa_dir()
    found: set[str] = set()
    for fn in sorted(os.listdir(aa)):
        if not fn.endswith(".bundle") or len(found) == len(want):
            continue
        try:
            env = UnityPy.load(os.path.join(aa, fn))
            hits = [o for o in env.objects if o.type.name == "Sprite" and o.peek_name() in want
                    and o.peek_name() not in found]
        except Exception:  # noqa: BLE001 -- one unreadable bundle never stops the rest
            continue
        for obj in hits:
            name = obj.peek_name()
            try:
                image = obj.read().image
            except Exception as exc:  # noqa: BLE001
                print(f"  !! {name}: {exc}")
                continue
            for out in want[name]:
                path = os.path.join(DUMPS, out)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                image.save(path)
            found.add(name)
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true", help="write every file again")
    args = ap.parse_args(argv)
    spec = json.load(open(SPEC, encoding="utf-8"))
    want, copies = plan(spec, args.all)
    found = extract(want) if want else set()
    missing = sorted(set(want) - found)
    copied = 0
    for out, src in copies.items():
        source = os.path.join(DUMPS, src)
        if os.path.isfile(source):
            os.makedirs(os.path.dirname(os.path.join(DUMPS, out)), exist_ok=True)
            shutil.copyfile(source, os.path.join(DUMPS, out))
            copied += 1
    total = sum(len(want[n]) for n in found)
    print(f"ui assets: {total} sprite file(s) written, {copied} item thumb(s) copied, "
          f"{len(spec['sprites']) + len(spec['copies']) - total - copied} already there")
    for name in missing:
        print(f"  MISSING sprite {name} (not in this game build's bundles)")
    if len(copies) > copied:
        print(f"  {len(copies) - copied} item thumb(s) wait for item_icons.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
