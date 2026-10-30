# Read hololive Dreams' SQLCipher-encrypted master DBs offline.
#
# Master files (listed by rpc.api.Master/Get: name -> hash, per-file seed, CDN url) are
# SQLCipher databases. The decryption key is the per-file SEED (from Master/Get field 4,
# hex-decoded to 32 ASCII bytes) used as SQLCipher's RAW 256-bit key:
#     PRAGMA key = "x'<seed as hex>'"
# (validated: opens the DB, hmac ok). No PBKDF2 passphrase, default SQLCipher 4 params.
# Each table is `<Name>(id TEXT, data BLOB)`; `data` is a protobuf record.
#
# Uses the game's shipped sqlcipher.dll via ctypes (stdlib sqlite3 has no cipher). Read-only.
#
# Usage: proxyenv/Scripts/python master_db.py <table.db> <seed_hex> [SELECT ...]
import os, sys, ctypes

# The game ships sqlcipher.dll inside its own install; the install path is machine-specific
# (Steam libraries live on different drives). Resolve it at load time rather than hardcoding:
#   1. the HOLODREAMS_DIR env var (points at the game root, ...\common\hololiveDreams), or
#      HOLODREAMS_SQLCIPHER pointing straight at the dll;
#   2. a list of known Steam library roots, then every Steam library of this PC (game_paths).
# The first existing candidate wins; if none exist we raise with the list we tried so the fix
# is obvious (set HOLODREAMS_DIR).
_DLL_SUBPATH = r"hololive-Dreams_Data\Plugins\x86_64\sqlcipher.dll"


def _sqlcipher_dll():
    direct = os.environ.get("HOLODREAMS_SQLCIPHER")
    if direct:
        return direct
    roots = []
    env_root = os.environ.get("HOLODREAMS_DIR")
    if env_root:
        roots.append(env_root)
    roots += [
        r"C:\Program Files (x86)\Steam\steamapps\common\hololiveDreams",
        r"D:\Steam\steamapps\common\hololiveDreams",
        r"D:\SteamLibrary\steamapps\common\hololiveDreams",
        r"E:\SteamLibrary\steamapps\common\hololiveDreams",
    ]
    tried = []
    for root in roots:
        cand = os.path.join(root, _DLL_SUBPATH)
        tried.append(cand)
        if os.path.exists(cand):
            return cand
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import game_paths  # noqa: PLC0415

        cand = os.path.join(game_paths.game_root(), _DLL_SUBPATH)
        tried.append(cand)
        if os.path.exists(cand):
            return cand
    except SystemExit:
        pass
    raise FileNotFoundError(
        "sqlcipher.dll not found. Set HOLODREAMS_DIR to the game root "
        "(the folder ...\\common\\hololiveDreams) or HOLODREAMS_SQLCIPHER to the dll. Tried:\n  "
        + "\n  ".join(tried))


SQLITE_ROW = 100

def _load():
    lib = ctypes.CDLL(_sqlcipher_dll())
    sig = {
        "sqlite3_open": ([ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
        "sqlite3_close": ([ctypes.c_void_p], ctypes.c_int),
        "sqlite3_exec": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        "sqlite3_prepare_v2": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_char_p)], ctypes.c_int),
        "sqlite3_step": ([ctypes.c_void_p], ctypes.c_int),
        "sqlite3_column_count": ([ctypes.c_void_p], ctypes.c_int),
        "sqlite3_column_name": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_char_p),
        "sqlite3_column_bytes": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
        "sqlite3_column_blob": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_void_p),
        "sqlite3_finalize": ([ctypes.c_void_p], ctypes.c_int),
    }
    for n, (a, r) in sig.items():
        f = getattr(lib, n); f.argtypes = a; f.restype = r
    return lib

class MasterDB:
    def __init__(self, path, seed_hex):
        self.lib = _load()
        self.db = ctypes.c_void_p()
        if self.lib.sqlite3_open(path.encode(), ctypes.byref(self.db)) != 0:
            raise RuntimeError("open failed")
        # raw 256-bit key = the seed's 32 bytes, given as hex
        self.lib.sqlite3_exec(self.db, b'PRAGMA key="x\'' + seed_hex.encode() + b'\'"', None, None, None)

    def query(self, sql):
        st = ctypes.c_void_p()
        if self.lib.sqlite3_prepare_v2(self.db, sql.encode(), -1, ctypes.byref(st), None) != 0:
            raise RuntimeError("prepare failed: " + sql)
        ncol = self.lib.sqlite3_column_count(st)
        names = [self.lib.sqlite3_column_name(st, i).decode() for i in range(ncol)]
        while self.lib.sqlite3_step(st) == SQLITE_ROW:
            row = {}
            for i in range(ncol):
                n = self.lib.sqlite3_column_bytes(st, i)
                p = self.lib.sqlite3_column_blob(st, i)
                row[names[i]] = ctypes.string_at(p, n) if p else b""
            yield row
        self.lib.sqlite3_finalize(st)

    def close(self):
        self.lib.sqlite3_close(self.db)
        # a private temp copy (`master_cache.open_table`) goes with the connection
        copy = getattr(self, "temp_copy", None)
        if copy:
            try:
                import os  # noqa: PLC0415
                os.remove(copy)
            except OSError:
                pass

# --- generic protobuf-record decoder (data BLOB) -------------------------------
def _varint(b, p):
    s = r = 0
    while p < len(b):
        x = b[p]; p += 1; r |= (x & 0x7f) << s
        if not x & 0x80:
            return r, p
        s += 7
    return r, p

def decode_record(b):
    """flat protobuf -> {field: [values]} (varint->int, len-delimited->bytes)."""
    p = 0; out = {}
    while p < len(b):
        tag, p = _varint(b, p); fn = tag >> 3; wt = tag & 7
        if wt == 0: v, p = _varint(b, p)
        elif wt == 2:
            ln, p = _varint(b, p); v = b[p:p + ln]; p += ln
        elif wt == 5: v = int.from_bytes(b[p:p + 4], "little"); p += 4
        elif wt == 1: v = int.from_bytes(b[p:p + 8], "little"); p += 8
        else: break
        out.setdefault(fn, []).append(v)
    return out

def main():
    import json
    path, seed = sys.argv[1], sys.argv[2]
    sql = sys.argv[3] if len(sys.argv) > 3 else "SELECT * FROM sqlite_master WHERE type='table'"
    db = MasterDB(path, seed)
    for row in db.query(sql):
        printable = {k: (v.decode("utf-8", "replace") if k != "data" else f"<{len(v)}B blob>") for k, v in row.items()}
        print(printable)
    db.close()

if __name__ == "__main__":
    main()
