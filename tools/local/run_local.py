"""Run holobuilder from a clone of this repository, entirely on this PC: no holobuilder server,
no sign-in, no approval. The three scripts at the top of the repository call it:

    setup.cmd     creates .venv and installs the packages, then:  run_local.py check
    refresh.cmd   run_local.py refresh [--region us|jp|as] [any refresh_all.py option]
    start.cmd     run_local.py start [--port N] [--no-browser] [--region us|jp|as]

`refresh` does what the desktop app does before it reads an account, with this PC's own game
install in place of the holobuilder server:

  1. the client profile: the headers the game's client sends, from this PC's hardware
     (tools/app/client_profile.py; a profile captured from the real client is never replaced);
  2. the game client's build constants: found in the game install when this PC has none, or
     when they no longer open the server's answers (tools/recon/proxy/game_keys.py --find);
  3. the account file: HB_CREDENTIAL_FILE, else the one file in the game's LocalLow folder
     shaped like one. It is read in place at each refresh and never copied;
  4. the account's region: --region, else HB_ACCOUNT_HOST, else the one whose server accepted
     the login the first time, kept in state/local.json;
  5. tools/recon/proxy/refresh_all.py --merge-collection --pictures-later: the scored data first,
     the pictures in the background afterwards (one picture run at a time); then the pull
     history and its banners (gacha_sync.py), as the pages' Refresh does.

`start` stops an earlier holobuilder server of this folder that still holds the port (nothing
else: another program keeps it, and the next free port is used), starts tools/web/server.py on
127.0.0.1 with the same account settings, so the pages' Refresh button works too, and opens
the browser once it answers.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROXY = REPO / "tools" / "recon" / "proxy"
KERNEL = REPO / "tools" / "web" / "kernel"
SERVER = REPO / "tools" / "web" / "server.py"
STATE = REPO / "state"
LOCAL = STATE / "local.json"             # this folder's own choices: the account's region
DUMPS = PROXY / "dumps"
DEFAULT_PORT = 8731
REGIONS = ("us", "as", "jp")             # the order a region is looked for: worldwide ones first
PACKAGES = (("Crypto", "pycryptodome"), ("UnityPy", "UnityPy"), ("h2", "h2"),
            ("highspy", "highspy"), ("PIL", "pillow"), ("requests", "requests"))
KERNELS = ("notewalk", "connect", "surrogate")
WINLIBS = "winget install BrechtSanders.WinLibs.POSIX.UCRT"
MARKS = {True: "ok", False: "X ", None: "--"}       # ready / blocks the local route / not yet

for _path in (REPO / "tools" / "app", PROXY, KERNEL):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
import app_settings  # noqa: E402
import game_paths  # noqa: E402

REGION_HOSTS = app_settings.REGION_HOSTS


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
        return (path, "HB_CREDENTIAL_FILE") if path.is_file() else \
            (None, f"HB_CREDENTIAL_FILE names no file: {path}")
    hits = app_settings.credential_files(folder)
    if len(hits) == 1:
        return hits[0], "the game's own folder"
    if hits:
        return None, (f"{len(hits)} files in {shown(folder)} look like an account file: set "
                      "HB_CREDENTIAL_FILE to the one of the account you play")
    return None, (f"no account file in {shown(folder)}: start the game on this PC once and get past "
                  "its title screen, or set HB_CREDENTIAL_FILE")


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


def ensure_profile() -> None:
    import client_profile  # noqa: PLC0415

    got = client_profile.ensure()
    what = ("captured from the game, kept as it is" if got["captured"]
            else "written from this PC's hardware" if got["written"] else "current")
    print(f"  client profile: {what}"
          + ("" if got["steam_id"] else " (no Steam ID found: sign in to Steam for the next refresh)"))


def ensure_keys(host: str) -> bool:
    """This PC has the game client's build constants and they still open the server's answers;
    found in the game install when not."""
    import game_keys  # noqa: PLC0415

    if game_keys.grpc_seed(required=False) is not None:
        if game_keys.check(host) is not False:     # current, or the server could not say
            print(f"  game constants: on this PC ({game_keys.source()})")
            return True
        print("  game constants: the ones on this PC no longer open the server's answers "
              "(a game update?); finding them again")
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
            env["HOLODREAMS_DIR"] = game_paths.game_root()
        except SystemExit:
            pass
    return env


def first_refresh() -> bool:
    return not (DUMPS / "charts.json").is_file()


def refresh(args: argparse.Namespace, extra: list[str]) -> int:
    print("holobuilder -- reading the game and your account into this folder")
    try:
        print(f"  game install: {shown(game_paths.game_root())}")
    except SystemExit:
        print("  [X] hololive Dreams is not installed where Steam keeps games; install it, or set "
              "HOLODREAMS_DIR to the folder that holds hololive-Dreams_Data")
        return 1
    credential, how = find_credential()
    if credential is None:
        print(f"  [X] account file: {how}")
        return 1
    os.environ["HB_CREDENTIAL_FILE"] = str(credential)
    print(f"  account file: found in {how} (read in place, never copied)")
    if args.region:
        save(region=args.region)
    ensure_profile()
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
    cmd = [sys.executable, "-u", str(PROXY / "refresh_all.py"), "--merge-collection", *extra]
    if "--pictures-later" not in extra:
        cmd.append("--pictures-later")
    print("  the pictures are fetched in the background afterwards (--pictures-later)\n")
    env = child_env(credential, host)
    try:
        code, seen = _stream(cmd, env)
        if code not in (3, 4):        # maintenance, no constants: the gacha read is refused too
            # the pull history and its banners, as the pages' Refresh reads them after the refresh
            print("\n--- your pull history (gacha_sync.py) ---")
            gacha, more = _stream([sys.executable, "-u", str(PROXY / "gacha_sync.py")], env)
            seen = f"{seen} {more}".strip()
            if gacha:
                print(f"  (the pull history was not read: exit {gacha})")
    except KeyboardInterrupt:
        print("\nrefresh stopped")
        return 130
    print()
    if code == 0:
        print("Done. Start holobuilder with start.cmd (or press Refresh in its pages next time).")
    elif "DATE_CHANGED" in seen:
        print("The game's day has changed since this account last opened the game (04:00 JST):")
        print("open the game once, then run refresh.cmd again. Everything else above is current.")
    else:
        print(f"The refresh ended with exit code {code}: see the lines above, and "
              "docs/troubleshooting.md.")
    return code


def _stream(cmd: list[str], env: dict) -> tuple[int, str]:
    """Run `cmd`, echoing its output as it comes -> (exit code, the words worth reacting to)."""
    proc = subprocess.Popen(cmd, cwd=str(REPO), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1)
    seen = set()
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        if "DATE_CHANGED" in line:
            seen.add("DATE_CHANGED")
    return proc.wait(), " ".join(sorted(seen))


# ---------------------------------------------------------------- the web app
def port_free(port: int) -> bool:
    """Whether server.py could bind 127.0.0.1:port now (it binds without SO_REUSEADDR too)."""
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def listeners(port: int) -> list[dict]:
    """The processes listening on `port`: [{"pid", "name", "cmd"}]; [] when it cannot be asked."""
    script = ("@(foreach ($id in (Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction "
              "SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)) {"
              " $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $id) -ErrorAction SilentlyContinue;"
              " [pscustomobject]@{pid=[int]$id; name=[string]$p.Name; cmd=[string]$p.CommandLine} })"
              " | ConvertTo-Json -Compress" % port)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()   # no quoting to get wrong
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        doc = json.loads(out) if out else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return [d for d in (doc if isinstance(doc, list) else [doc]) if isinstance(d, dict)]


def ours(cmdline: str, repo: Path = REPO) -> bool:
    """Whether a command line runs this folder's tools/web/server.py."""
    norm = cmdline.replace("/", "\\").lower()
    return str(repo / "tools" / "web" / "server.py").replace("/", "\\").lower() in norm


def claim_port(port: int, *, tries: int = 20) -> int | None:
    """`port` once free: an earlier holobuilder server of this folder on it is stopped (named
    first); anything else keeps it, and the next free port is used instead."""
    if port_free(port):
        return port
    for proc in listeners(port):
        if ours(proc.get("cmd") or ""):
            print(f"  port {port}: stopping an earlier holobuilder server of this folder "
                  f"(PID {proc['pid']})")
            subprocess.run(["taskkill", "/PID", str(proc["pid"]), "/F"], capture_output=True)
        else:
            print(f"  port {port} is in use by PID {proc.get('pid')} ({proc.get('name') or 'unknown'}); "
                  "leaving it alone")
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
        print("  [!] no game data in this folder yet: run refresh.cmd first, or the pages stay empty")
    port = claim_port(args.port)
    if port is None:
        return 1
    url = f"http://127.0.0.1:{port}/optimizer"
    proc = subprocess.Popen([sys.executable, "-u", str(SERVER), "--port", str(port)], cwd=str(REPO),
                            env=child_env(credential, region_host(args.region)))
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


def shown(path) -> str:
    """A path as printed: the user's own folder as %USERPROFILE%, so pasted output does not
    carry the Windows user name."""
    text, home = str(path), os.environ.get("USERPROFILE", "")
    if home and text.lower().startswith(home.lower()):
        return "%USERPROFILE%" + text[len(home):]
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
    row(py >= (3, 11), "Python", f"{platform.python_version()} ({shown(sys.executable)})"
        + ("" if py == (3, 13) else "; the project is tested on 3.13"),
        "" if py >= (3, 11) else "install Python 3.13 (python.org) and run setup.cmd again")
    missing = [pip for mod, pip in PACKAGES if importlib.util.find_spec(mod) is None]
    row(not missing, "packages", "installed" if not missing else "missing: " + ", ".join(missing),
        "" if not missing else "run setup.cmd again (it installs them)")
    gcc = shutil.which("gcc")
    if gcc is None:
        row(False, "C compiler", "gcc is not on the PATH: the team search needs it",
            f"install WinLibs ({WINLIBS} or winlibs.com), open a new window, run setup.cmd again")
    else:
        err = build_kernels()
        row(err is None, "C compiler", f"gcc ({shown(gcc)}): " + ("search kernels built" if err is None
                                                          else f"a kernel did not build: {err}"))
    try:
        row(True, "game install", shown(game_paths.game_root()))
    except SystemExit:
        row(False, "game install", "hololive Dreams not found in this PC's Steam libraries",
            "install it from Steam, or set HOLODREAMS_DIR to the folder that holds hololive-Dreams_Data")
    credential, how = find_credential()
    row(True if credential else False, "account file",
        f"found in {how} (read in place, never copied)" if credential else how)
    import game_keys  # noqa: PLC0415
    src = game_keys.source()
    row(True if src else None, "game constants",
        f"on this PC ({src})" if src else "not yet: refresh.cmd finds them in your game install")
    host = region_host()
    row(True if host else None, "region", host or "not yet: the first refresh.cmd finds it")
    built = not first_refresh() and (STATE / "collection.json").is_file()
    row(True if built else None, "game data", "built" if built else "not yet: run refresh.cmd")
    print()
    if blocking:
        print("Fix the [X] lines above, then run setup.cmd again.")
    elif built:
        print("Ready: start.cmd opens holobuilder; refresh.cmd reads your account again.")
    else:
        print("Ready for the first refresh: run refresh.cmd (it takes a while the first time), "
              "then start.cmd.")
    return 1 if blocking else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run_local.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="what this PC has, and what is missing (setup.cmd runs it)")
    r = sub.add_parser("refresh", help="read the game and your account (any other option goes to "
                                       "refresh_all.py; its --help lists them)")
    r.add_argument("--region", choices=sorted(REGION_HOSTS),
                   help="the server your account lives on (found by itself the first time)")
    s = sub.add_parser("start", help="start the local web app on 127.0.0.1 and open it")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--no-browser", action="store_true", help="do not open the browser")
    s.add_argument("--region", choices=sorted(REGION_HOSTS), help="as for refresh")
    args, extra = ap.parse_known_args(argv)
    if extra and args.cmd != "refresh":
        ap.error("unrecognized arguments: " + " ".join(extra))
    if args.cmd == "check":
        return check(args)
    if args.cmd == "refresh":
        return refresh(args, extra)
    return start(args)


if __name__ == "__main__":
    sys.exit(main())
