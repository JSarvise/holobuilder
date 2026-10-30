"""hb-auth: who may use api.holobuilder.dev (runs on the VPS, 127.0.0.1:8792, behind Caddy).

Anyone may download the installer from the public repo; they sign in with Google, and an
administrator APPROVES them before the app and the game data reach them.

  POST /register  {"id_token": <Google ID token>, "app": "0.1.3"}   (public, rate-limited)
        Verifies the token against Google's keys (RS256, iss, aud = our client id, exp,
        email_verified). Creates the user as 'pending' (an ADMIN_EMAILS address is approved at
        once), issues a fresh random access key and stores ONLY its sha256.
        -> {"token", "status", "username", "email"}
  GET  /me        Authorization: Bearer <key>  -> {"status", "username", "email", "admin"}
  POST /username  {"username": "..."}  Authorization: Bearer <key> (pending or approved)
        3-20 letters, digits, '_', '-' or '.', unique ignoring case -> {"username"};
        400 not a username, 409 taken
  GET  /check     Caddy's forward_auth: 200 for an approved key (or the server's admin key),
                  401 unknown, 403 pending / revoked. Notes last_seen (at most every 5 min).
  GET  /admin/users                      the list (admin only)
  POST /admin/user?id=N&action=approve|revoke|delete   (admin only)

Privacy, by construction: no IP address is stored or logged; the account file and the game
account never reach this server (the app's refresh goes from the user's PC to the game). What
is kept per user: Google's subject id, email, the username they chose, status, dates, app
version. Google's display name is the person's real name and is NOT kept: a user picks a
username instead. `name` in a response is the username, for the installers of 0.1.19 and
older that read it.

stdlib + pycryptodome (already in the VPS venv). SQLite at $HB_AUTH_DB.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

CLIENT_IDS = {c for c in os.environ.get("HB_GOOGLE_CLIENT_IDS", "").split(",") if c}
ADMIN_EMAILS = {e.lower() for e in os.environ.get("HB_ADMIN_EMAILS", "").split(",") if e}
ADMIN_KEY = os.environ.get("HB_API_TOKEN", "")
DB = os.environ.get("HB_AUTH_DB", "/var/lib/hb-auth/users.db")
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
MAX_BODY = 8192
REGISTER_PER_HOUR = 6          # per client address, kept in memory only
SEEN_EVERY_S = 300
USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,20}")


def valid_username(s) -> str | None:
    s = str(s or "").strip()
    return s if USERNAME_RE.fullmatch(s) else None


def b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class Keys:
    """Google's signing keys, cached for the time Google says (Cache-Control max-age)."""

    def __init__(self, fetch=None):
        self.fetch = fetch or self._fetch
        self.keys: dict[str, RSA.RsaKey] = {}
        self.until = 0.0
        self.lock = threading.Lock()

    @staticmethod
    def _fetch() -> tuple[dict, int]:
        with urllib.request.urlopen(JWKS_URL, timeout=10) as r:  # noqa: S310 -- google
            age = 3600
            for part in (r.headers.get("Cache-Control") or "").split(","):
                if part.strip().startswith("max-age="):
                    age = int(part.strip()[8:])
            return json.loads(r.read()), age

    def get(self, kid: str) -> RSA.RsaKey | None:
        with self.lock:
            if time.time() >= self.until or kid not in self.keys:
                doc, age = self.fetch()
                self.keys = {k["kid"]: RSA.construct((int.from_bytes(b64url(k["n"]), "big"),
                                                      int.from_bytes(b64url(k["e"]), "big")))
                             for k in doc.get("keys", []) if k.get("kty") == "RSA"}
                self.until = time.time() + max(60, age)
            return self.keys.get(kid)


def verify_id_token(token: str, keys: Keys, now: float | None = None) -> dict:
    """The claims of a valid Google ID token for one of our client ids; ValueError otherwise."""
    try:
        h64, p64, s64 = token.split(".")
        header, claims = json.loads(b64url(h64)), json.loads(b64url(p64))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("malformed token") from exc
    if header.get("alg") != "RS256":
        raise ValueError("unexpected algorithm")
    key = keys.get(header.get("kid", ""))
    if key is None:
        raise ValueError("unknown signing key")
    try:
        pkcs1_15.new(key).verify(SHA256.new(f"{h64}.{p64}".encode()), b64url(s64))
    except (ValueError, TypeError) as exc:
        raise ValueError("bad signature") from exc
    now = time.time() if now is None else now
    if claims.get("iss") not in ISSUERS:
        raise ValueError("wrong issuer")
    if claims.get("aud") not in CLIENT_IDS:
        raise ValueError("not issued for holobuilder")
    if float(claims.get("exp", 0)) < now - 60:
        raise ValueError("expired")
    if not claims.get("email_verified"):
        raise ValueError("email not verified")
    return claims


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, sub TEXT UNIQUE NOT NULL,
              email TEXT, name TEXT, status TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0,
              created INTEGER, approved INTEGER, last_seen INTEGER, app TEXT);
            CREATE TABLE IF NOT EXISTS keys (hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
              created INTEGER, last_seen INTEGER);
        """)
        with self.db:
            if "username" not in {r[1] for r in self.db.execute("PRAGMA table_info(users)")}:
                self.db.execute("ALTER TABLE users ADD COLUMN username TEXT")
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_username "
                            "ON users (username COLLATE NOCASE)")
            self.db.execute("UPDATE users SET name='' WHERE name IS NOT NULL AND name != ''")

    def register(self, claims: dict, app: str) -> tuple[str, dict]:
        email = str(claims.get("email", "")).lower()[:200]
        admin = email in ADMIN_EMAILS
        now = int(time.time())
        key = secrets.token_urlsafe(32)
        with self.lock, self.db:
            row = self.db.execute("SELECT id, status FROM users WHERE sub=?", (claims["sub"],)).fetchone()
            if row is None:
                status = "approved" if admin else "pending"
                cur = self.db.execute(
                    "INSERT INTO users (sub, email, name, status, admin, created, approved, app) "
                    "VALUES (?,?,'',?,?,?,?,?)",
                    (claims["sub"], email, status, int(admin), now, now if admin else None, app[:20]))
                uid, username = cur.lastrowid, None
            else:
                uid, status = row
                self.db.execute("UPDATE users SET email=?, app=? WHERE id=?", (email, app[:20], uid))
                username = self.db.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()[0]
            self.db.execute("INSERT INTO keys (hash, user_id, created) VALUES (?,?,?)", (key_hash(key), uid, now))
        return key, {"status": status, "username": username or "", "name": username or "",
                     "email": email, "admin": admin}

    def set_username(self, user_id: int, username) -> str:
        """'ok' | 'invalid' | 'taken'."""
        name = valid_username(username)
        if name is None:
            return "invalid"
        try:
            with self.lock, self.db:
                self.db.execute("UPDATE users SET username=? WHERE id=?", (name, user_id))
        except sqlite3.IntegrityError:
            return "taken"
        return "ok"

    def lookup(self, key: str) -> dict | None:
        if not key:
            return None
        if ADMIN_KEY and secrets.compare_digest(key, ADMIN_KEY):
            return {"id": 0, "status": "approved", "admin": True, "username": "owner", "email": ""}
        with self.lock:
            row = self.db.execute(
                "SELECT u.id, u.status, u.admin, u.username, u.email, k.last_seen FROM keys k "
                "JOIN users u ON u.id=k.user_id WHERE k.hash=?", (key_hash(key),)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "status": row[1], "admin": bool(row[2]), "username": row[3] or "",
                "email": row[4], "_seen": row[5] or 0}

    def seen(self, key: str, user_id: int, app: str | None) -> None:
        now = int(time.time())
        with self.lock, self.db:
            self.db.execute("UPDATE keys SET last_seen=? WHERE hash=?", (now, key_hash(key)))
            self.db.execute("UPDATE users SET last_seen=?, app=COALESCE(?, app) WHERE id=?",
                            (now, app[:20] if app else None, user_id))

    def users(self) -> list[dict]:
        cols = ["id", "email", "username", "status", "admin", "created", "approved", "last_seen", "app"]
        with self.lock:
            rows = self.db.execute(f"SELECT {', '.join(cols)} FROM users ORDER BY created").fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def act(self, user_id: int, action: str) -> bool:
        now = int(time.time())
        with self.lock, self.db:
            if action == "approve":
                n = self.db.execute("UPDATE users SET status='approved', approved=? WHERE id=?", (now, user_id)).rowcount
            elif action == "revoke":
                n = self.db.execute("UPDATE users SET status='revoked' WHERE id=?", (user_id,)).rowcount
            elif action == "delete":
                self.db.execute("DELETE FROM keys WHERE user_id=?", (user_id,))
                n = self.db.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount
            else:
                return False
        return n > 0


class Limiter:
    def __init__(self, per_hour: int):
        self.per_hour, self.hits, self.lock = per_hour, {}, threading.Lock()

    def ok(self, who: str) -> bool:
        now = time.time()
        with self.lock:
            hits = [t for t in self.hits.get(who, []) if now - t < 3600]
            if len(hits) >= self.per_hour:
                self.hits[who] = hits
                return False
            self.hits[who] = hits + [now]
            return True


def make_handler(store: Store, keys: Keys, limiter: Limiter):
    class H(BaseHTTPRequestHandler):
        server_version = "hb-auth"
        sys_version = ""

        def log_message(self, *a):          # no access log: no IPs anywhere
            pass

        def _json(self, code: int, doc: dict) -> None:
            body = json.dumps(doc).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _key(self) -> str:
            h = self.headers.get("Authorization", "")
            return h[7:].strip() if h.startswith("Bearer ") else ""

        def _who(self) -> dict | None:
            return store.lookup(self._key())

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path == "/check":
                u = self._who()
                if u is None:
                    return self._json(401, {"error": "unknown key"})
                if u["status"] != "approved":
                    return self._json(403, {"error": u["status"]})
                if u["id"] and time.time() - u["_seen"] > SEEN_EVERY_S:
                    store.seen(self._key(), u["id"], self.headers.get("X-HB-App"))
                return self._json(200, {"ok": True})
            if path == "/me":
                u = self._who()
                if u is None:
                    return self._json(401, {"error": "unknown key"})
                return self._json(200, {**{k: u[k] for k in ("status", "username", "email", "admin")},
                                        "name": u["username"]})
            if path == "/admin/users":
                u = self._who()
                if not (u and u["admin"] and u["status"] == "approved"):
                    return self._json(403, {"error": "admin only"})
                return self._json(200, {"users": store.users()})
            return self._json(404, {"error": "no such route"})

        def do_POST(self):  # noqa: N802
            url = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "too large"})
            raw = self.rfile.read(n) if n else b""
            if url.path == "/register":
                who = (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()
                if not limiter.ok(hashlib.sha256(who.encode()).hexdigest()):
                    return self._json(429, {"error": "too many registrations; try later"})
                try:
                    body = json.loads(raw or b"{}")
                    claims = verify_id_token(str(body.get("id_token", "")), keys)
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._json(400, {"error": str(exc)})
                key, info = store.register(claims, str(body.get("app", "")))
                return self._json(200, {"token": key, **info})
            if url.path == "/username":
                u = self._who()
                if u is None or not u["id"]:
                    return self._json(401, {"error": "unknown key"})
                if u["status"] == "revoked":
                    return self._json(403, {"error": "revoked"})
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    body = {}
                wanted = body.get("username") if isinstance(body, dict) else None
                r = store.set_username(u["id"], wanted)
                if r == "taken":
                    return self._json(409, {"error": "that username is taken"})
                if r != "ok":
                    return self._json(400, {"error": "3-20 letters, digits, _ - or ."})
                return self._json(200, {"username": valid_username(wanted)})
            if url.path == "/admin/user":
                u = self._who()
                if not (u and u["admin"] and u["status"] == "approved"):
                    return self._json(403, {"error": "admin only"})
                q = parse_qs(url.query)
                try:
                    uid, action = int(q["id"][0]), q["action"][0]
                except (KeyError, ValueError):
                    return self._json(400, {"error": "id and action needed"})
                return self._json(200 if store.act(uid, action) else 404, {"ok": True})
            return self._json(404, {"error": "no such route"})
    return H


def main() -> int:
    port = int(os.environ.get("HB_AUTH_PORT", "8792"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(Store(DB), Keys(), Limiter(REGISTER_PER_HOUR)))
    print(f"hb-auth on 127.0.0.1:{port}, {len(CLIENT_IDS)} client id(s), {len(ADMIN_EMAILS)} admin(s)")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
