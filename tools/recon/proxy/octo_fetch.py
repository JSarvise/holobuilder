"""Fetch a named Octo asset the local cache does not have, straight from the CDN.

Everything else in this directory reads the game's own on-disk cache by default. This is the
deliberate exception, for assets the client has never had a reason to download: the
skill tree's green branch is gated behind player level 30, so its node icons are absent from
the cache of an account that has not reached it.

How the URL is known: the decrypted catalogue carries a template in its header (field 4) and a
per-asset token (record field 7). The PRODUCTION catalogue must be used -- the one shipped in
the build points at the review environment and its tokens do not resolve.

    https://asset.game-hololive-dreams.com/{token}

Every download is checked against the catalogue's own md5, so a wrong or truncated body is
caught rather than silently saved. One request per second, and nothing is re-fetched if the
file is already on disk. A bundle the game's own cache already holds (same md5, so the same
bytes) is copied from there and the CDN is not asked at all.

    python octo_fetch.py <asset name> [<asset name> ...]
    python octo_fetch.py --list-missing        # names skill_icons.py could not find locally

Output: dumps/octo_fetched/<asset name>.bundle  (raw, exactly as served)
"""
import hashlib, os, sys, time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DUMPS = os.path.join(HERE, "dumps")
OUT = os.path.join(DUMPS, "octo_fetched")
DELAY = 1.0                      # be a good citizen; these are a handful of small files
UA = "hololive-builder-recon/1.0 (personal tooling; single-user)"


def catalog():
    """(url template, {asset name: (token, md5)}) from the production catalogue."""
    sys.path.insert(0, HERE)
    from master_db import decode_record
    for fn in ("octocacheevai_prod.dec", "octocacheevai.dec"):
        path = os.path.join(DUMPS, fn)
        if not os.path.exists(path):
            continue
        top = decode_record(open(path, "rb").read())
        tmpl = top.get(4, [b""])[0].decode()
        if "review-" in tmpl and fn != "octocacheevai.dec":
            continue
        out = {}
        for blob in list(top.get(2, [])) + list(top.get(3, [])):
            rec = decode_record(blob)
            name = rec.get(2, [None])[0]
            token = rec.get(7, [None])[0]
            md5 = rec.get(5, [None])[0]
            if name and token:
                out[name.decode()] = (token.decode(), (md5 or b"").decode())
        if out:
            return tmpl, out, fn
    raise SystemExit("no decrypted catalogue in dumps/ (refresh_all's catalog step writes it)")


_LOCAL: dict | None = None


def local_cache_index() -> dict:
    """{md5: path} of the game's own Octo cache (<install>/octo/v1), whose files are named by
    the md5 of the very bytes the CDN serves. {} without an install (or with HB_NO_GAME).

    On a fresh checkout most of the bundles a refresh needs (card art, jackets, Live2D, emblems;
    958 of 1,028 in one test) are already in this cache -- otherwise an hour of
    1-request-per-second downloads for files on disk. Walked once per process: chibi_build
    calls fetch() once per outfit."""
    global _LOCAL
    if _LOCAL is not None:
        return _LOCAL
    _LOCAL = {}
    try:
        import game_paths
        cache = game_paths.octo_cache()
    except (SystemExit, OSError, ImportError):
        return {}
    for root, _dirs, files in os.walk(cache):
        for fn in files:
            if len(fn) == 32:
                _LOCAL[fn] = os.path.join(root, fn)
    return _LOCAL


def _from_local(index: dict, want_md5: str):
    """The cached body when its md5 really is `want_md5` (a partial file is skipped), else None."""
    path = index.get(want_md5) if want_md5 else None
    if path is None:
        return None
    try:
        body = open(path, "rb").read()
    except OSError:
        return None
    return body if hashlib.md5(body).hexdigest() == want_md5 else None


def fetch(names):
    tmpl, index, src = catalog()
    if "review-" in tmpl:
        print(f"WARNING: {src} points at the review environment; its tokens do not resolve in production")
    print(f"catalogue: {src}\ntemplate: {tmpl}\n")
    os.makedirs(OUT, exist_ok=True)
    ok = bad = skipped = local = 0
    fetched_any = False
    cached = None                # the game cache's index, built on the first file to get
    for name in names:
        dst = os.path.join(OUT, name + ".bundle")
        if os.path.exists(dst):
            print(f"  on disk   {name}")
            skipped += 1
            continue
        if name not in index:
            print(f"  NOT IN CAT {name}")
            bad += 1
            continue
        token, want_md5 = index[name]
        if cached is None:
            cached = local_cache_index()
        body = _from_local(cached, want_md5)
        if body is not None:
            # the same bytes the CDN would send (same md5): no request at all
            with open(dst, "wb") as fh:
                fh.write(body)
            print(f"  LOCAL     {name}  {len(body)} B  md5 verified (the game's cache)")
            ok += 1
            local += 1
            continue
        # one request per second between NETWORK requests (a file already on disk costs
        # nothing, and a skip must not stop the pausing for later requests)
        if fetched_any:
            time.sleep(DELAY)
        fetched_any = True
        url = tmpl.replace("{o}", token)
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        except requests.RequestException as exc:
            print(f"  FALLO     {name}: {exc}")
            bad += 1
            continue
        if r.status_code != 200:
            print(f"  HTTP {r.status_code}  {name}")
            bad += 1
            continue
        got = hashlib.md5(r.content).hexdigest()
        if want_md5 and got != want_md5:
            # the catalogue's own md5 is the only proof the body is the asset requested
            print(f"  MD5 !=    {name}  expected {want_md5} got {got}")
            bad += 1
            continue
        with open(dst, "wb") as fh:
            fh.write(r.content)
        print(f"  OK        {name}  {len(r.content)} B  md5 verified")
        ok += 1
    print(f"\n{ok} downloaded ({local} from the game's cache), {skipped} already here, "
          f"{bad} failed -> {OUT}")
    return ok


def missing_names():
    """What skill_icons.py asked for and could not find in the cache."""
    import json
    p = os.path.join(DUMPS, "skill_icons", "index.json")
    if not os.path.exists(p):
        raise SystemExit("ejecuta skill_icons.py primero")
    return json.load(open(p, encoding="utf-8"))["missing"]


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    names = missing_names() if "--list-missing" in args else [a for a in args if a[0] != "-"]
    if "--list-missing" in args and "--fetch" not in args:
        print("\n".join(names) or "(ninguno)")
        return
    fetch(names)


if __name__ == "__main__":
    main()
