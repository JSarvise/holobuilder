"""In general on a maxed account, on the real catalogue and charts: the served #1 scores at least
what a known strong team scores on the same charts.

The team is Noel / Soleil Kiss leading Fubuki, Matsuri, Flare, Noel and Anya. Its outfit gives
+135 % performance only with 2+ Gen 3 members, so its best sets are performance-heavy sets holding a
Gen 3 pair, a region the leaders that rank best elsewhere value less. The served team may be
another one; it may not score less. Both numbers are the same leaf (`RefinePool.batch_score` on
every chart of the engine), so the comparison is exact.

Each case is a full search on the engine's worker pool (a few minutes), so the module is marked
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

LEADER = "card-00022-5-uniq-0063-00"  # Shirogane Noel / Soleil Kiss
MEMBERS = (
    "card-00006-5-uniq-0007-00",
    "card-00007-5-uniq-0008-00",
    "card-00021-5-uniq-0017-00",
    "card-00022-5-uniq-0063-00",
    "card-03005-5-uniq-0084-00",
)


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


@pytest.mark.parametrize("levels", [None, (25, 30)], ids=["every EXPERT chart", "levels 25-30"])
def test_in_general_maxed_serves_at_least_noel_soleil_kiss(kernels, levels):
    from hbuilder.domain.team import Team

    eng = ondemand.Engine("maxed", "EXPERT", levels=levels)
    try:
        by_id = {c.id: c for c in eng.reps}
        if LEADER not in by_id or not all(m in by_id for m in MEMBERS):
            pytest.skip("the catalogue no longer holds the reference team")
        reference = Team(leader=by_id[LEADER], members=tuple(by_id[m] for m in MEMBERS))
        reference_value = eng.pool.batch_score([reference])[0]
        served = eng.search(None)["ranking"][0]
    finally:
        eng.close()
    assert served["best"] >= reference_value, (
        f"served {served['leader_name']} + {served['member_names']} {served['best']:,.0f} "
        f"< Noel / Soleil Kiss {reference_value:,.0f}"
    )
