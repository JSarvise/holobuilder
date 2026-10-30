"""node_price.py -- the FULL price of a Holomem Board node.

A node's detail popup in the game shows THREE costs side by side (e.g. 50 / 10 / 4),
not one item plus a point cost. `SkillTreeNode` field 1000 (`consumptions_`) is a
REPEATED `Consumption`, and 89 of the 323 rows carry TWO entries. `tree_cost.py`
reads only `raw[0]`, so it drops the second one and reports the "-2" Hexa items as
unused (0/323). They are used on 89 rows.

Two traps this tool avoids:
  1. `master_cache.rows()` decodes every value with errors="replace", turning any
     byte >= 0x80 into U+FFFD -- and the quantity varint sits exactly there. Read
     the SQLCipher DB directly and keep field 1000 as bytes.
  2. field 1000 is repeated. Iterate every entry, never `[0]`.

Consumption submessage (field names as in the master dump of the community tool
holodori.best, dumps/holodori/main_SkillTreeNode.json):
    1 resourceType (3 == RESOURCE_TYPE_ITEM)   2 resourceId   3 quantity

Read-only. Touches nothing under data/.

Usage:
    python node_price.py                # full report
    python node_price.py --currencies   # the three currencies and their Lang names
    python node_price.py --node R-034   # one group's price vector
    python node_price.py --owner        # the account's board vs a full board
    python node_price.py --crosscheck   # re-derive vs holodori.best's dump + vs tree_cost.py
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
sys.path.insert(0, HERE)

# The four cube/hexa families, in group-prefix order.
FAMILIES = {
    "R": ("leader", "Red"),
    "B": ("card", "Blue"),
    "G": ("allmember", "Green"),
    "Y": ("content", "Yellow"),
    "S": (None, None),
}
POINT_ITEM = "<skill-tree points>"  # not an Item row; a per-character point pool


# --------------------------------------------------------------------------
# stdlib protobuf walk (no protobuf package dependency)
# --------------------------------------------------------------------------

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    val = shift = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def decode_protobuf(buf: bytes) -> dict[int, list[Any]]:
    out: dict[int, list[Any]] = collections.defaultdict(list)
    i = 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            val, i = _varint(buf, i)
        elif wire == 2:
            ln, i = _varint(buf, i)
            val, i = buf[i:i + ln], i + ln
        elif wire == 5:
            val, i = int.from_bytes(buf[i:i + 4], "little"), i + 4
        elif wire == 1:
            val, i = int.from_bytes(buf[i:i + 8], "little"), i + 8
        else:
            raise ValueError(f"unsupported wire type {wire} at {i}")
        out[field].append(val)
    return dict(out)


def txt(v: Any) -> Any:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v


# --------------------------------------------------------------------------
# loading -- straight from the SQLCipher DB, binary fields kept as bytes
# --------------------------------------------------------------------------

def load(table: str) -> list[dict]:
    import master_cache as mc  # noqa: PLC0415
    from master_db import decode_record  # noqa: PLC0415

    db = mc.open_table(table)
    out = []
    for r in db.query(f"SELECT * FROM {table}"):
        rec: dict[str, Any] = {}
        for k, v in r.items():
            if k == "data":
                rec["data"] = {fn: list(vs) for fn, vs in decode_record(v).items()}
            else:
                rec[k] = txt(v)
        out.append(rec)
    db.close()
    return out


def lang(table: str) -> dict[str, str]:
    """{id: english text}. EVERY Lang table's internal SQL table is named `Lang`.

    The row's `data` blob is protobuf {1: id, 1000: text}; the text follows the
    id and a 2-byte tag, so decode the blob rather than slicing the string.
    """
    import master_cache as mc  # noqa: PLC0415

    db = mc.open_table(table)
    out = {}
    for r in db.query("SELECT * FROM Lang"):
        msg = decode_protobuf(r["data"])
        vals = [txt(v) for vs in msg.values() for v in vs if isinstance(v, bytes)]
        key = txt(r["id"])
        body = [v for v in vals if v != key]
        out[key] = body[-1] if body else ""
    db.close()
    return out


def f(row: dict, num: int, default: Any = None) -> Any:
    v = row["data"].get(num)
    return txt(v[0]) if v else default


def char_ids(row: dict) -> list[str]:
    """field 3, repeated `characterIds`. EMPTY means "every character"."""
    return [txt(v) for v in row["data"].get(3, [])]


# --------------------------------------------------------------------------
# the price vector
# --------------------------------------------------------------------------

def price(row: dict) -> dict[str, int]:
    """A node's FULL price as {currency_id: quantity}. Points included.

    Reads EVERY entry of the repeated field 1000, which is the whole point of
    this tool -- `tree_cost.py::node_cost` reads only the first.
    """
    out: dict[str, int] = {}
    pts = f(row, 9, 0) or 0
    if pts:
        out[POINT_ITEM] = pts
    for blob in row["data"].get(1000, []):
        msg = decode_protobuf(blob)
        item = txt(msg.get(2, [b""])[0])
        qty = msg.get(3, [0])[0]
        if item:
            out[item] = out.get(item, 0) + qty
    return out


def add(acc: collections.Counter, vec: dict[str, int]) -> None:
    for k, v in vec.items():
        acc[k] += v


def fmt(vec: dict[str, int], names: dict[str, str]) -> str:
    parts = []
    for k, v in sorted(vec.items(), key=lambda kv: (kv[0] != POINT_ITEM, kv[0])):
        parts.append(f"{v} {names.get(k, k)}")
    return "  +  ".join(parts) if parts else "(free)"


def item_names() -> dict[str, str]:
    names = {POINT_ITEM: "points"}
    try:
        li = lang("LangItem_Eng")
        it = load("Item")
        for r in it:
            iid = f(r, 1, "")
            if "skill_tree" in str(iid):
                names[iid] = li.get(f(r, 2, ""), iid)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] item names unavailable: {exc}", file=sys.stderr)
    return names


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

def section_currencies(nodes: list[dict], names: dict[str, str]) -> None:
    print("=== the currencies a node is actually priced in ===")
    used: collections.Counter = collections.Counter()
    for r in nodes:
        for k in price(r):
            used[k] += 1
    for k, n in sorted(used.items(), key=lambda kv: (kv[0] != POINT_ITEM, kv[0])):
        print(f"  {k:<32} {names.get(k, '?'):<14} charged on {n:>3}/{len(nodes)} rows")
    print("\n  prefix -> family (the group-id letter IS the colour):")
    tab: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in nodes:
        pfx = str(f(r, 1, "?")).split("-")[0]
        for k in price(r):
            if k != POINT_ITEM:
                tab[pfx][k] += 1
    for pfx in sorted(tab):
        fam = ", ".join(f"{names.get(k, k)} x{n}" for k, n in sorted(tab[pfx].items()))
        print(f"    {pfx}-*  ->  {fam}")
    for pfx in sorted(FAMILIES):
        if pfx not in tab:
            print(f"    {pfx}-*  ->  (no item cost at all)")


def section_shape(nodes: list[dict]) -> None:
    print("\n=== field 1000 is REPEATED -- the bug tree_cost.py has ===")
    hist = collections.Counter(len(r["data"].get(1000, [])) for r in nodes)
    for n, c in sorted(hist.items()):
        print(f"  {c:>3} rows carry {n} consumption entr{'y' if n == 1 else 'ies'}")
    print("  reading only entry[0] loses every second entry -> the Hexa cost vanishes.")


def section_price_table(nodes: list[dict], names: dict[str, str]) -> None:
    print("\n=== distinct price vectors ===")
    tab: collections.Counter = collections.Counter()
    for r in nodes:
        tab[tuple(sorted(price(r).items()))] += 1
    for vec, n in sorted(tab.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4} rows   {fmt(dict(vec), names)}")


def section_full_board(nodes: list[dict], names: dict[str, str]) -> None:
    print("\n=== the price of a COMPLETE board, per character ===")
    print("  (f3 characterIds is repeated; empty = every character. 3 groups carry")
    print("   one row per character, so no board holds all 323 rows.)")
    every = sorted({c for r in nodes for c in char_ids(r)})
    buckets: dict[tuple, list[str]] = collections.defaultdict(list)
    for c in every:
        acc: collections.Counter = collections.Counter()
        for r in nodes:
            if not char_ids(r) or c in char_ids(r):
                add(acc, price(r))
        buckets[tuple(sorted(acc.items()))].append(c)
    for vec, chars in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"\n  {len(chars)} characters ({chars[0]}...):")
        for k, v in sorted(dict(vec).items(), key=lambda kv: (kv[0] != POINT_ITEM, kv[0])):
            print(f"      {v:>6}  {names.get(k, k)}")


def section_node(nodes: list[dict], gid: str, names: dict[str, str]) -> None:
    print(f"\n=== group {gid} ===")
    eff = None
    try:
        eff = lang("LangGeneratedSkillTreeEffect_Eng")
    except Exception:  # noqa: BLE001
        pass
    typ = {1: "socket", 2: "Leader Skill", 3: "Member Skill",
           4: "Overall Boost", 5: "content"}
    for r in nodes:
        if f(r, 1) != gid:
            continue
        e = f(r, 6, "")
        desc = (eff or {}).get(f"la-generated-{e}-description", "") if e else ""
        print(f"  number={f(r, 2)}  type={f(r, 4)} ({typ.get(f(r, 4), '?')})  "
              f"grade={f(r, 5)}  chars={char_ids(r) or 'ALL'}")
        print(f"  effect  : {e}")
        print(f"  text    : {desc.strip()}")
        print(f"  PRICE   : {fmt(price(r), names)}")


def section_owner(nodes: list[dict], names: dict[str, str]) -> None:
    print("\n=== the owner's board ===")
    path = os.path.join(DUMPS, "board_state.json")
    if not os.path.exists(path):
        print("  board_state.json absent -- skipped")
        return
    with open(path, encoding="utf-8") as fh:
        board = json.load(fh)
    by_group: dict[str, list[dict]] = collections.defaultdict(list)
    for r in nodes:
        by_group[str(f(r, 1))].append(r)
    grand: collections.Counter = collections.Counter()
    for chr_id, st in sorted(board.items()):
        acc: collections.Counter = collections.Counter()
        for g in st.get("nodes", []):
            for r in by_group.get(g, []):
                if not char_ids(r) or chr_id in char_ids(r):
                    add(acc, price(r))
        grand.update(acc)
        print(f"  {chr_id}: {len(st.get('nodes', [])):>2} nodes  {fmt(dict(acc), names)}")
    print(f"\n  ALL: {fmt(dict(grand), names)}")


def section_crosscheck(nodes: list[dict]) -> None:
    print("\n=== cross-check vs HolodoriDB's own master dump (real field names) ===")
    path = os.path.join(DUMPS, "holodori", "main_SkillTreeNode.json")
    if not os.path.exists(path):
        print("  holodori dump absent -- skipped")
        return
    with open(path, encoding="utf-8") as fh:
        ext = json.load(fh)
    theirs: dict[tuple, dict] = {}
    for r in ext:
        d = r["data"]
        v = {POINT_ITEM: d.get("consumptionSkillTreePointQuantity", 0)}
        for c in d.get("consumptions", []):
            v[c["resourceId"]] = int(c["quantity"])
        theirs[(r["group_id"], int(r["number"]))] = {k: x for k, x in v.items() if x}
    mine = {(str(f(r, 1)), int(f(r, 2))): price(r) for r in nodes}
    bad = [k for k in mine if mine[k] != theirs.get(k)]
    print(f"  rows: ours {len(mine)}  theirs {len(theirs)}  mismatches: {len(bad)}")
    for k in bad[:10]:
        print(f"    {k}  ours={mine[k]}  theirs={theirs.get(k)}")
    if not bad:
        print("  EXACT AGREEMENT on every row's full price vector.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--currencies", action="store_true")
    ap.add_argument("--node", metavar="GROUP")
    ap.add_argument("--owner", action="store_true")
    ap.add_argument("--crosscheck", action="store_true")
    args = ap.parse_args()

    nodes = load("SkillTreeNode")
    names = item_names()
    any_flag = args.currencies or args.node or args.owner or args.crosscheck
    if args.currencies or not any_flag:
        section_currencies(nodes, names)
        section_shape(nodes)
    if args.node:
        section_node(nodes, args.node, names)
    if not any_flag:
        section_price_table(nodes, names)
        section_node(nodes, "R-034", names)
        section_full_board(nodes, names)
    if args.owner or not any_flag:
        section_owner(nodes, names)
    if args.crosscheck or not any_flag:
        section_crosscheck(nodes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
