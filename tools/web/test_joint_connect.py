"""The maxed basis keeps every Connect card in one place and deals the team's cards together
with the bench's in every board it assembles (`RefineContext.joint_connect`).

The maxed bench (`leaf_inputs._with_maxed_bench`) socketed its cards once for every song; a
song's bench is dealt again for that song (`board_plan.joint_bench`, valued at the plan cache's
nominal context), and each team re-solves its own boards' rows in the same matching
(`board_plan.board_model_from_plan(joint=True)`). With several charts each chart's board is
assembled for its own cast. The detail (`develop_team`) develops the board the row's number was
scored on.

Real dumps (skilltree, master, charts, catalogue); skips when they are absent."""

from __future__ import annotations

import dataclasses

import pytest

# two strong maxed teams of m0342 (Korone / Stripe Strike, Okayu / Salty Breeze Runway)
TEAMS = (
    (
        "card-00017-5-uniq-0077-00",
        (
            "card-00018-5-uniq-0004-00",
            "card-00027-5-uniq-0092-00",
            "card-00030-5-uniq-0089-00",
            "card-00034-5-uniq-0090-00",
            "card-03003-5-uniq-0035-00",
        ),
    ),
    (
        "card-00016-5-uniq-0076-00",
        (
            "card-00006-5-uniq-0007-00",
            "card-00007-5-uniq-0008-00",
            "card-00016-5-uniq-0076-00",
            "card-00021-5-uniq-0017-00",
            "card-00022-5-uniq-0063-00",
        ),
    ),
)


def _maxed(song: str = "m0342"):
    import leaf_inputs
    from leaf_inputs import MissingInput

    try:
        built = leaf_inputs.leaf_inputs("maxed", "EXPERT", song)
        library = leaf_inputs.chart_library(leaf_inputs.charts_path("EXPERT"))
    except (OSError, MissingInput, KeyError) as exc:
        pytest.skip(f"the real inputs are not available here: {exc!r}")
    if any(c not in built["by_id"] for leader, members in TEAMS for c in (leader, *members)):
        pytest.skip("the catalogue lacks a test team's card")
    return built, library


def _team(by_id, leader, members):
    from hbuilder.domain.team import Team

    return Team(leader=by_id[leader], members=tuple(by_id[m] for m in members))


def _chrs(team) -> set[str]:
    return {"chr-" + c.id.split("-")[1] for c in (team.leader, *team.members)}


def test_the_maxed_leaf_deals_each_connect_card_once_with_the_bench():
    """The maxed leaf knows its bench's sockets (`maxed_bench_state`) and deals them with the
    team's: a developed team's placements name team and bench sockets, each card once, and a
    bench board whose card moved is scored with its new multipliers."""
    import leaf_inputs

    from hbuilder.domain.board_plan import bench_sockets_from_state
    from hbuilder.solver import parallel_refine as pr

    built, _library = _maxed()
    ctx = dataclasses.replace(built["ctx"], refine_activation=False)
    assert ctx.bench_sockets == bench_sockets_from_state(leaf_inputs.maxed_bench_state())
    assert ctx.joint_connect
    chart = built["chart"]
    with pr.RefinePool(ctx, workers=1) as pool:
        cache = pr.new_plan_cache(ctx, pool._by_id)
        for leader, members in TEAMS:
            team = _team(pool._by_id, leader, members)
            bt = pr.develop_team(ctx, team, pool._tree_for, pool._budget_for, chart, plan_cache=cache)
            cards = [p.card for p in bt.sockets]
            assert len(cards) == len(set(cards))
            bench = [p for p in bt.sockets if p.chr not in _chrs(team)]
            assert bench and any(p.chr in _chrs(team) for p in bt.sockets)
            for p in bench:  # the bench board carries the card the deal gave it
                name = ctx.name_by_chr[p.chr]
                mults = {n.connect_multiplier for n in bt.board.characters[name].nodes}
                assert p.multiplier in mults


@pytest.mark.parametrize("refine", [False, True, 2])
def test_a_maxed_row_scores_its_board_dealt_with_the_bench(refine):
    """A maxed row (plain or refined) is scored on its board dealt with the bench's, above the
    bench's cards kept where they are for these strong teams (measured +0.13..0.33 %); the
    detail's board scores the row's float."""
    from hbuilder.domain.song_rank import score_team_on_chart
    from hbuilder.solver import parallel_refine as pr

    built, _library = _maxed()
    ctx = dataclasses.replace(built["ctx"], refine_activation=refine)
    chart = built["chart"]
    with pr.RefinePool(ctx, workers=1) as pool:
        by_id = pool._by_id
        cache = pr.new_plan_cache(ctx, by_id)
        for leader, members in TEAMS:
            team = _team(by_id, leader, members)
            ids = (leader, members)
            joint = pr.evaluate(ctx, by_id, pool._tree_for, pool._budget_for, ids, plan_cache=cache)
            kept = pr.evaluate(
                dataclasses.replace(ctx, joint_connect=False),
                by_id,
                pool._tree_for,
                pool._budget_for,
                ids,
                plan_cache=pr.new_plan_cache(ctx, by_id),
            )
            assert joint > kept
            bt = pr.develop_team(ctx, team, pool._tree_for, pool._budget_for, chart, plan_cache=cache)
            got = score_team_on_chart(team, chart, dataclasses.replace(ctx.cfg, board=bt.board))
            assert got.total_score == joint


def test_the_joint_board_on_the_plan_cache_matches_the_full_matching():
    """The plan cache's joint deal (`joint_bench`, the bench valued once at the nominal context,
    re-solved per team) against the full matching with the bench valued for each team
    (`socket_bench`): within 0.05 % for these strong teams."""
    from hbuilder.domain.board_plan import board_model_from_plan, song_cast
    from hbuilder.domain.scoring import UNIT_SCORE_K
    from hbuilder.domain.song_rank import score_team_on_chart
    from hbuilder.solver import parallel_refine as pr

    built, _library = _maxed()
    ctx = dataclasses.replace(built["ctx"], refine_activation=False)
    chart = built["chart"]
    with pr.RefinePool(ctx, workers=1) as pool:
        cache = pr.new_plan_cache(ctx, pool._by_id)
        for leader, members in TEAMS:
            team = _team(pool._by_id, leader, members)
            off: list = []
            plan, board, _assemble = pr._planned_board(
                ctx, team, pool._tree_for, pool._budget_for, cache, song_cast(chart), off
            )
            _cfg, power, bonus, pool_rate = off
            full = board_model_from_plan(
                plan,
                team.leader,
                team.members,
                ctx.skilltree,
                ctx.node_effect_ids,
                overall_power=power,
                bonus_permil=bonus,
                k_constant=UNIT_SCORE_K,
                active_pool_rate=pool_rate,
                connect_cards=ctx.connect_cards,
                bench_characters=ctx.bench_characters,
                name_by_chr=ctx.name_by_chr,
                sings=song_cast(chart),
                bench_sockets=ctx.bench_sockets,
                socket_bench=True,
            )
            fast = score_team_on_chart(team, chart, dataclasses.replace(ctx.cfg, board=board)).total_score
            slow = score_team_on_chart(team, chart, dataclasses.replace(ctx.cfg, board=full)).total_score
            assert fast == pytest.approx(slow, rel=5e-4)


def test_an_off_team_group_of_two_reaches_its_song_bonus_with_the_bench_dealt():
    """A GROUP song of two singers off the team: their group yellows have no Connect card on
    the maxed bench (it socketed greens, then solo yellows), so the song bonus stops at 80 of
    its 100 cap. Dealt with the song, the bench gives them cards and the song bonus reaches
    the cap."""
    from hbuilder.solver import parallel_refine as pr

    built, library = _maxed()
    leader, members = TEAMS[1]
    by_id = built["by_id"]
    team = _team(by_id, leader, members)
    chart = next(
        (
            c
            for c in library.at("EXPERT")
            if c.singer_type == "group" and len(c.singers or ()) == 2 and not set(c.singers) & _chrs(team)
        ),
        None,
    )
    if chart is None:
        pytest.skip("no GROUP song of two singers off this team")
    ctx = dataclasses.replace(built["ctx"], charts=(chart,), refine_activation=False)
    bonus = {}
    with pr.RefinePool(ctx, workers=1) as pool:
        for joint in (False, True):
            c = dataclasses.replace(ctx, joint_connect=joint)
            bt = pr.develop_team(
                c, team, pool._tree_for, pool._budget_for, chart, plan_cache=pr.new_plan_cache(c, pool._by_id)
            )
            bonus[joint] = bt.board.song_score_bonus_for(chart.singers, scope="group")
    assert bonus[False] < 0.1 - 1e-9
    assert bonus[True] == pytest.approx(0.1)


def test_in_general_assembles_each_chart_for_its_cast_and_the_detail_shows_it():
    """In general (the frequency counts chosen per chart, `ChartCounts`): each chart is scored
    on its board dealt with the bench for that chart's cast, above the bench's cards kept where
    they are; a row's detail on one of those songs develops the board that chart was scored on."""
    from hbuilder.domain.song_rank import score_team_on_chart
    from hbuilder.solver import parallel_refine as pr

    built, library = _maxed()
    every = library.at("EXPERT")
    # a whole-roster song, a solo of a holomem off the teams, a group song
    picks = [
        next(c for c in every if not c.singers),
        next(c for c in every if c.singer_type == "solo" and c.singers[0] == "chr-04001"),
        next(c for c in every if c.singer_type == "group" and len(c.singers) == 2),
    ]
    ctx = dataclasses.replace(built["ctx"], charts=tuple(picks), board_per_chart=True)
    leader, members = TEAMS[0]
    ids = (leader, members)
    with pr.RefinePool(ctx, workers=1) as pool:
        by_id = pool._by_id
        team = _team(by_id, leader, members)
        cache = pr.new_plan_cache(ctx, by_id)
        joint = pr.evaluate(
            ctx,
            by_id,
            pool._tree_for,
            pool._budget_for,
            (*ids, None, pr.COUNTS),
            plan_cache=cache,
            per_chart=True,
        )
        kept = pr.evaluate(
            dataclasses.replace(ctx, joint_connect=False),
            by_id,
            pool._tree_for,
            pool._budget_for,
            (*ids, None, pr.COUNTS),
            plan_cache=pr.new_plan_cache(ctx, by_id),
            per_chart=True,
        )
        assert all(j > k for j, k in zip(joint, kept))
        for chart, value in zip(picks, joint):
            one = dataclasses.replace(ctx, charts=(chart,))
            bt = pr.develop_team(
                one, team, pool._tree_for, pool._budget_for, chart, plan_cache=cache, refine=pr.COUNTS
            )
            got = score_team_on_chart(team, chart, dataclasses.replace(ctx.cfg, board=bt.board))
            assert got.total_score == value, chart.music_id
