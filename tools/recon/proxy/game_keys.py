"""The game client's build constants, which this repo does not carry.

They are not secrets of this project: they are the client's own (the payload AES seed and
key, and the global-metadata XOR key), and anyone with the game installed can recover them --
see docs/dev/game-keys.md. Looked up, in order:

  1. tools/recon/proxy/_game_keys.py   (the official build ships it; never in the public repo)
  2. HB_GAME_KEYS=<path to a JSON file>  {"grpc_seed": "...", "metadata_key_hex": "..."}
  3. state/game_keys.json               the same JSON, on this PC

A tool that needs a constant and finds none raises GameKeyMissing with that list.

    python tools/recon/proxy/game_keys.py            # what this PC has (never the values)
    python tools/recon/proxy/game_keys.py --check    # do they open the server's Master/Get?
    python tools/recon/proxy/game_keys.py --find     # find them in this PC's game install

`--find` reads the installed client's own global-metadata.dat (decrypt_metadata.py), takes its
string literals, and keeps the one literal whose MD5 opens a live, anonymous `Master/Get`
response into well-formed protobuf: the same check the client's own traffic would pass. It
needs no account and sends nothing but that one anonymous request, and it writes
state/game_keys.json without printing the values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
STATE_FILE = REPO / "state" / "game_keys.json"
HOSTS = {r: f"{r}.game-hololive-dreams.com" for r in ("us", "jp", "as")}
MAX_SEED_LEN = 128                     # literals longer than this are text, not a build seed


class GameKeyMissing(RuntimeError):
    pass


def _from_json() -> dict:
    for path in (os.environ.get("HB_GAME_KEYS"), STATE_FILE):
        if path and Path(path).is_file():
            return json.loads(Path(path).read_text("utf8"))
    return {}


def _private():
    try:
        import _game_keys  # noqa: PLC0415
        return _game_keys
    except ImportError:
        return None


def grpc_seed(required: bool = True) -> bytes | None:
    """The payload cipher's build seed: KEY = MD5(seed), IV = MD5(seed || nonce). Not
    `required`: None when this PC has none (the module still imports; its use raises)."""
    p = _private()
    if p is not None:
        return p.GRPC_SEED
    seed = _from_json().get("grpc_seed")
    if not seed:
        if not required:
            return None
        raise GameKeyMissing("the game's payload seed is not on this PC: run "
                             "`python tools/recon/proxy/game_keys.py --find` (refresh.cmd does it for "
                             "you), or put it in state/game_keys.json (docs/dev/game-keys.md)")
    return seed.encode()


def grpc_key_hex(required: bool = True) -> str | None:
    seed = grpc_seed(required)
    return hashlib.md5(seed).hexdigest() if seed is not None else None


def metadata_key() -> bytes | None:
    """The global-metadata XOR key of the current build, or None (decrypt_metadata re-derives it)."""
    p = _private()
    hx = p.METADATA_KEY_HEX if p is not None else _from_json().get("metadata_key_hex")
    return bytes.fromhex(hx) if hx else None


def source() -> str | None:
    """Where this PC's payload seed comes from, or None when it has none."""
    if _private() is not None:
        return "tools/recon/proxy/_game_keys.py"
    for path in (os.environ.get("HB_GAME_KEYS"), STATE_FILE):
        if path and Path(path).is_file():                  # the file _from_json reads
            return str(path) if _from_json().get("grpc_seed") else None
    return None


# ---------------------------------------------------------------- finding them
def seed_candidates(literals) -> list[bytes]:
    """The string literals that can be the payload seed: short printable ASCII, each once."""
    seen: set[bytes] = set()
    out = []
    for s in literals:
        if not (0 < len(s) <= MAX_SEED_LEN and s.isascii() and s.isprintable()):
            continue
        b = s.encode()
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def opens(message: bytes, seed: bytes) -> bool:
    """Whether `seed` opens one sealed `Master/Get` response message: valid padding, the whole
    body well-formed protobuf, and its first field the master version string."""
    import account_snapshot as A  # noqa: PLC0415
    from Crypto.Cipher import AES  # noqa: PLC0415

    if len(message) < 4:
        return False
    nlen = message[3]
    nonce, ct = message[4:4 + nlen], message[4 + nlen:]
    if not ct or len(ct) % 16:
        return False
    # the last block's padding first: it does not need the IV, and a wrong key fails it
    # 255 times in 256, so most candidates cost one block
    prev = ct[-32:-16] if len(ct) >= 32 else hashlib.md5(seed + nonce).digest()
    last = bytes(a ^ b for a, b in zip(AES.new(hashlib.md5(seed).digest(), AES.MODE_ECB)
                                       .decrypt(ct[-16:]), prev))
    if not (1 <= last[-1] <= 16 and last[-last[-1]:] == bytes([last[-1]]) * last[-1]):
        return False
    try:
        top = list(A.fields(A.unseal(message, seed)))
        version = top[0][2].decode("ascii") if top and top[0][:2] == (1, 2) else ""
    except Exception:  # noqa: BLE001 -- a wrong seed decrypts to anything
        return False
    return 0 < len(version) <= 64 and version.isprintable()


def master_get_messages(host: str) -> list[bytes]:
    """The sealed messages of one anonymous `Master/Get` (no account, no session)."""
    import account_snapshot as A  # noqa: PLC0415

    data = A.master_get(HOSTS.get(host, host), A.client_headers("main"))
    return [m for m in A.unframe(data) if m]


def search(literals, message: bytes) -> list[bytes]:
    """Every candidate literal that opens `message` (exactly one when the client is current)."""
    return [c for c in seed_candidates(literals) if opens(message, c)]


def find(host: str = "us", metadata: str | None = None, *, out: Path = STATE_FILE) -> int:
    """Find the constants in this PC's game install and write them to `out`. 0 when found."""
    import decrypt_metadata as dm  # noqa: PLC0415
    import metadata_parse as mp  # noqa: PLC0415

    path = metadata or dm.installed_metadata()
    print(f"reading the game client's metadata: {path}")
    dec, mkey, _ = dm.decrypt(Path(path).read_bytes())
    literals = mp.Metadata(data=dec).literals()
    print(f"  {len(literals)} string literals, {len(seed_candidates(literals))} short enough to be the seed")
    print(f"asking {HOSTS.get(host, host)} for one anonymous Master/Get (no account involved)")
    messages = master_get_messages(host)
    if not messages:
        print("the server's answer had no message to test against")
        return 1
    hits = search(literals, messages[0])
    if len(hits) != 1:
        print("no literal opens the server's answer" if not hits else
              f"{len(hits)} literals open the server's answer: refusing to guess")
        print("the installed client may be older than the server's: update the game, start it once, "
              "and run this again")
        return 1
    try:
        doc = json.loads(out.read_text("utf8"))
    except (OSError, ValueError):
        doc = {}
    doc.update({"grpc_seed": hits[0].decode(), "metadata_key_hex": mkey.hex()})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1) + "\n", "utf8")
    print("found the payload seed: it opens the server's own Master/Get (values not printed)")
    print(f"wrote {out}")
    return 0


def check(host: str = "us") -> bool | None:
    """Whether this PC's seed opens a live anonymous `Master/Get`: None when there is no seed
    or the server could not be asked (offline, maintenance)."""
    seed = grpc_seed(required=False)
    if seed is None:
        return None
    try:
        messages = master_get_messages(host)
    except Exception:  # noqa: BLE001 -- unknown is not wrong
        return None
    return bool(messages) and opens(messages[0], seed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The game client's build constants on this PC.")
    ap.add_argument("--find", action="store_true",
                    help="find them in this PC's game install and write state/game_keys.json")
    ap.add_argument("--check", action="store_true",
                    help="ask the game's server (one anonymous request) whether this PC's are current")
    ap.add_argument("--host", default="us", help="us, jp, as, or a host name (default: us)")
    ap.add_argument("--metadata", help="a global-metadata.dat other than the installed one")
    args = ap.parse_args(argv)
    if args.find:
        return find(args.host, args.metadata)
    src = source()
    print(f"payload seed: {'present (' + src + ')' if src else 'missing'}")
    print(f"metadata key: {'present' if metadata_key() else 'not stored (re-derived when needed)'}")
    if args.check:
        ok = check(args.host)
        print({True: "check: it opens the server's Master/Get",
               False: "check: it does NOT open the server's Master/Get: run with --find",
               None: "check: could not ask the server (offline, maintenance, or no seed)"}[ok])
        return 0 if ok else 1
    return 0 if src else 1


if __name__ == "__main__":
    sys.exit(main())
