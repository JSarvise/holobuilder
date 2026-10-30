# Parse the decrypted `rpc.api.Card/GetParameters` gRPC response into structured card
# stat records. Pairs with grpc_decrypt.py (same constant AES key).
#
# Wire structure (verified: field 2 == field3+field4+field5 for every entry):
#   response = repeated Entry (top-level field 1, wire 2)
#   Entry    = { 1: id (string), 2: total (varint), 3/4/5: the three stats (varint) }
#   id       = "card-<num>-<rarityRank>-<rarityName>-<costume>-<xx>"
#             e.g. card-00002-3-nrml-0001-00  (rank 3 = nrml, 4 = cmmn, 5 = uniq)
#
# The three stats are hololive Dreams' Performance / Technique / Sense. FIELD ORDER is
# CONFIRMED against the in-game card screen (Aki Rosenthal 5-star: base 3765/4227/6157 ==
# in-game 3780/4242/6172 minus a flat +15/stat account bonus): stat3=Performance,
# stat4=Technique, stat5=Sense. These are BASE stats; in-game adds account-wide bonuses
# (green skill-tree nodes / Connect Bonus). This tool emits stat3/4/5 positionally (label
# them in the join step) and does NOT write data/cards.json
# (that file is hand-authored/authoritative per CLAUDE.md) -- it writes a separate review file.
#
# Usage: proxyenv/Scripts/python parse_card_params.py <flows.mitm> [-o out.json]
import sys, json
from Crypto.Cipher import AES

import game_keys  # noqa: E402
KEY = bytes.fromhex(game_keys.grpc_key_hex(required=False) or "00" * 16)   # placeholder: needs game_keys

def _varint(b, p):
    s = r = 0
    while p < len(b):
        x = b[p]; p += 1; r |= (x & 0x7f) << s
        if not x & 0x80:
            return r, p
        s += 7
    return r, p

def parse_message(b):
    """flat protobuf -> {field_num: [values]} (varint -> int, wire2 -> bytes)."""
    p = 0; out = {}
    while p < len(b):
        tag, p = _varint(b, p); fn = tag >> 3; wt = tag & 7
        if wt == 0:
            v, p = _varint(b, p)
        elif wt == 2:
            ln, p = _varint(b, p); v = b[p:p + ln]; p += ln
        elif wt == 5:
            v = int.from_bytes(b[p:p + 4], "little"); p += 4
        elif wt == 1:
            v = int.from_bytes(b[p:p + 8], "little"); p += 8
        else:
            break
        out.setdefault(fn, []).append(v)
    return out

def decrypt_responses(flow_path, rpc="Card/GetParameters"):
    from mitmproxy import io as mio
    outs = []
    with open(flow_path, "rb") as f:
        for fl in mio.FlowReader(f).stream():
            if fl.type != "http" or rpc not in fl.request.path:
                continue
            resp = getattr(fl, "response", None)
            if not resp or "grpc" not in resp.headers.get("content-type", ""):
                continue
            b = resp.raw_content or b""
            ln = int.from_bytes(b[1:5], "big"); m = b[5:5 + ln]
            tl = m[3]; ct = m[4 + tl:]
            # offline: zero IV -> first 16 plaintext bytes (a top-level wrapper, no card data) are lost
            outs.append(AES.new(KEY, AES.MODE_CBC, b"\x00" * 16).decrypt(ct))
    return outs

def parse_card_params(pt):
    """walk top-level repeated field 1; resync past the garbled first 16 bytes."""
    cards = {}; p = 0
    while p < len(pt):
        if pt[p] == 0x0a:
            ln, q = _varint(pt, p + 1)
            if 0 < ln < 500 and q + ln <= len(pt):
                f = parse_message(pt[q:q + ln])
                if 1 in f and isinstance(f[1][0], bytes) and f[1][0].startswith(b"card-"):
                    cid = f[1][0].decode()
                    parts = cid.split("-")   # card, num, rank, rarityname, costume, xx
                    rec = {
                        "id": cid,
                        "card_num": parts[1] if len(parts) > 1 else None,
                        "rarity": parts[3] if len(parts) > 3 else None,
                        "total": f.get(2, [None])[0],
                        "stat3": f.get(3, [None])[0],
                        "stat4": f.get(4, [None])[0],
                        "stat5": f.get(5, [None])[0],
                    }
                    s = [rec["stat3"], rec["stat4"], rec["stat5"]]
                    rec["total_matches_sum"] = all(x is not None for x in s) and rec["total"] == sum(s)
                    cards[cid] = rec   # dedup by id across duplicate responses
                    p = q + ln; continue
        p += 1
    return cards

def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    flow = sys.argv[1]
    out = sys.argv[sys.argv.index("-o") + 1] if "-o" in sys.argv else "tools/recon/proxy/dumps/card_params.json"
    merged = {}
    for pt in decrypt_responses(flow):
        merged.update(parse_card_params(pt))
    records = sorted(merged.values(), key=lambda r: r["id"])
    bad = [r["id"] for r in records if not r["total_matches_sum"]]
    with open(out, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    print(f"parsed {len(records)} unique cards -> {out}")
    print(f"total==sum check: {len(records)-len(bad)}/{len(records)} ok" + (f"; BAD: {bad}" if bad else ""))

if __name__ == "__main__":
    main()
