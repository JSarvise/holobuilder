"""hb-auth: Google ID tokens checked for real (signed here with a test RSA key), the approval
gate, revocation, and what is (not) stored. No network."""
from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))
import hb_auth as A  # noqa: E402

KEY = RSA.generate(2048)
CID = "holobuilder.apps.googleusercontent.com"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwks():
    n, e = KEY.n.to_bytes((KEY.n.bit_length() + 7) // 8, "big"), KEY.e.to_bytes(3, "big")
    return {"keys": [{"kty": "RSA", "kid": "k1", "n": b64(n), "e": b64(e)}]}, 3600


def token(**over) -> str:
    claims = {"iss": "https://accounts.google.com", "aud": CID, "sub": "111", "email": "Ann@x.com",
              "email_verified": True, "name": "Ann", "exp": time.time() + 600, **over}
    h = b64(json.dumps({"alg": "RS256", "kid": "k1"}).encode())
    p = b64(json.dumps(claims).encode())
    sig = pkcs1_15.new(KEY).sign(SHA256.new(f"{h}.{p}".encode()))
    return f"{h}.{p}.{b64(sig)}"


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(A, "CLIENT_IDS", {CID})
    monkeypatch.setattr(A, "ADMIN_EMAILS", {"owner@x.com"})
    monkeypatch.setattr(A, "ADMIN_KEY", "a" * 64)


def test_a_real_google_token_is_accepted_and_forgeries_are_not():
    keys = A.Keys(fetch=jwks)
    assert A.verify_id_token(token(), keys)["sub"] == "111"
    for bad, why in [(token(aud="someone-else"), "not issued"), (token(exp=time.time() - 3600), "expired"),
                     (token(email_verified=False), "not verified"), (token(iss="evil.com"), "issuer")]:
        with pytest.raises(ValueError, match=why):
            A.verify_id_token(bad, keys)
    h, p, s = token().split(".")
    forged = b64(json.dumps({**json.loads(A.b64url(p)), "sub": "999"}).encode())
    with pytest.raises(ValueError, match="signature"):
        A.verify_id_token(f"{h}.{forged}.{s}", keys)


def test_a_new_user_waits_for_approval_and_only_the_key_hash_is_kept():
    st = A.Store(":memory:")
    key, info = st.register(A.verify_id_token(token(), A.Keys(fetch=jwks)), "0.1.3")
    assert info["status"] == "pending" and info["email"] == "ann@x.com"
    assert st.lookup(key)["status"] == "pending"
    stored = [r[0] for r in st.db.execute("SELECT hash FROM keys")]
    assert key not in stored and A.key_hash(key) in stored          # the key itself: nowhere
    uid = st.users()[0]["id"]
    assert st.act(uid, "approve") and st.lookup(key)["status"] == "approved"
    assert st.act(uid, "revoke") and st.lookup(key)["status"] == "revoked"
    assert st.lookup("made-up") is None


def test_the_owner_is_approved_at_once_and_the_admin_key_still_works():
    st = A.Store(":memory:")
    _, info = st.register({"sub": "1", "email": "owner@x.com", "name": "Owner"}, "0.1.3")
    assert info["status"] == "approved" and info["admin"]
    assert st.lookup("a" * 64)["admin"]


def test_registration_is_rate_limited_per_address():
    lim = A.Limiter(2)
    assert lim.ok("ip") and lim.ok("ip") and not lim.ok("ip") and lim.ok("other")


def test_users_pick_a_username_and_no_real_name_is_kept():
    st = A.Store(":memory:")
    key, info = st.register(A.verify_id_token(token(), A.Keys(fetch=jwks)), "0.1.3")
    assert info["username"] == "" and info["name"] == ""                # Google's "Ann": not kept
    assert [r[0] for r in st.db.execute("SELECT name FROM users")] == [""]
    uid = st.lookup(key)["id"]
    assert st.set_username(uid, "ab") == "invalid" and st.set_username(uid, "a b c") == "invalid"
    assert st.set_username(uid, "Nene_fan.01") == "ok"
    assert st.lookup(key)["username"] == "Nene_fan.01"
    _, again = st.register(A.verify_id_token(token(), A.Keys(fetch=jwks)), "0.1.4")
    assert again["username"] == "Nene_fan.01"                         # a re-install keeps it
    k2, _ = st.register({"sub": "2", "email": "bo@x.com", "name": "Bo Real"}, "0.1.3")
    assert st.set_username(st.lookup(k2)["id"], "nene_FAN.01") == "taken"   # ignoring case
    assert {"username"} <= set(st.users()[0]) and "name" not in st.users()[0]


def test_names_stored_before_usernames_are_blanked(tmp_path):
    import sqlite3
    db = tmp_path / "users.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, sub TEXT UNIQUE NOT NULL, email TEXT, "
                "name TEXT, status TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0, created INTEGER, "
                "approved INTEGER, last_seen INTEGER, app TEXT)")
    con.execute("INSERT INTO users (sub, email, name, status) VALUES ('1', 'a@x.com', 'Real Name', 'approved')")
    con.commit()
    con.close()
    st = A.Store(str(db))
    assert st.db.execute("SELECT name, username FROM users").fetchone() == ("", None)
