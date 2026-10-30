"""chart_download.Asset.verify: a decrypted chart must start with a SUS header -- v1
(`#CREATE_APP_`) or v2 (`#VISION_SUS_FORMAT_VERSION`, used e.g. by m0366, whose body parses
exactly). No network."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import chart_download as cd  # noqa: E402


@pytest.mark.parametrize("body,ok", [
    (b"#CREATE_APP_VERSION 1.0.101\n#MUSIC_ID m0365\n", True),
    (b"#VISION_SUS_FORMAT_VERSION 2\n#CREATE_APP_VERSION \"1.2.100\"\n", True),
    (b"\x00garbage", False),
])
def test_verify_accepts_both_sus_headers(monkeypatch, body, ok):
    served = b"served-bytes"
    monkeypatch.setattr(cd, "decrypt_resource", lambda data, name: body)
    a = cd.Asset("chart_m0366_expert.sus", hashlib.md5(served).hexdigest(), "tok", len(served))
    if ok:
        assert a.verify(served) == body
    else:
        with pytest.raises(ValueError):
            a.verify(served)
