"""park_map's world -> map-pixel transform (THE CALIBRATION in park_map.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import park_map as M  # noqa: E402

EXPORT = HERE / "dumps" / "park_export" / "park_spots.json"


def test_each_map_projects_with_its_own_factor():
    # the client's anchored position x*ratio + off, times the map's own root-unit -> pixel factor
    assert M.project(10, 5, (2000, 1000), (2.0, 3.0, -1.0, 1.5, 2.0)) == (1000 + 1.5 * 23, 500 - 2.0 * 9)


@pytest.mark.skipif(not EXPORT.is_file(), reason="no park export on this checkout")
def test_the_exported_factors_are_the_maps_own():
    """a01 keeps its ~1.2 (its layout gives 1.1845); the Play Area and Lobby maps,
    drawn at Scale 0.52 / 0.30, are ~1.92 / ~3.33 -- a single K = 1.2 would draw their markers
    at 62 % / 36 % of the way out from the centre."""
    areas = json.loads(EXPORT.read_text("utf8"))["areas"]
    k = {a: v["calibration"]["kx"] for a, v in areas.items()}
    assert abs(k["area-a01"] - 1.1845) < 0.001
    assert abs(k["area-a02-game"] - 1.9231) < 0.001 and abs(k["area-a02-casino"] - 1.9231) < 0.001
    assert abs(k["area-a02-lobby"] - 3.3333) < 0.001
    assert "area-a03" in areas and areas["area-a03"]["name"] == "Soda Pop Lagoon"
