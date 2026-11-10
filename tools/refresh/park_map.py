# The park's area maps and the world -> map-pixel calibration (`park_export.py` builds the
# web page's maps and spots with these helpers).
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
#   python park_map.py        # check the calibration: every known walkable spot must land on
#                             # the drawn map (exit 1 if one does not)
import io
import os
import struct
import sys
import warnings

warnings.filterwarnings("ignore")

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402
HERE = str(paths.REFRESH_DIR)
sys.path.insert(0, HERE)
DUMPS = str(paths.DUMPS)
FETCHED = os.path.join(DUMPS, "octo_fetched")


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
    left alone (`load_facilities` reads that one)."""
    b = _scene_bytes(name)
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


def _scene_bytes(name):
    """The raw serialised AreaSceneData MonoBehaviour of a scene's `_data` bundle."""
    env = _load_bundle(name)
    scripts = {o.path_id: o.read().m_Name for o in env.objects if o.type.name == "MonoScript"}
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        d = o.read()
        pid = getattr(d.m_Script, "m_PathID", getattr(d.m_Script, "path_id", None))
        if scripts.get(pid) == "AreaSceneData":
            return o.get_raw_data()
    raise RuntimeError(f"{name}: no AreaSceneData MonoBehaviour")


def parse_facilities(b):
    """{facility id: {"position": (x, z), "spawn": {name: (x, z)}}} from AreaSceneData bytes.

    The class serialises, in declaration order: m_Name, warpPoints, spawnPoints,
    playerSpawnPoints (each PointData[] = int32 count, count x {string name; Vector3 position;
    Quaternion rotation}), then _facilityData: FacilityData[] = int32 count, count x
    {string facilityId; int32 priority; int32 parkActionType; Vector3 position;
    Vector3 hoverTextPoint; PointData[] spawnPoints; PointData warpPoint}. Strings are int32
    length + UTF-8, padded to 4 bytes.

    The facility spawn points carry the spots ParkPosition names relative to a facility
    (`facilityId` + `free_npc_01`): the same short name repeats in every facility, so these
    cannot be looked up in the flat arrays. The parse must end exactly at the end of the
    buffer; anything else is a layout this reader does not know, and it raises. A facility id
    can repeat (gates have one entry per variant); their spawn points are merged."""
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
        if not 0 <= n <= 200 or p[0] + n > len(b):
            raise ValueError(f"implausible string length {n} at byte {p[0] - 4}")
        v = b[p[0]:p[0] + n].decode("utf-8")
        p[0] = (p[0] + n + 3) & ~3
        return v

    def point():
        nm = text()
        x, _y, z = f32(), f32(), f32()
        for _q in range(4):
            f32()
        return nm, (x, z)

    def points():
        n = i32()
        if not 0 <= n <= 5000:
            raise ValueError(f"implausible point count {n}")
        return [point() for _ in range(n)]

    text()                                   # m_Name
    for _ in range(3):                       # warpPoints, spawnPoints, playerSpawnPoints
        points()
    n = i32()
    if not 0 <= n <= 1000:
        raise ValueError(f"implausible facility count {n}")
    out = {}
    for _ in range(n):
        fid = text()
        i32()                                # priority
        i32()                                # parkActionType
        x, _y, z = f32(), f32(), f32()
        f32(), f32(), f32()                  # hoverTextPoint
        spawn = points()
        point()                              # warpPoint
        entry = out.setdefault(fid, {"position": (x, z), "spawn": {}})
        entry["spawn"].update(spawn)
    if p[0] != len(b):
        raise ValueError(f"AreaSceneData parse ended at byte {p[0]} of {len(b)}")
    return out


def load_facilities(name):
    """`parse_facilities` of a scene's `_data` bundle."""
    return parse_facilities(_scene_bytes(name))


def facility_piece(name):
    """(PIL RGBA picture, (offset_x, offset_y, width, height) in map root units) of a
    `mdl_map_facility_*` prefab, or None when it holds no map picture.

    The prefab (ParkMapAreaFacilityView) is hung under the map's ViewParent at the facility's
    PositionToAnchoredPosition; its FacilityImageScale -> FacilityImage chain places the
    building's silhouette relative to that point. The offset is the picture's centre."""
    env = _load_bundle(name)
    textures = {o.path_id: o.read() for o in env.objects if o.type.name == "Texture2D"}
    for o in env.objects:
        if o.type.name != "MonoBehaviour":
            continue
        raw = o.get_raw_data()
        tex = next((textures[pid] for off in range(0, len(raw) - 12, 4)
                    for fid, pid in [struct.unpack_from("<iq", raw, off)]
                    if fid == 0 and pid in textures), None)
        if tex is None or "_map_facility" not in tex.m_Name:
            continue
        go = o.read().m_GameObject.read()
        rt = next(c for c in ((c.component if hasattr(c, "component") else c).read()
                              for c in go.m_Component)
                  if type(c).__name__ == "RectTransform")
        x = y = 0.0
        sx = sy = 1.0
        for r in reversed(_rect_chain(rt)):          # root down: position, then that node's scale
            x += r.m_AnchoredPosition.x * sx
            y += r.m_AnchoredPosition.y * sy
            sx *= r.m_LocalScale.x
            sy *= r.m_LocalScale.y
        w, h = rt.m_SizeDelta.x * sx, rt.m_SizeDelta.y * sy
        # anchoredPosition places the pivot; the picture's centre is half a size away from it
        x += (0.5 - rt.m_Pivot.x) * w
        y += (0.5 - rt.m_Pivot.y) * h
        return tex.image.convert("RGBA"), (x, y, w, h)
    return None


def composite_facility(image, cal, world_xz, piece):
    """Draw one `facility_piece` onto an area map at the facility's world position."""
    picture, (ox, oy, w, h) = piece
    kx, ky = cal[3], cal[4]
    px, py = project(world_xz[0], world_xz[1], image.size, cal)
    pw, ph = max(1, round(w * kx)), max(1, round(h * ky))
    cx, cy = px + ox * kx, py - oy * ky
    left, top = round(cx - pw / 2), round(cy - ph / 2)
    im = picture.resize((pw, ph))
    # alpha_composite takes no negative destination: clip the piece to the map first
    cl, ct = max(0, -left), max(0, -top)
    cr, cb = min(pw, image.width - left), min(ph, image.height - top)
    if cr > cl and cb > ct:
        image.alpha_composite(im.crop((cl, ct, cr, cb)), (left + cl, top + ct))


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


# --- drawing ------------------------------------------------------------------------


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


if __name__ == "__main__":
    sys.exit(0 if verify() else 1)
