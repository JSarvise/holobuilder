"""Convert captured account board state into a `domain/board.py` board file.

`dumps/board_state.json` records what the account has UNLOCKED and what it has
PLACED, in the game's own vocabulary:

    {"chr-00012": {"nodes": ["G-001", ...], "slots": {"S-001": "card-...."}}}

`hbuilder.domain.board.load_board` wants something else entirely — effect ids,
a Connect multiplier, keyed by **holomem name**:

    {"characters": {"Kiryu Coco": [{"node": "G-001",
                                    "effects": ["skill_tree_effect-..."],
                                    "connect_multiplier": 2.5}]}}

This script is the join between the two.

## The three joins, and where each side comes from

| join | from | to | source |
|---|---|---|---|
| 1 | `chr-NNNNN` | holomem name | `skilltree.json["members"]`, 54 rows |
| 2 | group id | effect id(s) | `master_SkillTreeNode.json`, field `1` = group, field `6` = effect, field `3` = the characters the row applies to |
| 3 | filled socket | `connect_multiplier` | `skilltree.json["card_connect"]` -> `["connect"]` -> `["extents"]`, against `["models"]` node coordinates (see the level caveat) |

### Join 2 — the multi-row groups

323 rows over 152 group ids. 137 groups have exactly one row. The rest:

* `G-008`, `G-011`, `G-021` carry **54 rows each**, one per character, and each
  row's field `3` names that one character. The effect targets that
  character's own grouping (`character_grouping-grp-gen_0-always`), so the row
  MUST be selected by character or the board pays the wrong generation.
* 12 `Y-*` groups carry **2 rows**: a default with no field `3`, and a variant
  whose field `3` lists specific characters (`-solo` vs `-solo-fuwamoko`).

So the rule is: prefer the row whose field `3` contains this character;
otherwise the row with no field `3`. A group where neither exists is reported
as unresolved and NOT emitted (a silently dropped node is what `board.BoardError`
exists to prevent).

### Join 3 — geometry is exact, the LEVEL depends on bloom

`skilltree.json["models"]` gives every node's `[x, y]` on that character's tree
model (4 models, 152 nodes each). `["extents"]` gives each Connect shape as
`(dx, dy)` offsets from the socket, so the covered set is exact arithmetic:

    covered = {(sx + dx, sy + dy) for dx, dy in extents[area]}

(`center_area-005` from `S-001` at `(0,0)` lands exactly on `G-001..G-005`.)

Every Connect effect has `levels: [{v: 1500}, {v: 2000}]` (level 2 = level 1 + 500
permil on all 20 effects), and the level follows the placed card's bloom through master
`CardPotential`:

    rarity_3  bloom 5 -> effect_type 4, value 100   (all params +100 permil)
    rarity_4  bloom 5 -> effect_type 5, value 2     (CONNECT bonus -> LEVEL 2)
    rarity_5  bloom 5 -> effect_type 5, value 2     (CONNECT bonus -> LEVEL 2)

(`hbuilder.domain.collection.connect_bonus_level(bloom, rarity)`). No 3★ has a Connect
effect at all. `board_state.json` records the placed card, not its bloom, so the bloom
comes from the collection.

### `--connect-level`

| setting | what it does | `basis` |
|---|---|---|
| **`1`** (default) | base level on every socket | `actual`, all sockets `forced` |
| `2` | Bloom-5 level everywhere | `actual`, all `forced` |
| `auto` | per socket, from `--collection`'s per-card bloom | see below |

Under `auto` each socket is tagged, and the tag decides the document:

* `collection` — a bloom from the collection: the account.
* `maxed-default` — inherited from `card_default: "max"`: taints the whole document as
  `basis: "upper-bound"` and raises a problem (socket cards below bloom 5 at level 2
  overshoot the real board).
* `assumed-base` — nothing said (`None` is "not told", never "level 1"), so the base is
  written and tagged. `"max"` with no `card_caps.json` lands here too.

Two sockets never overlap on any model: the farthest 4-neighbour reach of a Connect
shape is 3 cells and sockets sit 7 apart. Coverage is still summed per node, and the
report line stays, so an overlap on a future layout would be visible.

## Two further socket rules, from the game

* A card in a socket may ALSO be in the team, including as Leader, so the board can be
  optimised after a team is chosen.
* A card may occupy only ONE socket across the whole account: socket assignment is a
  global matching problem. This converter does not check it: `board_state.json` records
  placements as they are.

Writes `dumps/board.json` only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

# The domain parser validates the converter's output, so a file `load_board` would reject
# fails at build time, not at solve time.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402
from hbuilder.domain.board import BoardError, board_from_mapping  # noqa: E402
from hbuilder.domain.collection import (  # noqa: E402
    MAX,
    caps_by_rarity_from_mapping,
    connect_bonus_level,
    resolve_card_state,
)
from hbuilder.errors import MissingInput  # noqa: E402

# How a resolved Connect level was arrived at, least trustworthy last. Recorded per socket:
# it separates the account from a hypothetical one.
BASIS_MANUAL = "collection"  # a bloom the user typed -- account truth
BASIS_MAXED = "maxed-default"  # inherited from card_default: "max" -- upper bound
BASIS_ASSUMED = "assumed-base"  # nothing said; level 1, the shipped reading
BASIS_FORCED = "forced"  # --connect-level 1|2 given explicitly

_SOCKET_PREFIX = "S-"
# The master cache's field-bag key inside a `SkillTreeNode` row (the numbered protobuf
# fields live under it); named so a fixture can build a row in the real shape.
ROW_DATA_KEY = "data"


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _effect_rows_by_group(node_master: list[dict]) -> dict[str, list[dict]]:
    """`SkillTreeNode` rows bucketed by group id (field `1` / `group_id`)."""
    rows: dict[str, list[dict]] = defaultdict(list)
    for row in node_master:
        group = row.get("group_id")
        data = row.get(ROW_DATA_KEY) or {}
        # Field 1 is the group id; `group_id` is the cache's copy. Checked, since a
        # divergence would move an effect onto the wrong node.
        field_1 = (data.get("1") or [None])[0]
        if field_1 is not None and field_1 != group:
            raise MissingInput(f"SkillTreeNode row disagrees with itself: {group} vs {field_1}")
        rows[group].append(row)
    return rows


def _select_row(rows: list[dict], chr_id: str) -> tuple[dict | None, str]:
    """The one `SkillTreeNode` row that applies to `chr_id`, and why.

    Field `3` is the character filter. A row that has it applies ONLY to those
    characters; a row without it is the default."""
    specific = [r for r in rows if chr_id in (r[ROW_DATA_KEY].get("3") or [])]
    if len(specific) == 1:
        return specific[0], "character-specific"
    if len(specific) > 1:
        return None, f"{len(specific)} rows both name {chr_id}"
    default = [r for r in rows if not r[ROW_DATA_KEY].get("3")]
    if len(default) == 1:
        return default[0], "default"
    if not default:
        return None, f"no row applies to {chr_id} and no default row exists"
    return None, f"{len(default)} default rows"


def _rarity_of(card_id: str) -> str | None:
    """`card-04001-5-uniq-0042-00` -> `"5"`. None if the id is not that shape."""
    parts = card_id.split("-")
    return parts[2] if len(parts) > 2 and parts[2].isdigit() else None


class ConnectLevels:
    """Resolves each socket's Connect level, and remembers on what authority.

    `level=1|2` forces every socket. `level=None` means AUTO: derive from the
    collection's per-card bloom through `collection.connect_bonus_level`, the
    rule read from master `CardPotential` (bloom 5 on a 4★/5★ buys
    `effect_type 5` = Connect level 2; a 3★ gets stats instead and has no
    Connect effect at all).

    ⚠ A bloom inherited from `card_default: "max"` describes a hypothetical maxed account
    and overshoots any socket card below bloom 5, so it is recorded as `maxed-default`
    and taints the whole document as an UPPER BOUND."""

    def __init__(
        self,
        level: int | None,
        collection: dict | None,
        caps_by_rarity: dict | None,
    ) -> None:
        self.forced = level
        self.default = (collection or {}).get("card_default")
        self.entries = (collection or {}).get("cards") or {}
        self.caps_by_rarity = caps_by_rarity or {}
        self.basis_counts: dict[str, int] = defaultdict(int)

    def for_card(self, card_id: str) -> tuple[int, str]:
        """`(level, basis)` for the card sitting in one socket."""
        if self.forced is not None:
            self.basis_counts[BASIS_FORCED] += 1
            return self.forced, BASIS_FORCED

        rarity = _rarity_of(card_id)
        caps = self.caps_by_rarity.get(rarity) if rarity else None
        state = resolve_card_state(card_id, self.entries.get(card_id), self.default, caps)
        derived = connect_bonus_level(state.bloom, rarity)

        if derived is None:
            # `None` is "not told us", never "level 1" -- but a level has to be
            # written, so the base is used and the basis says it was assumed.
            self.basis_counts[BASIS_ASSUMED] += 1
            return 1, BASIS_ASSUMED

        if state.source in ("default", MAX):
            self.basis_counts[BASIS_MAXED] += 1
            return derived, BASIS_MAXED

        self.basis_counts[BASIS_MANUAL] += 1
        return derived, BASIS_MANUAL

    @property
    def is_upper_bound(self) -> bool:
        """True when any socket's level came from a maxed default, so the
        document describes a hypothetical account and not this one."""
        return self.basis_counts.get(BASIS_MAXED, 0) > 0


def _connect_multipliers(
    chr_id: str,
    state: dict,
    members: dict[str, dict],
    models: dict[str, dict],
    extents: dict[str, list],
    connect: dict[str, dict],
    card_connect: dict[str, str],
    levels: ConnectLevels,
    problems: list[str],
) -> tuple[dict[str, float], list[str], int]:
    """`{node_id: multiplier}` for every node covered by a filled socket.

    Returns the map, the human notes for the report, and how many sockets were
    resolved."""
    notes: list[str] = []
    slots = state.get("slots") or {}
    if not slots:
        return {}, notes, 0

    model_name = members[chr_id]["model"]
    positions = models.get(model_name)
    if positions is None:
        problems.append(f"{chr_id}: unknown tree model {model_name!r}")
        return {}, notes, 0

    # Permil is accumulated per node BEFORE becoming a multiplier, so the
    # two-socket overlap rule is stated in one place (see the module docstring).
    permil_by_node: dict[str, int] = defaultdict(int)
    resolved = 0
    for socket, card_id in sorted(slots.items()):
        socket_pos = positions.get(socket)
        if socket_pos is None:
            problems.append(f"{chr_id}: socket {socket} has no position on {model_name}")
            continue
        effect_id = card_connect.get(card_id)
        if effect_id is None:
            problems.append(
                f"{chr_id}: {socket} holds {card_id}, which has no Connect effect "
                "in card_connect -- its board boost is UNRESOLVED, not zero"
            )
            continue
        effect = connect.get(effect_id)
        if effect is None:
            problems.append(f"{chr_id}: {socket}: unknown connect effect {effect_id!r}")
            continue
        area = extents.get(effect["area"])
        if area is None:
            problems.append(f"{chr_id}: {socket}: unknown connect extent {effect['area']!r}")
            continue
        level, basis = levels.for_card(card_id)
        effect_levels = effect.get("levels") or []
        if len(effect_levels) < level:
            problems.append(
                f"{chr_id}: {socket}: {effect_id} has {len(effect_levels)} "
                f"level(s), Connect level {level} requested ({basis})"
            )
            continue
        permil = int(effect_levels[level - 1]["v"])
        resolved += 1

        sx, sy = socket_pos
        covered = {(sx + dx, sy + dy) for dx, dy in area}
        hit = [
            node
            for node, pos in positions.items()
            if tuple(pos) in covered and not node.startswith(_SOCKET_PREFIX)
        ]
        for node in hit:
            permil_by_node[node] += permil
        notes.append(
            f"{socket}={card_id} {effect['area']} {effect['rare']} "
            f"L{level}[{basis}] +{permil}permil covers {len(hit)} node(s)"
        )

    return (
        {node: 1.0 + permil / 1000.0 for node, permil in permil_by_node.items()},
        notes,
        resolved,
    )


def build(
    board_state: dict,
    skilltree: dict,
    node_master: list[dict],
    *,
    level: int | None,
    collection: dict | None = None,
    caps_by_rarity: dict | None = None,
) -> tuple[dict, list[str], list[str]]:
    """The converted board document, the per-character report, the problems.

    `level=None` derives each socket's Connect level from `collection`; an int
    forces every socket, as `--connect-level 1|2` does."""
    levels = ConnectLevels(level, collection, caps_by_rarity)
    members = {m["chr"]: m for m in skilltree["members"]}
    models = skilltree["models"]
    extents = skilltree["extents"]
    connect = skilltree["connect"]
    card_connect = skilltree["card_connect"]
    rows_by_group = _effect_rows_by_group(node_master)

    report: list[str] = []
    problems: list[str] = []
    characters: dict[str, list[dict]] = {}

    for chr_id in sorted(board_state):
        state = board_state[chr_id]
        member = members.get(chr_id)
        if member is None:
            problems.append(f"{chr_id}: not in skilltree.json members -- SKIPPED ENTIRELY")
            continue
        holomem = member["name"]

        multipliers, connect_notes, sockets_ok = _connect_multipliers(
            chr_id,
            state,
            members,
            models,
            extents,
            connect,
            card_connect,
            levels,
            problems,
        )

        node_ids = list(state.get("nodes") or [])
        rows: list[dict] = []
        effects_emitted = 0
        sockets_seen = 0
        for node_id in node_ids:
            if node_id.startswith(_SOCKET_PREFIX):
                # A socket is an unlock, not an effect. `SkillTreeNode` gives
                # S-001..S-004 no field 6 at all, so this is not a gap.
                sockets_seen += 1
                continue
            group_rows = rows_by_group.get(node_id)
            if not group_rows:
                problems.append(f"{chr_id}/{holomem}: node {node_id} not in SkillTreeNode")
                continue
            row, why = _select_row(group_rows, chr_id)
            if row is None:
                problems.append(f"{chr_id}/{holomem}: node {node_id} ({len(group_rows)} rows): {why}")
                continue
            effect_ids = row[ROW_DATA_KEY].get("6") or []
            if not effect_ids:
                problems.append(
                    f"{chr_id}/{holomem}: node {node_id} ({why} row) carries no "
                    "effect id -- emitted with no effect rather than dropped"
                )
            rows.append(
                {
                    "node": node_id,
                    "effects": list(effect_ids),
                    "connect_multiplier": round(multipliers.get(node_id, 1.0), 6),
                }
            )
            effects_emitted += len(effect_ids)

        characters[holomem] = rows
        boosted = sum(1 for r in rows if r["connect_multiplier"] > 1.0)
        report.append(
            f"{chr_id} {holomem:<24} nodes={len(node_ids):>3} "
            f"resolved={len(rows):>3} effects={effects_emitted:>3} "
            f"sockets_unlocked={sockets_seen} sockets_filled={sockets_ok}/"
            f"{len(state.get('slots') or {})} boosted_nodes={boosted}"
        )
        for note in connect_notes:
            report.append(f"    {note}")

    document = {
        "schema_version": "1",
        "generated_by": "tools/recon/proxy/build_board.py",
        "connect_level": level if level is not None else "auto",
        # What each socket's Connect level rested on, and therefore WHICH
        # ACCOUNT this document describes. `basis` is "actual" only when no
        # socket borrowed a maxed default.
        "connect_basis": dict(sorted(levels.basis_counts.items())),
        "basis": "upper-bound" if levels.is_upper_bound else "actual",
        "characters": characters,
        # chr-id -> holomem name. The board is name-keyed, but `charts.json`
        # singers are chr-ids, so the per-song methods need this to translate.
        "name_by_chr": {chr_id: member["name"] for chr_id, member in members.items()},
    }
    if levels.is_upper_bound:
        problems.append(
            f"UPPER BOUND, NOT THIS ACCOUNT: "
            f"{levels.basis_counts[BASIS_MAXED]} socket(s) took their Connect "
            f"level from card_default:'max'. That default is validated for "
            f"STATS (167/167) and REFUTED for Connect -- level 2 on both of "
            f"Ina's sockets overshoots the +28.73 pp acceptance number by "
            f"+16.31 %, and both are below "
            f"bloom 5. Name those cards' blooms in the collection, or pass "
            f"--connect-level 1, before quoting this board as the account's."
        )
    return document, report, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_board.py",
        description="Convert dumps/board_state.json into a load_board-shaped dumps/board.json",
    )
    parser.add_argument("--state", default=str(paths.DUMPS / "board_state.json"))
    parser.add_argument("--skilltree", default=str(paths.DUMPS / "skilltree.json"))
    parser.add_argument("--node-master", default=str(paths.DUMPS / "master_SkillTreeNode.json"))
    parser.add_argument("--out", default=str(paths.DUMPS / "board.json"))
    parser.add_argument(
        "--connect-level",
        choices=("auto", "1", "2"),
        default="1",
        help="Which Connect effect level each placement sits at. '1' (the "
        "default) forces the base level on every socket, which is what "
        "reproduces the +28.73 pp acceptance number. 'auto' derives it per "
        "socket from --collection's per-card bloom via CardPotential's rule; "
        "it stays opt-in because with a card_default of 'max' it describes a "
        "hypothetical maxed account, not this one (see the module docstring)",
    )
    parser.add_argument(
        "--collection",
        default=str(paths.STATE / "collection.json"),
        help="Read-only. Per-card bloom for --connect-level auto",
    )
    parser.add_argument(
        "--card-caps",
        default=str(paths.DUMPS / "card_caps.json"),
        help="Read-only. Supplies the per-rarity maximum bloom for 'max'",
    )
    parser.add_argument(
        "--cards",
        default=str(paths.DATA / "cards.json"),
        help="Read-only, to warn about a holomem no owned card names",
    )
    args = parser.parse_args(argv)

    board_state = _load(Path(args.state))
    skilltree = _load(Path(args.skilltree))
    node_master = _load(Path(args.node_master))

    level = None if args.connect_level == "auto" else int(args.connect_level)
    collection = None
    caps_by_rarity = None
    if level is None:
        collection_path = Path(args.collection)
        if not collection_path.exists():
            print(
                f"--connect-level auto needs a collection; {collection_path} does not exist",
                file=sys.stderr,
            )
            return 2
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
        caps_path = Path(args.card_caps)
        if caps_path.exists():
            caps_by_rarity = caps_by_rarity_from_mapping(json.loads(caps_path.read_text(encoding="utf-8")))

    document, report, problems = build(
        board_state,
        skilltree,
        node_master,
        level=level,
        collection=collection,
        caps_by_rarity=caps_by_rarity,
    )

    # Verify the artefact, not the counter: parse what is about to be written
    # with the solver's own parser before writing it.
    model = board_from_mapping(document, source=args.out)

    cards_path = Path(args.cards)
    if cards_path.exists():
        card_doc = json.loads(cards_path.read_text(encoding="utf-8"))
        card_members = {c["member"] for c in card_doc["cards"]}
        orphans = sorted(set(document["characters"]) - card_members)
        if orphans:
            problems.append(
                "board holomem with no card in cards.json (scores as zero, "
                "correctly, but check the name join): " + ", ".join(orphans)
            )

    import atomic_io  # noqa: PLC0415 -- beside this script

    atomic_io.write_text(Path(args.out), json.dumps(document, ensure_ascii=False, indent=2) + "\n", "utf-8")

    print("PER CHARACTER")
    for line in report:
        print("  " + line)
    total_nodes = sum(len(v) for v in document["characters"].values())
    total_effects = sum(len(r["effects"]) for v in document["characters"].values() for r in v)
    boosted = sum(1 for v in document["characters"].values() for r in v if r["connect_multiplier"] > 1.0)
    basis = " ".join(f"{k}={v}" for k, v in document["connect_basis"].items())
    print(
        f"TOTAL characters={len(document['characters'])} nodes={total_nodes} "
        f"effects={total_effects} boosted_nodes={boosted} "
        f"connect_level={document['connect_level']} basis={document['basis']}"
        + (f" [{basis}]" if basis else "")
    )
    print(f"PARSED BY load_board: {len(model.characters)} character board(s)")
    if problems:
        print(f"UNRESOLVED ({len(problems)}):", file=sys.stderr)
        for problem in problems:
            print("  " + problem, file=sys.stderr)
    else:
        print("UNRESOLVED (0): every unlocked node resolved to an effect")
    print(f"WROTE {args.out}")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except BoardError as exc:  # pragma: no cover
        print(f"the converted document does not parse: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
