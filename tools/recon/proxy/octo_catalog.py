"""Decrypt the client's PRODUCTION Octo catalogue into `dumps/octocacheevai_prod.dec`.

**No network.** The live client keeps the production catalogue in its own install at
`octo/pdb/5/<revision>/octocacheevai` and refreshes it on launch, so bringing the local copy
up to date is a local file read plus one AES decrypt. It matters because
`chart_download.py` can only ask for assets the catalogue names: a stale
`dumps/octocacheevai_prod.dec` silently hides every song and chart shipped since.

The format:

    byte 0        0x01                      version marker
    bytes 1..17   16-byte AES IV
    bytes 17..    AES-128-CBC ciphertext, PKCS#7 padded
    key           MD5("vision-admin")       the Octo key, 16 bytes

⚠ Two catalogues exist and the difference is load-bearing. The build ships
`octocacheevai` pointing at the REVIEW environment, whose `{o}` tokens do not resolve
against production (0 of 44 captured paths). The one this tool reads is the production
revision the running client actually uses, and its tokens do work. The output is verified to carry a PRODUCTION URL
template before it is written, so a review catalogue cannot be saved under the production
name by mistake.

    proxyenv/Scripts/python octo_catalog.py            # decrypt if the client's is newer
    proxyenv/Scripts/python octo_catalog.py --status    # what is on disk, decrypt nothing
    proxyenv/Scripts/python octo_catalog.py --force     # decrypt even if the copy looks current
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DUMPS = os.path.join(HERE, "dumps")
OUT = os.path.join(DUMPS, "octocacheevai_prod.dec")

import game_paths  # noqa: E402  (portable install resolution)

try:
    PDB = game_paths.octo_pdb()
except SystemExit:
    # Keep import-time cheap and let callers hit the "install not here" path via
    # newest_revision(None); a hard exit on import would break `--help` too.
    PDB = os.path.join(
        os.path.expandvars(r"%ProgramFiles(x86)%\Steam\steamapps\common\hololiveDreams"),
        "octo", "pdb", "5",
    )

OCTO_KEY = hashlib.md5(b"vision-admin").digest()
_REVIEW_MARK = b"review-"
_PROD_MARK = b"https://asset.game-hololive-dreams.com/"


def newest_revision(pdb_dir: str = PDB) -> str | None:
    """The client's newest catalogue file, or None if the install is not here.

    Revisions are numeric directory names; the newest is the highest, not the
    most recently written -- a re-download of an older revision would otherwise
    look newer than the one in use.
    """
    if not os.path.isdir(pdb_dir):
        return None
    best: tuple[int, str] | None = None
    for name in os.listdir(pdb_dir):
        path = os.path.join(pdb_dir, name, "octocacheevai")
        if not os.path.isfile(path):
            continue
        try:
            rank = int(name)
        except ValueError:
            continue
        if best is None or rank > best[0]:
            best = (rank, path)
    return best[1] if best else None


def decrypt(blob: bytes) -> bytes:
    if not blob or blob[0] != 1:
        raise ValueError(
            f"unexpected version marker {blob[:1]!r} — expected 0x01; the catalogue "
            "format has changed and this decrypt must not be trusted"
        )
    from Crypto.Cipher import AES  # pycryptodome, recon-side only

    iv, body = blob[1:17], blob[17:]
    if len(body) % 16:
        raise ValueError(f"ciphertext is {len(body)} bytes, not a multiple of the block size")
    plain = AES.new(OCTO_KEY, AES.MODE_CBC, iv).decrypt(body)
    pad = plain[-1]
    if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
        plain = plain[:-pad]
    return plain


def chart_names(blob: bytes) -> set[str]:
    return {m.decode() for m in re.findall(rb"chart_m\d{4}_[a-z]+\.sus", blob)}


# Asset names in the catalogue, bucketed by family prefix. Deliberately NOT a
# fixed list of families: the failure this module exists to prevent is
# "the game shipped something nobody had written a tool for", and a hardcoded
# list can only ever report the families someone already thought of.
_ASSET_NAME = re.compile(rb"[a-z][a-z0-9_]{6,}(?:\.[a-z0-9]{1,4})?")
_FAMILY_TRIM = re.compile(r"[-_]?\d{3,}.*$")
_MIN_FAMILY_KEY = 6


def asset_families(blob: bytes) -> dict[str, set[str]]:
    """{family prefix: {full asset names}} for everything the catalogue names.

    The family key is the name with its first long digit-run and everything
    after it removed, so `img_music_jacket_m0323` and `chart_m0323_expert.sus`
    bucket as `img_music_jacket_m` and `chart_m`. Crude on purpose — it has to
    group names nobody has seen before, not to be pretty.

    Two filters keep the output readable: a name must contain an underscore and its family key must survive at
    `_MIN_FAMILY_KEY` characters. Without them the catalogue's md5 and token
    fields shred into ~23,000 one-letter "families" (`a`, `b`, `c` … ~1,500
    each) that drown the real ones.
    """
    families: dict[str, set[str]] = {}
    for raw in set(_ASSET_NAME.findall(blob)):
        name = raw.decode("ascii", "replace")
        if "_" not in name:
            continue
        key = _FAMILY_TRIM.sub("", name)
        if len(key) < _MIN_FAMILY_KEY or "_" not in key:
            continue
        families.setdefault(key, set()).add(name)
    return families


def family_delta(old: bytes, new: bytes) -> list[tuple[str, int, int]]:
    """[(family, gained, total)] for every family that GREW, biggest first.

    Only growth is reported. A family that shrank is either a re-authoring (the
    md5 moves, the name does not) or the bucketing regex being unstable, and neither
    is the signal this is for.
    """
    before, after = asset_families(old), asset_families(new)
    rows = []
    for key, names in after.items():
        gained = len(names - before.get(key, set()))
        if gained:
            rows.append((key, gained, len(names)))
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--status", action="store_true", help="report only, decrypt nothing")
    ap.add_argument("--force", action="store_true", help="decrypt even if ours looks current")
    ap.add_argument("--install", default=PDB, help="the client's octo/pdb/5 directory")
    args = ap.parse_args(argv)

    src = newest_revision(args.install)
    if src is None:
        print(f"no client catalogue under {args.install} — is the game installed here?")
        return 1

    src_size = os.path.getsize(src)
    have = os.path.exists(OUT)
    old = open(OUT, "rb").read() if have else b""
    print(f"client   {src}  ({src_size:,} bytes)")
    print(f"ours     {OUT}  ({len(old):,} bytes)" if have else f"ours     {OUT}  MISSING")
    if have:
        print(f"         {len(chart_names(old))} chart names")

    blob = open(src, "rb").read()
    new = decrypt(blob)

    # Refuse to write a REVIEW catalogue under the production name. Their `{o}`
    # tokens do not resolve, so the mistake would look like a working catalogue
    # and fail only at download time, one asset at a time.
    if _PROD_MARK not in new:
        marker = "review" if _REVIEW_MARK in new else "unrecognised"
        raise SystemExit(
            f"REFUSED: the decrypted catalogue carries a {marker} URL template, not the "
            f"production one ({_PROD_MARK.decode()}). Writing it would silently break "
            "every download."
        )

    fresh = chart_names(new)
    print(f"decrypted {len(new):,} bytes, {len(fresh)} chart names")
    if have:
        gained = sorted(fresh - chart_names(old))
        print(f"new chart names vs ours: {len(gained)}")
        for name in gained[:24]:
            print(f"   + {name}")

        # ⚑ The per-family diff, and it is the point of this step. Every other
        # check in this repo is per-family and only exists where somebody
        # already wrote a tool, so a release that ships e.g. jackets and audio
        # alongside charts would go unreported. This bucketing needs no tool and
        # no master table, so it names a family the first time it appears.
        rows = family_delta(old, new)
        if rows:
            print(f"\nfamilies that GREW ({len(rows)}):")
            for key, count, total in rows[:30]:
                print(f"   +{count:<5} {key:<40} now {total}")
            print(
                "   ^ a family with no downloader is still reported here. That is "
                "the whole point -- it is how the five songs would have been caught."
            )

    if args.status:
        print("--status: nothing written")
        return 0
    if have and new == old and not args.force:
        print("identical to what we already have — nothing written")
        return 0

    with open(OUT, "wb") as fh:
        fh.write(new)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
