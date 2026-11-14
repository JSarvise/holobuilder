"""`connect.joint` (the compiled joint deal of one team) against `board_plan._joint_assignment`
on the Python path: the same placements and the same boards, `==`, for real maxed teams on
songs where the yellow cap binds (a GROUP song of two singers off the team, whose yellows the
bench is dealt for) and where it does not; and the Python path on the compiled matching against
the all-Python one. Real dumps; skips when they are absent or no compiler builds the kernel."""

from __future__ import annotations

import dataclasses

import pytest

connect = pytest.importorskip("connect")
jointmatch = pytest.importorskip("jointmatch")

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


@pytest.fixture(scope="module")
def maxed():
    import leaf_inputs
    from leaf_inputs import MissingInput

    try:
        connect.load()
        jointmatch.load()
    except Exception as exc:  # noqa: BLE001 - no compiler here
        pytest.skip(f"the kernel cannot be built here: {exc!r}")
    try:
        built = leaf_inputs.leaf_inputs("maxed", "EXPERT", "m0342")
        library = leaf_inputs.chart_library(leaf_inputs.charts_path("EXPERT"))
    except (OSError, MissingInput, KeyError) as exc:
        pytest.skip(f"the real inputs are not available here: {exc!r}")
    if any(c not in built["by_id"] for leader, members in TEAMS for c in (leader, *members)):
        pytest.skip("the catalogue lacks a test team's card")
    return built, library


def _charts(library, team_chrs):
    every = library.at("EXPERT")
    picks = [next(c for c in every if c.music_id == "m0342")]
    picks.append(next(c for c in every if not c.singers))
    picks.append(next(c for c in every if c.singer_type == "solo" and not set(c.singers) & team_chrs))
    picks.append(
        next(
            c
            for c in every
            if c.singer_type == "group" and len(c.singers) == 2 and not set(c.singers) & team_chrs
        )
    )
    return picks


def _deal(ctx, team, chart, pool, kernel: bool, compiled_matching: bool, monkeypatch):
    from hbuilder.domain import board_plan as bp
    from hbuilder.domain.board_plan import board_model_from_plan, song_cast
    from hbuilder.domain.scoring import UNIT_SCORE_K
    from hbuilder.solver import parallel_refine as pr

    monkeypatch.setattr(bp, "JOINT_KERNEL", connect.joint if kernel else None)
    monkeypatch.setenv(bp.JOINT_KERNEL_ENV, "")
    monkeypatch.setattr(bp, "JOINT_MATCHING", jointmatch.CJointMatching if compiled_matching else None)
    monkeypatch.setenv(bp.JOINT_MATCHING_ENV, "")
    bp._JOINT_BENCHES.clear()
    cast = song_cast(chart)
    cache = pr.new_plan_cache(ctx, pool._by_id)
    off: list = []
    plan, _board, _assemble = pr._planned_board(ctx, team, pool._tree_for, pool._budget_for, cache, cast, off)
    _cfg, power, bonus, rate = off
    placements: list = []
    board = board_model_from_plan(
        plan,
        team.leader,
        team.members,
        ctx.skilltree,
        ctx.node_effect_ids,
        overall_power=power,
        bonus_permil=bonus,
        k_constant=UNIT_SCORE_K,
        active_pool_rate=rate,
        connect_cards=ctx.connect_cards,
        placements_out=placements,
        bench_characters=ctx.bench_characters,
        name_by_chr=ctx.name_by_chr,
        sings=cast,
        plan_cache=cache,
        bench_sockets=ctx.bench_sockets,
        joint=True,
    )
    mults = {
        name: tuple((n.node_id, n.connect_multiplier) for n in cb.nodes)
        for name, cb in board.characters.items()
    }
    return placements, mults


def test_the_compiled_joint_deal_is_the_python_one_bit_for_bit(maxed, monkeypatch):
    from hbuilder.domain.team import Team
    from hbuilder.solver import parallel_refine as pr

    built, library = maxed
    ctx = dataclasses.replace(built["ctx"], refine_activation=False)
    capped = 0
    with pr.RefinePool(ctx, workers=1) as pool:
        for leader, members in TEAMS:
            team = Team(leader=pool._by_id[leader], members=tuple(pool._by_id[m] for m in members))
            chrs = {"chr-" + c.id.split("-")[1] for c in (team.leader, *team.members)}
            for chart in _charts(library, chrs):
                one = dataclasses.replace(ctx, charts=(chart,))
                compiled = _deal(one, team, chart, pool, True, True, monkeypatch)
                python = _deal(one, team, chart, pool, False, True, monkeypatch)
                reference = _deal(one, team, chart, pool, False, False, monkeypatch)
                assert compiled == python == reference, chart.music_id
                capped += chart.singer_type == "group"
    assert capped


def test_a_bench_board_is_one_object_per_content_on_every_song(maxed, monkeypatch):
    """Songs whose bench deals give a board the same multipliers share that board: the same
    rows list in each song's bench (so `PlanCache` parses it once and its digest's per-card
    memos serve every song) and the same `CharacterBoard` in every team's dealt model."""
    import json

    from hbuilder.domain import board_plan as bp
    from hbuilder.domain.board_plan import song_cast
    from hbuilder.domain.team import Team
    from hbuilder.solver import parallel_refine as pr

    built, library = maxed
    ctx = dataclasses.replace(built["ctx"], refine_activation=False)
    monkeypatch.setattr(bp, "JOINT_KERNEL", connect.joint)
    monkeypatch.setattr(bp, "JOINT_MATCHING", jointmatch.CJointMatching)
    bp._JOINT_BENCHES.clear()
    every = library.at("EXPERT")
    casts = list({(frozenset(c.singers), c.singer_type): song_cast(c) for c in every}.values())[:16]
    leader, members = TEAMS[0]
    with pr.RefinePool(ctx, workers=1) as pool:
        cache = pr.new_plan_cache(ctx, pool._by_id)
        benches = [
            bp.joint_bench(
                cache,
                ctx.bench_characters,
                ctx.bench_sockets,
                ctx.connect_cards,
                dict(ctx.name_by_chr),
                cast,
                skilltree=ctx.skilltree,
                node_effect_ids=ctx.node_effect_ids,
            )
            for cast in casts
        ]
        team = Team(leader=pool._by_id[leader], members=tuple(pool._by_id[m] for m in members))
        models = []
        for cast in casts:
            _plan, board, _assemble = pr._planned_board(
                dataclasses.replace(ctx, joint_connect=True),
                team,
                pool._tree_for,
                pool._budget_for,
                cache,
                cast,
                [],
                cast,
            )
            models.append(board)
    rows_of: dict = {}
    shared = 0
    for jb in benches:
        for name, rows in jb.bench.items():
            first = rows_of.setdefault((name, json.dumps(rows, sort_keys=True)), rows)
            assert first is rows, name
            shared += first is not ctx.bench_characters.get(name)
    boards_of: dict = {}
    for model in models:
        for name, cb in model.characters.items():
            first = boards_of.setdefault((name, cb), cb)
            assert first is cb, name
    assert shared


def test_a_search_value_keeps_the_bench_cards_where_they_are(maxed):
    """SEARCH (a search's cheap value over several charts) is the plain develop with the
    joint deal off: the bench keeps its cards, one board per singer context of the team. The
    served value deals every chart's cards with its cast's bench, a different number."""
    from hbuilder.domain.team import Team
    from hbuilder.solver import parallel_refine as pr

    built, library = maxed
    leader, members = TEAMS[1]
    by_id = built["by_id"]
    team = Team(leader=by_id[leader], members=tuple(by_id[m] for m in members))
    chrs = {"chr-" + c.id.split("-")[1] for c in (team.leader, *team.members)}
    ctx = dataclasses.replace(
        built["ctx"], charts=tuple(_charts(library, chrs)), board_per_chart=True, refine_activation=False
    )
    assert ctx.joint_connect and ctx.bench_sockets
    with pr.RefinePool(ctx, workers=1) as pool:
        search = pool.batch_score([team], refine_activation=pr.SEARCH)  # type: ignore[arg-type]
        served = pool.batch_score([team])
        per_chart = pool.batch_score_per_chart([team], refine_activation=pr.SEARCH)
    with pr.RefinePool(dataclasses.replace(ctx, joint_connect=False), workers=1) as pool:
        held = pool.batch_score([team])
        held_per_chart = pool.batch_score_per_chart([team])
    assert search == held and per_chart == held_per_chart
    assert served != held


def test_a_songs_bench_is_the_same_built_alone_or_after_other_songs(maxed, monkeypatch):
    """A bench board's side of the matching is shared by the songs that reach it alike (their
    cast's presence, scope and whether she sings): every song's bench built after the others
    equals the one built alone, values, yellows, the deal and its multipliers."""
    from hbuilder.domain import board_plan as bp
    from hbuilder.domain.board_plan import song_cast
    from hbuilder.solver import parallel_refine as pr

    built, library = maxed
    ctx = built["ctx"]
    monkeypatch.setattr(bp, "JOINT_MATCHING", jointmatch.CJointMatching)
    casts = list({(frozenset(c.singers), c.singer_type): song_cast(c) for c in library.at("EXPERT")}.values())
    with pr.RefinePool(ctx, workers=1) as pool:
        cache = pr.new_plan_cache(ctx, pool._by_id)

        def bench(cast):
            return bp.joint_bench(
                cache,
                ctx.bench_characters,
                ctx.bench_sockets,
                ctx.connect_cards,
                dict(ctx.name_by_chr),
                cast,
                skilltree=ctx.skilltree,
                node_effect_ids=ctx.node_effect_ids,
            )

        bp._JOINT_BENCHES.clear()
        bp._JOINT_BOARD_SIDES.clear()
        together = [bench(cast) for cast in casts]
        sides = len(bp._JOINT_BOARD_SIDES)
        for cast, jb in zip(casts, together):
            bp._JOINT_BENCHES.clear()
            bp._JOINT_BOARD_SIDES.clear()
            alone = bench(cast)
            assert alone.values == jb.values and alone.yellows == jb.yellows, sorted(cast)
            assert alone.mults == jb.mults and alone.matching.assignment() == jb.matching.assignment()
    assert sides < len(casts) * len(together[0].values) // 4
