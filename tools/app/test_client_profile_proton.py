"""client_profile on Linux: the profile the client sends under Proton, read from a sample prefix
(system.reg, user.reg, Player.log) and a fake /sys and /proc. Never a made-up profile."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import client_profile as cp  # noqa: E402

SYSTEM_REG = r"""WINE REGISTRY Version 2
;; All keys relative to \\Machine

#arch=win64

[Software\\Microsoft\\Windows NT\\CurrentVersion] 1793700000
#time=1db1234567890ab
"CurrentBuild"="19045"
"CurrentBuildNumber"="19045"
"CurrentMajorVersionNumber"=dword:0000000a
"ProductName"="Windows 10 Pro"

[Software\\Valve\\Steam] 1793700000
"InstallPath"="C:\\Program Files (x86)\\Steam"
"""

USER_REG = r"""WINE REGISTRY Version 2
;; All keys relative to \\User\\S-1-5-21-0-0-0-1000

[Control Panel\\Desktop] 1793700000
"LogPixels"=dword:00000078
"""

PLAYER_LOG = """Initialize engine version: 2022.3.62f1 (abc)
[Subsystems] Discovering subsystems at path Z:/steam/hololiveDreams/hololive-Dreams_Data/UnitySubsystems
GfxDevice: creating device client; threaded=1; jobified=0
Direct3D:
    Version:  Direct3D 11.0 [level 11.1]
    Renderer: NVIDIA GeForce RTX 3070 (ID=0x2484)
    Vendor:   NVIDIA
    VRAM:     7957 MB
"""


def _pc(
    tmp_path: Path,
    *,
    player_log: str | None = PLAYER_LOG,
    user_reg: str | None = USER_REG,
    bios_in_reg: bool = False,
) -> tuple[Path, Path]:
    """A Steam library with the game's prefix, and a root with /sys and /proc -> (game data, root)."""
    pfx = tmp_path / "lib" / "steamapps" / "compatdata" / "4282500" / "pfx"
    game_data = (
        pfx
        / "drive_c"
        / "users"
        / "steamuser"
        / "AppData"
        / "LocalLow"
        / "QualiArts, Inc_"
        / "hololive-Dreams"
    )
    game_data.mkdir(parents=True)
    system = SYSTEM_REG
    if bios_in_reg:
        system += (
            "\n[Hardware\\\\Description\\\\System\\\\BIOS] 1793700000\n"
            '"SystemManufacturer"="Reg Maker"\n"SystemProductName"="Reg Board"\n'
            "\n[Hardware\\\\Description\\\\System\\\\CentralProcessor\\\\0] 1793700000\n"
            '"ProcessorNameString"="Reg CPU 8-Core Processor      "\n'
        )
    (pfx / "system.reg").write_text(system, "utf8")
    if user_reg is not None:
        (pfx / "user.reg").write_text(user_reg, "utf8")
    if player_log is not None:
        (game_data / "Player.log").write_text(player_log, "utf8")
    root = tmp_path / "root"
    dmi = root / "sys" / "class" / "dmi" / "id"
    dmi.mkdir(parents=True)
    (dmi / "product_name").write_text("B650M-A\n")
    (dmi / "sys_vendor").write_text("Example Vendor Co., Ltd.\n")
    for name, status, modes in (
        ("card0-DP-1", "disconnected", ""),
        ("card0-HDMI-A-1", "connected", "2560x1440\n1920x1080\n"),
    ):
        conn = root / "sys" / "class" / "drm" / name
        conn.mkdir(parents=True)
        (conn / "status").write_text(status + "\n")
        (conn / "modes").write_text(modes)
    (root / "proc").mkdir()
    (root / "proc" / "cpuinfo").write_text(
        "processor\t: 0\nmodel name\t: AMD Ryzen 5 7600 6-Core Processor\n"
    )
    (root / "proc" / "meminfo").write_text("MemTotal:       32542068 kB\nMemFree:  1 kB\n")
    return game_data, root


def test_the_wine_registry_is_read_as_wine_writes_it():
    reg = cp.wine_reg(SYSTEM_REG)
    v = reg[r"software\microsoft\windows nt\currentversion"]
    assert v["currentbuild"] == "19045" and v["currentmajorversionnumber"] == 10
    assert reg[r"software\valve\steam"]["installpath"] == r"C:\Program Files (x86)\Steam"


def test_player_log_gives_the_gpu_unity_reports():
    assert cp.player_log_gpu(PLAYER_LOG) == {
        "name": "NVIDIA GeForce RTX 3070",
        "vendor": "4318",
        "vram_mb": 7957,
    }
    assert cp.player_log_gpu("no graphics here") is None


def test_a_proton_pc_gives_the_headers_the_client_sends_there(tmp_path):
    game_data, root = _pc(tmp_path)
    facts = cp.probe_proton(game_data, root)
    h = cp.headers(facts, version="1.2.101", steam_id="76561198000000001")
    assert h["x-app-os-version"] == "Windows 10  (10.0.19045)"
    assert h["x-app-device-name"] == "B650M-A (Example Vendor Co., Ltd.)"
    assert h["x-app-cpu"] == "AMD Ryzen 5 7600 6-Core Processor"
    assert h["x-app-gpu"] == "NVIDIA GeForce RTX 3070"
    assert h["x-app-graphics-device-vendor-id"] == "4318"
    assert h["x-app-graphics-device-memory-size"] == "7957"
    assert h["x-app-system-memory-size"] == str(32542068 // 1024)
    assert (h["x-app-screen-width"], h["x-app-screen-height"], h["x-app-screen-dpi"]) == (
        "2560",
        "1440",
        "120",
    )
    assert h["x-app-os-type"] == "Windows" and h["user-agent"] == cp.FIXED["user-agent"]


def test_what_wine_saved_in_the_prefix_wins_over_linux(tmp_path):
    game_data, root = _pc(tmp_path, bios_in_reg=True, user_reg=None)
    facts = cp.probe_proton(game_data, root)
    assert (facts["product"], facts["maker"]) == ("Reg Board", "Reg Maker")
    assert cp.cpu_name(facts["cpu"]) == "Reg CPU 8-Core Processor "
    assert facts["screen"][2] == 96  # Wine's default DPI


def test_without_the_games_player_log_there_is_no_profile(tmp_path):
    game_data, root = _pc(tmp_path, player_log=None)
    with pytest.raises(cp.ProfileUnavailable, match="start hololive Dreams once"):
        cp.probe_proton(game_data, root)


def test_without_a_prefix_there_is_no_profile(tmp_path):
    with pytest.raises(cp.ProfileUnavailable, match="client_profile.json"):
        cp.probe_proton(tmp_path / "nowhere", tmp_path)


def test_linux_steam_gives_the_running_account_then_the_most_recent(tmp_path):
    steam = tmp_path / ".local" / "share" / "Steam"
    (steam / "config").mkdir(parents=True)
    (steam / "config" / "loginusers.vdf").write_text(
        '"users"\n{\n\t"76561198000000002"\n\t{\n\t\t"MostRecent"\t\t"1"\n\t}\n}'
    )
    assert cp.steam_id_linux([str(steam)]) == "76561198000000002"
    (steam.parent / "registry.vdf").write_text('"Registry"\n{\n\t"ActiveUser"\t\t"39999999"\n}')
    assert cp.steam_id_linux([str(steam)]) == "76561198000265727"


def test_linux_writes_its_profile_and_keeps_one_put_there(tmp_path):
    game_data, root = _pc(tmp_path)
    facts = cp.probe_proton(game_data, root)
    p = tmp_path / "client_profile.json"
    r = cp.ensure(None, path=p, facts=facts, find_steam=lambda: None, version="1.2.101", platform="linux")
    assert r == {"captured": False, "written": True, "steam_id": None}
    assert json.loads(p.read_text())["source"] == cp.GENERATED_PROTON
    # a profile copied from a Windows PC (generated or captured) is never replaced on Linux
    for source in (cp.GENERATED, "captured from the client"):
        p.write_text(json.dumps({"headers": {"x-app-cpu": "kept"}, "source": source}))
        r = cp.ensure(None, path=p, facts=facts, find_steam=lambda: None, version="1.2.101", platform="linux")
        assert r["captured"] and json.loads(p.read_text())["headers"] == {"x-app-cpu": "kept"}


def test_linux_with_neither_stops_and_writes_nothing(tmp_path, monkeypatch):
    p = tmp_path / "client_profile.json"
    monkeypatch.setattr(cp, "probe_proton", lambda: (_ for _ in ()).throw(cp.ProfileUnavailable("x")))
    with pytest.raises(cp.ProfileUnavailable):
        cp.ensure(None, path=p, find_steam=lambda: None, version="1.2.101", platform="linux")
    assert not p.exists()
