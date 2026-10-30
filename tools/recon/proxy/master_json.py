"""`dumps/holodori/main_<Table>.json`: the master tables as named JSON, from the local master.

A third-party export of the master lags the game (a song released that day can arrive with no
cast), so these files are rendered locally instead: `master_index_live.py` fetches the live
index and `master_cache.py` decrypts any table; this module adds the field NAMES.

The names come from `master_schema/schema.json` (committed next to this file): field number -> json
name, type and enum value names for every master table. It is compiled
(`--compile-schema <dir of .proto>...`) from the client's master .proto set as captured in
`dumps/holodori/*.gen.proto` (the community HolodoriDB dump of the client's protos)
plus `master_schema/common.proto` (shapes that set lacks). It is a snapshot of NAMES only and nothing fetches
it: every value written comes from the local master. The client also embeds these descriptors in
global-metadata (base64 string literals), which would make the schema the game's own; the
literal table is sorted, so their order would have to be rebuilt, and it is not known whether
that can be done reliably.

The output reproduces the HolodoriDB file shape, so consumers read either: each row is the SQL
row's own key columns plus `data`, the record in proto3 JSON (camelCase names, int64 as a string,
enums as `<Type>_<VALUE>`). `--check` compares this rendering with a HolodoriDB copy on disk, row
by row (Card, CardLevel, LiveNote (floats), SkillTreeNode (nested Consumption), Poster, Music and
LangMusic_Eng match exactly, bar rows one side does not have yet).

A field number the schema does not name is a field the game added after the schema was
compiled. It is printed (UNNAMED) and left out, the same as an older export would; recompile the
schema from the newer protos to name it.

    proxyenv/Scripts/python master_json.py Music LangMusic_Eng      # write main_<T>.json
    proxyenv/Scripts/python master_json.py --check Music           # ours vs the file on disk
    proxyenv/Scripts/python master_json.py --check-all             # every table that can be rendered
    proxyenv/Scripts/python master_json.py --compile-schema dumps/holodori master_schema
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

OUT = HERE / "dumps" / "holodori"
SCHEMA = HERE / "master_schema" / "schema.json"

_SCALARS = {"string", "bytes", "bool", "float", "double", "int32", "int64", "uint32", "uint64",
            "sint32", "sint64", "fixed32", "fixed64", "sfixed32", "sfixed64"}
_FIELD = re.compile(r"^\s*(repeated\s+|optional\s+)?([.\w]+)\s+(\w+)\s*=\s*(\d+)\s*(\[[^\]]*\])?\s*;")


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# --- schema ------------------------------------------------------------------------------------

def compile_schema(proto_dirs: list[Path]) -> dict:
    """Every `message` and `enum` in the .proto files under `proto_dirs`, flattened to plain data."""
    messages: dict[str, list] = {}
    enums: dict[str, dict] = {}
    for path in sorted(p for d in proto_dirs for p in d.glob("*.proto")):
        stack: list[tuple[str, str]] = []           # (kind, name) of the open blocks
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*(message|enum)\s+(\w+)\s*\{", line)
            if m:
                stack.append((m.group(1), m.group(2)))
                if m.group(1) == "message":
                    messages.setdefault(m.group(2), [])
                else:
                    enums.setdefault(m.group(2), {})
                continue
            if re.match(r"^\s*\}", line) and stack:
                stack.pop()
                continue
            if not stack:
                continue
            kind, name = stack[-1]
            if kind == "enum":
                e = re.match(r"^\s*(\w+)\s*=\s*(-?\d+)\s*;", line)
                if e:
                    enums[name][e.group(2)] = e.group(1)
                continue
            f = _FIELD.match(line)
            if f:
                label, typ, fname, num, opts = f.groups()
                messages[name].append({"n": int(num), "name": fname, "json": _camel(fname),
                                       "type": typ.lstrip(".").rsplit(".", 1)[-1],
                                       "repeated": bool(label and label.strip() == "repeated"),
                                       "pk": bool(opts and "options.master.pk) = true" in opts)})
    return {"messages": messages, "enums": enums}


def load_schema() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


# --- wire decoding -----------------------------------------------------------------------------

def _varint(b: bytes, p: int) -> tuple[int, int]:
    s = r = 0
    while True:
        x = b[p]
        p += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            return r, p
        s += 7


def _signed(v: int, bits: int) -> int:
    v &= (1 << 64) - 1
    if bits == 32:
        v &= 0xFFFFFFFF
        return v - (1 << 32) if v >> 31 else v
    return v - (1 << 64) if v >> 63 else v


def _fields(b: bytes):
    """(field number, wire type, raw value) in wire order."""
    p = 0
    while p < len(b):
        tag, p = _varint(b, p)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, p = _varint(b, p)
        elif wt == 2:
            ln, p = _varint(b, p)
            v = b[p:p + ln]
            p += ln
        elif wt == 5:
            v = b[p:p + 4]
            p += 4
        elif wt == 1:
            v = b[p:p + 8]
            p += 8
        else:
            raise ValueError(f"wire type {wt} at byte {p}")
        yield fn, wt, v


class Renderer:
    def __init__(self, schema: dict):
        self.messages = schema["messages"]
        self.enums = schema["enums"]
        self.unnamed: dict[str, set[int]] = {}

    def _scalar(self, typ: str, wt: int, v):
        if typ in self.enums:
            name = self.enums[typ].get(str(_signed(v, 32)))
            return f"{typ}_{name}" if name else _signed(v, 32)
        if typ == "string":
            return v.decode("utf-8")
        if typ == "bytes":
            import base64
            return base64.b64encode(v).decode("ascii")
        if typ == "bool":
            return bool(v)
        if typ == "float":
            return _float32(v)
        if typ == "double":
            return struct.unpack("<d", v)[0]
        if typ in ("int32", "sint32", "sfixed32"):
            if typ == "sint32":
                return (v >> 1) ^ -(v & 1)
            if typ == "sfixed32":
                return struct.unpack("<i", v)[0]
            return _signed(v, 32)
        if typ in ("uint32", "fixed32"):
            return v if typ == "uint32" else struct.unpack("<I", v)[0]
        # 64-bit integers: proto3 JSON writes them as strings
        if typ == "int64":
            return str(_signed(v, 64))
        if typ == "sint64":
            return str((v >> 1) ^ -(v & 1))
        if typ == "uint64":
            return str(v)
        if typ == "fixed64":
            return str(struct.unpack("<Q", v)[0])
        if typ == "sfixed64":
            return str(struct.unpack("<q", v)[0])
        raise KeyError(typ)

    def message(self, typ: str, b: bytes) -> dict:
        spec = {f["n"]: f for f in self.messages[typ]}
        out: dict = {}
        for fn, wt, v in _fields(b):
            f = spec.get(fn)
            if f is None:
                self.unnamed.setdefault(typ, set()).add(fn)
                continue
            ftyp = f["type"]
            if ftyp in self.messages:
                val = [self.message(ftyp, v)]
            elif wt == 2 and ftyp not in ("string", "bytes"):
                val = list(self._packed(ftyp, v))          # a packed repeated scalar
            else:
                val = [self._scalar(ftyp, wt, v)]
            if f["repeated"]:
                out.setdefault(f["json"], []).extend(val)
            else:
                out[f["json"]] = val[-1]
        return out

    def _packed(self, typ: str, b: bytes):
        p = 0
        if typ in ("float", "fixed32", "sfixed32"):
            for i in range(0, len(b), 4):
                yield self._scalar(typ, 5, b[i:i + 4])
        elif typ in ("double", "fixed64", "sfixed64"):
            for i in range(0, len(b), 8):
                yield self._scalar(typ, 1, b[i:i + 8])
        else:
            while p < len(b):
                v, p = _varint(b, p)
                yield self._scalar(typ, 0, v)


def _float32(raw: bytes):
    """The shortest decimal that round-trips the float32 -- what protobuf JSON prints."""
    x = struct.unpack("<f", raw)[0]
    if x != x or x in (float("inf"), float("-inf")):
        return x
    for digits in range(1, 10):
        s = float(f"{x:.{digits}g}")
        if struct.unpack("<f", struct.pack("<f", s))[0] == x:
            return int(s) if s == int(s) and abs(s) < 1e15 else s
    return x


# --- tables ------------------------------------------------------------------------------------

def message_for(table: str, schema: dict) -> str | None:
    """The table's own message; every `Lang<X>_<Language>` table is the one `Lang` shape."""
    msgs = schema["messages"]
    if table in msgs:
        return table
    if re.fullmatch(r"Lang\w*_[A-Z][a-z]+", table) and "Lang" in msgs:
        return "Lang"
    return None


def render(table: str, renderer: Renderer | None = None) -> list[dict]:
    import master_cache as mc  # noqa: PLC0415  (needs proxyenv's sqlcipher)
    schema = renderer and {"messages": renderer.messages, "enums": renderer.enums} or load_schema()
    renderer = renderer or Renderer(schema)
    typ = message_for(table, schema)
    if typ is None:
        raise KeyError(f"{table}: no message in {SCHEMA.name}")
    pk_types = {f["name"]: f["type"] for f in schema["messages"][typ]}
    db = mc.open_table(table)
    try:
        out = []
        for r in db.query(f"SELECT * FROM {mc.sql_table(db, table)}"):
            row = {}
            for col, v in r.items():
                if col == "data":
                    row["data"] = renderer.message(typ, v)
                elif pk_types.get(col, "string") == "string":
                    row[col] = v.decode("utf-8")
                else:                                    # an int or enum key column
                    row[col] = int(v.decode())
            out.append(row)
        return out
    finally:
        db.close()


def write(table: str, renderer: Renderer) -> Path:
    rows = render(table, renderer)
    if not rows:
        raise SystemExit(f"{table}: 0 rows -- refusing to write an empty export")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"main_{table}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes(json.dumps(rows, ensure_ascii=False).encode("utf-8"))
    os.replace(tmp, path)
    return path


def compare(ours: list[dict], theirs: list[dict]) -> tuple[str, str]:
    """EXACT, or how the two differ (rows only one side has, rows that differ)."""
    def key(r):
        return json.dumps({k: v for k, v in r.items() if k != "data"}, sort_keys=True)
    a = {key(r): r for r in ours}
    b = {key(r): r for r in theirs}
    only_ours = sorted(set(a) - set(b))
    only_theirs = sorted(set(b) - set(a))
    differ = [k for k in a.keys() & b.keys() if a[k] != b[k]]
    if not (only_ours or only_theirs or differ):
        return "EXACT", f"{len(a)} rows"
    bits = [f"{len(a.keys() & b.keys()) - len(differ)} same"]
    if differ:
        k = differ[0]
        fields = sorted(f for f in set(a[k]["data"]) | set(b[k]["data"])
                        if a[k]["data"].get(f) != b[k]["data"].get(f))
        bits.append(f"{len(differ)} differ (e.g. {k} {fields[:4]})")
    if only_ours:
        bits.append(f"{len(only_ours)} only ours (e.g. {only_ours[0]})")
    if only_theirs:
        bits.append(f"{len(only_theirs)} only theirs (e.g. {only_theirs[0]})")
    return "DIFF", ", ".join(bits)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    if argv[:1] == ["--compile-schema"]:
        schema = compile_schema([Path(a) for a in argv[1:]])
        SCHEMA.write_bytes(json.dumps(schema, ensure_ascii=False, indent=0, sort_keys=True)
                           .encode("utf-8"))
        print(f"{len(schema['messages'])} messages, {len(schema['enums'])} enums -> {SCHEMA}")
        return 0
    renderer = Renderer(load_schema())
    if argv[:1] in (["--check"], ["--check-all"]):
        names = argv[1:] or sorted(p.stem[5:] for p in OUT.glob("main_*.json"))
        counts: dict[str, int] = {}
        for t in names:
            path = OUT / f"main_{t}.json"
            try:
                verdict, detail = compare(render(t, renderer),
                                          json.loads(path.read_text(encoding="utf-8")))
            except Exception as e:                       # noqa: BLE001  report and go on
                verdict, detail = "ERROR", f"{type(e).__name__}: {e}"
            counts[verdict] = counts.get(verdict, 0) + 1
            print(f"{t:44} {verdict:6} {detail}", flush=True)
        print("  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        bad = counts.get("ERROR", 0) + counts.get("DIFF", 0)
    else:
        if not argv:
            print(__doc__.split("    proxyenv")[0].strip(), file=sys.stderr)
            return 2
        bad = 0
        for t in argv:
            try:
                path = write(t, renderer)
                print(f"{t:36} -> {path.relative_to(HERE)}")
            except Exception as e:                       # noqa: BLE001
                print(f"{t:36} FAILED {type(e).__name__}: {e}")
                bad += 1
    for typ, nums in sorted(renderer.unnamed.items()):
        print(f"UNNAMED  {typ}: field(s) {sorted(nums)} are not in {SCHEMA.name} -- recompile it")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
