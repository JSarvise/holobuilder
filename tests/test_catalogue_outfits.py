"""Every card the game ships has an outfit — including the 54 3★ ones.

The extractor can get this wrong without anything visible failing, because
`data/cards.json` carries the right answer by hand and the wrong answer would
only exist in `dumps/cards_import_draft.json`.

The mechanism: resolving the outfit as
`Card.f23 -> Costume -> Costume.f4 -> LiveLeaderSkill` fails because
**`Card.f23` is blank on every 3★ card**. It is blank for a real reason — a 3★
wears the character's `Default` costume rather than a card-specific one, so the
card names no costume of its own — but the Costume row and its LiveLeaderSkill
both exist regardless, and that skill is worth **+15.0 % all stats, ungated,
target `all`**, identical on all 54 of them. Resolving instead by inverting
`Costume.f4` (which spells `live_leader_skill-<card id>`, so it carries the
whole card id) covers **169/169** and agrees with `Card.f23` on all 115 cards
where f23 is populated. See `build_cards_dataset._costume_by_card`.

Why this is worth a test rather than a comment: an automated path may **ADD** a
card the game ships to `data/cards.json`, but never modify one. A newly shipped
3★ added with `costume_skill: null` would silently miss a team-wide +15 %, and
no automated path could go back and fix it.

The assertion is deliberately **structural** (every card has an outfit) rather
than a pin on the 3★ magnitude or on 169. `test_mode_b` already records why this
codebase distrusts literals of that shape: they pin a date, not an invariant,
and they fail while nothing is broken.
"""

from __future__ import annotations

import json
from pathlib import Path

from hbuilder.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
# From the CLI's own default, not a literal — `test_fixture_isolation` forbids a
# hardcoded `data/` path in `tests/`, and this module is deliberately one of the
# few that asserts something about the real catalogue.
CARDS_PATH = REPO_ROOT / build_parser().parse_args(["solve"]).cards
ENRICHED_PATH = (
    REPO_ROOT / "tools" / "recon" / "proxy" / "dumps" / "cards_enriched.json"
)


def _catalogue() -> list[dict]:
    return json.loads(CARDS_PATH.read_text(encoding="utf-8"))["cards"]


def test_every_catalogue_card_ships_an_outfit():
    without = [c["id"] for c in _catalogue() if c.get("costume_skill") is None]
    assert without == [], (
        "every card the game ships carries a LiveLeaderSkill — a card with no "
        "costume_skill means the outfit was DROPPED on the way in, not that the "
        f"game omits one: {without}"
    )


def test_the_extraction_agrees_with_the_catalogue_on_which_cards_have_an_outfit():
    """The regression guard proper: the catalogue is hand-maintained and the
    extraction is machine-derived, so they are two independent statements about
    the same fact. `dumps/` is gitignored, so a fresh checkout skips this and
    keeps the structural test above."""
    if not ENRICHED_PATH.exists():
        import pytest

        pytest.skip("dumps/cards_enriched.json not present (gitignored recon data)")

    enriched = json.loads(ENRICHED_PATH.read_text(encoding="utf-8"))
    rows = enriched["cards"] if isinstance(enriched, dict) else enriched
    extracted_with_outfit = {
        row["id"] for row in rows if (row.get("costume") or {}).get("skill")
    }
    catalogue_with_outfit = {
        c["id"] for c in _catalogue() if c.get("costume_skill") is not None
    }
    missing = sorted(catalogue_with_outfit - extracted_with_outfit)
    assert missing == [], (
        "the extraction dropped an outfit the catalogue has — an auto-added card "
        f"would ship without it and no automated path may fix it later: {missing}"
    )
