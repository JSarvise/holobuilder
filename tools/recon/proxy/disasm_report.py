"""Annotated x86-64 listing for a dumped IL2CPP method body.

The technique: disassemble a 4 KB body, take every direct `call` target, and look the
target RVA up in `dumps/method_map.tsv` to get `Image!Class::Method` (how the gRPC AES
scheme was worked out). This is that technique as a tool.

What it adds on top of `il2cpp_rva.py --disasm` (which prints raw capstone):

  * every direct `call`/`jmp` rel32 target resolved through `method_map.tsv`;
    exact hit -> `Image!Class::Method`, otherwise nearest preceding entry `+0xNN`
  * RIP-relative operands resolved to an absolute RVA and flagged as a data
    reference -- with the value decoded if a constant pool is available
    (`dumps/const_pool.tsv`, produced by fastaes_proxy6)
  * integer immediates that are plausible IEEE-754 floats decoded, because scoring
    code is full of magic constants and `mov eax, 0x447a0000` IS `1000.0f`
  * the body truncated at the first >=6-byte `int3` run, which is how MSVC pads
    between functions -- so the listing stops at the real end of the method
  * a trailer summarising callees, data refs and constants

The RVA a body was loaded at is recovered automatically: from `dumps/dump_index.tsv`
(fastaes_proxy6) if present, else by parsing `dumps/dump_log.txt`, else from
`method_map.tsv` by matching the class/method encoded in the filename. `--at` overrides.

    proxyenv/Scripts/python disasm_report.py --selftest
    proxyenv/Scripts/python disasm_report.py <file.bin> [--at RVA] [--max N] [--raw]
    proxyenv/Scripts/python disasm_report.py [--v6] --all [--grep SUBSTR]
    proxyenv/Scripts/python disasm_report.py --verify-v6

`--v6` reads dumps/v6/ (the fastaes_proxy6 run) instead of dumps/. `--verify-v6`
checks that run's artefacts against ground truth on disk and disassembles
`LiveScoreCalculator::CalcScore`, which is the whole reason the run exists.

`--selftest` is the regression: it re-derives the known `CreateAesIV` callee sequence
(`MD5CryptoServiceProvider..ctor` -> `TransformBlock` -> `TransformFinalBlock`) from
`dumps/16__*CreateAesIV.bin` and fails loudly if it cannot. A tool that cannot reproduce
a known answer is not ready.
"""
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DUMP_DIR = os.path.join(HERE, "dumps")
METHOD_MAP = os.path.join(DUMP_DIR, "method_map.tsv")
CONST_POOL = os.path.join(DUMP_DIR, "const_pool.tsv")
DUMP_INDEX = os.path.join(DUMP_DIR, "dump_index.tsv")
DUMP_LOG = os.path.join(DUMP_DIR, "dump_log.txt")

# The stub every abstract/unimplemented method shares; not a real body.
NULL_STUB = 0x74C68F0


# --------------------------------------------------------------------------- names

class NameIndex:
    """rva -> [(image, class, method), ...] from method_map.tsv, plus nearest-preceding
    lookup so a call into the middle of a function still gets a label."""

    def __init__(self, path=METHOD_MAP):
        self.by_rva = {}
        self.base = None
        self.path = path
        if not os.path.exists(path):
            self.sorted_rvas = []
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#"):
                    if "base=" in line:
                        self.base = int(line.split("base=")[1].split()[0], 16)
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 5:
                    continue
                try:
                    rva = int(parts[0], 16)
                except ValueError:
                    continue
                self.by_rva.setdefault(rva, []).append(
                    (parts[2], parts[3], parts[4]))
        self.sorted_rvas = sorted(self.by_rva)

    def label(self, rva, allow_nearest=True, hint=None):
        """Return a human label for a code RVA, or None."""
        if rva == NULL_STUB:
            return "<abstract/null stub>"
        hit = self.by_rva.get(rva)
        if hit:
            return self._fmt(hit, hint)
        if not allow_nearest or not self.sorted_rvas:
            return None
        # nearest preceding entry
        lo, hi = 0, len(self.sorted_rvas)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.sorted_rvas[mid] <= rva:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return None
        prev = self.sorted_rvas[lo - 1]
        delta = rva - prev
        if delta > 0x2000:          # too far to be meaningful
            return None
        return f"{self._fmt(self.by_rva[prev])}+{delta:#x}"

    @staticmethod
    def _fmt(entries, hint=None):
        """Format the name(s) at one code RVA.

        il2cpp de-duplicates identical bodies, so a 5-byte leaf getter can be shared by
        a thousand methods. Picking entries[0] and printing it as THE name is a lie --
        the alias count is stated, and above a handful the label leads with SHARED so a
        reader never mistakes an arbitrary pick for an identification."""
        pick = entries[0]
        if hint:
            # the dumper sanitises '.', '`', ' ' etc to '_', so match '_' as any char
            hk = re.compile("^" + re.escape(hint[0]).replace("_", ".") + "$")
            hm = re.compile("^" + re.escape(hint[1]).replace("_", ".") + "$")
            for e in entries:
                if hk.match(e[1]) and hm.match(e[2]):
                    pick = e
                    break
        img, klass, meth = pick
        img = img.replace(".dll", "")
        s = f"{img}!{klass}::{meth}"
        n = len(entries)
        if n > 8:
            s = f"SHARED CODE ({n} methods dedup'd), e.g. {s}"
        elif n > 1:
            s += f"  (+{n - 1} aliases @ same code)"
        return s

    def find(self, needle, klass_needle=None):
        needle = needle.lower()
        kn = klass_needle.lower() if klass_needle else None
        out = []
        for rva, entries in self.by_rva.items():
            for img, klass, meth in entries:
                if needle not in meth.lower():
                    continue
                if kn and kn not in klass.lower():
                    continue
                out.append((rva, img, klass, meth))
        return sorted(out)


# ---------------------------------------------------------------- constant pool

class ConstPool:
    """rva -> bytes, harvested live by fastaes_proxy6 for every plausible
    RIP-relative target. Absent for dumps made without it."""

    def __init__(self, path=CONST_POOL):
        self.data = {}
        self.strings = {}
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2:
                    continue
                try:
                    rva = int(parts[0], 16)
                    self.data[rva] = bytes.fromhex(parts[1])
                except ValueError:
                    continue
                if len(parts) >= 3 and parts[2]:
                    self.strings[rva] = parts[2]

    def decode(self, rva):
        """Best-effort interpretation of a data reference."""
        b = self.data.get(rva)
        if b is None:
            return None
        return decode_bytes(b, self.strings.get(rva))


def decode_bytes(b, string=None):
    """Render a captured data word the several ways it might be meant."""
    bits = []
    if string:
        bits.append(f'str "{string}"')
    if len(b) >= 8:
        d = struct.unpack_from("<d", b)[0]
        if _nice_float(d):
            bits.append(f"f64={_fmt_float(d)}")
    if len(b) >= 4:
        fv = struct.unpack_from("<f", b)[0]
        if _nice_float(fv):
            bits.append(f"f32={_fmt_float(fv)}")
        iv = struct.unpack_from("<i", b)[0]
        if -1_000_000 < iv < 1_000_000:
            bits.append(f"i32={iv}")
    if not bits:
        bits.append("raw=" + b[:8].hex())
    return " ".join(bits)


class PeRdata:
    """A PROVEN-NEGATIVE probe: can scoring constants be read from the file offline?

    It looks like they should be. `--packing` shows only the CODE sections gutted, and
    the section table claims `.rdata` -- where compilers put float/double literals -- is
    almost entirely present on disk:

        il2cpp   VSize 0x6bce000  RawSize 0x1000      packed
        .text    VSize 0x09ff000  RawSize 0x1000      packed
        .rdata   VSize 0x226f000  RawSize 0x226d000   *claims* 36 MB on disk
        .data    VSize 0x0bb5000  RawSize 0x0911000   *claims* 9 MB on disk

    It is not true. `--probe-rdata` reads at the file offsets the section table gives:
    **0 of 400 sampled 16-byte reads inside `.rdata` are non-zero**, and the raw ranges
    for `.rdata` and `.data` are zero-filled wherever they were sampled. The real 147 MB
    of file content sits in the 101,376,000 bytes BEFORE the first section's RawPtr --
    the packed blob, claimed by no section at all.

    So the section table's file offsets are a lie in both directions, and constants are
    no more recoverable offline than code is. `const_pool.tsv` from a live run is the
    only way to read them. This class is kept, out of the disassembler's path, so the
    negative result can be reproduced instead of rediscovered."""

    def __init__(self, path=None):
        self.path = path or GAME_DLL
        self.secs = []
        try:
            with open(self.path, "rb") as f:
                d = f.read(0x1000)
        except OSError:
            return
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        n = struct.unpack_from("<H", d, pe + 6)[0]
        optsz = struct.unpack_from("<H", d, pe + 20)[0]
        sec = pe + 24 + optsz
        for i in range(n):
            o = sec + i * 40
            name = d[o:o + 8].rstrip(b"\0").decode("ascii", "replace")
            vs, va, rs, rp = struct.unpack_from("<IIII", d, o + 8)
            self.secs.append((name, va, vs, rp, rs))

    def read(self, rva, n=16):
        for name, va, vs, rp, rs in self.secs:
            if not (va <= rva < va + vs):
                continue
            if name != ".rdata":          # .data is runtime-filled; code is packed
                return None
            delta = rva - va
            if delta + n > rs:            # past what the file actually backs
                return None
            with open(self.path, "rb") as f:
                f.seek(rp + delta)
                b = f.read(n)
            return b if any(b) else None
        return None


GAME_DLL = (r"C:\Program Files (x86)\Steam\steamapps\common\hololiveDreams"
            r"\GameAssembly.dll")


def _nice_float(v):
    """True if v is a finite float a human would recognise as a game constant."""
    if v != v or v in (float("inf"), float("-inf")):
        return False
    a = abs(v)
    if a == 0.0:
        return False
    return 1e-6 <= a <= 1e9


def _fmt_float(v):
    s = f"{v:.10g}"
    return s


def imm_as_float(value, size):
    """Decode an integer immediate as an IEEE float when it plausibly is one.

    `mov eax, 0x447a0000` is how a compiler materialises 1000.0f into an xmm reg via
    a GPR; permil divisors and score coefficients show up exactly like this."""
    out = []
    if _looks_decimal(value):
        # 0x3b9aca00 is a perfectly good float bit pattern (0.0047237873) but it is
        # far more likely to be the integer 1000000000. Round decimals win.
        return out
    if size >= 4 and 0 <= value < (1 << 32):
        f = struct.unpack("<f", struct.pack("<I", value & 0xFFFFFFFF))[0]
        if _nice_float(f) and value > 0x1000:
            out.append(f"f32 {_fmt_float(f)}")
    if size >= 8 and 0 <= value < (1 << 64):
        d = struct.unpack("<d", struct.pack("<Q", value & ((1 << 64) - 1)))[0]
        if _nice_float(d):
            out.append(f"f64 {_fmt_float(d)}")
    return out


def _looks_decimal(v):
    """True when the integer reading of an immediate is the obvious one."""
    a = abs(v)
    if a <= 0x1000:
        return True
    if a & (a - 1) == 0:               # a power of two
        return True
    if a <= 10 ** 10 and a % 100 == 0:  # 1000, 10000, 1000000000, 2^n*100 ...
        return True
    return False


# ------------------------------------------------------------------ body extent

def body_len(buf):
    """Length up to the first run of >=6 int3, mirroring fastaes_proxy2's body_len.
    MSVC pads inter-function gaps with 0xCC, so this is the function end."""
    n = len(buf)
    i = 0
    while i + 6 <= n:
        if buf[i] == 0xCC:
            run = 1
            while run < 6 and buf[i + run] == 0xCC:
                run += 1
            if run >= 6:
                return i
        i += 1
    return n


# --------------------------------------------------------------- rva resolution

def _index_from_dump_index():
    """dump_index.tsv (fastaes_proxy6): file<TAB>rva<TAB>len<TAB>image<TAB>class<TAB>method"""
    out = {}
    if not os.path.exists(DUMP_INDEX):
        return out
    with open(DUMP_INDEX, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("#"):
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) < 2:
                continue
            try:
                out[os.path.basename(p[0])] = int(p[1], 16)
            except ValueError:
                pass
    return out


_LOG_RE = re.compile(
    r"\[DUMP\s*#?(\d+)\].*?::\s*([^\s]+)\.([^\s]+)\s+.*?rva=0x([0-9a-fA-F]+)")


def _index_from_dump_log():
    """dump_log.txt lines look like
       `  [DUMP #16] quaunity-api.Runtime.dll :: DefaultMarshallerFactory.CreateAesIV
        pcount=2  code=00007FF8E186C350  rva=0x3dac350  len=0x1000`
    Key by the two-digit ordinal, which is also the .bin filename prefix."""
    out = {}
    if not os.path.exists(DUMP_LOG):
        return out
    with open(DUMP_LOG, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _LOG_RE.search(line)
            if m:
                out[int(m.group(1))] = int(m.group(4), 16)
    return out


def filename_hint(path):
    """`NN__Image__Class__Method.bin` -> (Class, Method), used to disambiguate a code
    RVA that several de-duplicated methods share."""
    base = os.path.basename(path)
    stem = base[:-4] if base.lower().endswith(".bin") else base
    parts = stem.split("__")
    return (parts[-2], parts[-1]) if len(parts) >= 3 else None


def resolve_rva(path, names):
    """Recover the load RVA of a dumped body. Returns (rva, how)."""
    base = os.path.basename(path)
    idx = _index_from_dump_index()
    if base in idx:
        return idx[base], "dump_index.tsv"

    m = re.match(r"^(\d+)__", base)
    if m:
        log = _index_from_dump_log()
        n = int(m.group(1))
        if n in log:
            return log[n], "dump_log.txt"

    # last resort: the filename encodes Class__Method; ask the map.
    stem = base[:-4] if base.lower().endswith(".bin") else base
    parts = stem.split("__")
    if len(parts) >= 3:
        meth, klass = parts[-1], parts[-2]
        # the dumper sanitises '.', '`', ' ' etc to '_'
        pat = re.compile("^" + re.escape(meth).replace("_", ".") + "$")
        kpat = re.compile("^" + re.escape(klass).replace("_", ".") + "$")
        hits = []
        for rva, entries in names.by_rva.items():
            for img, k, mm in entries:
                if pat.match(mm) and kpat.match(k):
                    hits.append((rva, img, k, mm))
        uniq = {h[0] for h in hits}
        if len(uniq) == 1:
            return uniq.pop(), "method_map.tsv (filename match)"
    return None, "UNRESOLVED"


# ------------------------------------------------------------------- the report

def probe_rdata(samples=400):
    """Reproduce the measurement behind PeRdata's docstring."""
    import random
    r = PeRdata()
    if not r.secs:
        print("could not read", GAME_DLL)
        return 1
    print(f"{GAME_DLL}\nsections: {len(r.secs)}")
    first_raw = min(s[3] for s in r.secs)
    print(f"file bytes before the first section's RawPtr: {first_raw:,} "
          f"(the packed blob, claimed by no section)")
    for name, va, vs, rp, rs in r.secs:
        print(f"  {name:10} rva {va:#010x} vsize {vs:#010x} rawptr {rp:#010x} rawsize {rs:#010x}")
    rd = next((s for s in r.secs if s[0] == ".rdata"), None)
    if not rd:
        print("no .rdata section")
        return 1
    random.seed(1)
    ok = 0
    for _ in range(samples):
        k = random.randrange(0, rd[4] - 16)
        if r.read(rd[1] + k, 16):
            ok += 1
    print(f"\nnon-zero 16-byte reads inside .rdata: {ok}/{samples}")
    print("VERDICT:", "constants ARE readable offline -- wire PeRdata back into report()"
          if ok else "the .rdata raw range is zero-filled; constants are NOT readable "
                     "offline. const_pool.tsv from a live run is the only source.")
    return 0


def report(path, at=None, limit=4000, names=None, pool=None, raw=False, out=print):
    from capstone import Cs, CS_ARCH_X86, CS_MODE_64
    from capstone import x86 as cx

    names = names if names is not None else NameIndex()
    pool = pool if pool is not None else ConstPool()

    data = open(path, "rb").read()
    how = "--at"
    if at is None:
        at, how = resolve_rva(path, names)
    if at is None:
        out(f"!! cannot resolve the load RVA of {os.path.basename(path)}; pass --at")
        return None

    end = len(data) if raw else body_len(data)
    self_label = names.label(at, allow_nearest=False,
                            hint=filename_hint(path)) or "<not in method_map>"

    out(f"=== {os.path.basename(path)}")
    out(f"    {self_label}")
    out(f"    load rva {at:#x} (via {how})   dumped {len(data)} B   "
        f"body {end} B{' (raw, no int3 trim)' if raw else ''}")
    if names.base:
        out(f"    live address at dump time: {names.base + at:#x} "
            f"(module base {names.base:#x})")
    out("")

    md = Cs(CS_ARCH_X86, CS_MODE_64)
    md.detail = True

    callees, datarefs, consts = [], [], []
    n = 0
    last = at
    for ins in md.disasm(data[:end], at):
        last = ins.address + ins.size
        note = ""

        # --- direct call/jmp rel32 -> a method name
        if ins.mnemonic in ("call", "jmp") and len(ins.operands) == 1 \
                and ins.operands[0].type == cx.X86_OP_IMM:
            tgt = ins.operands[0].imm
            lbl = names.label(tgt)
            if lbl:
                note = f"  ; -> {lbl}"
                if ins.mnemonic == "call":
                    callees.append((ins.address, tgt, lbl))
            else:
                # method_map.tsv holds managed method entries only. A target with no
                # managed method within 0x2000 is il2cpp's own C++ runtime (allocation,
                # cast, null-check, generic-shim thunks) or a native icall.
                note = f"  ; -> {tgt:#x} <native / il2cpp runtime>"
                if ins.mnemonic == "call":
                    callees.append((ins.address, tgt, "<native / il2cpp runtime>"))

        # --- RIP-relative memory operand -> a data RVA
        for op in ins.operands:
            if op.type == cx.X86_OP_MEM and op.mem.base == cx.X86_REG_RIP:
                tgt = ins.address + ins.size + op.mem.disp
                dec = pool.decode(tgt)
                if dec:
                    note += f"  ; [rip] {tgt:#x} = {dec}"
                    consts.append((ins.address, tgt, dec))
                else:
                    codelbl = names.label(tgt, allow_nearest=False)
                    if codelbl:
                        note += f"  ; [rip] {tgt:#x} -> {codelbl}"
                    else:
                        note += f"  ; [rip] data @ {tgt:#x} (value UNKNOWN)"
                    datarefs.append((ins.address, tgt))

        # --- immediates that are really floats, or round decimal constants
        for op in ins.operands:
            if op.type == cx.X86_OP_IMM and ins.mnemonic not in ("call", "jmp") \
                    and not ins.mnemonic.startswith("j"):
                v = op.imm
                fl = imm_as_float(v, ins.operands[0].size if ins.operands else 4)
                if fl:
                    note += "  ; imm " + " / ".join(fl)
                    consts.append((ins.address, None, " ".join(fl)))
                elif abs(v) >= 10 and abs(v) < 10_000_000:
                    note += f"  ; imm {v}"

        out(f"  {ins.address:#010x}  {ins.bytes.hex():<20} "
            f"{ins.mnemonic:<9} {ins.op_str}{note}")
        n += 1
        if n >= limit:
            out("  ... (--max reached)")
            break

    out("")
    out(f"  -- {n} instructions, ends at {last:#x}")
    if callees:
        out(f"  -- {len(callees)} direct call(s):")
        seen = set()
        for _a, tgt, lbl in callees:
            if tgt in seen:
                continue
            seen.add(tgt)
            out(f"       {tgt:#010x}  {lbl}")
    if consts:
        out(f"  -- {len(consts)} decoded constant(s):")
        for a, tgt, dec in consts:
            where = f"@{tgt:#x}" if tgt else "imm"
            out(f"       {a:#010x}  {where:>12}  {dec}")
    if datarefs:
        out(f"  -- {len(datarefs)} unresolved data ref(s) "
            f"(dump const_pool.tsv to read them):")
        for a, tgt in datarefs[:40]:
            out(f"       {a:#010x}  -> {tgt:#x}")
        if len(datarefs) > 40:
            out(f"       ... and {len(datarefs) - 40} more")
    out("")
    return {"rva": at, "callees": callees, "consts": consts, "datarefs": datarefs}


# ---------------------------------------------------------------- v6 verification

V6_DIR = os.path.join(DUMP_DIR, "v6")


def use_dir(path):
    """Point every artefact path at `path` (used for --v6)."""
    global DUMP_DIR, METHOD_MAP, CONST_POOL, DUMP_INDEX, DUMP_LOG
    DUMP_DIR = path
    METHOD_MAP = os.path.join(path, "method_map.tsv")
    CONST_POOL = os.path.join(path, "const_pool.tsv")
    DUMP_INDEX = os.path.join(path, "dump_index.tsv")
    DUMP_LOG = os.path.join(path, "dump6_log.txt")


def _count_rows(path):
    if not os.path.exists(path):
        return None
    n = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip() and not line.startswith("#"):
                n += 1
    return n


def verify_v6():
    """Check what the v6 run actually left on disk, against ground truth -- not against
    the run's own log line. A pipeline reporting its own success proves nothing.

    The decisive check is the last one: LiveScoreCalculator::CalcScore must be present,
    readable and disassemblable. Everything else can be perfect and the run still be
    worthless if that body is missing."""
    use_dir(V6_DIR)
    fails, warns = [], []

    def chk(cond, ok_msg, bad_msg, warn=False):
        if cond:
            print(f"  [ OK ] {ok_msg}")
        else:
            print(f"  [{'WARN' if warn else 'FAIL'}] {bad_msg}")
            (warns if warn else fails).append(bad_msg)
        return cond

    print(f"verifying v6 artefacts in {V6_DIR}\n")
    if not os.path.isdir(V6_DIR):
        print("  [FAIL] the directory does not exist -- the run never started, or the "
              "proxy was not loaded. Check that fastaes_universal.dll in the game dir "
              "is the 24 KB proxy, not the 487,944 B engine.")
        return 1

    log = ""
    if os.path.exists(DUMP_LOG):
        log = open(DUMP_LOG, encoding="utf-8", errors="replace").read()
    chk("=== DONE" in log, "dump6_log.txt reports a complete run",
        "dump6_log.txt has no DONE line -- the run died partway. Read it: "
        + DUMP_LOG)

    nmap = _count_rows(METHOD_MAP)
    chk(nmap is not None and nmap > 250_000,
        f"method_map.tsv: {nmap:,} rows", f"method_map.tsv: {nmap} rows (expected >250k)")

    nidx = _count_rows(DUMP_INDEX)
    chk(nidx is not None and nidx > 1500,
        f"dump_index.tsv: {nidx:,} bodies indexed",
        f"dump_index.tsv: {nidx} bodies (expected ~1962)")

    # every indexed body must exist on disk at the stated length
    missing, wrong, trunc = 0, 0, 0
    rows = []
    if nidx:
        with open(DUMP_INDEX, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                p = line.rstrip("\n").split("\t")
                if len(p) < 8:
                    continue
                rows.append(p)
                fp = os.path.join(V6_DIR, p[0].replace("\\", os.sep))
                if not os.path.exists(fp):
                    missing += 1
                elif os.path.getsize(fp) != int(p[2], 16):
                    wrong += 1
                if p[3] == "1":
                    trunc += 1
    chk(missing == 0 and wrong == 0,
        f"all {len(rows):,} indexed bodies exist at the indexed length",
        f"{missing} body file(s) missing, {wrong} with the wrong length")
    chk(trunc == 0, "no body hit the 0x4000 dump window",
        f"{trunc} body/bodies hit the 0x4000 window and are TRUNCATED "
        f"-- raise WINDOW in fastaes_proxy6.c if one of them matters", warn=True)

    nconst = _count_rows(CONST_POOL)
    chk(nconst is not None and nconst > 1000,
        f"const_pool.tsv: {nconst:,} data targets captured",
        f"const_pool.tsv: {nconst} rows -- without it, every float constant stays UNKNOWN")

    nfld = _count_rows(os.path.join(V6_DIR, "field_map.tsv"))
    chk(nfld is not None and nfld > 10_000,
        f"field_map.tsv: {nfld:,} field offsets",
        f"field_map.tsv: {nfld} rows -- [rcx+0x30] will stay unnamed", warn=True)

    # the section-realigned full-image dump: check the realignment actually happened,
    # because a dump whose PointerToRawData still points at the packer's offsets is
    # worthless to Il2CppDumper and would fail silently there instead of here.
    img = os.path.join(V6_DIR, "GameAssembly_dumped.bin")
    if not os.path.exists(img):
        chk(False, "", "GameAssembly_dumped.bin missing -- phase 5 did not run", warn=True)
    else:
        with open(img, "rb") as f:
            hdr = f.read(0x1000)
        size = os.path.getsize(img)
        pe = struct.unpack_from("<I", hdr, 0x3C)[0]
        nsec = struct.unpack_from("<H", hdr, pe + 6)[0]
        optsz = struct.unpack_from("<H", hdr, pe + 20)[0]
        soi = struct.unpack_from("<I", hdr, pe + 24 + 56)[0]
        sec = pe + 24 + optsz
        bad = []
        for i in range(nsec):
            o = sec + i * 40
            name = hdr[o:o + 8].rstrip(b"\0").decode("ascii", "replace")
            vs, va, rs, rp = struct.unpack_from("<IIII", hdr, o + 8)
            if rp != va or rs != vs:
                bad.append(f"{name} rawptr={rp:#x} vaddr={va:#x}")
        chk(size == soi,
            f"GameAssembly_dumped.bin: {size:,} bytes == SizeOfImage",
            f"GameAssembly_dumped.bin is {size:,} bytes, SizeOfImage says {soi:,}")
        chk(not bad,
            f"all {nsec} sections realigned (PointerToRawData == VirtualAddress)",
            "sections NOT realigned: " + "; ".join(bad))

    # RVA stability: the four already-resolved targets must be where the previous map said.
    old = NameIndex(os.path.join(os.path.dirname(V6_DIR), "method_map.tsv"))
    new = NameIndex(METHOD_MAP)
    known = {
        ("CardStatus", "GetPotentialUpgradeBonusPermil"): 0x52ACEB0,
        ("ActiveSkillEffectDataCollection", "CalcScoreUpRatio"): 0x5EFDB90,
        ("WorkUtility", "GetTotalPowerWithBonus"): 0x68CA310,
    }
    if new.by_rva:
        drift = []
        for (k, m), want in known.items():
            got = {r for r, es in new.by_rva.items()
                   for _i, kk, mm in es if kk == k and mm == m}
            if want not in got:
                drift.append(f"{k}::{m} was {want:#x}, now {sorted(got)}")
        chk(not drift,
            "RVAs are stable across runs -- the July method_map is still valid",
            "RVA DRIFT (the build changed; old notes' addresses are stale): "
            + "; ".join(drift))
    del old

    # ---- the one that matters -----------------------------------------------------
    print("")
    target = None
    for p in rows:
        if p[6] == "LiveScoreCalculator" and p[7] == "CalcScore":
            target = p
            break
    if not target:
        print("  [FAIL] LiveScoreCalculator::CalcScore is NOT in the dump. This run did "
              "not answer the question it was for.")
        fails.append("CalcScore missing")
    else:
        fp = os.path.join(V6_DIR, target[0].replace("\\", os.sep))
        print(f"  [ OK ] LiveScoreCalculator::CalcScore dumped: {target[2]} bytes "
              f"at rva {target[1]}")
        print("")
        report(fp, int(target[1], 16), 4000, new, ConstPool(CONST_POOL))

    print(f"\nVERIFY: {len(fails)} failure(s), {len(warns)} warning(s)")
    if not fails:
        print("v6 artefacts are good. Read the rest with:")
        print("  proxyenv/Scripts/python disasm_report.py --v6 --all --grep skill")
    return 1 if fails else 0


# --------------------------------------------------------------------- selftest

def selftest():
    """Regression: re-derive the known CreateAesIV result from the dumped 4 KB body.

    Established by hand:
      CreateAesIV calls MD5CryptoServiceProvider..ctor, then HashAlgorithm.TransformBlock,
      then HashAlgorithm.TransformFinalBlock.  If the tool cannot see those three names in
      that order it is not reproducing a known answer and must not be trusted on an
      unknown one."""
    ref = os.path.join(
        DUMP_DIR, "16__quaunity-api_Runtime_dll__DefaultMarshallerFactory__CreateAesIV.bin")
    if not os.path.exists(ref):
        print("SELFTEST FAIL: reference body missing:", ref)
        return 1
    names = NameIndex()
    if not names.by_rva:
        print("SELFTEST FAIL: method_map.tsv missing or empty:", METHOD_MAP)
        return 1
    lines = []
    res = report(ref, names=names, out=lines.append)
    print("\n".join(lines))
    if not res:
        print("SELFTEST FAIL: no report produced")
        return 1
    if res["rva"] != 0x3DAC350:
        print(f"SELFTEST FAIL: rva resolved to {res['rva']:#x}, expected 0x3dac350")
        return 1
    seq = [lbl for _a, _t, lbl in res["callees"]]
    want = ["MD5CryptoServiceProvider::.ctor", "TransformBlock", "TransformFinalBlock"]
    pos = -1
    for w in want:
        nxt = next((i for i, s in enumerate(seq) if i > pos and w in s), None)
        if nxt is None:
            print(f"SELFTEST FAIL: expected callee containing {w!r} after index {pos}")
            print("  got:", seq)
            return 1
        pos = nxt
    print("SELFTEST PASS: CreateAesIV re-derived --")
    print("  MD5CryptoServiceProvider..ctor -> TransformBlock -> TransformFinalBlock")
    print("  i.e. IV = MD5(a1 || a2[4 : 4+a2[3]])  (MODEL-REVISION 45b, ATTESTED)")
    return 0


# -------------------------------------------------------------------------- CLI

def main():
    argv = sys.argv[1:]
    if not argv or "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0

    if "--selftest" in argv:
        return selftest()
    if "--verify-v6" in argv:
        return verify_v6()
    if "--probe-rdata" in argv:
        return probe_rdata()
    if "--v6" in argv:
        use_dir(V6_DIR)

    at = int(argv[argv.index("--at") + 1], 0) if "--at" in argv else None
    limit = int(argv[argv.index("--max") + 1]) if "--max" in argv else 4000
    raw = "--raw" in argv
    names = NameIndex()
    pool = ConstPool()

    if "--all" in argv:
        grep = argv[argv.index("--grep") + 1].lower() if "--grep" in argv else None
        files = []
        for root, _dirs, names_ in os.walk(DUMP_DIR):
            for f in names_:
                if f.endswith(".bin"):
                    files.append(os.path.join(root, f))
        files.sort()
        if grep:
            files = [f for f in files if grep in os.path.basename(f).lower()]
        print(f"# {len(files)} body/bodies under {DUMP_DIR}\n")
        for f in files:
            report(f, None, limit, names, pool, raw)
        return 0

    path = argv[0]
    if not os.path.isabs(path):
        cand = os.path.join(DUMP_DIR, path)
        path = cand if os.path.exists(cand) else path
    report(path, at, limit, names, pool, raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
