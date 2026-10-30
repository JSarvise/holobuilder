"""The Holomem Board.

On one fully decomposed deck the board is worth **37,472 of 135,736 Overall
Power (27.6 %)** and **18.9 of 164.9 Score Bonus points (11.5 %)**.


## What the board is

Each of the 54 holomem has her own board of **152 node groups** in five
branches. A node group is unlocked or not; unlocking it turns on its effect.

| branch | scope | read from | tiles |
|---|---|---|---|
| **R** | applies **only while that holomem is the unit Leader**, and then to the *whole* team | every R row's effect id ends `-skill_tree_effect_target-all-set_live_leader` | 63 |
| **B** | applies **always**, to **that holomem's own card**, while she is a member | `-skill_tree_effect_target-skill_tree_character-always` | 31 |
| **G** | applies **always**, to **every** card | `-skill_tree_effect_target-all-always` (179 of 182 rows) | 182 rows |
| **G** (3 groups) | applies **always**, to cards of **that holomem's own grouping** | `all_parameter_up_for_character_grouping-...-skill_tree_effect_target-character_grouping-grp-<group>-always` | `G-008`, `G-011`, `G-021` |
| **Y** | per-**song** score bonus and Holowork rewards | `live_score_bonus_add_permil_up_by_music_...` | 43 |
| **S** | the 4 Connect slots you place a card on | — | 4 |

Effect ids are self-describing and are parsed, not tabulated:

    skill_tree_effect-<family>-<value>-skill_tree_effect_target-<target>-<condition>

A `<family>` ending `_permil_up` carries a per-mille **rate**; otherwise the
value is **flat stat points**. Both shapes occur on the same branch.


## Green is BOTH a constant and a discriminator

Read out of `SkillTreeNode` (323 rows over 152 group ids), the green branch
holds two kinds of node:

* **179 of 182 G rows** target `all-always` — every card, always, no
  condition. Values are tiny and flat: `all_parameter_up-5`,
  `performance_up-5/10`, `technique_up-5/10`, `sense_up-5/10`, plus reward
  bonuses that never touch score. These are a genuine **constant**: they add
  the same number to every candidate team of five, so they cancel out of a
  ranking.
* **3 G node groups — `G-008`, `G-011`, `G-021` — are `character_grouping`
  scoped and are per-team DISCRIMINATORS.** Each carries **54 rows, one per
  character**, and each row's target is that character's own grouping
  (`grp-myth`, `grp-gen_0`, `grp-holox`, `grp-advent`, `grp-promise`,
  `grp-regloss`, `grp-gamers`, `grp-indonesia-gen_N`, ...). The family is
  `all_parameter_up_for_character_grouping`, so it adds flat all-stats to
  exactly those cards in the team that share the unlocking holomem's grouping,
  and the value depends on the group's size:

      G-008: 50 / 75 / 100      G-011: 150 / 225 / 300      G-021: 100 / 150 / 200

  which is where *"+450 all stats to her generation"* comes from
  (`50+150+100 = 300`, `75+225+150 = 450`, `100+300+200 = 600` at full unlock,
  by group size) and why the branch ceilings differ per character.

This module models both: a `"account"`-scope effect reaches every member, a
`"grouping"`-scope effect reaches only members whose `Card.generations`
contains the effect's grouping. The grouping half is a discriminator by
construction, so it changes rankings as soon as one of the three is unlocked.


## Connect nodes

A card placed on a Connect (S) node multiplies the value of every unlocked
tile inside that card's own fixed shape, anchored at that node:

    boosted = base * (1 + connect_permil / 1000)

Checked against the game screen: Calliope 5★ at 140 % turned "+50 to All" into
+120, "Sense +100" into +240 and "Score Support 10 %" into 24 % — x2.4 on all
three, so **the multiplier applies to the number whatever its unit** and no
per-family special case is needed.


## Units: flat points versus per-mille

Two facts matter when reading board values:

1. **`SkillTreeNode` has no per-node level.** 137 of 152 group ids have
   exactly one row; the 12 multi-row groups are all `Y` (a per-song variant,
   `-solo` vs `-solo-fuwamoko`, not a level) and the 3 remaining are the
   54-row `character_grouping` groups above. **No R node has a second level
   anywhere in the master data.**
2. **Flat stat points and per-mille rates are different units.** Ina's 27 R
   nodes sum to *flat* `All 470, Sense 350, Tech 200, Perf 200` and to
   *per-mille* `All 60, Sense 30, Tech 30, Act.Power 240`. Adding the two and
   dividing by 10 gives `53.0 / 38.0 / 23.0 / 20.0 / 24.0` %, which looks
   "nearly double" the observed +28.73 pp only because it prints a flat stat
   point as a tenth of a percent.

With the units separated and the Connect boosts applied, the numbers land on
the acceptance test. See `tests/test_board.py`, which pins them.


## What this module does NOT claim

* **The Y branch's per-song terms are computable but not ranked
  song-agnostically.** `song_score_bonus_by_holomem` +
  `song_score_bonus_for(singers, scope=...)` give the per-song aggregation
  from the song's own cast. Only the song-*independent* `"all"` bucket is
  offered to `scoring.py` as `ScoreUpBonusRatioBySkillTree`; whether a
  song-AGNOSTIC ranking should use the per-song ones is left to the caller.
* **Whether a leader's BLUE nodes count when she is not also one of the five
  members is not known.** Blue is defined as "in the unit", and the leader
  slot is not a member slot, so the default implemented is *no*.
* **The Connect placement's Bloom level is not in the captured state.**
  Every Connect effect has two levels (base and Bloom 5); `board_state.json`
  records the placed card id and not its bloom. `BoardNode.connect_multiplier`
  is therefore an input to this module, not something it derives, and the
  acceptance test supplies the *base* level — which is what reproduces the
  observation: 23,318.22 against the observed 23,272 (+0.20 %), where Bloom 5
  on both of Ina's slots gives 27,068.22 (+16.31 %). Both level-2 values come
  from master `SkillTreeConnectEffect`, where **level 2 is exactly level 1 +
  500 permil on all 20 effects** — `center_area-001-rare_5` 1400→1900
  (×2.4→×2.9) and `general_purpose_area-001-rare_5` 550→1050 (×1.55→×2.05).
  Reproduce with `tests/test_board.py::test_bloom_5_connect_overshoots_the_acceptance_number`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .card import Card

__all__ = [
    "BOARD_SCOPES",
    "BoardContribution",
    "BoardEffect",
    "BoardError",
    "BoardModel",
    "BoardNode",
    "CharacterBoard",
    "GROUPING_ALL_CAP",
    "SONG_ALL_CAP_PERMIL",
    "STAT_NAMES",
    "load_board",
    "parse_effect_id",
    "resolve",
]


class BoardError(Exception):
    """A board record that cannot be interpreted. Raised rather than guessed:
    a board effect this module does not recognise is silently worth zero
    otherwise, and a silently-zero term is indistinguishable from a correct
    zero."""


STAT_NAMES: tuple[str, ...] = ("performance", "technique", "sense")

# "leader"   — only while that holomem is the unit Leader; reaches all members.
# "member"   — always, but only that holomem's own card, and only if she is
#              among the five members.
# "account"  — always, every member. The constant half of the green branch.
# "grouping" — always, but only members sharing the unlocking holomem's
#              grouping. The discriminating half of the green branch.
BOARD_SCOPES: frozenset[str] = frozenset({"leader", "member", "account", "grouping"})

# The type-19 (`all` scope) per-song score-bonus ceiling, from the client
# constant `0x75cec38` = `0000c842` — 100 permil, displayed as 10.0 %.
# Per-recipient, never account-wide.
SONG_ALL_CAP_PERMIL: int = 100

# The type-20 ceiling: the grouping greens (`all_parameter_up_for_character_
# grouping`, G-008/011/021) a card RECEIVES, summed over every source board whose
# grouping it is in, never add more than 900 to each stat. Read from
# `main_SkillTreeEffectValueLimit` (the only other limit row) and the client's
# grouping-dialog presenter, which sums per receiver. Whether the server enforces it is
# not known (no observed board has reached it). Applied to the Connect-scaled
# sum, on the reading that the limit is on the effect value.
GROUPING_ALL_CAP: int = 900

# The bucket key the capped grouping sum is collected under before it is spread
# onto the three stats (`_close_grouping`). Never a stat name.
_GROUPING_KEY = "grouping_all"


def _close_grouping(flat: dict[str, float]) -> dict[str, float]:
    """`flat` with its grouping-green sum capped at `GROUPING_ALL_CAP` and added to
    every stat. Below the cap the additions are the ones the uncapped walk made,
    only later in the sequence: exact when every flat value is a whole number
    (`BoardModel._terms` explains `flat_exact`)."""
    if _GROUPING_KEY not in flat:
        return flat
    out = {k: v for k, v in flat.items() if k != _GROUPING_KEY}
    g = min(flat[_GROUPING_KEY], float(GROUPING_ALL_CAP))
    for stat in STAT_NAMES:
        out[stat] = out.get(stat, 0.0) + g
    return out


def _is_grouping_all(effect: "BoardEffect") -> bool:
    return effect.scope == "grouping" and effect.kind == "stat" and effect.stat == "all"

# The families this module knows how to route, and where each one goes.
# `("stat", <stat or "all">)`  — flat stat points.
# `("rate", <stat or "all">)`  — per-mille of that stat's BASE value. The
#   outfit's own stat rate applies to Member Parameter exactly (12,160 / 81,009
#   = 15.011 % against a stated 15 %), and the board's percentages are read the
#   same way — an inference from that one measurement.
_STAT_FAMILIES: dict[str, tuple[str, str]] = {
    "all_parameter_up": ("stat", "all"),
    "performance_up": ("stat", "performance"),
    "technique_up": ("stat", "technique"),
    "sense_up": ("stat", "sense"),
    "all_parameter_up_for_character_grouping": ("stat", "all"),
    "all_parameter_up_permil_up": ("rate", "all"),
    "performance_up_permil_up": ("rate", "performance"),
    "technique_up_permil_up": ("rate", "technique"),
    "sense_up_permil_up": ("rate", "sense"),
}

# Families that leave the stat block entirely. Each is per-mille.
_SKILL_EFFECT_UP = "live_active_skill_effect_up_permil_up"
_ACTIVATION_RATE_UP = "live_active_skill_activation_probability_up_permil_up"
_COOLDOWN_SHORTEN = "live_active_skill_cool_time_shorten_permil_up"
_SONG_SCORE_BONUS = (
    "live_score_bonus_add_permil_up_by_music_skill_tree_character_and_music_singer_type"
)

# Reward/economy families. Parsed so an unknown family still raises, but they
# reach neither power nor score and are dropped on the floor deliberately.
_IGNORED_FAMILY_PREFIXES = (
    "work_reward_",
    "live_reward_",
    "mini_game_reward_",
    # LIFE + leader-active-addition/level: no RANKING term, so score 0, not
    # RAISE. Raising would hurt the planner: a life node sits on a side branch
    # whose path SHARES the red corridor's prefix (R-001, R-002…), and the beam
    # marks a whole failed path unroutable, which would poison those shared nodes
    # so every later red buy is skipped (measured: raising buys no red at all,
    # -8.8 %; scoring 0 buys the full leader red, +4.9 %).
    "life_up",
    "live_deck_leader_active_skill",
)

_EFFECT_RE = re.compile(
    r"^skill_tree_effect-(?P<family>.+?)-(?P<value>\d+)"
    r"-skill_tree_effect_(?P<kind>target|passive_trigger)-(?P<rest>.+)$"
)

_GROUPING_RE = re.compile(r"^character_grouping-(?P<grouping>.+?)-(?P<condition>always)$")

# A `character_grouping` effect names its group as the game's TOKEN (`grp-myth`),
# but `Card.generations` carries the DISPLAY name the catalogue ships (`Myth`,
# `holoX`, `3期生`). Without this map a gen-scoped green (G-008/G-011/G-021) never
# matches a real card and scores 0, which makes the whole family look inert.
# Cards built with the token directly (the
# board tests) still match on the first leg, so the check accepts EITHER form.
# Source: `import_to_cards.GENERATION_BY_GROUP`.
_GENERATION_BY_GROUPING = {
    "grp-gen_0": "0期生", "grp-gen_1": "1期生", "grp-gen_2": "2期生",
    "grp-gen_3": "3期生", "grp-gen_4": "4期生", "grp-gen_5": "5期生",
    "grp-gamers": "ゲーマーズ", "grp-holox": "holoX",
    "grp-indonesia-gen_1": "ID1", "grp-indonesia-gen_2": "ID2",
    "grp-indonesia-gen_3": "ID3", "grp-myth": "Myth", "grp-promise": "Promise",
    "grp-advent": "Advent", "grp-regloss": "ReGLOSS",
}


@dataclass(frozen=True, slots=True)
class BoardEffect:
    """One unlocked node's one effect, already routed.

    `value` is flat stat points when `kind == "stat"` and a **fraction** (the
    per-mille divided by 1000) for every other kind, so no caller divides by
    1000 twice — the same rule `scoring._as_rate` follows."""

    family: str
    kind: str  # "stat" | "rate" | "skill_effect_up" | "activation_rate_up"
    #          | "cooldown_shorten" | "song_score_bonus" | "ignored"
    stat: str  # "all" | one of STAT_NAMES | "" for non-stat kinds
    value: float
    scope: str
    grouping: str | None = None
    song_scope: str | None = None
    # A `skill_tree_effect_passive_trigger` the deck preview does not fire —
    # e.g. R-002's `music_skill_tree_character` (only on that holomem's own
    # song). Carried, never silently dropped, and excluded from the
    # song-agnostic terms.
    conditional: bool = False

    def scaled(self, multiplier: float) -> BoardEffect:
        """This effect with a Connect multiplier applied. The multiplier
        applies to the number whatever its unit, so this is one line and not
        a per-family table.

        **And the product is CEILED** —
        `SkillTreeUtility::GetEffectValueWithConnectEffectsInternal` @`0x6811970`:

            value = Ceiling( (1 + connectPermil/1000) x base )

        with the `Ceiling` firing **only when a connect effect exists**, which is
        why the `multiplier == 1.0` early return above is the guard rather than
        any test on the value. The constants are DOUBLES (`0x75ae280` =
        1000.0, `0x75abdf0` = 1.0), not float32.

        Example: five of Ina'nis' `all`-stat nodes at x1.55 on a base of 50
        give `77.5` unceiled where the game carries `78`. On one measured deck
        that is +37.5 on the leader half and +145.5 on the member half, and it
        takes the model from -0.8 % to -0.003 % there.

        ⚠ **Applied to `stat` only, and that is a deliberate limit.** The game
        ceils *the value*, and for `stat` the unit is whole stat points, so the
        reading is unambiguous. For the permil-valued kinds (`rate`,
        `skill_effect_up`, `song_score_bonus`) the same `Ceiling` would round to
        a whole permil — plausible, but not known: no observed rate or permil
        node carries a connect multiplier other than 1.0, so no observation can
        separate it. Ceiling them on a guess would move numbers
        that currently reproduce the game (`R-002` reads 24.0 % in game and
        `10.0 x 2.4` is already integral; `R-049` reads 6.2 % and `4.0 x 1.55 =
        6.2` is not, and ceiling it to 7 would BREAK a confirmed reading).
        That last one is the measurement that decides it: **do not extend this
        to permil kinds.**"""
        if multiplier == 1.0:
            return self
        value = self.value * multiplier
        if self.kind == "stat":
            value = math.ceil(value)
        return BoardEffect(
            family=self.family,
            kind=self.kind,
            stat=self.stat,
            value=value,
            scope=self.scope,
            grouping=self.grouping,
            song_scope=self.song_scope,
            conditional=self.conditional,
        )


@dataclass(frozen=True, slots=True)
class BoardNode:
    """One unlocked node group, with its Connect multiplier already resolved.

    `connect_multiplier` is `1 + connect_permil/1000` for the Connect shape
    covering this node, or `1.0`. It is an INPUT: the placed card's Bloom
    level is not recoverable from the captured state (see the module
    docstring), so this module does not derive it."""

    node_id: str
    effects: tuple[BoardEffect, ...]
    connect_multiplier: float = 1.0

    @property
    def branch(self) -> str:
        return self.node_id[:1]


@dataclass(frozen=True, slots=True)
class CharacterBoard:
    """One holomem's unlocked nodes. `holomem` is the `Card.member` name, so a
    board joins to a card the same way `cfg.board_score_bonus` already does —
    by holomem, never by card id, because the board is a property of the
    character and not of a skin."""

    holomem: str
    nodes: tuple[BoardNode, ...] = ()


# The largest flat stat value `BoardDigest.flat_exact` accepts. A double holds
# every integer up to 2**53 exactly; 2**32 leaves room for two million addends
# before a partial sum could leave that range, and a real node is two or three
# digits.
_FLAT_EXACT_MAX = float(2**32)


def _FIRST(pair: tuple) -> object:
    return pair[0]


class BoardDigest:
    """Everything `BoardModel` derives from ONE `CharacterBoard`, walked once.

    A `BoardModel` is rebuilt per candidate team (`board_model_from_plan`), but
    only the team's own five or six boards differ between two teams: the other
    ~50 are the account's bench, the same frozen `CharacterBoard` objects every
    time. Re-walking all of them on the enumeration leaf — five times in
    `__post_init__`, once more per member in `_terms`, each visit paying
    `BoardEffect.scaled` — costs ~2/3 of the whole leaf. A digest holds the
    per-board half of those walks, so a caller that keeps boards alive
    (`PlanCache`) keeps their digests too and the model only stitches them.

    **Nothing here is summed across boards, and that is the correctness rule.**
    Float addition is not associative: `BoardModel` accumulates its buckets in
    `characters -> nodes -> effects` order, so a digest stores the ordered
    ADDENDS of its own board and the model replays them in board order — the
    same additions in the same sequence, not per-board subtotals. The only sums
    taken here are the ones the model already took per board (`song_scopes`,
    `uptime`).

    Not a dataclass and not hashable by content: identity is the point, it is a
    memo attached to one board object (`board`), which the model checks with
    `is` before trusting a digest it was handed."""

    __slots__ = (
        "board", "leader_scoped", "song_ops", "song_scopes", "skill_effect_up",
        "conditional_skill_effect_up", "conditional_stat", "uptime",
        "uptime_error", "stat_effects", "flat_exact", "inert", "_stat_ops",
        "_stat_ops_by_card",
    )

    def __init__(self, board: CharacterBoard) -> None:
        self.board = board
        leader_scoped = False
        song_ops: list[tuple[str, float]] = []
        song_scopes: dict[str, float] = {}
        skill_effect_up: list[BoardEffect] = []
        conditional_skill_effect_up: list[BoardEffect] = []
        conditional_stat: list[BoardEffect] = []
        stat_effects: list[BoardEffect] = []
        rate = cooldown = 0.0
        # `(node_id, kind, scope)` of the first uptime effect at a scope other
        # than "member". Carried, not raised: the message names the holomem the
        # MODEL keys this board by, and the model raises it (`__post_init__`).
        uptime_error: tuple[str, str, str] | None = None
        for node in board.nodes:
            for raw_effect in node.effects:
                if raw_effect.scope == "leader":
                    leader_scoped = True
                effect = raw_effect.scaled(node.connect_multiplier)
                kind = effect.kind
                if kind == "song_score_bonus":
                    key = effect.song_scope or "all"
                    song_ops.append((key, effect.value))
                    song_scopes[key] = song_scopes.get(key, 0.0) + effect.value
                elif kind == "skill_effect_up":
                    if effect.conditional:
                        conditional_skill_effect_up.append(effect)
                    else:
                        skill_effect_up.append(effect)
                elif kind in ("activation_rate_up", "cooldown_shorten"):
                    if uptime_error is None:
                        if effect.scope != "member":
                            uptime_error = (node.node_id, kind, effect.scope)
                        elif not effect.conditional:
                            if kind == "activation_rate_up":
                                rate += effect.value
                            else:
                                cooldown += effect.value
                elif kind in ("stat", "rate") and effect.conditional:
                    conditional_stat.append(effect)
                # What `BoardModel._terms` still has to test per card: every
                # effect past its first (card-independent) `continue`.
                if kind not in ("ignored", "song_score_bonus", "skill_effect_up"):
                    stat_effects.append(effect)
        self.leader_scoped = leader_scoped
        self.song_ops = tuple(song_ops)
        self.song_scopes = song_scopes
        self.skill_effect_up = tuple(skill_effect_up)
        self.conditional_skill_effect_up = tuple(conditional_skill_effect_up)
        self.conditional_stat = tuple(conditional_stat)
        self.uptime = (rate, cooldown)
        self.uptime_error = uptime_error
        self.stat_effects = tuple(stat_effects)
        # Whether every flat stat value on this board is a whole number small
        # enough that sums of them are exact — see `BoardModel._terms`.
        self.flat_exact = all(
            effect.value == math.floor(effect.value)
            and abs(effect.value) <= _FLAT_EXACT_MAX
            for effect in stat_effects
            if effect.kind == "stat"
        )
        # Nothing here but plain stat effects: `BoardModel.__post_init__` has
        # nothing to derive from this board and only files its digest.
        self.inert = not (
            leader_scoped or song_ops or skill_effect_up
            or conditional_skill_effect_up or conditional_stat
            or uptime_error is not None or rate or cooldown
        )
        self._stat_ops: dict[tuple, tuple[tuple, tuple, tuple]] = {}
        # The same memo reached by the card OBJECT: `(id(card), leader_here) ->
        # (card, holomem, result)`, the card kept alive in the entry so a
        # recycled `id()` cannot serve another card's answer. A search asks
        # with the same card objects millions of times, and hashing the
        # content key above is most of what a lookup costs.
        self._stat_ops_by_card: dict[tuple[int, bool], tuple] = {}

    def stat_ops(
        self, holomem: str, card: Card, card_key: tuple, leader_here: bool
    ) -> tuple[
        tuple[tuple[str, float], ...],
        tuple[tuple[str, float], ...],
        tuple[tuple[str, float], ...],
        tuple[tuple[str, int], ...],
    ]:
        """The `(flat, rate, flat_summed, flat_counts)` bucket additions this
        board makes for one card, as ordered `(stat, value)` addends —
        `BoardModel._terms`' loop body for this board, with the bucket writes
        recorded instead of performed. `flat_summed` is `flat` with each stat's
        addends added up, stats in first-arrival order; only meaningful under
        `flat_exact`. `flat_counts` is how many addends each stat received.

        `leader_here` is `leader_holomem == holomem`: the only thing the walk
        reads off the leader. `card_key` is `(id, member, generations)`, the
        only things it reads off the card (see `_terms`)."""
        fast_key = (id(card), leader_here)
        fast = self._stat_ops_by_card.get(fast_key)
        if fast is not None and fast[0] is card and fast[1] == holomem:
            return fast[2]
        key = (holomem, card_key, leader_here)
        cached = self._stat_ops.get(key)
        if cached is not None:
            self._stat_ops_by_card[fast_key] = (card, holomem, cached)
            return cached
        flat: list[tuple[str, float]] = []
        rate: list[tuple[str, float]] = []
        for effect in self.stat_effects:
            if effect.scope == "leader":
                if not leader_here:
                    continue
            elif not _reaches(effect, holomem, card):
                continue
            if effect.conditional:
                continue
            if effect.kind == "stat":
                ops = flat
            elif effect.kind == "rate":
                ops = rate
            else:
                continue
            if _is_grouping_all(effect):
                # capped per receiver across boards: closed in `_terms`
                ops.append((_GROUPING_KEY, effect.value))
                continue
            stats = STAT_NAMES if effect.stat == "all" else (effect.stat,)
            for stat in stats:
                ops.append((stat, effect.value))
        summed: dict[str, float] = {}
        counts: dict[str, int] = {}
        for stat, value in flat:
            summed[stat] = summed.get(stat, 0.0) + value
            counts[stat] = counts.get(stat, 0) + 1
        result = (tuple(flat), tuple(rate), tuple(summed.items()), tuple(counts.items()))
        self._stat_ops[key] = result
        self._stat_ops_by_card[fast_key] = (card, holomem, result)
        return result


class BoardBase:
    """The boards most models have in common — the account's bench — with their
    per-card stat terms added up ONCE, so a model that differs from the bench
    in five or six boards answers `_terms` from those boards alone.

    `digests` is the bench in ITS order; a model may use this base only when
    its own `characters` start with exactly these names in exactly this order
    (boards replaced in place, new characters appended — what
    `_board_model_from_cache` builds), which `BoardModel.__post_init__` checks
    rather than assumes.

    **Why subtracting a replaced board is exact.** Only the FLAT bucket is
    pre-summed, and only when every flat value is a whole number
    (`flat_exact`): integers far below 2**53 add and subtract exactly, so
    `bench total - replaced boards + this team's boards` is the same integer —
    the same bits — as adding the model's boards up one by one. The RATE
    bucket is never pre-summed: it is kept as the ordered list of boards that
    contribute to it, and the model replays that list with its own boards
    merged in at their positions."""

    __slots__ = ("digests", "names", "index", "flat_exact", "_terms")

    def __init__(self, digests: Mapping[str, BoardDigest]) -> None:
        self.digests: dict[str, BoardDigest] = dict(digests)
        self.names: tuple[str, ...] = tuple(self.digests)
        self.index: dict[str, int] = {name: pos for pos, name in enumerate(self.names)}
        self.flat_exact = all(digest.flat_exact for digest in self.digests.values())
        self._terms: dict[tuple, tuple] = {}

    def terms(self, card: Card, card_key: tuple) -> tuple[dict, dict, tuple]:
        """`(flat totals, flat addend counts, rate contributors)` of every
        board here for one card, with NO leader (`leader_here=False`) — the
        model corrects the leader's own board like any board it replaced.
        `rate contributors` is `(position, holomem, rate addends)` for each
        board whose rate addends are not empty, in board order."""
        hit = self._terms.get(card_key)
        if hit is None:
            total: dict[str, float] = {}
            count: dict[str, int] = {}
            rates: list[tuple[int, str, tuple]] = []
            for pos, (holomem, digest) in enumerate(self.digests.items()):
                if not digest.stat_effects:
                    continue
                ops = digest.stat_ops(holomem, card, card_key, False)
                for stat, value in ops[2]:
                    total[stat] = total.get(stat, 0.0) + value
                for stat, n in ops[3]:
                    count[stat] = count.get(stat, 0) + n
                if ops[1]:
                    rates.append((pos, holomem, ops[1]))
            hit = (total, count, tuple(rates))
            self._terms[card_key] = hit
        return hit


@dataclass(frozen=True, slots=True)
class BoardModel:
    """Every captured character board. Absent characters contribute nothing,
    which is the correct reading: a board with no unlocked node is worth zero,
    and this module never invents one."""

    characters: Mapping[str, CharacterBoard] = field(default_factory=dict)
    # `chr-NNNNN` -> holomem name. The board is name-keyed (`card.member` is a
    # name), but `charts.json` singers are chr-ids, so the per-song methods use
    # this to translate. Empty by default: an all-name caller (every existing
    # fixture) is unchanged, and an unknown id passes through untranslated.
    name_by_chr: Mapping[str, str] = field(default_factory=dict)
    # Derived in `__post_init__`, never passed in — see `leader_scoped_holomem`.
    _leader_scoped: frozenset[str] = field(default=frozenset(), repr=False)
    # Memo for `leader_invariant_power_bonus`, filled on demand and never read
    # by `__eq__`/`__repr__`: it is a cache of a pure function of
    # `(self, card)`, so two models that compare equal must not stop doing so
    # because one has been used and the other has not.
    # Derived in `__post_init__` too — `score_bonus_rate` is a model constant.
    _score_bonus_rate: float = field(default=0.0, repr=False)
    _terms_cache: dict[
        tuple[str | None, str, str, frozenset[str]], tuple[dict, dict]
    ] = field(default_factory=dict, compare=False, repr=False)
    # Every non-conditional `skill_effect_up` effect, in `resolve`'s own
    # characters -> nodes -> effects walk order, as `(holomem, effect)`. Order
    # is load-bearing: float addition is not associative, so summing a subset
    # of these in index order is what keeps `skill_effect_up_for` bit-identical
    # to `resolve`.
    _skill_effect_up_terms: tuple[tuple[str, "BoardEffect"], ...] = field(
        default=(), repr=False
    )
    # `(leader_holomem, card key) -> the indices of the terms above that reach
    # that one card`. Per CARD, so the memo survives the solver swapping one
    # member; the team's value is the sum over the UNION of five such sets.
    _skill_effect_up_cache: dict[
        tuple[str | None, str, str, frozenset[str]], frozenset[int]
    ] = field(default_factory=dict, compare=False, repr=False)
    # The same two shapes for the CONDITIONAL half — the terms `resolve` and
    # `_skill_effect_up_terms` both drop, and which a song-aware caller can
    # evaluate because it knows the cast. Kept apart from the unconditional
    # terms so a caller with no cast cannot accidentally pick them up.
    _conditional_skill_effect_up_terms: tuple[tuple[str, "BoardEffect"], ...] = field(
        default=(), repr=False
    )
    # The same, for the singer-conditional STAT/RATE effects (R-044/062/063:
    # "when [character] is included as a singer, grants +stat% to all"). `resolve`
    # and `_terms` both drop these (`if effect.conditional: continue`); a song-aware
    # caller applies them via `conditional_stat_power_for` when the holomem sings.
    _conditional_stat_terms: tuple[tuple[str, "BoardEffect"], ...] = field(
        default=(), repr=False
    )
    # holomem -> {song_scope: rate}, the Y branch kept per character. The pooled
    # `_score_bonus_rate` above is exact only for `all`.
    _song_score_bonus_by_holomem: Mapping[str, Mapping[str, float]] = field(
        default_factory=dict, repr=False
    )
    # holomem -> (activation_rate_up, cooldown_shorten), summed over that
    # character's own board. A model CONSTANT per character, not a per-team
    # quantity -- see `uptime_for` for the measurement that makes it one.
    _uptime: Mapping[str, tuple[float, float]] = field(
        default_factory=dict, repr=False
    )
    # holomem -> that board's `BoardDigest`. A caller that keeps boards alive
    # across models (`PlanCache`) passes the digests it kept; `__post_init__`
    # builds whichever are missing or belong to another board object. A cache
    # of a pure function of each board, so never part of `__eq__`.
    _digests: Mapping[str, BoardDigest] = field(
        default_factory=dict, compare=False, repr=False
    )
    # Derived: every board's `BoardDigest.flat_exact` holds — see `_terms`.
    _flat_exact: bool = field(default=False, compare=False, repr=False)
    # The bench this model was assembled over, if the caller has one
    # (`PlanCache.bench_base`). `__post_init__` keeps it only when this model's
    # boards really are that bench with some replaced in place and some
    # appended, and when flat sums are exact on both sides; otherwise it is
    # reset to `None` and `_terms` walks every board. A cache, never compared.
    _base: BoardBase | None = field(default=None, compare=False, repr=False)
    # Derived: holomem -> position in `characters`, for every board that is
    # NOT the base's own digest at that position.
    _base_special: Mapping[str, int] = field(
        default_factory=dict, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        # Both derived here rather than at every construction site so no caller
        # can forget them: `board_from_mapping`, the recon tools and the tests
        # all build `BoardModel` directly.
        #
        # Every derivation below reads the per-board `BoardDigest`s and replays
        # them in `characters` order — the same `characters -> nodes -> effects`
        # sequence the walks they replace made, so every accumulated float is
        # the same additions in the same order. A digest handed in by the
        # caller is used only if it is THIS board's (`is`); otherwise it is
        # built here, so a caller that passes none gets the same answer.
        #
        # ONE pass. Most boards are `inert` — nothing but plain stat effects —
        # and are only filed; the rest append to each derivation in the order
        # they are met, which is the order seven separate passes would meet them in.
        given = self._digests
        digests: dict[str, BoardDigest] = {}
        flat_exact = True
        base = self._base
        names: tuple[str, ...] = ()
        base_digests: Mapping[str, BoardDigest] = {}
        shared = 0
        if base is not None:
            names = base.names
            base_digests = base.digests
            shared = len(names)
            if not base.flat_exact or len(self.characters) < shared:
                base = None
        special: dict[str, int] = {}
        leader_scoped: list[str] = []
        song: dict[str, float] = {}
        # The `skill_effect_up` family that `resolve` routes and `_terms`
        # deliberately drops. Conditional effects are kept SEPARATE rather than
        # dropped or flagged: `resolve` excludes them (a trigger the
        # song-agnostic ranking cannot evaluate is carried nowhere), a caller
        # that knows the song's cast can evaluate them, and one that does not
        # must get exactly the song-agnostic answer.
        skill_effect_up_terms: list[tuple[str, BoardEffect]] = []
        conditional_terms: list[tuple[str, BoardEffect]] = []
        # The singer-conditional STAT/RATE terms, kept separate the same way:
        # `_terms`/`resolve` drop every conditional stat, so a song-aware
        # caller re-applies them per song when the holomem is a singer.
        conditional_stat_terms: list[tuple[str, BoardEffect]] = []
        # `song_score_bonus` kept PER HOLOMEM as well as pooled, mirroring
        # `BoardContribution`. The pooled `all` bucket is exact for a
        # whole-roster song and over-counts `solo` and `group` badly; this is
        # what `song_score_bonus_for` needs to get those two right.
        song_by_holomem: dict[str, dict[str, float]] = {}
        # The two ACTIVE-uptime families, summed per character. Constants per
        # holomem -- see `uptime_for`. The scope guard is here, at
        # construction, so a model that cannot be summarised this way never
        # gets built.
        uptime: dict[str, tuple[float, float]] = {}
        pos = 0
        for holomem, board in self.characters.items():
            digest = given.get(holomem)
            if digest is None or digest.board is not board:
                digest = BoardDigest(board)
            digests[holomem] = digest
            if not digest.flat_exact:
                flat_exact = False
            if base is not None:
                if pos >= shared:
                    special[holomem] = pos
                elif names[pos] != holomem:
                    base = None
                elif base_digests[holomem] is not digest:
                    special[holomem] = pos
            pos += 1
            if digest.inert:
                continue
            if digest.leader_scoped:
                leader_scoped.append(holomem)
            if digest.song_ops:
                for key, value in digest.song_ops:
                    song[key] = song.get(key, 0.0) + value
                song_by_holomem[holomem] = dict(digest.song_scopes)
            for effect in digest.skill_effect_up:
                skill_effect_up_terms.append((holomem, effect))
            for effect in digest.conditional_skill_effect_up:
                conditional_terms.append((holomem, effect))
            for effect in digest.conditional_stat:
                conditional_stat_terms.append((holomem, effect))
            if digest.uptime_error is not None:
                node_id, kind, scope = digest.uptime_error
                raise BoardError(
                    f"{holomem} node {node_id}: "
                    f"{kind} at scope {scope!r}. "
                    f"MEASURED over all 323 master SkillTreeNode rows, every "
                    f"node of these two families is branch B / scope 'member', "
                    f"which is what makes this term a per-character constant. "
                    f"A node at another scope invalidates that and must be "
                    f"routed per (card, role), not summed here"
                )
            rate, cooldown = digest.uptime
            if rate or cooldown:
                uptime[holomem] = (rate, cooldown)
        if not flat_exact:
            base = None
        if base is None:
            special = {}
        setter = object.__setattr__
        setter(self, "_digests", digests)
        setter(self, "_flat_exact", flat_exact)
        setter(self, "_base", base)
        setter(self, "_base_special", special)
        setter(self, "_leader_scoped", frozenset(leader_scoped))
        setter(
            self,
            "_score_bonus_rate",
            min(song.get("all", 0.0), SONG_ALL_CAP_PERMIL / 1000.0),
        )
        setter(self, "_skill_effect_up_terms", tuple(skill_effect_up_terms))
        setter(self, "_conditional_skill_effect_up_terms", tuple(conditional_terms))
        setter(self, "_conditional_stat_terms", tuple(conditional_stat_terms))
        setter(self, "_song_score_bonus_by_holomem", song_by_holomem)
        setter(self, "_uptime", uptime)

    def board_for(self, holomem: str) -> CharacterBoard | None:
        return self.characters.get(holomem)

    def fingerprint(self) -> str:
        """A content digest of every unlocked node, for `config_hash`.

        The board is part of the scoring model — it is 29.4-48.7 % of Overall
        Power in the game's own `Live/GetDeck` numbers — so a throughput
        measurement taken against one board does not describe a run against
        another. `config.json`'s merged document does not contain the board, so
        without this digest `bench/throughput.json` could not tell the two apart.

        Order-sensitive on purpose. `resolve` accumulates its buckets in
        `characters -> nodes -> effects` iteration order and float addition is
        not associative, so two models holding the same nodes in a different
        order can produce different last bits and are correctly different
        models to a benchmark. Every routed field is included rather than the
        raw effect id, because it is the ROUTING that scoring reads — a change
        in `parse_effect_id` that re-routes an unchanged id must show up here.
        """
        payload = [
            [
                holomem,
                [
                    [
                        node.node_id,
                        node.connect_multiplier,
                        [
                            [
                                effect.family,
                                effect.kind,
                                effect.stat,
                                effect.value,
                                effect.scope,
                                effect.grouping,
                                effect.song_scope,
                                effect.conditional,
                            ]
                            for effect in node.effects
                        ],
                    ]
                    for node in board.nodes
                ],
            ]
            for holomem, board in self.characters.items()
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    @property
    def leader_scoped_holomem(self) -> frozenset[str]:
        """The holomem whose board holds at least one `scope == "leader"`
        effect — i.e. the only ones for whom `resolve(model, leader, members)`
        can differ from `resolve(model, None, members)`.

        **This is a provable invariance, not a tolerance.** `leader` reaches
        `resolve` through exactly one reader, `_recipients`, which consults it
        in exactly one branch (`effect.scope == "leader"`) and discriminates on
        exactly one attribute (`leader.member != holomem`). For a leader whose
        own holomem appears nowhere in this set, every effect in the model takes
        the identical branch in the identical order as it does with
        `leader=None`, so the accumulated floats are bit-identical — the same
        additions in the same sequence, not two sums that happen to agree.

        `score_card_set` uses this to hoist the board term out of its per-leader
        loop. Without it, `resolve` walks every captured effect once per
        ARRANGEMENT — on one real account 211 effects, 88 % of a board-loaded
        exact run (a 6.2x slowdown) — while most pool leaders have no red node
        and would re-derive a number that cannot change.

        Over-inclusion here is safe (it only costs a `resolve` call);
        under-inclusion is a wrong score. `scaled()` preserves `scope`, and
        `parse_effect_id` hardcodes `scope="account"` for both the `ignored` and
        `song_score_bonus` kinds, so reading raw effect scopes cannot miss one.
        """
        return self._leader_scoped

    def _terms(self, card: Card, leader_holomem: str | None) -> tuple[dict, dict]:
        """This card's `(flat, rate)` stat buckets, memoised per
        `(leader_holomem, card)`.

        `leader_holomem` is `leader.member`, or `None` for no leader. It is the
        ONLY thing about the leader this walk can read — `_recipients` compares
        `leader.member != holomem` and consults nothing else — so two leaders
        sharing a holomem share these buckets exactly, and so does every leader
        outside `leader_scoped_holomem` with the `None` case.

        **Every branch below mirrors `resolve`'s own, in `resolve`'s own order,
        and that is a correctness requirement rather than a stylistic one.** A
        bucket is built by repeated `bucket[stat] = bucket.get(stat, 0.0) +
        value`, so these buckets equal `resolve`'s only if the additions happen
        in the same sequence. They do: the walk is the same nested
        `characters -> nodes -> effects` iteration, and every test it applies is
        a predicate on ONE card, so filtering per card visits that card's
        contributing effects in exactly the order the five-member walk does.

        `resolve` remains the reference implementation and is still what
        `_score_team`'s acceptance numbers were measured through;
        `tests/test_board_landing.py` checks this walk against it across the
        leader/member/grouping/account cross product rather than trusting the
        two to have stayed in step: two derivations and one comparison, not one
        derivation that reports itself.

        Keyed by `(leader_holomem, id, member, generations)` and not by id
        alone: `member` and `generations` are the only two card attributes the
        walk reads, and keying on the id alone would serve one card's buckets
        for a different card carrying the same id — silently, and as a
        plausible number."""
        key = (leader_holomem, card.id, card.member, frozenset(card.generations))
        cached = self._terms_cache.get(key)
        if cached is not None:
            return cached

        # The walk itself lives in `BoardDigest.stat_ops`, one board at a
        # time and memoised there per card, so a bench board's answer survives
        # from one team's model to the next. Replayed here in `characters`
        # order: the same additions, in the same sequence, as one nested walk.
        #
        # **The one regrouping, and why it is not a rounding risk.** When every
        # flat value in the model is a whole number (`_flat_exact`; true on real
        # accounts, where a card collects ~580 flat addends against ~20 rate
        # ones) each board's flat addends are added up per
        # stat FIRST and the subtotal is added here. Every addend, every
        # subtotal and every partial sum is then an integer far below 2**53, so
        # every one of those additions is EXACT and the total is the true
        # integer sum in either grouping — the same bits, by arithmetic and not
        # by luck. A model with one fractional flat value takes the addend-by-
        # addend path instead; the rate bucket (0.01, 0.03 … — not exact in
        # binary) always does.
        card_key = key[1:]
        if self._base is not None:
            flat, rate = self._terms_over_base(card, card_key, leader_holomem)
            result = (_close_grouping(flat), rate)
            self._terms_cache[key] = result
            return result
        flat_index = 2 if self._flat_exact else 0
        flat: dict[str, float] = {}
        rate: dict[str, float] = {}
        for holomem, digest in self._digests.items():
            if not digest.stat_effects:
                continue
            ops = digest.stat_ops(
                holomem,
                card,
                card_key,
                leader_holomem is not None and leader_holomem == holomem,
            )
            flat_ops = ops[flat_index]
            rate_ops = ops[1]
            for stat, value in flat_ops:
                flat[stat] = flat.get(stat, 0.0) + value
            for stat, value in rate_ops:
                rate[stat] = rate.get(stat, 0.0) + value

        flat = _close_grouping(flat)
        self._terms_cache[key] = (flat, rate)
        return (flat, rate)

    def _terms_over_base(
        self, card: Card, card_key: tuple, leader_holomem: str | None
    ) -> tuple[dict, dict]:
        """`_terms`' walk, taken as the base's totals corrected by the boards
        this model does not share with it — see `BoardBase` for why that is
        the same number. Only reached when `__post_init__` kept `_base`.

        The leader's own board is corrected too even when it IS the base's:
        the base was summed with no leader, and under her that one board also
        pays its leader-scope effects."""
        base = self._base
        digests = self._digests
        base_digests = base.digests
        total, count, base_rates = base.terms(card, card_key)
        special = self._base_special
        corrected = list(special.items())
        if (
            leader_holomem is not None
            and leader_holomem not in special
            and leader_holomem in base.index
        ):
            corrected.append((leader_holomem, base.index[leader_holomem]))
        flat_total = dict(total)
        flat_count = dict(count)
        rates: list[tuple[int, tuple]] = []
        replaced: set[str] = set()
        for holomem, pos in corrected:
            old = base_digests.get(holomem)
            if old is not None and old.stat_effects:
                replaced.add(holomem)
                ops = old.stat_ops(holomem, card, card_key, False)
                for stat, value in ops[2]:
                    flat_total[stat] -= value
                for stat, n in ops[3]:
                    flat_count[stat] -= n
            digest = digests[holomem]
            if digest.stat_effects:
                ops = digest.stat_ops(holomem, card, card_key, leader_holomem == holomem)
                for stat, value in ops[2]:
                    flat_total[stat] = flat_total.get(stat, 0.0) + value
                for stat, n in ops[3]:
                    flat_count[stat] = flat_count.get(stat, 0) + n
                if ops[1]:
                    rates.append((pos, ops[1]))
        # A stat is in the bucket exactly when some addend reached it — the
        # count, not the total, says so (a total can be a legitimate 0.0).
        flat = {stat: value for stat, value in flat_total.items() if flat_count[stat]}
        rates.extend(
            (pos, ops) for pos, holomem, ops in base_rates if holomem not in replaced
        )
        rates.sort(key=_FIRST)     # board order; positions are unique
        rate: dict[str, float] = {}
        for _pos, ops in rates:
            for stat, value in ops:
                rate[stat] = rate.get(stat, 0.0) + value
        return (flat, rate)

    def power_bonus_for(
        self, members: tuple[Card, ...], leader_holomem: str | None
    ) -> float:
        """`resolve(self, leader, members).power_bonus(members)`, without the walk.

        Bit-identical to it, and the accumulation loop below is copied from
        `BoardContribution.power_bonus` line for line for that reason: `total`
        is one running float, so `flat` and `rate * base` must be added in the
        same interleaved (members x STAT_NAMES) order or the last bits move.
        Summing per card and adding the subtotals would NOT be the same number.

        Why this exists: `resolve` walks every effect on every captured board —
        211 of them on one real account — and redoing that once per ARRANGEMENT
        is 88 % of a board-loaded run on a 91-card pool. The buckets it rebuilds
        are a pure function of the model, one card and the leader's holomem, so
        they are computed once per combination of those three and reused."""
        total = 0.0
        for card in members:
            flat, rate = self._terms(card, leader_holomem)
            for stat in STAT_NAMES:
                total += flat.get(stat, 0.0)
                # CEILED per (card, stat) over the SUMMED rate -- see
                # `BoardContribution.power_bonus`, which this is the twin of.
                total += math.ceil(rate.get(stat, 0.0) * card.stat(stat))
        return total

    def score_bonus_rate(self) -> float:
        """`resolve(self, leader, members).score_bonus_rate()` — a model
        constant, so it is derived once in `__post_init__` and returned here.

        `resolve` reaches `song_score_bonus` by a `continue` placed BEFORE
        `_recipients` is consulted, so this term depends on neither the leader
        nor the members: nothing that varies across a search can move it.

        ⚠ **This is NOT the board's whole score contribution, and treating it as
        such is a large error.** On a 34-character capture this reads 4.8
        permil, and both contributing nodes belong to characters who need not
        be on the team — it is an account constant that no team choice can
        move. The team-dependent half is `skill_effect_up_for` below."""
        return self._score_bonus_rate

    def uptime_for(self, holomem: str) -> tuple[float, float]:
        """`(activation_rate_up, cooldown_shorten)` for one character, as
        fractions. `(0.0, 0.0)` for a character with no board or no B branch.

        They belong to the ACTIVE UPTIME, not to any score pool —
        `cooldown_effective = cooldown / (1 + reduction)`, which DIVIDES, and
        the rate raises the activation probability. On a real team they can be
        large (0.674 and 0.120).

        **Why one number per character and not per (card, role).** Over all
        323 master `SkillTreeNode` rows — the whole game, not one account —
        every node carrying either family is branch **B**, scope
        **member**, non-conditional: 10 `activation_rate_up`, 3
        `cooldown_shorten`, and none at leader, account or grouping scope.
        `_reaches` resolves member scope to exactly `card.member == holomem`,
        so the value depends on neither the team nor the leader. That is why
        this is a dict built once in `__post_init__` rather than the memoised
        per-arrangement walk `skill_effect_up_for` needs.

        `__post_init__` REFUSES to build a model that breaks the property, so
        a future node at another scope is a loud failure rather than a term
        quietly paid to the wrong cards."""
        return self._uptime.get(holomem, (0.0, 0.0))

    def skill_effect_up_for(
        self, members: tuple[Card, ...], leader_holomem: str | None
    ) -> float:
        """`resolve(self, leader, members).skill_effect_up`, memoised per card.

        The board's `SkillEffectUpPermilUp` — it sits INSIDE
        `CalcScoreUpRatio`'s `(1 + SkillEffectUpPermilUp/1000 + B)` factor,
        multiplying the ACTIVE pool. It is **one team-wide addend, not one per
        recipient**: an effect that reaches anybody is counted exactly once,
        which is `resolve`'s own rule and the reason this cannot be a per-card
        sum. Paying it per recipient would multiply the term by five.

        It is large: Ina'nis' leader-scope node alone is 173 permil, against a
        `score_bonus_rate` of 4.8.

        The memo is per `(leader_holomem, card)` rather than per team because
        that is what survives the solver swapping one member. Each entry is the
        set of term INDICES that card receives; a team's value is the sum over
        the union, taken in index order so float addition happens in exactly
        `resolve`'s sequence and the two stay bit-identical."""
        if not self._skill_effect_up_terms:
            return 0.0
        reached: set[int] = set()
        for card in members:
            key = (
                leader_holomem,
                card.id,
                card.member,
                frozenset(card.generations),
            )
            indices = self._skill_effect_up_cache.get(key)
            if indices is None:
                indices = frozenset(
                    index
                    for index, (holomem, effect) in enumerate(
                        self._skill_effect_up_terms
                    )
                    if _reaches_one(effect, holomem, leader_holomem, card)
                )
                self._skill_effect_up_cache[key] = indices
            reached |= indices
        total = 0.0
        for index, (_holomem, effect) in enumerate(self._skill_effect_up_terms):
            if index in reached:
                total += effect.value
        return total

    def _holomem_names(self, singers: Iterable[str]) -> list[str]:
        """Map the game's chr-ids (what `charts.json` singers are) to the holomem
        NAMES this board is keyed by. Anything already a name -- or an unknown id
        -- passes through, so a name-keyed caller is unchanged and a bad id simply
        misses rather than being invented into a match."""
        return [self.name_by_chr.get(singer, singer) for singer in singers]

    def conditional_skill_effect_up_for(
        self,
        members: tuple[Card, ...],
        leader_holomem: str | None,
        singers: Iterable[str],
    ) -> float:
        """The singer-conditional half of `skill_effect_up`, for ONE song.

        Same term, same slot inside `CalcScoreUpRatio`'s parenthesis, same
        count-once-for-the-team rule as `skill_effect_up_for` — and **one extra
        condition**: the node's own holomem must be among the song's credited
        singers.

        Measured by a controlled triple with everything else held fixed. Aki's `R-002`
        (*"When Aki Rosenthal is included as a singer, Grants Score Support
        Effect of 10.0 % to All"*) moved the deck screen's bonus line by
        **exactly zero** permil on a songless screen, and read **41 permil** on
        SHALLYS (EXPERT), where Aki is the credited Singer. Both halves of the
        conditional, isolated against a zero background.

        Being a singer is a SECOND condition, not a replacement for the first:
        the effect's ordinary `scope` still has to reach the team, which is why
        this runs the same `_reaches_one` test. Aki's node is leader-scoped, so
        under a different leader it pays nothing even on Aki's own song.

        ⚠ **An inference the data cannot separate:** that the trigger names
        the node's OWN holomem. `R-002` sits on Aki's board and names Aki, so
        *"this node's owner sings"* and *"a character named in the effect id
        sings"* coincide on every known node. This implements
        the first — the simpler reading, and the only one the parsed effect id
        can express. A node whose conditional named someone else would separate
        them; none is known to exist. If one appears, this is the function to
        revisit.

        Returns `0.0` for an empty cast, which is what makes every song-agnostic
        caller's answer unchanged."""
        if not self._conditional_skill_effect_up_terms:
            return 0.0
        cast = set(self._holomem_names(singers))
        if not cast:
            return 0.0
        total = 0.0
        for holomem, effect in self._conditional_skill_effect_up_terms:
            if holomem not in cast:
                continue
            # Counted ONCE for the team, exactly as the unconditional half is:
            # `SkillEffectUpPermilUp` is a single addend in the game's
            # parenthesis, so `any` and not a per-card sum.
            if any(
                _reaches_one(effect, holomem, leader_holomem, card)
                for card in members
            ):
                total += effect.value
        return total

    def conditional_stat_power_for(
        self,
        members: tuple[Card, ...],
        leader_holomem: str | None,
        singers: Iterable[str],
    ) -> float:
        """The singer-conditional STAT/RATE power, for ONE song — the Overall
        Power the R-044/062/063 family ("when [character] is included as a singer,
        grants +stat% to all") adds when its holomem is in the song's cast.

        Same power arithmetic as `power_bonus_for` (flat added, rate ceiled per
        (card, stat) over the summed rate), same reach rule (`_reaches_one`), and
        the SAME extra condition as `conditional_skill_effect_up_for`: the node's
        own holomem must be a credited singer. Unlike the skill term this is PER
        CARD (a stat effect reaches each recipient), not one team-wide addend.

        ⚠ The rate ceiling here is taken over the CONDITIONAL rate alone, separate
        from the unconditional rate `power_bonus_for` already ceiled — so a card
        with both a conditional and an unconditional rate on the SAME stat is
        ceiled twice, over-counting by at most 1 per (card, stat) versus one
        combined ceiling. Negligible against a per-song power base, and the price
        of keeping the conditional half a clean add-on rather than threading the
        cast through the memoised `power_bonus_for`.

        `0.0` for an empty cast, so every song-agnostic caller is unchanged."""
        if not self._conditional_stat_terms:
            return 0.0
        cast = set(self._holomem_names(singers))
        if not cast:
            return 0.0
        flat_total = 0.0
        rate_by_card_stat: dict[tuple[str, str], float] = {}
        for holomem, effect in self._conditional_stat_terms:
            if holomem not in cast:
                continue
            for card in members:
                if not _reaches_one(effect, holomem, leader_holomem, card):
                    continue
                stats = STAT_NAMES if effect.stat == "all" else (effect.stat,)
                for stat in stats:
                    if effect.kind == "stat":
                        flat_total += effect.value
                    else:  # rate
                        key = (card.id, stat)
                        rate_by_card_stat[key] = (
                            rate_by_card_stat.get(key, 0.0) + effect.value
                        )
        total = flat_total
        card_by_id = {card.id: card for card in members}
        for (card_id, stat), rate in rate_by_card_stat.items():
            total += math.ceil(rate * card_by_id[card_id].stat(stat))
        return total

    def song_score_bonus_for(self, singers: Iterable[str], *, scope: str) -> float:
        """`ScoreUpBonusRatioBySkillTree` for ONE song — the `BoardModel` twin of
        `BoardContribution.song_score_bonus_for`, so a memoised caller does not
        have to run a full `resolve` walk to get it.

        See that method for the derivation
        (`SkillTreeEffectTargetMusicDialogPresenter::CalculateCurrentEffectValue`
        @`0x67f3670`): the contributing set comes from the SONG, and each
        singer's released effects are filtered by `musicSingerType_` — 3 whole,
        2 unit, 1 solo.

        Clamped to `SONG_ALL_CAP_PERMIL` per song and per this cast, never
        account-wide. Pooling across every character can make a 120-permil
        figure look like a live cap violation when the real solo number is
        **40**."""
        total = sum(
            self._song_score_bonus_by_holomem.get(holomem, {}).get(scope, 0.0)
            for holomem in set(self._holomem_names(singers))
        )
        return min(total, SONG_ALL_CAP_PERMIL / 1000.0)

    def song_score_bonus_by_singer(self, singers: Iterable[str], *, scope: str) -> dict[str, float]:
        """`{holomem: rate}` -- each singer's own share of `song_score_bonus_for`
        BEFORE the cap (Connect multipliers included), so a page can say whose
        yellows the song's bonus comes from and how much of it the cap wastes."""
        return {holomem: self._song_score_bonus_by_holomem.get(holomem, {}).get(scope, 0.0)
                for holomem in sorted(set(self._holomem_names(singers)))}


@dataclass(frozen=True, slots=True)
class BoardContribution:
    """What the board is worth to one (leader, members) arrangement.

    Every term is kept separate rather than pre-summed, because they enter the
    score at four structurally different places and summing them here would
    reintroduce exactly the "one Score Bonus number" error `CalcScore` refutes:

    * `flat_by_card` / `rate_by_card` are summands of **Overall Power**.
    * `skill_effect_up` is the game's `SkillEffectUpPermilUp` — it sits
      **inside** `ActiveSkillEffectDataCollection::CalcScoreUpRatio`'s
      `(1 + SkillEffectUpPermilUp/1000 + B)` factor, multiplying the ACTIVE
      pool, as the family's own name says.
    * `activation_rate_up` / `cooldown_shorten` are per-member modifiers of
      the ACTIVE uptime. `cooldown_effective = cooldown / (1 + reduction)` —
      it DIVIDES. Zero rows outside the board use `cool_time`, so
      the board is the only supplier of the cooldown axis.
    * `song_score_bonus` is `ScoreUpBonusRatioBySkillTree`, the SEPARATE
      multiplicative factor at the end of `CalcScore`, keyed by song scope.
    """

    flat_by_card: Mapping[str, Mapping[str, float]]
    rate_by_card: Mapping[str, Mapping[str, float]]
    skill_effect_up: float
    conditional_skill_effect_up: float
    activation_rate_up: Mapping[str, float]
    cooldown_shorten: Mapping[str, float]
    song_score_bonus: Mapping[str, float]
    # The same Y-branch effects, kept PER HOLOMEM instead of pooled.
    #
    # ⚠ Pooling is correct for exactly one scope and wrong for the other two.
    # From `SkillTreeEffectTargetMusicDialogPresenter::CalculateCurrentEffectValue`
    # @`0x67f3670`: type 19 is aggregated **per song, over that song's own
    # singers**, filtered by `musicSingerType_` (3 whole / 2 unit / 1 solo).
    # So:
    #
    #   "all"    a whole-roster song IS the all-54 sum -> pooling is exact
    #   "group"  only the song's singers contribute    -> pooling over-counts
    #   "solo"   exactly ONE singer contributes        -> pooling over-counts
    #            badly (a 120-permil pooled figure where the real number is 40)
    #
    # `song_score_bonus` above stays as the pooled view because
    # `score_bonus_rate()` reads only `"all"` from it, where pooling is right.
    # This mapping is what a per-song scorer needs: without it the song-specific
    # Y tiles cannot be attributed to the right singers.
    # Defaulted so a caller that only needs the pooled view — every existing
    # test fixture, and `score_bonus_rate()` itself — keeps working. An empty
    # mapping is honest: it says "no per-holomem breakdown was supplied", and
    # `song_score_bonus_for` then returns 0.0 for every cast rather than
    # inventing one from the pooled total.
    song_score_bonus_by_holomem: Mapping[str, Mapping[str, float]] = field(
        default_factory=dict
    )

    def power_bonus(self, members: Iterable[Card]) -> float:
        """The game's own `up_by_skill_tree` line — the "Holomem Board Bonus"
        summand of Overall Power.

        `flat + rate * base`, per card and per stat, where `base` is the
        card's own Member Parameter contribution. The outfit's stat rate is
        measured to apply to Member Parameter exactly; the board's rates are
        read the same way (an inference from that single measurement, and it
        is what reproduces the +28.73 pp acceptance number)."""
        total = 0.0
        for card in members:
            flat = self.flat_by_card.get(card.id, {})
            rate = self.rate_by_card.get(card.id, {})
            for stat in STAT_NAMES:
                total += flat.get(stat, 0.0)
                total += math.ceil(rate.get(stat, 0.0) * card.stat(stat))
        return total

    def deck_screen_score_bonus(self, base_active_score_ratio: float) -> float:
        """The deck screen's "Holomem Board Bonus" **Score Bonus** line.

        The deck panel is a display decomposition, not the score formula: it
        attributes to the board the uplift the board's `SkillEffectUpPermilUp`
        produces on the score sources beneath it. So the line is

            skill_effect_up * base_active_score_ratio

        where `base_active_score_ratio` is the Score Bonus the ACTIVE pool
        would show without this term. Present so the acceptance test can be
        stated in the game's own units; the ranking path does not use it."""
        return self.skill_effect_up * base_active_score_ratio

    def score_bonus_rate(self) -> float:
        """`ScoreUpBonusRatioBySkillTree` for a song-agnostic ranking — the
        song-INDEPENDENT Y term only.

        The Y branch is mostly per-song, but its ceiling includes a
        song-independent *"+0.7 % on all songs"* that a per-song exclusion
        does not cover. The song-scoped buckets stay on `song_score_bonus`
        for per-song callers.

        Clamped to `SONG_ALL_CAP_PERMIL` (100 permil = 10.0 %, the client
        constant `0x75cec38` = `0000c842`).
        Exactly correct under the per-song rule — a whole-roster song *is* the
        all-54 sum — and it is the only capped quantity that reaches a ranking.
        Usually tiny (one real capture sums to 2 permil) and up to 3.78x at a
        maximal roster.

        The `solo`/`group` buckets are deliberately NOT clamped and NOT pooled
        across characters here. Doing that correctly is
        not a clamp: `solo` takes one board and `group` takes the song's
        singers, so it needs `song_score_bonus` keyed per character, which
        `resolve()` does not do. The comment at `resolve()`'s song branch is
        true only of `all`."""
        return min(
            self.song_score_bonus.get("all", 0.0), SONG_ALL_CAP_PERMIL / 1000.0
        )

    def song_score_bonus_for(
        self, singers: Iterable[str], *, scope: str
    ) -> float:
        """`ScoreUpBonusRatioBySkillTree` for ONE song, as the game computes it.

        The song-specific Y tiles need to know *which boards contribute* —
        pooling them account-wide is wrong by a lot.

        From
        `SkillTreeEffectTargetMusicDialogPresenter::CalculateCurrentEffectValue`
        @`0x67f3670`: the contributing set comes from the SONG
        (`music.IsWholeMusic ? get_RegularCharacterListCache :
        Music::get_Characters`), and each singer's released effects are then
        filtered by `musicSingerType_` — **3** whole / **2** unit / **1** solo.

        So `singers` is the song's own cast, and `scope` is its kind:

        * `"solo"` — exactly ONE singer. 75 permil per full board,
          which is the best score-per-point in the tree for that one song
          (25 points), and the 100-permil cap is **structurally unreachable**.
        * `"group"` — the song's unit. 40 permil per full board, crossing the
          cap at 3 singers.
        * `"all"` — a whole-roster song, where the cast IS all 54, so this
          agrees with `score_bonus_rate()` by construction rather than by
          coincidence.

        Clamped to `SONG_ALL_CAP_PERMIL`, which is per song and per this cast —
        never account-wide. Pooling across every character can make a
        120-permil figure look like a live cap violation when the real solo
        number is **40**.

        Returns a RATE (permil/1000), matching `score_bonus_rate()`."""
        total = sum(
            self.song_score_bonus_by_holomem.get(holomem, {}).get(scope, 0.0)
            for holomem in set(singers)
        )
        return min(total, SONG_ALL_CAP_PERMIL / 1000.0)


# `(effect_id, branch) -> BoardEffect`, filled by `parse_effect_id` on success.
# `BoardEffect` is frozen, so one instance may be shared by every board that
# unlocks the node; uncached, the parse (regex + routing) is 15 % of a develop,
# because every `board_from_mapping` re-parses the whole bench.
# Failures are NOT cached: an unparseable id raises every time.
_PARSE_CACHE: dict[tuple[str, str], "BoardEffect"] = {}


def parse_effect_id(effect_id: str, *, branch: str) -> BoardEffect:
    """One `skill_tree_effect-...` id into a routed `BoardEffect`.

    Raises `BoardError` on anything unrecognised rather than returning a
    zero-valued effect. "Verify artefacts, not counters": a board term that is
    silently zero looks exactly like a board term that is correctly zero.

    Memoised per `(effect_id, branch)` in `_PARSE_CACHE` — a pure function of
    its arguments returning an immutable value, so the cached instance IS the
    answer, not a copy of it."""
    if not isinstance(effect_id, str):
        return _parse_effect_id_uncached(effect_id, branch=branch)
    key = (effect_id, branch)
    cached = _PARSE_CACHE.get(key)
    if cached is not None:
        return cached
    effect = _parse_effect_id_uncached(effect_id, branch=branch)
    _PARSE_CACHE[key] = effect
    return effect


def _parse_effect_id_uncached(effect_id: str, *, branch: str) -> BoardEffect:
    match = _EFFECT_RE.match(effect_id or "")
    if match is None:
        raise BoardError(f"unparseable board effect id: {effect_id!r}")

    family = match.group("family")
    raw_value = int(match.group("value"))
    rest = match.group("rest")
    # A `passive_trigger` id names a run-time trigger rather than a target.
    conditional = match.group("kind") == "passive_trigger"

    if family.startswith(_IGNORED_FAMILY_PREFIXES):
        return BoardEffect(
            family=family, kind="ignored", stat="", value=0.0, scope="account"
        )

    scope, grouping = _scope_for(branch, rest, effect_id)

    if family in _STAT_FAMILIES:
        kind, stat = _STAT_FAMILIES[family]
        value = float(raw_value) if kind == "stat" else raw_value / 1000.0
        # A stat id whose *target* clause carries a further passive trigger is
        # still a stat effect; the trigger only decides whether it fires.
        return BoardEffect(
            family=family,
            kind=kind,
            stat=stat,
            value=value,
            scope=scope,
            grouping=grouping,
            conditional=conditional or "passive_trigger" in rest,
        )

    if family == _SKILL_EFFECT_UP:
        return BoardEffect(
            family=family,
            kind="skill_effect_up",
            stat="",
            value=raw_value / 1000.0,
            scope=scope,
            grouping=grouping,
            conditional=conditional or "passive_trigger" in rest,
        )
    if family == _ACTIVATION_RATE_UP:
        return BoardEffect(
            family=family,
            kind="activation_rate_up",
            stat="",
            value=raw_value / 1000.0,
            scope=scope,
            grouping=grouping,
            conditional=conditional or "passive_trigger" in rest,
        )
    if family == _COOLDOWN_SHORTEN:
        return BoardEffect(
            family=family,
            kind="cooldown_shorten",
            stat="",
            value=raw_value / 1000.0,
            scope=scope,
            grouping=grouping,
            conditional=conditional or "passive_trigger" in rest,
        )
    if family == _SONG_SCORE_BONUS:
        # `...-<singer_type>-always-<singer_type>[-<unit>]`; the leading token
        # after the trigger name is the song scope: `all`, `solo` or `group`.
        song_scope = rest.split("-")[-1] if rest else ""
        for token in rest.split("-"):
            if token in ("all", "solo", "group"):
                song_scope = token
                break
        return BoardEffect(
            family=family,
            kind="song_score_bonus",
            stat="",
            value=raw_value / 1000.0,
            scope="account",
            song_scope=song_scope,
            conditional=False,
        )

    raise BoardError(
        f"unrouted board effect family {family!r} (from {effect_id!r}) — add it "
        "to board.py deliberately rather than letting it score as zero"
    )


def _scope_for(branch: str, rest: str, effect_id: str) -> tuple[str, str | None]:
    grouping_match = _GROUPING_RE.match(rest)
    if grouping_match is not None:
        return "grouping", grouping_match.group("grouping")
    if rest.endswith("set_live_leader"):
        return "leader", None
    if rest.startswith("skill_tree_character"):
        return "member", None
    if rest.startswith("all"):
        return "account", None
    if branch == "Y":
        return "account", None
    raise BoardError(f"unrecognised board effect target scope: {effect_id!r}")


def _reaches(effect: BoardEffect, holomem: str, card: Card) -> bool:
    """Whether one MEMBER card is a recipient of one effect, for the three
    scopes that do not consult the leader.

    `_recipients` is written in terms of this rather than repeating the three
    predicates, so `BoardModel._invariant_terms` — which must filter per card —
    and the five-member walk cannot drift apart. `"leader"` is deliberately not
    handled here: it is the one scope that depends on something other than the
    recipient card, and it stays visible in `_recipients` where the leader is."""
    if effect.scope == "member":
        return card.member == holomem
    if effect.scope == "grouping":
        # The effect names a group TOKEN (`grp-myth`); a card carries either that
        # token (board tests) or the catalogue's DISPLAY name (`Myth`) — accept both.
        return (effect.grouping in card.generations
                or _GENERATION_BY_GROUPING.get(effect.grouping) in card.generations)
    if effect.scope == "account":
        return True
    raise BoardError(f"unknown board scope {effect.scope!r}")


def _recipients(
    effect: BoardEffect, holomem: str, leader: Card | None, members: tuple[Card, ...]
) -> tuple[Card, ...]:
    """Which of the five member cards this effect actually reaches.

    The leader is never a recipient of *stats*: it contributes its outfit
    skill and nothing else, which is why `Team` has no `all_cards()`. That
    the R branch reaches exactly five cards is not an
    assumption of convenience — it is what reproduces +28.73 pp; reaching six
    overshoots it by 20 %."""
    if effect.scope == "leader":
        if leader is None or leader.member != holomem:
            return ()
        return members
    if effect.scope == "account":
        return members
    return tuple(card for card in members if _reaches(effect, holomem, card))


def _reaches_one(
    effect: BoardEffect, holomem: str, leader_holomem: str | None, card: Card
) -> bool:
    """`card in _recipients(effect, holomem, leader, members)`, for one card.

    Written as the per-card decomposition of `_recipients` and kept immediately
    beside it so the two cannot drift: every branch here mirrors one branch
    there. `leader.member` is the only thing `_recipients` reads off the leader,
    so taking the holomem rather than the card is the exact dependency — which
    is what makes `skill_effect_up_for`'s memo sound."""
    if effect.scope == "leader":
        return leader_holomem is not None and leader_holomem == holomem
    if effect.scope == "account":
        return True
    return _reaches(effect, holomem, card)


def resolve(
    model: BoardModel, leader: Card | None, members: tuple[Card, ...]
) -> BoardContribution:
    """Every unlocked node on every captured board, routed to the five members.

    Deterministic in `model.characters` iteration order then node order then
    effect order, so a caller that needs bit-identical parity between two
    drivers gets it by construction (the same reason `_board_score_rate` is a
    shared function in `scoring.py` rather than two summing loops)."""
    flat: dict[str, dict[str, float]] = {card.id: {} for card in members}
    rate: dict[str, dict[str, float]] = {card.id: {} for card in members}
    activation: dict[str, float] = {}
    cooldown: dict[str, float] = {}
    song: dict[str, float] = {}
    song_by_holomem: dict[str, dict[str, float]] = {}
    skill_effect_up = 0.0
    conditional_skill_effect_up = 0.0

    for holomem, board in model.characters.items():
        for node in board.nodes:
            for raw_effect in node.effects:
                effect = raw_effect.scaled(node.connect_multiplier)
                if effect.kind == "ignored":
                    continue
                if effect.kind == "song_score_bonus":
                    # Not multiplied by a recipient count — the Y branch pays
                    # per SONG, not per member.
                    #
                    # ⚠ The pooled total below is account-wide, which is exact
                    # only for the `"all"` scope (a whole-roster song IS the
                    # all-54 sum). `solo` and `group` are also kept per
                    # holomem, because only the song's own singers contribute
                    # to those. Keeping both is what lets
                    # `score_bonus_rate()` stay bit-identical while a per-song
                    # caller gets a number that is actually correct.
                    key = effect.song_scope or "all"
                    song[key] = song.get(key, 0.0) + effect.value
                    per = song_by_holomem.setdefault(holomem, {})
                    per[key] = per.get(key, 0.0) + effect.value
                    continue
                targets = _recipients(effect, holomem, leader, members)
                if not targets:
                    continue
                if effect.kind == "skill_effect_up":
                    # One team-wide term, not one per recipient: the game's
                    # `SkillEffectUpPermilUp` is a single addend inside
                    # `CalcScoreUpRatio`'s parenthesis, and the effect's own
                    # target clause (`-target-all-`) says it applies to the
                    # team, not that it is paid five times.
                    if effect.conditional:
                        conditional_skill_effect_up += effect.value
                    else:
                        skill_effect_up += effect.value
                    continue
                if effect.conditional:
                    # A trigger the song-agnostic ranking cannot evaluate.
                    # Carried nowhere rather than counted.
                    continue
                for card in targets:
                    if effect.kind == "stat":
                        bucket = flat[card.id]
                    elif effect.kind == "rate":
                        bucket = rate[card.id]
                    elif effect.kind == "activation_rate_up":
                        activation[card.id] = activation.get(card.id, 0.0) + effect.value
                        continue
                    elif effect.kind == "cooldown_shorten":
                        cooldown[card.id] = cooldown.get(card.id, 0.0) + effect.value
                        continue
                    else:  # pragma: no cover — parse_effect_id raises first
                        raise BoardError(f"unknown board effect kind {effect.kind!r}")
                    if _is_grouping_all(effect):
                        bucket[_GROUPING_KEY] = bucket.get(_GROUPING_KEY, 0.0) + effect.value
                        continue
                    stats = STAT_NAMES if effect.stat == "all" else (effect.stat,)
                    for stat in stats:
                        bucket[stat] = bucket.get(stat, 0.0) + effect.value

    flat = {card_id: _close_grouping(bucket) for card_id, bucket in flat.items()}
    return BoardContribution(
        flat_by_card=flat,
        rate_by_card=rate,
        skill_effect_up=skill_effect_up,
        conditional_skill_effect_up=conditional_skill_effect_up,
        activation_rate_up=activation,
        cooldown_shorten=cooldown,
        song_score_bonus=song,
        song_score_bonus_by_holomem=song_by_holomem,
    )


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

_BOARD_SCHEMA = """

    {"schema_version": "1",
     "characters": {
       "<holomem name, matching Card.member>": [
         {"node": "R-001",
          "effects": ["skill_tree_effect-..."],
          "connect_multiplier": 2.4}]}}
"""


def load_board(path: str | Path) -> BoardModel | None:
    """A `BoardModel` from a board file, or `None` if the file is absent.

    `None` for an absent file is deliberate, and is the same contract
    `load_scoring_config` has: no board data is a first-class, expected state.
    `data/` ships none, because a board is captured ACCOUNT state. An absent
    board makes `scoring._board_bonus` the identity, which is exactly right.

    Keyed by **holomem**, not by card id, because the board is a property of
    the character and reaches every skin of her. `effects` holds the game's
    own effect ids verbatim so the file stays checkable against
    `SkillTreeNode` by eye, and `connect_multiplier` is `1 + permil/1000` for
    whatever Connect shape covers the node — an INPUT, because the placed
    card's Bloom level is not recoverable from the captured state (see the
    module docstring)."""
    file_path = Path(path)
    if not file_path.exists():
        return None
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BoardError(f"{file_path}: unreadable board file: {exc}") from exc
    return board_from_mapping(raw, source=str(file_path))


def board_from_mapping(raw: object, *, source: str = "<mapping>") -> BoardModel:
    """The parsing half of `load_board`, split out so a caller holding an
    already-decoded mapping (a recon tool, a test) need not write a file
    first."""
    if not isinstance(raw, dict):
        raise BoardError(f"{source}: board file must be an object{_BOARD_SCHEMA}")
    characters_raw = raw.get("characters")
    if not isinstance(characters_raw, dict):
        raise BoardError(f"{source}: missing object key 'characters'{_BOARD_SCHEMA}")

    characters: dict[str, CharacterBoard] = {}
    for holomem, node_rows in characters_raw.items():
        if not isinstance(node_rows, list):
            raise BoardError(f"{source}: characters[{holomem!r}] must be a list")
        nodes: list[BoardNode] = []
        for row in node_rows:
            if not isinstance(row, dict):
                raise BoardError(f"{source}: characters[{holomem!r}] holds a non-object")
            node_id = row.get("node")
            if not isinstance(node_id, str) or not node_id:
                raise BoardError(
                    f"{source}: characters[{holomem!r}] has a row with no 'node' id"
                )
            effect_ids = row.get("effects") or []
            if not isinstance(effect_ids, list):
                raise BoardError(f"{source}: {node_id}'s 'effects' must be a list")
            multiplier = row.get("connect_multiplier", 1.0)
            if not isinstance(multiplier, (int, float)) or multiplier < 1.0:
                raise BoardError(
                    f"{source}: {node_id}'s connect_multiplier must be >= 1.0 "
                    f"(1 + permil/1000), got {multiplier!r}"
                )
            nodes.append(
                BoardNode(
                    node_id=node_id,
                    effects=tuple(
                        parse_effect_id(effect_id, branch=node_id[:1])
                        for effect_id in effect_ids
                    ),
                    connect_multiplier=float(multiplier),
                )
            )
        characters[holomem] = CharacterBoard(holomem=holomem, nodes=tuple(nodes))
    name_by_chr = raw.get("name_by_chr") or {}
    if not isinstance(name_by_chr, dict):
        raise BoardError(f"{source}: 'name_by_chr' must be an object of chr-id -> name")
    return BoardModel(characters=characters, name_by_chr=dict(name_by_chr))
