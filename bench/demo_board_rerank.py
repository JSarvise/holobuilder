"""End-to-end #3: the "top candidates -> optimise boards -> re-rank" flow, with
the phase-B cut making it cheap and provably lossless.

For each candidate team the expensive step is `optimize_team_boards` (six
per-character connected-subgraph searches). The cheap upper bound is the team's
board-off Unit Score plus the sum of every positive node value — no search, just
the valuation, and a true bound because the optimiser buys a subset of those
nodes under budget. Rank by the bound, optimise only the top-N, re-rank. The run
verifies against optimising EVERY team that the filtered top-10 is the exact
top-10.

Run: PYTHONPATH=src python bench/demo_board_rerank.py
"""

import json
import random
import time
import dataclasses
from pathlib import Path

from hbuilder.domain.loader import load_cards
from hbuilder.domain.scoring_config import load_scoring_config, DEFAULT_CONFIG_PATH
from hbuilder.domain.collection import (
    collection_stats, stat_basis_from_mapping, caps_by_rarity_from_mapping,
    skill_ladder_from_mapping, restate_card_skills,
)
from hbuilder.domain.card import Stats
from hbuilder.domain.team import Team, MEMBER_COUNT
from hbuilder.domain.scoring import score_with_breakdown, deck_screen_unit_score, UNIT_SCORE_K
from hbuilder.domain.board_plan import (
    skill_tree_from_dumps, model_for_character, node_effect_ids_from_master,
    power_node_values, optimize_team_boards,
)

D = Path("tools/recon/proxy/dumps")
SAMPLE = 150
TOP_K = 10
BUDGET = 200
PLAYER_LEVEL = 999
SEED = 20260813


def main() -> None:
    basis = stat_basis_from_mapping(json.load(open(D / "card_level_curve.json")))
    caps = caps_by_rarity_from_mapping(json.load(open(D / "card_caps.json")))
    ladder = skill_ladder_from_mapping(json.load(open(D / "skill_ladder.json")))
    cat = {c.id: c for c in load_cards("data/cards.json")}
    cfg_off = dataclasses.replace(load_scoring_config(DEFAULT_CONFIG_PATH), board=None, slot_order_rule="free")
    skilltree = json.load(open(D / "skilltree.json"))
    master = json.load(open(D / "master_SkillTreeNode.json"))
    effects = node_effect_ids_from_master(master)

    def maxed(cid):
        lv = {cid: {"level": 80, "bloom": 0, "limit_break": 0}}
        s, r = collection_stats([cid], {"cards": lv, "player": {}}, caps, basis)
        c = cat[cid]
        if cid in s:
            c = dataclasses.replace(c, stats=Stats(*s[cid]))
        st = r.states.get(cid)
        c, _ = restate_card_skills(c, st.bloom if st else None, ladder.get(cid, {}))
        return c

    cards = {cid: maxed(cid) for cid in cat}
    by_holomem = {}
    for c in cards.values():
        by_holomem.setdefault(c.member, []).append(c)

    # per-model tree cache; the model a character uses
    tree_cache: dict[str, object] = {}
    def tree_for(card):
        chr_id = "chr-" + card.id.split("-")[1]
        model = model_for_character(skilltree, chr_id)
        if model is None:
            return None
        if model not in tree_cache:
            tree_cache[model] = skill_tree_from_dumps(skilltree, master, model)
        return tree_cache[model]

    rng = random.Random(SEED)
    holomems = list(by_holomem)
    teams, seen = [], set()
    while len(teams) < SAMPLE:
        picks = rng.sample(holomems, MEMBER_COUNT + 1)
        members = tuple(rng.choice(by_holomem[h]) for h in picks[:MEMBER_COUNT])
        leader = rng.choice(by_holomem[picks[MEMBER_COUNT]])
        key = (leader.id, tuple(sorted(m.id for m in members)))
        if key in seen:
            continue
        seen.add(key)
        teams.append((leader, members))

    # board-off baseline + cheap upper bound (valuation sum, no search)
    base_us, bounds = [], []
    for leader, members in teams:
        ds = deck_screen_unit_score(score_with_breakdown(Team(leader, members), cfg_off))
        base_us.append(ds.unit_score)
        headroom = 0.0
        for card, is_leader in [(leader, True), *[(m, False) for m in members]]:
            vals = power_node_values(effects, is_leader=is_leader, own_card=card,
                                     member_cards=members, bonus_permil=ds.score_bonus_permil,
                                     overall_power=ds.overall_power, k_constant=UNIT_SCORE_K)
            headroom += sum(v for v in vals.values() if v > 0)
        bounds.append(ds.unit_score + headroom)

    def optimise(i):
        leader, members = teams[i]
        ds_bonus, ds_power = None, None
        # reuse the baseline breakdown values via a fresh score (cheap vs the search)
        ds = deck_screen_unit_score(score_with_breakdown(Team(leader, members), cfg_off))
        plan = optimize_team_boards(
            leader, members, tree_for=tree_for, budget_for=lambda c: BUDGET,
            node_effect_ids=effects, overall_power=ds.overall_power,
            bonus_permil=ds.score_bonus_permil, k_constant=UNIT_SCORE_K,
            player_level=PLAYER_LEVEL,
        )
        return base_us[i] + plan.total_gain

    # EXHAUSTIVE: optimise every team
    t0 = time.perf_counter()
    exhaustive = [optimise(i) for i in range(len(teams))]
    exhaustive_s = time.perf_counter() - t0
    true_top = sorted(range(len(teams)), key=lambda i: exhaustive[i], reverse=True)[:TOP_K]

    print(f"teams: {len(teams)}  | exhaustive optimise: {exhaustive_s:.2f}s "
          f"({len(teams)} team-optimisations)")
    for n_frac in (0.10, 0.20, 0.34):
        n = max(TOP_K, int(len(teams) * n_frac))
        cand = sorted(range(len(teams)), key=lambda i: bounds[i], reverse=True)[:n]
        opt = {i: optimise(i) for i in cand}
        cand_top = sorted(cand, key=lambda i: opt[i], reverse=True)[:TOP_K]
        match = set(cand_top) == set(true_top)
        # soundness: no team's optimised score exceeds its cheap bound
        sound = all(exhaustive[i] <= bounds[i] + 1e-6 for i in range(len(teams)))
        print(f"  N={n:4} ({n_frac:.0%})  top-{TOP_K} match: {match}  bound sound: {sound}  "
              f"optimisations: {n} vs {len(teams)}")

    win = true_top[0]
    leader, members = teams[win]
    print(f"\nbest after board optimisation: {leader.member} (leader) + "
          f"{[m.member for m in members]}")
    print(f"  board-off {base_us[win]:,.0f} -> optimised {exhaustive[win]:,.0f} "
          f"(+{100*(exhaustive[win]-base_us[win])/base_us[win]:.1f}%)")


if __name__ == "__main__":
    main()
