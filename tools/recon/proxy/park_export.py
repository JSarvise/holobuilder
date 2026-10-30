# Export the park's RAW map images and every named spot's pixel position -- ONCE, offline.
#
# park_map.py burns the day's markers into PNGs. That shape is wrong for the web page: the
# markers change every day and on every pickup, and re-running a Unity extraction per HTTP
# request is absurd. So this exports only the parts that never change -- the raw map texture
# (no markers drawn) and where each named spot lands on it in pixels -- and the browser draws
# the day's markers on top from the live /api/park state.
#
# WHY this file IMPORTS park_map instead of carrying its own transform: the calibration is
# park_map's (THE CALIBRATION there: the client's own PositionToAnchoredPosition and each map's
# layout). A second implementation of "where a treasure is" would drift from it. Everything
# positional here is park_map's own load_map / load_scene / master_join / project, byte for
# byte.
#
# Usage (SYSTEM python -- needs UnityPy + Pillow, same interpreter park_map.py documents):
#   python park_export.py            # export, then re-check the written artefacts
#   python park_export.py --check    # only re-check what is already on disk, export nothing
#
# Output: dumps/park_export/park-<area>.png + dumps/park_export/park_spots.json
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import park_map  # noqa: E402  -- owns the transform and the asset plumbing; never re-derive

OUT = os.path.join(park_map.DUMPS, "park_export")
JSON_PATH = os.path.join(OUT, "park_spots.json")

def area_names():
    """area id -> the game's own name (LangArea_Eng, through Area's name id)."""
    from master_cache import rows
    lang = {r["id"]: (r["data"].get(1000) or [""])[0] for r in rows("LangArea_Eng")}
    return {r["id"]: lang.get((r["data"].get(4) or [""])[0], "") for r in rows("Area")}


def export():
    """Write the raw map PNGs and park_spots.json. Everything comes from park_map."""
    spot, treasures, cands = park_map.master_join()
    names = area_names()
    os.makedirs(OUT, exist_ok=True)
    areas = {}
    for area, cfg in park_map.AREAS.items():
        park_map.ensure([cfg["map"], cfg["scene"]])
        base, cal = park_map.load_map(cfg["map"])
        world = park_map.load_scene(cfg["scene"])
        w, h = base.size
        image = f"park-{area}.png"
        # RAW texture, no markers -- RGBA kept so the containment check (and the browser's
        # transparent corners) still have the alpha channel park_map.verify() relies on.
        base.save(os.path.join(OUT, image))
        spots = {}
        for name, (x, z) in sorted(world.items()):
            px, py = park_map.project(x, z, (w, h), cal)
            spots[name] = {"x": round(px), "y": round(py)}
        areas[area] = {
            "name": names.get(area) or cfg["label"],
            "image": image,
            "width": w,
            "height": h,
            # informational -- the pixels above are already projected; rounded only to keep
            # float32 noise (8.399999618...) out of a file humans will read
            "calibration": {"scale": round(cal[0], 6),
                            "offset_x": round(cal[1], 6),
                            "offset_y": round(cal[2], 6),
                            "kx": round(cal[3], 6), "ky": round(cal[4], 6)},
            "spots": spots,
        }
        print(f"  {area:16s} {image}  {w}x{h}  {len(spots)} spots")

    # the game's own chest marker (the in-game map's icon), drawn by the page over each chest
    park_map.ensure(["img_park_map_icon_treasure"])
    for o in park_map._load_bundle("img_park_map_icon_treasure").objects:
        if o.type.name == "Texture2D":
            o.read().image.save(os.path.join(OUT, "park-chest.png"))
    # two areas share the game's name 'Play Area' (the day game floor and the night casino)
    taken = [a["name"] for a in areas.values()]
    for area, a in areas.items():
        if taken.count(a["name"]) > 1:
            a["name"] = f"{a['name']} · {park_map.AREAS[area]['label']}"

    doc = {
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "areas": areas,
        # park_target_mapping_id -> where it is, for resolving the day's PICKED ids.
        # Restricted to the 198 random-symbol candidate mappings on purpose: the full
        # ParkTargetMapping table is 8297 rows of NPC chats and content targets whose spot
        # names are not in the scene's reward arrays -- exporting them bloats the file 4x
        # and every picked id is by definition drawn from the candidate pool anyway.
        "mapping_to_spot": {m: {"area": spot[m][0], "spot": spot[m][1]}
                            for lst in cands.values()
                            for m, _name in sorted(lst)},
        # the candidate pool per area (what COULD spawn), for an optional overlay
        "candidates": {a: sorted(m for m, _ in lst) for a, lst in sorted(cands.items())},
        # the 42 fixed chests (background, not a task)
        "treasures": {tid: {"area": a, "spot": name}
                      for a, lst in sorted(treasures.items())
                      for tid, name in sorted(lst)},
    }
    # bytes, not text: Path.write_text would re-translate newlines on Windows
    with open(JSON_PATH, "wb") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8"))
    print(f"  wrote {os.path.relpath(JSON_PATH, HERE)}"
          f"  ({os.path.getsize(JSON_PATH)} bytes)")


def check():
    """Re-run park_map.verify()'s containment test, but against the WRITTEN artefacts.

    Reads park_spots.json and the exported PNGs back off disk and requires every exported
    pixel coordinate to land on a non-transparent pixel of the exported image -- the same
    evidence the calibration itself rests on (354/354). Verifies artefacts, not counters:
    a JSON that says 354 spots but points them into the void fails here."""
    if not os.path.exists(JSON_PATH):
        print(f"  MISSING {JSON_PATH} -- run park_export.py first")
        return False
    from PIL import Image
    data = json.loads(open(JSON_PATH, "rb").read().decode("utf-8"))
    total = good = 0
    ok_all = True
    for area, a in data["areas"].items():
        img_path = os.path.join(OUT, a["image"])
        if not os.path.exists(img_path):
            print(f"  MISSING {img_path}")
            ok_all = False
            continue
        img = Image.open(img_path).convert("RGBA")
        if img.size != (a["width"], a["height"]):
            print(f"  *** {area}: JSON says {a['width']}x{a['height']}, "
                  f"image is {img.size[0]}x{img.size[1]}")
            ok_all = False
        n_ok = 0
        for name, p in a["spots"].items():
            x, y = p["x"], p["y"]
            if 0 <= x < img.size[0] and 0 <= y < img.size[1] \
                    and img.getpixel((x, y))[3] > 16:
                n_ok += 1
        total += len(a["spots"])
        good += n_ok
        # With the client's own calibration 2 of Soda Pop Lagoon's 219
        # positions sit a few px past the drawn shoreline (a beach chest, a shore symbol): the
        # 3D shore and the drawn one do not agree to the pixel. Up to 1 % per area is reported,
        # more fails -- a wrong transform moves far more than that.
        off = len(a["spots"]) - n_ok
        if off * 100 > len(a["spots"]):
            ok_all = False
        flag = "OK " if not off else "~  " if off * 100 <= len(a["spots"]) else "*** "
        print(f"  {flag}{area:16s} {n_ok}/{len(a['spots'])} exported spots "
              f"on non-transparent pixels of {a['image']}")
    print(f"  total {good}/{total} on the drawn map")

    # join integrity: everything a consumer will look up must actually resolve.
    # 1. every candidate mapping id resolves through mapping_to_spot to a spot that
    #    exists in its area's spots dict (this is the path the day's picked ids take);
    joins = bad = 0
    for area, mappings in data["candidates"].items():
        for m in mappings:
            joins += 1
            ms = data["mapping_to_spot"].get(m)
            if not ms or ms["spot"] not in data["areas"].get(ms["area"], {}).get("spots", {}):
                bad += 1
                print(f"  *** candidate {m}: no pixel position")
    # 2. every treasure in an EXPORTED area resolves the same way. The three interior
    #    chests (a01-house/-restaurant/-theater) have no map asset and stay area-only.
    interior = 0
    for tid, t in data["treasures"].items():
        if t["area"] not in data["areas"]:
            interior += 1
            continue
        joins += 1
        if t["spot"] not in data["areas"][t["area"]]["spots"]:
            bad += 1
            print(f"  *** treasure {tid}: spot {t['spot']} not in {t['area']}")
    print(f"  joins {joins - bad}/{joins} resolve to a pixel position"
          f"  ({interior} interior treasures have no exported map, by design)")
    return ok_all and bad == 0


def main():
    ap = argparse.ArgumentParser(
        description="Export raw park maps + spot pixel positions for the web page.")
    ap.add_argument("--check", action="store_true",
                    help="only re-check the artefacts already on disk; export nothing")
    args = ap.parse_args()
    if not args.check:
        export()
    sys.exit(0 if check() else 1)


if __name__ == "__main__":
    main()
