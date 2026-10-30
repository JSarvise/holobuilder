"""client_profile: this PC's hardware in the client's own header formats. Pure parts only."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client_profile as cp  # noqa: E402


def test_formats_match_the_captured_profile():
    assert cp.os_version("10", 26200) == "Windows 11  (10.0.26200)"
    assert cp.os_version("10", 19045) == "Windows 10  (10.0.19045)"
    assert cp.device_name("B550M-X", "Example Board Co., Ltd.") == \
        "B550M-X (Example Board Co., Ltd.)"
    assert cp.vendor_id("PCI\VEN_10DE&DEV_2487&SUBSYS_1") == "4318"
    assert cp.vendor_id(None) == "0"
    assert cp.cpu_name("AMD Ryzen 5 5600X 6-Core Processor             ") == \
        "AMD Ryzen 5 5600X 6-Core Processor "
    assert cp.cpu_name("Intel(R) Core(TM) i5") == "Intel(R) Core(TM) i5"


def test_the_discrete_gpu_wins_and_the_steam_id_is_optional():
    gpu = cp.pick_gpu([{"name": "Intel UHD", "memory": 128 << 20, "vendor": "32902"},
                       {"name": "RTX 3060", "memory": 6 << 30, "vendor": "4318"}])
    facts = {"product": "X", "maker": "Y", "cpu": "CPU ", "threads": 8, "ram_mb": 16000,
             "gpu": gpu, "screen": [2560, 1440, 120], "build": 26100, "release": "10"}
    h = cp.headers(facts, version="1.2.1")
    assert h["x-app-gpu"] == "RTX 3060" and h["x-app-graphics-device-memory-size"] == "6144"
    assert h["x-app-processor-count"] == "8" and h["x-app-screen-width"] == "2560"
    assert "x-app-steam-id" not in h
    assert cp.headers(facts, version="1.2.1", steam_id="765")["x-app-steam-id"] == "765"


def test_the_steam_id_is_read_off_steam():
    assert cp.steam_id64(39999999) == "76561198000265727"
    assert cp.steam_id64(0) is None and cp.steam_id64(None) is None and cp.steam_id64("x") is None
    assert cp.valid_steam_id("76561198000265727") and not cp.valid_steam_id("7656119800026572")
    one = '"users"\n{\n\t"76561198000000001"\n\t{\n\t\t"AccountName"\t\t"a"\n\t}\n}'
    two = ('"users"\n{\n\t"76561198000000001"\n\t{\n\t\t"MostRecent"\t\t"0"\n\t}\n'
           '\t"76561198000000002"\n\t{\n\t\t"mostrecent"\t\t"1"\n\t}\n}')
    assert cp.most_recent_login(one) == "76561198000000001"
    assert cp.most_recent_login(two) == "76561198000000002"
    assert cp.most_recent_login(two.replace('"1"', '"0"')) is None     # two people, no guess
    log = "Direct3D:\n    Version:  Direct3D 11.0 [level 11.1]\n    VRAM:     15977 MB\n"
    assert cp.player_log_vram(log) == 15977 and cp.player_log_vram("nothing") is None


FACTS = {"product": "X", "maker": "Y", "cpu": "CPU ", "threads": 8, "ram_mb": 16000,
         "gpu": {"name": "G", "memory": 6 << 30, "vendor": "4318"}, "vram_mb": 5900,
         "screen": [1920, 1080, 96], "build": 26100, "release": "10"}


def test_ensure_fills_a_generated_profile_and_never_touches_a_captured_one(tmp_path):
    import json
    p = tmp_path / "client_profile.json"
    kw = {"path": p, "facts": FACTS, "version": "1.2.1"}
    r = cp.ensure(None, find_steam=lambda: None, **kw)                  # no Steam: written, flagged
    assert r == {"captured": False, "written": True, "steam_id": None}
    h = json.loads(p.read_text())["headers"]
    assert "x-app-steam-id" not in h and h["x-app-graphics-device-memory-size"] == "5900"
    r = cp.ensure("", find_steam=lambda: "76561198000000001", **kw)    # Steam signed in later
    assert r["written"] and r["steam_id"] == "found"
    assert json.loads(p.read_text())["headers"]["x-app-steam-id"] == "76561198000000001"
    assert not cp.ensure("", find_steam=lambda: "76561198000000001", **kw)["written"]   # unchanged
    r = cp.ensure("76561198000000009", find_steam=lambda: "76561198000000001", **kw)    # typed wins
    assert r["steam_id"] == "typed"
    assert json.loads(p.read_text())["headers"]["x-app-steam-id"] == "76561198000000009"
    assert cp.ensure("12", find_steam=lambda: "76561198000000001", **kw)["steam_id"] == "found"
    captured = {"headers": {"x-app-steam-id": "76561198000000003"}, "source": "extracted from x.mitm"}
    p.write_text(json.dumps(captured))
    assert cp.ensure("76561198000000009", find_steam=lambda: None, **kw) == \
        {"captured": True, "written": False, "steam_id": "found"}
    assert json.loads(p.read_text()) == captured


def test_settings_refuse_a_steam_id_that_is_not_one():
    import app_settings
    base = dict(app_settings.DEFAULTS)
    assert app_settings.validate({**base, "steam_id": "123"})["steam_id"]
    assert "steam_id" not in app_settings.validate({**base, "steam_id": ""})
    assert "steam_id" not in app_settings.validate({**base, "steam_id": "76561198000000001"})
