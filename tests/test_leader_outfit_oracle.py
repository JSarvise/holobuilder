"""The leader decides the ranking, and the board is what decides the leader.

ACCEPTANCE TEST against the game's own answer. Two `Live/GetDeck` evaluations
with the **same five members** and **only the leader changed**, isolated from
the captured deck corpus by `deck_oracle.py`.

                        leader Ina'nis    leader Nene
    Overall Power            164,449        181,079
      of which board          44,682         17,687
    Score Bonus               169.0 %        114.1 %
      of which board           22.6 %          5.9 %
    Unit Score               901,254        789,857     <- Ina wins by 14.1 %

Nene's outfit is `Performance UP 120 %` (a STAT outfit, so the server puts it
in the POWER column) and Ina's is `Score Support 60 %` (a SCORE outfit, so it
goes in the BONUS column). Nene buys MORE power and LESS score, and loses —
**more power, worse team**. Any objective ranking on power alone picks the
wrong leader here.

## Three tempting but wrong explanations

1. **"A stat outfit and a score outfit land in the wrong columns."** No.
   `_pool_for` routes Ina's `score_support`/`score` outfit to the
   "support" pool and Nene's `stat_up`/`performance` one to "stat", and the
   catalogue carries both correctly.
2. **"`base_stat_sum` moves between the two runs."** No — it is 129,924 in
   both. That reading comes from dividing `effective_stat_sum` by
   `(1 + combined_stat_rate)`, and that relation does not hold:
   `combined_stat_rate` is a reported summary while the stat stage works per
   stat. It must not be read as the multiplier applied.
3. **"The board is not connected."** No. `cli.py`'s `--board` defaults to
   `DEFAULT_BOARD_PATH`, so production scores the board. Without a board the
   ordering inverts, which is why the board fixture below is mandatory rather
   than optional.

## What is actually left

With the board connected the ordering is **right** and the board POWER half is
close to exact — 46,700 against the server's 44,682 for Ina, and 17,725
against 17,687 for Nene (+0.2 %). The margin is not: the model gives Ina +7.3 %
where the server gives +14.1 %.

The server's `by_skill_tree` BONUS line (0.059 / 0.226) is NOT something
`board_score_rate` should reproduce: the two are **different quantities**. Our
field is the account-scope `song_score_bonus` term, correctly leader-invariant,
while the server's line is `Ceiling(board skill_effect_up × Active / 1000)` — a
display decomposition of a multiplication `_score_up_pool` already performs. It
is not an additive term, so no additive structure fits it. A controlled node
reset confirms it: `173 → 142` permil predicts `72 → 59` displayed, and the
server reads exactly that — which also pins the node's Connect multiplier to
×1.55 against ×1.00 and ×2.40.

So the ordering assertion is a plain regression guard, the invariance is
asserted as CORRECT, and the leader dependence is guarded where it actually
lives — `skill_effect_up_for`.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from conftest import FIXTURES_DIR
from hbuilder.domain.board import load_board
from hbuilder.domain.loader import load_cards
from hbuilder.domain.scoring import score_with_breakdown
from hbuilder.domain.scoring_config import load_scoring_config
from hbuilder.domain.team import build_team

# Hermetic fixtures carved from the catalogue and from the account's captured
# board at the time of the pair. The canonical `data/` tree and the
# gitignored `dumps/` tree are both deliberately unread: `test_fixture_
# isolation.py` forbids the first, and the second is account state that does
# not exist on a clean checkout.
PAIR_CARDS_PATH = FIXTURES_DIR / "leader_outfit_pair_cards.json"
PAIR_BOARD_PATH = FIXTURES_DIR / "leader_outfit_pair_board.json"

# DEFAULT_CONFIG via the same missing-path fallback `conftest.py` uses, so the
# result cannot drift with a local `config.json` edit.
_NONEXISTENT_CONFIG = Path("tests/__leader_outfit_nonexistent_config__.json")

MEMBERS = (
    "card-00031-5-uniq-0025-00",  # Momosuzu Nene   5*
    "card-00036-5-uniq-0029-00",  # Takane Lui      5*
    "card-04001-5-uniq-0042-00",  # Mori Calliope   5*
    "card-00023-5-uniq-0019-00",  # Houshou Marine  5*
    "card-04003-5-uniq-0044-00",  # Ninomae Ina'nis 5*
)

LEADER_INA = "card-04003-5-uniq-0044-00"   # outfit: Score Support 60 % -> BONUS column
LEADER_NENE = "card-00031-5-uniq-0025-00"  # outfit: Performance UP 120 % -> POWER column

SERVER_UNIT_SCORE = {LEADER_INA: 901_254, LEADER_NENE: 789_857}
# The server's `by_skill_tree` BONUS line for this pair. ⚠ NOT a target for
# `board_score_rate` — see the docstring: it is `Ceiling(skill_effect_up × Active / 1000)`,
# a different quantity. Kept because the invariance test below rests on it.
SERVER_BOARD_SCORE_RATE = {LEADER_INA: 0.226, LEADER_NENE: 0.059}


def _breakdown(leader_id: str):
    cards = load_cards(PAIR_CARDS_PATH)
    board = load_board(PAIR_BOARD_PATH)
    assert board is not None, f"board fixture missing or empty: {PAIR_BOARD_PATH}"
    cfg = dataclasses.replace(load_scoring_config(_NONEXISTENT_CONFIG), board=board)
    return score_with_breakdown(build_team(cards, leader_id, MEMBERS), cfg)


def test_server_pair_is_a_genuine_inversion() -> None:
    """Guards the premise: the game really does rank the lower-power team
    higher. If this fails the captured numbers were mistranscribed and
    everything below is testing nothing."""
    assert SERVER_UNIT_SCORE[LEADER_INA] > SERVER_UNIT_SCORE[LEADER_NENE]


def test_score_support_leader_beats_stat_leader() -> None:
    """The ordering, with the board connected exactly as production connects
    it. A regression guard: it goes the wrong way when the board is absent,
    and the board is account state a future
    refactor could quietly stop passing through."""
    assert _breakdown(LEADER_INA).total_score > _breakdown(LEADER_NENE).total_score


def test_board_score_rate_is_leader_invariant_and_that_is_correct() -> None:
    """`board_score_rate` is leader-invariant, and that is correct.

    The server's `by_skill_tree` BONUS line (0.059 / 0.226) is not what
    `board_score_rate` reproduces; they are **different quantities**:

    * `board_score_rate` is fed by `song_score_bonus` at `song_scope == "all"`,
      which is `scope="account"` — genuinely independent of the leader, so
      invariance is the RIGHT answer, not a missing term. A captured deck agrees:
      Subaru's eight `solo`-scoped nodes contribute **0** from the leader slot,
      so the family filter is right too.
    * the server's line is `Ceiling(board skill_effect_up × Active / 1000)` — a
      DISPLAY decomposition of a multiplication `_score_up_pool` performs. A
      controlled node reset confirms it: `173 → 142` permil predicts `72 → 59`
      displayed, and the server reads exactly that.

    That line is not an additive term. The leader dependence is real and lives
    one field over — asserted below.

    ⚠ The `Ceiling(rate × Active/1000)` identity is deliberately NOT asserted here.
    It holds on leader-only decks and breaks once members carry their own ACTIVE /
    SPECIAL / PASSIVE, and this fixture's five members do."""
    ina = _breakdown(LEADER_INA).board_score_rate
    nene = _breakdown(LEADER_NENE).board_score_rate
    assert ina == nene, (
        "board_score_rate became leader-dependent; it is fed only by "
        f"account-scope song_score_bonus and must not be ({ina} vs {nene})"
    )


def test_the_leader_dependence_lives_in_skill_effect_up() -> None:
    """Where the 0.059-vs-0.226 spread actually comes from, as a regression guard.

    Ina's `set_live_leader`-scoped `effect_up` nodes are the whole of it, and they
    reach the score through `_score_up_pool`'s multiplier rather than through
    `board_score_rate`. 173.0 permil is derived offline from the board and
    agrees with the server (captured decks bracket it to 172..174 permil, and a
    node reset brackets the removed node to 30..33 against its stated
    `20 × 1.55 = 31`).

    The fixture board is frozen on purpose and carries `R-032`, whatever the live
    account holds."""
    board = load_board(PAIR_BOARD_PATH)
    assert board is not None
    members = build_team(load_cards(PAIR_CARDS_PATH), LEADER_INA, MEMBERS).members

    ina = board.skill_effect_up_for(members, "Ninomae Ina'nis")
    nene = board.skill_effect_up_for(members, "Momosuzu Nene")

    assert ina == pytest.approx(0.173), f"Ina's leader-scope effect_up moved: {ina}"
    assert nene == 0.0, f"Nene carries no set_live_leader effect_up, got {nene}"
