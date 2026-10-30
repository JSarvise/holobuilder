"""Pull the skill-tree node icons out of the game's own Octo cache.

The board's node glyphs are NOT composed at runtime from generic UI sprites. They are
finished Octo assets, and `SkillTreeEffect.iconAssetIdSuffix` (the field's real name) is the
tell: an asset id is being built by string concatenation.

    img_skill_icon_tree_<effectType lowercased, underscores stripped><iconAssetIdSuffix>
    img_skill_icon_active_<liveActiveSkillId tail, underscores stripped>

  SKILL_TREE_EFFECT_TYPE_LIVE_REWARD_CARD_EXP_QUANTITY_UP_PERMIL_UP
      -> img_skill_icon_tree_liverewardcardexpquantityuppermilup
  LIVE_SCORE_BONUS_..._SINGER_TYPE  + "-solo"
      -> img_skill_icon_tree_livescorebonusaddpermilupbymusicskilltreecharacterandmusicsingertype-solo

Verified against the decrypted catalogue: every name built this way is in it.

Nothing is downloaded. The game keeps every asset it has fetched under <install>/octo/v1/,
named by md5, and each bundle carries its own name in its AssetBundle container -- the same
route card_images.py --from-cache uses. Header-obfuscated bundles are recovered byte-exactly
via octo_crypto (a name-derived XOR, no key).

    python skill_icons.py [--cache-dir <path>]
        -> dumps/skill_icons/<asset name>.png
        -> dumps/skill_icons/index.json     {effectType or skill id: asset name}
"""
import io, json, os, sys, warnings

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import game_paths  # noqa: E402  (portable install resolution)

DUMPS = os.path.join(HERE, "dumps")
OUT = os.path.join(DUMPS, "skill_icons")
# Either decrypted catalogue works (same format); the production one is what
# octo_catalog.py writes, the shipped one is the older name. Prefer
# whichever is on disk.
CATALOG = next(
    (p for p in (os.path.join(DUMPS, "octocacheevai.dec"),
                 os.path.join(DUMPS, "octocacheevai_prod.dec")) if os.path.exists(p)),
    os.path.join(DUMPS, "octocacheevai.dec"),
)
CACHE_DIR = game_paths.octo_cache()
PREFIXES = ("img_skill_icon_tree_", "img_skill_icon_active_", "img_skill_icon_passive_")
MAX_BUNDLE = 8 << 20


def asset_name(effect_type, suffix=""):
    """SkillTreeEffectType_SKILL_TREE_EFFECT_TYPE_LIFE_UP -> img_skill_icon_tree_lifeup"""
    core = effect_type.split("SKILL_TREE_EFFECT_TYPE_")[-1]
    return "img_skill_icon_tree_" + core.lower().replace("_", "") + (suffix or "")


def skill_asset_name(skill_id):
    """live_active_skill-skill_tree-life_recovery -> img_skill_icon_active_liferecovery"""
    return "img_skill_icon_active_" + skill_id.rsplit("-", 1)[-1].replace("_", "")


def wanted_names():
    """Every icon name the skill tree can ask for, from the master table."""
    sys.path.insert(0, HERE)
    import master_cache as mc
    names, index = set(), {}
    for r in mc.rows("SkillTreeEffect"):
        d = r["data"]
        et = d.get(2, [None])[0]
        if not et:
            continue
        # the local DB stores the enum as an int; an exported dump names it. Either way the
        # asset name comes from the *string* form, so fall back to the id when it is numeric.
        eid = d.get(1, [None])[0] or ""
        core = eid.split("skill_tree_effect-", 1)[-1]
        core = core.rsplit("-", 1)[0] if "-" in core else core
        import re
        core = re.sub(r"-\d+-.*$", "", eid.replace("skill_tree_effect-", ""))
        suffix = d.get(13, [None])[0] or ""
        n = "img_skill_icon_tree_" + core.lower().replace("_", "") + str(suffix)
        names.add(n)
        index[core + str(suffix)] = n
        sk = d.get(7, [None])[0]
        if sk:
            n2 = skill_asset_name(sk)
            names.add(n2)
            index[sk] = n2
    return names, index


def catalog_md5(names):
    """md5 -> asset name, restricted to the icons needed.

    Needed because these bundles are the header-obfuscated kind: their UnityFS magic is gone,
    so the container name cannot be read and the only identity left is the file's md5, which is
    what the cache names it by. Reuses card_images' catalogue reader, which merges the shipped
    and production revisions.
    """
    sys.path.insert(0, HERE)
    from card_images import _catalog_by_md5
    return {m: n for m, n in _catalog_by_md5().items() if n in names}


def main():
    cache = sys.argv[sys.argv.index("--cache-dir") + 1] if "--cache-dir" in sys.argv else CACHE_DIR
    import UnityPy
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"
    import octo_crypto

    names, index = wanted_names()
    print(f"{len(names)} icon names derived from the master")
    if not os.path.isdir(cache):
        raise SystemExit(f"no existe la cache: {cache}")
    os.makedirs(OUT, exist_ok=True)

    import hashlib
    by_md5 = catalog_md5(names)
    print(f"{len(by_md5)} of them found in the catalogue by md5")

    def extract(env, name):
        for obj in env.objects:
            if obj.type.name not in ("Sprite", "Texture2D"):
                continue
            try:
                img = obj.read().image
            except Exception:
                continue
            if img:
                img.save(os.path.join(OUT, name + ".png"))
                return True
        return False

    found, saved = {}, 0
    # Anything octo_fetch.py pulled from the CDN, for assets this account has never unlocked
    # (the green branch is gated behind player level 30, so its icons are absent from the
    # cache until then). Same handling as a cached file -- named, so obfuscation is no issue.
    fetched = os.path.join(DUMPS, "octo_fetched")
    for root, _, files in ([(fetched, [], os.listdir(fetched))] if os.path.isdir(fetched) else []):
        for fn in files:
            if not fn.endswith(".bundle"):
                continue
            name = fn[:-7]
            # octo_fetched also holds card art, jackets and Live2D models (other steps' downloads):
            # only skill icons belong here -- the rest is gigabytes of PNGs nothing here serves.
            if not name.startswith(PREFIXES):
                continue
            raw = open(os.path.join(root, fn), "rb").read()
            try:
                data = raw if raw[:8] == b"UnityFS\x00" else octo_crypto.decrypt_bundle(raw, name)
                env = UnityPy.load(io.BytesIO(data))
            except Exception as exc:
                print(f"  !! {name}: {exc}")
                continue
            found[name] = fn
            if extract(env, name):
                saved += 1

    for root, _, files in os.walk(cache):
        for fn in files:
            if fn == ".meta":
                continue
            src = os.path.join(root, fn)
            try:
                if os.path.getsize(src) > MAX_BUNDLE:
                    continue
                raw = open(src, "rb").read()
            except OSError:
                continue

            hits = []
            if raw[:8] == b"UnityFS\x00":
                try:
                    env = UnityPy.load(io.BytesIO(raw))
                    hits = [(k, env) for k in env.container if k.startswith(PREFIXES)]
                except Exception:
                    pass
            else:
                # Header-obfuscated: identify by md5, then undo the XOR with the asset's own
                # name as the key. Nothing is guessed and nothing is reconstructed.
                name = by_md5.get(hashlib.md5(raw).hexdigest())
                if name:
                    try:
                        env = UnityPy.load(io.BytesIO(octo_crypto.decrypt_bundle(raw, name)))
                        hits = [(name, env)]
                    except Exception as exc:
                        print(f"  !! {name}: {exc}")
            for name, env in hits:
                if name in found:
                    continue
                found[name] = src
                if extract(env, name):
                    saved += 1

    have = sorted(n for n in names if n in found)
    missing = sorted(names - set(found))
    json.dump({"index": index, "have": have, "missing": missing},
              open(os.path.join(OUT, "index.json"), "w", encoding="utf-8"), indent=1)
    print(f"{saved} PNG -> {OUT}")
    print(f"wanted {len(names)}: {len(have)} in the cache, {len(missing)} not cached")
    extra = sorted(set(found) - names)
    if extra:
        print(f"({len(extra)} more skill icons found on the way)")
    for m in missing:
        print("   FALTA", m)


if __name__ == "__main__":
    main()
