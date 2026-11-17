"""`lawmerge.c` against `song_distribution.merge_laws`: the same values in the same order with
the same doubles, exact and binned, on random laws and on chains of merges that feed each
other (the order a merge leaves decides the next one's sums), and the same whole law on a
real row."""

from __future__ import annotations

import random
import shutil

import pytest

from hbuilder.domain import song_distribution as sd

pytestmark = pytest.mark.skipif(
    shutil.which("gcc") is None and not __import__("lawmerge").LIBRARY.exists(), reason="no gcc"
)


def _law(rng: random.Random, n: int, spread: int) -> dict[int, float]:
    law: dict[int, float] = {}
    for _ in range(n):
        law[rng.randrange(-spread, spread)] = rng.random() * 1e-3
    return law


def _same(kernel_out, py_out) -> None:
    (k_law, k_width), (p_law, p_width) = kernel_out, py_out
    assert k_width == p_width
    assert list(k_law.items()) == list(p_law.items())


@pytest.mark.parametrize("seed", range(40))
def test_one_merge_is_the_python_merge_bit_for_bit(seed):
    import lawmerge

    rng = random.Random(seed)
    a = _law(rng, rng.randrange(1, 400), rng.choice((50, 5_000, 2_000_000)))
    b = _law(rng, rng.randrange(1, 400), rng.choice((50, 5_000, 2_000_000)))
    wa, wb = rng.random(), rng.random()
    w = wa + wb
    args = (
        wa / w,
        rng.randrange(0, 3_000),
        wb / w,
        rng.randrange(0, 3_000),
        rng.randrange(0, 5_000_000),
        rng.choice((1, 1, 2, 7, 11, 64)),
        rng.choice((10, 60, 300, 400_000)),
    )
    share_a, shift_a, share_b, shift_b, base, width, cap = args
    _same(
        lawmerge.merge(a, share_a, shift_a, b, share_b, shift_b, base, width, cap),
        sd.merge_laws(a, share_a, shift_a, b, share_b, shift_b, base, width, cap),
    )


@pytest.mark.parametrize("seed", range(8))
def test_a_chain_of_merges_stays_bit_for_bit(seed):
    """Each merge's output fed to the next, as the sweep does, binned on the way (a small cap)."""
    import lawmerge

    rng = random.Random(100 + seed)
    k_law, k_width = {0: 1.0}, 1
    p_law, p_width = {0: 1.0}, 1
    k_off = p_off = 0
    for _step in range(60):
        other = _law(rng, rng.randrange(1, 200), 3_000)
        wa, wb = rng.random(), rng.random()
        shift = rng.randrange(0, 2_000)
        cap = rng.choice((150, 400, 2_000))
        k_law, k_width = lawmerge.merge(
            k_law, wa / (wa + wb), shift, other, wb / (wa + wb), 0, k_off, k_width, cap
        )
        p_law, p_width = sd.merge_laws(
            p_law, wa / (wa + wb), shift, other, wb / (wa + wb), 0, p_off, p_width, cap
        )
        assert k_width == p_width
        assert list(k_law.items()) == list(p_law.items())
        k_off += rng.randrange(0, 50)
        p_off = k_off


def test_the_whole_law_of_a_real_row_is_the_pythons(monkeypatch):
    """`run_score_distribution` with the kernel and without it: every field equal."""
    import lawmerge
    from hbweb import config

    from hbuilder.domain.song import load_charts
    from hbuilder.domain.song_rank import score_team_on_chart
    from hbuilder.domain.team import Team
    from hbuilder.inputs import apply_collection, load_inputs

    library = load_charts(str(config.CHARTS_PATH))
    if library is None or not config.CARDS_PATH.exists():
        pytest.skip("charts or cards absent")
    chart = library.get("m0342") or next(library.get(m) for m in library.music_ids)
    cards, owned, cfg, _ = load_inputs(config.CARDS_PATH, config.OWNED_PATH)
    cards, _cb = apply_collection(cards, None, owned_ids=[c.id for c in owned], maxed=True)
    five = [c for c in cards if c.rarity.startswith("5")]
    leader, members = five[0], []
    for c in five[1:]:
        if c.member != leader.member and c.member not in {m.member for m in members}:
            members.append(c)
        if len(members) == 5:
            break
    team = Team(leader=leader, members=tuple(members))
    s = score_team_on_chart(team, chart, cfg)
    order = s.member_order
    monkeypatch.setattr(sd, "MERGE_KERNEL", None)
    plain = sd.run_score_distribution(team.members, team.leader, chart, cfg, order, s.deck_power, cap=2_000)
    assert lawmerge.enable()
    fast = sd.run_score_distribution(team.members, team.leader, chart, cfg, order, s.deck_power, cap=2_000)
    assert fast == plain
    assert plain["bin_width"] > 1  # the binned path was taken
