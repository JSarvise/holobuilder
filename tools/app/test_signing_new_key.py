"""`signing.py --new-key`: the public half of a new key is installed in the module the app is built
from (it replaces whatever key is there), and nothing is written when it cannot be."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import signing  # noqa: E402


def _module_copy(tmp_path: Path) -> Path:
    module = tmp_path / "signing.py"
    module.write_bytes((HERE / "signing.py").read_bytes())
    return module


def test_a_new_key_replaces_the_installed_public_key_and_signs_for_it(tmp_path):
    module, private = _module_copy(tmp_path), tmp_path / "state" / "release_signing_key.pem"
    before = module.read_bytes()
    pub = signing.new_key(private=private, module=module)
    after = module.read_bytes()
    assert after.count(b"RELEASE_PUBKEY = ") == before.count(b"RELEASE_PUBKEY = ")
    assert f'RELEASE_PUBKEY = "{pub}"'.encode() in after
    assert signing.RELEASE_PUBKEY.encode() not in after  # the official key is gone
    assert len(after) - len(before) == len(pub) - len(signing.RELEASE_PUBKEY)  # one line changed
    signing.verify(b"release", signing.sign(b"release", private), pub)
    if os.name == "posix":
        assert private.stat().st_mode & 0o777 == 0o600


def test_an_existing_private_key_is_never_replaced(tmp_path):
    module, private = _module_copy(tmp_path), tmp_path / "key.pem"
    private.write_text("mine", "utf8")
    before = module.read_bytes()
    with pytest.raises(SystemExit, match="exists"):
        signing.new_key(private=private, module=module)
    assert private.read_text("utf8") == "mine" and module.read_bytes() == before


def test_a_module_without_the_key_line_gets_no_private_key_either(tmp_path):
    module, private = tmp_path / "signing.py", tmp_path / "key.pem"
    module.write_text("PUBKEY = ''\n", "utf8")
    with pytest.raises(SystemExit, match="RELEASE_PUBKEY"):
        signing.new_key(private=private, module=module)
    assert not private.exists()
