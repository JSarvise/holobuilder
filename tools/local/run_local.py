"""Run holobuilder from a clone of this repository, entirely on this PC: no holobuilder server,
no sign-in, no approval. The scripts at the top of the repository call it, `.cmd` on Windows and
`.sh` on Linux, and hold no logic of their own beyond finding Python:

    setup     run_local.py setup: creates .venv, installs the packages, then run_local.py check
    refresh   run_local.py refresh [--region us|jp|as] [any refresh_all.py option]
    start     run_local.py start [--port N] [--no-browser] [--region us|jp|as]
    update    run_local.py update [--no-refresh]: the newest holobuilder from GitHub, then refresh

`refresh` does what the desktop app does before it reads an account, with this PC's own game
install in place of the holobuilder server:

  1. the client profile: the headers the game's client sends, from this PC's hardware
     (tools/app/client_profile.py; a profile captured from the real client is never replaced);
  2. the game client's build constants: found in the game install when this PC has none, or
     when they no longer open the server's answers (tools/refresh/game_keys.py --find);
  3. the account file: HB_CREDENTIAL_FILE, else the one file in the game's LocalLow folder
     shaped like one. It is read in place at each refresh and never copied;
  4. the account's region: --region, else HB_ACCOUNT_HOST, else the one whose server accepted
     the login the first time, kept in state/local.json;
  5. tools/refresh/refresh_all.py --merge-collection --pictures-later: the scored data first,
     the pictures in the background afterwards (one picture run at a time); then the pull
     history and its banners (gacha_sync.py), as the pages' Refresh does.

`start` stops an earlier holobuilder server of this folder that still holds the port (the server
says which folder it serves at /api/server; anything else keeps the port, and the next free one
is used), starts tools/web/server.py on
127.0.0.1 with the same account settings, so the pages' Refresh button works too, and opens
the browser once it answers.

`update` is `git pull` made safe for this folder, then `setup` when the packages changed, then
`refresh`. A refresh adds the game's new cards to data/cards.json, a file the repository tracks,
and git refuses to pull over a changed file. When those added cards are the only change, they are
put aside (a copy goes to dumps/cards_before_update.json), the pull runs, and the refresh adds
again whatever the new catalogue lacks. Any other change to a tracked file stops the update and
is named: it is someone's own work, and an update never touches it.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first
from hbuilder import paths  # noqa: E402
from hbuilder.errors import MissingInput, run_cli  # noqa: E402

REPO = paths.REPO
REFRESH = paths.REFRESH_DIR
KERNEL = paths.WEB_DIR / "kernel"
SERVER = paths.WEB_DIR / "server.py"
STATE = paths.STATE
LOCAL = STATE / "local.json"  # this folder's own choices: the account's region
DUMPS = paths.DUMPS
DEFAULT_PORT = 8731
REGIONS = ("us", "as", "jp")  # the order a region is looked for: worldwide ones first
PACKAGES = (
    ("Crypto", "pycryptodome"),
    ("UnityPy", "UnityPy"),
    ("h2", "h2"),
    ("highspy", "highspy"),
    ("PIL", "pillow"),
    ("requests", "requests"),
)
KERNELS = ("notewalk", "connect", "surrogate", "cdvar")
WINLIBS = "winget install BrechtSanders.WinLibs.POSIX.UCRT"
WINDOWS = sys.platform == "win32"
# The oldest Python holobuilder runs on (pyproject.toml's requires-python): the compiled board
# assembly replays the built-in sum() as 3.12 compensates it. 3.13 is the tested one.
MIN_PYTHON = (3, 12)
MARKS = {True: "ok", False: "X ", None: "--"}  # ready / blocks the local route / not yet

for _path in (paths.APP_DIR, REFRESH, KERNEL):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
import app_settings  # noqa: E402

REGION_HOSTS = app_settings.REGION_HOSTS


def launcher(name: str) -> str:
    """How this PC runs one of the top-level scripts: `refresh.cmd` on Windows, `./refresh.sh`
    elsewhere."""
    return f"{name}.cmd" if WINDOWS else f"./{name}.sh"


# ---------------------------------------------------------------- this folder's choices
def saved(path: Path = LOCAL) -> dict:
    try:
        doc = json.loads(path.read_text("utf8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save(path: Path = LOCAL, **values) -> None:
    doc = saved(path)
    doc.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1) + "\n", "utf8")


# ---------------------------------------------------------------- the account
def find_credential(env=None, folder: Path = app_settings.LOCALLOW) -> tuple[Path | None, str]:
    """The game's account file -> (path, where it was found), or (None, what to do)."""
    env = os.environ if env is None else env
    named = (env.get("HB_CREDENTIAL_FILE") or "").strip()
    if named:
        path = Path(named)
        return (
            (path, "HB_CREDENTIAL_FILE")
            if path.is_file()
            else (None, f"HB_CREDENTIAL_FILE names no file: {path}")
        )
    hits = app_settings.credential_files(folder)
    if len(hits) == 1:
        return hits[0], "the game's own folder"
    if hits:
        return None, (
            f"{len(hits)} files in {shown(folder)} look like an account file: set "
            "HB_CREDENTIAL_FILE to the one of the account you play"
        )
    return None, (
        f"no account file in {shown(folder)}: start the game on this PC once and get past "
        "its title screen, or set HB_CREDENTIAL_FILE"
    )


def region_host(region: str | None = None, env=None, remembered: dict | None = None) -> str | None:
    """The API host of the account's region: `region`, else HB_ACCOUNT_HOST, else the region kept
    in state/local.json. None when none is known yet."""
    env = os.environ if env is None else env
    if region:
        return REGION_HOSTS[region]
    if (env.get("HB_ACCOUNT_HOST") or "").strip():
        return env["HB_ACCOUNT_HOST"].strip()
    return REGION_HOSTS.get((saved() if remembered is None else remembered).get("region"))


def detect_region(try_login, regions=REGIONS) -> str | None:
    """The first region whose server accepts the login. `try_login(host)` raises RuntimeError
    naming the grpc status when it is refused; status 16 (UNAUTHENTICATED) is what a server
    answers for an account that lives in another region."""
    for region in regions:
        try:
            try_login(REGION_HOSTS[region])
        except RuntimeError as exc:
            if "grpc-status 16" in str(exc):
                print(f"  {region}: not this account's region")
                continue
            raise
        return region
    return None


def _login(host: str) -> None:
    """One login on `host` with this PC's account file (HB_CREDENTIAL_FILE)."""
    import account_snapshot as A  # noqa: PLC0415

    _host, credential = A.load_credential("main")
    ident = A.client_headers("main")
    A.login(host, credential, ident, A.fetch_master_version(host, ident))


def ensure_profile() -> bool:
    """The client profile in place (kept, or written from this PC); False, having said what to
    do, when none can be written (Linux without the game's Proton prefix)."""
    import client_profile  # noqa: PLC0415

    try:
        got = client_profile.ensure()
    except client_profile.ProfileUnavailable as exc:
        print(f"  [X] client profile: {exc}")
        return False
    what = (
        "put here by hand or captured from the game, kept as it is"
        if got["captured"]
        else "written from this PC's hardware"
        if got["written"]
        else "current"
    )
    print(
        f"  client profile: {what}"
        + ("" if got["steam_id"] else " (no Steam ID found: sign in to Steam for the next refresh)")
    )
    return True


def ensure_keys(host: str) -> bool:
    """This PC has the game client's build constants and they still open the server's answers;
    found in the game install when not."""
    import game_keys  # noqa: PLC0415

    if game_keys.grpc_seed(required=False) is not None:
        if game_keys.check(host) is not False:  # current, or the server could not say
            print(f"  game constants: on this PC ({game_keys.source()})")
            return True
        print(
            "  game constants: the ones on this PC no longer open the server's answers "
            "(a game update?); finding them again"
        )
    else:
        print("  game constants: not on this PC yet; finding them in your game install")
    return game_keys.find(host) == 0


def child_env(credential: Path | None, host: str | None, base=None) -> dict:
    """The environment refresh_all.py and server.py run with."""
    env = dict(os.environ if base is None else base)
    env["PYTHONIOENCODING"] = "utf-8"
    if credential is not None:
        env["HB_CREDENTIAL_FILE"] = str(credential)
    if host:
        env["HB_ACCOUNT_HOST"] = host
    if not env.get("HOLODREAMS_DIR"):
        try:
            env["HOLODREAMS_DIR"] = paths.game_root()
        except MissingInput:
            pass
    return env


def first_refresh() -> bool:
    return not (DUMPS / "charts.json").is_file()


def refresh(args: argparse.Namespace, extra: list[str]) -> int:
    print("holobuilder -- reading the game and your account into this folder")
    try:
        print(f"  game install: {shown(paths.game_root())}")
    except MissingInput:
        print(
            "  [X] hololive Dreams is not installed where Steam keeps games; install it, or set "
            "HOLODREAMS_DIR to the folder that holds hololive-Dreams_Data"
        )
        return 1
    credential, how = find_credential()
    if credential is None:
        print(f"  [X] account file: {how}")
        return 1
    os.environ["HB_CREDENTIAL_FILE"] = str(credential)
    print(f"  account file: found in {how} (read in place, never copied)")
    if args.region:
        save(region=args.region)
    if not ensure_profile():
        return 1
    if not ensure_keys(region_host(args.region) or REGION_HOSTS["us"]):
        print("  [X] the game's constants could not be found; docs/dev/game-keys.md explains them")
        return 1
    host = region_host(args.region)
    if host is None:
        print("  region: finding the server your account lives on (one login per region)")
        try:
            region = detect_region(_login)
        except RuntimeError as exc:
            print(f"  [X] the login failed: {exc}")
            return 1
        if region is None:
            print("  [X] no region accepted this account; pass --region us, as or jp")
            return 1
        save(region=region)
        host = REGION_HOSTS[region]
    print(f"  region: {host}")
    cmd = [sys.executable, "-u", str(REFRESH / "refresh_all.py"), "--merge-collection", *extra]
    if "--pictures-later" not in extra:
        cmd.append("--pictures-later")
    print("  the pictures are fetched in the background afterwards (--pictures-later)\n")
    env = child_env(credential, host)
    try:
        code, seen = _stream(cmd, env)
        if code not in (3, 4):  # maintenance, no constants: the gacha read is refused too
            # the pull history and its banners, as the pages' Refresh reads them after the refresh
            print("\n--- your pull history (gacha_sync.py) ---")
            gacha, more = _stream([sys.executable, "-u", str(REFRESH / "gacha_sync.py")], env)
            seen = f"{seen} {more}".strip()
            if gacha:
                print(f"  (the pull history was not read: exit {gacha})")
    except KeyboardInterrupt:
        print("\nrefresh stopped")
        return 130
    print()
    if code == 0:
        print(f"Done. Start holobuilder with {launcher('start')} (or press Refresh in its pages next time).")
    elif "DATE_CHANGED" in seen:
        from hbuilder import game_day  # noqa: PLC0415

        print(
            "The game's day has changed since this account last opened the game "
            f"({game_day.reset_hour():02d}:00 JST):"
        )
        print(f"open the game once, then run {launcher('refresh')} again. Everything else above is current.")
    else:
        print(f"The refresh ended with exit code {code}: see the lines above, and docs/troubleshooting.md.")
    return code


def _stream(cmd: list[str], env: dict) -> tuple[int, str]:
    """Run `cmd`, echoing its output as it comes -> (exit code, the words worth reacting to)."""
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    seen = set()
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if "DATE_CHANGED" in line:
            seen.add("DATE_CHANGED")
    return proc.wait(), " ".join(sorted(seen))


# ---------------------------------------------------------------- update
CATALOGUE = "data/cards.json"  # the one tracked file a refresh writes (card_append.py adds to it)
CATALOGUE_BACKUP = "cards_before_update.json"  # in dumps/: the catalogue as it was before an update
PACKAGES_FILES = ("requirements.txt", "pyproject.toml")  # a change to these needs setup again


def only_added_cards(before: object, now: object) -> bool:
    """True when the catalogue `now` is `before` plus whole cards: the only change a refresh makes
    to it (card_append.py adds cards and never edits or removes one)."""
    if not (isinstance(before, dict) and isinstance(now, dict)):
        return False
    if {k: v for k, v in before.items() if k != "cards"} != {k: v for k, v in now.items() if k != "cards"}:
        return False
    mine = {c.get("id"): c for c in now.get("cards") or [] if isinstance(c, dict)}
    return all(isinstance(c, dict) and mine.get(c.get("id")) == c for c in before.get("cards") or [])


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
    )


def changed_files(repo: Path) -> list[str]:
    """The tracked files this folder has changed or staged (untracked files are left out)."""
    items = _git(repo, "status", "--porcelain", "--untracked-files=no", "-z").stdout.split("\0")
    out, skip = [], False
    for item in items:
        if skip:  # a rename's or copy's former name
            skip = False
            continue
        if len(item) > 3:
            out.append(item[3:])
            skip = "R" in item[:2] or "C" in item[:2]
    return out


def catalogue_only_grew(repo: Path) -> bool:
    """True when data/cards.json differs from the commit only by cards a refresh added."""
    head = _git(repo, "show", f"HEAD:{CATALOGUE}")
    if head.returncode:
        return False
    try:
        now = json.loads((repo / CATALOGUE).read_text(encoding="utf-8"))
        return only_added_cards(json.loads(head.stdout), now)
    except (OSError, ValueError):
        return False


def update(args: argparse.Namespace, repo: Path = REPO, run: Callable[[list[str]], int] | None = None) -> int:
    """Bring this clone to the newest holobuilder on its GitHub branch, then refresh (see the
    module docstring). `run` runs one step (setup, refresh) and returns its exit code."""
    if run is None:
        run = lambda cmd: _stream(cmd, dict(os.environ))[0]  # noqa: E731
    print("\n  holobuilder update\n  ==================\n")
    if shutil.which("git") is None or not (repo / ".git").exists():
        print("  [X] This folder was not cloned with git, so it cannot update itself.")
        print(
            "      Download the newest ZIP (the green Code button on GitHub), unpack it over this "
            f"folder, then run {launcher('setup')} and {launcher('refresh')}."
        )
        return 1
    if _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").returncode:
        print("  [X] This folder's branch follows no branch on GitHub: there is nothing to update from.")
        return 1
    blockers = [p for p in changed_files(repo) if p != CATALOGUE or not catalogue_only_grew(repo)]
    if blockers:
        print("  [X] These files have changes of your own, and an update never touches them:")
        for path in blockers:
            print(f"        {path}")
        print(
            "      Keep them aside with `git stash` (and `git stash pop` after the update), or undo "
            f"them with `git checkout -- <file>`; then run {launcher('update')} again."
        )
        return 1
    print("  asking GitHub for a newer holobuilder ...")
    fetch = _git(repo, "fetch", "--quiet")
    if fetch.returncode:
        print("  [X] git could not reach GitHub:")
        print("      " + (fetch.stderr.strip() or f"exit {fetch.returncode}").replace("\n", "\n      "))
        return 1
    old = _git(repo, "rev-parse", "HEAD").stdout.strip()
    new = _git(repo, "rev-parse", "@{u}").stdout.strip()
    put_aside = False
    if _git(repo, "merge-base", "--is-ancestor", new, old).returncode == 0:
        print("  this folder already has the newest holobuilder")
    else:
        backup = repo / paths.DUMPS_REL / CATALOGUE_BACKUP
        if (
            CATALOGUE in changed_files(repo)
            and _git(repo, "diff", "--quiet", old, new, "--", CATALOGUE).returncode
        ):
            # the new version changes the catalogue too: git would refuse to pull over the cards
            # a refresh added, so they are put aside and the refresh below adds them again
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo / CATALOGUE, backup)
            _git(repo, "checkout", "--", CATALOGUE)
            put_aside = True
            print(
                f"  the cards your refreshes added to {CATALOGUE} are put aside for the update "
                f"(a copy is in {shown(backup)})"
            )
        merge = _git(repo, "merge", "--ff-only", "--quiet", new)
        if merge.returncode:
            if put_aside:
                shutil.copyfile(backup, repo / CATALOGUE)
            print("  [X] git could not update this folder (it has commits of its own?):")
            print("      " + (merge.stderr.strip() or f"exit {merge.returncode}").replace("\n", "\n      "))
            return 1
        titles = _git(repo, "log", "--format=%s", f"{old}..{new}").stdout.splitlines()
        print(f"  updated: {len(titles)} new change(s); CHANGELOG.md says what each version brings")
        for title in titles[:3]:
            print(f"    - {title if len(title) <= 100 else title[:97] + '...'}")
        print(f"  if holobuilder is open, close it and run {launcher('start')} again to use the new version")
        changed = set(_git(repo, "diff", "--name-only", old, new).stdout.split())
        if changed & set(PACKAGES_FILES):
            print("\n  the packages changed: running setup\n")
            code = run([sys.executable, "-u", str(repo / "tools" / "local" / "run_local.py"), "setup"])
            if code:
                return code
    if args.no_refresh:
        if put_aside:
            print(f"\n  Run {launcher('refresh')} next: it adds back the cards the new catalogue lacks.")
        return 0
    print("\n  now the refresh: it signs in to your account, so close the game first\n")
    return run([sys.executable, "-u", str(repo / "tools" / "local" / "run_local.py"), "refresh"])


# ---------------------------------------------------------------- the web app
def port_free(port: int) -> bool:
    """Whether server.py could bind 127.0.0.1:port now (with the same SO_REUSEADDR choice)."""
    with socket.socket() as s:
        if not WINDOWS:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def server_on(port: int) -> dict | None:
    """What the holobuilder server on `port` says about itself (/api/server: {"repo", "pid"});
    None when the port holds anything else or does not answer."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/server", timeout=3) as resp:
            doc = json.loads(resp.read())
    except (OSError, ValueError):
        return None
    if isinstance(doc, dict) and isinstance(doc.get("repo"), str) and isinstance(doc.get("pid"), int):
        return doc
    return None


def ours(server: dict | None, repo: Path = REPO) -> bool:
    """Whether a server's /api/server answer names this folder."""
    if not server:
        return False
    try:
        return os.path.normcase(os.path.realpath(server["repo"])) == os.path.normcase(os.path.realpath(repo))
    except (OSError, TypeError, ValueError):
        return False


def claim_port(port: int, *, tries: int = 20) -> int | None:
    """`port` once free: an earlier holobuilder server of this folder on it is stopped (named
    first); anything else keeps it, and the next free port is used instead."""
    if port_free(port):
        return port
    server = server_on(port)
    if ours(server):
        print(f"  port {port}: stopping an earlier holobuilder server of this folder (PID {server['pid']})")
        try:
            os.kill(server["pid"], signal.SIGTERM)  # TerminateProcess on Windows
        except OSError:
            pass
    else:
        print(f"  port {port} is in use by another program; leaving it alone")
    for _ in range(25):
        if port_free(port):
            return port
        time.sleep(0.2)
    for other in range(port + 1, port + 1 + tries):
        if port_free(other):
            print(f"  using port {other} instead")
            return other
    print(f"  [X] ports {port}-{port + tries} are all in use: start with --port N")
    return None


def wait_until_up(url: str, proc: subprocess.Popen, timeout: float = 120) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with opener.open(url, timeout=3):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def start(args: argparse.Namespace) -> int:
    print("holobuilder -- starting the local web app")
    if args.region:
        save(region=args.region)
    credential, how = find_credential()
    if credential is None:
        print(f"  [!] account file: {how}; the pages' Refresh button needs it")
    if first_refresh():
        print(
            f"  [!] no game data in this folder yet: run {launcher('refresh')} first, or the pages stay empty"
        )
    port = claim_port(args.port)
    if port is None:
        return 1
    url = f"http://127.0.0.1:{port}/optimizer"
    env = child_env(credential, region_host(args.region))
    if args.park_scan:
        env["HB_PARK_SCAN"] = "1"  # the Park page's "Scan all areas" (park_scan.py)
    proc = subprocess.Popen([sys.executable, "-u", str(SERVER), "--port", str(port)], cwd=str(REPO), env=env)
    try:
        if wait_until_up(url, proc):
            print(f"\n  holobuilder is at {url}   (close this window or press Ctrl+C to stop it)\n")
            if not args.no_browser:
                webbrowser.open(url)
        return proc.wait()
    except KeyboardInterrupt:
        try:
            return proc.wait(15)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.kill()
            return proc.wait()


# ---------------------------------------------------------------- setup check
def build_kernels() -> str | None:
    """Compile the search's C kernels now (they are built on first use otherwise); the error
    text when one cannot be built."""
    import importlib  # noqa: PLC0415

    for name in KERNELS:
        try:
            importlib.import_module(name).build()
        except Exception as exc:  # noqa: BLE001 -- reported, with the compiler's own words
            return f"{name}: {exc}"
    return None


def shown(path, home: str | None = None, windows: bool = WINDOWS) -> str:
    """A path as printed: the user's own folder as %USERPROFILE% (Windows) or ~, so pasted
    output does not carry the user name."""
    text = str(path)
    if home is None:
        home = os.environ.get("USERPROFILE", "") if windows else str(Path.home())
    if not home:
        return text
    if text.lower().startswith(home.lower()) if windows else text.startswith(home):
        return ("%USERPROFILE%" if windows else "~") + text[len(home) :]
    return text


def check(_args: argparse.Namespace | None = None) -> int:
    print("holobuilder -- what this PC has for running it locally\n")
    blocking = 0

    def row(ok: bool | None, what: str, detail: str, fix: str = "") -> None:
        nonlocal blocking
        blocking += ok is False
        print(f"  [{MARKS[ok]}] {what:<15} {detail}")
        if fix:
            print(f"  {'':<20}{fix}")

    py = sys.version_info[:2]
    row(
        py >= MIN_PYTHON,
        "Python",
        f"{platform.python_version()} ({shown(sys.executable)})"
        + ("" if py == (3, 13) else "; the project is tested on 3.13")
        + ("" if py >= MIN_PYTHON else "; too old: 3.12 or newer is needed"),
        "" if py >= MIN_PYTHON else f"install Python 3.13 (python.org) and run {launcher('setup')} again",
    )
    missing = [pip for mod, pip in PACKAGES if importlib.util.find_spec(mod) is None]
    row(
        not missing,
        "packages",
        "installed" if not missing else "missing: " + ", ".join(missing),
        "" if not missing else f"run {launcher('setup')} again (it installs them)",
    )
    gcc = shutil.which("gcc")
    if gcc is None:
        row(
            False,
            "C compiler",
            "gcc is not on the PATH: the team search needs it",
            f"install WinLibs ({WINLIBS} or winlibs.com), open a new window, run setup.cmd again"
            if WINDOWS
            else "install gcc (Debian/Ubuntu: sudo apt install gcc), run ./setup.sh again",
        )
    else:
        err = build_kernels()
        row(
            err is None,
            "C compiler",
            f"gcc ({shown(gcc)}): "
            + ("search kernels built" if err is None else f"a kernel did not build: {err}"),
        )
    try:
        row(True, "game install", shown(paths.game_root()))
    except MissingInput:
        row(
            False,
            "game install",
            "hololive Dreams not found in this PC's Steam libraries",
            "install it from Steam, or set HOLODREAMS_DIR to the folder that holds hololive-Dreams_Data",
        )
    credential, how = find_credential()
    row(
        True if credential else False,
        "account file",
        f"found in {how} (read in place, never copied)" if credential else how,
    )
    import game_keys  # noqa: PLC0415

    src = game_keys.source()
    row(
        True if src else None,
        "game constants",
        f"on this PC ({src})" if src else f"not yet: {launcher('refresh')} finds them in your game install",
    )
    host = region_host()
    row(True if host else None, "region", host or f"not yet: the first {launcher('refresh')} finds it")
    built = not first_refresh() and (STATE / "collection.json").is_file()
    row(True if built else None, "game data", "built" if built else f"not yet: run {launcher('refresh')}")
    print()
    setup_, refresh_, start_ = launcher("setup"), launcher("refresh"), launcher("start")
    if blocking:
        print(f"Fix the [X] lines above, then run {setup_} again.")
    elif built:
        print(f"Ready: {start_} opens holobuilder; {refresh_} reads your account again.")
    else:
        print(
            f"Ready for the first refresh: run {refresh_} (it takes a while the first time), then {start_}."
        )
    return 1 if blocking else 0


def setup(_args: argparse.Namespace | None = None) -> int:
    """Create .venv with the interpreter running this, install the packages into it, then run
    `check` with the .venv's interpreter. Safe to run again: an existing .venv is updated."""
    print("\n  holobuilder setup\n  =================\n")
    if sys.version_info < MIN_PYTHON:
        print(
            f"  [X] Python {platform.python_version()} is too old: 3.12 or newer is needed "
            "(3.13 is the version the project is tested on)."
        )
        return 1
    print(f"  Python {platform.python_version()}  ({shown(sys.executable)})")
    venv_py = paths.venv_python(paths.VENV)
    old = venv_version(venv_py) if venv_py.is_file() else MIN_PYTHON
    if old is None or old < MIN_PYTHON:
        print(
            "  .venv "
            + (f"runs Python {old[0]}.{old[1]}, too old" if old else "does not start")
            + ": making it again with this Python ..."
        )
        subprocess.run([sys.executable, "-m", "venv", "--clear", str(paths.VENV)], cwd=str(REPO))
    elif venv_py.is_file():
        print("  .venv is already here: bringing its packages up to date")
    else:
        print("  creating .venv ...")
        subprocess.run([sys.executable, "-m", "venv", str(paths.VENV)], cwd=str(REPO))
    if (
        not venv_py.is_file()
        or subprocess.run(
            [str(venv_py), "-m", "pip", "--version"], cwd=str(REPO), capture_output=True
        ).returncode
    ):
        print("  [X] .venv could not be created (see the message above).")
        if not WINDOWS:
            print(
                "      On Debian or Ubuntu: sudo apt install python3-venv, delete the .venv folder "
                "and run ./setup.sh again."
            )
        return 1
    print("\n  installing the packages (a few minutes the first time) ...\n")
    pip = subprocess.run(
        [
            str(venv_py),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-e",
            ".",
            "-r",
            "requirements.txt",
        ],
        cwd=str(REPO),
    )
    if pip.returncode:
        print("\n  [X] The packages did not install (see pip's message above).")
        return 1
    print()
    return subprocess.run([str(venv_py), str(Path(__file__).resolve()), "check"], cwd=str(REPO)).returncode


def venv_version(venv_py: Path) -> tuple[int, int] | None:
    """The (major, minor) of the Python a .venv runs, or None when it does not answer."""
    try:
        out = subprocess.run(
            [str(venv_py), "-c", "import sys; print(*sys.version_info[:2])"], capture_output=True, text=True
        )
        major, minor = (int(x) for x in out.stdout.split())
    except (OSError, ValueError):
        return None
    return major, minor


def too_old() -> bool:
    """Say so and return True when this interpreter (the folder's .venv, for start and refresh)
    is older than MIN_PYTHON."""
    if sys.version_info >= MIN_PYTHON:
        return False
    print(
        f"  [X] This folder's .venv runs Python {platform.python_version()}: holobuilder needs "
        f"3.12 or newer. Install Python 3.13 (python.org) and run {launcher('setup')} again: "
        "it makes .venv again with it."
    )
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run_local.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser(
        "setup", help="create .venv, install the packages, then check (what setup.cmd and setup.sh run)"
    )
    sub.add_parser("check", help="what this PC has, and what is missing (setup runs it)")
    r = sub.add_parser(
        "refresh",
        help="read the game and your account (any other option goes to "
        "refresh_all.py; its --help lists them)",
    )
    r.add_argument(
        "--region",
        choices=sorted(REGION_HOSTS),
        help="the server your account lives on (found by itself the first time)",
    )
    s = sub.add_parser("start", help="start the local web app on 127.0.0.1 and open it")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--no-browser", action="store_true", help="do not open the browser")
    s.add_argument("--region", choices=sorted(REGION_HOSTS), help="as for refresh")
    s.add_argument(
        "--park-scan",
        action="store_true",
        help="turn on the Park page's 'Scan all areas': it logs into your account, "
        "enters every Park area with Free Chats and goes back to yours",
    )
    u = sub.add_parser(
        "update",
        help="the newest holobuilder from GitHub (git pull, safe for the cards a refresh added), "
        "setup when the packages changed, then refresh",
    )
    u.add_argument("--no-refresh", action="store_true", help="update the code only; refresh later")
    args, extra = ap.parse_known_args(argv)
    if extra and args.cmd != "refresh":
        ap.error("unrecognized arguments: " + " ".join(extra))
    if args.cmd == "setup":
        return setup(args)
    if args.cmd == "check":
        return check(args)
    if too_old():
        return 1
    if args.cmd == "refresh":
        return refresh(args, extra)
    if args.cmd == "update":
        return update(args)
    return start(args)


if __name__ == "__main__":
    run_cli(main)
