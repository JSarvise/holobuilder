"""The account refresh (`POST /api/refresh`), the Park scan (`POST /api/park/scan`) and the
Users console (`GET /api/admin/me`, `GET /api/admin/users`, `POST /api/admin/user`).

⚠ The refresh is the one route that WRITES, and it writes in a SEPARATE PROCESS:
`refresh_all.py` owns the data-authority contract (it hashes `data/owned.json` before and
after, only APPENDS to `data/cards.json`), so the rules live in one place rather than
being re-implemented in a web handler. It spends the account credential, so anything
that can reach this port can spend it; the server binds loopback only, which is the
whole of the protection.
"""

from __future__ import annotations

import json
import os
import threading
from urllib.parse import parse_qs, urlparse

from hbweb import config, gacha, scripts

# One account refresh at a time. A page reloaded mid-refresh with the button pressed again
# would start a SECOND refresh_all.py beside the first: two logins, two writers on the same
# dumps (a transient Errno 22 on dumps/inventory.json). The second is refused (409), like
# the gacha sync.
REFRESH_LOCK = threading.Lock()


def refresh_route(h) -> None:
    """`POST /api/refresh`, guarded: a second refresh is a 409, and any failure is JSON.
    An exception escaping a handler makes `http.server` emit an HTML error page, which the
    page reports as a JSON parse error naming a column number."""
    if not REFRESH_LOCK.acquire(blocking=False):
        h.reply_json(
            409,
            {
                "error": "a refresh is already running; wait for it to finish "
                "(the server console shows its progress)"
            },
        )
        return
    try:
        refresh(h)
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        # the page went away before the reply (a reload): the refresh itself ran
        # to the end; `handle_error` logs the one line
        raise
    except Exception as exc:  # noqa: BLE001
        import traceback  # noqa: PLC0415

        detail = traceback.format_exc()
        print(detail, flush=True)
        h.reply_json(
            500,
            {
                "returncode": -1,
                "stdout": "",
                "stderr": f"{type(exc).__name__}: {exc}\n\n{detail}",
            },
        )
    finally:
        REFRESH_LOCK.release()


def refresh(h) -> None:
    """Pull the account down from the game and rebuild every derived file, then sync the
    gacha pulls (`gacha.sync_after_refresh`). The output is returned whatever the exit
    code: a partial refresh that names what moved is more useful than a bare failure."""
    # `--merge-collection`: `state/collection.json` must reflect the account, so the
    # page's refresh button merges it. Without it a refresh moves every derived dump
    # forward and leaves the collection behind -- the solver would go on scoring a card
    # at level 1 that the account has at 70, and nothing in the output would say so.
    # The merge guesses nothing: every value is the account's own, read back from
    # `User/Get`, MERGED (not replaced), with one backup to
    # `dumps/collection_before_import.json`.
    args = ["--merge-collection"]
    # The desktop app refreshes the ACCOUNT only; everything shared comes from
    # api.holobuilder.dev (tools/app/app_settings.py).
    if os.environ.get("HB_APP_MODE") == "1":
        args.append("--account-only")
    ran = scripts.run_recon_script(h, config.REFRESH_SCRIPT, "refresh", args)
    if ran is None:
        return
    code, stdout = ran
    gacha_block = gacha.sync_after_refresh(h, code)
    if gacha_block is None:
        return
    h.reply_json(
        200,
        {
            "returncode": code,
            "stdout": stdout[-20000:],
            "stderr": "",
            "gacha": gacha_block,
        },
    )


def park_scan_route(h) -> None:
    """`POST /api/park/scan`, the Park page's "Scan all areas": `park_scan.py` in its own
    process, which owns every guard (game closed, quest gate, master check, a repeat login only,
    once per game day, always back to the starting area). Off unless `HB_PARK_SCAN=1`. It logs
    in, so it shares the refresh's lock: never two logins at once."""
    if os.environ.get(config.PARK_SCAN_ENV) != "1":
        h.reply_json(403, {"error": "Scan all areas is off: turn it on in Settings"})
        return
    if not REFRESH_LOCK.acquire(blocking=False):
        h.reply_json(409, {"error": "a refresh or a scan is already running; wait for it to finish"})
        return
    try:
        ran = scripts.run_recon_script(h, config.PARK_SCAN_SCRIPT, "park-scan")
        if ran is None:
            return
        code, stdout = ran
        h.reply_json(
            200,
            {
                "returncode": code,
                "stdout": stdout[-8000:],
                "last": config.read_json(config.PARK_SCAN_LAST_PATH),
            },
        )
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        raise
    except Exception as exc:  # noqa: BLE001
        import traceback  # noqa: PLC0415

        print(traceback.format_exc(), flush=True)
        h.reply_json(500, {"returncode": -1, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        REFRESH_LOCK.release()


def admin_proxy(h, method: str, route: str) -> None:
    """The Users page (the admin console):
    api.holobuilder.dev/auth/<route> with THIS PC's key, which the server only honours for
    an admin. Reads state/server_api.json; writes nothing here (approvals live on the VPS)."""
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    conf = config.read_json(config.SERVER_API_PATH) or {}
    if not conf.get("token"):
        h.reply_json(200, {"admin": False, "error": "no server key on this PC"})
        return
    req = urllib.request.Request(
        conf["url"].rstrip("/") + "/auth/" + route,
        method=method,
        data=b"" if method == "POST" else None,
        headers={"Authorization": f"Bearer {conf['token']}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 -- our own host
            h.reply_json(200, json.loads(r.read()))
    except urllib.error.HTTPError as exc:
        h.reply_json(exc.code, {"error": (json.loads(exc.read() or b"{}") or {}).get("error", str(exc))})
    except OSError as exc:
        h.reply_json(502, {"error": f"server unreachable: {exc}"})


def serve_me(h) -> None:
    """`GET /api/admin/me`: this PC's standing on the holobuilder server."""
    admin_proxy(h, "GET", "me")


def serve_users(h) -> None:
    """`GET /api/admin/users`: every user the holobuilder server knows (admins only)."""
    admin_proxy(h, "GET", "admin/users")


def admin_user(h) -> None:
    """`POST /api/admin/user?id=<n>&action=approve|revoke|delete`."""
    q = parse_qs(urlparse(h.path).query)
    uid, action = (q.get("id") or [""])[0], (q.get("action") or [""])[0]
    if not uid.isdigit() or action not in ("approve", "revoke", "delete"):
        h.reply_json(400, {"error": "id and action (approve | revoke | delete) needed"})
    else:
        admin_proxy(h, "POST", f"admin/user?id={uid}&action={action}")
