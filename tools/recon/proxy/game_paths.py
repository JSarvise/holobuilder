"""Portable resolution of the game install (the Steam library path varies by PC).

The game is not always under `C:\\Program Files (x86)\\Steam\\...`; on another
machine it may live in another Steam library (e.g. `D:\\SteamLibrary\\...`). This finds the install
once — via the `HOLODREAMS_DIR` env var, a few usual folders, then every Steam
library this PC's Steam knows — so no extractor needs a path edit, the same
approach `master_db.py` uses for `sqlcipher.dll`.
"""

from __future__ import annotations

import os
import re

# The folder that marks a hololiveDreams root (present in every install).
_MARKER = "hololive-Dreams_Data"

_CANDIDATES = (
    r"C:\Program Files (x86)\Steam\steamapps\common\hololiveDreams",
    r"D:\Steam\steamapps\common\hololiveDreams",
    r"D:\SteamLibrary\steamapps\common\hololiveDreams",
    r"E:\SteamLibrary\steamapps\common\hololiveDreams",
)
_LIBRARY_PATH = re.compile(r'"path"\s+"((?:[^"\\]|\\.)*)"')


def library_paths(vdf_text: str) -> list[str]:
    """The library folders listed in Steam's `steamapps/libraryfolders.vdf` (its "path"
    values, with the file's doubled backslashes undone)."""
    return [m.group(1).replace("\\\\", "\\") for m in _LIBRARY_PATH.finditer(vdf_text)]


def steam_libraries() -> list[str]:
    """Every Steam library folder on this PC: Steam's own list, read from where the registry
    says Steam is (or its default folder). [] without Steam."""
    roots = []
    try:
        import winreg  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            roots.append(str(winreg.QueryValueEx(key, "SteamPath")[0]))
    except (ImportError, OSError):
        pass
    roots.append(os.path.expandvars(r"%ProgramFiles(x86)%\Steam"))
    out: list[str] = []
    for root in roots:
        try:
            with open(os.path.join(root, "steamapps", "libraryfolders.vdf"),
                      encoding="utf-8", errors="replace") as fh:
                found = library_paths(fh.read())
        except OSError:
            continue
        for lib in [root, *found]:
            lib = os.path.normpath(lib)
            if os.path.normcase(lib) not in {os.path.normcase(p) for p in out}:
                out.append(lib)
    return out


def no_game() -> bool:
    """`HB_NO_GAME=1`: behave as on a PC without the game (the desktop app sets it, so a
    missing file is fetched or reported instead of read from an install that happens to be
    there; the clean-copy tests set it on a PC where the game IS installed)."""
    return os.environ.get("HB_NO_GAME", "") in ("1", "true", "yes")


def game_root() -> str:
    """The hololiveDreams install directory. `HOLODREAMS_DIR` wins; else the first
    candidate that actually contains the game. `SystemExit` (not a bare error) so
    a missing install reads as "point me at it", with the env var to set."""
    if no_game():
        raise SystemExit("HB_NO_GAME is set: this process must not use the game install")
    env = os.environ.get("HOLODREAMS_DIR")
    tried = []
    for cand in ([env] if env else []) + list(_CANDIDATES):
        if not cand:
            continue
        tried.append(cand)
        if os.path.isdir(os.path.join(cand, _MARKER)):
            return cand
    for lib in steam_libraries():
        cand = os.path.join(lib, "steamapps", "common", "hololiveDreams")
        if cand in tried:
            continue
        tried.append(cand)
        if os.path.isdir(os.path.join(cand, _MARKER)):
            return cand
    raise SystemExit(
        "hololiveDreams install not found. Set HOLODREAMS_DIR to the game root "
        "(the folder that contains hololive-Dreams_Data). Tried:\n  "
        + "\n  ".join(tried)
    )


def octo_cache() -> str:
    """`<install>/octo/v1` — the on-disk Octo asset cache (~11 GB of fetched
    assets), what skill_icons.py / card_images.py read from."""
    return os.path.join(game_root(), "octo", "v1")


def octo_pdb() -> str:
    """`<install>/octo/pdb/5` — the per-revision catalogue dir (octocacheevai)."""
    return os.path.join(game_root(), "octo", "pdb", "5")


def aa_dir() -> str:
    """The Addressables bundle dir (the `.bundle` files extract_ui.py reads). Its
    layout moved between builds — StreamingAssets/aa/StandaloneWindows64 here —
    so this returns the first candidate that actually holds `.bundle` files."""
    root = game_root()
    subs = (
        ("hololive-Dreams_Data", "StreamingAssets", "aa", "StandaloneWindows64"),
        ("hololive-Dreams_Data", "aa", "StandaloneWindows64"),
        ("hololive-Dreams_Data", "StreamingAssets", "aa"),
        ("hololive-Dreams_Data", "aa"),
    )
    for sub in subs:
        p = os.path.join(root, *sub)
        if os.path.isdir(p) and any(f.endswith(".bundle") for f in os.listdir(p)):
            return p
    # Fall back to the canonical location so the caller's own error names it.
    return os.path.join(root, "hololive-Dreams_Data", "StreamingAssets", "aa")
