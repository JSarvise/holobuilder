# Cross-validate the offline master tables against HolodoriDB's published dumps, and put real
# field NAMES on the numeric field indices.
#
# Two external repos, both public, both read-only from here:
#
#   HolodoriDB/holodori-app-protos   the client's own .proto dump  ->  field number -> real name
#   HolodoriDB/holodori-db-eng-diff  the same master DB as JSON    ->  the values, by name
#
# This project reads the same tables offline from the game's own SQLCipher cache (master_cache.py),
# keyed by protobuf field NUMBER. Joining the two gives, for every table:
#
#   * a name for every field number used here  (`--names <Table>`)
#   * a value-level diff of the local rows against theirs  (`--check <Table>`)
#
# The point of --check is to verify artefacts, not counters. Two independent pipelines reading the
# same bytes should produce the same numbers; where they do not, one of them is wrong and it is
# worth knowing which. Nothing here writes to data/ -- it prints, and it caches
# fetched files under dumps/holodori/.
#
# The refresh does not call `--fetch`: the main_<Table>.json files the runtime reads are written
# from the local master by master_json.py, so there is no external dependency. `--fetch` would
# overwrite them with HolodoriDB's copy, which lags the game -- use it only to compare, and note
# the cache it reads by default IS that same file, so a comparison needs --refresh into a copy.
#
#   python holodori_sync.py --names LiveActiveSkillLevel
#   python holodori_sync.py --check LiveActiveSkillLevel
#   python holodori_sync.py --check-all          # every table with both sides present
#   python holodori_sync.py --tables             # what the two sides each hold
#
# Etiquette: raw.githubusercontent.com, one request per second, every response cached to disk and
# reused forever. Pass --refresh to re-fetch.
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import master_cache  # noqa: E402

CACHE = os.path.join(HERE, "dumps", "holodori")
PROTO_BASE = ("https://raw.githubusercontent.com/HolodoriDB/holodori-app-protos/"
              "main/global/protobufs/entity/master/")
DB_BASE = ("https://raw.githubusercontent.com/HolodoriDB/holodori-db-eng-diff/"
           "main/")
UA = "hololive-builder-recon/1.0 (personal cross-validation; contact via repo)"
_last = [0.0]


def _snake(name):
    """LiveActiveSkillLevel -> live_active_skill_level (their proto file naming)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _camel(name):
    """live_active_skill_level -> liveActiveSkillLevel (their JSON key naming)."""
    head, *rest = name.split("_")
    return head + "".join(w.title() for w in rest)


def fetch(url, refresh=False):
    """GET with a permanent on-disk cache and a 1 req/sec floor. Returns text or None on 404."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, re.sub(r"[^A-Za-z0-9._-]", "_", url.rsplit("/", 2)[-2] + "_" +
                                      url.rsplit("/", 1)[-1]))
    if os.path.exists(path) and not refresh:
        text = open(path, encoding="utf-8").read()
        return None if text == "\0404" else text
    wait = 1.0 - (time.monotonic() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            open(path, "w", encoding="utf-8").write("\0404")
            return None
        raise
    open(path, "w", encoding="utf-8").write(text)
    return text


FIELD = re.compile(r"^\s*(?:repeated\s+)?([.\w]+)\s+(\w+)\s*=\s*(\d+)\s*[;\[]")
ENUM_VAL = re.compile(r"^\s*(\w+)\s*=\s*(\d+)\s*[;\[]")
ENUM_BASE = ("https://raw.githubusercontent.com/HolodoriDB/holodori-app-protos/"
             "main/global/protobufs/enums/")


def proto_fields(table, refresh=False):
    """{field number: (snake_case name, type)} for a master table, from their .proto dump.

    Only the fields of the table's OWN top-level message -- nested messages are skipped, which
    matters for the rpc messages but not for entity/master (one message per file)."""
    text = fetch(PROTO_BASE + _snake(table) + ".gen.proto", refresh)
    if text is None:
        return None
    out, depth, inside = {}, 0, False
    for line in text.splitlines():
        if re.match(rf"^\s*message\s+{re.escape(table)}\s*\{{", line):
            inside, depth = True, 1
            continue
        if not inside:
            continue
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            break
        m = FIELD.match(line)
        if m and "message " not in line:
            out[int(m.group(3))] = (m.group(2), m.group(1))
    return out or None


def enum_values(typ, refresh=False):
    """{'SomeEnum_SOME_VALUE': number} for a `.enums.X` proto type, or {} if unavailable."""
    short = typ.rsplit(".", 1)[-1]
    text = fetch(ENUM_BASE + _snake(short) + ".gen.proto", refresh)
    if text is None:
        return {}
    out, inside = {}, False
    for line in text.splitlines():
        if re.match(rf"^\s*enum\s+{re.escape(short)}\s*\{{", line):
            inside = True
            continue
        if not inside:
            continue
        if "}" in line:
            break
        m = ENUM_VAL.match(line)
        if m:
            # their JSON writes the enum-name-prefixed form, e.g.
            # LiveActiveSkillEffectType_LIVE_ACTIVE_SKILL_EFFECT_TYPE_SCORE_UP_PERMIL_UP
            out[m.group(1)] = int(m.group(2))
            out[f"{short}_{m.group(1)}"] = int(m.group(2))
    return out


def their_rows(table, refresh=False):
    """Their published rows for a table, as a list of the inner `data` dicts."""
    text = fetch(DB_BASE + table + ".json", refresh)
    if text is None:
        return None
    return [r.get("data", r) for r in json.loads(text)]


def our_rows(table):
    # a table whose local copy will not decrypt must not kill a --check-all sweep; report it and
    # keep going (Condition currently fails the SQLCipher hmac on page 1).
    try:
        return master_cache.rows(table)
    except Exception as e:                                   # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def _norm(v):
    """Compare across the two encodings: theirs stringifies int64, the local one does not."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    return v


def _ours_by_name(rec, fields):
    """The local numeric-keyed record, re-keyed by their camelCase names."""
    out = {}
    for num, vals in rec["data"].items():
        f = fields.get(int(num))
        out[f"#{num}" if f is None else _camel(f[0])] = vals
    return out


def _theirs_normalised(rec, fields, refresh=False):
    """Their record with enum names collapsed back to the stored wire number.

    They stringify enums (`LiveActiveSkillEffectType_..._SCORE_UP_PERMIL_UP`); the local decoder keeps
    the varint. Without this every row of every table with an enum column reads as a difference,
    which is noise, not drift."""
    enums = {}
    for _num, (name, typ) in fields.items():
        if typ.startswith(".enums."):
            enums[_camel(name)] = enum_values(typ, refresh)
    out = {}
    for k, v in rec.items():
        table = enums.get(k)
        vals = v if isinstance(v, list) else [v]
        if table:
            vals = [table.get(x, x) if isinstance(x, str) else x for x in vals]
        out[k] = vals
    return out


def names(table, refresh=False):
    fields = proto_fields(table, refresh)
    if fields is None:
        print(f"{table}: no proto in their dump")
        return 1
    ours = our_rows(table)
    seen = set()
    if isinstance(ours, list):
        for r in ours:
            seen.update(int(k) for k in r["data"])
    print(f"{table}  --  {len(fields)} fields in their proto, "
          f"{len(seen)} distinct field numbers in our {len(ours) if isinstance(ours, list) else 0} rows")
    for num in sorted(set(fields) | seen):
        mark = " " if num in seen else "."      # '.' = declared but never populated in the local copy
        name, typ = fields.get(num, ("?", "?"))
        extra = "" if num in fields else "   <-- NOT IN THEIR PROTO"
        print(f"  {mark}{num:5}  {name:52} {typ}{extra}")
    return 0


def _key(d):
    return json.dumps(sorted((k, [_norm(x) for x in (v if isinstance(v, list) else [v])])
                             for k, v in d.items()), ensure_ascii=False, sort_keys=True)


def check(table, refresh=False, quiet=False):
    """Row-for-row value diff. Returns (verdict, detail)."""
    fields = proto_fields(table, refresh)
    theirs = their_rows(table, refresh)
    ours = our_rows(table)
    if fields is None:
        return "NO-PROTO", ""
    if theirs is None:
        return "NOT-PUBLISHED", ""
    if isinstance(ours, str):
        return "NOT-CACHED", ours
    if len(ours) != len(theirs):
        return "ROW-COUNT", f"ours {len(ours)} vs theirs {len(theirs)}"
    mine = sorted(_key(_ours_by_name(r, fields)) for r in ours)
    yours = sorted(_key(_theirs_normalised(r, fields, refresh)) for r in theirs)
    if mine == yours:
        return "EXACT", f"{len(ours)} rows"
    bad = [(a, b) for a, b in zip(mine, yours) if a != b]
    if not quiet:
        for a, b in bad[:3]:
            da, db = dict(json.loads(a)), dict(json.loads(b))
            for k in sorted(set(da) | set(db)):
                if da.get(k) != db.get(k):
                    line = f"    {k:44} ours {str(da.get(k))[:60]:62} theirs {str(db.get(k))[:60]}"
                    # console encodings vary; a diff line must never kill the run
                    sys.stdout.write(line.encode(sys.stdout.encoding or "utf-8",
                                                 "replace").decode(sys.stdout.encoding or
                                                                   "utf-8") + "\n")
            print("    --")
    return "DIFFER", f"{len(bad)}/{len(ours)} rows differ"


def tables():
    idx = json.load(open(os.path.join(HERE, "dumps", "master_index.json"), encoding="utf-8"))
    have = [t for t in sorted(idx) if master_cache.cached_path(t)]
    print(f"our local cache: {len(have)}/{len(idx)} tables")
    return have


def main():
    refresh = "--refresh" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--tables" in sys.argv:
        for t in tables():
            print(" ", t)
        return 0
    if "--names" in sys.argv:
        return names(args[0], refresh)
    if "--check" in sys.argv:
        verdict, detail = check(args[0], refresh)
        print(f"{args[0]:36} {verdict:14} {detail}")
        return 0 if verdict == "EXACT" else 1
    if "--fetch" in sys.argv:
        # Re-download the given tables' published rows into dumps/holodori/ (the
        # display/cast sources: Music for casts, LangMusic_Eng for titles). One
        # request per table, 1 req/s. This replaces the master_json.py copies
        # that `build_charts.py` reads -- see the header before using it.
        bad = 0
        for t in args:
            rows = their_rows(t, refresh=True)
            if rows is None:
                print(f"{t:36} NOT-PUBLISHED"); bad = 1
            else:
                print(f"{t:36} {len(rows)} rows -> dumps/holodori/main_{t}.json")
        return bad
    if "--check-all" in sys.argv:
        worst = 0
        counts = {}
        for t in (args or tables()):
            verdict, detail = check(t, refresh, quiet=True)
            counts[verdict] = counts.get(verdict, 0) + 1
            if verdict not in ("EXACT", "NOT-CACHED", "NOT-PUBLISHED", "NO-PROTO"):
                worst = 1
            print(f"{t:40} {verdict:14} {detail}")
        print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return worst
    print(__doc__ or "usage: holodori_sync.py --names|--check <Table> | --check-all | --tables | --fetch <Table>...")
    return 2


if __name__ == "__main__":
    sys.exit(main())
