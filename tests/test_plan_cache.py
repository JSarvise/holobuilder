"""The plan cache (`domain/board_plan.py`: `plan_key`, `PlanCache`,
`cached_team_plan`, `optimize_and_rerank(plan_cache=...)`).

The board plan is a FIXED function of (character,
role, budget, singer context), so a plan computed once at the cache's nominal
context is reused for every team the character appears in. These pin: purity
(same roster, different team context -> same float), the leader-as-member
role, member-plan reuse across teams (hit count), and the default path
(`plan_cache=None`) staying the per-team `optimize_team_boards` path."""

from __future__ import annotations

import pytest

from hbuilder.domain.board_plan import (
    PlanCache, cached_team_plan, optimize_and_rerank, optimize_team_boards,
    plan_key, PLAN_ROLE_LEADER, PLAN_ROLE_LEADER_MEMBER, PLAN_ROLE_MEMBER,
    PLAN_ROLE_BENCH,
)
from hbuilder.domain.card import Stats
from hbuilder.domain.skilltree import SkillTree, TreeNode, ROOT_NODE
from hbuilder.domain.team import Team

from conftest import make_card, make_scoring_config

K = 2.03734
_STATS = Stats(performance=1000, technique=1000, sense=1000)
_BIG = Stats(performance=3000, technique=3000, sense=3000)

_LEADER = ("skill_tree_effect-all_parameter_up-100-skill_tree_effect_target"
           "-all-set_live_leader")
_MEMBER = ("skill_tree_effect-sense_up-50-skill_tree_effect_target"
           "-skill_tree_character-always")
_GREEN = "skill_tree_effect-technique_up-10-skill_tree_effect_target-all-always"
_R044 = ("skill_tree_effect-technique_up_permil_up-100-skill_tree_effect_target-all"
         "-skill_tree_effect_passive_trigger-music_skill_tree_character-set_live_leader")

EFFECTS = {"R-001": [_LEADER], "B-001": [_MEMBER], "G-001": [_GREEN], "R-044": [_R044]}
SKILLTREE = {"nodes": {n: {"chr": {}} for n in EFFECTS}, "members": [], "models": {}}


def _tree() -> SkillTree:
    ids = ["R-001", "B-001", "G-001", "R-044"]
    nodes = {ROOT_NODE: TreeNode(node_id=ROOT_NODE, branch="S", cost=0, player_level=None)}
    for n in ids:
        nodes[n] = TreeNode(node_id=n, branch=n[0], cost=1, player_level=None)
    adjacency = {ROOT_NODE: frozenset(ids), **{n: frozenset({ROOT_NODE}) for n in ids}}
    return SkillTree(nodes=nodes, positions={}, adjacency=adjacency)


TREE = _tree()


def _card(chr_no: int, star: int = 5, stats: Stats = _STATS):
    return make_card(f"card-{chr_no:05d}-{star}-uniq-0000-00", member=f"Chr{chr_no}",
                     stats=stats)


def _team(leader_no: int, member_nos, *, member_stats=None):
    leader = _card(leader_no)
    members = tuple(_card(n, stats=(member_stats or {}).get(n, _STATS)) for n in member_nos)
    return Team(leader=leader, members=members)


def _rerank(teams, *, cache=None, budget=2, sings=False):
    return optimize_and_rerank(
        teams, make_scoring_config(), tree_for=lambda c: TREE,
        budget_for=lambda c: budget, node_effect_ids=EFFECTS, skilltree=SKILLTREE,
        plan_cache=cache, sings=sings,
    )


def _fresh_cache(team=None):
    return PlanCache.from_team(team or _team(1, [2, 3, 4, 5, 6]), make_scoring_config())


# --- plan_key ---------------------------------------------------------------

def test_plan_key_is_by_character_role_budget_and_own_singer_context():
    a5, a4 = _card(7, 5), _card(7, 4)
    # a leader's plan is per character (any card of hers), split by `sings`.
    assert plan_key(a5, PLAN_ROLE_LEADER, 10, True) == plan_key(a4, PLAN_ROLE_LEADER, 10, True)
    assert plan_key(a5, PLAN_ROLE_LEADER, 10, True) != plan_key(a5, PLAN_ROLE_LEADER, 10, False)
    # a member's plan is per CARD (its blues use the card's stats)...
    assert plan_key(a5, PLAN_ROLE_MEMBER, 10, False) != plan_key(a4, PLAN_ROLE_MEMBER, 10, False)
    # ...and split by HER OWN singer context: a member who sings the song values her
    # solo/group yellows (board_plan.singer_weight_for), and by the song's
    # scope (`SongCast`: a solo song pays her solo yellows, a group one the
    # group ones). A bench plan is per character, split the same way: an off-team singer
    # pays her song's yellows (`singer_bench_characters`).
    assert plan_key(a5, PLAN_ROLE_MEMBER, 10, True) != plan_key(a5, PLAN_ROLE_MEMBER, 10, False)
    assert plan_key(a5, PLAN_ROLE_MEMBER, 10, "solo") != plan_key(a5, PLAN_ROLE_MEMBER, 10, "group")
    assert plan_key(a5, PLAN_ROLE_BENCH, 10, False) == plan_key(a4, PLAN_ROLE_BENCH, 10, False)
    assert plan_key(a5, PLAN_ROLE_BENCH, 10, "solo") != plan_key(a5, PLAN_ROLE_BENCH, 10, False)
    assert plan_key(a5, PLAN_ROLE_LEADER, 10, False) != plan_key(a5, PLAN_ROLE_LEADER, 11, False)
    with pytest.raises(ValueError):
        plan_key(a5, "captain", 10, False)


# --- purity -----------------------------------------------------------------

def test_same_team_scores_the_same_float_whatever_the_cache_saw_before():
    """The objective is a pure function of the roster: the plan for a team does
    not depend on which other teams were planned first (a cold cache vs one
    warmed by a different team, with different member stats, agree exactly)."""
    t = _team(1, [2, 3, 4, 5, 6])
    other = _team(9, [3, 4, 10, 11, 12], member_stats={3: _BIG, 4: _BIG})
    cold = _rerank([t], cache=_fresh_cache())[0]
    warm_cache = _fresh_cache()
    _rerank([other], cache=warm_cache)
    warm = _rerank([t], cache=warm_cache)[0]
    assert warm.optimised_us == cold.optimised_us
    for cid, p in cold.plan.per_character.items():
        assert warm.plan.per_character[cid].selected == p.selected


def test_a_members_plan_is_the_same_object_in_two_team_contexts_and_counts_a_hit():
    cache = _fresh_cache()
    t1 = _team(1, [2, 3, 4, 5, 6])
    t2 = _team(9, [3, 4, 10, 11, 12], member_stats={10: _BIG, 11: _BIG, 12: _BIG})
    r1 = _rerank([t1], cache=cache)[0]
    assert (cache.hits, cache.misses) == (0, 6)          # 1 leader + 5 members
    r2 = _rerank([t2], cache=cache)[0]
    # chr 3 and 4 as members again -> reused; the new leader and 3 new members miss.
    assert (cache.hits, cache.misses) == (2, 10)
    m3 = _card(3)
    assert r2.plan.per_character[m3.id] is r1.plan.per_character[m3.id]
    assert "B-001" in r1.plan.per_character[m3.id].selected


def test_leader_as_member_gets_the_leader_plus_member_plan_and_a_leader_only_no_blue():
    cache = _fresh_cache()
    lead = _card(1)
    lead_member = _card(1, 4)                       # same holomem, a member card
    others = [_card(n) for n in (2, 3, 4, 5)]
    both = Team(leader=lead, members=(lead_member, *others))
    only = Team(leader=lead, members=tuple(_card(n) for n in (2, 3, 4, 5, 6)))
    p_both = cached_team_plan(both.leader, both.members, cache=cache, tree_for=lambda c: TREE,
                              budget_for=lambda c: 4, node_effect_ids=EFFECTS,
                              skilltree=SKILLTREE)
    p_only = cached_team_plan(only.leader, only.members, cache=cache, tree_for=lambda c: TREE,
                              budget_for=lambda c: 4, node_effect_ids=EFFECTS,
                              skilltree=SKILLTREE)
    # ONE plan per character, keyed by the leader card, valued as leader+member.
    assert len(p_both.per_character) == 5 and lead_member.id not in p_both.per_character
    assert plan_key(lead_member, PLAN_ROLE_LEADER_MEMBER, 4, False) in cache.plans
    assert {"R-001", "B-001", "G-001"} <= p_both.per_character[lead.id].selected
    # leader-only: reds and greens, but her blue reaches nobody -> not bought.
    assert plan_key(lead, PLAN_ROLE_LEADER, 4, False) in cache.plans
    sel = p_only.per_character[lead.id].selected
    assert {"R-001", "G-001"} <= sel and "B-001" not in sel
    # song-agnostic: the conditional red is never bought; sung: it is.
    assert "R-044" not in sel
    p_sung = cached_team_plan(only.leader, only.members, cache=cache, tree_for=lambda c: TREE,
                              budget_for=lambda c: 4, node_effect_ids=EFFECTS,
                              skilltree=SKILLTREE, sings=True)
    assert "R-044" in p_sung.per_character[lead.id].selected


def test_bench_role_plans_only_the_greens():
    cache = _fresh_cache()
    plan = cache.plan_for(_card(42), PLAN_ROLE_BENCH, 4, False, tree=TREE,
                          node_effect_ids=EFFECTS, skilltree=SKILLTREE)
    assert plan.selected - {ROOT_NODE} == {"G-001"}


_Y_SOLO = ("skill_tree_effect-live_score_bonus_add_permil_up_by_music_skill_tree_character"
           "_and_music_singer_type-50-skill_tree_effect_passive_trigger-music_skill_tree"
           "_character_and_music_singer_type-solo-always-solo")
_Y_EFFECTS = {**EFFECTS, "Y-001": [_Y_SOLO]}
_Y_SKILLTREE = {**SKILLTREE, "nodes": {**SKILLTREE["nodes"],
                                       "Y-001": {"chr": {}, "e": {"t": _Y_SOLO, "tr": "solo"}}}}


def _y_tree() -> SkillTree:
    ids = ["R-001", "B-001", "G-001", "R-044", "Y-001"]
    nodes = {ROOT_NODE: TreeNode(node_id=ROOT_NODE, branch="S", cost=0, player_level=None)}
    for n in ids:
        nodes[n] = TreeNode(node_id=n, branch=n[0], cost=1, player_level=None)
    adjacency = {ROOT_NODE: frozenset(ids), **{n: frozenset({ROOT_NODE}) for n in ids}}
    return SkillTree(nodes=nodes, positions={}, adjacency=adjacency)


def test_a_singers_bench_plan_buys_the_yellows_her_song_pays():
    """A bench left as captured makes a singer off the team pay no yellow while the
    materials re-plan buys hers -- on one song that ranked every team without the
    singer ~5 % low and hid the best team. Her bench plan for her solo song buys the
    solo yellow; the song-agnostic plan and a group song's do not."""
    from hbuilder.domain.board_plan import SongCast, singer_bench_characters
    cache, tree, singer = _fresh_cache(), _y_tree(), _card(42)
    def chosen(sings):
        return cache.plan_for(singer, PLAN_ROLE_BENCH, 2, sings, tree=tree,
                              node_effect_ids=_Y_EFFECTS, skilltree=_Y_SKILLTREE).selected
    assert "Y-001" in chosen("solo")
    assert "Y-001" not in chosen(False) and "Y-001" not in chosen("group")
    bench = {"Chr42": [{"node": "G-001", "effects": [_GREEN], "connect_multiplier": 2.0}],
             "Chr43": [{"node": "G-001", "effects": [_GREEN], "connect_multiplier": 1.0}]}
    kw = dict(cache=cache, cards_by_chr={"chr-00042": singer}, tree_for=lambda c: tree,
              budget_for=lambda c: 2, node_effect_ids=_Y_EFFECTS, skilltree=_Y_SKILLTREE,
              name_by_chr={"chr-00042": "Chr42", "chr-00043": "Chr43"})
    out = singer_bench_characters(bench, SongCast({"chr-00042"}, "solo"), **kw)
    assert {e["node"] for e in out["Chr42"]} == {"G-001", "Y-001"}
    assert {e["node"]: e["connect_multiplier"] for e in out["Chr42"]}["G-001"] == 2.0  # kept
    assert out["Chr43"] is bench["Chr43"]                       # not a singer: as it was
    assert singer_bench_characters(bench, SongCast(), **kw) is bench   # no cast: unchanged


# --- the default path -------------------------------------------------------

def test_default_path_is_the_per_team_plan_and_agrees_with_the_cache_at_its_own_context():
    """`plan_cache=None` keeps the per-team `optimize_team_boards`; and a
    cache whose nominal context IS this team's context yields the same plan
    and the same optimised Unit Score, so the cached path can be diffed
    against the default one on any fixture."""
    t = _team(1, [2, 3, 4, 5, 6])
    default = _rerank([t])[0]
    # the default row equals a direct per-team plan at the same operating point.
    from dataclasses import replace
    from hbuilder.domain.scoring import score_with_breakdown, deck_screen_unit_score, UNIT_SCORE_K
    bd = score_with_breakdown(t, replace(make_scoring_config(), board=None))
    ds = deck_screen_unit_score(bd)
    direct = optimize_team_boards(
        t.leader, t.members, tree_for=lambda c: TREE, budget_for=lambda c: 2,
        node_effect_ids=EFFECTS, overall_power=ds.overall_power,
        bonus_permil=ds.score_bonus_permil, k_constant=UNIT_SCORE_K,
        active_pool_rate=bd.active_pool / (1.0 + bd.combined_support_rate),
        skilltree=SKILLTREE,
    )
    assert default.plan.total_gain == direct.total_gain
    for cid, p in direct.per_character.items():
        assert default.plan.per_character[cid].selected == p.selected
    # cached at this very team's context: same selection, same float.
    cached = _rerank([t], cache=PlanCache.from_team(t, make_scoring_config()))[0]
    assert cached.optimised_us == default.optimised_us
    assert cached.board_off_us == default.board_off_us
    for cid, p in default.plan.per_character.items():
        assert cached.plan.per_character[cid].selected == p.selected


def test_a_cast_values_every_singing_board_and_a_flag_only_the_leader():
    from hbuilder.domain.board_plan import singer_weight_for
    cast = frozenset({"chr-00027"})
    assert singer_weight_for(cast, "chr-00027", False) == 1.0      # a singing member
    assert singer_weight_for(cast, "chr-00036", True) == 0.0       # a leader who does not sing
    assert singer_weight_for(True, "chr-00027", False) == 0.0      # the bool flag: leader only
    assert singer_weight_for(True, "chr-00036", True) == 1.0
    assert singer_weight_for(frozenset(), "chr-00027", True) == 0.0
