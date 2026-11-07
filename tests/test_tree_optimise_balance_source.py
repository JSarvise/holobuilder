"""Which cube/hexa balance the board optimiser costs a plan against.

`tree_optimise.py` prices every purchase against the account's eight skill-tree
currencies, read from `dumps/inventory.json` (`inventory_snapshot.py`, a live
`User/Get`). The source is named with its capture time, and an incomplete or
unreadable file is UNKNOWN, never a partial or zero balance.

These fixtures are synthetic. The one test that reads the account's real dumps
skips when they are absent, which is the normal state of a fresh checkout.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hbuilder.errors import MissingInput

_REPO = Path(__file__).resolve().parents[1]
_REFRESH = _REPO / "tools" / "refresh"
_TREE_OPTIMISE = _REFRESH / "tree_optimise.py"
_DUMPS = _REPO / "tools" / "recon" / "proxy" / "dumps"


def _load_tree_optimise():
    """Imported by path — a recon script under `tools/`, not an installed
    package. Same shape as `tests/test_board_landing.py`."""
    spec = importlib.util.spec_from_file_location("tree_optimise", _TREE_OPTIMISE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tree_optimise"] = module
    spec.loader.exec_module(module)
    return module


try:
    _TREE = _load_tree_optimise()
except MissingInput as exc:  # gitignored account data missing
    _TREE = None
    _WHY = str(exc)
else:
    _WHY = ""

pytestmark = pytest.mark.skipif(
    _TREE is None, reason=f"tree_optimise needs gitignored account data: {_WHY}"
)


def _write_inventory(directory: Path, held: dict, captured_at: str) -> Path:
    path = directory / "inventory.json"
    path.write_text(
        json.dumps({"_captured_at": captured_at, "items": held}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_inventory_json_is_read_and_named_with_its_capture_time(tmp_path):
    """A source the reader cannot name is a source nobody can check — figures go
    stale silently precisely when the label stays truthful while the file
    behind it ages."""
    held = {item: 100 + index for index, item in enumerate(_TREE.CURRENCIES)}
    _write_inventory(tmp_path, held, "2026-08-09T02:50:04+0200")

    balance, source = _TREE.balance_from_disk(tmp_path)

    assert balance == held
    assert "inventory.json" in source
    assert "2026-08-09T02:50:04+0200" in source, (
        "the source must carry its capture time; a bare filename cannot be "
        "told apart from the same filename a week later"
    )


def test_a_partial_inventory_is_refused_rather_than_read_as_zero(tmp_path):
    """Seven of eight currencies is not a balance: treating the missing currency as
    zero would make an unaffordable plan look affordable, which is the one
    direction this cost model must never round."""
    partial = {item: 10 for item in _TREE.CURRENCIES[:-1]}
    _write_inventory(tmp_path, partial, "2026-08-09T02:50:04+0200")

    balance, source = _TREE.balance_from_disk(tmp_path)

    assert balance is None and source is None, (
        "an incomplete inventory means UNKNOWN, never a partial "
        f"balance; got {balance!r} from {source!r}"
    )


def test_an_unreadable_inventory_does_not_kill_the_run(tmp_path):
    """A corrupt `inventory.json` reads as UNKNOWN, it does not raise: a search
    must not die on a malformed cache."""
    (tmp_path / "inventory.json").write_text("{ this is not json", encoding="utf-8")

    balance, source = _TREE.balance_from_disk(tmp_path)

    assert balance is None and source is None


@pytest.mark.skipif(
    not (_DUMPS / "inventory.json").exists(),
    reason="dumps/ is gitignored account data",
)
def test_the_real_balance_matches_the_live_inventory_on_disk():
    """The regression itself: what the optimiser costs against must equal what
    `inventory_snapshot.py` last read off the wire."""
    document = json.loads((_DUMPS / "inventory.json").read_text(encoding="utf-8"))
    live = {k: int(v) for k, v in document["items"].items() if k in _TREE.CURRENCIES}
    if len(live) != len(_TREE.CURRENCIES):
        pytest.skip("the account's own inventory does not carry all eight currencies")

    balance, source = _TREE.balance_from_disk()

    assert balance == live, (
        "the optimiser is costing plans against a different balance than the "
        f"account's latest live read ({source})"
    )
