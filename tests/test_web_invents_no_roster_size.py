"""The web page may not state a roster size it was not told.

`collection.js` read *"above the ... % the current 167-card roster can reach"*
— two lines below a comment that said this value "is DERIVED from the current
roster by the server and moves when a card ships". By then the roster was 169.
The page had restated a number whose whole documented property is that it moves.

The server now sends `power_up_reachable_roster_size` beside the figure it
describes, so the sentence is built from the same response as the percentage in
it. This test is the guard on that: a structural scan for a hardcoded
"<digits>-card roster" anywhere in the page's scripts.

It is a text scan and not an AST walk, unlike `test_fixture_isolation`, because
the target is JavaScript and the shape being forbidden is a rendered SENTENCE
rather than a path constant — a template literal is exactly how it would come
back, and that is what this catches.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "tools" / "web" / "static"

# "167-card roster", "169 card roster" — a literal count in the phrase itself.
_HARDCODED_ROSTER = re.compile(r"\d+[-\s]card roster", re.IGNORECASE)


def test_no_script_hardcodes_a_roster_size_in_its_prose():
    offenders: list[str] = []
    for path in sorted(STATIC_DIR.rglob("*.js")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # A comment may say what the old bug was; rendered text may not.
            stripped = line.lstrip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if _HARDCODED_ROSTER.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")

    assert not offenders, (
        "a page script names a roster size as a literal. The size is derived "
        "server-side and moves whenever the game ships a card; take it from "
        "`power_up_reachable_roster_size` in the /api/collection response "
        f"instead: {offenders}"
    )


def test_the_server_sends_the_roster_size_beside_the_figure_it_describes():
    """The two must travel together — a page given the percentage but not the
    roster it was summed over has no honest way to write the sentence."""
    source = (REPO_ROOT / "tools" / "web" / "server.py").read_text(encoding="utf-8")
    assert '"power_up_reachable_permyriad"' in source
    assert '"power_up_reachable_roster_size"' in source
