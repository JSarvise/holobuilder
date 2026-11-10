"""Every file-system location the project uses, in one place.

* **The checkout.** Every repo path is derived from this file's own location, so a copy of the
  code (the desktop app's `app/` folder, a test sandbox) reads its own data, never another
  checkout's. A script outside `src/` puts its own checkout's `src` first on `sys.path` before
  importing this module; `_check_main_tree` refuses to load when that was skipped and the
  package came from another checkout (an editable install of a different tree).
* **The game on this PC.** The install (`HOLODREAMS_DIR`, a few usual folders, then every Steam
  library Steam lists), its `sqlcipher.dll`, its Octo asset cache, and the game's own data
  folder under LocalLow (the account file and the master cache). On Linux the game runs under
  Proton: Steam is looked for in its usual homes (`~/.steam/steam`, `~/.local/share/Steam`, the
  Flatpak and Snap ones), and LocalLow is inside the game's Proton prefix
  (`steamapps/compatdata/<app id>/pfx/drive_c/users/steamuser/AppData/LocalLow`).

Data lives under `dumps/` in the recon tree (`DUMPS`); the refresh code that writes it lives in
`tools/refresh` (`REFRESH_DIR`). The data is gitignored, so only the paths are here, never the
contents.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

from hbuilder.errors import CheckoutMismatch, MissingInput

# ---------------------------------------------------------------------------- the checkout
REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
DATA = REPO / "data"
STATE = REPO / "state"
CONFIG = REPO / "config.json"
# `data/cards.json`, `data/owned.json` and `state/collection.json` are spelled out where they
# are used: `tools/web/test_no_writes.py` tracks the two protected names by their literal.

TOOLS = REPO / "tools"
WEB_DIR = TOOLS / "web"
APP_DIR = TOOLS / "app"
RECON_DIR = TOOLS / "recon"
PROTOBUFS = RECON_DIR / "protobufs"
# The refresh scripts: what reads the game and the account and writes `DUMPS`.
REFRESH_DIR = TOOLS / "refresh"

# The derived data: what the refresh writes and the solver and the web read.
DUMPS_REL = "tools/recon/proxy/dumps"
DUMPS = REPO / DUMPS_REL
HOLODORI = DUMPS / "holodori"  # the master tables as JSON (main_<Table>.json)

# The checkout's own virtual environment (setup.cmd / setup.sh create it).
VENV = REPO / ".venv"


def venv_python(venv: Path = VENV, platform: str = sys.platform) -> Path:
    """The interpreter of a virtual environment: `Scripts/python.exe` on Windows, `bin/python`
    elsewhere."""
    return venv / ("Scripts/python.exe" if platform == "win32" else "bin/python")


# The master-data dump repo: HB_MASTER_REPO, else the folder next to this checkout.
MASTER_REPO = Path(os.environ.get("HB_MASTER_REPO") or REPO.parent / "hololive-dreams-master")


def _check_main_tree() -> None:
    """Refuse to run a script of one checkout on the package of another.

    The script being run (`__main__`) belongs to the first folder above it that holds
    `src/hbuilder/paths.py`. When that folder is not `REPO`, this module came from another
    checkout's `src` (an editable install), and every path here would point at that
    checkout's data. Interpreters and tools installed under `sys.prefix` (pytest, a venv's
    scripts) are not checked."""
    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    if not main_file:
        return
    script = Path(main_file).resolve()
    if Path(sys.prefix).resolve() in script.parents:
        return
    for folder in script.parents:
        if (folder / "src" / "hbuilder" / "paths.py").is_file():
            if folder != REPO:
                raise CheckoutMismatch(
                    f"hbuilder was imported from {REPO} but the script runs from {folder}: "
                    f"put {folder / 'src'} first on sys.path before importing hbuilder"
                )
            return


_check_main_tree()

# ---------------------------------------------------------------------------- the game on this PC
GAME_DIR_NAME = "hololiveDreams"
STEAM_APP_ID = "4282500"  # hololive Dreams on Steam (its appmanifest_<id>.acf)
_MARKER = "hololive-Dreams_Data"  # present in every install
_WINDOWS_CANDIDATES = (
    r"C:\Program Files (x86)\Steam\steamapps\common\hololiveDreams",
    r"D:\Steam\steamapps\common\hololiveDreams",
    r"D:\SteamLibrary\steamapps\common\hololiveDreams",
    r"E:\SteamLibrary\steamapps\common\hololiveDreams",
)
# On Linux every install is found through Steam's own library list.
_CANDIDATES = _WINDOWS_CANDIDATES if sys.platform == "win32" else ()
_LIBRARY_PATH = re.compile(r'"path"\s+"((?:[^"\\]|\\.)*)"')
SQLCIPHER_SUBPATH = os.path.join(_MARKER, "Plugins", "x86_64", "sqlcipher.dll")
# The game's folder under LocalLow, on Windows and inside the Proton prefix alike.
_LOCALLOW_GAME = ("LocalLow", "QualiArts, Inc_", "hololive-Dreams")
_PROTON_APPDATA = ("pfx", "drive_c", "users", "steamuser", "AppData")


def steam_roots(platform: str = sys.platform, home: Path | None = None) -> list[str]:
    """Where Steam itself may be installed, most likely first. Windows: the registry's
    `SteamPath`, then the default folder. Linux: `~/.steam/steam` and `~/.steam/root` (links Steam
    keeps to its real folder), `~/.local/share/Steam`, the Flatpak and the Snap homes."""
    if platform == "win32":
        root = steam_path()
        return ([root] if root else []) + [os.path.expandvars(r"%ProgramFiles(x86)%\Steam")]
    home = Path.home() if home is None else home
    return [
        str(home / ".steam" / "steam"),
        str(home / ".steam" / "root"),
        str(home / ".local" / "share" / "Steam"),
        str(home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"),
        str(home / "snap" / "steam" / "common" / ".local" / "share" / "Steam"),
    ]


def _same_folder_key(path: str) -> str:
    """Two spellings of one folder (a link, a case difference on Windows) give one key."""
    return os.path.normcase(os.path.realpath(path))


def steam_libraries(roots: list[str] | None = None) -> list[str]:
    """Every Steam library folder on this PC: Steam's own list (`steamapps/libraryfolders.vdf`),
    read from every place Steam may be (`steam_roots`). [] without Steam."""
    out: list[str] = []
    seen: set[str] = set()
    for root in steam_roots() if roots is None else roots:
        try:
            with open(
                os.path.join(root, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace"
            ) as fh:
                found = library_paths(fh.read())
        except OSError:
            continue
        for lib in [root, *found]:
            lib = os.path.normpath(lib)
            if _same_folder_key(lib) not in seen:
                seen.add(_same_folder_key(lib))
                out.append(lib)
    return out


def proton_game_data(library: str) -> Path:
    """The game's LocalLow folder inside its Proton prefix in one Steam library."""
    return Path(library, "steamapps", "compatdata", STEAM_APP_ID, *_PROTON_APPDATA, *_LOCALLOW_GAME)


def _game_data_dir(
    platform: str = sys.platform, environ=None, home: Path | None = None, libraries=None
) -> Path:
    """The game's own data folder: `<LocalLow>/QualiArts, Inc_/hololive-Dreams`.
    `LOCALAPPDATA`, when set, wins on every platform: LocalLow is its sibling (a test points that
    variable elsewhere to isolate it). Windows: under the user's AppData. Linux: inside the game's
    Proton prefix, in the first Steam library that has one (else where the default library would
    hold it, so a message can name it)."""
    environ = os.environ if environ is None else environ
    local = environ.get("LOCALAPPDATA")
    if local:
        return Path(local).parent.joinpath(*_LOCALLOW_GAME)
    home = Path.home() if home is None else home
    if platform == "win32":
        return home.joinpath("AppData", *_LOCALLOW_GAME)
    libs = steam_libraries(steam_roots(platform, home)) if libraries is None else libraries
    for lib in libs:
        if proton_game_data(lib).is_dir():
            return proton_game_data(lib)
    return proton_game_data(libs[0] if libs else str(home / ".local" / "share" / "Steam"))


def no_game() -> bool:
    """`HB_NO_GAME=1`: behave as on a PC without the game (the desktop app sets it, so a
    missing file is fetched or reported instead of read from an install that happens to be
    there; the clean-copy tests set it on a PC where the game IS installed)."""
    return os.environ.get("HB_NO_GAME", "") in ("1", "true", "yes")


def library_paths(vdf_text: str) -> list[str]:
    """The library folders listed in Steam's `steamapps/libraryfolders.vdf` (its "path"
    values, with the file's doubled backslashes undone)."""
    return [m.group(1).replace("\\\\", "\\") for m in _LIBRARY_PATH.finditer(vdf_text)]


def steam_path() -> str | None:
    """Where the registry says Steam is installed (`SteamPath`); None without it."""
    if sys.platform != "win32":
        return None
    try:
        import winreg  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            return str(winreg.QueryValueEx(key, "SteamPath")[0])
    except OSError:
        return None


# The account file (a 32-character name) and the master cache live here.
GAME_DATA = _game_data_dir()
MASTER_CACHE = GAME_DATA / "EB0A191797624DD3A48FA681D3061212"


def _library_installs() -> list[str]:
    return [os.path.join(lib, "steamapps", "common", GAME_DIR_NAME) for lib in steam_libraries()]


def game_root() -> str:
    """The hololiveDreams install directory. `HOLODREAMS_DIR` wins; else the first
    candidate that actually contains the game. `MissingInput` when there is none (or
    `HB_NO_GAME` forbids it), its message naming the env var to set."""
    if no_game():
        raise MissingInput("HB_NO_GAME is set: this process must not use the game install")
    env = os.environ.get("HOLODREAMS_DIR")
    tried = []
    for cand in ([env] if env else []) + list(_CANDIDATES):
        if not cand:
            continue
        tried.append(cand)
        if os.path.isdir(os.path.join(cand, _MARKER)):
            return cand
    for cand in _library_installs():
        if cand in tried:
            continue
        tried.append(cand)
        if os.path.isdir(os.path.join(cand, _MARKER)):
            return cand
    raise MissingInput(
        "hololiveDreams install not found. Set HOLODREAMS_DIR to the game root "
        "(the folder that contains hololive-Dreams_Data). Tried:\n  " + "\n  ".join(tried)
    )


def game_root_or_default() -> str:
    """`game_root()`, or Steam's default install folder when the game is not found (a caller
    that only reports "nothing there" keeps a path to name)."""
    try:
        return game_root()
    except MissingInput:
        if sys.platform == "win32":
            return _WINDOWS_CANDIDATES[0]
        return str(Path.home() / ".local" / "share" / "Steam" / "steamapps" / "common" / GAME_DIR_NAME)


def octo_cache() -> str:
    """`<install>/octo/v1` — the on-disk Octo asset cache (~11 GB of fetched
    assets), what skill_icons.py / card_images.py read from."""
    return os.path.join(game_root(), "octo", "v1")


def octo_pdb() -> str:
    """`<install>/octo/pdb/5` — the per-revision catalogue dir (octocacheevai)."""
    return os.path.join(game_root(), "octo", "pdb", "5")


def aa_dir() -> str:
    """The Addressables bundle dir (the `.bundle` files extract_ui.py reads). Its
    layout differs between builds — StreamingAssets/aa/StandaloneWindows64 here —
    so this returns the first candidate that actually holds `.bundle` files."""
    root = game_root()
    subs = (
        (_MARKER, "StreamingAssets", "aa", "StandaloneWindows64"),
        (_MARKER, "aa", "StandaloneWindows64"),
        (_MARKER, "StreamingAssets", "aa"),
        (_MARKER, "aa"),
    )
    for sub in subs:
        p = os.path.join(root, *sub)
        if os.path.isdir(p) and any(f.endswith(".bundle") for f in os.listdir(p)):
            return p
    # Fall back to the canonical location so the caller's own error names it.
    return os.path.join(root, _MARKER, "StreamingAssets", "aa")


def sqlcipher_dll() -> str:
    """The game's own `sqlcipher.dll`: `HOLODREAMS_SQLCIPHER` when set, else the dll inside
    `HOLODREAMS_DIR`, the usual Steam folders, then (unless `HB_NO_GAME`) every Steam library.
    `HB_NO_GAME` leaves the first three in: the dll is a library the master reader needs, not
    the install's data."""
    direct = os.environ.get("HOLODREAMS_SQLCIPHER")
    if direct:
        return direct
    env = os.environ.get("HOLODREAMS_DIR")
    tried: list[str] = []

    def first(roots) -> str | None:
        for root in roots:
            cand = os.path.join(root, SQLCIPHER_SUBPATH)
            if cand in tried:
                continue
            tried.append(cand)
            if os.path.exists(cand):
                return cand
        return None

    found = first(([env] if env else []) + list(_CANDIDATES))
    if found is None and not no_game():
        found = first(_library_installs())  # Steam's own list, only when needed
    if found is not None:
        return found
    raise FileNotFoundError(
        "sqlcipher.dll not found. Set HOLODREAMS_DIR to the game root "
        "(the folder ...\\common\\hololiveDreams) or HOLODREAMS_SQLCIPHER to the dll. Tried:\n  "
        + "\n  ".join(tried)
    )


SQLCIPHER_INSTALL_HINT = (
    "install SQLCipher (Debian/Ubuntu: sudo apt install libsqlcipher1; "
    "Fedora and Arch: the sqlcipher package)"
)


def sqlcipher_library(platform: str = sys.platform, find: Callable[[str], str | None] | None = None) -> str:
    """The SQLCipher library the master reader loads (`master_db`). `HOLODREAMS_SQLCIPHER` when
    set; on Windows the game's own `sqlcipher.dll` (`sqlcipher_dll`); elsewhere the system's
    `libsqlcipher`, since the game's dll is a Windows library. Both open the game's databases
    with SQLCipher 4's default settings and the raw key `master_db` gives them.
    FileNotFoundError when there is none."""
    direct = os.environ.get("HOLODREAMS_SQLCIPHER")
    if direct:
        return direct
    if platform == "win32":
        return sqlcipher_dll()
    if find is None:
        import ctypes.util  # noqa: PLC0415

        find = ctypes.util.find_library
    found = find("sqlcipher")
    if found:
        return found
    raise FileNotFoundError(
        f"libsqlcipher not found: {SQLCIPHER_INSTALL_HINT}, or set HOLODREAMS_SQLCIPHER to the library file"
    )
