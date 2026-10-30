"""The compiled Connect assignment must equal the Python it replaces, to the bit.

`connect.assign` stands in for `node_values_from_recipes` + the candidate gains
+ the exact capacitated matching. Random plans are generated in the shapes the
plan cache really produces — and with the things that make a matching ambiguous
on purpose: repeated node values (equal gains), several cards in one group
(capacities), two characters wanting the same card, a leader who is also a
member — and compared with `==` on the multipliers AND the placements.
"""
from __future__ import annotations

import dataclasses
import random
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="no gcc to build the kernel")

from hbuilder.domain import board_plan as bp  # noqa: E402
from hbuilder.domain.board import STAT_NAMES, BoardEffect  # noqa: E402
from hbuilder.domain.card import Card, Stats  # noqa: E402

import connect  # noqa: E402

REPO = HERE.parents[2]
GENERATIONS = ["Myth", "3期生", "holoX", "ReGLOSS"]
GROUPINGS = ["grp-myth", "grp-gen_3", "grp-holox", "grp-nowhere"]
# card groups as `entries` carry them: id-sorted tuples, some with several cards
GROUPS = [("cc-a1", "cc-a2"), ("cc-b1",), ("cc-c1", "cc-c2", "cc-c3"), ("cc-d1",),
          ("cc-e1", "cc-e2"), ("cc-f1",)]
PERMILS = [300, 550, 900, 1200, 1500]
VALUES = [10.0, 25.0, 25.0, 50.0, 50.0, 78.0]          # repeats: equal gains
RATES = [0.01, 0.03, 0.04, 0.05, 0.1, 0.173]


def _card(n: int, rng: random.Random) -> Card:
    return Card(
        id=f"card-{n:05d}-5-uniq-0000-0{rng.randrange(3)}", member=f"Chr{n}",
        generations=frozenset(rng.sample(GENERATIONS, rng.choice([1, 1, 2]))),
        type="Cute", rarity="SSR", costume_name="c",
        stats=Stats(performance=rng.randrange(900, 4000), technique=rng.randrange(900, 4000),
                    sense=rng.randrange(900, 4000)),
        skills=(None, None, None), costume_skill=None)


def _effect(rng: random.Random) -> BoardEffect:
    kind = rng.choice(["stat", "stat", "stat", "rate", "rate", "song_score_bonus",
                       "skill_effect_up", "activation_rate_up", "cooldown_shorten", "ignored"])
    return BoardEffect(
        family="f", kind=kind,
        stat=rng.choice(["all", *STAT_NAMES]) if kind in ("stat", "rate") else "",
        value=rng.choice(VALUES) if kind == "stat" else rng.choice(RATES),
        scope=("member" if kind in ("activation_rate_up", "cooldown_shorten")
               else rng.choice(["leader", "member", "grouping", "account"])),
        song_scope=rng.choice(["all", "solo", "group", None]) if kind == "song_score_bonus" else None,
        conditional=rng.random() < 0.15)


def _plan(rng: random.Random):
    """`(AssembledPlan, recipes)` for one character."""
    node_ids = [f"{rng.choice('RGBY')}-{i:03d}" for i in range(rng.randrange(1, 18))]
    recipes = []
    for node_id in node_ids:
        if rng.random() < 0.12:
            recipes.append((node_id, {"tgt": f"character_grouping-{rng.choice(GROUPINGS)}-always",
                                      "v": rng.choice([20, 50])}, ()))
        elif rng.random() < 0.05:
            recipes.append((node_id, {"tgt": "not-a-grouping", "v": 50}, ()))
        else:
            recipes.append((node_id, None, tuple(_effect(rng) for _ in range(rng.choice([1, 1, 2])))))
    covered_pool = node_ids + ["X-999"]            # a covered node with no recipe
    entries = []
    for socket in rng.sample(["S-001", "S-002", "S-003", "S-004"], rng.randrange(0, 5)):
        for group in rng.sample(GROUPS, rng.randrange(1, len(GROUPS) + 1)):
            entries.append((socket, rng.choice(PERMILS),
                            tuple(rng.sample(covered_pool, rng.randrange(1, min(5, len(covered_pool)) + 1))),
                            group))
    asm = bp.AssembledPlan(nodes=(), model_id=rng.choice(["m", "m", "m", ""]),
                           entries=tuple(entries), needed=tuple(n for n, _o, _e in recipes))
    return asm, tuple(recipes)


def _team(rng: random.Random):
    cards = [_card(n, rng) for n in rng.sample(range(1, 40), 6)]
    members = tuple(cards[:5])
    leader = rng.choice([cards[5], cards[5], members[0]])     # sometimes also a member
    chars = [leader, *[m for m in members if m is not leader]]
    if rng.random() < 0.2:                                     # two skins of one character
        twin = dataclasses.replace(chars[1], id=chars[1].id[:-1] + "9")
        chars.append(twin)
    sings = rng.random() < 0.5
    inputs = []
    for card in chars:
        asm, recipes = _plan(rng)
        is_leader = card is leader
        inputs.append(("chr-" + card.id.split("-")[1], asm, recipes, is_leader, card,
                       1.0 if (is_leader and sings) else 0.0))
    scalars = dict(overall_power=rng.uniform(150_000, 700_000), bonus_permil=rng.uniform(0, 900),
                   k_constant=2.03734, active_pool_rate=rng.uniform(0.0, 1.2))
    return inputs, members, scalars


def _both(inputs, members, scalars, seen):
    def counting(*args, **kwargs):
        seen["calls"] += 1
        out = connect.assign(*args, **kwargs)
        seen["declined"] += out is None
        seen["placed"] += len(out or ())
        return out

    try:
        bp.CONNECT_KERNEL = None
        want = bp._cached_connect_assignment(inputs, members, **scalars)
        bp.CONNECT_KERNEL = counting
        got = bp._cached_connect_assignment(inputs, members, **scalars)
    finally:
        bp.CONNECT_KERNEL = None
    return want, got


def test_random_teams_get_the_same_sockets_with_the_kernel_injected():
    rng = random.Random(20261013)
    seen = {"calls": 0, "declined": 0, "placed": 0}
    for _ in range(4000):
        inputs, members, scalars = _team(rng)
        (want_mults, want_placed), (got_mults, got_placed) = _both(inputs, members, scalars, seen)
        assert got_mults == want_mults
        assert [dataclasses.astuple(p) for p in got_placed] == \
            [dataclasses.astuple(p) for p in want_placed]
    # the kernel really ran and really placed cards, rather than declining its way to a pass
    assert seen["calls"] == 4000 and seen["declined"] == 0 and seen["placed"] > 8000, seen


def test_the_song_scope_gives_the_same_sockets_with_the_kernel_injected():
    # the scorer pays ONE scope of yellows per song (`SongCast.scope`): the kernel's
    # `song_mask` must zero the other scope exactly as `node_values_from_recipes` does
    rng = random.Random(20261020)
    seen = {"calls": 0, "declined": 0, "placed": 0}
    for _ in range(1500):
        inputs, members, scalars = _team(rng)
        inputs = [(*i[:5], 1.0 if rng.random() < 0.6 else 0.0) for i in inputs]
        scalars["song_scope"] = rng.choice(["", "solo", "group", "all"])
        (want_mults, want_placed), (got_mults, got_placed) = _both(inputs, members, scalars, seen)
        assert got_mults == want_mults
        assert [dataclasses.astuple(p) for p in got_placed] ==             [dataclasses.astuple(p) for p in want_placed]
    assert seen["calls"] == 1500 and seen["declined"] == 0 and seen["placed"] > 3000, seen


def test_a_plan_the_kernel_cannot_express_is_declined_not_guessed():
    rng = random.Random(3)
    inputs, members, scalars = _team(rng)
    asm, recipes = inputs[0][1], inputs[0][2]
    odd = dataclasses.replace(
        asm, model_id="m", entries=(("SOCKET-A", 500, (recipes[0][0],), GROUPS[0]),))
    inputs[0] = (inputs[0][0], odd, *inputs[0][2:])
    assert connect.assign([i for i in inputs if i[1].model_id], members, **scalars) is None
    seen = {"calls": 0, "declined": 0, "placed": 0}
    want, got = _both(inputs, members, scalars, seen)
    assert got[0] == want[0] and seen["declined"] == 1


def test_the_leaf_scores_real_teams_identically_with_the_kernel_injected():
    """End to end on the enumeration's own leaf: real cards, the real plan cache
    and bench, the real chart — the developed score with and without it."""
    import argparse
    import importlib.util

    probe = REPO / "tools" / "recon" / "probes" / "members_first_2026_08_19.py"
    for extra in (REPO / "tools" / "web", REPO / "tools" / "recon" / "proxy"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    try:
        spec = importlib.util.spec_from_file_location("_mfp_connect_test", probe)
        mfp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mfp)
        built = mfp.build(argparse.Namespace(song="m0334", budget=453, uptime="finite",
                                             platform="mobile"), mfp.PROXIES["nonsinger"])
    except Exception as exc:  # noqa: BLE001 - no dumps on this machine
        pytest.skip(f"the enumeration inputs are not available here: {exc!r}")
    from hbuilder.domain.team import Team
    from hbuilder.solver import parallel_refine as pr

    reps, proxy, ctx = built["reps"], built["proxy"], built["ctx"]
    rng = random.Random(20261013)
    teams = []
    while len(teams) < 150:
        picked = rng.sample(reps, 5)
        if len({c.member for c in picked}) == 5:
            teams.append(Team(leader=proxy, members=tuple(picked)))
    seen = {"calls": 0, "declined": 0}

    def counting(*args, **kwargs):
        seen["calls"] += 1
        out = connect.assign(*args, **kwargs)
        seen["declined"] += out is None
        return out

    try:
        bp.CONNECT_KERNEL = None
        want = pr.RefinePool(ctx, workers=1).batch_score(teams)
        bp.CONNECT_KERNEL = counting
        got = pr.RefinePool(ctx, workers=1).batch_score(teams)
    finally:
        bp.CONNECT_KERNEL = None
    assert got == want
    assert seen["calls"] == len(teams) and seen["declined"] == 0, seen


def test_a_spawned_worker_picks_the_kernel_up_from_the_environment():
    import os
    import subprocess

    connect.load()
    code = (
        "from hbuilder.domain import board_plan as bp;"
        "k = bp._connect_kernel();"
        "print('KERNEL', None if k is None else k.__name__, bp.CONNECT_KERNEL)"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    env[bp.CONNECT_KERNEL_ENV] = f"{HERE / 'connect.py'}:assign"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "KERNEL assign None" in out.stdout, (out.stdout, out.stderr)

    env[bp.CONNECT_KERNEL_ENV] = f"{HERE / 'no_such_file.py'}:assign"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "KERNEL None None" in out.stdout and "could not be loaded" in out.stderr, (out.stdout, out.stderr)


def test_cards_held_on_the_bench_give_the_same_sockets_with_the_kernel_injected():
    """`held` (cards an off-team board keeps on a green): the kernel's packed
    capacities cannot shrink, so its answer stands only when it fits the cards left and the
    Python path decides otherwise -- either way the same multipliers and placements as the
    Python path with the held cards dropped."""
    rng = random.Random(20261019)
    ids = [c for g in GROUPS for c in g]
    for _ in range(4000):
        inputs, members, scalars = _team(rng)
        held = frozenset(rng.sample(ids, rng.randrange(0, 5)))
        try:
            bp.CONNECT_KERNEL = None
            want = bp._cached_connect_assignment(inputs, members, held=held, **scalars)
            bp.CONNECT_KERNEL = connect.assign
            got = bp._cached_connect_assignment(inputs, members, held=held, **scalars)
        finally:
            bp.CONNECT_KERNEL = None
        assert got[0] == want[0]
        assert [dataclasses.astuple(p) for p in got[1]] == [dataclasses.astuple(p) for p in want[1]]
        assert not {p.card for p in want[1]} & held
