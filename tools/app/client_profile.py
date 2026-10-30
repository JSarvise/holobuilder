"""Write state/client_profile.json from THIS PC's hardware, for a PC without a captured one.

    python tools/app/client_profile.py            # show what would be written
    python tools/app/client_profile.py --write    # write it (never over a captured profile)
    python tools/app/client_profile.py --write --steam-id 7656...   # instead of Steam's own

The profile is the identity the game's own client sends on every call (account_snapshot.py
`client_headers`): device model, CPU, GPU, RAM, screen. A PC may have one captured from the
real client; on a PC without one, the honest identity is that PC's own hardware, read the way
Unity's SystemInfo reports it:

  x-app-device-name   "<SystemProductName> (<SystemManufacturer>)"  (BIOS registry key)
  x-app-cpu / processor-model   ProcessorNameString, kept verbatim (trailing space and all)
  x-app-gpu, vendor id (decimal), memory (MB)   the display adapter's registry class key
  x-app-system-memory-size   physical RAM in MB (GlobalMemoryStatusEx)
  screen width/height/dpi    the primary display, DPI-aware

Formats match a captured client profile (e.g. `B650M-A (Example Vendor Co., Ltd.)`,
vendor id `4318` = 0x10DE, `Windows 11  (10.0.26200)` with two spaces). The VRAM is read off the
game's own Player.log when the game has run on this PC (`VRAM: 8155 MB` where the registry says
8192). Against a capture of the real client every header this writes is identical, RAM
included: the client reports the OS's current physical-RAM figure.

The Steam ID (`x-app-steam-id`, the account's SteamID64: 17 digits, 7656119...) is sent by the
client on every call after the first two, so a profile claiming Steam without it is a request
no real client makes. It is read off this PC's Steam (`steam_id()`): the running account, else
the most recent one of loginusers.vdf -- both give the id the real client sends.
Typed in Settings it wins. Windows only: the game is.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROFILE = REPO / "state" / "client_profile.json"

FIXED = {
    "user-agent": "grpc-dotnet/2.76.0 (Mono Unity; CLR 4.0.30319.17020; netstandard2.1; windows; x64)",
    "x-app-lang-type": "eng",
    "x-app-os-type": "Windows",
    "x-app-store-type": "Steam",
    "x-app-bundle-id": "game.qualiarts.hololive.dreams.com",
    "x-app-operating-system-family": "Windows",
    "x-app-device-type": "Desktop",
    "x-app-graphics-device-type": "Direct3D11",
    "x-app-graphics-device-version": "Direct3D 11.0 [level 11.1]",
    "x-app-graphics-shader-level": "50",
}
LOCALLOW = Path(os.environ.get("USERPROFILE", "~")).expanduser() / "AppData" / "LocalLow"     / "QualiArts, Inc_" / "hololive-Dreams"
STEAM_BASE = 76561197960265728            # SteamID64 of account id 0 (individual, public universe)
_GPU_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def os_version(release: str, build: int) -> str:
    """Unity's spelling: 'Windows 11  (10.0.26200)'. Build 22000+ is Windows 11 whatever
    `platform.release()` says (it says '10' on some Pythons)."""
    name = "11" if build >= 22000 else release
    return f"Windows {name}  (10.0.{build})"


def device_name(product: str | None, maker: str | None) -> str:
    product, maker = (product or "").strip(), (maker or "").strip()
    return f"{product} ({maker})" if product and maker else product or maker or "System Product Name"


def vendor_id(matching_device_id: str | None) -> str:
    """'PCI\\VEN_10DE&DEV_2C02...' -> '4318'."""
    s = (matching_device_id or "").upper()
    i = s.find("VEN_")
    try:
        return str(int(s[i + 4:i + 8], 16)) if i >= 0 else "0"
    except ValueError:
        return "0"


def cpu_name(raw: str | None) -> str:
    """The registry pads ProcessorNameString with spaces; Unity's string keeps exactly one
    (e.g. registry 'AMD Ryzen 5 7600 6-Core Processor' + 11 spaces, captured
    profile the same name + 1 space)."""
    raw = raw or ""
    name = raw.rstrip()
    return name + " " if len(name) < len(raw) else name


def steam_id64(account_id) -> str | None:
    """Steam's 32-bit account id (what the registry holds) -> the SteamID64 the game sends."""
    try:
        n = int(account_id)
    except (TypeError, ValueError):
        return None
    return str(STEAM_BASE + n) if 0 < n < 1 << 32 else None


def valid_steam_id(s) -> bool:
    return bool(re.fullmatch(r"7656119\d{10}", str(s or "").strip()))


def most_recent_login(vdf: str) -> str | None:
    """loginusers.vdf -> the account marked MostRecent, or the only one; None when several
    accounts are listed and none is marked (a guess between two people is not made)."""
    users = re.findall(r'"(7656119\d{10})"\s*\{([^}]*)\}', vdf)
    recent = [sid for sid, body in users if re.search(r'"mostrecent"\s*"1"', body, re.I)]
    if len(recent) == 1:
        return recent[0]
    return users[0][0] if len(users) == 1 else None


def player_log_vram(text: str) -> int | None:
    """Unity's own VRAM figure from Player.log ('    VRAM:     8155 MB'), in MB."""
    m = re.search(r"^\s*VRAM:\s*(\d+)\s*MB", text, re.M)
    return int(m.group(1)) if m else None


def pick_gpu(adapters: list[dict]) -> dict:
    """The adapter with the most memory: on a laptop the discrete one, not the iGPU."""
    return max(adapters, key=lambda a: a.get("memory", 0)) if adapters else {}


# ------------------------------------------------------------------ Windows probes

def _reg(root, path: str, name: str):
    import winreg  # noqa: PLC0415
    try:
        with winreg.OpenKey(root, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _adapters() -> list[dict]:
    import winreg  # noqa: PLC0415
    out = []
    for i in range(32):
        sub = f"{_GPU_CLASS}\\{i:04d}"
        desc = _reg(winreg.HKEY_LOCAL_MACHINE, sub, "DriverDesc")
        if desc is None:
            continue
        mem = _reg(winreg.HKEY_LOCAL_MACHINE, sub, "HardwareInformation.qwMemorySize")
        if mem is None:
            raw = _reg(winreg.HKEY_LOCAL_MACHINE, sub, "HardwareInformation.MemorySize")
            mem = int.from_bytes(raw, "little") if isinstance(raw, bytes) else raw
        out.append({"name": desc, "memory": int(mem or 0),
                    "vendor": vendor_id(_reg(winreg.HKEY_LOCAL_MACHINE, sub, "MatchingDeviceId"))})
    return out


def _ram_mb() -> int:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(st)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    return st.ullTotalPhys // (1024 * 1024)


def _screen() -> tuple[int, int, int]:
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2
    except (AttributeError, OSError):
        pass
    try:
        dpi = user32.GetDpiForSystem()
    except AttributeError:
        dpi = 96
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1), dpi


def steam_id() -> str | None:
    """This PC's Steam account: the running one (registry ActiveUser), else loginusers.vdf."""
    import winreg  # noqa: PLC0415
    sid = steam_id64(_reg(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess", "ActiveUser"))
    if sid:
        return sid
    root = _reg(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath")
    try:
        return most_recent_login((Path(root) / "config" / "loginusers.vdf").read_text("utf8", "replace"))             if root else None
    except OSError:
        return None


def _vram_mb() -> int | None:
    for name in ("Player.log", "Player-prev.log"):
        try:
            v = player_log_vram((LOCALLOW / name).read_text("utf8", "replace"))
        except OSError:
            continue
        if v:
            return v
    return None


def probe() -> dict:
    """This PC's hardware, as raw facts."""
    import winreg  # noqa: PLC0415
    bios = r"HARDWARE\DESCRIPTION\System\BIOS"
    cpu = _reg(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
               "ProcessorNameString") or platform.processor()
    w, h, dpi = _screen()
    return {
        "product": _reg(winreg.HKEY_LOCAL_MACHINE, bios, "SystemProductName"),
        "maker": _reg(winreg.HKEY_LOCAL_MACHINE, bios, "SystemManufacturer"),
        "cpu": cpu, "threads": os.cpu_count() or 1, "ram_mb": _ram_mb(),
        "gpu": pick_gpu(_adapters()), "vram_mb": _vram_mb(), "screen": [w, h, dpi],
        "build": sys.getwindowsversion().build, "release": platform.release(),
    }


def headers(facts: dict, *, version: str, steam_id: str | None = None) -> dict:
    gpu = facts.get("gpu") or {}
    w, h, dpi = facts["screen"]
    out = dict(FIXED)
    out.update({
        "x-app-version": version,
        "x-app-device-name": device_name(facts.get("product"), facts.get("maker")),
        "x-app-os-version": os_version(facts["release"], facts["build"]),
        "x-app-cpu": cpu_name(facts["cpu"]),
        "x-app-gpu": gpu.get("name", "Microsoft Basic Render Driver"),
        "x-app-graphics-device-vendor-id": gpu.get("vendor", "0"),
        "x-app-graphics-device-memory-size": str(facts.get("vram_mb") or gpu.get("memory", 0) // (1024 * 1024)),
        "x-app-processor-model": cpu_name(facts["cpu"]),
        "x-app-processor-count": str(facts["threads"]),
        "x-app-system-memory-size": str(facts["ram_mb"]),
        "x-app-screen-dpi": str(dpi),
        "x-app-screen-height": str(h),
        "x-app-screen-width": str(w),
    })
    if steam_id:
        out["x-app-steam-id"] = steam_id
    return out


GENERATED = "generated from this PC's hardware (tools/app/client_profile.py)"


def ensure(steam_override: str | None = None, *, path: Path = PROFILE, facts: dict | None = None,
           find_steam=None, version: str | None = None) -> dict:
    """Make state/client_profile.json this PC's, at every start of the app.

    A CAPTURED profile (the real client's own headers) is never touched. A generated one is
    rewritten whenever what it would say changed -- a Steam ID found or typed since, a new GPU,
    the VRAM read off a Player.log that appeared -- so an app installed before the Steam ID was
    read automatically gets it at its next start. -> {"steam_id": "typed" | "found" | None,
    "written": bool, "captured": bool}."""
    try:
        old = json.loads(path.read_text("utf8"))
    except (OSError, ValueError):
        old = None
    if old is not None and old.get("source") != GENERATED:
        return {"captured": True, "written": False,
                "steam_id": "found" if old.get("headers", {}).get("x-app-steam-id") else None}
    typed = (steam_override or "").strip()
    sid, how = (typed, "typed") if valid_steam_id(typed) else (None, None)
    if not sid:
        sid = (find_steam or steam_id)()
        how = "found" if sid else None
    if version is None:
        sys.path.insert(0, str(REPO / "tools" / "recon" / "proxy"))
        import account_snapshot as A  # noqa: PLC0415
        version = A.app_version() or dict(A.GENERIC_HEADERS)["x-app-version"]
    hdrs = headers(facts or probe(), version=version, steam_id=sid)
    if old is not None and old.get("headers") == hdrs:
        return {"captured": False, "written": False, "steam_id": how}
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({
        "headers": hdrs, "source": GENERATED,
        "_warning": "This PC's hardware identity" + (" and Steam ID" if sid else "")
                    + ". state/ is gitignored."}, indent=1), "utf8")
    return {"captured": False, "written": True, "steam_id": how}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true",
                    help="write state/client_profile.json (a captured profile is never replaced)")
    ap.add_argument("--steam-id", default=None, help="the account's SteamID64; read off Steam when left out")
    args = ap.parse_args(argv)
    if args.write:
        r = ensure(args.steam_id)
        print(f"{PROFILE}: " + ("captured profile, left as it is" if r["captured"]
                               else "written" if r["written"] else "already current")
              + f"; Steam ID {r['steam_id'] or 'NOT FOUND'}")
        return 0
    sid = args.steam_id if valid_steam_id(args.steam_id) else steam_id()
    sys.path.insert(0, str(REPO / "tools" / "recon" / "proxy"))
    import account_snapshot as A  # noqa: PLC0415
    hdrs = headers(probe(), version=A.app_version() or dict(A.GENERIC_HEADERS)["x-app-version"],
                   steam_id=sid)
    for k, v in hdrs.items():
        print(f"  {k}: " + ("<steam id found>" if k == "x-app-steam-id" else v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
