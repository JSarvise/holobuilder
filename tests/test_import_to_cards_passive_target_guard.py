"""D-32 (Phase 03.1 plan 06): `import_to_cards.py::_slot` must RAISE rather than write `all`
for a PASSIVE slot whose target resolves to the team-wide marker. Across all 167 real
extracted cards a PASSIVE target never resolves to `all` (38 self, 65 type-max-2, 32
type-max-3, 32 generation-max-2) -- so this guard is exercised here directly rather than
relying on "the current data happens not to violate it".

`tools/recon/proxy/` is outside `src/hbuilder/`, so it is imported here via an explicit
`sys.path` insert rather than a package-relative import -- the module itself does the same
thing to reach `hbuilder.domain.schema` (see its own `sys.path.insert` at import time).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_DIR = REPO_ROOT / "tools" / "recon" / "proxy"
if str(PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(PROXY_DIR))

import import_to_cards  # noqa: E402


def _fresh_loss() -> dict:
    return {"costume_name_unknown": [], "target_unmapped": []}


def _passive_sk(target_kind: str, target_extra: dict | None = None) -> dict:
    target = {"kind": target_kind, "target": None, "max": None}
    if target_extra:
        target.update(target_extra)
    return {
        "effect": {
            "effect": "performance_up",
            "magnitude_permil": 140,
            "value": None,
            "target": target,
            "raw": "test-fixture",
        },
        "gate": {
            "kind": "type",
            "target": "attribute_1",
            "count": 2,
            "raw": "test-fixture-gate",
        },
    }


def test_a_passive_target_resolving_to_all_raises_rather_than_being_written():
    sk = _passive_sk("all")
    loss = _fresh_loss()

    with pytest.raises(ValueError) as exc_info:
        import_to_cards._slot("PASSIVE", sk, loss, "test_card_d32_01")

    message = str(exc_info.value)
    assert "test_card_d32_01" in message
    assert "PASSIVE" in message
    assert "all" in message


def test_a_passive_target_of_type_kind_does_not_raise_and_is_represented():
    sk = _passive_sk("type", {"target": "attribute_1", "max": 2})
    loss = _fresh_loss()

    slot = import_to_cards._slot("PASSIVE", sk, loss, "test_card_d32_02")

    assert slot is not None
    assert slot["target"] == {"kind": "type", "value": "Cute", "count": 2}
    assert loss["target_unmapped"] == []


def test_a_passive_target_of_self_kind_does_not_raise_and_is_represented():
    sk = _passive_sk("self")
    loss = _fresh_loss()

    slot = import_to_cards._slot("PASSIVE", sk, loss, "test_card_d32_03")

    assert slot is not None
    assert slot["target"] == {"kind": "self"}


def test_an_unmapped_passive_target_kind_emits_null_and_is_counted_never_guessed():
    sk = _passive_sk("unknown", {"target": "some-unparseable-string"})
    loss = _fresh_loss()

    slot = import_to_cards._slot("PASSIVE", sk, loss, "test_card_d32_04")

    assert slot is not None
    assert slot["target"] is None
    assert len(loss["target_unmapped"]) == 1
    assert loss["target_unmapped"][0][0] == "test_card_d32_04"
    assert loss["target_unmapped"][0][1] == "PASSIVE"
