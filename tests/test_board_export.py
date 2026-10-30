"""The per-character board export (`tools/recon/proxy/board_export.py`) must carry
the fixed-graph costs and states the frontend renders.

Dumps-backed and skips without them, like the other recon-backed tests. The load
-bearing check is that R-061 is LOCKED behind R-058 at the real 8 points -- the
same 4-neighbour adjacency `test_tree_optimise_adjacency.py` guards at the graph
level, here proven to reach the exported artifact the UI consumes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PROXY = _REPO / "tools" / "recon" / "proxy"
_DUMPS = _PROXY / "dumps"


def _load_board_export():
    spec = importlib.util.spec_from_file_location(
        "board_export", _PROXY / "board_export.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["board_export"] = module
    spec.loader.exec_module(module)
    return module


def _build_or_skip(**kw) -> dict:
    for name in ("skilltree.json", "board_state.json", "account_snapshot_main.json"):
        if not (_DUMPS / name).exists():
            pytest.skip(f"dumps/{name} unavailable (gitignored account data)")
    return _load_board_export().build(gain=kw.get("gain", False), pool=kw.get("pool", 60),
                                      board_state_path=kw.get("board_state_path"))


def test_export_shape_and_every_node_has_a_state():
    doc = _build_or_skip()
    assert doc["schema"] == "board-export/v0"
    assert doc["characters"], "no characters exported"
    for character in doc["characters"]:
        states = {n["state"] for n in character["nodes"]}
        assert states <= {"owned", "buyable", "locked", "unreachable"}
        assert "unreachable" not in states, character["chr"]  # the tree is connected
        # sockets are a subset view of the S- nodes
        assert {s["socket"] for s in character["sockets"]} <= {
            "S-001", "S-002", "S-003", "S-004"
        }


def test_r061_is_locked_behind_r058_at_the_real_cost():
    """4-neighbour adjacency reaching the exported artifact: chr-04003 owns R-055;
    a 6-direction hex graph would make R-061 `buyable` for 4 pt, but on the real
    4-neighbour graph it is `locked` behind R-058, an 8-point unlock path.

    Pinned to a frozen board capture (`tests/fixtures/…pre_respec.json`), not
    the live one, because the witness needs a board that owns R-055. The
    property guarded — the 4-neighbour graph in the exported artifact — is
    state-independent; its witness is not."""
    fixture = Path(__file__).parent / "fixtures" / "board_state_2026-08-09_pre_respec.json"
    doc = _build_or_skip(board_state_path=str(fixture))
    ina = next(c for c in doc["characters"] if c["chr"] == "chr-04003")
    by_id = {n["id"]: n for n in ina["nodes"]}
    assert by_id["R-055"]["state"] == "owned"
    r061 = by_id["R-061"]
    assert r061["state"] == "locked"
    assert r061["unlock_path"] == ["R-058", "R-061"]
    assert r061["unlock_points"] == 8


def test_gain_is_present_only_on_pooled_characters_and_never_scores_life_nodes():
    """`gain` is the marginal pool re-score, so it exists only where a character
    is in the fixed pool; and it must SKIP life_up nodes (no score model, by
    design) rather than crash on them."""
    doc = _build_or_skip(gain=True)
    assert doc["gain_included"] is True
    with_gain = [c for c in doc["characters"]
                 if any(n["gain"] is not None for n in c["nodes"])]
    assert with_gain, "no character received a gain value"
    # a gain value only ever attaches to a buyable, non-socket node
    for character in doc["characters"]:
        for node in character["nodes"]:
            if node["gain"] is not None:
                assert node["state"] == "buyable" and not node["id"].startswith("S-")


def test_icon_name_and_chr_are_inverse():
    """The icon<->chr mapping is a pure code-strip; the reverse check is the guard
    that a decoded asset can never be attributed to the wrong character."""
    spec = importlib.util.spec_from_file_location("member_icons", _PROXY / "member_icons.py")
    member_icons = importlib.util.module_from_spec(spec)
    sys.modules["member_icons"] = member_icons
    spec.loader.exec_module(member_icons)
    assert member_icons.icon_name("chr-04003") == "img_chr_icon_normal_04003"
    assert member_icons.icon_name("chr-04003", "img_chr_icon_sd_") == "img_chr_icon_sd_04003"
    assert member_icons._chr_of("img_chr_icon_normal_04003") == "chr-04003"
    assert member_icons._chr_of("img_chr_icon_sd_00001") == "chr-00001"
    assert member_icons._chr_of("no-digits-here") is None


def test_icon_points_at_the_characters_own_png_or_is_null():
    """When member_icons.py has run, each `icon` is that character's own chr-keyed
    ref -- never null-with-a-file-present, never another character's file."""
    doc = _build_or_skip()
    icons_dir = _DUMPS / "member_icons"
    if not any(icons_dir.glob("chr-*.png")):
        pytest.skip("dumps/member_icons/ not populated; run member_icons.py")
    for character in doc["characters"]:
        cid = character["chr"]
        if (icons_dir / f"{cid}.png").exists():
            assert character["icon"] == f"member_icons/{cid}.png"
        else:
            assert character["icon"] is None
