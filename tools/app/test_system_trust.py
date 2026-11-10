"""HTTPS in the app and the installer is verified by Windows (system_trust.py)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _run(code: str) -> str:
    env = dict(os.environ, PYTHONPATH=str(HERE))
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    ).stdout.strip()


def test_use_system_trust_makes_every_ssl_context_a_truststore_context():
    got = _run(
        "import ssl, system_trust\n"
        "print(system_trust.use_system_trust(), type(ssl.create_default_context()).__module__)"
    )
    assert got == "True truststore._api"


def test_an_h2_style_context_still_takes_alpn_after_the_switch():
    got = _run(
        "import ssl, system_trust\nsystem_trust.use_system_trust()\n"
        "ctx = ssl.create_default_context(); ctx.set_alpn_protocols(['h2']); print('ok')"
    )
    assert got == "ok"


def test_the_runtime_sitecustomize_turns_it_on_and_never_raises():
    sys.path.insert(0, str(HERE))
    import build_app  # noqa: PLC0415

    assert any(p.startswith("truststore==") for p in build_app.PACKAGES)
    got = _run(build_app.SITECUSTOMIZE + "import ssl\nprint(type(ssl.create_default_context()).__module__)")
    assert got == "truststore._api"
