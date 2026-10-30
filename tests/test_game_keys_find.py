"""tools/recon/proxy/game_keys.py --find: the seed is the one literal whose MD5 opens the server's
own Master/Get answer. Driven with a made-up seed, so no game, no keys and no network."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "tools" / "recon" / "proxy"
sys.path.insert(0, str(PROXY))

pytest.importorskip("Crypto")
import account_snapshot as A  # noqa: E402
import game_keys  # noqa: E402
import refresh_all  # noqa: E402

SEED = b"made-up-build-seed-for-tests"


def _pb(field: int, payload: bytes) -> bytes:
    return A.pb_bytes_field(field, payload)


def _answer(seed: bytes = SEED) -> bytes:
    """One sealed message shaped like MasterGetResponse { version = 1; tag packs = 2 }."""
    body = _pb(1, b"1.2.3") + _pb(2, _pb(1, b"main") + _pb(4, b"ab" * 16)) * 20
    return A.seal(body, seed)


@pytest.fixture
def no_private_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(game_keys, "_private", lambda: None)
    monkeypatch.setattr(game_keys, "STATE_FILE", tmp_path / "state" / "game_keys.json")
    monkeypatch.delenv("HB_GAME_KEYS", raising=False)
    return tmp_path


def test_only_the_right_seed_opens_the_answer():
    msg = _answer()
    assert game_keys.opens(msg, SEED)
    assert not game_keys.opens(msg, SEED + b"x")
    assert not game_keys.opens(msg, b"")
    assert not game_keys.opens(b"\x0a\x00", SEED)


def test_the_candidates_are_short_printable_ascii_each_once():
    lits = ["get_Item", "get_Item", "a" * 129, "日本語", "line\nbreak", "", "ok seed"]
    assert game_keys.seed_candidates(lits) == [b"get_Item", b"ok seed"]


def test_the_search_finds_the_one_literal_among_many():
    literals = [f"literal_{k}" for k in range(3000)] + [SEED.decode()] + ["System.String"]
    assert game_keys.search(literals, _answer()) == [SEED]


def _fake_install(monkeypatch, literals, answer):
    import decrypt_metadata
    import metadata_parse

    monkeypatch.setattr(decrypt_metadata, "installed_metadata", lambda: str(ROOT / "pyproject.toml"))
    monkeypatch.setattr(decrypt_metadata, "decrypt", lambda raw: (b"dec", b"\x01" * 128, []))

    class Meta:
        def __init__(self, *a, **k):
            pass

        def literals(self):
            return literals

    monkeypatch.setattr(metadata_parse, "Metadata", Meta)
    monkeypatch.setattr(game_keys, "master_get_messages", lambda host: [answer])


def test_find_writes_the_seed_and_keeps_other_fields(monkeypatch, no_private_keys):
    _fake_install(monkeypatch, ["noise", SEED.decode()], _answer())
    out = no_private_keys / "state" / "game_keys.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps({"note": "kept"}), "utf8")
    assert game_keys.find("us", out=out) == 0
    doc = json.loads(out.read_text("utf8"))
    assert doc == {"note": "kept", "grpc_seed": SEED.decode(), "metadata_key_hex": "01" * 128}


def test_find_refuses_to_guess(monkeypatch, no_private_keys, capsys):
    out = no_private_keys / "game_keys.json"
    _fake_install(monkeypatch, ["noise", "more noise"], _answer())
    assert game_keys.find("us", out=out) == 1
    assert not out.exists() and "no literal opens" in capsys.readouterr().out


def test_the_seed_is_read_from_the_state_file(monkeypatch, no_private_keys):
    assert game_keys.grpc_seed(required=False) is None and game_keys.source() is None
    with pytest.raises(game_keys.GameKeyMissing, match="--find"):
        game_keys.grpc_seed()
    game_keys.STATE_FILE.parent.mkdir(parents=True)
    game_keys.STATE_FILE.write_text(json.dumps({"grpc_seed": SEED.decode()}), "utf8")
    assert game_keys.grpc_seed() == SEED and game_keys.source() == str(game_keys.STATE_FILE)


def test_the_cipher_reads_a_seed_that_arrives_after_import(monkeypatch, no_private_keys):
    """account_snapshot is imported before refresh.cmd finds the constants: sealing must use
    the seed written afterwards, not the absence it saw at import."""
    monkeypatch.setattr(A, "SEED", None)
    game_keys.STATE_FILE.parent.mkdir(parents=True)
    game_keys.STATE_FILE.write_text(json.dumps({"grpc_seed": SEED.decode()}), "utf8")
    msg = A.seal(b"\x0a\x05hello")
    assert A.unseal(msg) == b"\x0a\x05hello" and A.unseal(msg, SEED) == b"\x0a\x05hello"


def test_a_refresh_that_reads_the_api_stops_before_anything_runs(monkeypatch, capsys):
    monkeypatch.setattr(game_keys, "grpc_seed", lambda required=True: None)
    monkeypatch.setattr(A, "in_maintenance", lambda account="main": None)   # no network here

    def ran(*a, **k):
        raise AssertionError("a step ran")

    monkeypatch.setattr(refresh_all, "_run", ran)
    assert refresh_all.main(["--only", "account"]) == refresh_all.EXIT_NO_KEYS
    assert "game_keys.py --find" in capsys.readouterr().out


def test_steps_that_do_not_read_the_api_need_no_keys(monkeypatch):
    monkeypatch.setattr(game_keys, "grpc_seed", lambda required=True: None)
    assert not refresh_all.missing_keys(["charts", "card_art", "songs"])
    assert refresh_all.missing_keys(["charts", "account"])
    monkeypatch.setattr(game_keys, "grpc_seed", lambda required=True: SEED)
    assert not refresh_all.missing_keys(["account"])
