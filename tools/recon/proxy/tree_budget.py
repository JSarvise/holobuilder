"""The Holomem Board point budget: where the points come from, and what the board costs.

Answers two questions in one place:

  1. "how is the holomem level obtained, which sets the number of points?"
        -> CHARACTER level (1..50), not player level. The curve is a table, not a formula:
           budget(L) = sum of CharacterLevel.skill_tree_point_quantity over levels 1..L.
           budget(50) = 361.

  2. "what does each node of the tree cost?"
        -> SkillTreeNode.consumption_skill_tree_point_quantity  (field 9).
           Field 13 is `auto_selection_priority` -- a tie-break rank for the client's
           auto-allocate button, NOT a price. Both names are the client's own, from
           `holodori_sync.py --names SkillTreeNode`.
           One character's board = 154 nodes = 453 points.

  361 < 447. A character at max level can buy about four fifths of its own board.

The account's own numbers settle it. `User/Get` carries, per character, a
`skill_tree_point-chr-NNNNN` resource with the UNSPENT balance. For every character with
any points spent (26 in one account):

        spent(field 9) + unspent  ==  budget(L)  exactly, for some integer L

26 hits out of 26. Field 13 does not do this. That simultaneously proves the cost field,
proves levelling is the only source of points, and makes the character level RECOVERABLE
(invert the curve) rather than hand-entered.

Usage:
    proxyenv/Scripts/python tree_budget.py               # curve + cost + verdict
    proxyenv/Scripts/python tree_budget.py --board       # price dumps/board_state.json
    proxyenv/Scripts/python tree_budget.py --wire <capture.mitm>   # read balances + levels
"""
import collections
import json
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DUMPS = os.path.join(HERE, "dumps")

MAX_LEVEL = 50


# --------------------------------------------------------------------------- master data
def _field(row, num, default=None):
    """One master field by number. `master_cache` keys `data` by INT, not str."""
    v = row["data"].get(num)
    if v is None:
        return default
    return v[0] if len(v) == 1 else v


def level_grants(group_id="level-group-1"):
    """{level: points granted ON reaching that level} from CharacterLevel field 11."""
    import master_cache as mc
    out = {}
    for r in mc.rows("CharacterLevel", "SELECT * FROM CharacterLevel"):
        if _field(r, 1) != group_id:
            continue
        out[_field(r, 2)] = _field(r, 11, 0)
    return out


def budget_curve(group_id="level-group-1"):
    """{level: TOTAL points a character has earned by that level}. budget(50) == 361."""
    grants = level_grants(group_id)
    total, out = 0, {}
    for lv in sorted(grants):
        total += grants[lv]
        out[lv] = total
    return out


def budget(level, curve=None):
    """Points available to a character at `level`."""
    curve = curve or budget_curve()
    if level < 1:
        return 0
    return curve[min(level, max(curve))]


def level_from_points(total, curve=None):
    """Invert the curve: the level(s) whose cumulative budget is exactly `total`.

    Returns a list because the curve is flat wherever a level grants 0 (only level 1)."""
    curve = curve or budget_curve()
    return [lv for lv, v in sorted(curve.items()) if v == total]


# --------------------------------------------------------------------------- node costs
def node_rows():
    import master_cache as mc
    return mc.rows("SkillTreeNode", "SELECT * FROM SkillTreeNode")


def nodes_by_group(rows=None):
    by = collections.defaultdict(list)
    for n in rows or node_rows():
        by[_field(n, 1)].append(n)
    return by


def row_for(group, character_id):
    """The row of a node group that applies to one character.

    323 rows / 152 groups: 137 groups are a single shared row, 3 groups carry one row per
    playable character, and 12 groups carry a shared default PLUS a `character_ids`
    override for chr-04016/chr-04017. Prefer the character-specific row, else the default."""
    if len(group) == 1:
        return group[0]
    default = None
    for r in group:
        ids = _field(r, 3)
        ids = ids if isinstance(ids, list) else ([ids] if ids else [])
        if character_id in ids:
            return r
        if not ids:
            default = r
    return default


def board_cost(character_id, by_group=None, field=9):
    """(node count, total point cost) of one character's WHOLE board. 154 / 453 for all."""
    by_group = by_group or nodes_by_group()
    rows = [row_for(g, character_id) for g in by_group.values()]
    rows = [r for r in rows if r is not None]
    return len(rows), sum(_field(r, field, 0) for r in rows)


def cost_of(node_ids, character_id, by_group=None, field=9):
    """Point cost of a specific set of node-group ids for one character."""
    by_group = by_group or nodes_by_group()
    total = 0
    for gid in node_ids:
        g = by_group.get(gid)
        if not g:
            continue
        r = row_for(g, character_id)
        if r is not None:
            total += _field(r, field, 0)
    return total


# --------------------------------------------------------------------------- the wire
POINT_RE = re.compile(rb"skill_tree_point-chr-(\d{5})")


def unspent_from_capture(mitm_path):
    """{chr-id: unspent skill tree points} read out of a `User/Get` response.

    Wire shape, inside the account's resource list:
        <outer tag> <len 30> 12 1a "skill_tree_point-chr-NNNNN" 18 <varint quantity>
    i.e. field 2 = resource id (string), field 3 = quantity. The quantity follows the id."""
    src = open(os.path.join(HERE, "grpc_decrypt.py"), encoding="utf-8").read().split("def main(")[0]
    gd = types.ModuleType("gd")
    exec(compile(src, "grpc_decrypt.py", "exec"), gd.__dict__)
    from mitmproxy import io as mio
    key = bytes.fromhex(gd.KEY_HEX)
    best = {}
    with open(mitm_path, "rb") as fh:
        for fl in mio.FlowReader(fh).stream():
            if fl.type != "http" or "User/Get" not in fl.request.path:
                continue
            resp = getattr(fl, "response", None)
            if not resp or not resp.raw_content:
                continue
            env = gd.parse_envelope(resp.raw_content)
            if not env:
                continue
            pt = gd.decrypt(env[1], key)
            for m in POINT_RE.finditer(pt):
                p = m.end()
                if p >= len(pt) or pt[p] != 0x18:      # field 3, varint
                    continue
                p += 1
                n = shift = 0
                while p < len(pt):
                    b = pt[p]
                    p += 1
                    n |= (b & 0x7F) << shift
                    shift += 7
                    if not b & 0x80:
                        break
                best["chr-" + m.group(1).decode()] = n
    return best


# --------------------------------------------------------------------------- reports
def board_state():
    with open(os.path.join(DUMPS, "board_state.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _nodes_of(entry):
    return entry if isinstance(entry, list) else entry.get("nodes", [])


def report_curve():
    curve = budget_curve()
    grants = level_grants()
    print("CharacterLevel.skill_tree_point_quantity -- the budget curve (group level-group-1)")
    print("  %-6s %-8s %s" % ("level", "grant", "cumulative budget"))
    prev = None
    for lv in sorted(curve):
        g = grants[lv]
        if g != prev or lv == max(curve):
            print("  %-6d %-8d %d" % (lv, g, curve[lv]))
            prev = g
    print("  budget(50) = %d" % curve[MAX_LEVEL])
    return curve


def report_cost():
    by = nodes_by_group()
    import master_cache as mc
    chars = [_field(c, 1) for c in mc.rows("Character", "SELECT * FROM Character") if _field(c, 21)]
    seen = collections.Counter(board_cost(c, by) for c in chars)
    print("\nSkillTreeNode -- one character's whole board")
    print("  %d rows / %d groups" % (sum(len(v) for v in by.values()), len(by)))
    for (n, pts), how_many in seen.most_common():
        print("  %d of %d playable characters: %d nodes, %d points (field 9)"
              % (how_many, len(chars), n, pts))
    return seen


def report_verdict():
    curve = budget_curve()
    have, need = curve[MAX_LEVEL], board_cost("chr-00001")[1]
    print("\nVERDICT")
    print("  budget at character level 50 : %d" % have)
    print("  cost of the whole board      : %d" % need)
    print("  shortfall                    : %d points (%.1f%% of the board unaffordable by cost)"
          % (need - have, 100.0 * (need - have) / need))


def report_board():
    by = nodes_by_group()
    st = board_state()
    print("\nOwner's board (dumps/board_state.json), priced with field 9")
    print("  %-12s %5s %7s" % ("character", "nodes", "points"))
    tot = 0
    for cid, entry in sorted(st.items()):
        n = _nodes_of(entry)
        c = cost_of(n, cid, by)
        tot += c
        print("  %-12s %5d %7d" % (cid, len(n), c))
    print("  %-12s %5s %7d" % ("TOTAL", "", tot))


def report_wire(mitm_path):
    curve = budget_curve()
    by = nodes_by_group()
    st = board_state()
    unspent = unspent_from_capture(mitm_path)
    print("\nUser/Get balances x board_state -> recovered character level")
    print("  %-12s %6s %8s %6s   %s" % ("character", "spent", "unspent", "total", "level"))
    hits = 0
    for cid in sorted(unspent):
        spent = cost_of(_nodes_of(st.get(cid, [])), cid, by)
        total = spent + unspent[cid]
        lv = level_from_points(total, curve)
        if lv:
            hits += 1
        print("  %-12s %6d %8d %6d   %s"
              % (cid, spent, unspent[cid], total, lv[-1] if lv else "NO EXACT LEVEL"))
    print("  %d/%d totals land exactly on the CharacterLevel cumulative curve" % (hits, len(unspent)))

    # The control: price the same boards with field 13 instead. Only the characters whose
    # two prices DIFFER can discriminate -- on a 5-node board both fields happen to total 5.
    disc = [cid for cid in sorted(unspent)
            if cost_of(_nodes_of(st.get(cid, [])), cid, by, 9)
            != cost_of(_nodes_of(st.get(cid, [])), cid, by, 13)]
    for f in (9, 13):
        ok = sum(1 for cid in disc
                 if level_from_points(cost_of(_nodes_of(st.get(cid, [])), cid, by, f)
                                      + unspent[cid], curve))
        print("  discriminating characters (f9 price != f13 price), priced with field %d: %d/%d"
              % (f, ok, len(disc)))


def main():
    args = sys.argv[1:]
    report_curve()
    report_cost()
    report_verdict()
    if "--board" in args or "--wire" in args:
        report_board()
    if "--wire" in args:
        p = args[args.index("--wire") + 1]
        report_wire(p if os.path.isabs(p) else os.path.join(DUMPS, p))


if __name__ == "__main__":
    main()
