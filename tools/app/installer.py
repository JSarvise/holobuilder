"""holobuilder-setup.exe: install, update and uninstall the desktop app.

    holobuilder-setup.exe                         install (or reinstall) for this Windows user
    holobuilder-setup.exe --update --pid N --dir D   what the app's "Update" button runs: waits for
                                                  the app to close, swaps the code, reopens it
    holobuilder-setup.exe --uninstall             what Windows' "Apps" runs

One window, the app's own look (ui/setup.html + ui/app.css + the tour): every step written out as
it happens, with its progress. It downloads the LATEST build from api.holobuilder.dev (app/
version.json + the zip it names, sha256-checked), so the exe stays small and never goes stale.

Installs per user, no administrator: %LOCALAPPDATA%\\Programs\\holobuilder\\{runtime,app}, a
Desktop and a Start-menu shortcut, an entry in Windows' Apps list, a copy of itself for updates.
An update keeps this PC's data: app/state (Settings, the account's derived files),
app/tools/recon/proxy/dumps (the synced data and the art cache), app/data/owned.json, app/data/gacha.

No secret inside (it is downloaded from a public repo): the user signs in with Google, the server
admin approves them (tools/app/server/hb_auth.py), and this PC gets its own access key, which the app
keeps in app/state/server_api.json. Every release it installs must carry the publisher's
signature (signing.py). For tests: HB_SETUP_TOKEN (an approved key),
HB_SETUP_SHORTCUTS=<dir> (shortcuts there instead of Desktop/Start), HB_SETUP_NO_REGISTRY=1.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

APP = "holobuilder"
FROZEN = getattr(sys, "frozen", False)
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
UI = HERE / "ui" / "setup.html"
DEFAULT_DIR = Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs")) / APP
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\holobuilder"
PRESERVE = ["app/state", "app/tools/recon/proxy/dumps", "app/data/owned.json", "app/data/gacha"]
SETUP_NAME = "holobuilder-setup.exe"
WEBVIEW2_KEYS = [r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                 r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"]
WEBVIEW2_BOOTSTRAP = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


OFFICIAL_SERVER = "https://api.holobuilder.dev"


def _server_url() -> str:
    """The holobuilder server this installer talks to: HB_SERVER_URL, else the one its build
    baked (build_setup.py, from HB_SERVER_URL at build time), else the official one -- so a
    self-hosted server needs only its own build, no code change."""
    if os.environ.get("HB_SERVER_URL"):
        return os.environ["HB_SERVER_URL"].rstrip("/")
    try:
        import _baked  # noqa: PLC0415
        return getattr(_baked, "SERVER_URL", "") or OFFICIAL_SERVER
    except ImportError:
        return OFFICIAL_SERVER


SERVER_URL = _server_url()

# This installer's own version (the GitHub release line installer-X.Y.Z). The signed
# version.json names the oldest installer still fit to install (`setup_min`, build_app.py):
# an older exe someone kept says so and points at the new one instead of installing with a
# flow the server no longer serves. Updates from the app never hit this: the app runs the
# installer.py of its own (current) version.
SETUP_VERSION = "1.0.0"
RELEASES_URL = "https://github.com/JSarvise/holobuilder/releases/latest"


def _vkey(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return ()


def setup_outdated(meta: dict) -> str | None:
    """The message for an installer older than the signed `setup_min`, else None."""
    if _vkey(SETUP_VERSION) >= _vkey(meta.get("setup_min", "0")):
        return None
    where = RELEASES_URL if SERVER_URL == OFFICIAL_SERVER else "where you got this installer"
    return (f"This installer ({SETUP_VERSION}) is out of date: download the new "
            f"{SETUP_NAME} from {where} and run that one.")


def google_client() -> tuple[str, str]:
    """The Google OAuth DESKTOP client (id, secret). For an installed app Google documents the
    'secret' as not confidential -- PKCE and the 127.0.0.1 redirect are what protect the flow --
    so it may ship inside the public exe (build_setup.py bakes it from state/google_oauth.json)."""
    try:
        import _baked  # noqa: PLC0415
        return _baked.GOOGLE_CLIENT_ID, _baked.GOOGLE_CLIENT_SECRET
    except ImportError:
        conf = json.loads((Path(__file__).resolve().parents[2] / "state" / "google_oauth.json").read_text("utf8"))
        return conf["client_id"], conf["client_secret"]


def google_id_token(open_browser=None, timeout: float = 300) -> str:
    """Sign in with Google in the user's own browser; return the ID token (OAuth 2.0 for
    installed apps: authorization code + PKCE + loopback redirect)."""
    import base64  # noqa: PLC0415
    import secrets  # noqa: PLC0415
    import webbrowser  # noqa: PLC0415
    from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: PLC0415
    from urllib.parse import parse_qs, urlencode, urlparse  # noqa: PLC0415

    cid, csecret = google_client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    got: dict = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                got.update({k: v[0] for k, v in q.items()})
            ok = "code" in got
            body = (("<h2>Signed in. You can go back to holobuilder setup.</h2>" if ok else
                     "<h2>Sign-in was not completed. Go back to holobuilder setup and try again.</h2>")
                    ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<body style='font-family:sans-serif;background:#0E0D20;color:#F3F2FF;"
                             b"display:grid;place-items:center;height:90vh'>" + body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_address[1]}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code",
        "scope": "openid email profile", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "prompt": "select_account"})
    (open_browser or webbrowser.open)(url)
    srv.timeout = 1
    deadline = time.time() + timeout
    while "code" not in got and "error" not in got and time.time() < deadline:
        srv.handle_request()
    srv.server_close()
    if "code" not in got:
        raise RuntimeError(f"Google sign-in did not complete ({got.get('error', 'timed out')})")
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=urlencode({
        "code": got["code"], "client_id": cid, "client_secret": csecret, "redirect_uri": redirect,
        "grant_type": "authorization_code", "code_verifier": verifier}).encode())
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 -- google
        return json.loads(r.read())["id_token"]


def post(route: str, doc: dict, token: str = "") -> dict:
    req = urllib.request.Request(SERVER_URL + "/" + route, data=json.dumps(doc).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 -- our own host
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(json.loads(exc.read() or b"{}").get("error", str(exc))) from exc


def clean_env(*, reset: bool = False) -> dict:
    """This process's environment without PyInstaller's own variables (_PYI_*, _MEIPASS*).

    Without this, 'Update now' fails with "Failed to load Python DLL ...\\_MEIxxxxxx\\
    python313.dll": the installer is a one-file PyInstaller program, and the app it opens at
    the end of an install inherits its _PYI_* variables; when that app later starts the
    installer again for an update, the new installer's bootloader reads them, takes itself for
    a child of the first one and looks for its DLLs in that one's _MEI folder, gone since it
    exited. `reset` also sets PYINSTALLER_RESET_ENVIRONMENT, PyInstaller's own switch for
    starting a separate PyInstaller program."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("_PYI_", "_MEIPASS"))}
    if reset:
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    else:
        env.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
    return env


def set_username(token: str, username: str) -> dict:
    """POST auth/username -> {"ok": True, "username"} or {"ok": False, "error"} (taken, invalid)."""
    try:
        return {"ok": True, **post("auth/username", {"username": username}, token)}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"the server did not answer ({exc})"}


def me(token: str) -> dict:
    try:
        with get(SERVER_URL, token, "auth/me", 15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return {"status": "unknown" if exc.code == 401 else "error"}


def saved_token(d: Path) -> str:
    try:
        return json.loads((d / "app" / "state" / "server_api.json").read_text("utf8"))["token"]
    except (OSError, ValueError, KeyError):
        return ""


def get(url: str, token: str, route: str, timeout: int = 30):
    req = urllib.request.Request(url.rstrip("/") + "/" + route, headers={"Authorization": f"Bearer {token}"})
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 -- our own https host


def installed_version(d: Path) -> str | None:
    try:
        return (d / "app" / "tools" / "app" / "VERSION").read_text("utf8").strip()
    except OSError:
        return None


def free_gb(d: Path) -> float:
    p = d
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free / 1e9


def preserve_moves(old_root: Path, new_root: Path) -> list[tuple[Path, Path]]:
    """(from, to) for every piece of this PC's data an update carries into the new tree."""
    return [(old_root / rel, new_root / rel) for rel in PRESERVE if (old_root / rel).exists()]


def webview2_ok() -> bool:
    import winreg  # noqa: PLC0415
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key in WEBVIEW2_KEYS:
            try:
                with winreg.OpenKey(root, key) as k:
                    if winreg.QueryValueEx(k, "pv")[0] not in ("", "0.0.0.0"):
                        return True
            except OSError:
                continue
    return False


def ensure_webview2() -> None:
    """Windows 11 ships WebView2; an old Windows 10 may not. Microsoft's own bootstrapper then
    (its own window, its own progress) before ours can open."""
    if webview2_ok():
        return
    exe = Path(tempfile.gettempdir()) / "MicrosoftEdgeWebview2Setup.exe"
    with urllib.request.urlopen(WEBVIEW2_BOOTSTRAP, timeout=60) as r:  # noqa: S310 -- microsoft.com
        exe.write_bytes(r.read())
    subprocess.run([str(exe), "/install"], check=False)


def ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def make_shortcut(lnk: Path, target: Path, args: str, workdir: Path, icon: Path) -> None:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({ps_quote(str(lnk))});"
              f"$s.TargetPath={ps_quote(str(target))};$s.Arguments={ps_quote(args)};"
              f"$s.WorkingDirectory={ps_quote(str(workdir))};$s.IconLocation={ps_quote(str(icon) + ',0')};"
              f"$s.Description='holobuilder';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], check=True,
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def shortcut_dirs() -> list[Path]:
    if os.environ.get("HB_SETUP_SHORTCUTS"):
        return [Path(os.environ["HB_SETUP_SHORTCUTS"])]
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)          # CSIDL_DESKTOPDIRECTORY
    desktop = Path(buf.value)
    start = Path(os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"))
    return [desktop, start]


def register(d: Path, version: str) -> None:
    if os.environ.get("HB_SETUP_NO_REGISTRY") == "1":
        return
    import winreg  # noqa: PLC0415
    size_kb = sum(p.stat().st_size for p in d.rglob("*") if p.is_file()) // 1024
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, kind, val in [
            ("DisplayName", winreg.REG_SZ, "holobuilder"), ("DisplayVersion", winreg.REG_SZ, version),
            ("Publisher", winreg.REG_SZ, "holobuilder"), ("InstallLocation", winreg.REG_SZ, str(d)),
            ("DisplayIcon", winreg.REG_SZ, str(d / "app" / "tools" / "app" / "ui" / "app.ico")),
            ("UninstallString", winreg.REG_SZ, f'"{d / SETUP_NAME}" --uninstall'),
            ("EstimatedSize", winreg.REG_DWORD, size_kb), ("NoModify", winreg.REG_DWORD, 1),
            ("NoRepair", winreg.REG_DWORD, 1)]:
            winreg.SetValueEx(k, name, 0, kind, val)


def unregister() -> None:
    if os.environ.get("HB_SETUP_NO_REGISTRY") == "1":
        return
    import winreg  # noqa: PLC0415
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass


def registered_dir() -> Path | None:
    import winreg  # noqa: PLC0415
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            return Path(winreg.QueryValueEx(k, "InstallLocation")[0])
    except OSError:
        return None


def wait_pid(pid: int, timeout: float = 60) -> None:
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000, False, pid)                                # SYNCHRONIZE
    if h:
        k32.WaitForSingleObject(h, int(timeout * 1000))
        k32.CloseHandle(h)


def install_dir(explicit: str | None) -> Path:
    """Where this run works: --dir; else the folder of THIS exe when it is an installed copy
    (the app's Update button and Windows' uninstall run that copy); else the registered
    install; else the default. Without the second rule an update run with the registry off
    would go to the default folder and install a second copy there."""
    if explicit:
        return Path(explicit)
    me = Path(sys.executable).resolve().parent if FROZEN else None
    if me is not None and (me / "app" / "tools" / "app" / "launcher.py").is_file():
        return me
    return registered_dir() or DEFAULT_DIR


class Api:
    def __init__(self, mode: str, pid: int | None, folder: str | None = None) -> None:
        self._window = None
        self.mode, self.pid = mode, pid
        self.dir = str(install_dir(folder))
        self._latest: dict | None = None
        # this PC's access key: an existing install's, or the one sign-in gets (never baked)
        self._token = os.environ.get("HB_SETUP_TOKEN") or saved_token(Path(self.dir))
        self._named, self._username = threading.Event(), ""

    def _emit(self, **ev) -> None:
        if self._window is not None:
            self._window.evaluate_js(f"window.hb && hb.progress({json.dumps(ev)})")

    def info(self) -> dict:
        d = Path(self.dir)
        status = me(self._token)["status"] if self._token else "signed-out"
        latest, error = None, None
        if status == "approved":
            try:
                with get(SERVER_URL, self._token, "app/version.json", 15) as r:
                    latest = self._latest = json.loads(r.read())
            except Exception as exc:  # noqa: BLE001 -- shown, with a retry
                error = f"the holobuilder server did not answer ({exc})"
        if latest and self.mode != "update":
            error = setup_outdated(latest) or error
        return {"mode": self.mode, "dir": self.dir, "installed": installed_version(d),
                "latest": latest, "error": error, "free_gb": round(free_gb(d), 1),
                "default_dir": str(DEFAULT_DIR), "status": status}

    def sign_in(self) -> dict:
        threading.Thread(target=self._sign_in, name="sign-in", daemon=True).start()
        return {"ok": True}

    def _sign_in(self) -> None:
        try:
            self._emit(step="auth", state="browser")
            r = post("auth/register", {"id_token": google_id_token(), "app": "setup"})
            self._token = r["token"]
            # a username before anything else: the admin approves people by it, and Google's
            # name (the person's real one) is not kept
            if "username" in r and not r["username"] and r["status"] != "revoked":
                self._named.clear()
                self._emit(step="auth", state="username")
                self._named.wait()
                r = {**r, "username": self._username, "name": self._username}
            self._emit(step="auth", state=r["status"], name=r["name"], email=r["email"])
            while r["status"] == "pending":         # the admin approves; ask every 10 s
                time.sleep(10)
                r = {**r, **me(self._token)}
            if r["status"] == "approved":
                self.info()
            self._emit(step="auth", state=r["status"], name=r.get("name"), email=r.get("email"))
        except Exception as exc:  # noqa: BLE001
            self._emit(step="auth", state="error", message=str(exc))

    def set_username(self, username: str) -> dict:
        r = set_username(self._token, username)
        if r["ok"]:
            self._username = r["username"]
            self._named.set()
        return r

    def choose_dir(self) -> str | None:
        import webview  # noqa: PLC0415
        got = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=str(Path(self.dir).parent))
        if got:
            p = Path(got[0])
            self.dir = str(p if p.name.lower() == APP else p / APP)
        return self.dir

    def run(self) -> dict:
        threading.Thread(target=self._run, name="setup", daemon=True).start()
        return {"ok": True}

    def _run(self) -> None:
        d = Path(self.dir)
        url, token = SERVER_URL, self._token
        work = d.parent / f".{APP}-setup"
        try:
            if self.mode == "update" and not (d / "app" / "tools" / "app" / "launcher.py").is_file():
                raise RuntimeError(f"no holobuilder installed in {d}: nothing to update")
            if self.pid:
                self._emit(step="wait", state="run")
                wait_pid(self.pid)
                time.sleep(1.0)                    # the engine's own exit, after the window's
                self._emit(step="wait", state="done")

            self._emit(step="pc", state="run")
            meta = self._latest or json.loads(get(url, token, "app/version.json").read())
            import signing  # noqa: PLC0415
            signing.verify(signing.canonical(meta), meta.get("sig", ""))   # BadSignature: refuse
            if self.mode != "update" and setup_outdated(meta):
                raise RuntimeError(setup_outdated(meta))
            need = meta.get("size", 50e6) * 4 / 1e9
            if free_gb(d) < need:
                raise RuntimeError(f"not enough free space: {free_gb(d):.1f} GB, {need:.1f} GB needed")
            self._emit(step="pc", state="done", detail=f"{free_gb(d):.0f} GB free")

            # download, sha-checked
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            zpath = work / "build.zip"
            total, got, h = int(meta.get("size") or 0), 0, hashlib.sha256()
            t0, last = time.time(), 0.0
            with get(url, token, meta["url"], 60) as r, open(zpath, "wb") as out:
                while True:
                    chunk = r.read(1 << 18)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if time.time() - last > 0.1:
                        last = time.time()
                        self._emit(step="download", state="run", bytes=got, total=total,
                                   rate=got / max(time.time() - t0, 1e-3))
            if meta.get("sha256") and h.hexdigest() != meta["sha256"]:
                raise RuntimeError("the download arrived damaged (sha256 mismatch); try again")
            self._emit(step="download", state="done", bytes=got, total=total, version=meta["latest"])

            # unpack
            new = work / "new"
            with zipfile.ZipFile(zpath) as zf:
                names = zf.namelist()
                for i, n in enumerate(names):
                    zf.extract(n, new)
                    if i % 40 == 0 or i == len(names) - 1:
                        self._emit(step="unpack", state="run", i=i + 1, n=len(names))
            self._emit(step="unpack", state="done", n=len(names))

            # keep this PC's data, then switch
            if (d / "app").exists():
                self._emit(step="keep", state="run")
                for src, dst in preserve_moves(d, new):
                    if dst.exists():
                        shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                self._emit(step="keep", state="done")
            self._emit(step="switch", state="run")
            d.mkdir(parents=True, exist_ok=True)
            old = work / "old"
            old.mkdir()
            for part in ("app", "runtime"):
                if (d / part).exists():
                    os.replace(d / part, old / part)
                os.replace(new / part, d / part)
            # the server's key, so the app never asks for it
            state = d / "app" / "state"
            state.mkdir(parents=True, exist_ok=True)
            api = state / "server_api.json"
            if not api.exists():
                api.write_text(json.dumps({"url": url, "token": token}, indent=1), "utf8")
            if FROZEN:                              # a copy of this exe, for updates and removal
                me = Path(sys.executable)
                if me.resolve() != (d / SETUP_NAME).resolve():
                    shutil.copy2(me, d / SETUP_NAME)
            self._emit(step="switch", state="done")

            self._emit(step="shortcuts", state="run")
            icon = d / "app" / "tools" / "app" / "ui" / "app.ico"
            for folder in shortcut_dirs():
                make_shortcut(folder / "holobuilder.lnk", d / "runtime" / "pythonw.exe",
                              f'"{d / "app" / "tools" / "app" / "launcher.py"}"', d, icon)
            register(d, meta["latest"])
            self._emit(step="shortcuts", state="done")
            shutil.rmtree(work, ignore_errors=True)
            self._emit(step="done", version=meta["latest"])
        except Exception as exc:  # noqa: BLE001 -- shown with a retry
            self._emit(step="error", message=f"{type(exc).__name__}: {exc}")

    def launch(self) -> None:
        d = Path(self.dir)
        subprocess.Popen([str(d / "runtime" / "pythonw.exe"), str(d / "app" / "tools" / "app" / "launcher.py")],
                         cwd=str(d), env=clean_env(), creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_GROUP
        self._window.destroy()

    def close(self) -> None:
        self._window.destroy()

    def uninstall(self) -> dict:
        threading.Thread(target=self._uninstall, name="uninstall", daemon=True).start()
        return {"ok": True}

    def _uninstall(self) -> None:
        d = Path(self.dir)
        try:
            self._emit(step="remove", state="run")
            # the app, its engine and workers all run d\runtime\python*.exe: close them first,
            # or Windows keeps their files and the folder cannot go
            runtime = str(d / "runtime").replace("'", "''")
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like "
                            f"'{runtime}\\*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"],
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            time.sleep(1.0)
            for folder in shortcut_dirs():
                (folder / "holobuilder.lnk").unlink(missing_ok=True)
            unregister()
            for part in ("app", "runtime"):
                shutil.rmtree(d / part, ignore_errors=True)
            self._emit(step="remove", state="done")
            # this exe cannot delete itself while it runs: cmd removes the folder after we exit
            subprocess.Popen(f'cmd /c ping -n 6 127.0.0.1 >nul & rmdir /s /q "{d}"', shell=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._emit(step="removed")
        except Exception as exc:  # noqa: BLE001
            self._emit(step="error", message=f"{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    # our own agent: the server sits behind Cloudflare, whose browser checks stop Python's
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "holobuilder-setup")]
    urllib.request.install_opener(opener)
    mode = "uninstall" if "--uninstall" in args else "update" if "--update" in args else "install"
    pid = int(args[args.index("--pid") + 1]) if "--pid" in args else None
    folder = args[args.index("--dir") + 1] if "--dir" in args else None
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("holobuilder.setup")
    except (AttributeError, OSError):
        pass
    ensure_webview2()
    import webview  # noqa: PLC0415
    api = Api(mode, pid, folder)
    window = webview.create_window(
        "holobuilder setup" if mode != "uninstall" else "Remove holobuilder", url=str(UI), js_api=api,
        width=1280, height=780, min_size=(1100, 700), background_color="#0E0D20")
    api._window = window
    webview.start(private_mode=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
