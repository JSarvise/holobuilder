# Octo header obfuscation for hololive Dreams assets -- decrypt and re-encrypt.
#
# WHAT IT IS. The first 256 bytes of an Octo asset are XORed with a keystream that is a pure
# function of the asset's own name. There is no key and no secret: the name is printed in the
# public catalog, so anyone holding the file already holds everything needed to reverse it.
# XOR is an involution, so the recovered bytes are the ORIGINAL bytes, exactly -- this is not a
# rebuild, nothing is donated, reconstructed or inferred.
#
# THE KEYSTREAM, for a name of n characters:
#   1. Lay out a 2n-byte buffer. Walking the name forwards, write each character's low byte at
#      even index 2*i; walking BACKWARDS from the end, write its bitwise complement at the
#      mirrored odd index 2n-1-2*i. (So the buffer holds the name interleaved with its own
#      reversed complement.)
#   2. Fold a one-byte checksum over that whole buffer: start at 0x7C, and per byte rotate the
#      accumulator right by one bit then XOR in the byte.
#   3. XOR every byte of the buffer by that checksum. That is the keystream.
#   4. XOR the first 256 bytes of the file against it, repeating the keystream as needed.
#
# TWO CONTAINER SHAPES.
#   bundles   -- plaintext starts with "UnityFS"; if it already does, the file is unencrypted
#                and is passed through untouched (many ship in the clear).
#   resources -- encrypted ones carry a literal "QUAVMAGIC" sentinel; the 256-byte window starts
#                AFTER the sentinel, and the sentinel is stripped on decrypt.
#
# PLATFORM NOTE. Verified on OUR files: the Steam/StandaloneWindows64 build. The algorithm was
# characterised from an Android-targeting third-party project, so anything of theirs that is
# platform-scoped (CDN endpoint, app version, the android_* Octo API keys) does NOT transfer and
# is deliberately not implemented here -- see tools/recon/HOLODORIDB-FINDINGS.md section 4/5.
# The header obfuscation itself is platform-independent, which the 790/790 run below establishes
# on Windows bundles directly rather than assuming.
#
# Stdlib only, by project constraint. Run this file directly to re-verify against real bundles.

BUNDLE_MAGIC = b"UnityFS"
RESOURCE_MAGIC = b"QUAVMAGIC"
WINDOW = 256                       # bytes of each file that are obfuscated
_SEED = 0x7C


def keystream(name: str) -> bytes:
    """The 2*len(name)-byte repeating keystream for an asset name. Pure function of the name."""
    n = len(name)
    buf = bytearray(2 * n)
    for i, ch in enumerate(name):
        byte = ord(ch) & 0xFF
        buf[2 * i] = byte                       # name, forwards, on even indices
        buf[2 * n - 1 - 2 * i] = byte ^ 0xFF    # its complement, backwards, on odd indices
    acc = _SEED
    for byte in buf:
        acc = ((acc >> 1) | ((acc & 1) << 7)) ^ byte    # rotate right 1, then XOR in
        acc &= 0xFF
    return bytes(b ^ acc for b in buf)


def _apply(data, name, start):
    """XOR WINDOW bytes of `data` beginning at `start`, in place on a bytearray."""
    ks = keystream(name)
    span = min(WINDOW, len(data) - start)
    for i in range(span):
        data[start + i] ^= ks[i % len(ks)]
    return data


def decrypt_bundle(data: bytes, name: str) -> bytes:
    """Recover an AssetBundle. Already-plaintext input is returned unchanged."""
    if data[:len(BUNDLE_MAGIC)] == BUNDLE_MAGIC:
        return data
    return bytes(_apply(bytearray(data), name, 0))


def encrypt_bundle(data: bytes, name: str) -> bytes:
    """Inverse of decrypt_bundle. Only meaningful on plaintext input."""
    if data[:len(BUNDLE_MAGIC)] != BUNDLE_MAGIC:
        return data
    return bytes(_apply(bytearray(data), name, 0))


def decrypt_resource(data: bytes, name: str) -> bytes:
    """Recover a resource, stripping the QUAVMAGIC sentinel. Unsentinelled input passes through."""
    if data[:len(RESOURCE_MAGIC)] != RESOURCE_MAGIC:
        return data
    out = _apply(bytearray(data), name, len(RESOURCE_MAGIC))
    return bytes(out[len(RESOURCE_MAGIC):])


def encrypt_resource(data: bytes, name: str) -> bytes:
    """Inverse of decrypt_resource: prepend the sentinel and obfuscate the window after it."""
    if data[:len(RESOURCE_MAGIC)] == RESOURCE_MAGIC:
        return data
    return bytes(_apply(bytearray(RESOURCE_MAGIC + data), name, len(RESOURCE_MAGIC)))


def decrypt(data: bytes, name: str) -> bytes:
    """Decrypt either container shape, chosen by sentinel."""
    if data[:len(RESOURCE_MAGIC)] == RESOURCE_MAGIC:
        return decrypt_resource(data, name)
    return decrypt_bundle(data, name)


# --------------------------------------------------------------------------------------------
# Self-verification. `python octo_crypto.py [dir]` re-runs the check that established this
# module against real files: every *.bundle in dumps/card_assets/ is stored under its own asset
# name, so the name is unambiguous and no catalog lookup (or guessing) is involved.
#
# Do NOT weaken this into a counter that only reports successes -- the lesson from the recovery
# work this replaces is to verify artefacts, not counters, so it loads each result through
# UnityPy and insists on a real Texture2D with real dimensions.
if __name__ == "__main__":
    import os, sys, io, glob, warnings, collections

    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "dumps", "card_assets")
    files = sorted(glob.glob(os.path.join(src, "*.bundle")))
    if not files:
        sys.exit(f"no *.bundle under {src}")

    import UnityPy
    from UnityPy.exceptions import UnityVersionFallbackWarning
    # Card bundles ship with the Unity version string stripped, so the fallback always fires.
    UnityPy.config.FALLBACK_UNITY_VERSION = "6000.3.15f1"
    warnings.simplefilter("ignore", UnityVersionFallbackWarning)

    stat, fams, bad = collections.Counter(), collections.Counter(), []
    for path in files:
        name = os.path.basename(path)[:-len(".bundle")]     # filename IS the asset name
        raw = open(path, "rb").read()
        was_plain = raw[:len(BUNDLE_MAGIC)] == BUNDLE_MAGIC
        out = decrypt_bundle(raw, name)
        if out[:len(BUNDLE_MAGIC)] != BUNDLE_MAGIC:
            bad.append((name, "no UnityFS magic after decrypt")); continue
        try:
            env = UnityPy.load(io.BytesIO(out))
            tex = [o.read() for o in env.objects if o.type.name == "Texture2D"]
        except Exception as exc:
            bad.append((name, f"{type(exc).__name__}: {exc}"[:70])); continue
        if not tex or not tex[0].m_Width:
            bad.append((name, "no usable Texture2D")); continue
        stat["plaintext" if was_plain else "encrypted"] += 1
        fams["_".join(name.split("_")[:3])] += 1

    total = sum(stat.values())
    print(f"{src}\n  bundles          {len(files)}")
    print(f"  already plaintext {stat['plaintext']}")
    print(f"  decrypted         {stat['encrypted']}")
    print(f"  FAILED            {len(bad)}")
    print("  families         ", dict(fams.most_common()))
    for n, why in bad[:10]:
        print(f"    FAIL {n[:52]:52} {why}")
    print(f"\n{total}/{len(files)} recovered" + (" -- OK" if not bad else " -- REGRESSION"))
    sys.exit(1 if bad else 0)
