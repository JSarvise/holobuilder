"""The desktop app's Settings (docs/APP-ESCRITORIO.md §5): what they are, where they live, and
how they reach the processes the app starts.

state/app_settings.json (gitignored, this PC only):

  credential_path  the game's own credential file (LocalLow ...\\hololive-Dreams\\<hex>). Read at
                   each refresh, never copied anywhere, never sent to the server.
  region           us | jp | as -- the API host the account lives on
  steam_id         the account's SteamID64; empty = read off this PC's Steam (client_profile)
  server_url       https://api.holobuilder.dev
  server_token     the bearer token (also written to state/server_api.json, which the content
                   store and the tracker pull read)
  workers          0 = every logical core (the measured best, SEARCH-STRATEGY.md §4), else N
  cache_mb         the art cache cap (Live2D, chibis, card art); 0 = unlimited (default)
  holomem          chr-xxxxx whose face is the app's icon and start screen (default Nene)

Everything the server and the refresh need is passed as environment variables (`env()`), so
none of the existing tools learns about this file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATE = REPO / "state"
SETTINGS = STATE / "app_settings.json"
SERVER_API = STATE / "server_api.json"

REGION_HOSTS = {"us": "us.game-hololive-dreams.com", "jp": "jp.game-hololive-dreams.com",
                "as": "as.game-hololive-dreams.com"}
# The game's own folder on this PC. Its account file is the one file there with a 32-character
# name and a credential's size: 255 bytes, plus a UTF-8 BOM.
LOCALLOW = (Path(os.environ.get("USERPROFILE", "~")).expanduser() / "AppData" / "LocalLow"
            / "QualiArts, Inc_" / "hololive-Dreams")
NENE = "chr-00031"
DEFAULTS = {
    "credential_path": "", "region": "us", "steam_id": "",
    # the holobuilder server (HB_SERVER_URL for a self-hosted one; the installer saves its own)
    "server_url": os.environ.get("HB_SERVER_URL") or "https://api.holobuilder.dev", "server_token": "",
    "workers": 0, "cache_mb": 0, "holomem": NENE,
}


def load(path: Path = SETTINGS) -> dict:
    try:
        saved = json.loads(path.read_text("utf8"))
    except (OSError, ValueError):
        saved = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in saved.items() if k in DEFAULTS})
    if not out["server_token"]:                     # a PC set up before Settings existed
        try:
            out["server_token"] = json.loads(SERVER_API.read_text("utf8")).get("token", "")
        except (OSError, ValueError):
            pass
    return out


def credential_files(folder: Path = LOCALLOW) -> list[Path]:
    """The files in the game's LocalLow folder shaped like its account file ([] without it)."""
    try:
        return sorted(p for p in folder.iterdir()
                      if p.is_file() and len(p.name) == 32 and 200 <= p.stat().st_size <= 303)
    except OSError:
        return []


def validate(s: dict) -> dict:
    """{field: problem} for what cannot work; {} when the app can start."""
    bad = {}
    p = s.get("credential_path") or ""
    if not p:
        bad["credential_path"] = "missing"
    elif not Path(p).is_file():
        bad["credential_path"] = "no such file"
    else:
        n = Path(p).stat().st_size
        if not 200 <= n - (3 if Path(p).read_bytes()[:3] == b"\xef\xbb\xbf" else 0) <= 300:
            bad["credential_path"] = f"not a credential file ({n} bytes)"
    if s.get("region") not in REGION_HOSTS:
        bad["region"] = "must be us, jp or as"
    if not str(s.get("server_url", "")).startswith("https://"):
        bad["server_url"] = "must be https://"
    if len(str(s.get("server_token", ""))) < 32:
        bad["server_token"] = "missing"
    sid = str(s.get("steam_id") or "").strip()
    if sid and not (len(sid) == 17 and sid.startswith("7656119") and sid.isdigit()):
        bad["steam_id"] = "17 digits starting 7656119"
    try:
        if int(s.get("workers", 0)) < 0 or int(s.get("cache_mb", 0)) < 0:
            raise ValueError
    except (TypeError, ValueError):
        bad["workers"] = "must be a whole number"
    return bad


def save(s: dict, path: Path = SETTINGS) -> dict:
    clean = {k: s.get(k, DEFAULTS[k]) for k in DEFAULTS}
    clean["workers"] = int(clean["workers"] or 0)
    clean["cache_mb"] = int(clean["cache_mb"] or 0)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(clean, indent=1), "utf8")
    if clean["server_token"]:
        SERVER_API.write_text(json.dumps({"url": clean["server_url"], "token": clean["server_token"],
                                          "_warning": "Bearer token for the holobuilder API."},
                                         indent=1), "utf8")
    return clean


def env(s: dict, base: dict | None = None) -> dict:
    """The environment for the server and everything it starts."""
    e = dict(os.environ if base is None else base)
    e.update({
        "HB_APP_MODE": "1",                  # server: the refresh button is --account-only
        "HB_NO_GAME": "1",                   # never read a game install, even if there is one
        "HB_CREDENTIAL_FILE": s["credential_path"],
        "HB_ACCOUNT_HOST": REGION_HOSTS.get(s["region"], REGION_HOSTS["us"]),
        "HB_CONTENT_CACHE_MB": str(int(s["cache_mb"])),
        "PYTHONIOENCODING": "utf-8",
    })
    if int(s["workers"]) > 0:
        e["HBUILDER_WORKERS"] = e["HBUILDER_WEB_WORKERS"] = str(int(s["workers"]))
    return e
