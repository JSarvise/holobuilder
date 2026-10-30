"""Down-levelling a card's skills to the level its bloom actually bought.

`data/cards.json` carries the LEVEL-2 magnitude for ACTIVE, SPECIAL and PASSIVE
alike (checked on every card, `collection.CARDS_JSON_SKILL_LEVEL`), so a card
below the bloom step that buys a kind would be scored with a skill it does not
have. `overstated_skills` detects this; restating `stats` alone is not enough.

⚠ **It is not a magnitude rescale.** Across the master's skill tables, of 170
ACTIVE skill ids with both levels, 38 change `duration_ms`
between level 1 and 2 and 2 change the activation probability outright; of 169
SPECIALs, 55 change `duration_ms`. Ninomae Ina'nis' 5★ ACTIVE is one of them —
same 450 permil at both levels, 13 s at level 1 against 14 s at level 2. So the
level-1 row REPLACES the slot; scaling `magnitude` alone would keep a duration
the card does not have and would look right while being wrong.

The acceptance case is the one the game settled to the unit: Ina'nis and Takane
Lui, both bloom 0, both `all_stats` PASSIVE reading 32 % in `data/cards.json`
and 24 % in the game.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import make_card
from hbuilder.domain.card import SkillSlot, skill_slot_from_record
from hbuilder.domain.collection import (
    CARDS_JSON_SKILL_LEVEL,
    SKILL_BLOOM_STEP,
    CollectionError,
    restate_card_skills,
    skill_ladder_from_mapping,
    skill_level_at_bloom,
)

# One real pair of rows, transcribed from the master: Ina'nis' 5★ PASSIVE, and
# her ACTIVE whose DURATION is what
# moves between the levels rather than its magnitude.
INA = "card-04003-5-uniq-0044-00"

_UNGATED = {"kind": "ungated"}
_ALL = {"kind": "all"}
_NONE = {"kind": "none"}


def _slot_record(**over):
    record = {
        "family": "stat_up",
        "effect_type": "all_stats",
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": 32.0,
        "condition": _UNGATED,
        "target": _ALL,
        "override": _NONE,
        "rider": _NONE,
    }
    record.update(over)
    return record


def _ladder_document():
    return {
        "schema_version": 1,
        "cards": {
            INA: {
                "PASSIVE": {
                    "1": _slot_record(magnitude=24.0),
                    "2": _slot_record(magnitude=32.0),
                },
                "ACTIVE": {
                    "1": _slot_record(
                        family="conditional_score_up",
                        effect_type="score",
                        magnitude=45.0,
                        duration=13.0,
                        interval=32.0,
                        probability_tier="High",
                    ),
                    "2": _slot_record(
                        family="conditional_score_up",
                        effect_type="score",
                        magnitude=45.0,
                        duration=14.0,
                        interval=32.0,
                        probability_tier="High",
                    ),
                },
            }
        },
    }


# --------------------------------------------------------------------------
# skill_level_at_bloom -- which level a card's skill of `kind` actually has
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,bloom,expected",
    [
        ("ACTIVE", 0, 1),
        ("ACTIVE", 1, 2),
        ("SPECIAL", 2, 1),
        ("SPECIAL", 3, 2),
        ("PASSIVE", 3, 1),
        ("PASSIVE", 4, 2),
        ("PASSIVE", 5, 2),
    ],
)
def test_skill_level_at_bloom_follows_the_bloom_step(kind, bloom, expected):
    assert skill_level_at_bloom(kind, bloom) == expected


def test_skill_level_at_bloom_is_unknown_when_the_bloom_is():
    """`None` is not zero. A card whose bloom the collection does not record
    must not be silently down-levelled to level 1 — that is the same fabricated
    -zero failure `AccountBonuses` is organised against."""
    assert skill_level_at_bloom("PASSIVE", None) is None


def test_skill_level_at_bloom_refuses_an_unknown_kind():
    with pytest.raises(KeyError):
        skill_level_at_bloom("COSTUME", 0)


def test_every_kind_reaches_the_level_cards_json_carries():
    """The ladder tops out at exactly the level `data/cards.json` holds — if a
    kind could reach level 3 the dataset would be UNDER-stating it and this
    whole module would have the sign backwards."""
    for kind, step in SKILL_BLOOM_STEP.items():
        assert skill_level_at_bloom(kind, step) == CARDS_JSON_SKILL_LEVEL


# --------------------------------------------------------------------------
# skill_ladder_from_mapping -- reading the side artefact
# --------------------------------------------------------------------------


def test_ladder_parses_into_skill_slots_keyed_by_int_level():
    ladder = skill_ladder_from_mapping(_ladder_document())
    slot = ladder[INA]["PASSIVE"][1]
    assert isinstance(slot, SkillSlot)
    assert slot.kind == "PASSIVE"
    assert slot.magnitude == 24.0


def test_ladder_is_absent_not_empty_when_the_document_is_none():
    """Same first-class-absent contract as `card_level_curve.json`: no export
    means nothing to restate, not a restatement to zero."""
    assert skill_ladder_from_mapping(None) == {}


def test_ladder_refuses_a_schema_version_it_does_not_know():
    document = _ladder_document()
    document["schema_version"] = 999
    with pytest.raises(CollectionError):
        skill_ladder_from_mapping(document)


# --------------------------------------------------------------------------
# restate_card_skills -- the substitution itself
# --------------------------------------------------------------------------


def _passive_card(card_id="c1", **over):
    return make_card(
        card_id, passive=skill_slot_from_record(_slot_record(**over), "PASSIVE")
    )


def test_a_bloom_0_card_gets_the_level_1_passive():
    card = _passive_card()
    ladder = skill_ladder_from_mapping(_ladder_document())
    restated, notes = restate_card_skills(card, bloom=0, ladder=ladder[INA])
    assert restated.skill("PASSIVE").magnitude == 24.0
    assert notes == ("PASSIVE 2->1",)


def test_a_bloom_4_card_keeps_the_level_2_passive_untouched():
    card = _passive_card()
    ladder = skill_ladder_from_mapping(_ladder_document())
    restated, notes = restate_card_skills(card, bloom=4, ladder=ladder[INA])
    assert restated is card
    assert notes == ()


def test_the_whole_slot_is_replaced_not_just_the_magnitude():
    """The falsified half. Ina's ACTIVE carries the SAME magnitude at both
    levels and a different DURATION — a magnitude-only rescale is a no-op here
    and leaves 14 s on a card that has 13 s."""
    card = make_card(
        "c1",
        active=skill_slot_from_record(
            _slot_record(
                family="conditional_score_up",
                effect_type="score",
                magnitude=45.0,
                duration=14.0,
                interval=32.0,
                probability_tier="High",
            ),
            "ACTIVE",
        ),
    )
    ladder = skill_ladder_from_mapping(_ladder_document())
    restated, _ = restate_card_skills(card, bloom=0, ladder=ladder[INA])
    assert restated.skill("ACTIVE").duration == 13.0
    assert restated.skill("ACTIVE").magnitude == 45.0


def test_an_unknown_bloom_changes_nothing():
    card = _passive_card()
    ladder = skill_ladder_from_mapping(_ladder_document())
    restated, notes = restate_card_skills(card, bloom=None, ladder=ladder[INA])
    assert restated is card
    assert notes == ()


def test_a_card_missing_from_the_ladder_is_reported_never_guessed():
    """The rule this project runs on: a card that cannot be restated keeps
    `data/cards.json`'s value and SAYS so. Silently scoring a bloom-0 card at
    level 2 is exactly the error this prevents."""
    card = _passive_card()
    restated, notes = restate_card_skills(card, bloom=0, ladder={})
    assert restated is card
    assert notes == ("PASSIVE: no level-1 row exported, left at level 2",)


def test_the_leader_costume_skill_is_never_down_levelled():
    """`LiveLeaderSkill` has ONE row per card — no level column at all. Bloom
    buys nothing on the outfit, so touching `costume_skill` here would invent a
    mechanic (the leader contributes only its outfit)."""
    card = make_card(
        "c1",
        passive=skill_slot_from_record(_slot_record(), "PASSIVE"),
        costume_skill=skill_slot_from_record(_slot_record(), "COSTUME"),
    )
    ladder = skill_ladder_from_mapping(_ladder_document())
    restated, _ = restate_card_skills(card, bloom=0, ladder=ladder[INA])
    assert restated.costume_skill == card.costume_skill


# --------------------------------------------------------------------------
# Acceptance: the real export, against the game's own number
# --------------------------------------------------------------------------

# ⚠ NOT `dumps/skill_levels.json`. That path may hold a stale file of another
# shape that carries no family/condition/target — so it cannot replace a slot.
# A distinct name means such a file can never be silently read as this one.
LADDER_PATH = Path("tools/recon/proxy/dumps/skill_ladder.json")


# The level-2-reproduces-the-catalogue self-check deliberately does NOT live
# here. `tests/test_fixture_isolation.py` forbids a test reaching the canonical
# `data/` files at all, and that rule is right: the check is an
# extraction-versus-catalogue agreement, a recon concern. It runs inside
# `import_to_cards.py`, where a disagreement is a non-zero exit rather than a
# printed note.


@pytest.mark.skipif(not LADDER_PATH.exists(), reason="skill_ladder.json not exported")
def test_apply_collection_down_levels_a_bloom_0_passive(tmp_path):
    """The wiring, end to end, against the REAL exported ladder.

    Hermetic on purpose: the card is built from the ladder's own level-2 rung —
    which `import_to_cards.py` has already proven identical to the catalogue's —
    and the collection is a temp file, so this touches no `data/` path. Without
    this wiring, `apply_collection` would restate `stats` only and hand the
    solver a 32 % skill on a card the game scores at 24 %."""
    from hbuilder.cli import apply_collection

    ladder = skill_ladder_from_mapping(
        json.loads(LADDER_PATH.read_text(encoding="utf-8"))
    )
    card = make_card(INA, passive=ladder[INA]["PASSIVE"][CARDS_JSON_SKILL_LEVEL])
    assert card.skill("PASSIVE").magnitude == 32.0  # what the catalogue carries

    document = tmp_path / "collection.json"
    document.write_text(
        json.dumps(
            {
                "schema_version": "collection-1",
                "card_default": "unknown",
                # exactly a real account's entry for this card
                "cards": {INA: {"bloom": 0, "level": 69, "limit_break": 3}},
            }
        ),
        encoding="utf-8",
    )

    restated, basis = apply_collection((card,), str(document))
    assert restated[0].skill("PASSIVE").magnitude == 24.0
    assert basis.skills_restated == 1
    assert basis.skills_unavailable == ()


@pytest.mark.skipif(not LADDER_PATH.exists(), reason="skill_ladder.json not exported")
def test_apply_collection_leaves_a_full_bloom_card_alone(tmp_path):
    """The other side of the same wiring: bloom 4 bought the PASSIVE, so the
    catalogue's level-2 magnitude is the right one and nothing moves."""
    from hbuilder.cli import apply_collection

    ladder = skill_ladder_from_mapping(
        json.loads(LADDER_PATH.read_text(encoding="utf-8"))
    )
    card = make_card(INA, passive=ladder[INA]["PASSIVE"][CARDS_JSON_SKILL_LEVEL])
    document = tmp_path / "collection.json"
    document.write_text(
        json.dumps(
            {
                "schema_version": "collection-1",
                "card_default": "unknown",
                "cards": {INA: {"bloom": 5, "level": 80, "limit_break": 4}},
            }
        ),
        encoding="utf-8",
    )

    restated, basis = apply_collection((card,), str(document))
    assert restated[0].skill("PASSIVE").magnitude == 32.0
    assert basis.skills_restated == 0


@pytest.mark.skipif(not LADDER_PATH.exists(), reason="skill_ladder.json not exported")
def test_ina_and_lui_passives_read_24_percent_at_level_1():
    """The acceptance case. Both cards are bloom 0 on a real account and the
    game scores their PASSIVE at 24 %, not the 32 % `data/cards.json`
    carries — `32/24 = 1.333`, the observed +32.5 % overstatement."""
    ladder = skill_ladder_from_mapping(
        json.loads(LADDER_PATH.read_text(encoding="utf-8"))
    )
    for card_id in (INA, "card-00036-5-uniq-0029-00"):
        assert ladder[card_id]["PASSIVE"][2].magnitude == 32.0
        assert ladder[card_id]["PASSIVE"][1].magnitude == 24.0
