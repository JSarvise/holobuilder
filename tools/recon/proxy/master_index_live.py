"""Rebuild `dumps/master_index.json` from a LIVE `Master/Get` — **no credential, no account**.

`build_master_index.py` reads the same thing out of a saved mitm capture. That works and it goes
stale: a capture pins the master version it was taken at, and every table the game has shipped
since then is in the local cache under a NEW seed the old index does not carry. With one old
capture, **305 of 371 tables still opened, 66 did not** — and the 66 included `Card`, `Costume`
and every `Lang*_Eng` the card pipeline needs, so the whole offline chain is blocked on an index
that is merely old.

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
    proxyenv/Scripts/python master_index_live.py
    proxyenv/Scripts/python master_index_live.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import atomic_io
import os
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import account_snapshot as snap  # noqa: E402
from build_master_index import parse  # noqa: E402

INDEX = HERE / "dumps" / "master_index.json"


def fetch() -> dict:
    """The master manifest, live and unauthenticated."""
    host, _credential = snap.load_credential("main")
    ident = snap.client_headers("main")
    response_headers, trailers, data = snap.call(
        host, "/rpc.api.Master/Get", snap.frame(b""), ident=ident
    )
    status = trailers.get("grpc-status", response_headers.get("grpc-status"))
    if status not in (None, "0"):
        raise RuntimeError(
            f"Master/Get rejected: grpc-status {status} {trailers.get('grpc-message', '')}"
        )
    for message in snap.unframe(data):
        if not message:
            continue
        plaintext = snap.unseal(message)
        # The same three window sizes `build_master_index.master_get_plaintext` tries: the body
        # may or may not be DEFLATEd depending on size.
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
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would change and write nothing")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    print(f"whitelist : {sorted(snap.ALLOWED)}")
    print("credential: NOT USED — Master/Get is unauthenticated")
    fresh = fetch()
    print(f"fetched   : {len(fresh)} master tables")

    old = json.loads(INDEX.read_text("utf8")) if INDEX.exists() else {}
    added = sorted(set(fresh) - set(old))
    changed = sorted(
        t for t in set(fresh) & set(old)
        if fresh[t].get("seed_hex") != old[t].get("seed_hex")
        or fresh[t].get("hash") != old[t].get("hash")
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
    sys.exit(main())
