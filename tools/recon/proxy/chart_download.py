# Fetch every chart (.sus) file (4 difficulties per song) from the Octo CDN, one per second, resumable.
#
# WHY THIS EXISTS. A chart only lands in the game's own cache once you clear that song and open
# its chart info, so filling the cache by playing is not practical: most songs are not owned and
# not clearable on the top difficulty, so only a handful are cached. The CDN is the only route.
#
# WHAT IT NEEDS, all already on disk -- nothing is guessed:
#   dumps/octocacheevai_prod.dec   the PRODUCTION catalog (the review one shipped in the build
#                                  has different tokens)
#     header field 4   URL template   https://asset.game-hololive-dreams.com/{o}
#     record field 2   asset name     chart_m0524_expert.sus
#     record field 5   octo md5       md5 of the SERVED body, and what each download is checked against
#     record field 7   {o} token      UVeXbL
#   The observed request shape is  GET /{token}?octo_md5={md5}.
#
# WHAT COMES BACK. A .sus is served as an Octo *resource*: 9-byte "QUAVMAGIC" sentinel, then the
# next 256 bytes XORed with the name-derived keystream (octo_crypto). So the served body is 9
# bytes longer than the chart and its md5 is NOT the chart's md5. Verification is therefore done
# on the body exactly as received, before touching it -- which is the right order anyway.
#
# This can be checked offline: re-encrypting a chart already on disk reproduces the catalog's
# md5 and size exactly, which confirms the production catalog is current and the container
# shape is right.
#
# CONDITIONS THIS TOOL ENFORCES, to keep the load on the CDN low and the output verifiable:
#   * 1 request/second, sequential, single-threaded. No concurrency knob exists.
#   * every response's md5 is checked against the catalog; a mismatch is discarded and reported.
#   * resumable: an output file that re-encrypts to the catalog md5 is never re-requested.
#   * chart_m* only. The asset filter is not a parameter.
#   * output stays under dumps/, which is gitignored (it holds ~11 GB).
#
# Stdlib only, so it runs under either interpreter.
#
#   python chart_download.py --preflight     fetch exactly ONE and verify it end to end
#   python chart_download.py --seed          copy what the game already cached (no network)
#   python chart_download.py                 fetch the rest, ~12 min at 1 req/s
#   python chart_download.py --status        what is on disk, no network
import hashlib, os, shutil, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from master_db import decode_record
from octo_crypto import decrypt_resource, encrypt_resource

CATALOG = os.path.join(HERE, "dumps", "octocacheevai_prod.dec")
OUT = os.path.join(HERE, "dumps", "charts_cdn")
try:
    import game_paths                     # the install wherever Steam put it (or HOLODREAMS_DIR)
    GAME_CACHE = game_paths.octo_cache()
except SystemExit:                        # not found, or HB_NO_GAME: the usual folder, if any
    GAME_CACHE = (r"C:\Program Files (x86)\Steam\steamapps\common"
                  r"\hololiveDreams\octo\v1")
PREFIX = "chart_m"           # not configurable, deliberately
# A decrypted chart starts with one of these. SUS format v2 (e.g. m0366) puts
# `#VISION_SUS_FORMAT_VERSION 2` before `#CREATE_APP_VERSION`; its body is the same SUS --
# sus_parse reproduces all eight of m0366's header note counts unchanged.
PLAINTEXT_MAGIC = (b"#CREATE_APP_", b"#VISION_SUS_FORMAT_VERSION")
INTERVAL = 1.0               # seconds between request starts
TIMEOUT = 30


class Asset:
    """One catalog record: everything needed to fetch and verify a single chart."""

    def __init__(self, name, md5, token, size):
        self.name, self.md5, self.token, self.size = name, md5, token, size

    @property
    def path(self):
        return os.path.join(OUT, self.name)

    def url(self, template):
        return template.replace("{o}", self.token) + "?octo_md5=" + self.md5

    def verify(self, served):
        """The served body must hash to the catalog md5 and decrypt to plaintext SUS.

        Returns the chart text bytes, or raises. Order matters: hash first, on the bytes exactly
        as received, before any transformation could hide a bad response."""
        got = hashlib.md5(served).hexdigest()
        if got != self.md5:
            raise ValueError(f"md5 {got} != catalog {self.md5} ({len(served)}B, want {self.size}B)")
        chart = decrypt_resource(served, self.name)
        if not chart.startswith(PLAINTEXT_MAGIC):
            raise ValueError(f"not a SUS chart after decrypt: {chart[:24]!r}")
        return chart

    def on_disk(self):
        """True if the stored chart re-encrypts to the catalog md5.

        Existence alone is not enough to skip a file -- a truncated or half-written chart would
        then be resumed over forever. Re-encrypting proves the bytes are the ones the CDN serves."""
        try:
            with open(self.path, "rb") as fh:
                chart = fh.read()
        except OSError:
            return False
        return hashlib.md5(encrypt_resource(chart, self.name)).hexdigest() == self.md5


def catalog():
    """Every chart_m* record in the production catalog, plus the URL template."""
    if not os.path.exists(CATALOG):
        sys.exit(f"missing {CATALOG} -- decrypt octo/pdb/5/<rev>/octocacheevai first")
    with open(CATALOG, "rb") as fh:
        top = decode_record(fh.read())
    template = top.get(4, [b""])[0].decode()
    if "{o}" not in template:
        sys.exit(f"catalog URL template looks wrong: {template!r}")
    assets = []
    for blob in list(top.get(2, [])) + list(top.get(3, [])):
        rec = decode_record(blob)
        name = rec.get(2, [b""])[0].decode()
        if not name.startswith(PREFIX):
            continue
        assets.append(Asset(name, rec.get(5, [b""])[0].decode(),
                            rec.get(7, [b""])[0].decode(), rec.get(3, [0])[0]))
    assets.sort(key=lambda a: a.name)
    return assets, template


def seed_from_game_cache(assets):
    """Take what the client already downloaded. No network, no CDN, nothing to rate-limit.

    Cache files are named by the SERVED md5 but hold the DECRYPTED body, so they are verified by
    re-encrypting, exactly as on_disk() does."""
    wanted = {a.md5: a for a in assets if not a.on_disk()}
    if not wanted or not os.path.isdir(GAME_CACHE):
        return 0
    taken = 0
    for root, _, files in os.walk(GAME_CACHE):
        for fn in files:
            asset = wanted.get(fn)
            if asset is None:
                continue
            src = os.path.join(root, fn)
            with open(src, "rb") as fh:
                body = fh.read()
            if body.startswith(PLAINTEXT_MAGIC):
                ok = hashlib.md5(encrypt_resource(body, asset.name)).hexdigest() == asset.md5
            else:
                try:
                    body, ok = asset.verify(body), True
                except ValueError:
                    ok = False
            if not ok:
                print(f"  cache copy of {asset.name} does not verify -- ignoring")
                continue
            _write(asset, body)
            taken += 1
    return taken


def _write(asset, chart):
    os.makedirs(OUT, exist_ok=True)
    tmp = asset.path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(chart)
    os.replace(tmp, asset.path)


def fetch(asset, template):
    req = urllib.request.Request(asset.url(template), headers={"User-Agent": "octo"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def download(assets, template, limit=None):
    """Sequential, one request per second, verifying every response. Resumable by construction."""
    todo = [a for a in assets if not a.on_disk()]
    if limit is not None:
        todo = todo[:limit]
    done = len(assets) - len([a for a in assets if not a.on_disk()])
    print(f"{done}/{len(assets)} already on disk; fetching {len(todo)} at {INTERVAL:g} req/s "
          f"(~{len(todo) * INTERVAL / 60:.0f} min)")
    if not todo:
        return 0, []

    failures, got, started = [], 0, time.monotonic()
    next_allowed = 0.0
    for i, asset in enumerate(todo, 1):
        wait = next_allowed - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        next_allowed = time.monotonic() + INTERVAL
        try:
            chart = asset.verify(fetch(asset, template))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            failures.append((asset.name, f"{type(exc).__name__}: {exc}"))
            print(f"  [{i}/{len(todo)}] FAIL {asset.name}: {exc}")
            if len(failures) >= 10 and got == 0:
                print("  10 failures and nothing recovered -- stopping rather than hammering")
                break
            continue
        _write(asset, chart)
        got += 1
        if i % 25 == 0 or i == len(todo):
            rate = (time.monotonic() - started) / i
            print(f"  [{i}/{len(todo)}] {asset.name}  ok={got} fail={len(failures)}  "
                  f"eta {(len(todo) - i) * rate / 60:.1f} min")
    return got, failures


def status(assets):
    have = [a for a in assets if a.on_disk()]
    print(f"{len(have)}/{len(assets)} charts verified in {OUT}")
    songs = {a.name.split("_")[1] for a in have}
    print(f"  {len(songs)}/{len({a.name.split('_')[1] for a in assets})} songs")
    missing = [a.name for a in assets if not a.on_disk()]
    if missing:
        print(f"  missing {len(missing)}, first few: {', '.join(missing[:5])}")


def main():
    assets, template = catalog()
    print(f"catalog: {len(assets)} {PREFIX}* assets, template {template}")

    if "--status" in sys.argv:
        return status(assets)

    if "--seed" in sys.argv:
        print(f"seeded {seed_from_game_cache(assets)} from the game's own cache")
        return status(assets)

    if "--preflight" in sys.argv:
        # Prove the production token works before asking for hundreds of them. One request.
        todo = [a for a in assets if not a.on_disk()]
        if not todo:
            print("nothing left to fetch -- preflight has nothing to prove")
            return
        got, failures = download(assets, template, limit=1)
        if failures:
            sys.exit("preflight FAILED -- do not run the full pull; the catalog may be stale")
        print("preflight OK: md5 verified and the file starts with a SUS header "
              f"({' or '.join(m.decode() for m in PLAINTEXT_MAGIC)})")
        return

    seeded = seed_from_game_cache(assets)
    if seeded:
        print(f"seeded {seeded} from the game's own cache (no network)")
    got, failures = download(assets, template)
    print(f"\nfetched {got}, failed {len(failures)}")
    for name, why in failures:
        print(f"  FAIL {name}: {why}")
    status(assets)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
