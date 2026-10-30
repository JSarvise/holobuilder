"""The compiled note-walk kernel must equal the Python reference, to the bit.

`hd_pool` is `scoring._score_up_pool` for one set of live terms. Random term
sets are generated in the shapes the per-song path really produces (two buckets,
ratio and boost populations, team-wide and count-targeted supports, repeated
magnitudes so the stable sort matters) and compared with `==`, never `approx`.
"""
from __future__ import annotations

import random
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="no gcc to build the kernel")

from hbuilder.domain.scoring_config import load_scoring_config  # noqa: E402
from hbuilder.domain.song import _Term, _ratio_at  # noqa: E402

import notewalk  # noqa: E402

REPO = HERE.parents[2]
MEMBERS = ("m0", "m1", "m2", "m3", "m4")
MAGNITUDES = (0.0, 0.11, 0.3, 0.3, 0.45, 0.6, 0.65, 0.95, 1.1, 1.35)
OCCUPANCIES = (0.0, 0.37, 0.46, 0.55, 0.61, 1.0, 1.0)


def _random_terms(rng: random.Random) -> list[_Term]:
    terms = []
    for _ in range(rng.randint(0, 12)):
        is_boost = rng.random() < 0.45
        owner = rng.choice((*MEMBERS, "leader-card", None))
        targets = None
        if is_boost and rng.random() < 0.4:
            targets = frozenset(rng.sample(MEMBERS, rng.randint(0, 3)))
        terms.append(
            _Term(
                bucket=rng.choice(("ACTIVE", "SPECIAL")),
                magnitude=rng.choice(MAGNITUDES) * rng.choice((1.0, 1.0, 0.7)),
                occupancy=rng.choice(OCCUPANCIES),
                is_boost=is_boost,
                windows=None,
                special_member=None,
                special_duration=0.0,
                owner=owner,
                targets=targets,
            )
        )
    return terms


def test_pool_is_bit_identical_to_the_python_score_up_pool():
    cfg = load_scoring_config(REPO / "config.json")
    assert cfg.active_stacking_model.name == "max" and cfg.active_instant_model is None
    lib = notewalk.load()
    rng = random.Random(20261013)
    checked = nonzero = targeted = 0
    for _ in range(20000):
        terms = _random_terms(rng)
        effect_up = rng.choice((0.0, 0.0, 0.02, 0.221, 0.251))
        packed = notewalk.pack_terms(terms, MEMBERS)
        live = (1 << len(terms)) - 1 if rng.random() < 0.5 else rng.getrandbits(len(terms) or 1)
        chosen = [t for bit, t in enumerate(terms) if live >> bit & 1]
        expected = _ratio_at(
            [t for t in chosen if not t.is_boost], [t for t in chosen if t.is_boost], cfg, effect_up
        )
        got = lib.hd_pool(packed, len(terms), live, effect_up)
        assert got == expected, (terms, live, effect_up, got, expected)
        checked += 1
        nonzero += expected != 0.0
        targeted += any(t.is_boost and t.targets is not None for t in chosen)
    # The comparison must have had something to disagree about.
    assert checked == 20000 and nonzero > 5000 and targeted > 3000


def test_py_sum_matches_cpython_compensated_sum():
    """`sum()` over floats is Neumaier-compensated since CPython 3.12; a plain
    left fold differs on exactly the inputs below, and the kernel must not."""
    cfg = load_scoring_config(REPO / "config.json")
    lib = notewalk.load()
    targets = frozenset(MEMBERS)
    boosts = [
        _Term("SPECIAL", m, 1.0, True, None, None, 0.0, None, targets)
        for m in (0.1, 0.2, 0.3, 1e-17, 0.7, 0.1)
    ]
    # The premise: on these values the two summations really do differ.
    naive = 0.0
    for boost in boosts:
        naive += boost.magnitude
    assert naive != sum(boost.magnitude for boost in boosts)
    ratio = _Term("ACTIVE", 0.65, 0.55, False, None, None, 0.0, "m2", None)
    terms = [ratio, *boosts]
    expected = _ratio_at([ratio], boosts, cfg, 0.0)
    got = lib.hd_pool(notewalk.pack_terms(terms, MEMBERS), len(terms), (1 << len(terms)) - 1, 0.0)
    assert got == expected


def _real_inputs():
    import dataclasses
    from hbuilder.domain.board import load_board
    from hbuilder.domain.loader import load_cards
    from hbuilder.domain.song import load_charts

    charts_path = REPO / "tools/recon/proxy/dumps/charts.json"
    board_path = REPO / "tools/recon/proxy/dumps/board.json"
    if not (charts_path.exists() and board_path.exists()):
        pytest.skip("dumps/ is gitignored; no real charts or board here")
    cfg = load_scoring_config(REPO / "config.json")
    cfg = dataclasses.replace(cfg, board=load_board(str(board_path)))
    cards = [c for c in load_cards(REPO / "data/cards.json") if "-5-" in c.id]
    charts = load_charts(charts_path)
    return cfg, cards, charts


def _random_team(rng, cards):
    while True:
        members = tuple(rng.sample(cards, 5))
        if len({c.member for c in members}) == 5:
            return members, tuple(rng.sample(cards, 3))


def test_the_whole_song_score_is_bit_identical_with_the_kernel_injected():
    """End to end, on real cards, the real board and real charts: every leader's
    total AND the winning slot order must be equal with and without the kernel."""
    from hbuilder.domain import song

    cfg, cards, charts = _real_inputs()
    rng = random.Random(20261013)
    used = declined = 0
    seen = {"calls": 0, "declined": 0}

    def counting(*args, **kwargs):
        seen["calls"] += 1
        out = notewalk.walk(*args, **kwargs)
        seen["declined"] += out is None
        return out

    try:
        for music_id in ("m0334", "m0321", "m0129"):
            chart = charts.get(music_id, "EXPERT")
            if chart is None:
                continue
            for _ in range(120):
                members, leaders = _random_team(rng, cards)
                song.NOTE_WALK_KERNEL = None
                expected = song.score_song_detailed(members, leaders, chart, cfg)
                song.NOTE_WALK_KERNEL = counting
                got = song.score_song_detailed(members, leaders, chart, cfg)
                assert [(g.total_score, g.member_order, g.deck_power) for g in got] == [
                    (e.total_score, e.member_order, e.deck_power) for e in expected
                ], (music_id, [c.id for c in members])
                used += 1
    finally:
        song.NOTE_WALK_KERNEL = None
    assert used >= 240
    # The kernel must actually have run, not declined its way to a pass.
    assert seen["calls"] > used and seen["declined"] == 0, seen


def test_a_spawned_worker_picks_the_kernel_up_from_the_environment():
    """A pool on Windows spawns fresh interpreters: the module attribute set in
    the parent is not there, the environment is. Checked in a real child."""
    import os
    import subprocess

    from hbuilder.domain import song

    notewalk.load()
    code = (
        "from hbuilder.domain import song;"
        "k = song._note_walk_kernel();"
        "print('KERNEL', None if k is None else k.__name__, song.NOTE_WALK_KERNEL)"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    env[song.NOTE_WALK_KERNEL_ENV] = f"{HERE / 'notewalk.py'}:walk"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "KERNEL walk None" in out.stdout, (out.stdout, out.stderr)

    env[song.NOTE_WALK_KERNEL_ENV] = f"{HERE / 'no_such_file.py'}:walk"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "KERNEL None None" in out.stdout and "could not be loaded" in out.stderr, (out.stdout, out.stderr)


def test_threads_walking_the_same_chart_at_once_get_the_serial_scores():
    """The web server scores in threads, and the walk runs with the GIL released
    (CDLL): two threads on the same chart must not share scratch arrays. If they
    did, four concurrent /api/row-detail calls on one song would return totals
    from 1.87M to 3.50M for a team whose score is 3.43M, and 'Not verified' runs."""
    import threading

    from hbuilder.domain import song

    cfg, cards, charts = _real_inputs()
    chart = charts.get("m0334", "EXPERT")
    if chart is None:
        pytest.skip("m0334 EXPERT is not in the library")
    rng = random.Random(23)
    teams = [_random_team(rng, cards) for _ in range(24)]
    try:
        song.NOTE_WALK_KERNEL = notewalk.walk
        serial = [song.score_song_detailed(m, l, chart, cfg)[0].total_score for m, l in teams]
        got: dict[tuple[int, int], float] = {}

        def run(worker: int) -> None:
            for k, (m, l) in enumerate(teams):
                got[(worker, k)] = song.score_song_detailed(m, l, chart, cfg)[0].total_score

        threads = [threading.Thread(target=run, args=(w,)) for w in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        song.NOTE_WALK_KERNEL = None
    assert all(got[(w, k)] == serial[k] for w in range(6) for k in range(len(teams)))
