"""The card extractor reads every field a skill row carries, and a card it cannot render
faithfully is never added to the catalogue.

Two cards carried fields nobody read: Sakura Miko 0120's and
Himemori Luna 0121's ACTIVE gate (`LiveActiveSkillLevel` f3, "With 2 or more Cute Type
Members"), and Luna's PASSIVE second effect (`LivePassiveSkillLevel` f5/f6). Miko was added
with her ACTIVE ungated. Two guards now stand in front of the catalogue, both read from the
game rather than from this translator: a populated field outside `skills.READ_FIELDS`, and the
game's own English skill text naming a different number of gates than the slot carries.
"""

from __future__ import annotations

import sys
from pathlib import Path

REFRESH_DIR = Path(__file__).resolve().parents[1] / "tools" / "refresh"
if str(REFRESH_DIR) not in sys.path:
    sys.path.insert(0, str(REFRESH_DIR))

import card_append  # noqa: E402
import import_to_cards as itc  # noqa: E402
import skills  # noqa: E402

CUTE2 = "live_skill_trigger-deck_card_attribute-attribute_1-2"
CUTE3 = "live_skill_trigger-deck_card_attribute-attribute_1-3"


def _active_row(extra=None):
    row = {
        1: ["live_active_skill-x"],
        2: [2],
        4: ["live_active_skill_effect-score_up_permil_up-1400"],
        7: [22000],
        8: [460],
        10: [7000],
        1000: ["desc"],
    }
    row.update(extra or {})
    return row


def test_an_active_reads_its_principal_gate():
    got = skills.resolve_active(_active_row({3: [CUTE2]}))
    assert got["gate"] == {"kind": "type", "target": "attribute_1", "count": 2, "raw": CUTE2}
    assert got["unread"] == []
    assert skills.resolve_active(_active_row())["gate"] is None


def test_a_passive_reads_its_second_effect():
    row = {
        1: ["live_passive_skill-x"],
        2: [2],
        3: [CUTE3],
        4: [
            "live_passive_skill_effect-live_active_skill_activation_probability_up_permil_up-190-live_skill_effect_target-attribute-attribute_1-2"
        ],
        5: [CUTE3],
        6: [
            "live_passive_skill_effect-sense_up_permil_up-170-live_skill_effect_target-attribute-attribute_1-2"
        ],
        1000: ["desc"],
    }
    got = skills.resolve_passive(row)
    assert got["additional"]["effect"]["effect"] == "sense_up"
    assert got["additional"]["effect"]["magnitude_permil"] == 170
    assert got["unread"] == []


def test_a_field_nobody_reads_is_reported():
    assert skills.resolve_active(_active_row({11: [5]}))["unread"] == [11]


def _enriched(active, passive, descriptions):
    return {
        "id": "card-x",
        "skills_by_level": {"ACTIVE": {"2": active}, "PASSIVE": {"2": passive}},
        "costume": {"skill": None},
        "descriptions": descriptions,
    }


def test_the_games_text_and_the_slot_must_name_the_same_gates():
    active = skills.resolve_active(_active_row({3: [CUTE2]}))
    passive = skills.resolve_passive(
        {
            1: ["p"],
            2: [2],
            3: [CUTE3],
            4: [
                "live_passive_skill_effect-sense_up_permil_up-170-live_skill_effect_target-attribute-attribute_1-2"
            ],
        }
    )
    text = {
        "ACTIVE": "With 2 or more Cute Type Members, For 7s, Score UP 140%. Every 22s with a Medium Probability chance.",
        "PASSIVE": "With 3 or more Cute Type Members, Grants Sense UP 17% to 2 Cute Type Members.",
        "SPECIAL": "For 9s, Grants Score Support Effect of 110%. GOOD or higher becomes PERFECT.",
    }
    loss = {"costume_name_unknown": [], "target_unmapped": []}
    record = {
        "skills": {
            "ACTIVE": itc._slot("ACTIVE", active, loss, "card-x"),
            "PASSIVE": itc._slot("PASSIVE", passive, loss, "card-x"),
        }
    }
    assert record["skills"]["ACTIVE"]["condition"] == {"kind": "type", "target": "Cute", "count": 2}
    assert itc.untranslated(_enriched(active, passive, text), record) == []
    # the same text over an ungated ACTIVE (Miko 0120 as first imported): reported
    ungated = dict(record["skills"]["ACTIVE"], condition={"kind": "ungated"})
    why = itc.untranslated(
        _enriched(active, passive, text), {"skills": {**record["skills"], "ACTIVE": ungated}}
    )
    assert why == ["ACTIVE: the game's text names 1 gate(s), the slot carries 0"]


def test_a_passive_with_a_stat_and_a_rate_effect_leads_with_the_stat():
    """The stat effect is the slot's own (the stat passives' recipients rule); the
    activation rate is its rider, with its own gate and target."""
    passive = skills.resolve_passive(
        {
            1: ["p"],
            2: [2],
            3: [CUTE3],
            4: [
                "live_passive_skill_effect-live_active_skill_activation_probability_up_permil_up-190-live_skill_effect_target-attribute-attribute_1-2"
            ],
            5: [CUTE3],
            6: [
                "live_passive_skill_effect-sense_up_permil_up-170-live_skill_effect_target-attribute-attribute_1-2"
            ],
        }
    )
    slot = itc._slot("PASSIVE", passive, {"costume_name_unknown": [], "target_unmapped": []}, "card-x")
    assert (slot["family"], slot["effect_type"], slot["magnitude"]) == ("stat_up", "sense", 17.0)
    assert slot["rider"]["family"] == "activation_rate_up" and slot["rider"]["magnitude"] == 19.0
    assert slot["rider"]["target"] == {"kind": "type", "value": "Cute", "count": 2}
    assert slot["rider"]["condition"] == {"kind": "type", "target": "Cute", "count": 3}


def test_an_untranslated_draft_is_never_added():
    catalogue = {"schema_version": 1, "cards": []}
    draft = {"schema_version": 1, "cards": [{"id": "card-x"}]}
    plan = card_append.plan_additions(catalogue, draft, {"card-x": ["ACTIVE: unread master fields [3]"]})
    assert plan.additions == []
    assert [(r.card_id, r.reason) for r in plan.rejections] == [("card-x", "untranslated")]
