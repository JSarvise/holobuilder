"""Answer "which sprite does this prefab node actually draw?" without IL2CPP metadata.

`Image` is a MonoBehaviour, so its fields are unreadable without a type tree -- but a
PPtr<Sprite> is just (int32 file_id, int64 path_id) sitting in the raw bytes. So: take the
GameObject's components, scan their raw bytes for every plausible PPtr, resolve file_id
through the SerializedFile's `externals` table, and look the path_id up in that file. Any hit
that lands on a real Sprite is the answer.

Two gotchas this handles: for a `.bundle`, `env.files` holds the *container*, not the
SerializedFile (reach it via `obj.assets_file`); and externals name a cab file, not a bundle,
so resolving them needs one pass over the whole Addressables directory -- done once, for all
wanted path_ids at a time, not per lookup.

    python resolve_images.py <bundle> <NombreDelGameObject> [<mas nombres>...]
"""
import os, sys, struct, warnings, collections
warnings.filterwarnings("ignore")
import UnityPy
UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"

AA = (r"C:\Program Files (x86)\Steam\steamapps\common\hololiveDreams"
      r"\hololive-Dreams_Data\StreamingAssets\aa\StandaloneWindows64")

if len(sys.argv) < 3:
    sys.exit("uso: resolve_images.py <bundle> <NombreGameObject> [...]")
bundle, want = sys.argv[1], set(sys.argv[2:])

env = UnityPy.load(os.path.join(AA, bundle))
objs = list(env.objects)
by_pid = {}
for o in objs:
    by_pid.setdefault(id(o.assets_file), {})[o.path_id] = o

# node name -> [(component type, [(ext_path, path_id) or (None, local path_id)], ...)]
pending = collections.defaultdict(list)          # (ext_basename, path_id) -> [(node, off)]
local_hits = collections.defaultdict(list)
found_nodes = []

for o in objs:
    if o.type.name != "GameObject":
        continue
    try:
        d = o.read()
    except Exception:
        continue
    nm = str(getattr(d, "m_Name", "") or "")
    if nm not in want:
        continue
    sf = o.assets_file
    externals = [getattr(e, "path", "") for e in (getattr(sf, "externals", None) or [])]
    local = by_pid[id(sf)]
    found_nodes.append(nm)
    for c in (getattr(d, "m_Component", None) or []):
        cp = getattr(c, "component", c)
        co = local.get(getattr(cp, "path_id", None))
        if co is None or co.type.name != "MonoBehaviour":
            continue
        raw = co.get_raw_data()
        seen = set()
        for off in range(0, max(len(raw) - 12, 0), 4):
            fid, tgt = struct.unpack_from("<iq", raw, off)
            if not (0 <= fid <= len(externals)) or tgt == 0 or (fid, tgt) in seen:
                continue
            seen.add((fid, tgt))
            if fid == 0:
                t = local.get(tgt)
                if t is not None and t.type.name in ("Sprite", "Texture2D", "Material"):
                    n2 = ""
                    try:
                        n2 = str(getattr(t.read(), "m_Name", "") or "")
                    except Exception:
                        pass
                    local_hits[nm].append((off, t.type.name, n2, bundle))
            else:
                pending[(os.path.basename(externals[fid - 1]).lower(), tgt)].append((nm, off))

print(f"nodos encontrados: {sorted(set(found_nodes))}")
print(f"PPtr externos a resolver: {len(pending)}")

resolved = {}
files = sorted(f for f in os.listdir(AA) if f.endswith(".bundle"))
cabs_wanted = {k[0] for k in pending}
for i, bf in enumerate(files):
    if i % 300 == 0:
        print(f"  .. {i}/{len(files)}", flush=True)
    if not pending:
        break
    try:
        e2 = UnityPy.load(os.path.join(AA, bf))
    except Exception:
        continue
    for o in e2.objects:
        cab = os.path.basename(getattr(o.assets_file, "name", "") or "").lower()
        if cab not in cabs_wanted:
            continue
        key = (cab, o.path_id)
        if key in pending and o.type.name in ("Sprite", "Texture2D", "Material"):
            nm2 = ""
            try:
                nm2 = str(getattr(o.read(), "m_Name", "") or "")
            except Exception:
                pass
            resolved[key] = (o.type.name, nm2, bf)

print()
for node in sorted(set(found_nodes)):
    print(f"=== {node}")
    for off, t, n, b in local_hits.get(node, []):
        print(f"    +{off:<5d} local -> {t} {n}")
    for key, refs in pending.items():
        for n2, off in refs:
            if n2 != node:
                continue
            hit = resolved.get(key)
            if hit:
                print(f"    +{off:<5d} {key[0][:26]:<26s} -> {hit[0]} {hit[1]}  [{hit[2]}]")
