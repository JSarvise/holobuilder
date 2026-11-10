"""Test tiers for a clone of the public repo.

Most tests run anywhere. The files in NEEDS_DATA score real teams on the game's own data (charts,
master tables, card art, an account's boards), which this repo does not carry: build it on your
own PC first (docs/run-locally.md: `refresh.cmd` or `refresh.sh`). Without it they are
SKIPPED with that reason, never silently passed; with it they run like every other test.

NEEDS_MAINTAINER lists the few tests pinned to the maintainer's own account captures or notes, which
this repo never carries: wherever the file is missing they are SKIPPED, naming it.

NEEDS_ACCOUNT lists tests that read your own account as a refresh leaves it (your cards, your
boards): SKIPPED, naming the file, until a refresh has built it.

KNOWN_FAILING are open issues tracked upstream: marked xfail (not hidden), so a fix shows up as
XPASS.

Every session also installs the write guard (tests/support/write_guard.py) before any test module
is imported: no test may write into your real `state/` or `tools/recon/proxy/dumps/`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))  # `support`, as the tests themselves import it
from support import write_guard  # noqa: E402

sys.path.remove(str(ROOT / "tests"))
write_guard.install()

DATA = ROOT / "tools" / "recon" / "proxy" / "dumps"
HAS_DATA = (DATA / "charts.json").is_file()

# what fails in a clean clone and passes on a PC with the game data
NEEDS_DATA = {
    "tests/test_model_version.py",
    "tests/test_refresh_all_append_guard.py",
    "tests/test_tree_optimise_scenarios.py",
    "tools/web/test_board_doc_route.py",
    "tools/web/test_board_plan_account_route.py",
    "tools/web/test_compare_route.py",
    "tools/web/test_difficulty.py",
    "tools/web/test_event_highlight.py",
    "tools/web/test_gacha_route.py",
    "tools/web/test_meta_route.py",
    "tools/web/test_routes.py",
    "tools/web/test_timeline_route.py",
}
_CAPTURE = "tests/fixtures/board_state_2026-08-09_pre_respec.json"  # one account's frozen boards
_GACHA_ART = "data/gacha/art/gacha-pickup-normal-260807"  # one account's banner art
NEEDS_MAINTAINER = {
    "tests/test_board_export.py::test_r061_is_locked_behind_r058_at_the_real_cost": _CAPTURE,
    "tests/test_board_landing.py::test_the_real_capture_converts_with_nothing_unresolved": _CAPTURE,
    "tests/test_tree_optimise_adjacency.py::test_cheapest_path_to_r061_costs_the_real_eight_points": _CAPTURE,
    "tests/test_model_version.py::test_model_version_matches_objective_v2_header": "docs/dev/objective.md",
    "tools/web/test_gacha_route.py::test_gachaart_serves_png_and_rejects_bad_paths": _GACHA_ART,
    "tools/web/test_gacha_route.py::test_gachaart_extensionless_serves_whichever_file_exists": _GACHA_ART,
    "tools/web/test_gacha_route.py::test_gachaart_scales_to_webp_when_a_width_is_asked_for": _GACHA_ART,
}
# what reads your own account as a refresh leaves it (your cards, your boards)
NEEDS_ACCOUNT = {
    "tools/web/test_leaf_inputs.py::test_the_maxed_basis_scores_every_bench_holomem_on_a_complete_board": "data/owned.json",
    "tools/web/test_leaf_inputs.py::test_the_maxed_bench_as_a_board_state_is_every_whole_tree_with_its_connect_cards": "data/owned.json",
    "tools/web/test_leaf_inputs.py::test_the_maxed_bench_targets_are_only_what_scores": "data/owned.json",
    "tools/refresh/test_board_plan_solver_ties.py::test_no_refund_charges_only_new_buys_against_the_inventory": "tools/recon/proxy/dumps/board_state.json",
}
KNOWN_FAILING = ()


class _NeedsData(pytest.Module):
    """A test file that cannot even be imported without the game data: never imported,
    reported as one skipped file with the reason."""

    def collect(self):
        pytest.skip(
            f"{self.nodeid} needs the game data (tools/recon/proxy/dumps), which this "
            "repository does not carry: build it on your own PC first "
            "(docs/run-locally.md: refresh.cmd or refresh.sh)",
            allow_module_level=True,
        )


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makemodule(module_path, parent):
    path = module_path.resolve()
    rel = path.relative_to(ROOT).as_posix() if ROOT in path.parents else ""
    if not HAS_DATA and rel in NEEDS_DATA:
        return _NeedsData.from_parent(parent, path=module_path)
    return None


def pytest_collection_modifyitems(config, items):
    for item in items:
        need = NEEDS_MAINTAINER.get(item.nodeid)
        if need and not (ROOT / need).exists():
            item.add_marker(
                pytest.mark.skip(
                    reason=f"needs {need}, which this repository does not "
                    "carry (the maintainer's own captures and notes)"
                )
            )
        need = NEEDS_ACCOUNT.get(item.nodeid)
        if need and not (ROOT / need).exists():
            item.add_marker(
                pytest.mark.skip(
                    reason=f"needs {need}, which a refresh builds from your own account (docs/run-locally.md)"
                )
            )
        if any(item.nodeid.startswith(k) for k in KNOWN_FAILING):
            item.add_marker(pytest.mark.xfail(reason="known failure, tracked upstream", strict=False))
