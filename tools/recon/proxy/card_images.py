# Recover card artwork from captures, and render thumbnails for the explorer.
#
# The Octo asset CDN serves card art as PLAIN UnityFS bundles -- no encryption, and the body's
# md5 matches the catalog's, so an asset lifted out of a capture is byte-identical to the one the
# game got. Nothing is downloaded here: --dump re-reads images that already travelled through
# the proxy, and --from-cache reads the game's own cache.
#
# This module never touches the CDN. It does not need to: the game keeps every asset it has ever
# downloaded in its own on-disk Octo cache, under <install>/octo/v1/, ~11 GB of it. Each cached
# file is the untouched bundle body, named by its md5 (verified: all 26 bundles lifted from the
# captures are byte-identical to their cache copies), and each bundle carries its own asset name
# in its AssetBundle container -- so the cache can be indexed by name with no catalog at all.
# That is what --from-cache does, and it covers far more cards than any single capture.
#
# A minority of cards are served with their UnityFS header (only) Octo-obfuscated; their container
# name is therefore unreadable, so --from-cache still has to identify them by md5 against the
# decrypted catalog. --render then recovers them byte-exactly via octo_crypto: the 256-byte header
# is XORed with a keystream derived from the asset's own name, so there is no key and nothing is
# reconstructed.
#
# (Fetching from the CDN by URL needs the PRODUCTION catalogue's tokens -- the catalogue shipped
# in the build points at the review environment. octo_fetch.py does that for assets the cache
# lacks.)
#
# Three modes. The environments are split: mitmproxy lives in proxyenv, UnityPy/Pillow in the
# main interpreter. --from-cache reads bundle names, so it runs in the main one alongside --render.
#
#   python card_images.py --from-cache [--cache-dir <path>]
#       -> dumps/card_assets/<asset name>.bundle   (copied out of the game's own cache)
#   proxyenv/Scripts/python card_images.py --dump <flows.mitm> [more.mitm ...]
#       -> dumps/card_assets/<asset name>.bundle
#   python card_images.py --render [--max-width 320] [--full] [--as-displayed [--screen WxH]]
#                                  [--only <name prefix>]
#       -> dumps/card_images/<asset name>.webp   (+ .png with --full)
#       -> dumps/card_art/img_card_full/2048x1152_as_displayed/<asset name>.png (--as-displayed)
#
# Everything written lands under dumps/, which is gitignored: extracted assets stay local.
import io, os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import game_paths  # noqa: E402  (portable install resolution)

DUMPS = os.path.join(HERE, "dumps")
ASSETS = os.path.join(DUMPS, "card_assets")
IMAGES = os.path.join(DUMPS, "card_images")
FAMILIES = ("img_card_full", "img_card_thumb", "img_card_thin", "img_card_vert",
            "img_card_live_thumb", "img_chr_mem_thumb", "img_cos_sd_thumb")

# The game's own asset cache. Card art bundles run ~2.5 MB; the cap skips the movie/voice blobs
# (the largest is ~200 MB) without ever opening them.
CACHE_DIR = game_paths.octo_cache()
MAX_BUNDLE = 40 * 1024 * 1024


def _catalog_by_md5():
    """octo_md5 -> asset name, from the decrypted Octo catalog(s).

    Two catalogs exist: the one shipped in the build (octocacheevai.dec) and the PRODUCTION
    revision the live client actually uses (octocacheevai_prod.dec). They agree on almost every
    asset, but a handful of cards (e.g. the summer banner) were re-authored in production and carry
    DIFFERENT md5s there -- so their on-disk cache copies are named by the production md5 and are
    invisible to the shipped catalog alone. Both are merged, production winning, so those cards are
    found. The prod .dec is produced by decrypting octo/pdb/5/<rev>/octocacheevai with the Octo
    key (MD5("vision-admin"), AES-128-CBC, [0x01][16B IV][ciphertext]) -- Crypto lives in proxyenv,
    so that decryption is a proxyenv step; this reader just consumes whatever .dec files exist."""
    sys.path.insert(0, HERE)
    from master_db import decode_record
    out = {}
    # shipped first, then production overrides it
    for fn in ("octocacheevai.dec", "octocacheevai_prod.dec"):
        path = os.path.join(DUMPS, fn)
        if not os.path.exists(path):
            continue
        top = decode_record(open(path, "rb").read())
        for blob in list(top.get(2, [])) + list(top.get(3, [])):
            rec = decode_record(blob)
            md5, name = rec.get(5, [None])[0], rec.get(2, [None])[0]
            if md5 and name:
                out[md5.decode()] = name.decode()
    return out


def dump(paths):
    """Lift every card-art asset body out of the given captures."""
    from mitmproxy import io as mio
    import hashlib
    by_md5 = _catalog_by_md5()
    os.makedirs(ASSETS, exist_ok=True)
    saved, skipped = 0, 0
    for path in paths:
        with open(path, "rb") as fh:
            for fl in mio.FlowReader(fh).stream():
                if fl.type != "http" or "asset." not in fl.request.pretty_host:
                    continue
                if not getattr(fl, "response", None) or not fl.response.content:
                    continue
                md5 = dict(fl.request.query).get("octo_md5")
                name = by_md5.get(md5)
                if not name or not name.startswith(FAMILIES):
                    continue
                body = fl.response.content
                if hashlib.md5(body).hexdigest() != md5:
                    skipped += 1          # truncated/partial response, never half-write it
                    continue
                dst = os.path.join(ASSETS, name + ".bundle")
                if not os.path.exists(dst):
                    open(dst, "wb").write(body)
                    saved += 1
    print(f"{saved} new asset bundles -> {ASSETS}"
          + (f"  ({skipped} skipped: md5 mismatch)" if skipped else ""))


def from_cache(cache_dir):
    """Copy card-art bundles out of the game's own on-disk Octo cache.

    The cache is flat and content-addressed (<token>/<md5>), so the only way to tell what a file
    is, is to open it: every bundle names itself in its AssetBundle container. Read-only on the
    install; the copies land in card_assets/ under the same naming --dump uses, so --render is
    unchanged either way."""
    import hashlib
    import warnings
    warnings.filterwarnings("ignore")
    import UnityPy
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"
    if not os.path.isdir(cache_dir):
        raise SystemExit(f"cache not found: {cache_dir}\n(pass --cache-dir <path>)")
    os.makedirs(ASSETS, exist_ok=True)

    # md5 -> catalog name, restricted to card-art families. This is the fallback identity for
    # bundles whose UnityFS container cannot be read (header-encrypted cards): the file is named by
    # its md5 on disk, and the catalog maps that md5 to the asset name.
    by_md5 = {m: n for m, n in _catalog_by_md5().items() if n.startswith(FAMILIES)}

    opened = saved = already = unreadable = enc = 0
    for root, _, files in os.walk(cache_dir):
        for fn in files:
            if fn == ".meta":
                continue
            src = os.path.join(root, fn)
            try:
                if os.path.getsize(src) > MAX_BUNDLE:
                    continue
                with open(src, "rb") as fh:
                    head = fh.read(8)
            except OSError:
                continue

            names = []
            if head == b"UnityFS\x00":
                opened += 1
                try:
                    names = [k for k in UnityPy.load(src).container if k.startswith(FAMILIES)]
                except Exception:
                    unreadable += 1    # a bad bundle must not abort an 11 GB walk
            # Header-encrypted card (no UnityFS magic, or magic but empty container): identify by
            # md5 against the catalog, and copy under the real name so --render can recover it.
            if not names and by_md5:
                digest = hashlib.md5(open(src, "rb").read()).hexdigest()
                if digest in by_md5:
                    names = [by_md5[digest]]
                    enc += 1
            for name in names:
                dst = os.path.join(ASSETS, name + ".bundle")
                if os.path.exists(dst):
                    already += 1
                else:
                    shutil.copyfile(src, dst)
                    saved += 1
    print(f"{saved} new card bundles -> {ASSETS}  "
          f"({already} already had, {opened} bundles inspected"
          + (f", {enc} header-encrypted matched by md5" if enc else "")
          + (f", {unreadable} unreadable" if unreadable else "") + ")")


def render(max_width, keep_full, only=None, out_dir=None, quality=82, lossless=False,
           as_displayed=False, screen=(2048, 1152)):
    """UnityFS bundle -> the largest Texture2D in it -> webp (and optional full png).

    `max_width=0` keeps the texture at its NATIVE resolution (no downscale — the
    right thing when the page rescales in CSS and a local server has no bandwidth
    cost). `lossless=True` writes lossless webp (the decoded texture byte-for-byte),
    `quality` sets the lossy level otherwise; `method=6` is the slowest/best encoder.

    `as_displayed=True` additionally writes, for the `img_card_full` family only, the texture the
    way the GAME shows it -- 16:9, sampled the way its GPU samples it -- at `screen` pixels.
    The default 2048x1152 keeps the full 2048 width of the texture 1:1 (no column lost), height
    brought to 16:9. It is NOT a size the game itself ever produces -- the game has no fixed
    output size, it draws the texture onto whatever screen it has (`--screen 1920x1080` gives
    the literal frame on a 1080p monitor, checked against in-game screenshots). Everything else
    is read off the game's own data:
      * geometry: `CardZoomScreen` (Addressables bundle 4f9a76df...) draws the art through
        FullscreenRoot > AutographedCardHolder > CardPlayer/LoadingImage, all anchored 0..1, on a
        canvas that scales to height 1080 (ScreenRoot 2328.55x1080). The RawImage has uvRect
        (0,0,1,1) and no aspect correction, so the 2048x1024 (2:1)
        texture is stretched onto the 16:9 screen. (CardDetailScreen shows the same texture at
        960x540; the zoom is the largest the game ever draws it.)
      * sampling: the texture carries m_FilterMode=1 (Bilinear, not Trilinear), m_Aniso=1, clamp,
        12 mips. At 1920x1080 the texel/pixel ratio is 2048/1920=1.067 x 1024/1080=0.948, LOD
        log2(1.067)=0.09 -> the GPU samples MIP 0, four texels per screen pixel, bilinear.
    That is reproduced exactly with Image.transform(AFFINE, BILINEAR): a pure 4-texel bilinear
    tap at each output pixel centre, which is what the GPU does -- NOT Image.resize, whose
    filters widen with the scale factor (antialiasing the GPU does not do). Screens narrower than
    ~1445 px would put the GPU on mip 1; only mip 0 is decoded here, so those are refused rather
    than approximated. Colour-space caveat: if the project renders in linear space the GPU
    interpolates in linear light; PIL interpolates the sRGB bytes. At a 1.07 ratio the difference
    is sub-LSB. Also note the 5-star cards play `mov_card_full` (USM) in this view; the still is
    what LoadingImage shows and what every non-movie surface uses.
    PNG only, no webp: nothing reads it but a human. Lands in
    dumps/card_art/img_card_full/<W>x<H>_as_displayed/, next to what --organize sorts.

    `out_dir` defaults to dumps/card_images/, which is what the explorer reads -- so it must
    contain ONLY trusted images. A bundle that fails therefore has any previous output
    DELETED rather than left behind: a stale file from an earlier run is indistinguishable from
    a fresh one, and a recovery bug that is later fixed would otherwise leave its corrupted
    output in place for ever. Use --out to render experiments somewhere else instead.

    There is only ONE path: decrypt the header from the asset name, load, read the texture."""
    import warnings
    warnings.filterwarnings("ignore")
    import UnityPy
    # Card bundles ship with the Unity version string stripped, exactly like StreamingAssets.
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"
    import octo_crypto
    images = out_dir or IMAGES
    os.makedirs(images, exist_ok=True)

    def discard(name):
        """Drop any earlier output for a bundle that can no longer be trusted."""
        gone = 0
        for ext in (".webp", ".png"):
            p = os.path.join(images, name + ext)
            if os.path.exists(p):
                os.remove(p)
                gone += 1
        return gone
    bundles = sorted(f for f in os.listdir(ASSETS) if f.endswith(".bundle")) \
        if os.path.isdir(ASSETS) else []

    if only:                                  # re-render one family without redoing the rest
        bundles = [f for f in bundles if f.startswith(only)]

    done, failed, encrypted, purged = 0, [], 0, 0
    for fname in bundles:
        name = fname[:-len(".bundle")]
        try:
            path = os.path.join(ASSETS, fname)
            raw = open(path, "rb").read()
            # The bundle's own asset name IS the filename here, and the name is the whole key:
            # octo_crypto reverses the 256-byte header XOR to the ORIGINAL bytes. Plaintext
            # bundles pass through untouched. Nothing is donated, cropped or reconstructed --
            # if a texture comes out at all, it is exactly what the game would decompress.
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
                purged += discard(name)
                continue
            if keep_full:
                best.save(os.path.join(images, name + ".png"))
            if as_displayed and name.startswith("img_card_full_"):
                w, h = screen
                import math
                lod = math.log2(max(best.width / w, best.height / h))
                if lod >= 0.5:
                    raise SystemExit(f"--screen {w}x{h}: the GPU would sample mip "
                                     f"{round(lod)}, only mip 0 is decoded here")
                disp = os.path.join(DUMPS, "card_art", "img_card_full",
                                    f"{w}x{h}_as_displayed")
                os.makedirs(disp, exist_ok=True)
                # output pixel (x,y) centre -> texel (x*sx, y*sy); PIL's affine path uses
                # pixel centres and a 4-tap bilinear filter, i.e. GPU bilinear at mip 0.
                # Per channel: PIL premultiplies alpha when transforming RGBA (1-LSB drift where
                # BC7 left alpha at 254); the GPU samples straight channels, so do the same.
                sx, sy = best.width / w, best.height / h
                from PIL import Image
                frame = Image.merge("RGBA", [ch.transform((w, h), 0, (sx, 0, 0, 0, sy, 0), 2)
                                              for ch in best.convert("RGBA").split()])
                # 0 = AFFINE, 2 = BILINEAR
                frame.save(os.path.join(disp, name + ".png"))
            thumb = best.copy()
            if max_width:                                     # 0 = keep native
                thumb.thumbnail((max_width, max_width * 4), 1)   # 1 = LANCZOS
            save_kw = {"lossless": True, "method": 6} if lossless \
                else {"quality": quality, "method": 6}
            thumb.convert("RGBA").save(os.path.join(images, name + ".webp"), **save_kw)
            done += 1
        except Exception as exc:
            failed.append((name, repr(exc)[:70]))
            purged += discard(name)
    total = sum(os.path.getsize(os.path.join(images, f)) for f in os.listdir(images))
    print(f"{done}/{len(bundles)} rendered -> {images}  ({total/1024:.0f} KB total)")
    if encrypted:
        print(f"  {encrypted} header-encrypted bundle(s) decrypted byte-exactly from their "
              f"asset name (see octo_crypto.py)")
    if purged:
        print(f"  {purged} stale output file(s) deleted (bundle no longer decodable)")
    if failed:
        print(f"  {len(failed)} failed: " + ", ".join(n for n, _ in failed[:4]))
    for name, why in failed:
        print(f"   FAILED {name}: {why}")


def organize(dest=None):
    """Sort the flat render output into <family>/<WxH>/ folders.

    The native PNGs are MOVED (they are the bulk, ~1.1 GB, and nothing else reads them); the
    320 px webp are COPIED, because dumps/card_images/ must stay a flat webp directory -- that
    is exactly what build_explorer.py globs, and it only ever looks at `.webp`.
    Re-runnable: render writes flat again, organize re-sorts."""
    from PIL import Image
    dest = dest or os.path.join(DUMPS, "card_art")
    if not os.path.isdir(IMAGES):
        raise SystemExit(f"nothing to organize: {IMAGES} does not exist")
    moved = copied = 0
    sizes = {}
    for fname in sorted(os.listdir(IMAGES)):
        stem, ext = os.path.splitext(fname)
        if ext not in (".png", ".webp"):
            continue
        fam = next((f for f in FAMILIES if stem.startswith(f + "_")), None)
        if fam is None:
            continue
        src = os.path.join(IMAGES, fname)
        with Image.open(src) as im:
            res = f"{im.size[0]}x{im.size[1]}"
        out = os.path.join(dest, fam, res)
        os.makedirs(out, exist_ok=True)
        dst = os.path.join(out, fname)
        if ext == ".png":
            shutil.move(src, dst)
            moved += 1
        else:
            shutil.copyfile(src, dst)
            copied += 1
        sizes.setdefault((fam, res, ext), 0)
        sizes[(fam, res, ext)] += 1
    print(f"{moved} PNG moved + {copied} webp copied -> {dest}")
    print(f"({IMAGES} keeps the flat .webp set the explorer reads)")
    for (fam, res, ext), n in sorted(sizes.items()):
        print(f"   {fam}/{res}/  {n:4d} {ext}")


def main():
    if "--from-cache" in sys.argv:
        cdir = sys.argv[sys.argv.index("--cache-dir") + 1] if "--cache-dir" in sys.argv \
            else CACHE_DIR
        from_cache(cdir)
    elif "--dump" in sys.argv:
        paths = [a for a in sys.argv[sys.argv.index("--dump") + 1:] if not a.startswith("-")]
        if not paths:
            raise SystemExit("usage: --dump <flows.mitm> [...]")
        dump(paths)
    elif "--organize" in sys.argv:
        i = sys.argv.index("--organize") + 1
        organize(sys.argv[i] if i < len(sys.argv) and not sys.argv[i].startswith("-") else None)
    elif "--render" in sys.argv:
        width = int(sys.argv[sys.argv.index("--max-width") + 1]) if "--max-width" in sys.argv \
            else 320
        only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
        out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
        quality = int(sys.argv[sys.argv.index("--quality") + 1]) if "--quality" in sys.argv \
            else 82
        screen = tuple(int(v) for v in sys.argv[sys.argv.index("--screen") + 1].lower()
                       .split("x")) if "--screen" in sys.argv else (2048, 1152)
        render(width, "--full" in sys.argv, only, out, quality=quality,
               lossless="--lossless" in sys.argv, as_displayed="--as-displayed" in sys.argv,
               screen=screen)
    else:
        raise SystemExit("usage: --from-cache [--cache-dir <path>] | --dump <flows.mitm> ... "
                         "| --render [--max-width N] [--full] [--as-displayed [--screen WxH]] [--only <prefix>]"
                         " [--out <dir>]"
                         " | --organize [<dir>]\n"
                         "  --out      renders somewhere other than dumps/card_images/, so "
                         "trial runs never touch the set the explorer reads\n"
                         "  --organize sorts the flat output into <family>/<WxH>/ under "
                         "dumps/card_art/ (PNGs moved, webp copied)")


if __name__ == "__main__":
    main()
