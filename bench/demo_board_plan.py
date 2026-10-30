"""End-to-end demo: a real board plan for one team's leader.

Assembles the real SkillTree, values every node in Unit-Score terms for the
leader (whose red board reaches all five members), and runs the optimiser under
a point budget -> "develop these nodes, for +X Unit Score". This is the
"board plan" step, backend side; board_ceiling.py's phase-B bound is what would
decide which teams reach it.

Run: PYTHONPATH=src python bench/demo_board_plan.py
"""

import json
import dataclasses
from pathlib import Path

from hbuilder.domain.loader import load_cards
from hbuilder.domain.scoring_config import load_scoring_config, DEFAULT_CONFIG_PATH
from hbuilder.domain.collection import (
    collection_stats, stat_basis_from_mapping, caps_by_rarity_from_mapping,
    skill_ladder_from_mapping, restate_card_skills,
)
from hbuilder.domain.card import Stats
from hbuilder.domain.team import Team
from hbuilder.domain.scoring import score_with_breakdown, deck_screen_unit_score, UNIT_SCORE_K
from hbuilder.domain.board_plan import (
    skill_tree_from_dumps, model_for_character, node_effect_ids_from_master,
    power_node_values,
)
from hbuilder.domain.board_optimizer import optimize_board

D = Path("tools/recon/proxy/dumps")
LEADER = "card-04003-5-uniq-0044-00"          # Ina (a strong red/leader board)
MEMBERS = [
    "card-00030-5-uniq-0024-00", "card-00031-5-uniq-0025-00",
    "card-00037-5-uniq-0030-00", "card-06002-5-uniq-0058-00",
    "card-06004-5-uniq-0060-00",
]
BUDGET = 200          # ~a mid-level character's point grant (full tree is 447)
PLAYER_LEVEL = 999    # availability: assume everything is unlockable


def main() -> None:
    basis = stat_basis_from_mapping(json.load(open(D / "card_level_curve.json")))
    caps = caps_by_rarity_from_mapping(json.load(open(D / "card_caps.json")))
    ladder = skill_ladder_from_mapping(json.load(open(D / "skill_ladder.json")))
    cat = {c.id: c for c in load_cards("data/cards.json")}
    cfg_off = dataclasses.replace(load_scoring_config(DEFAULT_CONFIG_PATH), board=None, slot_order_rule="free")
    skilltree = json.load(open(D / "skilltree.json"))
    master = json.load(open(D / "master_SkillTreeNode.json"))

    def maxed(cid):
        lv = {cid: {"level": 80, "bloom": 0, "limit_break": 0}}
        s, r = collection_stats([cid], {"cards": lv, "player": {}}, caps, basis)
        c = cat[cid]
        if cid in s:
            c = dataclasses.replace(c, stats=Stats(*s[cid]))
        st = r.states.get(cid)
        c, _ = restate_card_skills(c, st.bloom if st else None, ladder.get(cid, {}))
        return c

    leader = maxed(LEADER)
    members = tuple(maxed(m) for m in MEMBERS)

    ds_off = deck_screen_unit_score(score_with_breakdown(Team(leader, members), cfg_off))
    print(f"team: leader {leader.member}, members {[m.member for m in members]}")
    print(f"board-off Unit Score: {ds_off.unit_score:,.0f}  "
          f"(power {ds_off.overall_power:,.0f}, bonus {ds_off.score_bonus_permil:.0f}permil)\n")

    chr_id = "chr-" + leader.id.split("-")[1]
    model = model_for_character(skilltree, chr_id)
    tree = skill_tree_from_dumps(skilltree, master, model)
    effects = node_effect_ids_from_master(master)
    values = power_node_values(
        effects,
        is_leader=True, own_card=None, member_cards=members,
        bonus_permil=ds_off.score_bonus_permil, overall_power=ds_off.overall_power,
        k_constant=UNIT_SCORE_K,
    )
    valued = {n: v for n, v in values.items() if v > 0 and n in tree.nodes}
    print(f"tree model {model}: {len(tree.nodes)} nodes, full cost {tree.cost_of(frozenset(tree.nodes))} points; "
          f"{len(valued)} nodes have positive value to this team\n")

    plan = optimize_board(tree, budget=BUDGET, node_value=values, player_level=PLAYER_LEVEL)
    print(f"BOARD PLAN under {BUDGET} points: buy {len(plan.selected) - 1} nodes for "
          f"{plan.spent} points -> +{plan.value:,.0f} Unit Score")
    # show the highest-value bought nodes
    bought = sorted(((values.get(n, 0.0), n) for n in plan.selected if n != "S-001"),
                    reverse=True)
    print("  top nodes bought:")
    for v, n in bought[:10]:
        eff = effects.get(n, ["?"])[0]
        fam = eff.split("-")[1] if "-" in eff else eff
        print(f"    {n:7} +{v:8,.0f}  cost {tree.nodes[n].cost}  ({fam})")
    print(f"\n  Unit Score with this board plan: ~{ds_off.unit_score + plan.value:,.0f} "
          f"(+{100 * plan.value / ds_off.unit_score:.1f}%)")


if __name__ == "__main__":
    main()
