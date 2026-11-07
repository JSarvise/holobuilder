"""Start the local web server: `python tools/web/server.py [--port N] [--art-dir DIR]`.

The server is the `hbweb` package beside this file: `hbweb/router.py` holds the request
handler, the route tables and `main`; one module per feature holds its routes.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info < (3, 12):  # the oldest Python holobuilder runs on (pyproject.toml)
    raise SystemExit(f"holobuilder needs Python 3.12 or newer; this is {sys.version.split()[0]}")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))  # this checkout's hbuilder first

from hbweb.router import BIND_HOST, DEFAULT_PORT, Handler, ThreadingServer, build_parser, main  # noqa: E402

__all__ = ["BIND_HOST", "DEFAULT_PORT", "Handler", "ThreadingServer", "build_parser", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
