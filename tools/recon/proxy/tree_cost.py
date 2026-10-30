"""tree_cost.py -- what a Holomem Board node actually costs.

SkillTreeNode field 9 sums to 1002 over the tree and field 13 sums to 420 --
which is the point cost? It is field 9. Settled from the binary, not from this
data: SkillTreeNode::InternalWriteTo writes tag 0x48 == (9<<3) for the field at
+0x50, which the client's field map names
`consumptionSkillTreePointQuantity_`. Field 13 (+0x54, tag 0x68) is
`autoSelectionPriority_` and is not a price at all.

What this script adds on top of that: points are only half the price. Field 1000
(`consumptions_`) carries an ITEM cost in coloured cubes, and the group-id prefix
is the colour. It also computes the true per-character tree ceiling, which is NOT
the 1002 sum -- 3 groups carry one row per character.

Read-only. Touches nothing under data/.

Usage:
    python tree_cost.py            # full report
    python tree_cost.py --owner    # only the cross-check against the account's own boards
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
DUMPS = os.path.join(HERE, "dumps")


# --------------------------------------------------------------------------
# minimal stdlib protobuf walker (no protobuf package dependency)
# --------------------------------------------------------------------------

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def decode_protobuf(buf: bytes) -> dict[int, list[Any]]:
    """Blind tag/wire-type walk. Returns {field_number: [values]}."""
    out: dict[int, list[Any]] = collections.defaultdict(list)
    i = 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            val, i = _varint(buf, i)
        elif wire == 2:
            ln, i = _varint(buf, i)
            raw = buf[i:i + ln]
            i += ln
            try:
                val = raw.decode("utf-8")
            except UnicodeDecodeError:
                val = raw
        elif wire == 5:
            val, i = int.from_bytes(buf[i:i + 4], "little"), i + 4
        elif wire == 1:
            val, i = int.from_bytes(buf[i:i + 8], "little"), i + 8
        else:
            raise ValueError(f"unsupported wire type {wire} at {i}")
        out[field].append(val)
    return dict(out)


def blob_bytes(s: str) -> bytes:
    """The master JSON stores raw bytes round-tripped through latin-1-ish text."""
    return s.encode("utf-8", "surrogateescape") if any(ord(c) > 0xFF for c in s) else bytes(
        ord(c) for c in s
    )


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def load(table: str) -> list[dict]:
    """Rows with `data` as {str(field): [values]}, values kept as BYTES where binary.

    master_cache.rows() decodes every value with errors="replace", which silently
    destroys any byte >= 0x80 -- and field 1000 is a raw protobuf blob whose amount
    varint is exactly where those bytes live. So go to the DB directly. The dumped
    master_*.json files carry the same damage and are only a last-resort fallback.
    """
    sys.path.insert(0, HERE)
    try:
        import master_cache as mc  # noqa: PLC0415
        from master_db import decode_record  # noqa: PLC0415

        db = mc.open_table(table)
        out = []
        for r in db.query(f"SELECT * FROM {table}"):
            rec: dict[str, Any] = {}
            for k, v in r.items():
                if k == "data":
                    rec["data"] = {
                        str(fn): list(vs) for fn, vs in decode_record(v).items()
                    }
                else:
                    rec[k] = v.decode("utf-8", "replace")
            out.append(rec)
        db.close()
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] live DB read failed ({exc}); falling back to LOSSY json dump",
              file=sys.stderr)
        with open(os.path.join(DUMPS, f"master_{table}.json"), encoding="utf-8") as fh:
            return json.load(fh)


def f(row: dict, num: str, default: Any = None) -> Any:
    v = row["data"].get(num)
    if not v:
        return default
    x = v[0]
    return x.decode("utf-8", "replace") if isinstance(x, bytes) and num != "1000" else x


def node_cost(row: dict) -> tuple[str | None, int]:
    """Decode field 1000 into (item_id, amount)."""
    raw = row["data"].get("1000")
    if not raw:
        return None, 0
    buf = raw[0]
    if not isinstance(buf, bytes):
        buf = blob_bytes(buf)
    msg = decode_protobuf(buf)
    item = msg.get(2, [None])[0]
    if isinstance(item, bytes):
        item = item.decode("utf-8", "replace")
    return item, msg.get(3, [0])[0]


# --------------------------------------------------------------------------
# report sections
# --------------------------------------------------------------------------

def section_fields(nodes: list[dict]) -> None:
    print("=== SkillTreeNode: field presence and range ===")
    print(f"rows: {len(nodes)}   groups: {len({r['group_id'] for r in nodes})}")
    seen: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in nodes:
        for k in r["data"]:
            seen[k][type(f(r, k)).__name__] += 1
    for k in sorted(seen, key=int):
        vals = [f(r, k) for r in nodes if k in r["data"]]
        nums = [v for v in vals if isinstance(v, int)]
        extra = f"  min={min(nums)} max={max(nums)} sum={sum(nums)}" if nums else ""
        print(f"  f{k:<5} present={len(vals):>4}  types={dict(seen[k])}{extra}")


def section_f9_f13(nodes: list[dict]) -> None:
    print("\n=== f9 vs f13 vs f5 ===")
    for a, b in (("9", "13"), ("5", "13"), ("5", "9")):
        tab = collections.Counter((f(r, a), f(r, b)) for r in nodes)
        agree = sum(n for (x, y), n in tab.items() if x == y)
        print(f"  f{a} vs f{b}: identical on {agree}/{len(nodes)} rows")
        for (x, y), n in sorted(tab.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
            print(f"      f{a}={x!s:<6} f{b}={y!s:<6} -> {n}")


def section_costs(nodes: list[dict]) -> None:
    print("\n=== field 1000 decoded (the actual unlock price) ===")
    tab = collections.Counter()
    for r in nodes:
        tab[node_cost(r)] += 1
    for (item, amt), n in sorted(tab.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        print(f"  {str(item):<40} x{amt:<6} on {n:>4} rows")
    print("\n  totals over all rows:")
    per_item: collections.Counter = collections.Counter()
    for r in nodes:
        item, amt = node_cost(r)
        if item:
            per_item[item] += amt
    for item, tot in per_item.most_common():
        print(f"    {item:<40} {tot}")


def section_cost_vs_fields(nodes: list[dict]) -> None:
    print("\n=== does f9 or f13 track the item amount? ===")
    for fld in ("9", "13", "5", "4"):
        tab = collections.defaultdict(collections.Counter)
        for r in nodes:
            _, amt = node_cost(r)
            tab[f(r, fld)][amt] += 1
        print(f"  f{fld} -> item amount:")
        for v in sorted(tab, key=str):
            print(f"      f{fld}={v!s:<6} amounts={dict(sorted(tab[v].items()))}")


def char_ids(row: dict) -> list[str]:
    """field 3 is `repeated string characterIds` -- empty means "every character"."""
    return [
        v.decode("utf-8", "replace") if isinstance(v, bytes) else v
        for v in row["data"].get("3", [])
    ]


def rows_for(nodes: list[dict], chr_id: str) -> list[dict]:
    """Every SkillTreeNode row that appears in `chr_id`'s own tree."""
    return [r for r in nodes if not char_ids(r) or chr_id in char_ids(r)]


def section_full_tree(nodes: list[dict]) -> None:
    print("\n=== the full tree, PER CHARACTER (f9 = consumptionSkillTreePointQuantity) ===")
    print("  1002 is the sum over all 323 rows -- but 3 groups carry one row PER character,")
    print("  so no single character's tree ever contains all 323. The real ceiling:")
    every = sorted({c for r in nodes for c in char_ids(r)})
    tot: collections.Counter = collections.Counter()
    for c in every:
        mine = rows_for(nodes, c)
        pts = sum(f(r, "9", 0) or 0 for r in mine)
        items: collections.Counter = collections.Counter()
        for r in mine:
            item, amt = node_cost(r)
            if item:
                items[item] += amt
        tot[(pts, tuple(sorted(items.items())))] += 1
    for (pts, items), n in tot.most_common():
        print(f"  {n:>2} characters: {pts} points  +  {dict(items)}")


def section_budget() -> None:
    print("\n=== the budget: CharacterLevel field 11 = skillTreePointQuantity ===")
    try:
        lv = load("CharacterLevel")
    except Exception as exc:  # noqa: BLE001
        print(f"  unavailable: {exc}")
        return
    lv.sort(key=lambda r: f(r, "2", 0) or 0)
    run = 0
    marks = {10, 20, 30, 40, 50}
    for r in lv:
        level = f(r, "2", 0) or 0
        run += f(r, "11", 0) or 0
        if level in marks:
            print(f"  cumulative at character level {level:>2}: {run}")
    print(f"  TOTAL over all {len(lv)} levels: {run}")


def section_branches(nodes: list[dict]) -> None:
    print("\n=== group-id prefix -> f4 (SkillTreeNodeType) -> currency ===")
    tab: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in nodes:
        gid = f(r, "1", "?")
        item, _ = node_cost(r)
        tab[gid.split("-")[0]][(f(r, "4"), item)] += 1
    for pfx in sorted(tab):
        for (typ, item), n in sorted(tab[pfx].items(), key=lambda kv: str(kv[0])):
            print(f"  {pfx + '-*':<6} f4={typ!s:<5} {str(item):<28} {n:>4} rows")


def section_owner(nodes: list[dict]) -> None:
    print("\n=== owner's board cross-check ===")
    path = os.path.join(DUMPS, "board_state.json")
    if not os.path.exists(path):
        print("  board_state.json absent -- skipped")
        return
    with open(path, encoding="utf-8") as fh:
        board = json.load(fh)
    by_group: dict[str, list[dict]] = collections.defaultdict(list)
    for r in nodes:
        by_group[r["group_id"]].append(r)

    grand: collections.Counter = collections.Counter()
    tot9 = tot13 = 0
    for chr_id, st in sorted(board.items()):
        unlocked = st.get("nodes", [])
        s9 = s13 = 0
        items: collections.Counter = collections.Counter()
        for g in unlocked:
            # characterIds is REPEATED: empty = every character, else must list this one
            mine = [
                r for r in by_group.get(g, [])
                if not char_ids(r) or chr_id in char_ids(r)
            ]
            for r in mine:
                s9 += f(r, "9", 0) or 0
                s13 += f(r, "13", 0) or 0
                item, amt = node_cost(r)
                if item:
                    items[item] += amt
        tot9 += s9
        tot13 += s13
        grand.update(items)
        if unlocked:
            print(f"  {chr_id}: {len(unlocked):>2} nodes  f9={s9:>3} f13={s13:>3}  {dict(items)}")
    print(f"  ALL CHARACTERS: f9={tot9} f13={tot13}")
    print(f"  items spent: {dict(grand)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", action="store_true", help="only the owner's-board check")
    args = ap.parse_args()

    nodes = load("SkillTreeNode")
    if args.owner:
        section_owner(nodes)
        return 0
    section_fields(nodes)
    section_f9_f13(nodes)
    section_costs(nodes)
    section_cost_vs_fields(nodes)
    section_branches(nodes)
    section_full_tree(nodes)
    section_budget()
    section_owner(nodes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
