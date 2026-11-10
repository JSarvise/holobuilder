"""Rebuild `dumps/master_index.json` from a LIVE `Master/Get` — **no credential, no account**.

The index maps every master table to its content hash, size, SQLCipher seed and CDN url. It must
come from the live manifest: every table the game ships under a new seed is unreadable with an
older index, and the card pipeline needs `Card`, `Costume` and every `Lang*_Eng` table.

**This needs no credential and touches no account.** `Master/Get` declares
`disabled_game_auth_token + disabled_master_version`, so it is unauthenticated — it is literally
what the real client calls at t+0.49s, *before* it logs in (`account_snapshot.py`'s
`fetch_master_version` already relies on exactly this). The response is the public master
manifest: for each table, its name, content hash, size, SQLCipher seed and CDN url. Nothing here
identifies the player.

Safety, inherited deliberately rather than re-derived:
  * The request goes through `account_snapshot.call` and so through its **read-only whitelist**.
    `/rpc.api.Master/Get` is on it. Do not add anything here.
  * It writes ONE file, `dumps/master_index.json`, and reads nothing of the player's.
  * `--dry-run` prints what would change without writing.

Usage:
    python master_index_live.py
    python master_index_live.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import zlib
from pathlib import Path

import atomic_io

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402
from hbuilder.errors import run_cli  # noqa: E402

sys.path.insert(0, str(paths.REFRESH_DIR))

import account_snapshot as snap  # noqa: E402
from master_db import _varint, decode_record  # noqa: E402

INDEX = paths.DUMPS / "master_index.json"


def _resync(pt: bytes) -> int:
    """Offset at which `pt` parses as a clean repeated length-delimited protobuf to the byte."""
    for start in range(0, 256):
        p, n = start, 0
        while p < len(pt):
            tag, p2 = _varint(pt, p)
            fn, wt = tag >> 3, tag & 7
            if wt != 2 or fn == 0 or fn > 2000:
                break
            ln, p3 = _varint(pt, p2)
            if ln <= 0 or p3 + ln > len(pt):
                break
            p, n = p3 + ln, n + 1
        else:
            if n >= 3:
                return start
    raise ValueError("could not resynchronise the Master/Get payload")


def parse(pt: bytes) -> dict:
    """The `Master/Get` payload -> {table: {hash, seed_hex, size, url}}. Each entry is field 2
    of the response: f1 name, f2 hash, f3 size, f4 seed (the SQLCipher raw key), f5 CDN url."""
    p = _resync(pt)
    out = {}
    while p < len(pt):
        tag, p = _varint(pt, p)
        ln, p = _varint(pt, p)
        blob, p = pt[p : p + ln], p + ln
        if (tag >> 3) != 2:
            continue
        r = decode_record(blob)
        if 1 not in r:
            continue
        name = r[1][0].decode("utf-8", "replace")
        out[name] = {
            "hash": r[2][0].decode() if 2 in r else None,
            "seed_hex": r[4][0].decode() if 4 in r else None,
            "size": r[3][0] if 3 in r else None,
            "url": r[5][0].decode() if 5 in r else None,
        }
    return out


def fetch() -> dict:
    """The master manifest, live and unauthenticated."""
    host, _credential = snap.load_credential("main")
    ident = snap.client_headers("main")
    response_headers, trailers, data = snap.call(host, "/rpc.api.Master/Get", snap.frame(b""), ident=ident)
    status = trailers.get("grpc-status", response_headers.get("grpc-status"))
    if status not in (None, "0"):
        raise RuntimeError(f"Master/Get rejected: grpc-status {status} {trailers.get('grpc-message', '')}")
    for message in snap.unframe(data):
        if not message:
            continue
        plaintext = snap.unseal(message)
        # The body may or may not be DEFLATEd depending on size: try the three window sizes.
        for wbits in (15, -15, 47):
            try:
                plaintext = zlib.decompress(plaintext, wbits)
                break
            except Exception:
                pass
        return parse(plaintext)
    raise RuntimeError("empty Master/Get response")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print what would change and write nothing")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    print(f"whitelist : {sorted(snap.ALLOWED)}")
    print("credential: NOT USED — Master/Get is unauthenticated")
    fresh = fetch()
    print(f"fetched   : {len(fresh)} master tables")

    old = json.loads(INDEX.read_text("utf8")) if INDEX.exists() else {}
    added = sorted(set(fresh) - set(old))
    changed = sorted(
        t
        for t in set(fresh) & set(old)
        if fresh[t].get("seed_hex") != old[t].get("seed_hex") or fresh[t].get("hash") != old[t].get("hash")
    )
    dropped = sorted(set(old) - set(fresh))
    print(f"vs on-disk: {len(added)} new, {len(changed)} changed, {len(dropped)} gone")
    for label, names in (("new", added), ("changed", changed), ("gone", dropped)):
        if names:
            print(f"   {label:8s}: {', '.join(names[:12])}" + (" ..." if len(names) > 12 else ""))

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0
    if not fresh:
        print("refusing to write an empty index", file=sys.stderr)
        return 1

    INDEX.parent.mkdir(exist_ok=True)
    atomic_io.write_text(INDEX, json.dumps(fresh, indent=1, ensure_ascii=False), "utf8")
    print(f"written   : {INDEX}")
    return 0


if __name__ == "__main__":
    run_cli(main)
