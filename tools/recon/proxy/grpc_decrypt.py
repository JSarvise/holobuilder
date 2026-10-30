# Decrypt hololive Dreams' gRPC "application/grpc+proto-enc" payloads.
#
# The API layer wraps each gRPC message in an Octo/quaunity envelope and encrypts the
# body with AES-128-CBC (PKCS7) under a CONSTANT build key. Scheme (reversed from
# quaunity-api DefaultMarshallerFactory.Decrypt):
#
#   envelope (after the 5-byte gRPC frame header):
#       [2B u16le nonceLen+2][1B compressed flag][1B nonceLen][nonce][ciphertext]
#   ciphertext = AES-128-CBC(plaintext, KEY, IV), PKCS7 padded, 16-aligned
#   SEED = a build constant recovered from the string literals (game_keys.py)
#   KEY  = MD5(SEED)
#   IV   = MD5(SEED || nonce)                -- the nonce IS in the envelope (the field
#          parse_envelope() returns as `token`). Nothing is lost. See decrypt_exact() below.
#   Compression is GZIP, gated on the flag byte [2] (DecompressBuffer uses GZipStream).
#
# Legacy, kept only so existing callers keep working: decrypt() below assumes a zero IV,
# which corrupts the first 16 plaintext bytes and forces resync workarounds in its callers.
# New code should call decrypt_exact()/iter_grpc_exact().
#
# Usage: proxyenv/Scripts/python grpc_decrypt.py <flows.mitm> [--key HEX] [--strings]
import hashlib, sys, zlib, re
from Crypto.Cipher import AES

import game_keys  # noqa: E402

KEY_HEX = game_keys.grpc_key_hex(required=False)   # None: looked up when used

def parse_envelope(body: bytes):
    """gRPC frame -> (token, ciphertext). Returns None if not a proto-enc envelope."""
    if len(body) < 6:
        return None
    msg_len = int.from_bytes(body[1:5], "big")   # gRPC length-prefixed frame
    msg = body[5:5 + msg_len]
    if len(msg) < 4:
        return None
    token_len = msg[3]
    token = msg[4:4 + token_len]
    ct = msg[4 + token_len:]
    if len(ct) < 16 or len(ct) % 16 != 0:
        return None
    return token, ct

def decrypt(ct: bytes, key: bytes, iv: bytes = b"\x00" * 16) -> bytes:
    """AES-128-CBC. With the default zero IV, the first 16 plaintext bytes are wrong but
    blocks 2+ are correct (CBC property). Strips PKCS7 padding if valid."""
    pt = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
    n = pt[-1]
    if 1 <= n <= 16 and all(b == n for b in pt[-n:]):
        pt = pt[:-n]
    # some payloads are DEFLATE-compressed before encryption
    for wbits in (-15, 15, 31):
        try:
            return zlib.decompress(pt[16:] if wbits == -15 else pt, wbits)
        except Exception:
            pass
    return pt


# ---------------------------------------------------------------------------
# EXACT decryption. Nothing is lost.
#
# From quaunity-api.Runtime!DefaultMarshallerFactory::CreateAesIV (RVA 0x3dac350):
#
#     MD5.TransformBlock(arg1, 0, arg1.Length)      ; arg1 = a BUILD CONSTANT, below
#     MD5.TransformFinalBlock(arg2, 4, arg2[3])     ; arg2 = the header -> the PER-MESSAGE NONCE
#
# So what parse_envelope() calls `token` is the nonce, and it travels in the clear in every
# message. The constant is recovered by MD5-preimage search over the IL2CPP string literals --
# MD5(SEED) is exactly the AES key, which is what makes the chain certain rather than
# plausible:
#
#     KEY = MD5(SEED)
#     IV  = MD5(SEED || nonce)
#
# Header layout, from DefaultMarshallerFactory::Header::.ctor (RVA 0x3dad170):
#     [0:2] u16le(nonce_len + 2)   [2] compressed flag   [3] nonce_len   [4:] nonce, then ct
# Requests build their nonce as BitConverter.GetBytes(DateTime.Now.Ticks) (8 bytes); responses
# carry a server-generated random ASCII nonce of 2-31 bytes in the same slot.
#
# Compression is GZIP gated on byte [2], not raw DEFLATE by trial and error (DecompressBuffer
# RVA 0x3dac4f0 uses GZipStream and reads the ISIZE trailer).
#
# Check: every captured message parses as protobuf from offset 0 with the buffer consumed
# exactly; a 1-bit-flipped IV control passes only about 1 % of them.
#
# The independent reimplementation yuvlian/symphytum-server (rpc/src/quali_crypt.rs) derives
# the same SEED, KEY, IV and header layout, but inflates with zlib. The compressed payloads
# seen on the wire (e.g. a /rpc.api.User/Get response) start `1f 8b 08 00`: gzip.
SEED = game_keys.grpc_seed(required=False)


def decrypt_exact(body: bytes, seed: bytes | None = SEED):
    """gRPC response/request body -> (nonce, full plaintext). No bytes lost.

    Returns None if the body is not a proto-enc envelope."""
    seed = seed if seed is not None else game_keys.grpc_seed()
    if len(body) < 6:
        return None
    msg = body[5:5 + int.from_bytes(body[1:5], "big")]
    if len(msg) < 4:
        return None
    nonce = msg[4:4 + msg[3]]
    ct = msg[4 + msg[3]:]
    if len(ct) < 16 or len(ct) % 16:
        return None
    key = hashlib.md5(seed).digest()
    iv = hashlib.md5(seed + nonce).digest()
    pt = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
    n = pt[-1]
    if 1 <= n <= 16 and all(b == n for b in pt[-n:]):
        pt = pt[:-n]
    if msg[2]:
        pt = zlib.decompress(pt, 31)
    return nonce, pt


def iter_grpc_exact(flow_path: str):
    """Like iter_grpc, but yields (path, nonce, FULL plaintext) -- no resync needed."""
    from mitmproxy import io as mio
    with open(flow_path, "rb") as f:
        for fl in mio.FlowReader(f).stream():
            if fl.type != "http":
                continue
            resp = getattr(fl, "response", None)
            if not resp or "grpc" not in resp.headers.get("content-type", ""):
                continue
            got = decrypt_exact(resp.raw_content or b"")
            if got:
                yield fl.request.path, got[0], got[1]


def iter_grpc(flow_path: str):
    from mitmproxy import io as mio
    with open(flow_path, "rb") as f:
        for fl in mio.FlowReader(f).stream():
            if fl.type != "http":
                continue
            resp = getattr(fl, "response", None)
            if not resp or "grpc" not in resp.headers.get("content-type", ""):
                continue
            env = parse_envelope(resp.raw_content or b"")
            if env:
                yield fl.request.path, env[0], env[1]

def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    path = sys.argv[1]
    key = bytes.fromhex(sys.argv[sys.argv.index("--key") + 1]) if "--key" in sys.argv else bytes.fromhex(KEY_HEX or game_keys.grpc_key_hex())
    want_strings = "--strings" in sys.argv
    for rpc, token, ct in iter_grpc(path):
        pt = decrypt(ct, key)
        printable = sum(1 for x in pt if 9 <= x <= 13 or 32 <= x <= 126) / max(1, len(pt))
        print(f"\n=== {rpc}  ctlen={len(ct)} ptlen={len(pt)} printable={printable:.2f} ===")
        if want_strings:
            for s in re.findall(rb"[\x20-\x7e]{4,}", pt)[:20]:
                print("   ", s.decode("latin1"))

if __name__ == "__main__":
    main()
