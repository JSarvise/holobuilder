"""In general on a maxed account, on the real catalogue and charts: the unpinned search serves at
least what known strong teams score on the same charts, and at least what the same search serves
with the leader pinned.

The known teams are the best ones of three leaders whose regions an unpinned search once left
out: Noel / Soleil Kiss (+135 % performance with 2+ Gen 3 members), Miko / Radiance Smile on the
same five members, and La+ / Sweet & Spicy on another set. They lean to performance while the
leaders that score best on a chart sample raise technique or sense, and Fubuki and Matsuri pay
only together. The served team may be another one; it may not score less. Every number is the
value the engine serves a row at (the better of its plain mean over every chart and its mean with
the frequency node counts chosen per chart), so the comparison is exact.

A search with the leader pinned to one card explores that leader's teams alone: whatever it
serves is a team the unpinned search could have served, so the unpinned #1 may not be below it.

Each case is several full searches on the engine's worker pool (minutes), so the module is marked
`recall` and deselected by default:

    python -m pytest tools/web/test_search_recall.py -m recall
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import ondemand
import pytest

from hbuilder import paths

KERNELS = Path(__file__).resolve().parent / "kernel"
if str(KERNELS) not in sys.path:
    sys.path.insert(0, str(KERNELS))

pytestmark = [
    pytest.mark.recall,
    pytest.mark.skipif(shutil.which("gcc") is None, reason="no gcc to build the screen"),
    pytest.mark.skipif(
        not all(
            (paths.DUMPS / p).exists()
            for p in ("charts.json", "charts_all.json", "holodori/main_Poster.json")
        ),
        reason="needs the catalogue's derived data (dumps/, gitignored)",
    ),
]

NOEL_SET = (
    "card-00006-5-uniq-0007-00",  # Fubuki / Golden Petal Dew
    "card-00007-5-uniq-0008-00",  # Matsuri / Sweet Deception
    "card-00021-5-uniq-0017-00",  # Flare / Azure Ribbon Chic
    "card-00022-5-uniq-0063-00",  # Noel / Soleil Kiss
    "card-03005-5-uniq-0084-00",  # Anya / Tribal Oasis
)
LAPLUS_SET = (
    "card-00006-5-uniq-0007-00",  # Fubuki / Golden Petal Dew
    "card-00007-5-uniq-0008-00",  # Matsuri / Sweet Deception
    "card-00032-5-uniq-0026-00",  # Botan / WH Lion Vanguard
    "card-03003-5-uniq-0115-00",  # Iofifteen / Sunlit Daydream
    "card-06005-5-uniq-0061-00",  # Hajime / Sweet Badass Style
)
NOEL = "card-00022-5-uniq-0063-00"  # Shirogane Noel / Soleil Kiss
MIKO = "card-00015-5-uniq-0067-00"  # Sakura Miko / Radiance Smile
LAPLUS = "card-00035-5-uniq-0082-00"  # La+ Darknesss / Sweet & Spicy
KNOWN = {
    "Noel / Soleil Kiss": (NOEL, NOEL_SET),
    "Miko / Radiance Smile": (MIKO, NOEL_SET),
    "La+ / Sweet & Spicy": (LAPLUS, LAPLUS_SET),
}
# the leaders pinned: the three above, and the best technique, sense and gated-performance ones
PINNED = (
    LAPLUS,
    MIKO,
    NOEL,
    "card-00017-5-uniq-0077-00",  # Inugami Korone / Stripe Strike
    "card-00018-5-uniq-0068-00",  # Hoshimachi Suisei / Melody of the Tides
    "card-04014-5-uniq-0094-00",  # Koseki Bijou / Glittering Gem
)
CASES = pytest.mark.parametrize("levels", [None, (25, 30)], ids=["every EXPERT chart", "levels 25-30"])


@pytest.fixture
def kernels(monkeypatch):
    """The compiled note walk, Connect assignment and board assembly, as the server runs them
    (bit-identical to the Python path), switched off again after the test."""
    from hbuilder.domain import board_plan, song

    for module, attr, env in (
        (song, "NOTE_WALK_KERNEL", song.NOTE_WALK_KERNEL_ENV),
        (board_plan, "CONNECT_KERNEL", board_plan.CONNECT_KERNEL_ENV),
        (board_plan, "BOARD_KERNEL", board_plan.BOARD_KERNEL_ENV),
    ):
        monkeypatch.setattr(module, attr, getattr(module, attr))
        monkeypatch.setenv(env, "")
    import connect
    import notewalk

    notewalk.enable()
    connect.enable()


def served_value(eng, team) -> float:
    """What the engine serves `team` at: the better of its plain mean over every chart and its
    mean with the frequency node counts chosen per chart."""
    plain = float(eng.pool.batch_score([team])[0])
    counted = float(eng.pool.batch_score([team], refine_activation=ondemand.COUNTS)[0])
    return max(plain, counted)


@CASES
def test_in_general_maxed_serves_at_least_the_known_teams(kernels, levels):
    from hbuilder.domain.team import Team

    eng = ondemand.Engine("maxed", "EXPERT", levels=levels)
    try:
        by_id = {c.id: c for c in eng.reps}
        known = {
            name: Team(leader=by_id[leader], members=tuple(by_id[m] for m in members))
            for name, (leader, members) in KNOWN.items()
            if leader in by_id and all(m in by_id for m in members)
        }
        if not known:
            pytest.skip("the catalogue no longer holds the reference teams")
        values = {name: served_value(eng, team) for name, team in known.items()}
        served = eng.search(None)["ranking"][0]
    finally:
        eng.close()
    below = {name: v for name, v in values.items() if served["best"] < v}
    assert not below, (
        f"served {served['leader_name']} + {served['member_names']} {served['best']:,.0f} < "
        + ", ".join(f"{name} {v:,.0f}" for name, v in below.items())
    )


@CASES
def test_no_pinned_leader_beats_the_unpinned_search(kernels, levels):
    eng = ondemand.Engine("maxed", "EXPERT", levels=levels)
    try:
        ids = {c.id for c in eng.reps}
        plain = eng.search(None)["ranking"][0]
        pinned = {}
        for leader in PINNED:
            if leader not in ids:
                continue
            eng.hints.clear()
            row = eng.search(None, ondemand.Filters(leader_card=leader))["ranking"][0]
            pinned[row["leader_name"]] = row["best"]
    finally:
        eng.close()
    above = {name: v for name, v in pinned.items() if v > plain["best"]}
    assert not above, f"unpinned #1 {plain['leader_name']} {plain['best']:,.0f} < pinned " + ", ".join(
        f"{name} {v:,.0f}" for name, v in above.items()
    )
