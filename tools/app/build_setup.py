"""Build holobuilder-setup.exe, the one file that installs the app on another PC.

    proxyenv/Scripts/python tools/app/build_setup.py              # build/holobuilder-setup.exe
    proxyenv/Scripts/python tools/app/build_setup.py --publish    # ...and put a copy on the server

PyInstaller, one file, no console. Unlike the app (build_app.py), the installer starts no Python
helpers of its own, so freezing it costs nothing. Inside: installer.py, the shared look
(ui/app.css, ui/tour.js, ui/setup.html, the six previews) and _baked.py with the server's address
and Google's public installed-app client: the exe holds no secret and is published.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
WORK = REPO / "build" / "_setup"
EXE = REPO / "build" / "holobuilder-setup.exe"


def build() -> Path:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    # NO access key inside: the exe is public. Only the Google OAuth
    # DESKTOP client, which Google documents as not confidential (PKCE + loopback protect it).
    g = json.loads((REPO / "state" / "google_oauth.json").read_text("utf8"))
    (WORK / "_baked.py").write_text(
        "# written by build_setup.py -- public by design: Google's installed-app client and the\n"
        "# server it installs from (HB_SERVER_URL at build time, else the official one)\n"
        f"GOOGLE_CLIENT_ID = {g['client_id']!r}\nGOOGLE_CLIENT_SECRET = {g['client_secret']!r}\n"
        f"SERVER_URL = {os.environ.get('HB_SERVER_URL', 'https://api.holobuilder.dev').rstrip('/')!r}\n", "utf8")
    sys.path.insert(0, str(HERE))
    import launcher  # noqa: PLC0415
    icon = launcher.icon_file("chr-00031")                     # Nene, the app's default face
    ui = WORK / "ui"
    shutil.copytree(HERE / "ui", ui, ignore=shutil.ignore_patterns("index.html"))
    if icon:
        shutil.copy2(icon, ui / "app.ico")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
           "--name", "holobuilder-setup", "--distpath", str(EXE.parent), "--workpath", str(WORK / "pyi"),
           "--specpath", str(WORK), "--paths", str(WORK), "--hidden-import", "_baked",
           "--add-data", f"{ui}{';'}ui"]
    if icon:
        cmd += ["--icon", str(icon)]
    subprocess.run(cmd + [str(HERE / "installer.py")], check=True, cwd=str(WORK))
    print(f"built {EXE} ({EXE.stat().st_size / 1e6:.0f} MB)")
    return EXE


def publish(exe: Path) -> None:
    sys.path.insert(0, str(HERE))
    import publish as pub  # noqa: PLC0415
    subprocess.run(["scp", "-q", str(exe), f"{pub.ssh_target()}:/srv/holobuilder/app/{exe.name}"], check=True)
    print(f"published {exe.name} (behind the access key, like everything under /app/)")


def sums(files: list[Path], out_dir: Path) -> tuple[Path, Path]:
    """SHA256SUMS (the `sha256sum` format) next to the release files, and SHA256SUMS.sig, its
    Ed25519 signature with the release key: attach both to the GitHub release. Anyone can then
    check a download with `signing.py --verify-sums` (or just its hash with Get-FileHash)."""
    import hashlib  # noqa: PLC0415
    sys.path.insert(0, str(HERE))
    import signing  # noqa: PLC0415
    text = "".join(f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}\n" for f in files)
    out, sig = out_dir / "SHA256SUMS", out_dir / "SHA256SUMS.sig"
    out.write_bytes(text.encode())
    sig.write_bytes((signing.sign(text.encode()) + "\n").encode())
    signing.verify(text.encode(), sig.read_text("utf8").strip())
    return out, sig


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--publish", action="store_true")
    args = ap.parse_args(argv)
    exe = build()
    for p in sums([exe], exe.parent):
        print(f"wrote {p}")
    if args.publish:
        publish(exe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
