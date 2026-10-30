"""The absent-vs-zero seam: an absent field and an explicit zero
are two different values that no code path can merge.

`UNKNOWN` (never Python's `None`) is the in-memory sentinel for "never
populated". `is_known()` distinguishes it from an explicit zero;
`require_known()` is the only sanctioned way to turn an unknown-capable
field into a number, and it raises — naming the card id and field — rather
than substituting a default.
"""

from __future__ import annotations

import pytest

from hbuilder.domain.card import card_from_record
from hbuilder.domain.schema import SchemaError
from hbuilder.domain.unknown import UNKNOWN, is_known, require_known


def _card_record(magnitude: object) -> dict:
    """A minimal, schema-shaped card record whose ACTIVE slot magnitude is
    the given value (either `None`, meaning unknown on disk, or an explicit
    number).

    Note: this record's `condition` is also `None`, which
    means UNKNOWN and therefore also taints `skills.ACTIVE.condition` on
    both the magnitude=None and magnitude=0 cards below. The assertions in
    this file only check `skills.ACTIVE.magnitude`'s presence/absence, so
    they are unaffected by that additional taint."""
    return {
        "id": "test_card_01",
        "member": "Test Member",
        "generation": ["5期生"],
        "type": "Cute",
        "rarity": "SSR",
        "costume_name": "Test Costume",
        "stats": {"performance": 100, "technique": 100, "sense": 100},
        "skills": {
            "SPECIAL": None,
            "ACTIVE": {
                "family": "conditional_score_up",
                "effect_type": "score",
                "duration": 8,
                "interval": 15,
                "probability_tier": "Medium",
                "magnitude": magnitude,
                "condition": None,
                "target": {"kind": "all"},
                "override": {"kind": "none"},
                "rider": {"kind": "none"},
            },
            "PASSIVE": None,
        },
        "costume_skill": None,
    }


# --- is_known --------------------------------------------------------------


def test_is_known_is_false_for_unknown_and_true_for_explicit_zero():
    assert is_known(UNKNOWN) is False
    assert is_known(0.0) is True
    assert is_known(0) is True


# --- require_known -----------------------------------------------------------


def test_require_known_returns_explicit_zero_unchanged():
    assert require_known(0.0, "nene_01", "skills.ACTIVE.magnitude") == 0.0


def test_require_known_raises_schema_error_naming_card_id_and_field():
    with pytest.raises(SchemaError) as exc_info:
        require_known(UNKNOWN, "mio_01", "skills.ACTIVE.magnitude")
    message = str(exc_info.value)
    assert "mio_01" in message
    assert "skills.ACTIVE.magnitude" in message


# --- strict-explicit truthiness / coercion behavior -------------------------


def test_bool_of_unknown_raises_type_error():
    with pytest.raises(TypeError):
        bool(UNKNOWN)


def test_truthiness_test_against_unknown_raises_rather_than_silently_passing():
    with pytest.raises(TypeError):
        if UNKNOWN:
            pass


def test_float_of_unknown_raises_schema_error():
    with pytest.raises(SchemaError):
        float(UNKNOWN)


def test_int_of_unknown_raises_schema_error():
    with pytest.raises(SchemaError):
        int(UNKNOWN)


def test_unknown_repr_is_the_literal_string_unknown():
    assert repr(UNKNOWN) == "UNKNOWN"


def test_unknown_compares_equal_only_to_itself():
    assert UNKNOWN == UNKNOWN
    assert (UNKNOWN == 0) is False
    assert (UNKNOWN == None) is False  # noqa: E711 - deliberately not `is None`


# --- absent vs. explicit-zero never collapse into one representation -------


def test_unknown_magnitude_and_explicit_zero_magnitude_produce_unequal_cards():
    unknown_card = card_from_record(_card_record(magnitude=None))
    zero_card = card_from_record(_card_record(magnitude=0))

    assert unknown_card != zero_card
    assert unknown_card.skill("ACTIVE").magnitude is UNKNOWN
    assert zero_card.skill("ACTIVE").magnitude == 0


def test_unknown_magnitude_and_explicit_zero_magnitude_differ_in_unknown_fields():
    unknown_card = card_from_record(_card_record(magnitude=None))
    zero_card = card_from_record(_card_record(magnitude=0))

    assert "skills.ACTIVE.magnitude" in unknown_card.unknown_fields()
    assert "skills.ACTIVE.magnitude" not in zero_card.unknown_fields()
    assert unknown_card.unknown_fields() != zero_card.unknown_fields()


def test_explicit_zero_card_reads_as_number_while_unknown_card_raises():
    unknown_card = card_from_record(_card_record(magnitude=None))
    zero_card = card_from_record(_card_record(magnitude=0))

    assert (
        require_known(
            zero_card.skill("ACTIVE").magnitude, zero_card.id, "skills.ACTIVE.magnitude"
        )
        == 0.0
    )
    with pytest.raises(SchemaError):
        require_known(
            unknown_card.skill("ACTIVE").magnitude,
            unknown_card.id,
            "skills.ACTIVE.magnitude",
        )


# ---------------------------------------------------------------------------
# The sentinel has to survive being COPIED, because `is_known` tests identity
# ---------------------------------------------------------------------------


def test_the_unknown_sentinel_survives_pickle_and_copy_as_the_same_object():
    """`is_known(value)` is `value is not UNKNOWN`. An identity test means a
    *copy* of the sentinel is not the sentinel — and the failure is silent, not
    loud: `is_known` returns True, the branch that exists to refuse a fabricated
    number is skipped, and the run produces a score.

    This is not hypothetical. `multiprocessing` on Windows uses `spawn`, so the
    parallel exact search ships every `Card` to its workers through `pickle`.
    Without `Unknown.__reduce__` the round trip produces a second `Unknown`
    instance and `is_known` returns True for it.
    """
    import copy
    import pickle

    assert pickle.loads(pickle.dumps(UNKNOWN)) is UNKNOWN
    assert copy.copy(UNKNOWN) is UNKNOWN
    assert copy.deepcopy(UNKNOWN) is UNKNOWN

    assert not is_known(pickle.loads(pickle.dumps(UNKNOWN)))
    assert not is_known(copy.deepcopy(UNKNOWN))


def test_a_pickled_card_keeps_its_unknown_fields_unknown():
    """The end-to-end statement, on a `Card` rather than the bare sentinel: a
    worker process must see exactly the same unknown fields the parent did.
    Asserted through `unknown_fields()` and `require_known` — the two callers
    that decide whether a number gets fabricated — not through `==`, which a
    broken sentinel could still satisfy by comparing two distinct instances of
    a class with no `__eq__`."""
    import pickle

    original = card_from_record(_card_record(magnitude=None))
    shipped = pickle.loads(pickle.dumps(original))

    assert shipped.unknown_fields() == original.unknown_fields()
    assert shipped.skill("ACTIVE").magnitude is UNKNOWN
    with pytest.raises(SchemaError):
        require_known(
            shipped.skill("ACTIVE").magnitude, shipped.id, "skills.ACTIVE.magnitude"
        )
