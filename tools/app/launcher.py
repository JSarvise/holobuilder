"""holobuilder, the desktop app: one native window over the web app.

    runtime\\pythonw.exe app\\tools\\app\\launcher.py        (what the installer's shortcut runs)
    proxyenv\\Scripts\\python tools\\app\\launcher.py        (development)

What it does, each step shown on the start screen (ui/index.html) as it happens:

  1. reads this PC: physical cores, threads, RAM, GPU -- the search uses every thread unless
     Settings say otherwise
  2. asks api.holobuilder.dev for the app's latest version: "update available" or, below the
     server's min_version, "update required"
  3. syncs the shared data (tools/app/content_store.py; nothing when it is current)
  4. writes a client profile from this PC's hardware if there is none (client_profile.py)
  5. starts the web server (tools/web/server.py) on a free local port with the Settings as
     environment (app_settings.env) and opens it in this window

The first run, or a Settings problem, stops before step 3 at the Settings form. The pages of the
web app reach the same Settings through `window.pywebview.api` (the rail's gear button).

It is the only part of the app that writes Settings; tools/web still writes nothing.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import app_settings  # noqa: E402
import content_store  # noqa: E402

UI = HERE / "ui" / "index.html"
VERSION_FILE = HERE / "VERSION"          # written by the build; absent in a development checkout
LOG = REPO / "state" / "server.log"
DUMPS = REPO / "tools" / "recon" / "proxy" / "dumps"
APP_ID = "holobuilder.app"
UPDATE_POLL_S = 600                       # one small GET every 10 min while the app is open
LOCALLOW = app_settings.LOCALLOW


def clean_env(*, reset: bool = False) -> dict:
    """This process's environment without PyInstaller's own variables (_PYI_*, _MEIPASS*).

    Without this, 'Update now' fails with "Failed to load Python DLL ...\\_MEI220522\\
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


def server_me(s: dict) -> dict:
    """GET auth/me with this PC's key; {} when the server cannot be asked."""
    try:
        req = urllib.request.Request(s["server_url"].rstrip("/") + "/auth/me",
                                     headers={"Authorization": f"Bearer {s['server_token']}"})
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 -- the app's own https host
            doc = json.loads(r.read())
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001 -- offline, an older server: the start goes on
        return {}


def server_set_username(s: dict, username: str) -> dict:
    """POST auth/username -> {"ok": True, "username"} or {"ok": False, "error"}."""
    req = urllib.request.Request(s["server_url"].rstrip("/") + "/auth/username",
                                 data=json.dumps({"username": username}).encode(),
                                 headers={"Authorization": f"Bearer {s['server_token']}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 -- the app's own https host
            return {"ok": True, **json.loads(r.read())}
    except urllib.error.HTTPError as exc:
        try:
            return {"ok": False, "error": json.loads(exc.read() or b"{}").get("error", str(exc))}
        except ValueError:
            return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": f"the server did not answer ({exc})"}


def account_failure(lines: list[str]) -> list[str]:
    """What the start screen shows when the account read fails: the `account` step's own
    last lines (its error: a refused login, a missing account file, the region) and the run's
    FAILED line. The refresh's closing summary alone does not say why."""
    start = max((k for k, ln in enumerate(lines) if ln.startswith("[run ] account ")), default=None)
    if start is None:
        return lines[-12:]
    block = []
    for ln in lines[start + 1:]:
        if ln.startswith("[run ] ") or ln.startswith("------"):
            break
        if ln.strip():
            block.append(ln.strip())
    failed = [ln for ln in lines if ln.startswith("FAILED")]
    return block[-10:] + failed[-1:]


def app_version() -> str:
    try:
        return VERSION_FILE.read_text("utf8").strip() or "dev"
    except OSError:
        return "dev"


def version_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return ()


def update_state(local: str, remote: dict) -> str:
    """'none' | 'available' | 'required' | 'dev' (a development checkout never updates)."""
    if local == "dev" or not version_key(local):
        return "dev"
    if version_key(local) < version_key(str(remote.get("min_version", "0"))):
        return "required"
    if version_key(local) < version_key(str(remote.get("latest", "0"))):
        return "available"
    return "none"


# ------------------------------------------------------------------ this PC

def hardware() -> dict:
    """Physical cores, threads, RAM (GB), GPU name."""
    threads = os.cpu_count() or 1
    cores = threads
    try:   # one 32-byte SYSTEM_LOGICAL_PROCESSOR_INFORMATION per relation; Relationship 0 = core
        k32 = ctypes.windll.kernel32
        n = ctypes.c_ulong(0)
        k32.GetLogicalProcessorInformation(None, ctypes.byref(n))
        buf = ctypes.create_string_buffer(n.value)
        if k32.GetLogicalProcessorInformation(buf, ctypes.byref(n)):
            cores = sum(1 for i in range(0, n.value, 32)
                        if int.from_bytes(buf.raw[i + 8:i + 12], "little") == 0) or threads
    except (AttributeError, OSError):
        pass
    ram_gb, gpu = 0.0, ""
    try:
        import client_profile  # noqa: PLC0415
        ram_gb = round(client_profile._ram_mb() / 1024, 1)
        gpu = client_profile.pick_gpu(client_profile._adapters()).get("name", "")
    except Exception:  # noqa: BLE001 -- cosmetic: the start screen shows what it could read
        pass
    return {"cores": cores, "threads": threads, "ram_gb": ram_gb, "gpu": gpu}


LOG_TAIL_LINES = 60
_SECRETISH = None


def redact(text: str) -> str:
    """Hide what identifies the person in text meant for a public issue: the Windows user
    folder (it carries the account name) and any long token-like run (credentials, keys)."""
    import re  # noqa: PLC0415
    global _SECRETISH
    if _SECRETISH is None:
        _SECRETISH = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
    home = os.path.expandvars("%USERPROFILE%")
    if home and "%" not in home:
        text = re.sub(re.escape(home), "%USERPROFILE%", text, flags=re.IGNORECASE)
    return _SECRETISH.sub("<hidden>", text)


def diagnostics(s: dict, *, engine: str, log: Path = LOG) -> str:
    """The text "Copy diagnostics" puts on the clipboard, for a bug report: versions, this PC,
    the settings WITHOUT the personal ones (account file path, Steam ID, access key: only
    whether they are set), the content version and the end of the engine's log, redacted."""
    import platform  # noqa: PLC0415
    hw = hardware()
    try:
        content = json.loads((content_store.STORE / "manifest.json").read_text("utf8"))
        content_id = str(content.get("id", "?"))
    except (OSError, ValueError):
        content_id = "none"
    cred = s.get("credential_path") or ""
    lines = [
        "holobuilder diagnostics",
        f"app {app_version()} · content {content_id} · engine {engine}",
        f"Windows {platform.version()} · Python {platform.python_version()}",
        f"{hw['cores']} cores / {hw['threads']} threads · {hw['ram_gb']} GB · {hw['gpu'] or 'GPU ?'}",
        f"region {s.get('region')} · workers {s.get('workers') or 'all'} · art cache "
        f"{s.get('cache_mb') or 'unlimited'} MB · server {s.get('server_url')}",
        f"account file {'set, exists' if cred and Path(cred).is_file() else 'set, MISSING' if cred else 'not set'}"
        f" · Steam ID {'typed' if s.get('steam_id') else 'from Steam'}"
        f" · access key {'set' if s.get('server_token') else 'NOT SET'}",
    ]
    problems = app_settings.validate(s)
    if problems:
        lines.append("settings problems: " + "; ".join(f"{k}: {v}" for k, v in problems.items()))
    try:
        tail = log.read_text("utf8", errors="replace").splitlines()[-LOG_TAIL_LINES:]
    except OSError:
        tail = ["(no engine log yet)"]
    lines += ["", f"--- last {len(tail)} lines of the engine log ---", *tail]
    return redact("\n".join(lines))


def _collection_current(path: Path | None = None, board: Path | None = None) -> bool:
    """Whether the account was read by this app's refresh: one read before 0.1.7 has no
    player.power_up_permyriad (no Member Power-Up Bonus), one before 0.1.9 built the
    boards with every socket at Connect level 1 (a bloom-5 card's level 2 missed), one before
    0.1.13 shows the publisher's event history (its results) instead of its own."""
    path = path or REPO / "state" / "collection.json"
    board = board or DUMPS / "board.json"
    try:
        doc = json.loads(path.read_text("utf-8"))
        built = json.loads(board.read_text("utf-8"))
    except (OSError, ValueError):
        return False
    history = DUMPS / "event_history.json" if board == DUMPS / "board.json" else board.with_name("event_history.json")
    try:     # before 0.1.13 the app received the publisher's own history (its results)
        own_history = json.loads(history.read_text("utf-8")).get("built_for") is not None
    except (OSError, ValueError):
        own_history = True                       # none yet: nothing of anyone's to replace
    return ("power_up_permyriad" in (doc.get("player") or {}) and built.get("connect_level") == "auto"
            and own_history)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def data_uri(path: Path, mime: str) -> str | None:
    try:
        return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
    except OSError:
        return None


def holomems() -> list[dict]:
    """[{chr, name, card}] for every holomem in the catalogue: the card is her first 5-star
    (the start screen's art), else her first card."""
    try:
        doc = json.loads((REPO / "data" / "cards.json").read_text("utf8"))
    except (OSError, ValueError):
        return []
    out: dict[str, dict] = {}
    for c in doc.get("cards", doc) if isinstance(doc, dict) else doc:
        cid, name = c.get("id", ""), c.get("member", "")
        if not cid.startswith("card-") or not name:
            continue
        chr_id = "chr-" + cid.split("-")[1]
        five = "-5-" in cid
        cur = out.get(chr_id)
        if cur is None or (five and "-5-" not in cur["card"]):
            out[chr_id] = {"chr": chr_id, "name": name, "card": cid}
    return sorted(out.values(), key=lambda h: h["name"])


FACES = HERE / "ui" / "faces"   # every holomem's face, 128 px webp, shipped IN the build


def face_file(chr_id: str) -> Path | None:
    """The holomem's face: the full 256 px game icon once synced, else the copy the build ships
    -- which exists on the very first start, before any sync (otherwise the Settings picker
    shows empty circles on a fresh install)."""
    for p in (DUMPS / "member_icons" / f"{chr_id}.png", FACES / f"{chr_id}.webp"):
        if p.is_file():
            return p
    return None


def icon_file(chr_id: str) -> Path | None:
    """state/app_icon_<chr>.ico from the game's own character icon."""
    out = REPO / "state" / f"app_icon_{chr_id}.ico"
    if out.is_file():
        return out
    src = face_file(chr_id)
    try:
        out.parent.mkdir(exist_ok=True)
        from PIL import Image  # noqa: PLC0415
        with Image.open(src) as im:
            im.convert("RGBA").save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                                 (64, 64), (128, 128), (256, 256)])
        return out
    except Exception:  # noqa: BLE001 -- no icon is a default icon, never a failed start
        return None


def set_window_icon(window, path: Path | None) -> None:
    """Title bar + taskbar icon of the native window (WM_SETICON, big and small)."""
    if path is None:
        return
    try:
        hwnd = int(str(window.native.Handle.ToInt64()))
        u32 = ctypes.windll.user32
        for which, size in ((1, 256), (0, 32)):                     # ICON_BIG, ICON_SMALL
            h = u32.LoadImageW(None, str(path), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            if h:
                u32.SendMessageW(hwnd, 0x80, which, h)               # WM_SETICON
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ the JS side's API

class Api:
    """Every method is callable from the page as `await pywebview.api.<name>(...)`."""

    def __init__(self) -> None:
        # private: pywebview exposes every public attribute to the page, and walking the
        # native window object breaks it
        self._window = None
        self._server: subprocess.Popen | None = None
        self.url: str | None = None
        self.home: str | None = None          # the start screen's own URL (pywebview serves it)
        self._started = False
        self._pending: dict | None = None     # a newer version found while open
        self._named = threading.Event()

    # -- reads
    def boot(self) -> dict:
        s = app_settings.load()
        hm = {h["chr"]: h for h in holomems()}
        pick = hm.get(s["holomem"]) or hm.get(app_settings.NENE) or {}
        return {
            "version": app_version(), "settings": self._public(s),
            "problems": app_settings.validate(s), "hardware": hardware(),
            "holomem": pick, "url": self.url,
            "font": data_uri(REPO / "tools" / "explorer" / "fonts" / "line-seed-sans-xbd-latin.woff2",
                             "font/woff2"),
        }

    def holomem_list(self) -> list[dict]:
        return holomems()

    def holomem_icon(self, chr_id: str) -> str | None:
        p = face_file(chr_id)
        return data_uri(p, "image/png" if p.suffix == ".png" else "image/webp") if p else None

    @staticmethod
    def _public(s: dict) -> dict:
        p = dict(s)
        p["server_token"] = ("•" * 8 + s["server_token"][-4:]) if s["server_token"] else ""
        return p

    # -- writes
    def save_settings(self, new: dict) -> dict:
        s = app_settings.load()
        for k, v in (new or {}).items():
            if k == "server_token" and (not v or str(v).startswith("•")):
                continue                                     # the masked value: unchanged
            if k in app_settings.DEFAULTS:
                s[k] = v
        problems = app_settings.validate(s)
        if problems:
            return {"ok": False, "problems": problems}
        app_settings.save(s)
        if self._started:                    # a Steam ID typed or cleared: the next request uses it
            import client_profile  # noqa: PLC0415
            client_profile.ensure(s["steam_id"])
        if self._window is not None:
            set_window_icon(self._window, icon_file(s["holomem"]))
        return {"ok": True, "restart": self._started}

    def browse_credential(self) -> str | None:
        import webview  # noqa: PLC0415
        start = str(LOCALLOW) if LOCALLOW.is_dir() else str(Path.home())
        got = self._window.create_file_dialog(webview.OPEN_DIALOG, directory=start)
        return got[0] if got else None

    def suggest_credential(self) -> str | None:
        """The one file in the game's LocalLow folder that has a credential's size."""
        hits = app_settings.credential_files(LOCALLOW)
        return str(hits[0]) if len(hits) == 1 else None

    # -- the start sequence
    def check_update(self) -> dict:
        s = app_settings.load()
        local = app_version()
        try:
            req = urllib.request.Request(s["server_url"].rstrip("/") + "/app/version.json",
                                         headers={"Authorization": f"Bearer {s['server_token']}",
                                                  **content_store.version_header()})
            with urllib.request.urlopen(req, timeout=10) as r:
                remote = json.loads(r.read())
            # only the publisher's signature makes the file believable: an unsigned or altered
            # one could otherwise show a false "update required" (the installer already refuses
            # to install it; this keeps the banner honest too)
            import signing  # noqa: PLC0415
            signing.verify(signing.canonical(remote), remote.get("sig", ""))
        except Exception as exc:  # noqa: BLE001 -- offline is not a failed start
            return {"state": "unknown", "local": local, "error": str(exc)}
        return {"state": update_state(local, remote), "local": local,
                "latest": remote.get("latest"), "notes": remote.get("notes", "")}

    def start(self) -> dict:
        """Run steps 3-5 in the background; the page follows `hb.progress(...)` calls."""
        threading.Thread(target=self._start, name="start", daemon=True).start()
        return {"ok": True}

    def _emit(self, **ev) -> None:
        if self._window is not None:
            self._window.evaluate_js(f"window.hb && hb.progress({json.dumps(ev)})")

    def _ask_username(self, s: dict) -> None:
        """An account made before usernames existed picks one now, before anything else: the
        server's admin sees people by it, and their Google name is not kept. Only when the
        server says so -- an older server, or no network, never blocks the start."""
        me = server_me(s)
        if not ("username" in me and not me["username"] and me.get("status") in ("approved", "pending")):
            return
        self._named.clear()
        self._emit(step="username", state="ask")
        self._named.wait()
        self._emit(step="username", state="done")

    def set_username(self, username: str) -> dict:
        s = app_settings.load()
        r = server_set_username(s, username)
        if r["ok"]:
            self._named.set()
        return r

    def _start(self) -> None:
        s = app_settings.load()
        try:
            self._ask_username(s)
            self._emit(step="sync", state="run")
            store = content_store.ContentStore(
                fetch=content_store.http_fetch({"url": s["server_url"], "token": s["server_token"]}),
                cap=int(s["cache_mb"]) * 1024 * 1024)
            last = [0.0]

            def progress(e: dict) -> None:
                if time.time() - last[0] > 0.1 or e["i"] == e["n"]:
                    last[0] = time.time()
                    self._emit(step="sync", state="run", i=e["i"], n=e["n"],
                               bytes=e["bytes"], total=e["total"])
            try:
                r = store.sync(progress)
                self._emit(step="sync", state="done", files=r["files"], bytes=r["bytes"])
            except RuntimeError as exc:
                if "publishes" not in str(exc):
                    raise
                self._emit(step="sync", state="done", note="this PC publishes the data")

            # a first install has no character icons until this sync: the window's icon now
            set_window_icon(self._window, icon_file(s["holomem"]))
            self._emit(step="profile", state="run")
            # every start, not only the first: the Steam ID is read off this PC's Steam, so an
            # app set up before that (or before Steam was signed in) gets it now
            import client_profile  # noqa: PLC0415
            prof = client_profile.ensure(s["steam_id"])
            self._emit(step="profile", state="done", **({} if prof["steam_id"] else {
                "note": "no Steam ID found: sign in to Steam or type it in Settings"}))

            if not all(p.is_file() for p in (REPO / "state" / "collection.json",
                                             REPO / "data" / "owned.json", DUMPS / "board.json")):
                self._account_refresh(s)          # a first start: the app has no account yet
            elif not _collection_current():
                # read by an older app (before 0.1.7 the Member Power-Up was left out): read it
                # again now, not on the user's first Refresh. An account that
                # cannot be read keeps the app usable on what it already has.
                try:
                    self._account_refresh(s)
                except RuntimeError:
                    pass

            self._emit(step="server", state="run")
            self.url = self._start_server(s)
            # the card art every page draws, into the cache while the user looks around: the
            # first visit to Collection must not wait for 300 cards one by one (~80 MB, once)
            threading.Thread(target=store.prefetch, name="prefetch", daemon=True,
                             args=(("tools/explorer/cards_explorer_art/",
                                    "tools/recon/proxy/dumps/card_images/"),)).start()
            self._started = True
            self._emit(step="server", state="done", url=self.url)
            threading.Thread(target=self._watch_updates, name="updates", daemon=True).start()
        except Exception as exc:  # noqa: BLE001 -- shown on the start screen, with a retry
            self._emit(step="error", message=f"{type(exc).__name__}: {exc}")

    def _account_refresh(self, s: dict) -> None:
        """refresh_all --account-only, its step lines streamed to the start screen."""
        self._emit(step="account", state="run", detail="logging in…")
        proc = subprocess.Popen(
            [sys.executable, "-u", str(REPO / "tools" / "recon" / "proxy" / "refresh_all.py"),
             "--account-only"], cwd=str(REPO), env=app_settings.env(s), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tail: list[str] = []
        names = {"account": "reading your cards and boards…", "inventory": "reading your items…",
                 "board": "building your Holomem Boards…", "board_export": "laying out the boards…"}
        for line in proc.stdout:
            tail = (tail + [line.rstrip()])[-600:]
            for step, text in names.items():
                if line.startswith(f"[run ] {step} "):
                    self._emit(step="account", state="run", detail=text)
        proc.wait()
        if not (REPO / "state" / "collection.json").is_file():
            raise RuntimeError("the account could not be read:\n" + "\n".join(account_failure(tail)))
        warn = "DATE_CHANGED" in "\n".join(tail)
        self._emit(step="account", state="done",
                   detail="open the game once today to refresh card parameters" if warn else "read")

    def _start_server(self, s: dict) -> str:
        if self._server and self._server.poll() is None:
            return self.url
        port = free_port()
        LOG.parent.mkdir(exist_ok=True)
        # one previous log kept, never more than ~5 MB each (it is what a user sends with a bug)
        if LOG.is_file() and LOG.stat().st_size > LOG_MAX_BYTES:
            LOG.replace(LOG.with_name(LOG.name + ".1"))
        log = open(LOG, "ab")
        art = REPO / "tools" / "explorer" / "cards_explorer_art"
        self._server = subprocess.Popen(
            [sys.executable, "-u", str(REPO / "tools" / "web" / "server.py"), "--port", str(port),
             "--art-dir", str(art)],
            cwd=str(REPO), env=app_settings.env(s), stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        url = f"http://127.0.0.1:{port}"
        deadline = time.time() + 90
        while time.time() < deadline:
            if self._server.poll() is not None:
                raise RuntimeError(f"the engine stopped at start (see {LOG})")
            try:
                with urllib.request.urlopen(url + "/optimizer", timeout=2):
                    return url
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("the engine did not answer in 90 s")

    # -- a new version while the app is open: the update notice arrives in real time
    def _watch_updates(self) -> None:
        """Ask the server every UPDATE_POLL_S; a newer version is held for the pages
        (`pending_update`, read by hb-shell.js on every page) and shown on the open one."""
        while True:
            time.sleep(UPDATE_POLL_S)
            got = self.check_update()
            if got.get("state") in ("available", "required") and got != self._pending:
                self._pending = got
                if self._window is not None:
                    try:
                        self._window.evaluate_js(f"window.hbUpdate && hbUpdate({json.dumps(got)})")
                    except Exception:  # noqa: BLE001 -- a page mid-load picks it up itself
                        pass

    def pending_update(self) -> dict | None:
        """The newer version found while the app is open, for the update banner."""
        return self._pending if self._pending and self.can_update() else None

    def can_update(self) -> bool:
        """Installed by holobuilder-setup.exe (it leaves a copy of itself next to app/)."""
        return (REPO.parent / "holobuilder-setup.exe").is_file()

    def update_now(self) -> dict:
        """Hand over to the installer in update mode: it waits for this process to end, swaps
        the code (keeping this PC's data) and reopens the app."""
        setup = REPO.parent / "holobuilder-setup.exe"
        if not setup.is_file():
            return {"ok": False, "error": "this copy was not installed by holobuilder-setup.exe"}
        subprocess.Popen([str(setup), "--update", "--pid", str(os.getpid()), "--dir", str(REPO.parent)],
                         cwd=str(REPO.parent), env=clean_env(reset=True),
                         creationflags=0x00000008 | 0x00000200)          # DETACHED | NEW_GROUP
        self.stop()
        if self._window is not None:
            self._window.destroy()
        return {"ok": True}

    def open_app(self) -> None:
        if self.url and self._window is not None:
            self._window.load_url(self.url + "/optimizer")

    def open_settings(self) -> None:
        if self._window is not None:
            self._window.load_url((self.home or UI.as_uri()).split("#")[0] + "#settings")

    def diagnostics(self) -> str:
        running = bool(self._server and self._server.poll() is None)
        return diagnostics(app_settings.load(), engine="running" if running else "stopped")

    def restart_engine(self) -> dict:
        """After a Settings change: the server reads them at start."""
        self.stop()
        self.url = self._start_server(app_settings.load())
        return {"url": self.url}

    def stop(self) -> None:
        if self._server and self._server.poll() is None:
            self._server.terminate()
            try:
                self._server.wait(5)
            except subprocess.TimeoutExpired:
                self._server.kill()


LOG_MAX_BYTES = 5_000_000


def main() -> int:
    content_store.install_user_agent()
    import webview  # noqa: PLC0415

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # own taskbar entry
    except (AttributeError, OSError):
        pass
    api = Api()
    window = webview.create_window(
        "holobuilder", url=str(UI), js_api=api, width=1440, height=900, min_size=(1200, 760),
        background_color="#0E0D20", maximized=True, text_select=True)
    api._window = window
    window.events.shown += lambda: set_window_icon(window, icon_file(app_settings.load()["holomem"]))
    window.events.closed += api.stop

    def loaded() -> None:
        if api.home is None:
            api.home = window.get_current_url()
    window.events.loaded += loaded
    webview.start(private_mode=False, storage_path=str(REPO / "state" / "webview"))
    api.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
