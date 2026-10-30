"""The page's vocabulary must be the data's vocabulary.

A stylesheet rule that names a state the data never produces fails silently.
For example, a rule dimming a skill that paid nothing, written as
`.state-not_applied, .state-gate_unmet`, matches nothing: **neither of those
states exists**. `scoring.CONTRIBUTION_STATES` is `applied`, `gate_not_met`,
`data_missing`. A skill contributing nothing would then render in exactly the
same colour as one contributing everything — the single distinction the rule
is meant to draw, silently absent, in a table whose whole job is explaining
why a number is what it is.

Reading either file alone shows nothing wrong. Only holding them against each
other does, which is what this module does.

Both directions matter and they fail differently:

* a token the page does not know renders as a raw slug or an unstyled chip —
  visible, ugly, harmless;
* a token the DATA does not know is a dead rule that reads as working — the
  defect above, and the reason this test is not just a completeness check.

The plain-language layer these guard translates the raw terms and keeps the
raw term beside them, so the page stays traceable back to the file.
"""

from __future__ import annotations

import re
from pathlib import Path

from hbuilder.domain.schema import EFFECT_TYPES_BY_FAMILY
from hbuilder.domain.scoring import CONTRIBUTION_STATES

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "tools" / "web" / "static"
RANKING_JS = STATIC_DIR / "ranking.js"
STYLE_CSS = STATIC_DIR / "style.css"


def _js_object_keys(source: str, name: str) -> set[str]:
    """The keys of a top-level `const NAME = { ... };` object literal."""
    start = source.index(f"const {name} = {{")
    body = source[start : source.index("\n};", start)]
    return set(re.findall(r'^\s*"?([A-Za-z_/]+)"?\s*:', body, re.MULTILINE))


def _css_state_classes(source: str) -> set[str]:
    """State classes in the RULES, not in the prose.

    Comments are stripped first: a comment may name dead classes in order to
    explain a mistake, and a scan that counted those would report the very
    defect the comment is documenting. Prose stays
    free to name a mistake; only the selectors are the claim.
    """
    rules = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return set(re.findall(r"\.state-([a-z_]+)", rules))


def test_every_family_and_effect_pair_the_schema_allows_has_a_plain_phrase():
    expected = {
        f"{family}/{effect}"
        for family, effects in EFFECT_TYPES_BY_FAMILY.items()
        for effect in effects
    }
    have = _js_object_keys(RANKING_JS.read_text(encoding="utf-8"), "EFFECT_PLAIN")

    assert expected <= have, (
        "the breakdown table would print a raw family/effect token with no "
        f"plain restatement beside it: {sorted(expected - have)}"
    )


def test_the_plain_phrases_name_no_family_or_effect_the_schema_does_not_have():
    """A phrase for a pair that cannot occur is a claim about a mechanic this
    project does not model — dead text that reads as documentation."""
    expected = {
        f"{family}/{effect}"
        for family, effects in EFFECT_TYPES_BY_FAMILY.items()
        for effect in effects
    }
    have = _js_object_keys(RANKING_JS.read_text(encoding="utf-8"), "EFFECT_PLAIN")

    assert have <= expected, f"phrases for pairs the schema forbids: {sorted(have - expected)}"


def test_every_contribution_state_has_a_plain_phrase_and_a_tooltip():
    source = RANKING_JS.read_text(encoding="utf-8")
    plain = _js_object_keys(source, "STATE_PLAIN")
    titles = _js_object_keys(source, "STATE_TITLE")

    assert set(CONTRIBUTION_STATES) <= plain, (
        f"states with no plain wording: {sorted(set(CONTRIBUTION_STATES) - plain)}"
    )
    assert set(CONTRIBUTION_STATES) <= titles, (
        f"states with no explanation on hover: {sorted(set(CONTRIBUTION_STATES) - titles)}"
    )


def test_the_stylesheet_colours_exactly_the_states_that_exist():
    """The defect this module was written for, in both directions.

    A missing rule means a state renders undistinguished. A rule for a state
    that cannot occur is worse: it looks like the distinction is being drawn
    when nothing is drawing it.
    """
    styled = _css_state_classes(STYLE_CSS.read_text(encoding="utf-8"))
    states = set(CONTRIBUTION_STATES)

    assert states <= styled, f"states with no colour rule: {sorted(states - styled)}"
    assert styled <= states, (
        "the stylesheet colours a contribution state that scoring.py never "
        f"emits — a dead rule that reads as working: {sorted(styled - states)}"
    )
