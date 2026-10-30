"""The game's own chibi bone map (read out of the binary by chibi_human_map.py) agrees with the
map chibi_build bakes the run on. Needs the memory dump and the installed client; skips without.
    python -m pytest tools/recon/proxy/test_chibi_human_map.py
"""
from __future__ import annotations

import os

import pytest

import chibi_human_map as hm


def _metadata():
    try:
        import game_paths
        return os.path.join(game_paths.game_root(), "hololive-Dreams_Data", "il2cpp_data",
                            "Metadata", "global-metadata.dat")
    except SystemExit:
        return None


def test_the_games_bone_map_is_the_one_the_chibis_are_baked_on():
    meta = _metadata()
    if not (os.path.exists(hm.DUMP) and meta and os.path.exists(meta)):
        pytest.skip("needs dumps/v7/GameAssembly_dumped.bin and the installed client")
    game = hm.read_map(meta)                     # raises when rank pairing and names disagree
    assert len(game) == 51 and game["jnt_C_hips00_00"] == "Hips" and game["jnt_C_spine00_01"] == "Chest"
    import chibi_build
    import re
    unity = lambda b: re.sub(r"([A-Za-z]+?)([123])$",
                             lambda m: m.group(1) + ("Proximal", "Intermediate", "Distal")[int(m.group(2)) - 1], b)
    for bone, joint in chibi_build.HUMAN.items():
        assert game.get(joint) == unity(bone), joint


def test_names_say_their_human_bone():
    assert hm.by_name("jnt_L_foreArm00_00") == "LeftLowerArm"
    assert hm.by_name("jnt_R_fingerPinky00_02") == "RightLittleDistal"
    assert hm.by_name("jnt_C_headHair00_00") is None


def test_the_twist_split_is_the_games():
    if not os.path.exists(hm.DUMP):
        pytest.skip("needs dumps/v7/GameAssembly_dumped.bin")
    import humanoid
    assert hm.twist_split() == humanoid.GAME_TWIST
