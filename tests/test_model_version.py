"""`MODEL_VERSION` is the single string every certificate folds into its
`inputs_hash` (OBJECTIVE-v2.md §5). The spec header must name the SAME
string, or a certificate could claim a version the spec does not describe."""
from __future__ import annotations

import re
from pathlib import Path

from hbuilder.domain.model_version import MODEL_VERSION

REPO = Path(__file__).resolve().parents[1]


def test_model_version_matches_objective_v2_header():
    text = (REPO / "tools/recon/OBJECTIVE-v2.md").read_text(encoding="utf-8")
    m = re.search(r'`model_version = "([^"]+)"`', text)
    assert m, "OBJECTIVE-v2.md must open with `model_version = \"...\"`"
    assert m.group(1) == MODEL_VERSION


def test_model_version_is_versioned_string():
    assert re.fullmatch(r"objective-v\d+\.\d+(-[a-z]+)?", MODEL_VERSION)
