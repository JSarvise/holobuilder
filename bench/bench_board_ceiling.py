"""Measure the phase-A/B rerank-safe candidate cut (board_ceiling.py).

Generates a large sample of maxed teams, then compares:
  * EXHAUSTIVE — the expensive board pass (score_with_breakdown with the board)
    on every team, ranked by board-on Unit Score -> the true top-K.
  * BOUNDED    — the cheap board-OFF score + phase-B upper bound on every team,
    the expensive board pass on only the top-N by that bound -> its top-K.

It asserts two things a valid cut must satisfy:
  1. no team's board-on Unit Score exceeds its bound (the bound is sound), and
  2. the BOUNDED top-K equals the EXHAUSTIVE top-K (nothing was missed),
and reports how many expensive passes the cut saved.

Run: PYTHONPATH=src python bench/bench_board_ceiling.py
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
from hbuilder.domain.board import load_board
from hbuilder.domain.board_ceiling import (
    holomem_ceilings, unit_score_upper_bound, top_n_candidates,
)

D = Path("tools/recon/proxy/dumps")
SAMPLE = 4000
TOP_K = 10
SEED = 20260813


def main() -> None:
    basis = stat_basis_from_mapping(json.load(open(D / "card_level_curve.json")))
    caps = caps_by_rarity_from_mapping(json.load(open(D / "card_caps.json")))
    ladder = skill_ladder_from_mapping(json.load(open(D / "skill_ladder.json")))
    cat = {c.id: c for c in load_cards("data/cards.json")}
    board = load_board(str(D / "board.json"))
    base_cfg = load_scoring_config(DEFAULT_CONFIG_PATH)
    cfg_on = dataclasses.replace(base_cfg, board=board, slot_order_rule="free")
    cfg_off = dataclasses.replace(base_cfg, board=None, slot_order_rule="free")

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
    # best (highest-stat) card per holomem, for the ceiling's rate nodes
    best_by_holomem: dict[str, object] = {}
    for c in cards.values():
        cur = best_by_holomem.get(c.member)
        if cur is None or c.stat_sum() > cur.stat_sum():
            best_by_holomem[c.member] = c
    # Leader-scope RATE multiplies each member's stat INCLUDING its own
    # member-scope board boost, so the stat basis must include that headroom or
    # a leader with a strong red board (Ina) slips over. Bound it with the
    # per-holomem member-power ceiling added to the base team stat sum.
    max_card_stat = max(c.stat_sum() for c in cards.values())
    probe = holomem_ceilings(board, best_by_holomem, 0.0)  # member_power only
    max_member_board = max((c.member_power for c in probe.values()), default=0.0)
    max_team_stat_sum = MEMBER_COUNT * (max_card_stat + max_member_board)
    t0 = time.perf_counter()
    ceilings = holomem_ceilings(board, best_by_holomem, max_team_stat_sum)
    phase_a_s = time.perf_counter() - t0

    # sample teams: leader (6th, distinct holomem outfit-leader) + 5 distinct-holomem members
    rng = random.Random(SEED)
    by_holomem = {}
    for c in cards.values():
        by_holomem.setdefault(c.member, []).append(c)
    holomems = list(by_holomem)
    teams = []
    seen = set()
    while len(teams) < SAMPLE:
        picks = rng.sample(holomems, MEMBER_COUNT + 1)
        member_hs, leader_h = picks[:MEMBER_COUNT], picks[MEMBER_COUNT]
        members = tuple(rng.choice(by_holomem[h]) for h in member_hs)
        leader = rng.choice(by_holomem[leader_h])
        key = (leader.id, tuple(sorted(m.id for m in members)))
        if key in seen:
            continue
        seen.add(key)
        teams.append((leader, members))

    # EXHAUSTIVE — expensive board pass on all
    t0 = time.perf_counter()
    on_scores = []
    for leader, members in teams:
        ds = deck_screen_unit_score(score_with_breakdown(Team(leader, members), cfg_on))
        on_scores.append(ds.unit_score)
    exhaustive_s = time.perf_counter() - t0
    true_topk = sorted(range(len(teams)), key=lambda i: on_scores[i], reverse=True)[:TOP_K]

    # BOUNDED — cheap board-off pass + bound on all; expensive pass on top-N only
    t0 = time.perf_counter()
    bounds = []
    violations = 0
    worst_slack = 0.0
    for i, (leader, members) in enumerate(teams):
        ds_off = deck_screen_unit_score(score_with_breakdown(Team(leader, members), cfg_off))
        ub = unit_score_upper_bound(
            overall_power_no_board=ds_off.overall_power,
            score_bonus_rate_no_board=ds_off.score_bonus_permil,
            member_holomems=tuple(m.member for m in members),
            leader_holomem=leader.member,
            ceilings=ceilings,
            k_constant=UNIT_SCORE_K,
        )
        bounds.append((i, ub))
        if on_scores[i] > ub + 1e-6:
            violations += 1
            if violations <= 6:
                bd_on = score_with_breakdown(Team(leader, members), cfg_on)
                ds_on = deck_screen_unit_score(bd_on)
                print(f"  VIOLATION team {i}: on_US={ds_on.unit_score:,.0f} > bound={ub:,.0f}")
                print(f"     on_power={ds_on.overall_power:,.0f} off_power={ds_off.overall_power:,.0f}"
                      f"  on_bonus={ds_on.score_bonus_permil:.0f} off_bonus={ds_off.score_bonus_permil:.0f}"
                      f"  leader={leader.member}")
        worst_slack = max(worst_slack, ub - on_scores[i])
    cheap_s = time.perf_counter() - t0

    for n_frac in (0.05, 0.10, 0.25):
        n = max(TOP_K, int(len(teams) * n_frac))
        cand = top_n_candidates(bounds, n)
        cand_topk = sorted(cand, key=lambda i: on_scores[i], reverse=True)[:TOP_K]
        match = set(cand_topk) == set(true_topk)
        print(f"  N={n:5} ({n_frac:.0%})  top-{TOP_K} match: {match}   "
              f"expensive passes: {n} vs {len(teams)}  ({n/len(teams):.0%})")

    print(f"\nteams sampled              : {len(teams)}")
    print(f"phase A (precompute) once  : {phase_a_s*1000:.1f} ms")
    print(f"bound violations           : {violations}  (must be 0 for a sound cut)")
    print(f"worst bound slack          : {worst_slack:,.0f} Unit Score (looseness)")
    print(f"exhaustive board passes    : {exhaustive_s:.2f} s for {len(teams)} teams")
    print(f"cheap (off+bound) passes   : {cheap_s:.2f} s for {len(teams)} teams")


if __name__ == "__main__":
    main()
