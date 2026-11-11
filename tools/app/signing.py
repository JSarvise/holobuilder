"""Release signatures: nothing the app runs or reads comes from the server unsigned.

The server is public, so it is not trusted: whoever took it over could
otherwise push "an update" or "game data" to every holobuilder. Ed25519 (pycryptodome, already
in the app):

  - the PRIVATE key lives only on the publisher PC: state/release_signing_key.pem (gitignored),
    made once with `python tools/app/signing.py --new-key`
  - the PUBLIC key is RELEASE_PUBKEY below, in the code the installer and the app ship
  - publish.py signs manifest.json (manifest.sig); build_app.py signs version.json's fields.
    content_store.sync and the installer refuse anything that does not verify.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

REPO = Path(__file__).resolve().parents[2]
PRIVATE = REPO / "state" / "release_signing_key.pem"
RELEASE_PUBKEY = "ac1cff4cda27563e4f478c95d0dd0b8905fda0814cc0c82a822f5bff22ed55f6"  # hex, set by --new-key


class BadSignature(Exception):
    pass


def canonical(doc: dict) -> bytes:
    """The bytes a JSON document is signed as: sorted keys, no whitespace, no 'sig' field."""
    return json.dumps(
        {k: v for k, v in doc.items() if k != "sig"}, sort_keys=True, separators=(",", ":")
    ).encode()


def sign(data: bytes, key_path: Path | None = None) -> str:
    key = ECC.import_key((key_path or PRIVATE).read_text("utf8"))
    return base64.b64encode(eddsa.new(key, "rfc8032").sign(data)).decode()


def verify(data: bytes, sig_b64: str, pubkey_hex: str | None = None) -> None:
    pub = pubkey_hex if pubkey_hex is not None else RELEASE_PUBKEY
    if not pub:
        raise BadSignature("no release key in this build")
    try:
        eddsa.new(eddsa.import_public_key(bytes.fromhex(pub)), "rfc8032").verify(
            data, base64.b64decode(sig_b64)
        )
    except (ValueError, TypeError) as exc:
        raise BadSignature("signature does not match the holobuilder release key") from exc


# the one line --new-key rewrites: whatever key it holds (empty, the official one, a self-hoster's)
_PUBKEY_LINE = re.compile(rb'^RELEASE_PUBKEY = "[0-9a-f]*"', re.M)


def new_key(private: Path | None = None, module: Path | None = None) -> str:
    """A new release key pair: the private half to `private` (state/, never in git; owner-only
    on Linux), the public half installed as `RELEASE_PUBKEY` in `module` (this file), which the
    installer and the app are built from. Refuses before writing anything when the private key
    exists or `module` has no single `RELEASE_PUBKEY` line to replace."""
    private = private or PRIVATE
    module = module or Path(__file__)
    if private.exists():
        raise SystemExit(f"{private} exists: a new key would orphan every installed app")
    source = module.read_bytes()
    if len(_PUBKEY_LINE.findall(source)) != 1:
        raise SystemExit(f"{module}: no single RELEASE_PUBKEY line to install the new public key in")
    key = ECC.generate(curve="ed25519")
    pub = key.public_key().export_key(format="raw").hex()
    private.parent.mkdir(exist_ok=True)
    fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf8") as fh:
        fh.write(key.export_key(format="PEM"))
    # bytes in and out: the file keeps its own line endings
    module.write_bytes(_PUBKEY_LINE.sub(f'RELEASE_PUBKEY = "{pub}"'.encode(), source, count=1))
    return pub


def verify_sums(sums_path: Path, files: list[Path]) -> list[str]:
    """Check SHA256SUMS against SHA256SUMS.sig beside it, then each named file's hash.
    Returns one line per file; raises BadSignature when the list itself is not ours."""
    import hashlib  # noqa: PLC0415

    data = sums_path.read_bytes()
    verify(data, sums_path.with_name(sums_path.name + ".sig").read_text("utf8").strip())
    want = dict(reversed(line.split("  ", 1)) for line in data.decode().splitlines() if line)
    out = []
    for f in files:
        got = hashlib.sha256(f.read_bytes()).hexdigest()
        ok = want.get(f.name) == got
        out.append(
            f"{'OK ' if ok else 'BAD'} {f.name}" + ("" if ok else " (not in the signed list or changed)")
        )
    return out


if __name__ == "__main__":
    if "--new-key" in sys.argv:
        print("public key", new_key())
    elif "--verify-sums" in sys.argv:
        # python tools/app/signing.py --verify-sums SHA256SUMS holobuilder-setup.exe
        rest = sys.argv[sys.argv.index("--verify-sums") + 1 :]
        lines = verify_sums(Path(rest[0]), [Path(p) for p in rest[1:]])
        print("signature OK (holobuilder release key)", *lines, sep="\n")
        sys.exit(0 if all(ln.startswith("OK") for ln in lines) else 1)
