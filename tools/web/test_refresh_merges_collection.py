"""The page's refresh button must bring `state/collection.json` with it.

The refresh button runs the collection merge, so `state/collection.json`
reflects the account's state. A bare `refresh_all.py` from the command line
does not merge unless asked to.

Why it matters: a refresh WITHOUT the merge leaves the collection stale while
every derived dump moves forward. The solver then goes on scoring a card (e.g.
Luna 5*, `card-00028-5-uniq-0023-00`) at **level 1** while the account has her
at **70** — so the tool's best-team answer silently cannot recommend her. A
staleness probe that only compared the SET of card ids would not notice,
because she is already in the set.

The merge is the single authorised writer (`account_snapshot.py
--merge-collection` -> `collection_from_snapshot`): it guesses nothing, every
value is the account's own read back from `User/Get`, and it backs up once to
`dumps/collection_before_import.json`. The button only decides WHO invokes it.

    python -m pytest tools/web/test_refresh_merges_collection.py
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_the_refresh_route_passes_merge_collection():
    """A structural check on the source, deliberately: `_refresh` shells out to
    a subprocess that spends the account credential, so a test that RAN it would
    hit the live game. What can be checked without the network is that the argv
    the handler builds carries the flag."""
    source = (REPO / "tools" / "web" / "server.py").read_text(encoding="utf-8")
    body = source.split("def _refresh(self)", 1)[1].split("\n    def ", 1)[0]
    assert "refresh_all.py" in body
    assert "--merge-collection" in body, (
        "the refresh button must merge the account into state/collection.json; "
        "without it the solver keeps scoring stale card levels and says nothing"
    )
    # ...and it must reach the subprocess, not merely appear in a comment.
    argv = re.search(r"\[\s*sys\.executable[^\]]*\]", body, re.S)
    assert argv is not None, "could not find the subprocess argv in _refresh"
    assert "--merge-collection" in argv.group(0)


def test_refresh_all_still_defaults_the_flag_OFF_for_the_command_line():
    """The button runs the merge; the command line does not by default. A bare
    `python refresh_all.py` must NOT write the account's collection, so the
    exception stays one explicit caller rather than becoming the default."""
    source = (REPO / "tools" / "recon" / "proxy" / "refresh_all.py").read_text(
        encoding="utf-8"
    )
    marker = 'ap.add_argument(\n        "--merge-collection",'
    block = source.split(marker, 1)
    assert len(block) == 2, "could not find the --merge-collection argparse block"
    flag = block[1].split(")", 1)[0]
    assert 'action="store_true"' in flag
    assert "default=True" not in flag
