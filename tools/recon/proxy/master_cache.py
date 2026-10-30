# Open ANY master table offline, straight from the game's own local cache.
#
# The game keeps every master file it has downloaded under
#   %LOCALAPPDATA%\..\LocalLow\QualiArts, Inc_\hololive-Dreams\EB0A191797624DD3A48FA681D3061212\
# named  <MD5(TableName).upper()>_<MD5(?)>  -- 240 of the 248 tables listed by rpc.api.Master/Get
# are present. The file IS the SQLCipher database (no extra wrapper), so it opens with the same
# per-file seed as a CDN-downloaded copy: PRAGMA key = "x'<seed hex>'" (see master_db.py).
#
# So no download and no live capture are needed to read a new table -- only its seed, which is
# already in dumps/master_index.json.
#
# Usage:  proxyenv/Scripts/python master_cache.py <Table> ["SELECT ..."]
#         proxyenv/Scripts/python master_cache.py --list
import hashlib, json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from master_db import MasterDB, decode_record  # noqa: E402

CACHE = os.path.expandvars(
    r"%LOCALAPPDATA%\..\LocalLow\QualiArts, Inc_\hololive-Dreams"
    r"\EB0A191797624DD3A48FA681D3061212")
INDEX = os.path.join(HERE, "dumps", "master_index.json")


def _index():
    return json.load(open(INDEX, encoding="utf-8"))


REFETCHED = os.path.join(HERE, "dumps", "master_refetched")


def _game_cache_path(table):
    key = hashlib.md5(table.encode()).hexdigest().upper()
    if os.environ.get("HB_NO_GAME", "") in ("1", "true", "yes") or not os.path.isdir(CACHE):
        return None                                # no game on this PC (game_paths.no_game)
    for name in os.listdir(CACHE):
        if name.split("_")[0] == key:
            return os.path.join(CACHE, name)
    return None


def _decrypts(path, seed_hex):
    """True if SQLCipher can decrypt this file under this seed (per-page hmac on page 1).

    The copy is what `open_table` would open anyway, so this costs one extra file copy per
    table per process -- see `_PICKED`, which remembers the verdict.

    A private copy per call: a shared temp name would race between the engine's workers
    (one overwriting the file another is decrypting), so a good copy would read as stale and
    be parked and re-downloaded mid-plan."""
    fd, tmp = tempfile.mkstemp(prefix="hbmaster_probe_", suffix=".db")
    os.close(fd)
    try:
        shutil.copy(path, tmp)          # a copy failure raises, as it always did
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
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


_PICKED = {}
# Tables whose chosen file has already been decrypt-tested in THIS process. The test costs a
# file copy and an open, so it is paid once per table, not once per `open_table`.
_VERIFIED = set()


def cached_path(table, index=None):
    """Local file for a master table, or None if there is no copy at all.

    A re-downloaded copy under dumps/master_refetched/ is preferred -- that is why the
    directory exists: `Condition`'s cached copy fails the SQLCipher hmac on page 1 while the
    CDN body opens with 1962 rows.

    ⚠ Preferred, NOT trusted. The directory only holds bodies that decrypted when written,
    but the SEEDS ROTATE with every new master version: a file that decrypted last month no
    longer decrypts under today's `master_index.json`. A stale body usually still has the
    index's exact `size`, so a size check does not see it, and picking it fails with
    `prepare failed: SELECT ... sqlite_master` while the game's own cache may hold a good,
    current copy.

    So each candidate is TRIED against the current seed and the first that decrypts wins."""
    index = index or _index()
    seed = (index.get(table) or {}).get("seed_hex")
    if table in _PICKED:
        if os.path.exists(_PICKED[table]):
            return _PICKED[table]
        # the remembered file is gone: another process parked it as `.stale` (below) after
        # this one picked it. Keeping the vanished path would make a long-lived web server
        # fail with FileNotFoundError until a restart. Pick again from what is on disk now.
        del _PICKED[table]
        _VERIFIED.discard(table)
    candidates = [os.path.join(REFETCHED, f"{table}.db"), _game_cache_path(table)]
    candidates = [c for c in candidates if c and os.path.exists(c)]
    if not candidates:
        return None
    pick = candidates[0]
    # One candidate: nothing to choose between, and probing it would make a bare
    # "do we have this table?" sweep over all 373 tables pay a copy+open each.
    if seed and len(candidates) > 1:
        for cand in candidates:
            if _decrypts(cand, seed):
                pick = cand
                _VERIFIED.add(table)
                break
        if pick is not candidates[0]:
            # The refetched body lost. Park it so the next process has ONE candidate and
            # never probes it again -- the probe is cheap but not silent: sqlcipher.dll
            # prints "hmac check failed for pgno=1" to stderr, which reads like the step
            # failing when it is only a stale file being passed over. Renamed, not
            # deleted: master_refetch.py rebuilds the directory, and a body that failed
            # under today's seed is still evidence of what the last version shipped.
            try:
                os.replace(candidates[0], candidates[0] + ".stale")
                print(f"  master_cache: {table}: refetched copy is stale for this master "
                      f"version -- parked as {os.path.basename(candidates[0])}.stale",
                      file=sys.stderr)
            except OSError:
                pass
    _PICKED[table] = pick
    return pick


# Set HB_MASTER_NO_FETCH=1 to forbid the self-heal below (offline runs, or when a run
# must provably touch no network).
def _refetch_allowed():
    return os.environ.get("HB_MASTER_NO_FETCH", "") not in ("1", "true", "yes")


def _self_heal(table, index):
    """Re-download `table` from the CDN when NO local copy decrypts under today's seed.

    The other half of the seed-rotation problem. `cached_path` picks whichever local copy
    still decrypts, which covers the usual case -- the game has already downloaded the new
    master, the refetched copy is the stale one. But `master_index.json` is refreshed from
    a LIVE `Master/Get`, while the game's own cache only moves when the GAME downloads it,
    so after a game-side master bump that the client has not pulled yet, BOTH copies can be
    stale at once and there is nothing local to fall back to. Without this, reads fail with
    `prepare failed: SELECT ... sqlite_master`, and the cards/songs/charts chain stops.

    The index carries the file's own URL, size and seed, so the fix is to fetch exactly the
    file it describes -- `master_refetch.fetch` already does it at 1 req/s, keeps the body
    only if the size matches AND it opens under the seed, and never re-requests a table it
    has already verified. Returns the healed path, or None."""
    if not _refetch_allowed():
        return None
    import master_refetch  # noqa: PLC0415 -- only on the failure path
    entry = index.get(table) or {}
    if not entry.get("url"):
        return None
    print(f"  master_cache: {table}: no local copy decrypts under the current seed "
          f"-- re-downloading the file the index names", file=sys.stderr)
    master_refetch.fetch([table])
    healed = master_refetch.path_for(table)
    # `verified`, not fetch's counter: a body already on disk is reported as "already have"
    # with ok == 0, and that copy is exactly the one wanted.
    if os.path.exists(healed) and master_refetch.verified(table, index):
        _PICKED[table] = healed
        _VERIFIED.add(table)
        return healed
    return None


def open_table(table, index=None):
    """MasterDB for `table`, read from a temp copy so the game's cache is never touched."""
    index = index or _index()
    src = cached_path(table, index)
    seed = (index.get(table) or {}).get("seed_hex")
    if src is None:
        src = _self_heal(table, index)
    elif seed and table not in _VERIFIED:
        if _decrypts(src, seed):
            _VERIFIED.add(table)
        else:
            src = _self_heal(table, index) or src
    if src is None:
        raise FileNotFoundError(f"{table} is not in the local master cache")
    if table not in index:
        raise KeyError(f"{table} has no seed in master_index.json")
    # one copy PER OPEN: a shared name races when several processes (web workers
    # planning boards) or two threads of one server read the same table at once --
    # one overwrites or deletes the copy another is reading ("prepare failed",
    # FileNotFoundError). Unique name, removed on close.
    fd, tmp = tempfile.mkstemp(prefix=f"hbmaster_{table}_", suffix=".db")
    os.close(fd)
    shutil.copy(src, tmp)
    db = MasterDB(tmp, index[table]["seed_hex"])
    try:
        db.temp_copy = tmp          # `MasterDB.close` removes it
    except AttributeError:
        pass
    return db


def sql_table(db, table):
    """The name of the SQL table INSIDE the file, which is not always the master table's name.

    All 124 `Lang*_Eng` files hold their rows in a table called plain `Lang`, so the obvious
    `SELECT * FROM LangMusic_Eng` fails with `prepare failed`. Resolve it from the file rather
    than assuming."""
    have = [next(iter(r.values())) for r in
            db.query("SELECT name FROM sqlite_master WHERE type='table'")]
    have = [n.decode() if isinstance(n, bytes) else n for n in have]
    if table in have:
        return table
    real = [n for n in have if not n.startswith("sqlite_")]
    if len(real) == 1:
        return real[0]
    raise KeyError(f"{table}: cannot pick a table among {have}")


def rows(table, sql=None, index=None):
    """Decoded rows: every non-`data` column as text, `data` as {field: [values]}."""
    db = open_table(table, index)
    if sql is None:
        sql = f"SELECT * FROM {sql_table(db, table)}"
    out = []
    for r in db.query(sql):
        rec = {}
        for k, v in r.items():
            if k == "data":
                rec["data"] = {f: [x.decode("utf-8", "replace") if isinstance(x, bytes) else x
                                   for x in vs] for f, vs in decode_record(v).items()}
            else:
                rec[k] = v.decode("utf-8", "replace")
        out.append(rec)
    db.close()
    return out


def main():
    index = _index()
    if "--list" in sys.argv:
        have = [t for t in sorted(index) if cached_path(t)]
        missing = [t for t in sorted(index) if not cached_path(t)]
        print(f"{len(have)}/{len(index)} master tables cached locally")
        print("missing:", missing)
        return
    table = sys.argv[1]
    sql = sys.argv[2] if len(sys.argv) > 2 else None
    for r in rows(table, sql, index):
        print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
