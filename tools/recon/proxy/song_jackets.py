"""Fetch and render the 177 song jacket images.

WHY THIS EXISTS. `tools/recon/SONGS.md` §4 established that every song has a jacket in the
production catalog under `img_music_jacket_<music id>` -- 177/177, no placeholders, no
exceptions -- and that none of them are on disk. They are ordinary Octo bundles: the same
record shape, the same name-derived 256-byte header XOR, the same fetch path that already
brought down the charts (708-file corpus then, 752 now). Nothing new is reverse engineered here.

WHY NOT `chart_download.py`. That tool hard-codes `PREFIX = "chart_m"` and says the filter is
deliberately not a parameter, so a chart downloader can never be pointed at anything else.
That invariant is worth keeping, so this is a sibling rather than a flag. The two differ in
substance anyway: a chart is served as an Octo *resource* (9-byte QUAVMAGIC sentinel), a jacket
as a *bundle* (UnityFS), so verification and decryption take different paths.

CONDITIONS, the same ones the chart pull was approved under and enforced here in code:
  * 1 request/second, sequential, single-threaded. No concurrency knob exists.
  * every response is md5-checked against the catalog before it is written.
  * resumable: a file already on disk is never re-requested.
  * `img_music_jacket_` only. The asset filter is not a parameter.
  * output stays under dumps/, which is gitignored (it holds ~11 GB).

The name list comes from the catalog, not from the master DB, so this runs under the main
interpreter (requests + UnityPy + Pillow) with no SQLCipher dependency.

  python song_jackets.py --status              what is on disk, no network
  python song_jackets.py --preflight           fetch exactly ONE and render it end to end
  python song_jackets.py --fetch               fetch the rest, ~3 min at 1 req/s
  python song_jackets.py --render [--max-width 320] [--full]
      -> dumps/song_jackets/<asset name>.webp  (+ .png with --full)
"""
import io, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DUMPS = os.path.join(HERE, "dumps")
BUNDLES = os.path.join(DUMPS, "octo_fetched")      # shared with octo_fetch.py
IMAGES = os.path.join(DUMPS, "song_jackets")
PREFIX = "img_music_jacket_"                        # not configurable, deliberately


def names():
    """Every jacket asset name in the production catalog, sorted."""
    import octo_fetch
    _tmpl, index, src = octo_fetch.catalog()
    out = sorted(n for n in index if n.startswith(PREFIX))
    if not out:
        raise SystemExit(f"no {PREFIX}* records in {src}")
    return out, src


def on_disk(name):
    return os.path.exists(os.path.join(BUNDLES, name + ".bundle"))


def status():
    have, src = names()
    got = [n for n in have if on_disk(n)]
    size = sum(os.path.getsize(os.path.join(BUNDLES, n + ".bundle")) for n in got)
    print(f"catalog: {src}")
    print(f"bundles: {len(got)}/{len(have)} on disk  ({size / 1e6:.1f} MB)")
    if os.path.isdir(IMAGES):
        imgs = [f for f in os.listdir(IMAGES) if f.endswith((".webp", ".png"))]
        print(f"images : {len(imgs)} in {IMAGES}")
    else:
        print(f"images : none ({IMAGES} does not exist)")
    return have, got


def fetch(limit=None):
    import octo_fetch
    have, _src = names()
    todo = [n for n in have if not on_disk(n)]
    if limit:
        todo = todo[:limit]
    if not todo:
        print("nothing to fetch; all jackets are already on disk")
        return 0
    print(f"fetching {len(todo)} of {len(have)} jackets at 1 req/s "
          f"(~{len(todo) // 60}m{len(todo) % 60}s)")
    return octo_fetch.fetch(todo)


def render(max_width=320, keep_full=False, only_missing=False):
    """Bundle -> largest Texture2D -> webp thumbnail (and optional full png).

    Same single path as card_images.render: decrypt the 256-byte header from the asset's own
    name, load, take the largest texture. Nothing is donated, cropped or reconstructed. A
    bundle that fails has any earlier output DELETED rather than left behind, so a stale image
    can never be mistaken for a fresh one.
    """
    import warnings
    warnings.filterwarnings("ignore")
    import UnityPy
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"
    import octo_crypto

    os.makedirs(IMAGES, exist_ok=True)
    have, _src = names()
    todo = [n for n in have if on_disk(n)]
    if only_missing:
        todo = [n for n in todo if not os.path.exists(os.path.join(IMAGES, n + ".webp"))]
        if not todo:
            print("nothing to render; every fetched jacket already has its image")
            return 0
    done, failed, encrypted, purged = 0, [], 0, 0
    for name in todo:
        try:
            raw = open(os.path.join(BUNDLES, name + ".bundle"), "rb").read()
            if raw[:7] != b"UnityFS":
                encrypted += 1
            env = UnityPy.load(io.BytesIO(octo_crypto.decrypt_bundle(raw, name)))
            best = None
            for obj in env.objects:
                if obj.type.name != "Texture2D":
                    continue
                img = obj.read().image
                if img and (best is None or img.width * img.height > best.width * best.height):
                    best = img
            if best is None:
                failed.append((name, "no Texture2D"))
                purged += _discard(name)
                continue
            if keep_full:
                best.save(os.path.join(IMAGES, name + ".png"))
            thumb = best.copy()
            thumb.thumbnail((max_width, max_width), 1)       # 1 = LANCZOS; jackets are square-ish
            thumb.convert("RGBA").save(os.path.join(IMAGES, name + ".webp"), quality=82)
            done += 1
        except Exception as exc:
            failed.append((name, repr(exc)[:70]))
            purged += _discard(name)
    total = sum(os.path.getsize(os.path.join(IMAGES, f)) for f in os.listdir(IMAGES))
    print(f"{done}/{len(todo)} rendered -> {IMAGES}  ({total / 1024:.0f} KB total)")
    if encrypted:
        print(f"  {encrypted} header-encrypted bundle(s) decrypted byte-exactly from their name")
    if purged:
        print(f"  {purged} stale output file(s) purged")
    for name, why in failed[:10]:
        print(f"  FAILED {name}: {why}")
    return len(failed)


def _discard(name):
    gone = 0
    for ext in (".webp", ".png"):
        p = os.path.join(IMAGES, name + ext)
        if os.path.exists(p):
            os.remove(p)
            gone += 1
    return gone


def main():
    argv = sys.argv[1:]
    if not argv or "--status" in argv:
        status()
        return 0
    if "--preflight" in argv:
        fetch(limit=1)
        return render(keep_full="--full" in argv)
    if "--sync" in argv:
        # fetch what the catalogue names and disk lacks, then render ONLY what is
        # not rendered yet -- the refresh chain's step, idempotent and cheap when
        # nothing is new.
        fetch()
        return render(only_missing=True)
    if "--fetch" in argv:
        fetch()
        return 0
    if "--render" in argv:
        mw = 320
        if "--max-width" in argv:
            mw = int(argv[argv.index("--max-width") + 1])
        return render(max_width=mw, keep_full="--full" in argv)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
