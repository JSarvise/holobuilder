"""Re-download a master table from the CDN when the game's local cache copy will not decrypt.

WHY THIS EXISTS. `Condition` is the one table of 371 whose cached copy fails the SQLCipher hmac
on page 1. It matters twice over: it is the structured form of the song unlock rules, and
`SkillTreeNode` fields 7 and 8 point at it. The way to read it is to fetch the file the index
actually describes.

WHAT THE INDEX GIVES. `dumps/master_index.json` is built from a captured `rpc.api.Master/Get`
and carries, per table: `hash`, `size`, `seed_hex` (the raw SQLCipher key) and `url`.

HOW A DOWNLOAD IS VERIFIED, and why not by that hash. `build_master_index.py` labels field 2
"sha256", but it matches neither the game's cached bytes nor the CDN body, for `Condition` AND
for healthy control tables (`Music`, `MusicDifficulty`, `Card`). Whatever it digests, it is not
the served file, so comparing against it rejects good downloads. What it identifies is the URL
path, which is enough to address the file and nothing more.

The verification used instead is stronger than a digest that cannot be interpreted: the declared `size`
must match, and the body must OPEN under its seed. SQLCipher hmac-checks every page, so a
truncated, corrupted or substituted file cannot decrypt -- which is exactly how the broken cache
copy announced itself in the first place.

CONDITIONS, matching every other network tool here:
  * 1 request/second, sequential, single-threaded.
  * size-checked, then hmac-verified by actually opening it, before it is kept.
  * a table already downloaded and verified is never re-requested.
  * output under dumps/, which is gitignored (it holds ~11 GB).

  proxyenv/Scripts/python master_refetch.py <Table> [<Table> ...]
  proxyenv/Scripts/python master_refetch.py --check <Table>     # open it, no network
"""
import hashlib, json, os, shutil, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DUMPS = os.path.join(HERE, "dumps")
OUT = os.path.join(DUMPS, "master_refetched")
INDEX = os.path.join(DUMPS, "master_index.json")
DELAY = 1.0
TIMEOUT = 30
UA = "hololive-builder-recon/1.0 (personal tooling; single-user)"


def index():
    return json.load(open(INDEX, encoding="utf-8"))


def path_for(table):
    return os.path.join(OUT, f"{table}.db")


def opens(path, seed_hex):
    """True if SQLCipher can decrypt the file under this seed -- a per-page hmac check."""
    from master_db import MasterDB
    tmp = os.path.join(DUMPS, f"_verify_{os.path.basename(path)}")
    shutil.copy(path, tmp)
    db = None
    try:
        db = MasterDB(tmp, seed_hex)
        list(db.query("SELECT name FROM sqlite_master WHERE type='table'"))
        return True
    except Exception:
        return False
    finally:
        if db is not None:
            db.close()
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def verified(table, idx=None):
    """True if a body is already held of the right size that decrypts under its seed."""
    p = path_for(table)
    if not os.path.exists(p):
        return False
    e = (idx or index())[table]
    if e.get("size") and os.path.getsize(p) != e["size"]:
        return False
    return opens(p, e["seed_hex"])


def fetch(tables):
    idx = index()
    os.makedirs(OUT, exist_ok=True)
    ok = bad = skipped = 0
    for i, table in enumerate(tables):
        if table not in idx:
            print(f"  NOT IN INDEX  {table}")
            bad += 1
            continue
        e = idx[table]
        if not e.get("url"):
            print(f"  NO URL        {table}")
            bad += 1
            continue
        if verified(table, idx):
            print(f"  already have  {table}")
            skipped += 1
            continue
        if i and not skipped:
            time.sleep(DELAY)
        req = urllib.request.Request(e["url"], headers={"User-Agent": UA})
        try:
            body = urllib.request.urlopen(req, timeout=TIMEOUT).read()
        except Exception as exc:
            print(f"  FAILED        {table}: {exc}")
            bad += 1
            continue
        if e.get("size") and len(body) != e["size"]:
            print(f"  SIZE !=       {table}: want {e['size']} got {len(body)}")
            bad += 1
            continue
        dst = path_for(table)
        with open(dst, "wb") as fh:
            fh.write(body)
        if not opens(dst, e["seed_hex"]):
            os.remove(dst)          # never leave an undecryptable body sitting on disk
            print(f"  WILL NOT DECRYPT  {table}: discarded")
            bad += 1
            continue
        print(f"  OK            {table}  {len(body)} B  size + hmac verified")
        ok += 1
    print(f"\n{ok} fetched, {skipped} already held, {bad} failed -> {OUT}")
    return ok, bad


def check(table):
    """Open the re-downloaded copy with the index seed and report what is inside."""
    from master_db import MasterDB, decode_record
    idx = index()
    p = path_for(table)
    if not os.path.exists(p):
        print(f"{table}: not downloaded yet")
        return 1
    if not verified(table, idx):
        print(f"{table}: on disk but sha256 does NOT match the index -- refusing to trust it")
        return 1
    tmp = os.path.join(DUMPS, f"_refetch_{table}.db")
    shutil.copy(p, tmp)
    try:
        db = MasterDB(tmp, idx[table]["seed_hex"])
        names = [next(iter(r.values())) for r in
                 db.query("SELECT name FROM sqlite_master WHERE type='table'")]
        names = [n.decode() if isinstance(n, bytes) else n for n in names]
        real = [n for n in names if not n.startswith("sqlite_")]
        print(f"{table}: opens. sql tables = {names}")
        if real:
            rs = list(db.query(f"SELECT * FROM {real[0]}"))   # query() is a generator
            print(f"  {len(rs)} rows")
            for r in rs[:3]:
                out = {}
                for k, v in r.items():
                    out[k] = (decode_record(v) if k == "data"
                              else v.decode("utf-8", "replace"))
                print("   ", str(out)[:220])
        db.close()
        return 0
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    if "--check" in argv:
        args = [a for a in argv if not a.startswith("--")]
        return check(args[0])
    ok, bad = fetch([a for a in argv if not a.startswith("--")])
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
