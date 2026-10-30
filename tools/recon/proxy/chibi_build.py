"""The holomems' 3D chibi models for the web: the game's Unity character bundles -> one
binary glTF (.glb) per outfit, small enough to load when a search starts.

What the game ships (e.g. chr 00001 / cmmn-0000):
  * `mdl_chr_drs_<chr>-<kind>-<n>-00_body`: the body prefab. SkinnedMeshRenderers Geo_Body_LOD0
    (8,471 verts) / Geo_Body_LOD1 (3,537), Geo_Eye_LOD0, Geo_Iris_LOD0, Geo_Brow_LOD0; a
    Maya-style skeleton (`jnt_C_hips00_00`, `jnt_L_thigh00_00`, ... bone axis = local X); toon
    materials whose colour is `_BaseMap` (`t_..._bdy_col`), plus shade/ramp maps not used here.
  * `mdl_chr_drs_<chr>-<kind>-<n>-00_hair` (or the character's `-base-0000-00_hair` when the
    outfit has none): Geo_Hair_LOD0/LOD1, rooted at `jnt_C_headHair00_00`, which the game hangs
    under `jnt_C_head00_00` (the motions bind `.../jnt_C_head00_00/jnt_C_headHair00_00/...`).
  * The face's mouth and blush are URP DecalProjectors (`FacialDecal/Mouth`, ...): not exported.

Conversion: the lightest LOD of each renderer, positions / normals / UV0 / 4 bone weights; the
skeleton as glTF nodes; Unity's left-handed frame -> glTF's right-handed one by mirroring X
(positions and normals x -> -x, rotations (x, y, z, w) -> (x, -y, -z, w), bind matrices S M S,
triangle winding reversed); each material's `_BaseMap` as a WebP texture (EXT_texture_webp),
alpha-tested where the game's material is (`_ALPHATEST_ON`).

Downloads go through `octo_fetch` (the game's CDN, md5-checked, 1 request/s, nothing re-fetched).
Which outfits: the master `SdCostume` table (or `dumps/holodori/main_SdCostume.json`), body =
`mdl_chr_drs_<chr>-<bodyAssetId>_body`, hair = `..._<hairAssetId>_hair`. Its
`accessoryHairAssetId` (the character's `base-0000` hair: the same hair without the outfit's
props) is not used.

Output: tools/recon/proxy/dumps/chibi/ (gitignored)
    index.json                  {"outfits": {sd id: {chr, name, typ, model, bytes}}}
    models/<sd id>.glb

    python chibi_build.py --chr 00001             # every outfit of one character
    python chibi_build.py --outfit sdcos-00001-cmmn-0000-00
    python chibi_build.py --all
Nothing here writes data/ or state/.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import struct
import sys
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
DUMPS = os.path.join(HERE, "dumps")
FETCHED = os.path.join(DUMPS, "octo_fetched")
OUT = os.path.join(DUMPS, "chibi")
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TEX_SIZE = 512          # the runner is drawn ~150 px tall: 512 px textures are plenty
WEBP_QUALITY = 85
# the renderers kept, lightest LOD first (a renderer missing at LOD1 falls back to LOD0)
PARTS = ("Body", "Eye", "Iris", "Brow", "Hair")


def _unity():
    warnings.filterwarnings("ignore")
    import UnityPy  # noqa: PLC0415
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"
    return UnityPy


def load(name: str):
    import octo_crypto  # noqa: PLC0415
    raw = open(os.path.join(FETCHED, name + ".bundle"), "rb").read()
    return _unity().load(octo_crypto.decrypt_bundle(raw, name))


def fetch(names: list[str]) -> None:
    missing = [n for n in names if not os.path.exists(os.path.join(FETCHED, n + ".bundle"))]
    if missing:
        import octo_fetch  # noqa: PLC0415
        octo_fetch.fetch(missing)


# ------------------------------------------------------------------ catalogue
HOLODORI = os.path.join(DUMPS, "holodori")


def catalogue_names() -> set[str]:
    from master_db import decode_record  # noqa: PLC0415
    top = decode_record(open(os.path.join(DUMPS, "octocacheevai_prod.dec"), "rb").read())
    return {decode_record(b).get(2, [b""])[0].decode() for b in top.get(2, [])}


def _rows(table: str) -> list[dict]:
    return [r["data"] for r in json.load(open(os.path.join(HOLODORI, f"main_{table}.json"), encoding="utf-8"))]


def _master(table: str, fields: dict[str, int]) -> list[dict] | None:
    """The game's CURRENT master table (master_cache: the client's own copy, re-downloaded when
    the seed rotated), as {name: value} by proto field number; None when it cannot be read
    (then the holodori JSON, which only moves when someone re-exports it, stands in)."""
    try:
        import node_price  # noqa: PLC0415

        rows = node_price.load(table)
    except Exception:  # noqa: BLE001 -- the fallback says so below
        return None
    out = []
    for r in rows:
        data = r.get("data") or {}
        rec = {}
        for name, num in fields.items():
            v = (data.get(num) or [None])[0]
            rec[name] = v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else v
        out.append(rec)
    return out


def sd_costumes(names: set[str]) -> list[dict]:
    """Every SdCostume (the game's 3D chibi outfits) whose two bundles the catalogue names:
    {id, chr, name, typ, body, hair}. `typ` is the character's CharacterSdPersonalityType
    (TYP000/001/002), which picks the motion variant (`..._typ00N_...`)."""
    # master proto field numbers (dumps/holodori/master_character / master_sd_costume .gen.proto)
    mc = _master("Character", {"id": 1, "nameEng": 4, "sdPersonalityType": 20})
    ms = _master("SdCostume", {"id": 1, "characterId": 2, "hairAssetId": 4, "bodyAssetId": 6})
    if mc is not None and ms is not None:
        for c in mc:                                 # enum 1..3 -> TYP000..TYP002
            v = c.get("sdPersonalityType")
            c["sdPersonalityType"] = f"_TYP{int(v) - 1:03d}" if isinstance(v, int) and v > 0 else ""
        chars, costumes = {c["id"]: c for c in mc}, ms
    else:
        print("  ! master SdCostume / Character unreadable: using the holodori JSON (may be stale)")
        chars, costumes = {c["id"]: c for c in _rows("Character")}, _rows("SdCostume")
    out = []
    for r in costumes:
        chr5 = r["characterId"].split("-")[1]
        body = f"mdl_chr_drs_{chr5}-{r['bodyAssetId']}_body"
        hair = f"mdl_chr_drs_{chr5}-{r['hairAssetId']}_hair"
        c = chars.get(r["characterId"], {})
        typ = str(c.get("sdPersonalityType", "")).rsplit("_TYP", 1)[-1]
        if body not in names or hair not in names or not typ.isdigit():
            continue
        out.append({"id": r["id"], "chr": r["characterId"], "name": c.get("nameEng", ""),
                    "typ": int(typ), "body": body, "hair": hair})
    return out


# ------------------------------------------------------------------ reading a bundle
def transforms(env) -> dict[int, dict]:
    """{Transform path id: {name, parent, p, q, s}} of one bundle."""
    out = {}
    for o in env.objects:
        if o.type.name != "Transform":
            continue
        t = o.read_typetree()
        go = o.assets_file.objects[t["m_GameObject"]["m_PathID"]].read()
        p, q, s = t["m_LocalPosition"], t["m_LocalRotation"], t["m_LocalScale"]
        out[o.path_id] = {"name": go.m_Name, "parent": t["m_Father"]["m_PathID"],
                          "p": (p["x"], p["y"], p["z"]), "q": (q["x"], q["y"], q["z"], q["w"]),
                          "s": (s["x"], s["y"], s["z"])}
    return out


def renderers(env) -> dict[str, dict]:
    """{renderer GameObject name: {mesh, bones (Transform path ids), materials}}."""
    from UnityPy.helpers.MeshHelper import MeshHandler  # noqa: PLC0415
    out = {}
    for o in env.objects:
        if o.type.name != "SkinnedMeshRenderer":
            continue
        r = o.read()
        if not r.m_Mesh:
            continue
        mesh = r.m_Mesh.read()
        h = MeshHandler(mesh)
        h.process()
        mats = []
        for ptr in r.m_Materials:
            try:
                mats.append(ptr.read())
            except Exception:      # noqa: BLE001 -- an external shared material (outline pass)
                mats.append(None)
        out[r.m_GameObject.read().m_Name] = {
            "mesh": mesh, "h": h, "tris": h.get_triangles(),
            "bones": [b.m_PathID for b in r.m_Bones], "materials": mats,
            "bindposes": list(getattr(mesh, "m_BindPose", None) or []),
        }
    return out


def material_texture(mat) -> tuple[object | None, bool]:
    """(the material's `_BaseMap` Texture2D, alpha-tested?)."""
    rd = mat.object_reader
    t = rd.read_typetree()
    kw = t.get("m_ValidKeywords") or t.get("m_ShaderKeywords") or []
    if isinstance(kw, str):
        kw = kw.split()
    tex = None
    for k, v in t["m_SavedProperties"]["m_TexEnvs"]:
        if k == "_BaseMap" and v["m_Texture"]["m_PathID"]:
            obj = rd.assets_file.objects.get(v["m_Texture"]["m_PathID"])
            tex = obj.read() if obj is not None else None
    return tex, any("ALPHATEST" in k or "TRANSPARENT" in k for k in kw)


# ------------------------------------------------------------------ math
def mirror_q(q):
    x, y, z, w = q
    return (x, -y, -z, w)


def mirror_m(m):
    """Unity column-major 4x4 (as a flat 16-list, m[col*4+row]) -> mirrored, glTF order."""
    # S M S with S = diag(-1, 1, 1, 1): negate the entries where exactly one of row/col is x
    out = list(m)
    for col in range(4):
        for row in range(4):
            if (row == 0) != (col == 0):
                out[col * 4 + row] = -out[col * 4 + row]
    return out


def unity_matrix(mm) -> list[float]:
    """UnityPy Matrix4x4 -> flat column-major list."""
    if isinstance(mm, (list, tuple)) and len(mm) == 16:
        return list(mm)
    vals = []
    for col in range(4):
        for row in range(4):
            vals.append(getattr(mm, f"e{row}{col}"))
    return vals


# ------------------------------------------------------------------ glTF writer
class Glb:
    def __init__(self):
        self.bin = bytearray()
        self.j = {"asset": {"version": "2.0", "generator": "hololive-builder chibi_build"},
                  "scene": 0, "scenes": [{"nodes": []}], "nodes": [], "meshes": [], "skins": [],
                  "materials": [], "textures": [], "images": [], "samplers": [
                      {"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}],
                  "accessors": [], "bufferViews": [], "buffers": [],
                  "extensionsUsed": ["EXT_texture_webp"], "extensionsRequired": ["EXT_texture_webp"]}

    def view(self, data: bytes, target: int | None = None) -> int:
        while len(self.bin) % 4:
            self.bin.append(0)
        v = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        if target:
            v["target"] = target
        self.bin += data
        self.j["bufferViews"].append(v)
        return len(self.j["bufferViews"]) - 1

    def accessor(self, fmt: str, comp: int, typ: str, rows: list, target=None, minmax=False) -> int:
        flat = [x for r in rows for x in (r if isinstance(r, (list, tuple)) else (r,))]
        data = struct.pack("<%d%s" % (len(flat), fmt), *flat)
        a = {"bufferView": self.view(data, target), "componentType": comp, "count": len(rows), "type": typ}
        if minmax:
            n = len(rows[0])
            a["min"] = [min(r[i] for r in rows) for i in range(n)]
            a["max"] = [max(r[i] for r in rows) for i in range(n)]
        self.j["accessors"].append(a)
        return len(self.j["accessors"]) - 1

    def image(self, webp: bytes) -> int:
        self.j["images"].append({"bufferView": self.view(webp), "mimeType": "image/webp"})
        self.j["textures"].append({"sampler": 0, "extensions": {"EXT_texture_webp": {"source": len(self.j["images"]) - 1}}})
        return len(self.j["textures"]) - 1

    def write(self, path: str) -> int:
        while len(self.bin) % 4:
            self.bin.append(0)
        self.j["buffers"] = [{"byteLength": len(self.bin)}]
        js = json.dumps(self.j, separators=(",", ":")).encode()
        js += b" " * (-len(js) % 4)
        total = 12 + 8 + len(js) + 8 + len(self.bin)
        with open(path, "wb") as f:
            f.write(struct.pack("<III", 0x46546C67, 2, total))
            f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
            f.write(struct.pack("<II", len(self.bin), 0x004E4942) + bytes(self.bin))
        return total


def webp_of(tex) -> bytes:
    img = tex.image.convert("RGBA")
    if max(img.size) > TEX_SIZE:
        img = img.resize((TEX_SIZE, TEX_SIZE * img.size[1] // img.size[0]))
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=WEBP_QUALITY, method=6)
    return buf.getvalue()


# ------------------------------------------------------------------ the run motion
RUN_MOTION = "mot_define_general_chr_drs_run00_typ{typ:03d}_lp_bdy00"
# Mecanim human bone -> the chibi rig's joint (names as on chr 00001's skeleton)
HUMAN = {"Hips": "jnt_C_hips00_00", "Spine": "jnt_C_spine00_00", "Chest": "jnt_C_spine00_01",
         "Neck": "jnt_C_neck00_00", "Head": "jnt_C_head00_00"}
for _s, _side in (("L", "Left"), ("R", "Right")):
    HUMAN.update({f"{_side}Shoulder": f"jnt_{_s}_shoulder00_00", f"{_side}UpperArm": f"jnt_{_s}_upperArm00_00",
                  f"{_side}LowerArm": f"jnt_{_s}_foreArm00_00", f"{_side}Hand": f"jnt_{_s}_hand00_00",
                  f"{_side}UpperLeg": f"jnt_{_s}_thigh00_00", f"{_side}LowerLeg": f"jnt_{_s}_leg00_00",
                  f"{_side}Foot": f"jnt_{_s}_foot00_00"})
    for _f, _jf in (("Thumb", "Thumb"), ("Index", "Index"), ("Middle", "Middle"), ("Ring", "Ring"), ("Little", "Pinky")):
        for _k in range(3):
            HUMAN[f"{_side}{_f}{_k + 1}"] = f"jnt_{_s}_finger{_jf}00_{_k:02d}"
_motions: dict = {}


def humanoid_frames(motion: str) -> tuple[float, list]:
    """(frame rate, [137 humanoid values per frame]) of a looping humanoid clip, one loop
    (the last frame equals the first). Dense and constant curves only: the game's run clip
    keeps every humanoid value in those (its 6 streamed curves are blend shapes)."""
    if motion in _motions:
        return _motions[motion]
    fetch([motion])
    env = load(motion)
    clip = next(o.read_typetree() for o in env.objects if o.type.name == "AnimationClip")
    mc = clip["m_MuscleClip"]
    c = mc["m_Clip"].get("data", mc["m_Clip"])
    binds = clip["m_ClipBindingConstant"]["genericBindings"]
    sc, dc, cc = c["m_StreamedClip"], c["m_DenseClip"], c["m_ConstantClip"]
    ns, nd = sc["curveCount"], dc["m_CurveCount"]
    # one binding is several curves for a Transform (position 3, rotation 4, scale 3, euler 3)
    width = lambda b: {1: 3, 2: 4, 3: 3, 4: 3}.get(b["attribute"], 1) if b["typeID"] == 4 else 1
    curves = [i for i, b in enumerate(binds) for _ in range(width(b))]
    if len(curves) != ns + nd + len(cc["data"]):
        raise ValueError(f"{motion}: {len(curves)} curves for {ns}+{nd}+{len(cc['data'])}")
    rate = dc["m_SampleRate"] or clip["m_SampleRate"]
    n = int(round((mc["m_StopTime"] - mc["m_StartTime"]) * rate)) + 1
    n = min(n, dc["m_FrameCount"]) if nd else n
    arr = dc["m_SampleArray"]
    frames = [[0.0] * 137 for _ in range(n)]
    for f in frames:                                     # MotionQ / RootQ default identity
        f[6] = f[13] = 1.0
    for ci, bi in enumerate(curves):
        b = binds[bi]
        if b["typeID"] != 95 or b["attribute"] >= 137:
            continue
        if ci < ns:
            raise ValueError(f"{motion}: humanoid value {b['attribute']} is a streamed curve")
        for fi in range(n):
            frames[fi][b["attribute"]] = (arr[fi * nd + ci - ns] if ci < ns + nd
                                          else cc["data"][ci - ns - nd])
    _motions[motion] = (rate, frames)
    return _motions[motion]


def add_run_animation(g: "Glb", T: dict, node_of: dict, root, typ: int) -> int:
    """Bake the run motion of personality `typ` onto this skeleton as a glTF animation."""
    import humanoid  # noqa: PLC0415
    rate, frames = humanoid_frames(RUN_MOTION.format(typ=typ))
    names = {k: v["name"] for k, v in T.items() if k in node_of}
    nodes = {names[k]: (T[k]["p"], T[k]["q"]) for k in names}
    parent = {names[k]: (names.get(T[k]["parent"]) if k != root else None) for k in names}
    human = {b: n for b, n in HUMAN.items() if n in nodes}
    baked = humanoid.bake(nodes, parent, human, frames)
    by_name = {v: k for k, v in names.items()}
    times = [i / rate for i in range(len(frames))]
    t_acc = g.accessor("f", 5126, "SCALAR", times, minmax=False)
    g.j["accessors"][t_acc]["min"], g.j["accessors"][t_acc]["max"] = [times[0]], [times[-1]]
    channels, samplers = [], []
    for n, qs in baked["rot"].items():
        out = g.accessor("f", 5126, "VEC4", [mirror_q(q) for q in qs])
        samplers.append({"input": t_acc, "output": out, "interpolation": "LINEAR"})
        channels.append({"sampler": len(samplers) - 1, "target": {"node": node_of[by_name[n]], "path": "rotation"}})
    out = g.accessor("f", 5126, "VEC3", [(-x, y, z) for x, y, z in baked["hips_pos"]])
    samplers.append({"input": t_acc, "output": out, "interpolation": "LINEAR"})
    channels.append({"sampler": len(samplers) - 1, "target": {"node": node_of[by_name[human["Hips"]]], "path": "translation"}})
    g.j.setdefault("animations", []).append({"name": "run", "channels": channels, "samplers": samplers})
    return len(frames)


# ------------------------------------------------------------------ one outfit
def build_outfit(sd: dict) -> dict:
    """One SdCostume (see `sd_costumes`) -> models/<sd id>.glb."""
    outfit, body_name, hair_name = sd["id"], sd["body"], sd["hair"]
    fetch([body_name, hair_name])
    body, hair = load(body_name), load(hair_name)
    T = transforms(body)
    H = transforms(hair)
    by_name = {v["name"]: k for k, v in T.items()}
    head = by_name.get("jnt_C_head00_00")
    # the hair prefab's root is a container; its joint hangs under the head (see module doc)
    hair_root = next(k for k, v in H.items() if v["name"] == "jnt_C_headHair00_00")
    for k, v in H.items():
        if k == hair_root:
            T[("h", k)] = {**v, "parent": head}
        elif v["parent"] in H and v["name"].startswith(("jnt_", "ign_", "loc_")):
            T[("h", k)] = {**v, "parent": ("h", v["parent"])}
    # the bone tree: skip the prefab containers and the Geo_* holders (no bones under them)
    keep = {k for k, v in T.items() if v["name"].startswith(("jnt_", "ign_", "loc_")) or k in (head,)}
    root = by_name["jnt_C_root00_00"]
    g = Glb()
    node_of: dict = {}

    def add_node(k):
        v = T[k]
        n = {"name": v["name"], "translation": [-v["p"][0], v["p"][1], v["p"][2]],
             "rotation": list(mirror_q(v["q"]))}
        if any(abs(c - 1) > 1e-6 for c in v["s"]):
            n["scale"] = list(v["s"])
        g.j["nodes"].append(n)
        node_of[k] = len(g.j["nodes"]) - 1
        kids = [c for c, w in T.items() if w["parent"] == k and c in keep]
        for c in kids:
            add_node(c)
        if kids:
            n["children"] = [node_of[c] for c in kids]

    add_node(root)
    g.j["scenes"][0]["nodes"].append(node_of[root])

    rend = {**{("b", k): v for k, v in renderers(body).items()}, **{("h", k): v for k, v in renderers(hair).items()}}
    tex_cache: dict = {}
    stats = {}
    for part in PARTS:
        cands = [k for k in rend if k[1] == f"Geo_{part}_LOD1"] or [k for k in rend if k[1] == f"Geo_{part}_LOD0"]
        if not cands:
            continue
        src, rname = cands[0]
        R = rend[(src, rname)]
        h = R["h"]
        # joints: the renderer's bone list, mapped onto the node tree
        joints = []
        for pid in R["bones"]:
            key = pid if src == "b" else ("h", pid)
            if key not in node_of:            # a bone outside the kept tree (should not happen)
                key = root
            joints.append(node_of[key])
        binds = []
        for m in R["bindposes"]:
            binds.append(mirror_m(unity_matrix(m)))
        if not binds:
            raise ValueError(f"{rname}: no bind poses")
        g.j["skins"].append({"joints": joints, "inverseBindMatrices": g.accessor("f", 5126, "MAT4", binds),
                             "skeleton": node_of[root]})
        pos = [(-x, y, z) for x, y, z in h.m_Vertices]
        nrm = [(-x, y, z) for x, y, z in (h.m_Normals or [(0, 1, 0)] * len(pos))]
        uv = [(u, 1.0 - v) for u, v in h.m_UV0]
        # a renderer bound to one bone carries indices only (weight 1 implied); two-bone
        # renderers carry 2 of each: pad both to the 4 glTF wants
        bi = [tuple(int(i) for i in t) + (0,) * (4 - len(t)) for t in h.m_BoneIndices]
        bw = []
        for w in (h.m_BoneWeights or [(1.0,)] * len(bi)):
            s = sum(w) or 1.0
            bw.append(tuple(x / s for x in w) + (0.0,) * (4 - len(w)))
        attrs = {"POSITION": g.accessor("f", 5126, "VEC3", pos, 34962, minmax=True),
                 "NORMAL": g.accessor("f", 5126, "VEC3", nrm, 34962),
                 "TEXCOORD_0": g.accessor("f", 5126, "VEC2", uv, 34962),
                 "JOINTS_0": g.accessor("H", 5123, "VEC4", bi, 34962),
                 "WEIGHTS_0": g.accessor("f", 5126, "VEC4", bw, 34962)}
        prims = []
        for si, tris in enumerate(R["tris"]):
            mat = R["materials"][si] if si < len(R["materials"]) else None
            if mat is None or not tris:
                continue
            tex, alpha = material_texture(mat)
            if tex is None:
                continue
            key = (src, tex.m_Name)
            if key not in tex_cache:
                tex_cache[key] = g.image(webp_of(tex))
            g.j["materials"].append({"name": mat.m_Name, "pbrMetallicRoughness": {
                "baseColorTexture": {"index": tex_cache[key]}, "metallicFactor": 0, "roughnessFactor": 1},
                **({"alphaMode": "MASK", "alphaCutoff": 0.5} if alpha else {}),
                "doubleSided": part in ("Hair",), "extensions": {}})
            g.j["materials"][-1].pop("extensions")
            idx = [i for a, b, c in tris for i in (a, c, b)]          # winding reversed by the mirror
            prims.append({"attributes": attrs, "material": len(g.j["materials"]) - 1,
                          "indices": g.accessor("I", 5125, "SCALAR", idx, 34963)})
        if not prims:
            continue
        g.j["meshes"].append({"name": rname, "primitives": prims})
        g.j["nodes"].append({"name": rname, "mesh": len(g.j["meshes"]) - 1, "skin": len(g.j["skins"]) - 1})
        g.j["scenes"][0]["nodes"].append(len(g.j["nodes"]) - 1)
        stats[rname] = len(pos)
    stats["run frames"] = add_run_animation(g, T, node_of, root, sd["typ"])
    os.makedirs(os.path.join(OUT, "models"), exist_ok=True)
    size = g.write(os.path.join(OUT, "models", outfit + ".glb"))
    return {**sd, "model": f"models/{outfit}.glb", "bytes": size, "vertices": stats}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--chr", nargs="+", help="character ids, e.g. 00001")
    g.add_argument("--outfit", nargs="+", help="SdCostume ids, e.g. sdcos-00001-cmmn-0000-00")
    g.add_argument("--all", action="store_true")
    g.add_argument("--new", action="store_true",
                   help="only the SD outfits with no model yet (what refresh_all runs)")
    a = ap.parse_args(argv)
    sds = sd_costumes(catalogue_names())
    todo = [d for d in sds if a.all or (a.outfit and d["id"] in a.outfit)
            or (a.chr and d["chr"].split("-")[1] in a.chr)
            or (a.new and not os.path.exists(os.path.join(OUT, "models", d["id"] + ".glb")))]
    print(f"{len(sds)} SD outfits in the catalogue, {len(todo)} to build")
    index_path = os.path.join(OUT, "index.json")
    index = json.load(open(index_path, encoding="utf-8")) if os.path.exists(index_path) else {"outfits": {}}
    for d in todo:
        try:
            r = build_outfit(d)
            index["outfits"][d["id"]] = {k: r[k] for k in ("chr", "name", "typ", "model", "bytes")}
            print(f"  OK  {d['id']}  {r['bytes'] / 1024:.0f} KB  {r['vertices']}")
            write_index(index_path, index)          # after each one: a long run can be stopped
        except Exception as e:  # noqa: BLE001 -- one bad outfit never stops the rest
            print(f"  FAIL {d['id']}: {e!r}")
    write_index(index_path, index)
    return 0


def write_index(path: str, index: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


if __name__ == "__main__":
    raise SystemExit(main())
