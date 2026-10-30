"""Test tiers for a clone of the public repo.

Most tests run anywhere. The files in NEEDS_DATA score real teams on the game's own data (charts,
master tables, card art, an account's boards), which this repo does not carry: build it on your
own PC first (docs/run-locally.md: `refresh.cmd`). Without it they are
SKIPPED with that reason, never silently passed; with it they run like every other test.

NEEDS_MAINTAINER lists the few tests pinned to the maintainer's own account captures or notes, which
this repo never carries: wherever the file is missing they are SKIPPED, naming it.

KNOWN_FAILING are open issues tracked upstream: marked xfail (not hidden), so a fix shows up as
XPASS.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "tools" / "recon" / "proxy" / "dumps"
HAS_DATA = (DATA / "charts.json").is_file()

# measured: what fails in a clean clone and passes on a PC with the game data
NEEDS_DATA = {
    "tests/test_cli_heuristic.py", "tests/test_mode_b.py",
    "tests/test_model_version.py", "tests/test_refresh_all_append_guard.py",
    "tests/test_tree_optimise_scenarios.py",
    "tools/web/test_board_doc_route.py", "tools/web/test_board_plan_account_route.py",
    "tools/web/test_compare_route.py", "tools/web/test_difficulty.py",
    "tools/web/test_event_highlight.py", "tools/web/test_gacha_route.py",
    "tools/web/test_members_first_seeds.py", "tools/web/test_meta_route.py",
    "tools/web/test_optimize_rerank_route.py", "tools/web/test_routes.py",
    "tools/web/test_run_route.py", "tools/web/test_timeline_route.py",
}
_CAPTURE = "tests/fixtures/board_state_2026-08-09_pre_respec.json"   # one account's frozen boards
_GACHA_ART = "data/gacha/art/gacha-pickup-normal-260807"            # one account's banner art
NEEDS_MAINTAINER = {
    "tests/test_board_export.py::test_r061_is_locked_behind_r058_at_the_real_cost": _CAPTURE,
    "tests/test_board_landing.py::test_the_real_capture_converts_with_nothing_unresolved": _CAPTURE,
    "tests/test_tree_optimise_adjacency.py::test_cheapest_path_to_r061_costs_the_real_eight_points": _CAPTURE,
    "tests/test_model_version.py::test_model_version_matches_objective_v2_header": "tools/recon/OBJECTIVE-v2.md",
    "tools/web/test_gacha_route.py::test_gachaart_serves_png_and_rejects_bad_paths": _GACHA_ART,
    "tools/web/test_gacha_route.py::test_gachaart_extensionless_serves_whichever_file_exists": _GACHA_ART,
    "tools/web/test_gacha_route.py::test_gachaart_scales_to_webp_when_a_width_is_asked_for": _GACHA_ART,
}
KNOWN_FAILING = (
)


def pytest_ignore_collect(collection_path, config):
    # a file that cannot even be imported without the data
    rel = collection_path.resolve().relative_to(ROOT).as_posix() if ROOT in collection_path.resolve().parents else ""
    return (not HAS_DATA and rel in NEEDS_DATA) or None


def pytest_collection_modifyitems(config, items):
    for item in items:
        need = NEEDS_MAINTAINER.get(item.nodeid)
        if need and not (ROOT / need).exists():
            item.add_marker(pytest.mark.skip(reason=f"needs {need}, which this repository does not "
                                                    "carry (the maintainer's own captures and notes)"))
        if any(item.nodeid.startswith(k) for k in KNOWN_FAILING):
            item.add_marker(pytest.mark.xfail(reason="known failure, tracked upstream", strict=False))
