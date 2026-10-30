# Read hololive Dreams' DECRYPTED global-metadata.dat directly, without Il2CppDumper.
#
# Why not Il2CppDumper: this build customises four of the 31 metadata tables
# (see decrypt_metadata.py's header note), so a stock parser misaligns the moment
# it walks typeDefinitions. The tables we actually need for the score-model
# questions -- string literals, type names, method names -- are NOT among the
# customised ones except typeDefinitions, whose 82-byte layout is recovered here
# empirically rather than assumed.
#
# Input: the output of `decrypt_metadata.py` (plaintext, triple-header intact).
# This script reads the TRIPLE header (offset, size, count) natively, so it needs
# no `--deobf-header` copy.
#
# Usage:
#   python metadata_parse.py <decrypted.dat> literals [substring ...]
#   python metadata_parse.py <decrypted.dat> names    [substring ...]
#   python metadata_parse.py <decrypted.dat> layout
import struct
import sys

MAGIC = 0xFAB11BAF
SECTIONS = [
    "stringLiteral", "stringLiteralData", "string", "events", "properties",
    "methods", "parameterDefaultValues", "fieldDefaultValues",
    "fieldAndParamDefaultData", "fieldMarshaledSizes", "parameters", "fields",
    "genericParameters", "genericParameterConstraints", "genericContainers",
    "nestedTypes", "interfaces", "vtableMethods", "interfaceOffsets",
    "typeDefinitions", "images", "assemblies", "fieldRefs",
    "referencedAssemblies", "attributeData", "attributeDataRange",
    "unresolvedVCallParamTypes", "unresolvedVCallParamRanges",
    "windowsRTTypeNames", "windowsRTStrings", "exportedTypeDefinitions",
]


class Metadata:
    def __init__(self, path=None, *, data=None):
        """From a decrypted file's path, or its bytes (`data=`)."""
        self.d = data if data is not None else open(path, "rb").read()
        magic, self.version = struct.unpack_from("<II", self.d, 0)
        if magic != MAGIC:
            raise SystemExit(f"bad magic {magic:#x} -- is this the DECRYPTED file?")
        head = struct.unpack_from("<%dI" % (2 + 3 * len(SECTIONS)), self.d, 0)
        self.sec = {}
        for k, name in enumerate(SECTIONS):
            off, size, count = head[2 + 3 * k], head[3 + 3 * k], head[4 + 3 * k]
            self.sec[name] = (off, size, count)

    def elem_size(self, name):
        _off, size, count = self.sec[name]
        return size // count if count else 0

    # --- strings ---------------------------------------------------------
    def literals(self):
        """User string literals, in table order.

        `stringLiteral` is 4 bytes/entry here (stock IL2CPP is 8: length +
        dataIndex). The 4 bytes are the OFFSET into `stringLiteralData`; the
        length is the gap to the next offset. Verified by `layout`: the
        offsets are non-decreasing and the last one lands inside the data
        section."""
        off, _size, count = self.sec["stringLiteral"]
        doff, dsize, _dc = self.sec["stringLiteralData"]
        ents = struct.unpack_from("<%dI" % count, self.d, off)
        out = []
        for i, o in enumerate(ents):
            end = ents[i + 1] if i + 1 < count else dsize
            out.append(self.d[doff + o:doff + end].decode("utf-8", "replace"))
        return out

    def cstr(self, index):
        """A NUL-terminated name from the `string` section (type/method names)."""
        base = self.sec["string"][0] + index
        end = self.d.index(b"\0", base)
        return self.d[base:end].decode("utf-8", "replace")

    # --- methods ---------------------------------------------------------
    def method_names(self):
        """Every method name. `methods` is 32 bytes/entry (stock v29 layout);
        field 0 is the nameIndex into `string`."""
        off, _size, count = self.sec["methods"]
        step = self.elem_size("methods")
        out = []
        for i in range(count):
            (name_index,) = struct.unpack_from("<i", self.d, off + i * step)
            out.append(self.cstr(name_index) if name_index >= 0 else "")
        return out

    def type_names(self):
        """Every type's (namespace, name). `typeDefinitions` is 82 bytes here,
        not the stock 92 -- but the first two int32 are still nameIndex and
        namespaceIndex, which is all this needs. The 82-byte stride is read
        from the header, never hardcoded."""
        off, _size, count = self.sec["typeDefinitions"]
        step = self.elem_size("typeDefinitions")
        out = []
        for i in range(count):
            n, ns = struct.unpack_from("<ii", self.d, off + i * step)
            out.append((self.cstr(ns) if ns >= 0 else "", self.cstr(n) if n >= 0 else ""))
        return out


def _plausible(names):
    """Fraction of names that look like real identifiers -- the check that says
    whether a stride guess is right, instead of trusting that it parsed."""
    ok = sum(1 for s in names if s and all(c.isalnum() or c in "_.<>`|-=[],:" for c in s))
    return ok / max(1, len(names))


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    md = Metadata(sys.argv[1])
    mode = sys.argv[2]
    needles = [s.lower() for s in sys.argv[3:]]

    if mode == "layout":
        print(f"version={md.version}")
        print(f"{'section':30} {'offset':>10} {'size':>10} {'count':>9} {'elem':>5}")
        for name in SECTIONS:
            off, size, count = md.sec[name]
            print(f"{name:30} {off:>10} {size:>10} {count:>9} {md.elem_size(name):>5}")
        lits = md.literals()
        types = md.type_names()
        meths = md.method_names()
        print(f"\nliterals={len(lits)} plausible={_plausible(lits):.3f}")
        print(f"types={len(types)} plausible={_plausible([n for _, n in types]):.3f}")
        print(f"methods={len(meths)} plausible={_plausible(meths):.3f}")
        print("sample literals:", lits[:5])
        print("sample types:", types[:5])
        print("sample methods:", meths[:5])
        return

    if mode == "literals":
        rows = md.literals()
    elif mode == "names":
        rows = [f"{ns}.{n}" if ns else n for ns, n in md.type_names()]
    elif mode == "methods":
        rows = md.method_names()
    else:
        raise SystemExit(f"unknown mode {mode!r}")

    hits = 0
    for s in rows:
        if not needles or any(x in s.lower() for x in needles):
            print(s)
            hits += 1
    print(f"-- {hits} / {len(rows)} matched", file=sys.stderr)


if __name__ == "__main__":
    main()
