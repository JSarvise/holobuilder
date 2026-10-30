"""Per-character Active-skill cooldown and probability modifiers from the Holomem Board.

`GAME-DATA-FINDINGS` §9.3c: no card can alter activation probability or cooldown — those come
ONLY from the board. `skilltree.json` locates them precisely: branch **B** ("only while this
member is in the unit") carries three `Act.CD` nodes and ten `Act.Prob` nodes, and nothing else
in the tree touches either. `board_state.json` says which of them a character has unlocked.

Values are stored as `value/10` percent, per the nodes' own description string
("Active Skill Activation Frequency UP [value/10]%"), so a stored 40 is 4.0 %.

Cooldown reduction DIVIDES (§13): `cooldown / (1 + reduction)`.

⚠ The probability side is NOT closed. Whether `Act.Prob` adds percentage points to the tier or
scales it is the same unverified interpretation `scoring_models.ACTIVATION_RATE_MODELS` exists
to switch between (SCOR-05), so this module reports the raw total and refuses to pick.

    proxyenv/Scripts/python board_active_mods.py [chr-id ...]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TREE = os.path.join(HERE, "dumps", "skilltree.json")
STATE = os.path.join(HERE, "dumps", "board_state.json")


def node_effects():
    """{node_id: ("cooldown"|"probability", fraction)} for every branch-B timing node."""
    tree = json.load(open(TREE, encoding="utf-8"))
    out = {}
    for node_id, node in tree["nodes"].items():
        effect = node.get("e") or {}
        kind = effect.get("t")
        if kind == "Act.CD":
            out[node_id] = ("cooldown", effect["v"] / 10.0 / 100.0)
        elif kind == "Act.Prob":
            out[node_id] = ("probability", effect["v"] / 10.0 / 100.0)
    return out


def modifiers(character_ids=None):
    """{chr_id: {"cooldown": r, "probability": p, "nodes": [...]}}.

    A character absent from `board_state.json` was never captured; it is reported with zeros
    and `captured: False` rather than silently treated as an empty board — "we did not look"
    and "there is nothing there" are different facts."""
    effects = node_effects()
    state = json.load(open(STATE, encoding="utf-8"))
    ids = character_ids or sorted(state)
    out = {}
    for chr_id in ids:
        entry = state.get(chr_id)
        acc = {"cooldown": 0.0, "probability": 0.0, "nodes": [],
               "captured": entry is not None}
        for node_id in (entry or {}).get("nodes", []):
            hit = effects.get(node_id)
            if hit:
                acc[hit[0]] += hit[1]
                acc["nodes"].append(node_id)
        out[chr_id] = acc
    return out


def main():
    wanted = [a for a in sys.argv[1:] if not a.startswith("--")]
    mods = modifiers(wanted or None)
    print(f"{'character':14} {'cooldown':>9} {'probability':>12}  nodes")
    for chr_id, m in mods.items():
        if not m["captured"]:
            print(f"{chr_id:14} {'--':>9} {'--':>12}  not captured")
            continue
        print(f"{chr_id:14} {m['cooldown'] * 100:>8.1f}% {m['probability'] * 100:>11.1f}%"
              f"  {' '.join(m['nodes']) or '-'}")


if __name__ == "__main__":
    main()
