"""Assemble the browser demo's assets (all gitignored / generated).

Versioned by hand: `index.html`, `demo.js`, `make_board.py`, this file, `README.md`.
Everything else is fetched or generated here so ~18 MB of WASM binaries + account-derived
board data never enter git:

  1. the two WASM runtimes  (npm: `pyodide`, `highs`)  -> copied in
  2. `hbuilder.zip`         (src/hbuilder, stdlib-only) -> zipped in
  3. `data/`                (cards.json + config.json)  -> copied from the repo
  4. `board.lp` + `board.json`                          -> `make_board.py` (account-derived)

Run it (any Python) from anywhere; needs `node`/`npm` on PATH and the proxyenv interpreter
for step 4:

    python tools/web/wasmdemo/setup.py

Then serve the folder (`python -m http.server 8791`) and open index.html. See README.md.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PROXY_PY = REPO / "tools" / "recon" / "proxy" / "proxyenv" / "Scripts" / "python.exe"

PYODIDE_FILES = ["pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm",
                 "python_stdlib.zip", "pyodide-lock.json"]
HIGHS_FILES = ["build/highs.js", "build/highs.wasm"]


def step(msg):
    print(f"[setup] {msg}", flush=True)


def npm_assets():
    step("installing WASM runtimes via npm (pyodide, highs)…")
    tmp = Path(tempfile.mkdtemp(prefix="wasmdemo-npm-"))
    try:
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            sys.exit("[setup] ERROR: npm not found on PATH — install Node.js first.")
        subprocess.run([npm, "install", "--prefix", str(tmp), "pyodide", "highs"],
                       check=True, capture_output=True, text=True)
        nm = tmp / "node_modules"
        for f in PYODIDE_FILES:
            shutil.copy2(nm / "pyodide" / f, HERE / Path(f).name)
        for f in HIGHS_FILES:
            shutil.copy2(nm / "highs" / f, HERE / Path(f).name)
        step(f"copied {len(PYODIDE_FILES) + len(HIGHS_FILES)} runtime files")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def zip_hbuilder():
    step("zipping src/hbuilder …")
    root = REPO / "src" / "hbuilder"
    out = HERE / "hbuilder.zip"
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for py in root.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            z.write(py, py.relative_to(root.parent))  # arcname: hbuilder/...
            n += 1
    step(f"hbuilder.zip: {n} modules")


def copy_data():
    step("copying game data (cards.json, config.json) …")
    (HERE / "data").mkdir(exist_ok=True)
    shutil.copy2(REPO / "data" / "cards.json", HERE / "data" / "cards.json")
    shutil.copy2(REPO / "config.json", HERE / "data" / "config.json")


def make_board():
    step("generating board model (make_board.py, proxyenv) …")
    if not PROXY_PY.exists():
        sys.exit(f"[setup] ERROR: proxyenv interpreter not found at {PROXY_PY}")
    subprocess.run([str(PROXY_PY), str(HERE / "make_board.py")], check=True, cwd=str(REPO))


def main():
    npm_assets()
    zip_hbuilder()
    copy_data()
    make_board()
    step("done — serve this folder (python -m http.server 8791) and open index.html")


if __name__ == "__main__":
    main()
