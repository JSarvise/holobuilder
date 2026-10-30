"""SHA256SUMS + SHA256SUMS.sig for the installer release: a
download checks out only when the list is signed by the release key AND the file matches it."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_setup  # noqa: E402
import signing  # noqa: E402
from Crypto.PublicKey import ECC  # noqa: E402


@pytest.fixture
def key(tmp_path, monkeypatch):
    k = ECC.generate(curve="ed25519")
    pem = tmp_path / "key.pem"
    pem.write_text(k.export_key(format="PEM"), "utf8")
    monkeypatch.setattr(signing, "PRIVATE", pem)
    monkeypatch.setattr(signing, "RELEASE_PUBKEY", k.public_key().export_key(format="raw").hex())
    return pem


def test_signed_sums_accept_the_file_and_refuse_a_changed_one(tmp_path, key):
    exe = tmp_path / "holobuilder-setup.exe"
    exe.write_bytes(b"MZ" + b"\0" * 100)
    sums, sig = build_setup.sums([exe], tmp_path)
    assert sums.read_text().endswith("  holobuilder-setup.exe\n") and sig.is_file()
    assert signing.verify_sums(sums, [exe]) == ["OK  holobuilder-setup.exe"]
    exe.write_bytes(b"MZ" + b"\1" * 100)
    assert signing.verify_sums(sums, [exe])[0].startswith("BAD")


def test_a_list_not_signed_by_the_release_key_is_refused(tmp_path, key):
    exe = tmp_path / "holobuilder-setup.exe"
    exe.write_bytes(b"MZ")
    sums, _ = build_setup.sums([exe], tmp_path)
    sums.write_bytes(sums.read_bytes().replace(b"holobuilder", b"holobuildeR"))
    with pytest.raises(signing.BadSignature):
        signing.verify_sums(sums, [exe])
