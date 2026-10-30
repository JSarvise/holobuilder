"""Export every UI Sprite from the game's built-in Addressables bundles.

StreamingAssets/aa ships PLAINTEXT UnityFS -- none of the Octo header encryption applies -- so
the card chrome (rarity frames, rarity stars, type icons, generation labels, 9-slice bases) can
be read directly. UnityPy crops each Sprite out of its atlas, so we get one PNG per element
rather than a 4096x4096 sheet.

Output: dumps/ui_sprites/<atlas-or-bundle>/<sprite name>.png
"""
import os, re, sys, warnings, collections
warnings.filterwarnings("ignore")
import UnityPy
UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import game_paths  # noqa: E402  (portable install resolution)

AA = game_paths.aa_dir()
OUT = os.path.join(HERE, "dumps", "ui_sprites")

SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def group_of(env, fallback):
    """Name the output folder after the SpriteAtlas in the bundle, else the bundle id."""
    for o in env.objects:
        if o.type.name == "SpriteAtlas":
            try:
                n = str(o.read().m_Name)
                if n:
                    return SAFE.sub("_", n)
            except Exception:
                pass
    return fallback[:12]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    files = sorted(f for f in os.listdir(AA) if f.endswith(".bundle"))
    if only:
        files = [f for f in files if f.startswith(only)]
    os.makedirs(OUT, exist_ok=True)
    saved = 0
    per = collections.Counter()
    for i, f in enumerate(files):
        try:
            env = UnityPy.load(os.path.join(AA, f))
            sprites = [o for o in env.objects if o.type.name == "Sprite"]
            if not sprites:
                continue
            grp = group_of(env, f)
            d = os.path.join(OUT, grp)
            os.makedirs(d, exist_ok=True)
            for o in sprites:
                try:
                    s = o.read()
                    name = SAFE.sub("_", str(s.m_Name))[:120]
                    if not name:
                        continue
                    img = s.image
                    if img is None or img.width == 0:
                        continue
                    p = os.path.join(d, name + ".png")
                    if os.path.exists(p):          # duplicate names across bundles
                        p = os.path.join(d, f"{name}__{f[:8]}.png")
                    img.save(p)
                    saved += 1
                    per[grp] += 1
                except Exception:
                    continue
        except Exception:
            continue
        if i % 200 == 0:
            print(f"  {i}/{len(files)}  ({saved} sprites)", flush=True)
    print(f"\n{saved} sprites -> {OUT}")
    for g, n in per.most_common(30):
        print(f"   {n:5d}  {g}")


if __name__ == "__main__":
    main()
