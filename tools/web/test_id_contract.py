"""Every element id the page's JavaScript reaches for must exist in the HTML.

⛔ **The failure this prevents is silent.** `document.getElementById` returns
`null` for an id that is not there — it does not throw. The module evaluates,
the page renders, and one control simply does nothing when clicked. There is no
console error and no visual difference from a control that is merely disabled.
`test_js_syntax.py` guards the loud version of this (a broken string literal
takes the WHOLE module down, which at least announces itself); this guards the
quiet one.

It exists because `index.html` is about to be restructured into numbered
sections and `app.js` addresses everything by id. A rename or a dropped element
during that move is invisible until someone presses the button.

The check is deliberately textual, not a DOM parse: the page is hand-written
HTML with no build step, so the ids in the file ARE the ids in the browser.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent / "static"
INDEX = STATIC / "index.html"

# `app.js` defines `const $ = (id) => document.getElementById(id)`, so both
# spellings are lookups and a check that knew only one would pass while half
# the page went unchecked.
_LOOKUP = re.compile(r"""(?:getElementById|\$)\(\s*["']([A-Za-z0-9_-]+)["']\s*\)""")
# Ids the scripts CREATE are not required to pre-exist. Two spellings both
# create an element the moment they run: `el.id = "noiseband"`, and an
# `id="scoremax"` written straight into an `innerHTML` string (it exists as soon
# as that HTML is inserted, the same as a static one). Excluding both by pattern
# rather than a hand-kept list means a new dynamic element does not need this
# file edited to stay green.
_CREATED = re.compile(r"""\.id\s*=\s*["']([A-Za-z0-9_-]+)["']""")
_HTML_ID = re.compile(r"""\bid=["']([A-Za-z0-9_-]+)["']""")
# The same `id="..."` shape, used against the SCRIPT source to find ids it
# builds in markup strings — not only against index.html.
_CREATED_HTML = _HTML_ID


def _scripts() -> list[Path]:
    return sorted(STATIC.glob("*.js"))


def _html_ids() -> set[str]:
    return set(_HTML_ID.findall(INDEX.read_text(encoding="utf-8")))


@pytest.mark.parametrize("script", _scripts(), ids=lambda p: p.name)
def test_every_id_the_script_looks_up_exists_in_the_html(script):
    """A lookup that misses is a control that does nothing, quietly."""
    source = script.read_text(encoding="utf-8")
    created = set(_CREATED.findall(source)) | set(_CREATED_HTML.findall(source))
    wanted = set(_LOOKUP.findall(source)) - created
    available = _html_ids()

    missing = sorted(wanted - available)
    assert not missing, (
        f"{script.name} looks up {len(missing)} id(s) that index.html does not "
        f"define, so each is a silent no-op: {missing}"
    )


def test_the_html_defines_ids_at_all():
    """A guard on the guard.

    If the regex or the file ever stops matching, every assertion above passes
    vacuously — an empty `wanted` set is a subset of anything. This makes that
    failure mode loud instead of green."""
    ids = _html_ids()
    assert len(ids) > 40, (
        f"index.html defines only {len(ids)} ids; the id contract test above "
        "is passing vacuously"
    )
    looked_up = set()
    for script in _scripts():
        source = script.read_text(encoding="utf-8")
        looked_up |= set(_LOOKUP.findall(source)) - set(_CREATED.findall(source))
    assert len(looked_up) > 40, (
        f"the scripts look up only {len(looked_up)} ids; the pattern has "
        "stopped matching and the contract is unchecked"
    )
