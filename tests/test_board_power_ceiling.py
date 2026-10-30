"""The board's power line CEILINGs twice.

From the client, `SkillTreeUtility::GetEffectValueWithConnectEffectsInternal` @`0x6811970`:

    value = Ceiling( (1 + connectPermil/1000) x base )

with the `Ceiling` firing **only when a connect effect exists** — the constants are
DOUBLES (`0x75ae280` = 1000.0, `0x75abdf0` = 1.0), so a float32 reading of this
function is wrong twice over.

Applying the multiplier without the ceiling is visibly wrong: five `all`-stat
nodes at x1.55 on a base of 50 would each be carried as `77.5` where the game
carries `78` — worth `0.5 x 3 stats x 5 nodes x 5 cards = 37.5` on the leader
half alone, and `+145.5` on the member half.

The second ceiling is on the per-`(card, stat)` rate PRODUCT, and it is
**inferred, not read from code** — see `test_the_rate_product_ceiling_is_inferred`.

## Why the basis matters

Feeding `board.py` `data/cards.json`'s CATALOGUE max-level stats instead of the
account's (`apply_collection`, which the CLI applies by default and an ad-hoc
harness does not) inflates the base and over-shoots, which hides a missing
`Ceiling`. On the account basis the model without the ceilings is **UNDER** on
both halves, which is the direction a missing `Ceiling` produces.

⚠⚠ **`power_bonus` and `power_bonus_for` are documented as bit-identical twins**
and `test_board_landing.py` checks it across the leader/member/grouping/account
cross product. They must change together; that parity test is the guard, and it
is the reason both are exercised below rather than just the memoised one.
"""

from __future__ import annotations

import math

import pytest

from hbuilder.domain.card import Stats
from hbuilder.domain.board import (
    BoardEffect,
    BoardModel,
    BoardNode,
    CharacterBoard,
    resolve,
)

from conftest import make_card

# `conftest.make_card` gives every stat 300, so a rate of 0.01 lands on 3.0
# exactly. Anything that must land OFF an integer says so with its own stats.
BASE = 300


def _stat(value: float, *, stat: str = "all", scope: str = "account"):
    return BoardEffect(
        family="live_deck_power_add",
        kind="stat",
        stat=stat,
        value=value,
        scope=scope,
    )


def _rate(value: float, *, stat: str = "all", scope: str = "account"):
    return BoardEffect(
        family="live_deck_power_rate_up",
        kind="rate",
        stat=stat,
        value=value,
        scope=scope,
    )


def _model(effect, multiplier: float) -> BoardModel:
    return BoardModel(
        characters={
            "Someone": CharacterBoard(
                holomem="Someone",
                nodes=(BoardNode("R-001", (effect,), multiplier),),
            )
        }
    )


def _members(n: int = 5):
    return tuple(make_card(f"card-{i}", member=f"Member {i}") for i in range(n))


# ---------------------------------------------------------------------------
# 1. The per-node Ceiling — read from the client
# ---------------------------------------------------------------------------


def test_a_connect_boosted_stat_node_is_ceiled():
    """`x1.55` on a base of 50 is `77.5`, and the game carries **78**.

    One node, `all`, five members: `78 x 3 stats x 5 = 1170`. Without the
    ceiling it is `77.5 x 15 = 1162.5`."""
    model = _model(_stat(50.0), 1.55)
    members = _members()
    assert model.power_bonus_for(members, None) == pytest.approx(1170.0)
    assert resolve(model, None, members).power_bonus(members) == pytest.approx(1170.0)


def test_an_unconnected_node_is_NOT_ceiled():
    """The `Ceiling` fires only when a connect effect exists — read from the client, and it
    is why the multiplier is the guard rather than the value's fractionality.
    A node at `x1.0` carrying a fractional value keeps it."""
    model = _model(_stat(50.5), 1.0)
    members = _members()
    assert model.power_bonus_for(members, None) == pytest.approx(50.5 * 15)
    assert resolve(model, None, members).power_bonus(members) == pytest.approx(
        50.5 * 15
    )


def test_the_ceiling_is_UP_never_to_nearest():
    """`x1.01` on 50 is `50.5`, which rounds to 50 half-to-even and to 51 under
    `Ceiling`. Pins the direction: without it both halves of the board come
    out UNDER."""
    model = _model(_stat(50.0), 1.01)
    members = _members(1)
    assert model.power_bonus_for(members, None) == pytest.approx(51.0 * 3)


def test_an_already_integral_product_is_unchanged():
    """`x2.4` on 50 is exactly 120 — the game's own screen reads `R-001 = 120`.
    A ceiling must not push an exact value up, which is what a naive
    `floor(x)+1` would do."""
    model = _model(_stat(50.0), 2.4)
    members = _members(1)
    assert model.power_bonus_for(members, None) == pytest.approx(120.0 * 3)


# ---------------------------------------------------------------------------
# 2. The rate-product Ceiling — inferred, and labelled
# ---------------------------------------------------------------------------


def test_the_rate_product_is_ceiled_per_card_and_stat():
    """`rate x base` is ceiled once per `(card, stat)` cell, not per node.

    Two rate nodes of 0.01 on a base of 305 sum to 0.02 -> `6.1` -> **7**.
    Ceiling per NODE would give `ceil(3.05) x 2 = 8`, and no ceiling `6.1`.
    The three are distinguishable and this pins which."""
    model = BoardModel(
        characters={
            "Someone": CharacterBoard(
                holomem="Someone",
                nodes=(
                    BoardNode("R-001", (_rate(0.01),), 1.0),
                    BoardNode("R-002", (_rate(0.01),), 1.0),
                ),
            )
        }
    )
    member = make_card("card-0", stats=Stats(performance=305, technique=305, sense=305))
    assert model.power_bonus_for((member,), None) == pytest.approx(7.0 * 3)
    assert resolve(model, None, (member,)).power_bonus((member,)) == pytest.approx(
        7.0 * 3
    )


# ---------------------------------------------------------------------------
# 3. The twins stay twins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("multiplier", [1.0, 1.55, 2.4])
@pytest.mark.parametrize("value", [50.0, 50.5, 70.0])
def test_both_drivers_agree_bit_for_bit(multiplier, value):
    """`power_bonus_for` is the memoised twin of `power_bonus` and the two are
    documented as bit-identical. A ceiling added to one and not the other is
    exactly the drift that documentation exists to prevent."""
    model = BoardModel(
        characters={
            "Someone": CharacterBoard(
                holomem="Someone",
                nodes=(BoardNode("R-001", (_stat(value), _rate(0.017)), multiplier),),
            )
        }
    )
    members = _members()
    assert model.power_bonus_for(members, None) == resolve(
        model, None, members
    ).power_bonus(members)


def test_a_board_with_no_power_effects_is_still_exactly_zero():
    """The identity case. `math.ceil` on an empty sum must not invent a point —
    a ceiling applied to `0.0` cells would turn every empty board into a
    positive number and quietly move every existing score."""
    model = BoardModel(characters={})
    assert model.power_bonus_for(_members(), None) == 0.0
    assert resolve(model, None, _members()).power_bonus(_members()) == 0.0
    assert math.ceil(0.0) == 0
