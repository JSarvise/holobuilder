"""Who may talk to this server: its own pages, not any other page open in the same browser.

Binding to 127.0.0.1 keeps other machines out, not other web pages: a page from anywhere can
send this server a request (a form, an image, a `fetch`), and through DNS rebinding a hostile
name can even point at 127.0.0.1 and read the answers. So:

* **Every request** must name this server in its `Host` header (127.0.0.1 or localhost, on
  this server's port): a rebound name does not.
* **Every POST** (everything that changes something: the collection, a refresh that signs in to
  the game account, the admin routes, a search) must also come from this server's own pages:
  an `Origin`, when the browser sends one, of this server; a `Sec-Fetch-Site`, when sent, of
  `same-origin`; and the session key (`HEADER`), which only this server's own pages carry.

The key is made when the server starts and lives only in its memory. Every page the server
sends carries it (`inject`: a `<meta>` and `hb-session.js`, which adds it to the page's own
non-GET requests). Another page cannot read it: the browser keeps a page from reading another
origin's answers, and the `Host` check keeps a rebound name from reading this one's.
"""

from __future__ import annotations

import hmac
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hbweb.router import Handler

HEADER = "X-HB-Session"
TOKEN = secrets.token_urlsafe(32)
HOSTS = ("127.0.0.1", "localhost")
# what `inject` puts first in every page's <head>, before any other script runs
_HEAD = b"<head>"


def _port(h: Handler) -> int:
    return int(h.server.server_address[1])  # type: ignore[index]  # an AF_INET (host, port)


def host_ok(h: Handler) -> bool:
    port = _port(h)
    return (h.headers.get("Host") or "").lower() in {f"{name}:{port}" for name in HOSTS}


def refusal(h: Handler, method: str) -> str | None:
    """Why this request is refused, or None when it may go on."""
    if not host_ok(h):
        return "this server answers requests made to 127.0.0.1 or localhost only"
    if method in ("GET", "HEAD"):
        return None
    origin = h.headers.get("Origin")
    if origin is not None and origin.lower() not in {f"http://{name}:{_port(h)}" for name in HOSTS}:
        return f"a request from another site ({origin}) may not change anything here"
    site = h.headers.get("Sec-Fetch-Site")
    if site is not None and site not in ("same-origin", "none"):
        return f"a {site} request may not change anything here"
    if not hmac.compare_digest(h.headers.get(HEADER, ""), TOKEN):
        return "this request does not come from one of this server's own pages (no session key)"
    return None


def inject(page: bytes) -> bytes:
    """`page` (an HTML page of `static/`) with the session key and the script that sends it,
    first in its <head>."""
    tag = (
        f'<head>\n<meta name="hb-session" content="{TOKEN}">\n<script src="/hb-session.js"></script>'
    ).encode()
    return page.replace(_HEAD, tag, 1)
