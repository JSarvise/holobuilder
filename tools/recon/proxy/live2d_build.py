"""The leaders' Live2D figures for the web: the game's Unity Cubism bundles -> standard
Live2D web files (model3.json + moc3 + texture + physics3/pose3/exp3/motion3), plus an
index card -> outfit -> model the web server serves (`/api/live2d/index`, `/live2d/...`).

What the game shows on its unit screen: the model of the leader's OUTFIT, `live2d_mdl_<Costume.asset_id>`;
the character's `CharacterMotion` type 10 (IDLE_01) motion, looped, with its expression
(`live2d_exp_idle-01_<chr>_<Costume.expression_asset_id>`); blinking from the model's
EyeBlink parameters; no pointer tracking.

Conversion (checked on Noel / Soleil Kiss, loaded and animated in Chrome):
  * moc3: `CubismMoc._bytes` verbatim (MOC3 v5 -> Cubism Core 5).
  * texture: the model's Texture2D, written as lossy WebP (4096^2, ~1 MB instead of ~4 MB PNG).
  * physics3.json: `CubismPhysicsController._rig`, field by field.
  * pose3.json: from `CubismPosePart` components, when the model has any.
  * exp3.json: `CubismExpressionData` 1:1 (blend 0/1/2 = Overwrite/Add/Multiply).
  * motion3.json: the AnimationClip's `m_MuscleClip` (streamed / dense / constant curves);
    a binding path is crc32("Parameters/<Id>") or crc32("Parts/<Id>"), named by the model's
    own CubismParameter / CubismPart GameObjects and by the motion's `Live2DMotionDefine`;
    every streamed cubic key becomes an exact Bezier segment. Motions are generic (bound by
    parameter name), so each is converted once into a shared folder.

Downloads go through `octo_fetch.fetch` (the game's CDN, md5-checked, 1 request/s, nothing
re-fetched). Output: tools/recon/proxy/dumps/live2d/ (gitignored):
    index.json
    models/<asset id>/model3.json, model.moc3, texture_00.webp, physics3.json[, pose3.json]
    expressions/<expression name>.exp3.json
    motions/<motion name>.motion3.json

    python live2d_build.py --owned          # every outfit the account's cards grant (default)
    python live2d_build.py --all            # every outfit with a model in the catalogue
    python live2d_build.py --cards card-00022-5-uniq-0063-00 ...
Nothing here writes data/ or state/.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import time
import warnings
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
DUMPS = os.path.join(HERE, "dumps")
FETCHED = os.path.join(DUMPS, "octo_fetched")
OUT = os.path.join(DUMPS, "live2d")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

BUILD_VERSION = 1
BLEND = {0: "Overwrite", 1: "Add", 2: "Multiply"}          # CubismParameterBlendMode
COMP = {0: "X", 1: "Y", 2: "Angle"}                        # CubismPhysicsSourceComponent
IDLE_01 = 10                                               # enums.CharacterMotionType
WEBP_QUALITY = 92


def _unity():
    warnings.filterwarnings("ignore")
    import UnityPy  # noqa: PLC0415
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"
    return UnityPy


def load(name: str):
    import octo_crypto  # noqa: PLC0415
    raw = open(os.path.join(FETCHED, name + ".bundle"), "rb").read()
    return _unity().load(octo_crypto.decrypt_bundle(raw, name))


def dump_json(path: str, obj) -> None:
    """Write then rename: outfits are converted in parallel and two may write the same shared
    motion or expression at once (the same bytes); a reader never sees half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    try:
        os.replace(tmp, path)
    except PermissionError:          # Windows: the other writer holds it -- its copy is the same
        os.remove(tmp)


# ------------------------------------------------------------------ the master's mapping
def _field(row, n, default=None):
    v = (row.get("data") or {}).get(n)
    return v[0] if v else default


def leader_map() -> dict:
    """{"costumes": {costume id: {asset, chr, expression}}, "cards": {card id: costume id},
    "idle": {chr: (motion, expression)}} from the master tables (Card.reward_costume_id = field
    23; 3-star cards grant none: their outfit is the costume whose leader skill is the card's,
    else the character's default costume)."""
    import master_cache as mc  # noqa: PLC0415

    costumes, by_skill = {}, {}
    for r in mc.rows("Costume", "SELECT * FROM Costume"):
        cid = _field(r, 1)
        costumes[cid] = {"asset": _field(r, 5), "chr": _field(r, 2), "expression": _field(r, 6, "000")}
        skill = _field(r, 4)
        if skill:
            by_skill[skill] = cid
    defaults = {_field(r, 1): _field(r, 7) for r in mc.rows("Character", "SELECT * FROM Character")}
    cards = {}
    for r in mc.rows("Card", "SELECT * FROM Card"):
        card = _field(r, 1)
        cos = _field(r, 23) or by_skill.get(f"live_leader_skill-{card}") or defaults.get(_field(r, 2))
        if cos in costumes:
            cards[card] = cos
    idle = {}
    for r in mc.rows("CharacterMotion", "SELECT * FROM CharacterMotion"):
        if _field(r, 2) == IDLE_01:
            idle[_field(r, 1)] = (_field(r, 3), _field(r, 4))
    return {"costumes": costumes, "cards": cards, "idle": idle}


# ------------------------------------------------------------------ model
def read_model(name: str) -> dict:
    env = load(name)
    scripts, gos, mbs = {}, {}, []
    tex = []
    for o in env.objects:
        tn = o.type.name
        if tn == "MonoScript":
            scripts[o.path_id] = o.read_typetree()["m_ClassName"]
        elif tn == "GameObject":
            gos[o.path_id] = o.read_typetree()["m_Name"]
        elif tn == "MonoBehaviour":
            mbs.append(o.read_typetree())
        elif tn == "Texture2D":
            tex.append(o.read())
    m = {"params": [], "parts": [], "blink": [], "mouth": [], "pose": [], "hit": [], "moc": None,
         "rig": None, "tex": sorted(tex, key=lambda t: t.m_Name)}
    for t in mbs:
        cls = scripts.get(t["m_Script"]["m_PathID"], "?")
        go = gos.get(t["m_GameObject"]["m_PathID"])
        if cls == "CubismParameter":
            m["params"].append((t["_unmanagedIndex"], go))
        elif cls == "CubismPart":
            m["parts"].append((t["_unmanagedIndex"], go))
        elif cls == "CubismEyeBlinkParameter":
            m["blink"].append(go)
        elif cls == "CubismMouthParameter":
            m["mouth"].append(go)
        elif cls == "CubismPosePart":
            m["pose"].append((go, t))
        elif cls == "CubismHitDrawable":
            m["hit"].append((go, t["Name"]))
        elif cls == "CubismMoc":
            m["moc"] = bytes(t["_bytes"])
        elif cls == "CubismPhysicsController":
            m["rig"] = t["_rig"]
    m["params"].sort()
    m["parts"].sort()
    if not m["moc"] or not m["tex"]:
        raise ValueError(f"{name}: no CubismMoc or no texture")
    return m


def physics3(rig: dict) -> dict:
    settings, dictionary = [], []
    n_in = n_out = n_v = 0
    for i, s in enumerate(rig["SubRigs"], 1):
        pid = f"PhysicsSetting{i}"
        dictionary.append({"Id": pid, "Name": s["Name"]})
        inputs = [{"Source": {"Target": "Parameter", "Id": x["SourceId"]}, "Weight": x["Weight"],
                   "Type": COMP[x["SourceComponent"]], "Reflect": bool(x["IsInverted"])} for x in s["Input"]]
        outputs = [{"Destination": {"Target": "Parameter", "Id": x["DestinationId"]},
                    "VertexIndex": x["ParticleIndex"], "Scale": x["AngleScale"], "Weight": x["Weight"],
                    "Type": COMP[x["SourceComponent"]], "Reflect": bool(x["IsInverted"])} for x in s["Output"]]
        verts = [{"Position": {"X": v["InitialPosition"]["x"], "Y": v["InitialPosition"]["y"]},
                  "Mobility": v["Mobility"], "Delay": v["Delay"], "Acceleration": v["Acceleration"],
                  "Radius": v["Radius"]} for v in s["Particles"]]
        nz = s["Normalization"]
        norm = {k: {"Minimum": nz[k]["Minimum"], "Default": nz[k]["Default"], "Maximum": nz[k]["Maximum"]}
                for k in ("Position", "Angle")}
        n_in += len(inputs)
        n_out += len(outputs)
        n_v += len(verts)
        settings.append({"Id": pid, "Input": inputs, "Output": outputs, "Vertices": verts,
                         "Normalization": norm})
    meta = {"PhysicsSettingCount": len(settings), "TotalInputCount": n_in, "TotalOutputCount": n_out,
            "VertexCount": n_v,
            "EffectiveForces": {"Gravity": {"X": rig["Gravity"]["x"], "Y": rig["Gravity"]["y"]},
                                "Wind": {"X": rig["Wind"]["x"], "Y": rig["Wind"]["y"]}},
            "PhysicsDictionary": dictionary}
    if rig.get("Fps"):
        meta["Fps"] = rig["Fps"]
    return {"Version": 3, "Meta": meta, "PhysicsSettings": settings}


def exp3(name: str) -> dict:
    for o in load(name).objects:
        if o.type.name == "MonoBehaviour":
            t = o.read_typetree()
            if "Parameters" in t and "FadeInTime" in t:
                return {"Type": "Live2D Expression", "FadeInTime": t["FadeInTime"],
                        "FadeOutTime": t["FadeOutTime"],
                        "Parameters": [{"Id": p["Id"], "Value": p["Value"], "Blend": BLEND[p["Blend"]]}
                                       for p in t["Parameters"]]}
    raise ValueError(f"{name}: no CubismExpressionData")


# ------------------------------------------------------------------ motions
def streamed_keys(sc: dict) -> dict:
    """Unity StreamedClip -> {curve index: [(time, a, b, c, d)]} (value = ((a x + b) x + c) x + d)."""
    buf = struct.pack("<%dI" % len(sc["data"]), *sc["data"])
    pos, out = 0, {}
    while pos < len(buf):
        t, nkeys = struct.unpack_from("<fI", buf, pos)
        pos += 8
        for _ in range(nkeys):
            idx, a, b, c, d = struct.unpack_from("<i4f", buf, pos)
            pos += 20
            out.setdefault(idx, []).append((t, a, b, c, d))
    return out


def curve_segments(keys, t_end):
    """A streamed curve as motion3 Segments: each cubic key is an exact Bezier segment."""
    keys = sorted((k for k in keys if math.isfinite(k[0]) and -1e-4 <= k[0] <= t_end + 1e-4),
                  key=lambda k: k[0])
    if not keys:
        return None, 0
    t0, a, b, c, d = keys[0]
    seg, nseg = [max(t0, 0.0), d], 0
    for i in range(len(keys) - 1):
        t0, a, b, c, d = keys[i]
        t1, _, _, _, d1 = keys[i + 1]
        h = t1 - t0
        if h <= 0:
            continue
        if a == 0 and b == 0 and c == 0:
            seg += [0, t1, d1] if d1 == d else [2, t1, d1]
        elif a == 0 and b == 0:
            seg += [0, t1, d1]
        else:
            vh = ((a * h + b) * h + c) * h + d
            dvh = (3 * a * h + 2 * b) * h + c
            seg += [1, t0 + h / 3, d + c * h / 3, t1 - h / 3, vh - dvh * h / 3, t1, d1]
        nseg += 1
    if seg[-2] < t_end - 1e-4:
        seg += [0, t_end, seg[-1]]
        nseg += 1
    return seg, nseg


def _seg_types(seg):
    i = 2
    while i < len(seg):
        t = int(seg[i])
        yield t
        i += 1 + (6 if t == 1 else 2)


def motion3(name: str, hashes: dict) -> tuple[dict, list]:
    env = load(name)
    clip = define = None
    for o in env.objects:
        if o.type.name == "AnimationClip":
            clip = o.read_typetree()
        elif o.type.name == "MonoBehaviour":
            t = o.read_typetree()
            if "baseAnimation" in t:
                define = t
    hashes = dict(hashes)
    for b in (define or {}).get("baseAnimation", {}).get("bindings", []):   # self-describing names
        hashes.setdefault(zlib.crc32(b["path"].encode()), b["path"])
    mc = clip["m_MuscleClip"]
    c = mc["m_Clip"].get("data", mc["m_Clip"])
    dur = mc["m_StopTime"] - mc["m_StartTime"]
    binds = clip["m_ClipBindingConstant"]["genericBindings"]
    sc, dc, cc = c["m_StreamedClip"], c["m_DenseClip"], c["m_ConstantClip"]
    n_s, n_d, n_c = sc["curveCount"], dc["m_CurveCount"], len(cc["data"])
    if n_s + n_d + n_c != len(binds):
        raise ValueError(f"{name}: {n_s}+{n_d}+{n_c} curves for {len(binds)} bindings")
    curves, unknown = [], []
    skeys = streamed_keys(sc)
    value, opacity = zlib.crc32(b"Value"), zlib.crc32(b"Opacity")

    def target(i):
        bnd = binds[i]
        path = hashes.get(bnd["path"])
        if path is None:
            unknown.append(bnd["path"])
            return None
        grp, _, ident = path.partition("/")
        if grp == "Parameters" and bnd["attribute"] == value:
            return ("Parameter", ident)
        if grp == "Parts" and bnd["attribute"] == opacity:
            return ("PartOpacity", ident)
        unknown.append(path)
        return None

    for i in range(n_s):
        tg = target(i)
        seg, n = curve_segments(skeys.get(i, []), dur)
        if tg and seg:
            curves.append((tg, seg, n))
    if n_d:
        frames, arr = dc["m_FrameCount"], dc["m_SampleArray"]
        for j in range(n_d):
            tg = target(n_s + j)
            seg = [0.0, arr[j]]
            for f in range(1, frames):
                seg += [0, dc["m_BeginTime"] + f / dc["m_SampleRate"], arr[f * n_d + j]]
            if tg:
                curves.append((tg, seg, frames - 1))
    for j in range(n_c):
        tg = target(n_s + n_d + j)
        v = cc["data"][j]
        if tg:
            curves.append((tg, [0.0, v, 0, dur, v], 1))
    out = {"Version": 3,
           "Meta": {"Duration": dur, "Fps": clip["m_SampleRate"], "Loop": bool(mc["m_LoopTime"]),
                    "AreBeziersRestricted": True, "CurveCount": len(curves),
                    "TotalSegmentCount": sum(n for _, _, n in curves),
                    "TotalPointCount": sum(1 + sum({0: 1, 1: 3, 2: 1, 3: 1}[t] for t in _seg_types(seg))
                                           for _, seg, _ in curves),
                    "UserDataCount": 0, "TotalUserDataSize": 0},
           "Curves": [{"Target": t[0], "Id": t[1], "Segments": seg} for t, seg, _ in curves]}
    if define:
        out["Meta"]["FadeInTime"] = define.get("fadeInTime", 0.5)
        out["Meta"]["FadeOutTime"] = define.get("fadeOutTime", 0.5)
    return out, unknown


# ------------------------------------------------------------------ one outfit
def build_outfit(asset: str, motion: str | None, expression: str | None) -> dict:
    """Convert `live2d_mdl_<asset>` (+ its idle motion / expression) into OUT. Returns the
    index entry. The bundles must already be in dumps/octo_fetched/."""
    name = f"live2d_mdl_{asset}"
    m = read_model(name)
    folder = os.path.join(OUT, "models", asset)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "model.moc3"), "wb") as f:
        f.write(m["moc"])
    textures = []
    for k, tex in enumerate(m["tex"]):
        fn = f"texture_{k:02d}.webp"
        tex.image.save(os.path.join(folder, fn), "WEBP", quality=WEBP_QUALITY, method=6)
        textures.append(fn)
    missing = [n for _, n in m["params"] + m["parts"] if (n.encode() + b"\0") not in m["moc"]]
    if missing:
        raise ValueError(f"{name}: ids not in its moc3: {missing[:5]}")
    refs = {"Moc": "model.moc3", "Textures": textures}
    if m["rig"]:
        dump_json(os.path.join(folder, "physics3.json"), physics3(m["rig"]))
        refs["Physics"] = "physics3.json"
    if m["pose"]:
        groups = {}
        for go, t in m["pose"]:
            groups.setdefault(t["GroupIndex"], []).append(
                (t["PartIndex"], {"Id": go, "Link": list(t.get("Link", []))}))
        dump_json(os.path.join(folder, "pose3.json"),
                  {"Type": "Live2D Pose", "FadeInTime": 0.5,
                   "Groups": [[x for _, x in sorted(g)] for _, g in sorted(groups.items())]})
        refs["Pose"] = "pose3.json"
    entry = {"model": f"models/{asset}/model3.json", "motion": None, "expression": None,
             "pose_parts": len(m["pose"]), "textures": len(textures)}
    if expression and os.path.exists(os.path.join(FETCHED, expression + ".bundle")):
        path = os.path.join(OUT, "expressions", expression + ".exp3.json")
        if not os.path.exists(path):
            dump_json(path, exp3(expression))
        refs["Expressions"] = [{"Name": "idle", "File": f"../../expressions/{expression}.exp3.json"}]
        entry["expression"] = expression
    if motion and os.path.exists(os.path.join(FETCHED, motion + ".bundle")):
        hashes = {zlib.crc32(("Parameters/" + n).encode()): "Parameters/" + n for _, n in m["params"]}
        hashes.update({zlib.crc32(("Parts/" + n).encode()): "Parts/" + n for _, n in m["parts"]})
        data, unknown = motion3(motion, hashes)
        ids = {n for _, n in m["params"]} | {n for _, n in m["parts"]}
        absent = sorted({c["Id"] for c in data["Curves"]} - ids)
        # a shared motion: written once; a curve on an id this model lacks is harmless (skipped)
        path = os.path.join(OUT, "motions", motion + ".motion3.json")
        if not os.path.exists(path):
            dump_json(path, data)
        refs["Motions"] = {"Idle": [{"File": f"../../motions/{motion}.motion3.json",
                                     "FadeInTime": data["Meta"].get("FadeInTime", 0.5),
                                     "FadeOutTime": data["Meta"].get("FadeOutTime", 0.5)}]}
        entry.update(motion=motion, motion_unknown_bindings=len(unknown), motion_absent_ids=absent)
    model3 = {"Version": 3, "FileReferences": refs,
              "Groups": [{"Target": "Parameter", "Name": "EyeBlink", "Ids": m["blink"]},
                         {"Target": "Parameter", "Name": "LipSync", "Ids": m["mouth"]}],
              "HitAreas": [{"Id": go, "Name": nm} for go, nm in m["hit"]]}
    dump_json(os.path.join(folder, "model3.json"), model3)
    return entry


def _build_one(job: tuple) -> tuple:
    """(index entry, None) or (None, error line): one model failing is a line, not a dead run."""
    try:
        return build_outfit(*job), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------ main
def _owned_cards() -> list[str]:
    doc = json.load(open(os.path.join(REPO, "data", "owned.json"), encoding="utf-8"))
    ids = doc.get("owned") if isinstance(doc, dict) else doc
    if isinstance(ids, dict):
        ids = list(ids)
    return [c if isinstance(c, str) else c.get("id") for c in ids or []]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--owned", action="store_true", help="the outfits the account's cards grant (default)")
    g.add_argument("--all", action="store_true", help="every outfit with a model in the catalogue")
    g.add_argument("--cards", nargs="+", help="these card ids' outfits")
    ap.add_argument("--rebuild", action="store_true", help="convert again even when built")
    ap.add_argument("--workers", type=int, default=0,
                    help="parallel conversions (default: every thread but one; 1 = one by one)")
    args = ap.parse_args(argv)

    import octo_fetch  # noqa: PLC0415

    _tmpl, catalogue, _src = octo_fetch.catalog()
    mp = leader_map()
    costumes, cards, idle = mp["costumes"], mp["cards"], mp["idle"]
    if args.all:
        wanted_cards = list(cards)
    elif args.cards:
        wanted_cards = args.cards
    else:
        wanted_cards = _owned_cards()
    outfits = sorted({cards[c] for c in wanted_cards if c in cards})
    index_path = os.path.join(OUT, "index.json")
    try:
        index = json.load(open(index_path, encoding="utf-8"))
        if index.get("version") != BUILD_VERSION:
            index = {}
    except (OSError, ValueError):
        index = {}
    models = index.get("models") or {}

    plan, names = [], []
    for cos in outfits:
        info = costumes[cos]
        asset = info["asset"]
        mdl = f"live2d_mdl_{asset}"
        if mdl not in catalogue:
            continue                                   # an outfit the game ships no model for
        motion, expression = idle.get(info["chr"], (None, None))
        if expression and info["expression"] and not expression.endswith("_" + info["expression"]):
            expression = expression.rsplit("_", 1)[0] + "_" + info["expression"]
        plan.append((cos, asset, motion, expression))
        if args.rebuild or asset not in models:
            names += [n for n in (mdl, motion, expression) if n and n in catalogue]
    names = list(dict.fromkeys(names))
    if names:
        octo_fetch.fetch(names)
    built = failed = 0
    errors = {}
    todo = list(dict.fromkeys((asset, motion, expression) for _cos, asset, motion, expression in plan
                              if args.rebuild or asset not in models))
    # Each outfit converts on its own (CPU-bound, ~11 s, mostly the 4096^2 WebP), so they run
    # side by side in processes; the files are the same as one by one (one at a time, the 200
    # outfits of a first refresh take ~37 min).
    workers = max(1, min(args.workers or (os.cpu_count() or 2) - 1, len(todo)))
    if workers == 1:
        results = [_build_one(job) for job in todo]
    else:
        from concurrent.futures import ProcessPoolExecutor  # noqa: PLC0415
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_build_one, todo))
    for (asset, _motion, _expression), (entry, error) in zip(todo, results):
        if error is None:
            models[asset] = entry
            built += 1
        else:
            errors[asset] = error
            failed += 1
    by_card = {card: costumes[cos]["asset"] for card, cos in cards.items()
               if costumes[cos]["asset"] in models}
    index = {"version": BUILD_VERSION, "written": int(time.time()), "models": models,
             "by_card": by_card, "errors": errors}
    dump_json(index_path, index)
    print(f"{built} built, {failed} failed, {len(models)} models in the index, "
          f"{len(by_card)} cards with a figure -> {OUT}")
    for asset, why in errors.items():
        print(f"  FAILED {asset}: {why}")
    return 1 if failed and not built else 0


if __name__ == "__main__":
    raise SystemExit(main())
