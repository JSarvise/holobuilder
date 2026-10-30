"""launcher + app_settings: the update verdict, the Settings checks and what reaches the server.
No window, no network."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))
import app_settings as st  # noqa: E402
import launcher  # noqa: E402


def test_update_verdict():
    remote = {"latest": "0.2.0", "min_version": "0.1.5"}
    assert launcher.update_state("0.2.0", remote) == "none"
    assert launcher.update_state("0.1.9", remote) == "available"
    assert launcher.update_state("0.1.4", remote) == "required"
    assert launcher.update_state("0.10.0", remote) == "none"        # numeric, not text, order
    assert launcher.update_state("dev", remote) == "dev"


def test_settings_validation(tmp_path):
    cred = tmp_path / "C0FFEE"
    cred.write_bytes(b"\xef\xbb\xbf" + b"x" * 255)
    good = dict(st.DEFAULTS, credential_path=str(cred), server_token="t" * 64)
    assert st.validate(good) == {}
    assert "credential_path" in st.validate(dict(good, credential_path=""))
    small = tmp_path / "small"
    small.write_bytes(b"x" * 20)
    assert "not a credential" in st.validate(dict(good, credential_path=str(small)))["credential_path"]
    assert "region" in st.validate(dict(good, region="eu"))
    assert "server_url" in st.validate(dict(good, server_url="http://x"))


def test_env_carries_the_settings_and_never_reads_the_game():
    e = st.env(dict(st.DEFAULTS, credential_path="C:/c", region="jp", workers=6, cache_mb=0), base={})
    assert e["HB_NO_GAME"] == "1" and e["HB_APP_MODE"] == "1"
    assert e["HB_CREDENTIAL_FILE"] == "C:/c" and e["HB_ACCOUNT_HOST"] == "jp.game-hololive-dreams.com"
    assert e["HBUILDER_WORKERS"] == e["HBUILDER_WEB_WORKERS"] == "6"
    assert e["HB_CONTENT_CACHE_MB"] == "0"
    assert "HBUILDER_WORKERS" not in st.env(dict(st.DEFAULTS), base={})   # 0 = every thread


def test_workers_setting_reaches_the_solver(monkeypatch):
    from hbuilder.solver import parallel
    monkeypatch.setenv("HBUILDER_WORKERS", "3")
    assert parallel.default_workers() == 3
    monkeypatch.setenv("HBUILDER_WORKERS", "")
    assert parallel.default_workers() >= 1


def test_a_collection_read_before_0_1_7_is_read_again(tmp_path):
    """After updating, the Member Power-Up must not stay missing until a Refresh."""
    p, b = tmp_path / "collection.json", tmp_path / "board.json"
    b.write_text(json.dumps({"connect_level": "auto"}), "utf-8")
    p.write_text(json.dumps({"player": {"posters": 41}}), "utf-8")
    assert not launcher._collection_current(p, b)
    p.write_text(json.dumps({"player": {"posters": 41, "power_up_permyriad": "auto"}}), "utf-8")
    assert launcher._collection_current(p, b)
    assert not launcher._collection_current(tmp_path / "absent.json", b)
    b.write_text(json.dumps({"connect_level": 1}), "utf-8")         # before 0.1.9: sockets forced to 1
    assert not launcher._collection_current(p, b)
    b.write_text(json.dumps({"connect_level": "auto"}), "utf-8")
    h = tmp_path / "event_history.json"
    h.write_text(json.dumps({"events": {}}), "utf-8")               # the publisher's, synced
    assert not launcher._collection_current(p, b)
    h.write_text(json.dumps({"events": {}, "built_for": "this account"}), "utf-8")
    assert launcher._collection_current(p, b)


def test_a_new_version_while_open_reaches_the_page(monkeypatch):
    """The update notice arrives with the app open, not only at start."""
    api = launcher.Api()
    calls = []

    class W:
        def evaluate_js(self, js):
            calls.append(js)
    api._window = W()
    found = {"state": "available", "local": "0.1.7", "latest": "0.1.8", "notes": ""}
    seq = iter([{"state": "none"}, found, found])
    monkeypatch.setattr(api, "check_update", lambda: next(seq))
    ticks = iter(range(3))
    monkeypatch.setattr(launcher.time, "sleep", lambda s: next(ticks))   # 3 polls, then StopIteration
    try:
        api._watch_updates()
    except StopIteration:
        pass
    assert len(calls) == 1 and "0.1.8" in calls[0]          # shown once, not on every poll
    monkeypatch.setattr(api, "can_update", lambda: True)
    assert api.pending_update() == found
    monkeypatch.setattr(api, "can_update", lambda: False)    # a dev checkout never offers it
    assert api.pending_update() is None


def test_a_username_is_asked_only_when_the_server_says_one_is_missing(monkeypatch):
    import launcher as L
    api = L.Api()
    asked = []
    monkeypatch.setattr(api, "_emit", lambda **ev: asked.append(ev))
    for me in ({}, {"status": "approved", "name": "x"},                 # offline / older server
               {"status": "approved", "username": "nene"},             # already has one
               {"status": "revoked", "username": ""}):
        monkeypatch.setattr(L, "server_me", lambda s, me=me: me)
        api._ask_username({})
    assert asked == []
    monkeypatch.setattr(L, "server_me", lambda s: {"status": "approved", "username": ""})
    monkeypatch.setattr(L, "server_set_username", lambda s, u: {"ok": u == "nene", "username": u})
    monkeypatch.setattr(L.app_settings, "load", lambda: {})
    import threading
    t = threading.Thread(target=api._ask_username, args=({},))
    t.start()
    for _ in range(100):
        if asked:
            break
        threading.Event().wait(0.01)
    assert asked[0] == {"step": "username", "state": "ask"}
    assert not api.set_username("x")["ok"] and t.is_alive()             # refused: still asking
    assert api.set_username("nene")["ok"]
    t.join(2)
    assert not t.is_alive() and asked[-1] == {"step": "username", "state": "done"}


def test_the_updater_is_started_without_pyinstallers_variables(monkeypatch):
    import launcher as L
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", r"C:\Temp\_MEI220522")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("_MEIPASS2", r"C:\Temp\_MEI220522")
    monkeypatch.setenv("HB_KEEP_ME", "1")
    env = L.clean_env(reset=True)
    assert not [k for k in env if k.upper().startswith(("_PYI_", "_MEIPASS"))]
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1" and env["HB_KEEP_ME"] == "1"
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in L.clean_env()


def test_an_unsigned_version_file_never_asks_for_an_update(monkeypatch):
    """Only the publisher's signature makes a version.json believable: an unsigned
    or altered one leaves the state 'unknown', never 'required'."""
    import io
    import signing

    def serve(doc):
        monkeypatch.setattr(launcher.urllib.request, "urlopen",
                            lambda req, timeout=0: io.BytesIO(json.dumps(doc).encode()))
    monkeypatch.setattr(launcher.app_settings, "load", lambda: {"server_url": "https://x", "server_token": ""})
    monkeypatch.setattr(launcher, "app_version", lambda: "0.1.0")
    serve({"latest": "9.9.9", "min_version": "9.9.9"})
    assert launcher.Api().check_update()["state"] == "unknown"
    serve({"latest": "9.9.9", "min_version": "9.9.9", "sig": "AAAA"})
    assert launcher.Api().check_update()["state"] == "unknown"
    monkeypatch.setattr(signing, "verify", lambda data, sig, pub=None: None)   # a valid signature
    serve({"latest": "9.9.9", "min_version": "9.9.9", "sig": "ok"})
    assert launcher.Api().check_update()["state"] == "required"


def test_diagnostics_hide_what_identifies_the_person(tmp_path, monkeypatch):
    """"Copy diagnostics" text goes into public bug reports: no account file path,
    Steam ID, access key, Windows user folder or token-like run may reach it."""
    home = tmp_path / "Users" / "SomeName"
    cred = home / "AppData" / "cred"
    cred.parent.mkdir(parents=True)
    cred.write_bytes(b"x" * 255)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(launcher, "hardware", lambda: {"cores": 8, "threads": 16, "ram_gb": 32.0, "gpu": "G"})
    token = "Zm9vYmFyYmF6cXV4" * 4
    log = tmp_path / "server.log"
    log.write_text(f"start\nreading {cred}\nAuthorization: Bearer {token}\nlast line\n", "utf8")
    s = dict(st.DEFAULTS, credential_path=str(cred), steam_id="76561190000000000", server_token=token)
    text = launcher.diagnostics(s, engine="running", log=log)
    assert "account file set, exists" in text and "access key set" in text and "last line" in text
    for secret in (str(cred), "SomeName", "76561190000000000", token):
        assert secret not in text
