# Decrypt hololive Dreams' obfuscated IL2CPP global-metadata.dat.
#
# The game XOR-obfuscates global-metadata.dat with a REPEATING 128-byte key
# (found via index-of-coincidence: a clean peak at period 128). This script
# recovers that key from the ciphertext alone (no game runtime needed) and writes
# the plaintext metadata, which is then loadable by Il2CppDumper / a metadata parser.
#
# Key-recovery pipeline (fully self-contained, works even if the key changes on a
# game update, as long as the scheme stays repeating-XOR/128):
#   1. base key = per-byte MODE over 128-aligned blocks whose first 4 bytes == the
#      magic-derived key[0:4] (those are plaintext-zero regions -> block leaks key).
#   2. crib-drag: vote key bytes from known-ubiquitous plaintext ("k__BackingField",
#      "get_"/"set_"/"System." ...), iterating as more text becomes clean.
#   3. surgical fix: any residual corrupt "k__Backing??eld" pinpoints the exact bad
#      phase; correct it from the known crib byte.
#   4. refine_by_literal_text: per phase, the byte that makes the string-literal data and
#      identifier strings most identifier-shaped. Steps 1-3 alone can leave a dozen or so
#      phases wrong; with step 4 the result has 0 corrupt "k__BackingField" and a fully
#      monotone string-literal index table, which is a whole-file check.
#
# Usage: python decrypt_metadata.py [global-metadata.dat] [out.dat] [--keyout key.bin]
import sys, struct, collections, re
import game_keys

MAGIC = 0xFAB11BAF
L = 128
# Recovered key for the current build (fast path / cross-check); pipeline re-derives anyway.
KNOWN_KEY = game_keys.metadata_key() or b""  # game_keys.py; empty = re-derive

CRIB = b"k__BackingField"

def recover_key(d):
    kmagic = bytes(a ^ b for a, b in zip(d[:4], struct.pack('<I', MAGIC)))
    # 1) base: mode over zero-anchored blocks
    blocks = [d[i:i+L] for i in range(0, len(d)-L, L) if d[i:i+4] == kmagic]
    key = bytearray(L)
    for j in range(L):
        key[j] = collections.Counter(b[j] for b in blocks).most_common(1)[0][0]
    key[0:4] = kmagic
    # 2+3) crib-drag AND surgical-fix, looped together to convergence: the surgical
    # step corrects the exact phase pinned by a corrupt "k__Backing??eld", which
    # then lets more cribs match on the next crib-drag pass (bootstrap).
    cribs = [CRIB, b".ctor", b".cctor", b"get_", b"set_", b"add_", b"remove_",
             b"System.", b"UnityEngine.", b"Async", b"Encrypt", b"Decrypt",
             b"Exception", b"Attribute", b"<Module>", b"IEnumerator"]
    for _ in range(20):
        dec = bytes(d[i] ^ key[i % L] for i in range(len(d)))
        votes = [collections.Counter() for _ in range(L)]
        for crib in cribs:
            s = 0
            while True:
                p = dec.find(crib, s)
                if p < 0: break
                for j in range(len(crib)):
                    votes[(p+j) % L][d[p+j] ^ crib[j]] += 1
                s = p+1
        # surgical: corrupt "k__Backing??eld" pinpoints exact bad phases
        for m in re.finditer(rb'k__Backing(..)eld', dec):
            p = m.start()
            for j in range(15):
                if dec[p+j] != CRIB[j]:
                    votes[(p+j) % L][d[p+j] ^ CRIB[j]] += 5   # strong known-plaintext vote
        changed = 0
        for ph in range(4, L):
            if sum(votes[ph].values()) >= 4:
                v = votes[ph].most_common(1)[0][0]
                if v != key[ph]:
                    key[ph] = v; changed += 1
        key[0:4] = kmagic
        if changed == 0:
            break
    return bytes(key)

# --- Header de-obfuscation -------------------------------------------------
# On top of the XOR, the game restructures the metadata header: instead of stock
# (offset, size) pairs it stores TRIPLES (offset, size, count) -- an extra int32
# (the element COUNT) after every pair. Proof: offset[k]+size[k]==offset[k+1] for
# all 31 sections, the first section starts at byte 380 (== 2 + 31*3 int32 = the
# header size), and section 30 ends exactly at EOF. Stripping the count int from
# each triple and writing a stock 64-int32 v29/v31 header lets a standard parser
# read the header. (NOTE: this build ALSO uses a few customized struct layouts --
# images=36B not 40, typeDefinitions=82B not 92, genericParameters=14B not 16 --
# so stock Il2CppDumper still misaligns on those structs afterwards; the element-size
# table is the fingerprint.)
STOCK_FIELD_ORDER = 31  # sections from stringLiteral .. exportedTypeDefinitions

def deobfuscate_header(dec, version=29):
    """Given decrypted metadata with the triple-header, return a copy whose first
    256 bytes are a stock (offset,size)-pair header. Data sections are untouched."""
    H = list(struct.unpack_from('<128I', dec, 0))
    pairs = [(H[2+3*k], H[3+3*k]) for k in range(STOCK_FIELD_ORDER)]  # drop the count
    ints = [MAGIC, version]
    for off, sz in pairs:
        ints += [off, sz]
    out = bytearray(dec)
    out[0:256] = struct.pack('<64I', *ints)
    for i in range(256, pairs[0][0]):   # zero the leftover header padding
        out[i] = 0
    return bytes(out)

def section_table(dec):
    """Return [(name, offset, size, count, elem_size)] from the triple-header."""
    H = list(struct.unpack_from('<128I', dec, 0))
    names = ["stringLiteral","stringLiteralData","string","events","properties","methods",
        "parameterDefaultValues","fieldDefaultValues","fieldAndParamDefaultData","fieldMarshaledSizes",
        "parameters","fields","genericParameters","genericParameterConstraints","genericContainers",
        "nestedTypes","interfaces","vtableMethods","interfaceOffsets","typeDefinitions","images",
        "assemblies","fieldRefs","referencedAssemblies","attributeData","attributeDataRange",
        "unresolvedVCallParamTypes","unresolvedVCallParamRanges","windowsRTTypeNames","windowsRTStrings",
        "exportedTypeDefinitions"]
    out = []
    for k in range(STOCK_FIELD_ORDER):
        off, sz, cnt = H[2+3*k], H[3+3*k], H[4+3*k]
        out.append((names[k], off, sz, cnt, (sz//cnt if cnt else 0)))
    return out

def refine_by_literal_text(d, key):
    """Step 4: correct the key phases the cribs never pin, and keep the result only when the
    whole file decrypts better (`_whole_file_errors`). Two candidates are tried, each starting
    from the literal-index fix (`_fix_literal_index_top_bytes`):
      * the text vote over the string-literal data AND the identifier strings (section 2);
      * the text vote over the string-literal data only -- from metadata version 39 on,
        section 2 is 28 MB of binary and voting it for text breaks correct phases.
    Returns (key, [changed phases])."""
    before = bytes(key)
    best, best_err = before, _whole_file_errors(d, before)
    for with_identifiers in (True, False):
        candidate = _text_vote(d, _fix_literal_index_top_bytes(d, before), with_identifiers)
        err = _whole_file_errors(d, candidate)
        if err < best_err:
            best, best_err = candidate, err
    return best, [j for j in range(L) if best[j] != before[j]]


def _sections(d, key):
    """Header triples (offset, size, count) of sections 0..2, decrypted with `key`."""
    H = struct.unpack_from('<11I', bytes(d[i] ^ key[i % L] for i in range(0, 48)), 0)
    return (H[2], H[3], H[4]), (H[5], H[6], H[7]), (H[8], H[9], H[10])


def _fix_literal_index_top_bytes(d, key):
    """Section 0 is one uint32 offset into the literal data per literal; the data is far below
    16 MB, so every entry's top byte is zero in plaintext and leaks its key phase exactly."""
    key = bytearray(key)
    (lo, ls, ln), _, _ = _sections(d, key)
    if not (0 < lo < len(d) and 0 < ln and lo + 4 * ln <= len(d)):
        return bytes(key)
    votes = collections.defaultdict(collections.Counter)
    for e in range(ln):
        p = lo + 4 * e + 3
        votes[p % L][d[p]] += 1
    for phase, counts in votes.items():
        key[phase] = counts.most_common(1)[0][0]
    return bytes(key)


def _text_vote(d, key, with_identifiers):
    """Per key phase, the byte that makes the text sections most identifier-shaped. Those
    sections are ~95 % ASCII, so a wrong key byte shows as a collapsed column. A byte is
    changed only when it beats the current one by 2 %."""
    key = bytearray(key)
    _, (o1, n1, _), (o2, n2, _) = _sections(d, key)
    spans = [(o1, n1)] + ([(o2, n2)] if with_identifiers else [])
    spans = [(o, n) for o, n in spans if 0 < o < len(d) and 0 < n <= len(d) - o]
    if not spans:
        return bytes(key)
    # identifier-shaped text: lowercase most, then upper / digits / _ . / space; other printable
    # bytes a little (a printable-only score cannot tell a byte from its bit-0 twin, 0x26 / 0x27)
    weight = [0] * 256
    for c in range(0x20, 0x7f):
        weight[c] = 1
    for c in b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./ ":
        weight[c] = 3
    for c in b"abcdefghijklmnopqrstuvwxyz":
        weight[c] = 4
    printable = [c for c in range(256) if weight[c]]
    for j in range(L):
        hist = [0] * 256
        for off, size in spans:
            for c in d[off + ((j - off) % L):off + size:L]:
                hist[c] += 1
        score = lambda k: sum(hist[c ^ k] * weight[c] for c in printable)
        cur = score(key[j])
        best = max(range(256), key=score)
        if score(best) > cur * 1.02:
            key[j] = best
    return bytes(key)


def _whole_file_errors(d, key):
    """How wrong a key is over the whole file: a wrong magic is fatal; otherwise the corrupt
    k__BackingField count plus the literal-index entries that break its ascending order."""
    dec = bytes(d[i] ^ key[i % L] for i in range(len(d)))
    if struct.unpack_from('<I', dec, 0)[0] != MAGIC:
        return 10 ** 9
    corrupt = len(re.findall(rb'k__Backing(..)eld', dec)) - dec.count(b"k__BackingField")
    lo, _ls, ln = struct.unpack_from('<3I', dec, 8)
    disorder = 0
    if 0 < lo and 0 < ln and lo + 4 * ln <= len(dec):
        idx = struct.unpack_from(f'<{ln}I', dec, lo)
        disorder = sum(1 for i in range(1, ln) if idx[i] < idx[i - 1])
    return corrupt + disorder

def installed_metadata():
    """The installed client's global-metadata.dat (game_paths: HOLODREAMS_DIR, Steam libraries)."""
    import os  # noqa: PLC0415
    import game_paths  # noqa: PLC0415
    return os.path.join(game_paths.game_root(), "hololive-Dreams_Data", "il2cpp_data", "Metadata",
                        "global-metadata.dat")

def decrypt(d, known=KNOWN_KEY):
    """(plaintext, key, refined phases) for the bytes of an encrypted global-metadata.dat.
    Fast path: the known key when it still decrypts this build cleanly; otherwise re-derived
    from scratch (e.g. after a game update changed the key)."""
    def quality(k):
        dc = bytes(d[i] ^ k[i % L] for i in range(min(len(d), 0x1000000)))
        return struct.unpack_from('<I', dc, 0)[0] == MAGIC and \
            len(re.findall(rb'k__Backing(..)eld', dc)) - dc.count(b"k__BackingField") == 0
    if len(known) == L and quality(known):
        key = known
    else:
        key = recover_key(d)
    key, refined = refine_by_literal_text(d, key)
    return bytes(d[i] ^ key[i % L] for i in range(len(d))), key, refined

def main():
    src = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else installed_metadata()
    out = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else "global-metadata-decrypted.dat"
    keyout = None
    if "--keyout" in sys.argv:
        keyout = sys.argv[sys.argv.index("--keyout")+1]
    d = open(src, 'rb').read()
    dec, key, refined = decrypt(d)
    if refined:
        print(f"string-literal refine changed key phases {refined}")
    mg = struct.unpack_from('<I', dec, 0)[0]
    ver = struct.unpack_from('<I', dec, 4)[0]
    corrupt = len(re.findall(rb'k__Backing(..)eld', dec)) - dec.count(b"k__BackingField")
    print(f"key={key.hex()}")
    print(f"matches KNOWN_KEY: {key == KNOWN_KEY}")
    print(f"magic={hex(mg)} ({'OK' if mg == MAGIC else 'BAD'}) version={ver}")
    print(f"exact k__BackingField={dec.count(b'k__BackingField')} corrupt={corrupt}")
    open(out, 'wb').write(dec)
    print(f"wrote {out} ({len(dec)} bytes)")
    if keyout:
        open(keyout, 'wb').write(key)
        print(f"wrote key -> {keyout}")
    # also emit a header-deobfuscated copy (stock-order header) next to `out`
    if "--deobf-header" in sys.argv:
        ver = 29
        if "--metaver" in sys.argv:
            ver = int(sys.argv[sys.argv.index("--metaver")+1])
        stock = deobfuscate_header(dec, version=ver)
        so = out.rsplit('.', 1)[0] + f"-stockhdr-v{ver}.dat"
        open(so, 'wb').write(stock)
        print(f"wrote header-deobfuscated (v{ver}) -> {so}")
        print("section table (name off size count elemsz):")
        for n, o, s, c, e in section_table(dec):
            print(f"  {n:28} {o:>9} {s:>9} {c:>9} {e:>4}")

if __name__ == "__main__":
    main()
