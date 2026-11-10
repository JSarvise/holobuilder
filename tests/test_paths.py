"""hbuilder.paths: every location comes from the checkout the module sits in, and the game
install is looked for in every Steam library, as Steam lists them."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hbuilder import paths

ROOT = Path(__file__).resolve().parents[1]

VDF = r""""libraryfolders"
{
	"0"
	{
		"path"		"C:\\Program Files (x86)\\Steam"
		"label"		""
		"apps"
		{
			"228980"		"394349418"
		}
	}
	"1"
	{
		"path"		"F:\\Games\\SteamLibrary"
	}
}
"""


def test_the_repo_is_the_checkout_holding_the_module():
    assert paths.REPO == ROOT
    assert paths.DUMPS == ROOT / "tools" / "recon" / "proxy" / "dumps"
    assert paths.DUMPS == paths.REPO / paths.DUMPS_REL
    assert paths.REFRESH_DIR == ROOT / "tools" / "refresh"
    assert (paths.REFRESH_DIR / "refresh_all.py").is_file()


def test_a_script_of_another_checkout_refuses_this_package(tmp_path):
    """A script run from one checkout that imports hbuilder from another (an editable install
    of a different tree) would read that tree's data: the import fails instead."""
    other = tmp_path / "other"
    (other / "src" / "hbuilder").mkdir(parents=True)
    shutil.copy2(ROOT / "src" / "hbuilder" / "paths.py", other / "src" / "hbuilder" / "paths.py")
    script = other / "tools" / "x.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        f"import sys\nsys.path.insert(0, {str(ROOT / 'src')!r})\nfrom hbuilder import paths\n", "utf8"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env)
    assert proc.returncode != 0 and "put " in proc.stderr and "first on sys.path" in proc.stderr


def test_library_paths_reads_steams_list():
    assert paths.library_paths(VDF) == [r"C:\Program Files (x86)\Steam", r"F:\Games\SteamLibrary"]


def test_the_install_is_found_in_any_steam_library(tmp_path, monkeypatch):
    game = tmp_path / "lib" / "steamapps" / "common" / "hololiveDreams"
    (game / "hololive-Dreams_Data").mkdir(parents=True)
    monkeypatch.delenv("HOLODREAMS_DIR", raising=False)
    monkeypatch.delenv("HB_NO_GAME", raising=False)
    monkeypatch.setattr(paths, "_CANDIDATES", ())
    monkeypatch.setattr(paths, "steam_libraries", lambda: [str(tmp_path / "other"), str(tmp_path / "lib")])
    assert paths.game_root() == str(game)


def test_HOLODREAMS_DIR_still_wins(tmp_path, monkeypatch):
    mine = tmp_path / "mine"
    (mine / "hololive-Dreams_Data").mkdir(parents=True)
    monkeypatch.setenv("HOLODREAMS_DIR", str(mine))
    monkeypatch.delenv("HB_NO_GAME", raising=False)
    monkeypatch.setattr(paths, "steam_libraries", lambda: [])
    assert paths.game_root() == str(mine)


LINUX_VDF = """"libraryfolders"
{
	"0"
	{
		"path"		"%s"
	}
	"1"
	{
		"path"		"%s"
	}
}
"""


def _steam(root: Path, *libraries: Path) -> None:
    """A fake Steam folder whose libraryfolders.vdf lists `root` and `libraries`."""
    (root / "steamapps").mkdir(parents=True, exist_ok=True)
    listed = [root, *libraries] + [root] * (2 - len(libraries))
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        LINUX_VDF % (listed[0].as_posix(), listed[1].as_posix()), "utf8"
    )


def test_linux_steam_homes_are_the_usual_ones(tmp_path):
    roots = paths.steam_roots("linux", tmp_path)
    assert roots[:3] == [
        str(tmp_path / ".steam" / "steam"),
        str(tmp_path / ".steam" / "root"),
        str(tmp_path / ".local" / "share" / "Steam"),
    ]
    assert str(tmp_path / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam") in roots


def test_linux_libraries_come_from_every_steam_home_once(tmp_path):
    native = tmp_path / ".local" / "share" / "Steam"
    extra = tmp_path / "games" / "SteamLibrary"
    _steam(native, extra)
    flatpak = tmp_path / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"
    _steam(flatpak)
    libs = paths.steam_libraries(paths.steam_roots("linux", tmp_path))
    assert libs == [os.path.normpath(native), os.path.normpath(extra), os.path.normpath(flatpak)]


@pytest.mark.skipif(sys.platform == "win32", reason="a symlink needs privileges on Windows")
def test_the_steam_links_are_not_a_second_library(tmp_path):
    native = tmp_path / ".local" / "share" / "Steam"
    _steam(native)
    (tmp_path / ".steam").mkdir()
    (tmp_path / ".steam" / "steam").symlink_to(native)
    libs = paths.steam_libraries(paths.steam_roots("linux", tmp_path))
    assert len(libs) == 1


def test_the_install_is_found_in_a_linux_library(tmp_path, monkeypatch):
    native = tmp_path / ".local" / "share" / "Steam"
    extra = tmp_path / "games"
    _steam(native, extra)
    game = extra / "steamapps" / "common" / "hololiveDreams"
    (game / "hololive-Dreams_Data").mkdir(parents=True)
    monkeypatch.delenv("HOLODREAMS_DIR", raising=False)
    monkeypatch.delenv("HB_NO_GAME", raising=False)
    monkeypatch.setattr(paths, "_CANDIDATES", ())
    monkeypatch.setattr(paths, "steam_roots", lambda *a, **k: [str(native)])
    assert paths.game_root() == os.path.normpath(game)


def test_the_proton_prefix_holds_the_game_data(tmp_path):
    native = tmp_path / ".local" / "share" / "Steam"
    extra = tmp_path / "games"
    _steam(native, extra)
    want = (
        extra
        / "steamapps"
        / "compatdata"
        / "4282500"
        / "pfx"
        / "drive_c"
        / "users"
        / "steamuser"
        / "AppData"
        / "LocalLow"
        / "QualiArts, Inc_"
        / "hololive-Dreams"
    )
    want.mkdir(parents=True)
    got = paths._game_data_dir("linux", {}, tmp_path)
    assert os.path.normpath(got) == os.path.normpath(want)


def test_without_a_prefix_the_default_library_is_named(tmp_path):
    got = paths._game_data_dir("linux", {}, tmp_path)
    assert got == paths.proton_game_data(str(tmp_path / ".local" / "share" / "Steam"))


def test_LOCALAPPDATA_wins_on_every_platform(tmp_path):
    local = tmp_path / "AppData" / "Local"
    want = tmp_path / "AppData" / "LocalLow" / "QualiArts, Inc_" / "hololive-Dreams"
    for platform in ("win32", "linux"):
        assert paths._game_data_dir(platform, {"LOCALAPPDATA": str(local)}, tmp_path) == want


def test_windows_game_data_is_under_the_users_appdata(tmp_path):
    assert paths._game_data_dir("win32", {}, tmp_path) == (
        tmp_path / "AppData" / "LocalLow" / "QualiArts, Inc_" / "hololive-Dreams"
    )


def test_sqlcipher_is_the_games_dll_on_windows_and_the_system_library_elsewhere(tmp_path, monkeypatch):
    monkeypatch.delenv("HOLODREAMS_SQLCIPHER", raising=False)
    monkeypatch.setattr(paths, "sqlcipher_dll", lambda: "game.dll")
    assert paths.sqlcipher_library("win32", find=lambda _n: "unused") == "game.dll"
    assert paths.sqlcipher_library("linux", find=lambda name: f"lib{name}.so.1") == "libsqlcipher.so.1"
    with pytest.raises(FileNotFoundError, match="apt install libsqlcipher1"):
        paths.sqlcipher_library("linux", find=lambda _n: None)
    monkeypatch.setenv("HOLODREAMS_SQLCIPHER", str(tmp_path / "mine.so"))
    assert paths.sqlcipher_library("linux", find=lambda _n: None) == str(tmp_path / "mine.so")


def test_the_dll_is_found_in_the_usual_folders_even_with_HB_NO_GAME(tmp_path, monkeypatch):
    usual = tmp_path / "usual"
    dll = usual / paths.SQLCIPHER_SUBPATH
    dll.parent.mkdir(parents=True)
    dll.write_bytes(b"")
    monkeypatch.delenv("HOLODREAMS_SQLCIPHER", raising=False)
    monkeypatch.delenv("HOLODREAMS_DIR", raising=False)
    monkeypatch.setenv("HB_NO_GAME", "1")
    monkeypatch.setattr(paths, "_CANDIDATES", (str(usual),))
    monkeypatch.setattr(paths, "steam_libraries", lambda: (_ for _ in ()).throw(AssertionError))
    assert paths.sqlcipher_dll() == str(dll)
