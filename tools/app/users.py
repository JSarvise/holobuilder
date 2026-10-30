"""Who uses holobuilder, and the administrator's yes/no (hb-auth, tools/app/server/hb_auth.py).

    python tools/app/users.py                 # everyone: status, username, email, last seen, version
    python tools/app/users.py approve 3       # let user 3 in
    python tools/app/users.py revoke 3        # cut user 3 off (at once: every request checks)
    python tools/app/users.py delete 3        # forget user 3 entirely

Uses this PC's admin key (state/server_api.json). Nothing about anyone's game account is on the
server to show: the username they chose, their email (from Google) and when they last connected.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

CONF = Path(__file__).resolve().parents[2] / "state" / "server_api.json"


def call(method: str, route: str) -> dict:
    conf = json.loads(CONF.read_text("utf8"))
    req = urllib.request.Request(conf["url"].rstrip("/") + "/auth/" + route, method=method,
                                 data=b"" if method == "POST" else None,
                                 headers={"Authorization": f"Bearer {conf['token']}"})
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 -- our own host
        return json.loads(r.read())


def ago(t: int | None) -> str:
    if not t:
        return "never"
    s = int(time.time() - t)
    return f"{s // 86400} d ago" if s >= 86400 else f"{s // 3600} h ago" if s >= 3600 else f"{s // 60} min ago"


def main(argv: list[str]) -> int:
    # A custom User-Agent: the server is behind Cloudflare, whose browser checks stop Python's default one.
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "holobuilder-admin")]
    urllib.request.install_opener(opener)
    if argv and argv[0] in ("approve", "revoke", "delete") and len(argv) == 2:
        call("POST", f"admin/user?id={int(argv[1])}&action={argv[0]}")
        print(f"user {argv[1]}: {argv[0]}d")
    users = call("GET", "admin/users")["users"]
    if not users:
        print("no one has signed in yet")
    for u in users:
        mark = {"pending": "⏳", "approved": "✓", "revoked": "✗"}.get(u["status"], "?")
        print(f"{u['id']:>3}  {mark} {u['status']:<9} {(u.get('username') or '(no username)')[:20]:<20} {u['email'][:34]:<34} "
              f"seen {ago(u['last_seen']):<12} app {u['app'] or '-'}{'  (admin)' if u['admin'] else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
