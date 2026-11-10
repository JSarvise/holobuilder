"""The local web server: stdlib `http.server` on 127.0.0.1, serving the pages under
`static/` and their JSON API.

* **stdlib only, bound to loopback.** The page is for local use, and `127.0.0.1` enforces
  that, so the bind address is a module constant, not a flag.
* **One table per method.** A request's path is looked up in the exact-path table, then
  in the prefix table (the asset routes, whose remainder is the file name); `GET` also
  serves the pages' modules by extension first and the pages themselves last.
* **Searches are jobs** (`jobs.py`): the Optimizer and the Event page start one and read
  its progress off `GET /events/<id>`.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hbuilder import paths
from hbweb import (
    admin,
    board,
    boardplan,
    collection,
    config,
    event,
    gacha,
    inventory,
    jobs,
    park,
    search,
    songs,
    static,
)

BIND_HOST = "127.0.0.1"  # loopback: the page is served to this machine only.
DEFAULT_PORT = 8731

Route = Callable[["Handler"], None]
PrefixRoute = Callable[["Handler", str], None]


def serve_server(h: Handler) -> None:
    """Which checkout this server runs from, and its process id: `run_local.py start` stops an
    earlier server of its own folder that still holds the port, and nothing else."""
    h.reply_json(200, {"repo": str(paths.REPO), "pid": os.getpid()})


GET_ROUTES: dict[str, Route] = {
    "/": static.redirect_home,
    "/api/server": serve_server,
    "/api/collection": collection.serve_collection,
    "/api/board": board.serve_board,
    "/api/meta": search.serve_meta,
    "/api/songs": songs.serve_songs,
    "/api/ranks": search.serve_ranks,
    "/api/event": event.serve_event,
    "/api/event/songs": event.serve_event_songs,
    "/api/event/cutoffs": event.serve_event_cutoffs,
    "/api/event/top": event.serve_event_top,
    "/api/event/tracker": event.serve_event_tracker,
    "/api/event/history": event.serve_event_history,
    "/api/admin/me": admin.serve_me,
    "/api/admin/users": admin.serve_users,
    "/api/park": park.serve_park,
    "/api/resources": inventory.serve_resources,
    "/api/gacha": gacha.serve_gacha,
    "/api/live2d/index": static.serve_live2d_index,
    "/live2d/core.js": static.serve_live2d_core,
    "/api/chibi/index": static.serve_chibi_index,
    "/font/line-seed-sans-xbd-latin.woff2": static.serve_font,
}
# Checked after the exact paths, in this order; the handler gets the rest of the path.
GET_PREFIXES: tuple[tuple[str, PrefixRoute], ...] = (
    ("/gachaart/", gacha.serve_gachaart),
    ("/live2d/", static.serve_live2d_file),
    ("/chibi/", static.serve_chibi_file),
    ("/park/", park.serve_park_image),
    ("/cardart/", static.serve_cardart),
    ("/jacket/", static.serve_jacket),
    ("/eventart/", static.serve_eventart),
    ("/chricon/", static.serve_chricon),
    ("/itemicon/", static.serve_itemicon),
    ("/icon/", static.serve_icon),
    ("/chrome/", static.serve_chrome),
    ("/ui/", static.serve_ui_sprite),
    ("/events/", jobs.serve_events),
)
POST_ROUTES: dict[str, Route] = {
    "/api/event/run": event.start_event_run,
    "/api/event/points": event.event_points,
    "/api/search": search.start_search,
    "/api/board-plan-account": boardplan.serve_board_plan_account,
    "/api/card-states": collection.serve_card_states,
    "/api/board-doc": boardplan.serve_board_doc,
    "/api/collection": collection.save_collection,
    "/api/admin/user": admin.admin_user,
    "/api/refresh": admin.refresh_route,
    "/api/park/scan": admin.park_scan_route,
    "/api/row-detail": search.row_detail_route,
    "/api/export-team": search.export_team_route,
    "/api/timeline": search.serve_timeline,
    "/api/gacha/sync": gacha.gacha_sync,
    "/api/gacha/export": gacha.gacha_export,
    "/api/compare": search.compare,
}
POST_PREFIXES: tuple[tuple[str, PrefixRoute], ...] = (("/cancel/", jobs.cancel),)


def _dispatch(
    h: Handler, path: str, routes: dict[str, Route], prefixes: tuple[tuple[str, PrefixRoute], ...]
) -> bool:
    """Run the route for `path`; False when none matches."""
    route = routes.get(path)
    if route is not None:
        route(h)
        return True
    for prefix, prefix_route in prefixes:
        if path.startswith(prefix):
            prefix_route(h, path[len(prefix) :])
            return True
    return False


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "hbuilder-web"
    art_dir: Path = config.DEFAULT_ART_DIR

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- replies --------------------------------------------------------------
    def reply(self, code: int, body: bytes, ctype: str, *, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def reply_json(self, code: int, obj: Any) -> None:
        self.reply(code, json.dumps(obj).encode("utf-8"), "application/json")

    def reply_error(self, exc: Exception) -> None:
        """An unexpected failure as a JSON 500 (never an HTML error page), with the
        traceback on the server's console. Call it from inside the `except` block."""
        import traceback  # noqa: PLC0415

        print(traceback.format_exc(), flush=True)
        self.reply_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    # -- request --------------------------------------------------------------
    def read_json_body(self) -> Any:
        """The request body as JSON, `{}` when empty; raises `json.JSONDecodeError`."""
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def body_or_empty(self) -> Any:
        """The request body as JSON, `{}` when empty or not JSON."""
        try:
            return self.read_json_body()
        except json.JSONDecodeError:
            return {}

    def query_width(self) -> int:
        """The `?w=N` width from the request URL (0 if absent/invalid), clamped to
        a sane ceiling so a crafted value cannot ask for a giant re-encode."""
        if "?" not in self.path:
            return 0
        for part in self.path.split("?", 1)[1].split("&"):
            if part.startswith("w="):
                try:
                    return max(0, min(4096, int(part[2:])))
                except ValueError:
                    return 0
        return 0

    def send_response(self, code: int, message: str | None = None) -> None:
        self._answered = True
        super().send_response(code, message)

    def _answer_or_error(self, route: Callable[[], None]) -> None:
        """Run `route`; an exception it lets through before anything was sent becomes a
        JSON 500 naming it, so the page shows the failure instead of a dropped connection.
        Once an answer has started, the exception goes on to `handle_error`."""
        self._answered = False
        try:
            route()
        except Exception as exc:  # noqa: BLE001 — every request gets an answer
            if self._answered:
                raise
            self.reply_error(exc)

    # -- routes ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._answer_or_error(self._get)

    def do_POST(self) -> None:  # noqa: N802
        self._answer_or_error(self._post)

    def _get(self) -> None:
        path = self.path.split("?", 1)[0]
        if static.is_module_path(path):
            static.serve_static(self, path.lstrip("/"))
        elif _dispatch(self, path, GET_ROUTES, GET_PREFIXES):
            pass
        elif static.is_page(path):
            static.serve_static(self, f"{path[1:]}.html")
        else:
            self.reply(404, b"not found", "text/plain; charset=utf-8")

    def _post(self) -> None:
        path = self.path.split("?", 1)[0]
        if not _dispatch(self, path, POST_ROUTES, POST_PREFIXES):
            self.reply(404, b"not found", "text/plain; charset=utf-8")


class ThreadingServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    # ⛔ FALSE on Windows, and there this is a correctness fix rather than a
    # preference: `SO_REUSEADDR` means **a second process may HIJACK a port
    # another process is already listening on**. Both bind, both print
    # "serving on ...", and requests go to whichever the stack picks --
    # in practice the older one, which has none of the new routes. With it
    # False, a second launch FAILS on bind and says so.
    # On POSIX it only means "reuse a socket in TIME_WAIT" (a second listener
    # still fails), so a restart is not refused for a minute after a stop.
    allow_reuse_address = sys.platform != "win32"

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A browser that drops its connection (a closed tab, a reload, an
        EventSource closed once its search ended -- WinError 10053/10054) is not
        a server fault: one line instead of socketserver's traceback. Anything
        else keeps the full traceback."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            print(f"{client_address[0]} closed the connection ({type(exc).__name__})", flush=True)
            return
        super().handle_error(request, client_address)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools/web/server.py",
        description="Local Solve -> Result view. Binds 127.0.0.1 only.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--art-dir",
        default=str(config.DEFAULT_ART_DIR),
        help=(
            "Directory holding <card-id>.webp portraits. Default is "
            "tools/explorer/cards_explorer_art in this checkout; point it at "
            "another checkout's copy rather than copying the files."
        ),
    )
    return parser


def _enable_kernels() -> None:
    """The compiled note walk (tools/web/kernel): bit-identical to the Python path and
    ~4x faster per song score. `enable()` sets it for this process and, via the
    environment, for every worker spawned from here; it returns False and changes
    nothing if there is no compiler. HBUILDER_NO_KERNEL=1 opts out."""
    if os.environ.get("HBUILDER_NO_KERNEL"):
        return
    sys.path.insert(0, str(paths.WEB_DIR / "kernel"))
    import notewalk  # noqa: PLC0415

    notewalk.enable()
    # ...and the compiled Connect socket assignment and team board assembly, the same
    # contract (`board_plan.CONNECT_KERNEL`, `board_plan.BOARD_KERNEL`): ~280 -> ~40 us
    # per team below the song cap, ~6 -> ~0.4 ms per maxed team over it.
    import connect  # noqa: PLC0415

    connect.enable()
    # ...and the cooldown screen (an estimate of a team's frequency-count variants, for
    # ordering them: the search's count-aware value and the refinement's screens)
    import cdvar  # noqa: PLC0415

    cdvar.enable()


def _start_cutoffs_poller() -> None:
    """ON by default: the border curves and the heatmap need a dense series; it idles an
    hour when no chapter is open. HBUILDER_CUTOFFS_POLL_MIN=0 turns it off, any other
    value sets the interval."""
    import cutoffs_poller  # noqa: PLC0415

    poll_min = os.environ.get("HBUILDER_CUTOFFS_POLL_MIN", str(cutoffs_poller.DEFAULT_INTERVAL_MIN))
    if poll_min.strip().lower() in ("0", "off", "no", ""):
        print("cutoffs poller: off (HBUILDER_CUTOFFS_POLL_MIN=0)", file=sys.stderr)
        return
    try:
        event.CUTOFFS_POLLER.configure(enabled=True, interval_min=poll_min)
        print(
            f"cutoffs poller: on, every {event.CUTOFFS_POLLER.interval_min} min "
            + (
                "(pulls the VPS tracker)"
                if event.CUTOFFS_POLL_SCRIPT == config.CUTOFFS_PULL_SCRIPT
                else "(guests only)"
            ),
            file=sys.stderr,
        )
    except ValueError as exc:
        print(f"cutoffs poller: HBUILDER_CUTOFFS_POLL_MIN ignored -- {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    # a custom User-Agent on the requests to the holobuilder server: it sits behind
    # Cloudflare, whose browser checks stop Python's default one
    import urllib.request  # noqa: PLC0415

    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "holobuilder-web")]
    urllib.request.install_opener(opener)
    _enable_kernels()
    # The repo root is the working directory of the server and of every process it
    # starts, whatever directory it was launched from.
    os.chdir(config.REPO_ROOT)
    args = build_parser().parse_args(argv)
    Handler.art_dir = Path(args.art_dir).resolve()
    try:
        server = ThreadingServer((BIND_HOST, args.port), Handler)
    except OSError as exc:
        # The message matters more than the traceback: the operator's next
        # move is to find and stop the process holding the port, and on
        # Windows that process may be elevated and refuse an ordinary kill.
        find = (
            f"    netstat -ano | findstr :{args.port}"
            if sys.platform == "win32"
            else f"    ss -ltnp 'sport = :{args.port}'"
        )
        print(
            f"cannot bind {BIND_HOST}:{args.port} -- {exc}\n"
            f"\n"
            f"Something is already listening there. Find it with:\n"
            f"{find}\n"
            f"then stop that PID, or start this on another port with --port.",
            file=sys.stderr,
        )
        return 2
    art_note = (
        f"{Handler.art_dir}"
        if Handler.art_dir.is_dir()
        else f"{Handler.art_dir} (missing -- faces degrade to gradients)"
    )
    print(f"art: {art_note}", file=sys.stderr)
    print(f"serving on http://{BIND_HOST}:{args.port}/", file=sys.stderr)
    _start_cutoffs_poller()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        event.CUTOFFS_POLLER.stop()
        server.server_close()
    return 0
