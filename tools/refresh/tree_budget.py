"""The Holomem Board point budget: where the points come from, and what the board costs.

* The budget follows the CHARACTER level (1..50), not the player level, through a table:
  budget(L) = sum of CharacterLevel.skill_tree_point_quantity over levels 1..L;
  budget(50) = 361.
* A node costs SkillTreeNode.consumption_skill_tree_point_quantity (field 9). Field 13 is
  `auto_selection_priority`, a tie-break rank for the client's auto-allocate button, not a
  price. One character's board = 154 nodes = 453 points, so a max-level character can buy
  about four fifths of it.

`User/Get` carries, per character, a `skill_tree_point-chr-NNNNN` resource with the UNSPENT
balance, and on every character with points spent

        spent(field 9) + unspent  ==  budget(L)  exactly, for some integer L

which makes the character level recoverable (invert the curve) rather than hand-entered.

Usage:
    python tree_budget.py               # curve + cost + verdict
    python tree_budget.py --board       # price dumps/board_state.json
"""

import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402
from hbuilder.errors import MissingInput  # noqa: E402

sys.path.insert(0, str(paths.REFRESH_DIR))
DUMPS = str(paths.DUMPS)

MAX_LEVEL = 50


# --------------------------------------------------------------------------- master data
def _field(row, num, default=None):
    """One master field by number. `master_cache` keys `data` by INT, not str."""
    v = row["data"].get(num)
    if v is None:
        return default
    return v[0] if len(v) == 1 else v


def level_grants(group_id="level-group-1"):
    """{level: points granted ON reaching that level} from CharacterLevel field 11.

    Without a SQLCipher library (`master_cache.can_decrypt`), from the synced export
    `holodori/main_CharacterLevel.json` (field 11 is `skillTreePointQuantity`)."""
    import master_cache as mc

    if not mc.can_decrypt():
        return level_grants_from_export(group_id)
    out = {}
    for r in mc.rows("CharacterLevel", "SELECT * FROM CharacterLevel"):
        if _field(r, 1) != group_id:
            continue
        out[_field(r, 2)] = _field(r, 11, 0)
    return out


def level_grants_from_export(group_id="level-group-1", path=None):
    """`level_grants` read from the master export `holodori/main_CharacterLevel.json`."""
    path = path or paths.HOLODORI / "main_CharacterLevel.json"
    try:
        rows = json.loads(Path(path).read_text("utf8"))
    except FileNotFoundError:
        raise MissingInput(
            f"missing {path}: the board budget needs the game's sqlcipher.dll or this synced export"
        ) from None
    out = {}
    for row in rows:
        data = row.get("data") or {}
        if data.get("groupId") != group_id:
            continue
        out[int(data["level"])] = int(data.get("skillTreePointQuantity") or 0)
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
        print(
            "  %d of %d playable characters: %d nodes, %d points (field 9)" % (how_many, len(chars), n, pts)
        )
    return seen


def report_verdict():
    curve = budget_curve()
    have, need = curve[MAX_LEVEL], board_cost("chr-00001")[1]
    print("\nVERDICT")
    print("  budget at character level 50 : %d" % have)
    print("  cost of the whole board      : %d" % need)
    print(
        "  shortfall                    : %d points (%.1f%% of the board unaffordable by cost)"
        % (need - have, 100.0 * (need - have) / need)
    )


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


def main():
    args = sys.argv[1:]
    report_curve()
    report_cost()
    report_verdict()
    if "--board" in args:
        report_board()


if __name__ == "__main__":
    main()
