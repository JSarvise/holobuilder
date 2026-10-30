# Draw the park's treasures and random symbols ONTO the game's own area maps.
#
# park_treasure.py answers *which* spots are live; this answers *where they are*. It joins
# three things that live in three different places:
#
#   1. The map image -- Texture2D inside the Octo bundle named by master `AreaMap.map_asset_id`
#      (`mdl_map_area-*`). The same bundle also holds the `ParkMapAreaView` component, and that
#      component is the calibration (see below).
#   2. The world positions -- an `AreaSceneData` MonoBehaviour in the scene's `_data` bundle,
#      a flat array of {string name; Vector3 position; Quaternion rotation}. The names are
#      exactly the strings master `ParkPosition.position` holds (`reward_treasure_07`,
#      `reward_random_82`), so the join to master data is by name, not by guesswork.
#      The array parses cleanly and its counts match master exactly: 23 / 6 / 7
#      `reward_treasure_*` entries against 23 / 6 / 7 `ParkTreasure` rows for a01 / game / casino.
#   3. The account state -- from park_treasure.py, which reads it out of `/rpc.api.User/Get`.
#
# THE CALIBRATION
# ---------------
# `ParkMapAreaView` serialises (ratio, offsetX, offsetY); the client's
# PositionToAnchoredPosition is literally  anchored = (x*ratio + offX, z*ratio + offY),  an
# anchored position in the view's ViewParent, whose origin is the map root's centre. The map
# picture is the fixed chain AreaBack -> Scale -> Image, all centre-anchored on that origin, so
# it spans Image.sizeDelta x Scale.localScale root units. Hence, per map:
#
#     Kx = texture width  / (Image.sizeDelta.x * Scale.localScale.x)
#     px = W/2 + Kx*(x*ratio + offX),   py = H/2 - Ky*(z*ratio + offY)
#
# K differs per map: a01's is 1551 / (5960 x 0.2197) = 1.1845, but with Scale 0.52 / 0.30 the
# Play Area and Lobby maps have K = 1.923 / 3.333. A single K would draw their markers at
# 62 % / 36 % of their true distance from the centre -- and still pass a "lands on a
# non-transparent pixel" check, so that check alone cannot validate the calibration. Ignoring
# the Scale rect makes those maps ~2-3x too wide.
#
# Facility pictures that the map draws as separate images (Soda Pop Lagoon's islands and
# lighthouses, under `Islands` / `LightHouses`) are composited onto the exported texture at
# their own rects, so the exported PNG is the map the game shows.
#
# Usage:
#   python park_map.py --live            # fresh login, draw every area
#   python park_map.py <flows.mitm>      # from a capture
#   python park_map.py --candidates      # also show every spot a symbol COULD spawn on
#   python park_map.py --area area-a01   # just one
#
# Runs on the SYSTEM python (needs UnityPy + Pillow). The account state comes from
# park_treasure.py, which needs proxyenv's pycryptodome, so it is invoked as a subprocess
# rather than imported -- the two toolchains genuinely do not share an interpreter here.
#
# Output: dumps/park_maps/park-<area>.png
import argparse
import io
import json
import os
import struct
import subprocess
import sys
import warnings

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DUMPS = os.path.join(HERE, "dumps")
OUT = os.path.join(DUMPS, "park_maps")
FETCHED = os.path.join(DUMPS, "octo_fetched")
PROXY_PY = os.path.join(HERE, "proxyenv", "Scripts", "python.exe")

UI_TO_PIXEL = 1.2   # the old measured constant: reported, no longer used to project

AREAS = {
    "area-a01": dict(
        map="mdl_map_area-a01-02",
        scene="env_3d_park_area-a01-area-02-noon_data",
        label="Main Park"),
    "area-a02-game": dict(
        map="mdl_map_area-a02-02",
        scene="env_3d_park_area-a02-area-02-noon_data",
        label="Game Corner"),
    "area-a02-casino": dict(
        map="mdl_map_area-a02-03",
        scene="env_3d_park_area-a02-area-03-night_data",
        label="Casino"),
    "area-a02-lobby": dict(
        map="mdl_map_area-a02-01",
        scene="env_3d_park_area-a02-area-01-noon_data",
        label="Lobby"),
    # Soda Pop Lagoon: the three maps are one picture
    # at three stages of the unlock (-00 / -01 carry a 'disable' mask over the part still
    # locked); -02 is the whole lagoon. Its two scenes hold the same 219 positions.
    "area-a03": dict(
        map="mdl_map_area-a03-02",
        scene="env_3d_park_area-a03-area-01-noon_data",
        label="Soda Pop Lagoon"),
}

# marker colours (RGB)
C_TREASURE_OPEN = (215, 30, 40)
C_TREASURE_DONE = (120, 120, 130)
C_SYMBOL_OUT = (255, 150, 0)
C_SYMBOL_DONE = (150, 140, 120)
C_CANDIDATE = (90, 150, 255)


# --- assets -------------------------------------------------------------------------

def ensure(names):
    """Download any of `names` not already on disk. One request/sec, md5-checked."""
    missing = [n for n in names if not os.path.exists(os.path.join(FETCHED, f"{n}.bundle"))]
    if not missing:
        return
    print(f"fetching {len(missing)} asset(s) from the CDN: {', '.join(missing)}")
    from octo_fetch import fetch
    fetch(missing)


def _load_bundle(name):
    import UnityPy
    import octo_crypto
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"
    raw = open(os.path.join(FETCHED, f"{name}.bundle"), "rb").read()
    return UnityPy.load(io.BytesIO(octo_crypto.decrypt_bundle(raw, name)))


def _rect_chain(rt):
    """[rt, parent, ...] up to the root."""
    out = []
    while rt is not None:
        out.append(rt)
        rt = rt.m_Father.read() if rt.m_Father.path_id else None
    return out


def load_map(name):
    """(PIL RGBA image, (ratio, offset_x, offset_y, kx, ky)) for a `mdl_map_area-*` bundle.

    The picture is the `ui_200_img_map_area_*` texture (a bundle can also hold facility pieces
    and a 'disable' mask), with the facility pieces composited on; kx / ky convert root units to
    its pixels (see THE CALIBRATION)."""
    env = _load_bundle(name)
    scripts = {o.path_id: o.read().m_Name for o in env.objects if o.type.name == "MonoScript"}
    textures = {o.path_id: o.read() for o in env.objects if o.type.name == "Texture2D"}
    base = next((t for t in textures.values() if t.m_Name.startswith("ui_200_img_map_area_")), None)
    ratio = None
    image_rt = None
    pieces = []
    for o in env.objects:
        if o.type.name == "RectTransform":
            rt = o.read()
            if rt.m_GameObject.read().m_Name == "Image" and rt.m_Father.path_id:
                father = rt.m_Father.read()
                if father.m_GameObject.read().m_Name == "Scale":
                    image_rt = rt
        elif o.type.name == "MonoBehaviour":
            d = o.read()
            cls = scripts.get(getattr(d.m_Script, "m_PathID", getattr(d.m_Script, "path_id", None)))
            raw = o.get_raw_data()
            if cls == "ParkMapAreaView":
                ratio = tuple(struct.unpack_from("<f", raw, 28 + 4 * i)[0] for i in (4, 5, 6))
            elif cls == "VisionRawImage":
                tex = next((textures[pid] for off in range(0, len(raw) - 12, 4)
                            for fid, pid in [struct.unpack_from("<iq", raw, off)]
                            if fid == 0 and pid in textures), None)
                if tex is not None and "_map_facility" in tex.m_Name:
                    pieces.append((d.m_GameObject.read(), tex))
    if base is None or ratio is None or image_rt is None:
        raise RuntimeError(f"{name}: no map texture, ParkMapAreaView or AreaBack/Scale/Image")
    image = base.image.convert("RGBA")
    scale = image_rt.m_Father.read().m_LocalScale
    kx = image.width / (image_rt.m_SizeDelta.x * scale.x)
    ky = image.height / (image_rt.m_SizeDelta.y * scale.y)
    for go, tex in pieces:
        rt = next(c for c in ((c.component if hasattr(c, "component") else c).read() for c in go.m_Component)
                  if type(c).__name__ == "RectTransform")
        x = y = 0.0
        sx = sy = 1.0
        for r in reversed(_rect_chain(rt)):          # root down: position, then that node's scale
            x += r.m_AnchoredPosition.x * sx
            y += r.m_AnchoredPosition.y * sy
            sx *= r.m_LocalScale.x
            sy *= r.m_LocalScale.y
        w = max(1, round(rt.m_SizeDelta.x * sx * kx))
        h = max(1, round(rt.m_SizeDelta.y * sy * ky))
        im = tex.image.convert("RGBA").resize((w, h))
        cx, cy = image.width / 2 + x * kx, image.height / 2 - y * ky
        image.alpha_composite(im, (round(cx - w / 2), round(cy - h / 2)))
    return image, ratio + (kx, ky)


def load_scene(name):
    """{position name: (world_x, world_z)} from the scene's AreaSceneData MonoBehaviour.

    Layout, recovered by hand off the bytes (the build is IL2CPP, so there is no type tree):
        <MonoBehaviour header> string m_Name;
        then a SEQUENCE of arrays, each  int32 count; count x { string name;
                                          float x,y,z; float qx,qy,qz,qw }

    How many such arrays precede the interesting one varies by area -- area-a01 opens with an
    empty one, area-a02-lobby with a one-entry spawnpoint array -- so the count is parsed
    rather than assumed, and every array is merged. Parsing stops at the first array that
    fails validation, which is how the differently-shaped facility array that follows is
    left alone."""
    env = _load_bundle(name)
    scripts = {o.path_id: o.read().m_Name for o in env.objects if o.type.name == "MonoScript"}
    target = None
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        d = o.read()
        pid = getattr(d.m_Script, "m_PathID", getattr(d.m_Script, "path_id", None))
        if scripts.get(pid) == "AreaSceneData":
            target = o
            break
    if target is None:
        raise RuntimeError(f"{name}: no AreaSceneData MonoBehaviour")
    b = target.get_raw_data()
    p = [28]

    def i32():
        v = struct.unpack_from("<i", b, p[0])[0]
        p[0] += 4
        return v

    def f32():
        v = struct.unpack_from("<f", b, p[0])[0]
        p[0] += 4
        return v

    def text():
        n = i32()
        if not 0 <= n <= 120 or p[0] + n > len(b):
            raise ValueError("implausible string length")
        v = b[p[0]:p[0] + n].decode("utf-8", "replace")
        p[0] += n
        p[0] = (p[0] + 3) & ~3
        return v

    text()  # m_Name
    out = {}
    while p[0] + 4 <= len(b):
        mark = p[0]
        try:
            count = i32()
            if not 0 <= count <= 5000:
                raise ValueError("implausible array count")
            batch = {}
            for _ in range(count):
                nm = text()
                if not nm or not all(c.isalnum() or c in "_-" for c in nm):
                    raise ValueError(f"implausible name {nm!r}")
                x, _y, z = f32(), f32(), f32()
                for _q in range(4):
                    f32()
                if not (abs(x) < 10000 and abs(z) < 10000):
                    raise ValueError("implausible coordinate")
                batch[nm] = (x, z)
        except (ValueError, struct.error):
            p[0] = mark
            break
        out.update(batch)
    return out


def project(x, z, size, cal):
    w, h = size
    s, ox, oy, kx, ky = cal
    return (w / 2 + kx * (x * s + ox),
            h / 2 - ky * (z * s + oy))


# --- master joins -------------------------------------------------------------------

def master_join():
    """(spot_of_mapping, treasures_by_area, candidates_by_area).

    spot_of_mapping: park_target_mapping_id -> (area_id, position name)."""
    from master_cache import rows
    pos = {}
    for r in rows("ParkPosition"):
        d = r["data"]
        pos[d[1][0]] = (d.get(2, [""])[0], d.get(4, [""])[0])
    spot = {}
    for r in rows("ParkTargetMapping"):
        d = r["data"]
        pid = d.get(8, [None])[0]
        if pid and pid in pos:
            spot[d[1][0]] = pos[pid]
    treasures = {}
    for r in rows("ParkTreasure"):
        d = r["data"]
        tid, mapping = d[1][0], d[2][0]
        if mapping in spot:
            area, name = spot[mapping]
            treasures.setdefault(area, []).append((tid, name))
    cands = {}
    for r in rows("ParkRandomSymbol"):
        d = r["data"]
        mapping = d[2][0]
        if mapping in spot:
            cands.setdefault(d[1][0], []).append((mapping, spot[mapping][1]))
    return spot, treasures, cands


def account_state(source_args):
    """Run park_treasure.py under proxyenv and take its --json `state`."""
    if not os.path.exists(PROXY_PY):
        raise RuntimeError(f"proxyenv interpreter not found at {PROXY_PY}")
    cmd = [PROXY_PY, os.path.join(HERE, "park_treasure.py"), "--json"] + source_args
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, cwd=HERE)
    if r.returncode != 0:
        raise RuntimeError(f"park_treasure.py failed:\n{r.stderr[-2000:]}")
    return json.loads(r.stdout)["state"]


# --- drawing ------------------------------------------------------------------------

def _font(size):
    from PIL import ImageFont
    for f in (r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf",
              r"C:\Windows\Fonts\arial.ttf"):
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, size)
            except Exception:
                pass
    return ImageFont.load_default()


def marker(d, xy, colour, radius, label=None, font=None, dim=False):
    px, py = xy
    fill = colour + (110,) if dim else colour + (255,)
    ring = (255, 255, 255, 110) if dim else (255, 255, 255, 255)
    d.ellipse([px - radius, py - radius, px + radius, py + radius],
              fill=fill, outline=ring, width=max(2, radius // 4))
    if label and font:
        tw = d.textlength(label, font=font)
        tx, ty = px - tw / 2, py + radius + 4
        d.text((tx + 2, ty + 2), label, font=font, fill=(0, 0, 0, 190))
        d.text((tx, ty), label, font=font, fill=(255, 255, 255, 255))


def draw_area(area, state, spot, treasures, cands, show_candidates):
    from PIL import Image, ImageDraw
    cfg = AREAS[area]
    ensure([cfg["map"], cfg["scene"]])
    base, cal = load_map(cfg["map"])
    world = load_scene(cfg["scene"])
    canvas = base.copy()
    d = ImageDraw.Draw(canvas, "RGBA")
    W, H = canvas.size
    r_big = max(14, W // 90)
    font = _font(max(20, W // 60))
    title_font = _font(max(30, W // 34))

    missing = []

    def place(name):
        if name not in world:
            missing.append(name)
            return None
        return project(*world[name], (W, H), cal)

    counts = dict(treasure_open=0, treasure_done=0, symbol_out=0, symbol_done=0)

    if show_candidates:
        for _mapping, name in cands.get(area, []):
            xy = place(name)
            if xy:
                marker(d, xy, C_CANDIDATE, max(6, r_big // 3), dim=True)

    opened = set(state.get("opened_treasures") or [])
    for tid, name in sorted(treasures.get(area, [])):
        xy = place(name)
        if not xy:
            continue
        done = tid in opened
        counts["treasure_done" if done else "treasure_open"] += 1
        marker(d, xy, C_TREASURE_DONE if done else C_TREASURE_OPEN, r_big,
               label=None if done else tid.rsplit("-", 1)[-1], font=font, dim=done)

    rs = (state.get("random") or {}).get(area) or {}
    collected = set(rs.get("collected") or [])
    for mapping in rs.get("picked") or []:
        name = spot.get(mapping, (None, None))[1]
        xy = place(name) if name else None
        if not xy:
            continue
        done = mapping in collected
        counts["symbol_done" if done else "symbol_out"] += 1
        marker(d, xy, C_SYMBOL_DONE if done else C_SYMBOL_OUT, int(r_big * 1.15),
               label=None if done else mapping.rsplit("-", 1)[-1], font=font, dim=done)

    # header
    import datetime
    t = rs.get("last_refreshed_time")
    when = datetime.datetime.fromtimestamp(t / 1000).strftime("%Y-%m-%d %H:%M") if t else "-"
    line1 = f"{cfg['label']}  ({area})"
    line2 = (f"symbols out {counts['symbol_out']}  collected {counts['symbol_done']}"
             f"   |   treasures unopened {counts['treasure_open']}"
             f"  opened {counts['treasure_done']}   |   rolled {when}")
    d.rectangle([0, 0, W, 116], fill=(15, 15, 25, 205))
    d.text((22, 14), line1, font=title_font, fill=(255, 255, 255, 255))
    d.text((22, 68), line2, font=font, fill=(210, 215, 235, 255))

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"park-{area}.png")
    canvas.convert("RGB").save(path)
    return path, counts, missing


def verify():
    """Re-run the calibration check: every known walkable spot must land on the drawn map.

    This is the evidence for the transform, kept runnable instead of written down once.
    A drop below 100% means the calibration or an asset variant has moved."""
    total = good = 0
    for area, cfg in AREAS.items():
        ensure([cfg["map"], cfg["scene"]])
        base, cal = load_map(cfg["map"])
        world = load_scene(cfg["scene"])
        W, H = base.size
        px_ok = 0
        for name, (x, z) in world.items():
            px, py = project(x, z, (W, H), cal)
            if 0 <= px < W and 0 <= py < H and base.getpixel((int(px), int(py)))[3] > 16:
                px_ok += 1
        total += len(world)
        good += px_ok
        flag = "OK " if px_ok == len(world) else "*** "
        print(f"  {flag}{area:16s} {px_ok}/{len(world)} positions on drawn map"
              f"   (map {W}x{H}, calibration scale={cal[0]:g} offset=({cal[1]:g},{cal[2]:g}))")
    print(f"  total {good}/{total}")
    return good == total


def main():
    ap = argparse.ArgumentParser(
        description="Draw park treasures and random symbols onto the game's own area maps.")
    ap.add_argument("capture", nargs="?", help="flows.mitm to read state from")
    ap.add_argument("--live", action="store_true", help="log in and read current state")
    ap.add_argument("--area", action="append", help="limit to this area id (repeatable)")
    ap.add_argument("--candidates", action="store_true",
                    help="also plot every spot a random symbol COULD occupy")
    ap.add_argument("--verify", action="store_true",
                    help="check the world->map calibration; draws nothing")
    args = ap.parse_args()

    if args.verify:
        sys.exit(0 if verify() else 1)

    src = ["--live"] if args.live else ([args.capture] if args.capture else [])
    state = account_state(src)
    spot, treasures, cands = master_join()

    areas = args.area or list(AREAS)
    for area in areas:
        if area not in AREAS:
            print(f"  {area}: no map defined, skipped")
            continue
        path, c, missing = draw_area(area, state, spot, treasures, cands, args.candidates)
        print(f"  {area:16s} symbols {c['symbol_out']} out / {c['symbol_done']} collected"
              f" | treasures {c['treasure_open']} unopened / {c['treasure_done']} opened"
              f"  -> {os.path.relpath(path, HERE)}")
        if missing:
            print(f"      WARNING {len(missing)} position(s) absent from the scene: "
                  f"{sorted(set(missing))[:5]}")


if __name__ == "__main__":
    main()
