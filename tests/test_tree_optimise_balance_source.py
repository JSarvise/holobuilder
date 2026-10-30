"""Which cube/hexa balance the board optimiser costs a plan against.

`tree_optimise.py` prices every purchase against the account's eight skill-tree
currencies. Where that balance is READ FROM decides whether a plan answers a
question about today or about whenever the last packet capture happened to be
taken.

⛔ `inventory_snapshot.py` writes the live figure to `dumps/inventory.json`.
Reading only the `*.mitm` captures misses it, and the optimiser then reports,
say, Red Cube 5,013 / Blue Cube 227 from the newest capture while the account
actually holds 5,288 / 292. Nothing looks wrong: the tool names its source and
the source is real. It simply is not the freshest one on disk.

These fixtures are synthetic. The one test that reads the account's real dumps
skips when they are absent, which is the normal state of a fresh checkout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_PROXY = _REPO / "tools" / "recon" / "proxy"
_TREE_OPTIMISE = _PROXY / "tree_optimise.py"
_DUMPS = _PROXY / "dumps"


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
except SystemExit as exc:  # gitignored account data missing
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


def test_inventory_json_wins_when_it_is_newer_than_every_capture(tmp_path):
    """The live snapshot is preferred, and the run SAYS so.

    A source the reader cannot name is a source nobody can check — figures go
    stale silently precisely when the label stays truthful while the file
    behind it ages."""
    held = {item: 100 + index for index, item in enumerate(_TREE.CURRENCIES)}
    _write_inventory(tmp_path, held, "2026-08-09T02:50:04+0200")
    stale = tmp_path / "old_capture.mitm"
    stale.write_bytes(b"not a real capture")
    os.utime(stale, (1, 1))  # the capture is older than the inventory

    balance, source = _TREE.balance_from_disk(tmp_path)

    assert balance == held
    assert "inventory.json" in source
    assert "2026-08-09T02:50:04+0200" in source, (
        "the source must carry its capture time; a bare filename cannot be "
        "told apart from the same filename a week later"
    )


def test_a_partial_inventory_is_refused_rather_than_read_as_zero(tmp_path):
    """Seven of eight currencies is not a balance.

    Falling back is right; silently treating the missing currency as zero would
    make an unaffordable plan look affordable, which is the one direction this
    cost model must never round."""
    partial = {item: 10 for item in _TREE.CURRENCIES[:-1]}
    _write_inventory(tmp_path, partial, "2026-08-09T02:50:04+0200")

    balance, source = _TREE.balance_from_disk(tmp_path)

    assert balance is None and source is None, (
        "no capture and an incomplete inventory means UNKNOWN, never a partial "
        f"balance; got {balance!r} from {source!r}"
    )


def test_an_unreadable_inventory_does_not_kill_the_run(tmp_path):
    """A corrupt `inventory.json` degrades to the captures, it does not raise.

    `--plan` is a long run; aborting it on a malformed cache would trade a
    recoverable staleness for a total failure."""
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
