"""Two questions, two answers: "what is my best team" and "what would it be maxed".

`data/cards.json` holds every card's FULLY-MAXED stats, so a ranking on it alone
answers the second question while looking like it answers the first.
`state/collection.json` supplies the real levels — and the maxed basis keeps the
second question askable on purpose.

Both are legitimate. The real basis says which team to field tonight; the maxed
basis says which cards are worth chasing, and it is the only basis on which
`--acquire` means anything at all (a card you do not own has no level to score).

A maxed account is a specific, computable thing, not a vibe:

* every card owned — so the pool is the whole catalogue, not `data/owned.json`
* every card at max level and max bloom — which is exactly what `data/cards.json`
  already holds, so the collection is simply not applied
* max bloom means **no skill down-levelling**: level-2 magnitudes are correct
  there, so the open question of how ACTIVE/SPECIAL magnitudes down-level
  cannot affect this basis at all
* Memory Bonus at its ceiling — 50 posters, `MEMORY_BONUS_MAX_POSTERS`
* Member Power-Up at the **roster** ceiling: the ladder summed over every card
  at its own max level, `max_member_power_up_permyriad`, which is derived from
  the card list and grows the day a card ships

⚠ **The Holomem Board is NOT maxed and must never silently pretend to be.** What
a maxed board even looks like is undecided — at character level 50 a Leader has
no choice to make, but which branches a MEMBER should buy depends on the song,
the cast and whether an event bonus is in play. So the board stays the account's
real one, and every maxed run says so rather than inventing a build.
"""

from __future__ import annotations

import pytest

from hbuilder.domain.collection import (
    MEMBER_POWER_UP_CAP_PERMYRIAD,
    MEMORY_BONUS_MAX_POSTERS,
    PLAYER_LEVEL_MAX,
    StatBasis,
    max_member_power_up_permyriad,
    maxed_account_bonuses,
)

LADDER_5 = {lvl: (0 if lvl < 40 else 8) for lvl in range(1, 81)}
LADDER_4 = {lvl: (0 if lvl < 30 else 5) for lvl in range(1, 71)}


def _basis() -> StatBasis:
    return StatBasis(
        level_mult={},
        bloom_permil={},
        cards={
            "five-a": ("grp-card-level-rarity_5-01", "pot-5", ()),
            "five-b": ("grp-card-level-rarity_5-01", "pot-5", ()),
            "four-a": ("grp-card-level-rarity_4-01", "pot-4", ()),
        },
        power_up_permyriad={
            "grp-card-level-rarity_5-01": LADDER_5,
            "grp-card-level-rarity_4-01": LADDER_4,
        },
    )


def test_posters_sit_at_the_ceiling():
    assert maxed_account_bonuses(_basis()).posters == MEMORY_BONUS_MAX_POSTERS


def test_player_level_sits_at_the_ceiling():
    assert maxed_account_bonuses(_basis()).level == PLAYER_LEVEL_MAX


def test_power_up_is_the_whole_rosters_ladder_not_the_owned_one():
    """Member bonus bounded by every card at max level: every card in the
    catalogue, each at its own max level."""
    bonuses = maxed_account_bonuses(_basis())
    assert bonuses.power_up_permyriad == 8 + 8 + 5
    assert bonuses.power_up_permyriad == max_member_power_up_permyriad(_basis())


def test_power_up_never_exceeds_the_declared_cap():
    """The roster bound is what is REACHABLE, the declared cap is what the
    mechanic ALLOWS, and the honest ceiling is the smaller of the two."""
    huge = StatBasis(
        level_mult={},
        bloom_permil={},
        cards={f"c{i}": ("grp", "pot", ()) for i in range(5000)},
        power_up_permyriad={"grp": {1: 8}},
    )
    assert maxed_account_bonuses(huge).power_up_permyriad == MEMBER_POWER_UP_CAP_PERMYRIAD


def test_power_up_is_absent_rather_than_zero_with_no_curve_export():
    """Same first-class-absent contract as everywhere else: no ladder means the
    tool does not know, not that a maxed account earns nothing."""
    assert maxed_account_bonuses(None).power_up_permyriad is None
    assert maxed_account_bonuses(None).posters == MEMORY_BONUS_MAX_POSTERS


def test_it_is_never_auto():
    """`"auto"` derives from the account's own cards. On a maxed basis there is
    no account to derive from — the value is a property of the catalogue — so
    the flag must be off or `apply_collection` would overwrite the ceiling with
    the real account's value."""
    assert not maxed_account_bonuses(_basis()).power_up_permyriad_is_auto


# --------------------------------------------------------------------------
# End to end through apply_collection
# --------------------------------------------------------------------------


def test_a_maxed_run_does_not_down_level_any_skill(tmp_path):
    """The point of the basis, and the reason it sidesteps skill down-levelling entirely: at max
    bloom every skill IS at level 2, so `data/cards.json`'s magnitudes are the
    right ones and nothing is restated."""
    import json

    from conftest import make_card
    from hbuilder.cli import apply_collection
    from hbuilder.domain.card import skill_slot_from_record

    record = {
        "family": "stat_up",
        "effect_type": "all_stats",
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": 32.0,
        "condition": {"kind": "ungated"},
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    card = make_card("c1", passive=skill_slot_from_record(record, "PASSIVE"))

    document = tmp_path / "collection.json"
    document.write_text(
        json.dumps(
            {
                "schema_version": "collection-1",
                "card_default": "unknown",
                "cards": {"c1": {"bloom": 0, "level": 1, "limit_break": 0}},
            }
        ),
        encoding="utf-8",
    )

    real, real_basis = apply_collection((card,), str(document))
    maxed, maxed_basis = apply_collection((card,), str(document), maxed=True)

    assert maxed[0].skill("PASSIVE").magnitude == 32.0
    assert maxed_basis.skills_restated == 0
    assert maxed_basis.maxed
    # ...and the real basis is untouched by the existence of the maxed one
    assert real_basis.maxed is False
    assert real[0] is not None


def test_a_maxed_run_says_the_board_is_still_the_real_one(tmp_path):
    """It must never be possible to read a maxed run and think the board was
    maxed too. The board stays real, and the run says so."""
    import json

    from conftest import make_card
    from hbuilder.cli import apply_collection

    document = tmp_path / "collection.json"
    document.write_text(
        json.dumps({"schema_version": "collection-1", "cards": {}}), encoding="utf-8"
    )
    _cards, basis = apply_collection((make_card("c1"),), str(document), maxed=True)
    text = " ".join(basis.lines()).lower()
    assert "board" in text
