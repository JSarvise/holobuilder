"""game_paths: the game install is looked for in every Steam library, as Steam lists them."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "recon" / "proxy"))

import game_paths  # noqa: E402

VDF = r'''"libraryfolders"
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
'''


def test_library_paths_reads_steams_list():
    assert game_paths.library_paths(VDF) == [r"C:\Program Files (x86)\Steam", r"F:\Games\SteamLibrary"]


def test_the_install_is_found_in_any_steam_library(tmp_path, monkeypatch):
    game = tmp_path / "lib" / "steamapps" / "common" / "hololiveDreams"
    (game / "hololive-Dreams_Data").mkdir(parents=True)
    monkeypatch.delenv("HOLODREAMS_DIR", raising=False)
    monkeypatch.delenv("HB_NO_GAME", raising=False)
    monkeypatch.setattr(game_paths, "_CANDIDATES", ())
    monkeypatch.setattr(game_paths, "steam_libraries", lambda: [str(tmp_path / "other"), str(tmp_path / "lib")])
    assert game_paths.game_root() == str(game)


def test_HOLODREAMS_DIR_still_wins(tmp_path, monkeypatch):
    mine = tmp_path / "mine"
    (mine / "hololive-Dreams_Data").mkdir(parents=True)
    monkeypatch.setenv("HOLODREAMS_DIR", str(mine))
    monkeypatch.delenv("HB_NO_GAME", raising=False)
    monkeypatch.setattr(game_paths, "steam_libraries", lambda: [])
    assert game_paths.game_root() == str(mine)
