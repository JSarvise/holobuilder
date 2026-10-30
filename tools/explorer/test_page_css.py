"""The generated page's CSS must actually parse.

Why this exists, and it is not hypothetical. A comment block in `PAGE` was edited so that a `*/`
landed in the middle of it. Everything after that point became raw text in the stylesheet, the
browser discarded rules until it resynchronised, and the card faces lost `aspect-ratio` -- they
rendered as tall slivers with the art blown up inside them. Nothing else caught it:

  - `python build_explorer.py` exited 0 and reported its usual card and byte counts;
  - the structural checks all passed -- the strings were present, the file was written;
  - the tests were green.

The generator has no CSS parser and does not need one. What it needs is the three cheap invariants
that this specific failure violates, checked against the ARTEFACT rather than the template:
balanced comments, balanced braces, and the presence of the handful of rules the card face cannot
be correct without.

`POSTMORTEM.md` 1a: a pipeline reporting its own success proves nothing.

Run: `python -m pytest tools/explorer/test_page_css.py`
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "cards_explorer.html")

# Rules the card face is wrong without. Each is here because losing it has a visible consequence,
# not because it happens to exist today.
REQUIRED = {
    # the 5:7 card shape; without it the face collapses to whatever the art's ratio is
    r"\.face\s*\{[^}]*aspect-ratio\s*:\s*5\s*/\s*7": "the 5:7 face ratio",
    # The art element must declare SOME object-fit. Which one is a modelling question that has
    # already changed once (cover -> fill, when the game's own code showed the card art is a
    # RawImage stretched full-bleed), so the guard checks that the rule exists, not which value it
    # carries. Pinning the value here would make an intended model change look like a regression.
    r"\.face\s+\.port\s*\{[^}]*object-fit\s*:\s*\w+": "the card art fit rule",
    # the art must be clipped to the rounded card, or square corners leak past it
    r"\.face\s+\.port\s*\{[^}]*border-radius": "the card art corner radius",
    # the white inner frame, which is a real element because an inset shadow paints under children
    r"\.face\s+\.ring\s*\{[^}]*box-shadow\s*:\s*inset": "the white inner frame",
}


def _css(html: str) -> str:
    blocks = re.findall(r"<style>(.*?)</style>", html, re.S)
    assert blocks, "the page has no <style> block at all"
    return "\n".join(blocks)


@pytest.fixture(scope="module")
def css() -> str:
    if not os.path.exists(PAGE):
        # The page is gitignored and absent in a fresh worktree. Build it if the inputs are there;
        # skip with a clear reason if they are not, rather than failing for the wrong cause.
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "build_explorer.py")],
            capture_output=True, text=True, cwd=HERE,
        )
        if proc.returncode != 0 or not os.path.exists(PAGE):
            pytest.skip(f"cannot build the page here (extracted data absent): {proc.stderr[-300:]}")
    with open(PAGE, encoding="utf-8") as fh:
        return _css(fh.read())


def test_comments_are_balanced(css: str) -> None:
    """No stray `*/`, and no `/*` left open.

    This is the exact defect: text intended as a comment ending up outside one. Scanned by hand
    rather than by regex because CSS comments do not nest and a regex cannot report *where*.
    """
    depth = 0
    i = 0
    problems: list[str] = []
    while i < len(css) - 1:
        pair = css[i:i + 2]
        if pair == "/*":
            if depth:
                problems.append(f"nested /* at offset {i}: ...{css[max(0, i - 60):i + 20]!r}")
            depth += 1
            i += 2
            continue
        if pair == "*/":
            if not depth:
                problems.append(f"stray */ at offset {i}: ...{css[max(0, i - 200):i + 20]!r}")
            else:
                depth -= 1
            i += 2
            continue
        i += 1
    assert not problems, "unbalanced CSS comments:\n" + "\n".join(problems[:3])
    assert depth == 0, f"{depth} CSS comment block(s) left open"


def test_braces_are_balanced_outside_comments(css: str) -> None:
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    opens, closes = stripped.count("{"), stripped.count("}")
    assert opens == closes, f"CSS braces unbalanced: {opens} open, {closes} close"


@pytest.mark.parametrize("pattern,what", sorted(REQUIRED.items(), key=lambda kv: kv[1]))
def test_required_rule_survives(css: str, pattern: str, what: str) -> None:
    """Present in the OUTPUT, not merely in the template.

    A broken comment leaves the source string looking perfectly fine; only the emitted stylesheet
    shows the rule has been swallowed.
    """
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert re.search(pattern, stripped, re.S), f"{what} is missing from the generated CSS"


def test_no_raw_prose_between_rules(css: str) -> None:
    """Catch the failure one step earlier than the balance check does.

    When a comment breaks open, its prose lands between rules. A CSS declaration block is always
    `selector { ... }`, so any run of text outside a comment that contains no brace, no colon and
    several spaces is prose that escaped. Checked on the comment-stripped stylesheet.
    """
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    outside = re.sub(r"\{[^{}]*\}", "", stripped)          # drop declaration bodies
    for line in outside.splitlines():
        line = line.strip()
        if not line or line.startswith(("@", ".", "#", ":", ">", "*", "[")) or line.endswith(","):
            continue
        if "{" in line or "}" in line or ":" in line:
            continue
        assert line.count(" ") < 4, f"prose escaped a comment into the stylesheet: {line[:120]!r}"
