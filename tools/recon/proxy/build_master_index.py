# Rebuild dumps/master_index.json (table -> hash, seed, url) from a captured rpc.api.Master/Get.
#
# The first cut of that index was made by eye and came out with 248 tables, several of them
# missing their first character. Parsing the response properly yields **371** — the 124 extra
# are the `Lang*_Eng` LOCALIZATION tables, which is where every `la-*` key resolves. Without
# them the localization looked absent from the game entirely (it is in no asset bundle, no Octo
# asset, and no string literal in the metadata).
#
# Offline caveat: the per-message nonce is not in the envelope, so the FIRST 16 plaintext bytes
# of any captured response are unrecoverable (see grpc_decrypt.py). The response is one repeated
# field, so the fix is to resynchronise: find the offset at which the remainder parses as a clean
# sequence of length-delimited fields that consumes the buffer exactly. Only the first entry or
# two are lost that way, and both are recovered from the local cache anyway.
#
# Entry: f1 name, f2 sha256, f3 size, f4 seed (the SQLCipher raw key), f5 CDN url.
#
# Usage: proxyenv/Scripts/python build_master_index.py <flows.mitm> [-o dumps/master_index.json]
import json, os, sys, types, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from master_db import decode_record, _varint  # noqa: E402

_src = open(os.path.join(HERE, "grpc_decrypt.py"), encoding="utf-8").read().split("def main(")[0]
_gd = types.ModuleType("grpc_decrypt_partial")
exec(compile(_src, "grpc_decrypt.py", "exec"), _gd.__dict__)


def _resync(pt):
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


def master_get_plaintext(mitm_path):
    from mitmproxy import io as mio
    key = bytes.fromhex(_gd.KEY_HEX)
    with open(mitm_path, "rb") as fh:
        for fl in mio.FlowReader(fh).stream():
            if fl.type != "http" or "Master/Get" not in fl.request.path:
                continue
            if not getattr(fl, "response", None) or not fl.response.content:
                continue
            env = _gd.parse_envelope(fl.response.content)
            if not env:
                continue
            pt = _gd.decrypt(env[1], key)
            for wbits in (15, -15, 47):
                try:
                    return zlib.decompress(pt, wbits)
                except Exception:
                    pass
            return pt
    raise SystemExit("no Master/Get response in that capture")


def parse(pt):
    p = _resync(pt)
    out = {}
    while p < len(pt):
        tag, p = _varint(pt, p)
        ln, p = _varint(pt, p)
        blob, p = pt[p:p + ln], p + ln
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


def main():
    mitm = sys.argv[1]
    dst = sys.argv[sys.argv.index("-o") + 1] if "-o" in sys.argv \
        else os.path.join(HERE, "dumps", "master_index.json")
    index = parse(master_get_plaintext(mitm))
    if os.path.exists(dst):
        old = json.load(open(dst, encoding="utf-8"))
        for name, entry in old.items():          # never lose a seed a previous capture had
            index.setdefault(name, entry)
    json.dump(index, open(dst, "w", encoding="utf-8"), indent=1, ensure_ascii=False,
              sort_keys=True)
    lang = [n for n in index if n.startswith("Lang")]
    print(f"{len(index)} master tables -> {dst}  ({len(lang)} localization tables)")


if __name__ == "__main__":
    main()
