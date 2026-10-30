"""Pull the game's ITEM/RESOURCE thumbnails out of the local Octo cache.

The Resources view serves `/itemicon/<item_id>` from `dumps/item_icons/` and
falls back to a coloured glyph on a miss (`server._serve_itemicon`). The icons
are finished Octo assets named `img_item_thumb_<id>` / `img_resource_thumb_<id>`;
unlike `skill_icons.py` there is no master-table derivation to do — the
catalogue itself is the complete list, so this pulls EVERY asset under those
prefixes rather than guessing which item ids a view might ask for.

Nothing is downloaded. Cached bundles are identified by md5 against the
decrypted catalogue and header-deobfuscated with the name-derived XOR
(`octo_crypto`), the same route `skill_icons.py` / `card_images.py` use;
anything `octo_fetch.py` pulled earlier into `dumps/octo_fetched/` is read too.

    python item_icons.py [--cache-dir <path>]
        -> dumps/item_icons/<asset name>.webp
        -> dumps/item_icons/index.json     {asset name: source}
"""
import hashlib
import io
import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import game_paths  # noqa: E402

DUMPS = os.path.join(HERE, "dumps")
OUT = os.path.join(DUMPS, "item_icons")
CACHE_DIR = game_paths.octo_cache()
PREFIXES = ("img_item_thumb_", "img_resource_thumb_")
MAX_BUNDLE = 8 << 20


def main() -> None:
    cache = sys.argv[sys.argv.index("--cache-dir") + 1] \
        if "--cache-dir" in sys.argv else CACHE_DIR
    import UnityPy
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"
    import octo_crypto
    from card_images import _catalog_by_md5

    by_md5 = {m: n for m, n in _catalog_by_md5().items() if n.startswith(PREFIXES)}
    wanted = set(by_md5.values())
    print(f"{len(wanted)} item/resource icons in the catalogue")
    if not os.path.isdir(cache):
        raise SystemExit(f"no existe la cache: {cache}")
    os.makedirs(OUT, exist_ok=True)

    def extract(env, name) -> bool:
        for obj in env.objects:
            if obj.type.name not in ("Sprite", "Texture2D"):
                continue
            try:
                img = obj.read().image
            except Exception:
                continue
            if img:
                img.save(os.path.join(OUT, name + ".webp"), lossless=True)
                return True
        return False

    found: dict[str, str] = {}
    saved = 0

    fetched = os.path.join(DUMPS, "octo_fetched")
    if os.path.isdir(fetched):
        for fn in os.listdir(fetched):
            if not fn.endswith(".bundle"):
                continue
            name = fn[:-7]
            if not name.startswith(PREFIXES) or name in found:
                continue
            raw = open(os.path.join(fetched, fn), "rb").read()
            try:
                data = raw if raw[:8] == b"UnityFS\x00" \
                    else octo_crypto.decrypt_bundle(raw, name)
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
                    hits = [(k, env) for k in env.container
                            if k.startswith(PREFIXES)]
                except Exception:
                    pass
            else:
                name = by_md5.get(hashlib.md5(raw).hexdigest())
                if name:
                    try:
                        env = UnityPy.load(
                            io.BytesIO(octo_crypto.decrypt_bundle(raw, name)))
                        hits = [(name, env)]
                    except Exception as exc:
                        print(f"  !! {name}: {exc}")
            for name, env in hits:
                if name in found:
                    continue
                found[name] = src
                if extract(env, name):
                    saved += 1

    # `item_id -> asset name` straight from master `Item` field 8 (for example
    # `item-selectable_reward_box-item-card_limit_break-attribute_1-10` carries
    # `selectable-limit-break-attribute-box-01-01` there) — the game's own
    # mapping, for the ids whose asset name is NOT the `item-`->`img_item_thumb_`
    # convention. The server tries this map first, then the convention.
    by_item: dict[str, str] = {}
    try:
        import master_cache as mc
        for r in mc.rows("Item"):
            rid = (r.get("data") or {}).get(1, [None])[0]
            suffix = (r.get("data") or {}).get(8, [None])[0]
            if rid and suffix:
                by_item[rid] = "img_item_thumb_" + str(suffix)
    except Exception as exc:  # noqa: BLE001 — the icons still serve by convention
        print(f"  !! master Item map unavailable: {exc}")

    missing = sorted(wanted - set(found))
    json.dump({"have": sorted(found), "missing": missing, "by_item": by_item},
              open(os.path.join(OUT, "index.json"), "w", encoding="utf-8"),
              indent=1)
    print(f"{saved} webp -> {OUT}")
    print(f"catalogue {len(wanted)}: {len(found)} found, {len(missing)} not in the game's cache")
    for m in missing:
        print("   FALTA", m)


if __name__ == "__main__":
    main()
