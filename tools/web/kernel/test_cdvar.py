"""The cooldown screen (`cdvar.c`, `song.screen_song_cooldown_variants`) orders frequency-count
variants as the exact scorer does.

It is an estimate by design (runs of notes, the per-note `ceil` taken at its mean), so it is
held to a tolerance, not `==`: every variant within 0.01 % of `score_song_cooldown_variants`'
total, and the same best variant. Without a kernel the screen IS the exact totals. Real
teams and charts on the maxed basis; skipped without the catalogue's derived data.
"""
from __future__ import annotations

import dataclasses
import random
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
for extra in (HERE, HERE.parent):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from hbuilder import paths  # noqa: E402

pytestmark = [
    pytest.mark.skipif(shutil.which("gcc") is None, reason="no gcc to build the kernel"),
    pytest.mark.skipif(not (paths.DUMPS / "charts.json").exists(),
                       reason="needs the catalogue's derived data (dumps/, gitignored)"),
]


@pytest.fixture(scope="module")
def leaf():
    import leaf_inputs
    from hbuilder.domain.song import load_charts
    from hbuilder.solver.parallel_refine import new_plan_cache

    built = leaf_inputs.leaf_inputs("maxed", "EXPERT")
    charts = load_charts(str(leaf_inputs.charts_path("EXPERT"))).at("EXPERT")
    return built, {c.music_id: c for c in charts}, new_plan_cache(built["ctx"], built["by_id"])


def _cases(leaf, song: str, teams: int, seed: int):
    from hbuilder.domain.board_plan import SongCast
    from hbuilder.domain.song import overall_power_for
    from hbuilder.domain.team import Team
    from hbuilder.solver import parallel_refine as pr

    built, charts, cache = leaf
    ctx = dataclasses.replace(built["ctx"], charts=(charts[song],))
    by_id, reps, chart = built["by_id"], list(built["reps"]), charts[song]
    pool = pr.RefinePool(ctx, workers=1)
    sings = SongCast(frozenset(chart.singers), chart.singer_type)
    rng = random.Random(seed)
    out = []
    while len(out) < teams:
        picked = rng.sample(reps, 6)
        if len({c.member for c in picked[1:]}) < 5:
            continue
        team = Team(leader=picked[0], members=tuple(picked[1:]))
        ctx_d = pr._with_singer_bench(ctx, by_id, pool._tree_for, pool._budget_for, cache, sings)
        board = pr._cached_board(ctx_d, team, pool._tree_for, pool._budget_for, cache, sings)
        cfg = dataclasses.replace(ctx.cfg, board=board)
        unit, have = pr._cooldown_counts(team, board)
        if not have:
            continue
        full = {h: unit[h] * have[h] for h in have}
        variants = [full] + [{**full, h: unit[h] * c} for h in have for c in range(have[h])]
        out.append((team, chart, cfg, overall_power_for(tuple(team.members), team.leader, cfg), variants))
    return out


@pytest.mark.parametrize("song", ["m0001", "m0342", "m0155"])
def test_the_screen_is_within_a_hundredth_of_a_percent_and_picks_the_same_variant(leaf, song):
    import cdvar
    from hbuilder.domain import song as song_mod

    assert cdvar.enable()
    kernel_used = []
    real = song_mod._cooldown_screen_spec

    def spy(*a, **k):
        spec = real(*a, **k)
        kernel_used.append(spec is not None)
        return spec
    song_mod._cooldown_screen_spec = spy
    try:
        for team, chart, cfg, power, variants in _cases(leaf, song, 12, 7):
            exact = [s.total_score for s in song_mod.score_song_cooldown_variants(
                team.members, team.leader, chart, cfg, variants, deck_power=power)]
            est = song_mod.screen_song_cooldown_variants(
                team.members, team.leader, chart, cfg, variants, deck_power=power)
            for e, a in zip(exact, est):
                assert abs(a / e - 1) < 1e-4, (team, e, a)
            assert max(range(len(exact)), key=exact.__getitem__) == max(range(len(est)), key=est.__getitem__)
            plain, screened = song_mod.score_song_and_screen(
                team.members, team.leader, chart, cfg, variants, deck_power=power)
            assert screened == est
            assert plain == song_mod.score_song_detailed(
                team.members, (team.leader,), chart, cfg, deck_power=power)[0]
    finally:
        song_mod._cooldown_screen_spec = real
    assert kernel_used and all(kernel_used)


def test_without_a_kernel_the_screen_is_the_exact_totals(leaf, monkeypatch):
    from hbuilder.domain import song as song_mod

    monkeypatch.setattr(song_mod, "COOLDOWN_SCREEN_KERNEL", None)
    monkeypatch.setenv(song_mod.COOLDOWN_SCREEN_KERNEL_ENV, "")
    for team, chart, cfg, power, variants in _cases(leaf, "m0342", 4, 11):
        exact = [s.total_score for s in song_mod.score_song_cooldown_variants(
            team.members, team.leader, chart, cfg, variants, deck_power=power)]
        assert song_mod.screen_song_cooldown_variants(
            team.members, team.leader, chart, cfg, variants, deck_power=power) == exact
