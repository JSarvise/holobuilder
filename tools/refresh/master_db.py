# Read hololive Dreams' SQLCipher-encrypted master DBs offline.
#
# Master files (listed by rpc.api.Master/Get: name -> hash, per-file seed, CDN url) are
# SQLCipher databases. The decryption key is the per-file SEED (from Master/Get field 4,
# hex-decoded to 32 ASCII bytes) used as SQLCipher's RAW 256-bit key:
#     PRAGMA key = "x'<seed as hex>'"
# (validated: opens the DB, hmac ok). No PBKDF2 passphrase, default SQLCipher 4 params.
# Each table is `<Name>(id TEXT, data BLOB)`; `data` is a protobuf record.
#
# Loads SQLCipher via ctypes (stdlib sqlite3 has no cipher): the game's own sqlcipher.dll on
# Windows, the system's libsqlcipher on Linux (`paths.sqlcipher_library`). Read-only.
#
# Usage: python master_db.py <table.db> <seed_hex> [SELECT ...]
import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402

SQLITE_ROW = 100


def _load():
    lib = ctypes.CDLL(paths.sqlcipher_library())
    sig = {
        "sqlite3_open": ([ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
        "sqlite3_close": ([ctypes.c_void_p], ctypes.c_int),
        "sqlite3_exec": (
            [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p],
            ctypes.c_int,
        ),
        "sqlite3_prepare_v2": (
            [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_char_p),
            ],
            ctypes.c_int,
        ),
        "sqlite3_step": ([ctypes.c_void_p], ctypes.c_int),
        "sqlite3_column_count": ([ctypes.c_void_p], ctypes.c_int),
        "sqlite3_column_name": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_char_p),
        "sqlite3_column_bytes": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
        "sqlite3_column_blob": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_void_p),
        "sqlite3_finalize": ([ctypes.c_void_p], ctypes.c_int),
    }
    for n, (a, r) in sig.items():
        f = getattr(lib, n)
        f.argtypes = a
        f.restype = r
    return lib


class MasterDB:
    def __init__(self, path, seed_hex):
        self.lib = _load()
        self.db = ctypes.c_void_p()
        if self.lib.sqlite3_open(path.encode(), ctypes.byref(self.db)) != 0:
            raise RuntimeError("open failed")
        # raw 256-bit key = the seed's 32 bytes, given as hex
        self.lib.sqlite3_exec(self.db, b"PRAGMA key=\"x'" + seed_hex.encode() + b"'\"", None, None, None)

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
        x = b[p]
        p += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, p
        s += 7
    return r, p


def decode_record(b):
    """flat protobuf -> {field: [values]} (varint->int, len-delimited->bytes)."""
    p = 0
    out = {}
    while p < len(b):
        tag, p = _varint(b, p)
        fn = tag >> 3
        wt = tag & 7
        if wt == 0:
            v, p = _varint(b, p)
        elif wt == 2:
            ln, p = _varint(b, p)
            v = b[p : p + ln]
            p += ln
        elif wt == 5:
            v = int.from_bytes(b[p : p + 4], "little")
            p += 4
        elif wt == 1:
            v = int.from_bytes(b[p : p + 8], "little")
            p += 8
        else:
            break
        out.setdefault(fn, []).append(v)
    return out


def main():
    path, seed = sys.argv[1], sys.argv[2]
    sql = sys.argv[3] if len(sys.argv) > 3 else "SELECT * FROM sqlite_master WHERE type='table'"
    db = MasterDB(path, seed)
    for row in db.query(sql):
        printable = {
            k: (v.decode("utf-8", "replace") if k != "data" else f"<{len(v)}B blob>") for k, v in row.items()
        }
        print(printable)
    db.close()


if __name__ == "__main__":
    main()
