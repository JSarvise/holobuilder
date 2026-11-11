"""The Holomem Board.

Each of the 54 holomem has her own board of 154 node groups in five branches; unlocking a
node group turns on its effect. On a typical deck the board is a large share of Overall
Power (about 28 %) and a smaller one of the Score Bonus (about 11 %).

| branch | scope | read from | tiles |
|---|---|---|---|
| **R** | applies **only while that holomem is the unit Leader**, and then to the *whole* team | every R row's effect id ends `-skill_tree_effect_target-all-set_live_leader` | 63 |
| **B** | applies **always**, to **that holomem's own card**, while she is a member | `-skill_tree_effect_target-skill_tree_character-always` | 31 |
| **G** | applies **always**, to **every** card | `-skill_tree_effect_target-all-always` (179 of 182 rows) | 182 rows |
| **G** (3 groups) | applies **always**, to cards of **that holomem's own grouping** | `all_parameter_up_for_character_grouping-...-skill_tree_effect_target-character_grouping-grp-<group>-always` | `G-008`, `G-011`, `G-021` |
| **Y** | per-**song** score bonus and Holowork rewards | `live_score_bonus_add_permil_up_by_music_...` | 31 |
| **S** | the 4 Connect slots you place a card on | — | 4 |

Effect ids are self-describing and are parsed, not tabulated (`parse_effect_id`):

    skill_tree_effect-<family>-<value>-skill_tree_effect_target-<target>-<condition>

A `<family>` ending `_permil_up` carries a per-mille rate; otherwise the value is flat stat
points. Both shapes occur on the same branch, and the two units must not be mixed.
`SkillTreeNode` has no per-node level (multi-row groups are per-song Y variants or the
54-row grouping greens).

Green holds two kinds of node. 179 of its 182 rows target every card (`"account"` scope):
a constant across teams. `G-008`, `G-011` and `G-021` have one row per character targeting
that character's own grouping (`"grouping"` scope), so they reach only the team members of
the unlocking holomem's generation and do change rankings. Their value depends on the
group's size (G-008 50/75/100, G-011 150/225/300, G-021 100/150/200: "+450 all stats to her
generation" is the middle column), and a card receives at most `GROUPING_ALL_CAP`.

## Connect nodes

A card placed on a Connect (S) node multiplies every unlocked tile inside that card's own
fixed shape, anchored at that node, whatever the tile's unit (Calliope 5★ at 140 % turns
"+50 to All" into +120 and "Score Support 10 %" into 24 %):

    boosted = base * (1 + connect_permil / 1000)

Every Connect effect has two levels (base and Bloom 5, level 2 = level 1 + 500 permil in
master `SkillTreeConnectEffect`), and the captured board state records the placed card but
not its bloom, so `BoardNode.connect_multiplier` is an input to this module.

## Scope limits

* The Y branch's per-song terms come from the song's own cast (`song_score_bonus_for`);
  only the song-independent `"all"` bucket enters `score_bonus_rate`.
* A leader's BLUE nodes do not count unless she is also a member: blue applies "in the
  unit", and the leader slot is not a member slot.

The product reads the board through `BoardModel`'s memoised readers; the plain reference
walk they are tested against is `tests/support/board_reference.py` (`resolve`). Every
memoised path keeps that walk's float accumulation order, so the two are bit-identical.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .card import Card

__all__ = [
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
]


class BoardError(Exception):
    """A board record that cannot be interpreted. Raised rather than guessed: a
    silently-zero term is indistinguishable from a correct zero."""


STAT_NAMES: tuple[str, ...] = ("performance", "technique", "sense")

# "leader"   — only while that holomem is the unit Leader; reaches all members.
# "member"   — always, but only that holomem's own card, and only if she is
#              among the five members.
# "account"  — always, every member. The constant half of the green branch.
# "grouping" — always, but only members sharing the unlocking holomem's
#              grouping. The discriminating half of the green branch.

# The type-19 (`all` scope) per-song score-bonus ceiling, a client constant:
# 100 permil, displayed as 10.0 %. Per song and cast, never account-wide.
SONG_ALL_CAP_PERMIL: int = 100

# The type-20 ceiling (`main_SkillTreeEffectValueLimit`): the grouping greens
# (G-008/011/021) a card RECEIVES, summed over every source board whose grouping it
# is in, add at most 900 to each stat, as the client's grouping dialog sums them per
# receiver. Applied to the Connect-scaled sum (the limit is on the effect value).
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


def _is_grouping_all(effect: BoardEffect) -> bool:
    return effect.scope == "grouping" and effect.kind == "stat" and effect.stat == "all"


# The families this module knows how to route, and where each one goes.
# `("stat", <stat or "all">)`  — flat stat points.
# `("rate", <stat or "all">)`  — per-mille of that stat's BASE value (Member
#   Parameter), as the outfit's own stat rate applies.
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
_SONG_SCORE_BONUS = "live_score_bonus_add_permil_up_by_music_skill_tree_character_and_music_singer_type"

# Reward/economy families. Parsed so an unknown family still raises, but they
# reach neither power nor score.
_IGNORED_FAMILY_PREFIXES = (
    "work_reward_",
    "live_reward_",
    "mini_game_reward_",
    # LIFE + leader-active-addition/level: no ranking term, so worth 0 rather than
    # an error. Raising would hurt the planner: a life node's path shares the red
    # corridor's prefix (R-001, R-002...), and the beam marks a whole failed path
    # unroutable, so every later red buy would be skipped.
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
# `holoX`, `3期生`); without this map a gen-scoped green never matches a real card.
# `_reaches` accepts either form (the board tests build cards with the token).
# Same mapping as `import_to_cards.GENERATION_BY_GROUP`.
_GENERATION_BY_GROUPING = {
    "grp-gen_0": "0期生",
    "grp-gen_1": "1期生",
    "grp-gen_2": "2期生",
    "grp-gen_3": "3期生",
    "grp-gen_4": "4期生",
    "grp-gen_5": "5期生",
    "grp-gamers": "ゲーマーズ",
    "grp-holox": "holoX",
    "grp-indonesia-gen_1": "ID1",
    "grp-indonesia-gen_2": "ID2",
    "grp-indonesia-gen_3": "ID3",
    "grp-myth": "Myth",
    "grp-promise": "Promise",
    "grp-advent": "Advent",
    "grp-regloss": "ReGLOSS",
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
    # A `skill_tree_effect_passive_trigger` the deck preview does not fire, e.g.
    # R-002's `music_skill_tree_character` (only on that holomem's own song).
    # Excluded from the song-agnostic terms; song-aware callers apply it.
    conditional: bool = False

    def scaled(self, multiplier: float) -> BoardEffect:
        """This effect with a Connect multiplier applied, whatever its unit.

        The client ceils the product, only when a connect effect exists (hence the
        `multiplier == 1.0` early return), in doubles
        (`SkillTreeUtility::GetEffectValueWithConnectEffectsInternal`):

            value = Ceiling( (1 + connectPermil/1000) x base )

        e.g. an `all`-stat node of 50 at x1.55 carries 78, not 77.5.

        Applied to `stat` only: the game shows `R-049` (4.0 % at x1.55) as 6.2 %, so
        a permil-valued kind is not ceiled to a whole permil. Do not extend this to
        the permil kinds."""
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
    covering this node, or `1.0`: an input (module docstring)."""

    node_id: str
    effects: tuple[BoardEffect, ...]
    connect_multiplier: float = 1.0

    @property
    def branch(self) -> str:
        return self.node_id[:1]


@dataclass(frozen=True, slots=True)
class CharacterBoard:
    """One holomem's unlocked nodes. `holomem` is the `Card.member` name: the board
    belongs to the character and reaches every card of hers, never one card id."""

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

    A `BoardModel` is rebuilt per candidate team (`board_model_from_plan`), but only the
    team's own five or six boards differ between two teams: the other ~50 are the
    account's bench, the same frozen `CharacterBoard` objects. Re-walking them all would
    cost about 2/3 of the enumeration leaf, so a caller that keeps boards alive
    (`PlanCache`) keeps their digests too and the model only stitches them.

    Nothing here is summed across boards: float addition is not associative, so a digest
    stores the ordered ADDENDS of its own board and the model replays them in
    `characters -> nodes -> effects` order. The only sums taken here are per-board ones
    the model takes anyway (`song_scopes`, `uptime`).

    Identity is the point: a memo attached to one board object (`board`), which the
    model checks with `is` before trusting a digest it was handed."""

    __slots__ = (
        "board",
        "leader_scoped",
        "song_ops",
        "song_scopes",
        "skill_effect_up",
        "conditional_skill_effect_up",
        "conditional_stat",
        "uptime",
        "uptime_error",
        "stat_effects",
        "flat_exact",
        "inert",
        "_stat_ops",
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
        # than "member". The model raises it (`__post_init__`), naming the holomem
        # it keys this board by.
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
            effect.value == math.floor(effect.value) and abs(effect.value) <= _FLAT_EXACT_MAX
            for effect in stat_effects
            if effect.kind == "stat"
        )
        # Nothing here but plain stat effects: `BoardModel.__post_init__` has
        # nothing to derive from this board and only files its digest.
        self.inert = not (
            leader_scoped
            or song_ops
            or skill_effect_up
            or conditional_skill_effect_up
            or conditional_stat
            or uptime_error is not None
            or rate
            or cooldown
        )
        self._stat_ops: dict[tuple, tuple[tuple, tuple, tuple]] = {}
        # The same memo reached by the card OBJECT: `(id(card), leader_here) ->
        # (card, holomem, result)`, the card kept alive in the entry so a recycled
        # `id()` cannot serve another card's answer. Hashing the content key is
        # most of what a lookup costs, and a search asks millions of times.
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
            return cached  # type: ignore[return-value]
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
        self._stat_ops[key] = result  # type: ignore[assignment]
        self._stat_ops_by_card[fast_key] = (card, holomem, result)
        return result


class BoardBase:
    """The boards most models have in common — the account's bench — with their
    per-card stat terms added up ONCE, so a model that differs from the bench
    in five or six boards answers `_terms` from those boards alone.

    `digests` is the bench in its order; a model may use this base only when its own
    `characters` start with exactly these names in this order (boards replaced in place,
    new characters appended), which `BoardModel.__post_init__` checks.

    Subtracting a replaced board is exact: only the FLAT bucket is pre-summed, and only
    when every flat value is a whole number (`flat_exact`), so `bench total - replaced
    boards + this team's boards` is the same integer as adding the boards one by one.
    The RATE bucket is never pre-summed: the model replays the ordered list of
    contributing boards with its own merged in at their positions."""

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
    """Every captured character board. An absent character contributes nothing."""

    characters: Mapping[str, CharacterBoard] = field(default_factory=dict)
    # `chr-NNNNN` -> holomem name. The board is name-keyed (`card.member` is a
    # name), but `charts.json` singers are chr-ids, so the per-song methods use
    # this to translate; an unknown id passes through untranslated.
    name_by_chr: Mapping[str, str] = field(default_factory=dict)
    # Derived in `__post_init__`, never passed in — see `leader_scoped_holomem`.
    _leader_scoped: frozenset[str] = field(default=frozenset(), repr=False)
    # Derived in `__post_init__` too — `score_bonus_rate` is a model constant.
    _score_bonus_rate: float = field(default=0.0, repr=False)
    # Memo of a pure function of `(self, card)`, never compared, so two equal models
    # stay equal whether or not one has been used.
    _terms_cache: dict[tuple[str | None, str, str, frozenset[str]], tuple[dict, dict]] = field(
        default_factory=dict, compare=False, repr=False
    )
    # Every non-conditional `skill_effect_up` effect, in characters -> nodes ->
    # effects walk order, as `(holomem, effect)`. Order is load-bearing: float
    # addition is not associative, so summing a subset in index order keeps
    # `skill_effect_up_for` bit-identical to the reference walk.
    _skill_effect_up_terms: tuple[tuple[str, BoardEffect], ...] = field(default=(), repr=False)
    # `(leader_holomem, card key) -> the indices of the terms above that reach
    # that one card`. Per CARD, so the memo survives the solver swapping one
    # member; the team's value is the sum over the UNION of five such sets.
    _skill_effect_up_cache: dict[tuple[str | None, str, str, frozenset[str]], frozenset[int]] = field(
        default_factory=dict, compare=False, repr=False
    )
    # The CONDITIONAL (singer-triggered) skill-effect-up terms, kept apart so a
    # caller with no cast cannot pick them up; a song-aware caller evaluates them.
    _conditional_skill_effect_up_terms: tuple[tuple[str, BoardEffect], ...] = field(default=(), repr=False)
    # The same, for the singer-conditional STAT/RATE effects (R-044/062/063:
    # "when [character] is included as a singer, grants +stat% to all"). `_terms`
    # drops these; a song-aware caller applies them via `conditional_stat_power_for`.
    _conditional_stat_terms: tuple[tuple[str, BoardEffect], ...] = field(default=(), repr=False)
    # holomem -> {song_scope: rate}, the Y branch kept per character. The pooled
    # `_score_bonus_rate` above is exact only for `all`.
    _song_score_bonus_by_holomem: Mapping[str, Mapping[str, float]] = field(default_factory=dict, repr=False)
    # holomem -> (activation_rate_up, cooldown_shorten), summed over that
    # character's own board: a constant per character (`uptime_for`).
    _uptime: Mapping[str, tuple[float, float]] = field(default_factory=dict, repr=False)
    # holomem -> that board's `BoardDigest`. A caller that keeps boards alive
    # across models (`PlanCache`) passes the digests it kept; `__post_init__`
    # builds whichever are missing or belong to another board object.
    _digests: Mapping[str, BoardDigest] = field(default_factory=dict, compare=False, repr=False)
    # Derived: every board's `BoardDigest.flat_exact` holds — see `_terms`.
    _flat_exact: bool = field(default=False, compare=False, repr=False)
    # The bench this model was assembled over, if the caller has one
    # (`PlanCache.bench_base`). `__post_init__` keeps it only when this model's
    # boards are that bench with some replaced in place and some appended, and
    # flat sums are exact on both sides; otherwise `_terms` walks every board.
    _base: BoardBase | None = field(default=None, compare=False, repr=False)
    # Derived: holomem -> position in `characters`, for every board that is
    # NOT the base's own digest at that position.
    _base_special: Mapping[str, int] = field(default_factory=dict, compare=False, repr=False)
    # The model a `with_cooldowns` copy was made from (the root of a chain of copies), or
    # `None`. A copy differs from it only in the cooldown terms, so anything that is not a
    # function of the cooldowns (the Overall Power) can be computed once for both.
    _cooldown_source: BoardModel | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        # Derived here rather than at every construction site so no caller can
        # forget them (`board_from_mapping`, the recon tools and the tests all
        # build `BoardModel` directly).
        #
        # One pass over the per-board `BoardDigest`s in `characters` order, so every
        # accumulated float is the same additions in the same order as a plain
        # `characters -> nodes -> effects` walk. A digest handed in is used only if
        # it is this board's (`is`). Most boards are `inert` (plain stat effects
        # only) and are only filed.
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
        # The `skill_effect_up` family, which `_terms` leaves out. Conditional
        # effects are kept separate: a caller that knows the song's cast evaluates
        # them, one that does not gets exactly the song-agnostic answer.
        skill_effect_up_terms: list[tuple[str, BoardEffect]] = []
        conditional_terms: list[tuple[str, BoardEffect]] = []
        # The singer-conditional STAT/RATE terms, kept separate the same way.
        conditional_stat_terms: list[tuple[str, BoardEffect]] = []
        # `song_score_bonus` kept per holomem as well as pooled: the pooled `all`
        # bucket is exact for a whole-roster song and over-counts `solo` and
        # `group`, which `song_score_bonus_for` needs per singer.
        song_by_holomem: dict[str, dict[str, float]] = {}
        # The two ACTIVE-uptime families, summed per character (`uptime_for`). The
        # scope guard is here, so a model that cannot be summarised this way is
        # never built.
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
                    f"Over all 323 master SkillTreeNode rows, every "
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

        The board is part of the scoring model (a large share of Overall Power) and
        the merged `config.json` does not contain it, so a result recorded against
        one board must not be confused with another.

        Order-sensitive on purpose: buckets accumulate in `characters -> nodes ->
        effects` order and float addition is not associative, so the same nodes in
        another order can differ in the last bits. Every routed field is included
        rather than the raw effect id, because scoring reads the routing: a change
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
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()

    @property
    def leader_scoped_holomem(self) -> frozenset[str]:
        """The holomem whose board holds at least one `scope == "leader"` effect:
        the only leaders under whom the board's terms can differ from no leader.

        An exact invariance, not a tolerance: the leader is read in exactly one
        branch (`effect.scope == "leader"`, `_recipients` / `_reaches_one`) and only
        through `leader.member`, so for any other leader every effect takes the same
        branch in the same order and the floats are bit-identical.
        `score_card_set` uses this to hoist the board term out of its per-leader loop.

        Over-inclusion is safe (it only costs a walk); under-inclusion is a wrong
        score. `scaled()` preserves `scope`, and `parse_effect_id` sets
        `scope="account"` for the `ignored` and `song_score_bonus` kinds.
        """
        return self._leader_scoped

    def _terms(self, card: Card, leader_holomem: str | None) -> tuple[dict, dict]:
        """This card's `(flat, rate)` stat buckets, memoised per
        `(leader_holomem, card)`.

        `leader_holomem` is `leader.member`, or `None` for no leader: the only thing
        about the leader the walk reads, so two leaders sharing a holomem share these
        buckets exactly.

        The walk mirrors the reference walk's (`tests/support/board_reference.py`)
        branches in its order: a bucket is built by repeated addition, so it equals
        the reference only if the additions happen in the same sequence. They do:
        the iteration is the same `characters -> nodes -> effects`, and every test
        is a predicate on one card. `tests/test_board_landing.py` checks the two
        across the leader/member/grouping/account cross product.

        Keyed by `(leader_holomem, id, member, generations)`, not by id alone:
        `member` and `generations` are the card attributes the walk reads, and two
        cards can carry the same id."""
        key = (leader_holomem, card.id, card.member, frozenset(card.generations))
        cached = self._terms_cache.get(key)
        if cached is not None:
            return cached

        # The walk itself lives in `BoardDigest.stat_ops`, one board at a time and
        # memoised there per card, so a bench board's answer survives from one
        # team's model to the next; replayed here in `characters` order.
        #
        # The one regrouping: when every flat value is a whole number (`_flat_exact`,
        # true on real accounts, where a card collects ~580 flat addends) each board's
        # flat addends are summed per stat first. Every partial sum is then an integer
        # far below 2**53, so every addition is exact and the total has the same bits
        # in either grouping. Otherwise, and always for the rate bucket (0.01, 0.03...
        # are not exact in binary), the addends are replayed one by one.
        card_key = key[1:]
        if self._base is not None:
            flat, rate = self._terms_over_base(card, card_key, leader_holomem)
            result = (_close_grouping(flat), rate)
            self._terms_cache[key] = result
            return result
        flat_index = 2 if self._flat_exact else 0
        flat: dict[str, float] = {}  # type: ignore[no-redef]
        rate: dict[str, float] = {}  # type: ignore[no-redef]
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

    def _terms_over_base(self, card: Card, card_key: tuple, leader_holomem: str | None) -> tuple[dict, dict]:
        """`_terms`' walk, taken as the base's totals corrected by the boards this
        model does not share with it (`BoardBase` explains why that is the same
        number). The leader's own board is corrected too even when it is the base's:
        the base was summed with no leader."""
        base = self._base
        digests = self._digests
        base_digests = base.digests  # type: ignore[union-attr]
        total, count, base_rates = base.terms(card, card_key)  # type: ignore[union-attr]
        special = self._base_special
        corrected = list(special.items())
        if leader_holomem is not None and leader_holomem not in special and leader_holomem in base.index:  # type: ignore[union-attr]
            corrected.append((leader_holomem, base.index[leader_holomem]))  # type: ignore[union-attr]
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
        rates.extend((pos, ops) for pos, holomem, ops in base_rates if holomem not in replaced)
        rates.sort(key=_FIRST)  # type: ignore[arg-type]  # board order; positions are unique
        rate: dict[str, float] = {}
        for _pos, ops in rates:
            for stat, value in ops:
                rate[stat] = rate.get(stat, 0.0) + value
        return (flat, rate)

    def power_bonus_for(self, members: tuple[Card, ...], leader_holomem: str | None) -> float:
        """The board's Overall Power for these members: per card and stat, flat plus
        `ceil(rate * stat)`.

        Bit-identical to the reference walk's `BoardContribution.power_bonus`: `total`
        is one running float, so flat and rate terms are added in the same
        interleaved (members x STAT_NAMES) order; per-card subtotals would not be the
        same number. The buckets are memoised per (card, leader holomem) (`_terms`)."""
        total = 0.0
        for card in members:
            flat, rate = self._terms(card, leader_holomem)
            for stat in STAT_NAMES:
                total += flat.get(stat, 0.0)
                # ceiled per (card, stat) over the summed rate
                total += math.ceil(rate.get(stat, 0.0) * card.stat(stat))
        return total

    def score_bonus_rate(self) -> float:
        """The pooled `all`-scope song score bonus, capped: a model constant (it
        depends on neither the leader nor the members), derived in `__post_init__`.

        Not the board's whole score contribution: the team-dependent half is
        `skill_effect_up_for`, and per-song bonuses are `song_score_bonus_for`."""
        return self._score_bonus_rate

    def uptime_for(self, holomem: str) -> tuple[float, float]:
        """`(activation_rate_up, cooldown_shorten)` for one character, as
        fractions. `(0.0, 0.0)` for a character with no board or no B branch.

        They belong to the ACTIVE uptime, not to a score pool: `cooldown_effective =
        cooldown / (1 + reduction)`, and the rate raises the activation probability.

        One number per character, not per (card, role): in the master `SkillTreeNode`
        every node of either family is branch B, scope `member`, non-conditional, so
        the value depends on neither the team nor the leader. `__post_init__` refuses
        a model that breaks this, so a future node at another scope fails loudly."""
        return self._uptime.get(holomem, (0.0, 0.0))

    def with_cooldowns(self, cooldowns: Mapping[str, float]) -> BoardModel:
        """This model with some holomems' activation-frequency term (`uptime_for`'s
        `cooldown_shorten`, a fraction) replaced: `{holomem: cooldown_shorten}`. Everything
        else, derived terms and caches included, is shared, so the copy scores what this
        board would score with those cooldowns. Built for screening activation-frequency
        choices without re-assembling a board (`board_refine.refine_cooldown_counts`)."""
        new = copy.copy(self)
        uptime = dict(self._uptime)
        for holomem, shorten in cooldowns.items():
            rate, _old = uptime.get(holomem, (0.0, 0.0))
            uptime[holomem] = (rate, float(shorten))
        object.__setattr__(new, "_uptime", uptime)
        object.__setattr__(new, "_cooldown_source", self.power_identity)
        return new

    @property
    def power_identity(self) -> BoardModel:
        """The model whose Overall Power this one shares: the one a `with_cooldowns` copy
        was made from, else itself. The cooldowns never enter the Overall Power."""
        return self._cooldown_source if self._cooldown_source is not None else self

    def skill_effect_up_for(self, members: tuple[Card, ...], leader_holomem: str | None) -> float:
        """The board's `SkillEffectUpPermilUp` for this team, memoised per card.

        It sits inside `CalcScoreUpRatio`'s `(1 + SkillEffectUpPermilUp/1000 + B)`
        factor, multiplying the ACTIVE pool, and is one team-wide addend: an effect
        that reaches anybody counts exactly once, not once per recipient.

        The memo is per `(leader_holomem, card)` so it survives the solver swapping
        one member. Each entry is the set of term indices that card receives; the
        team's value sums the union in index order, bit-identical to the reference
        walk."""
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
                    for index, (holomem, effect) in enumerate(self._skill_effect_up_terms)
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
        """Map the game's chr-ids (`charts.json` singers) to the holomem names this
        board is keyed by. A name or an unknown id passes through (and simply misses)."""
        return [self.name_by_chr.get(singer, singer) for singer in singers]

    def conditional_skill_effect_up_for(
        self,
        members: tuple[Card, ...],
        leader_holomem: str | None,
        singers: Iterable[str],
    ) -> float:
        """The singer-conditional half of `skill_effect_up`, for ONE song.

        Same term, same slot and same count-once rule as `skill_effect_up_for`, with
        one extra condition: the node's own holomem must be among the song's credited
        singers (Aki's `R-002`, "When Aki Rosenthal is included as a singer, Grants
        Score Support Effect of 10.0 % to All", adds nothing on a songless deck screen
        and pays on a song Aki sings). The effect's ordinary `scope` must still reach
        the team: Aki's node is leader-scoped, so under another leader it pays nothing
        even on her song.

        Every known node of this kind names its own board's holomem, so "the node's
        owner sings" and "the character named in the effect id sings" coincide; this
        implements the first, the only one the parsed effect id can express.

        `0.0` for an empty cast, so every song-agnostic caller is unchanged."""
        if not self._conditional_skill_effect_up_terms:
            return 0.0
        cast = set(self._holomem_names(singers))
        if not cast:
            return 0.0
        total = 0.0
        for holomem, effect in self._conditional_skill_effect_up_terms:
            if holomem not in cast:
                continue
            # counted once for the team, as the unconditional half is
            if any(_reaches_one(effect, holomem, leader_holomem, card) for card in members):
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

        Same power arithmetic and reach rule as `power_bonus_for`, and the same extra
        condition as `conditional_skill_effect_up_for` (the node's own holomem sings).
        Unlike the skill term this is per card, not one team-wide addend.

        It is the increment over `power_bonus_for`: the game sums the conditional rate
        with the card's unconditional board rate on the same stat and ceils once, so
        a card's conditional power is `ceil((unconditional + conditional) * stat) -
        ceil(unconditional * stat)` (ceiling it alone over-counts by up to 1 per card
        and stat). `power_bonus_for + conditional_stat_power_for` is the song's
        Holomem Board power line.

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
                        rate_by_card_stat[key] = rate_by_card_stat.get(key, 0.0) + effect.value
        total = flat_total
        card_by_id = {card.id: card for card in members}
        for (card_id, stat), rate in rate_by_card_stat.items():
            card = card_by_id[card_id]
            base_rate = self._terms(card, leader_holomem)[1].get(stat, 0.0)
            value = card.stat(stat)
            total += math.ceil((base_rate + rate) * value) - math.ceil(base_rate * value)
        return total

    def song_score_bonus_for(self, singers: Iterable[str], *, scope: str) -> float:
        """`ScoreUpBonusRatioBySkillTree` for one song.

        As the client's `SkillTreeEffectTargetMusicDialogPresenter` computes it: the
        contributing set is the song's singers, and each singer's released effects
        are filtered by the song's singer type (`scope`: whole, unit or solo).
        Capped at `SONG_ALL_CAP_PERMIL` per song and cast, never account-wide."""
        total = sum(
            self._song_score_bonus_by_holomem.get(holomem, {}).get(scope, 0.0)
            for holomem in set(self._holomem_names(singers))
        )
        return min(total, SONG_ALL_CAP_PERMIL / 1000.0)

    def song_score_bonus_by_singer(self, singers: Iterable[str], *, scope: str) -> dict[str, float]:
        """`{holomem: rate}` -- each singer's own share of `song_score_bonus_for`
        BEFORE the cap (Connect multipliers included), so a page can say whose
        yellows the song's bonus comes from and how much of it the cap wastes."""
        return {
            holomem: self._song_score_bonus_by_holomem.get(holomem, {}).get(scope, 0.0)
            for holomem in sorted(set(self._holomem_names(singers)))
        }


# `(effect_id, branch) -> BoardEffect`, filled by `parse_effect_id` on success.
# `BoardEffect` is frozen, so one instance is shared by every board that unlocks
# the node; uncached, the parse is 15 % of a develop, because every
# `board_from_mapping` re-parses the whole bench. Failures are not cached.
_PARSE_CACHE: dict[tuple[str, str], BoardEffect] = {}


def parse_effect_id(effect_id: str, *, branch: str) -> BoardEffect:
    """One `skill_tree_effect-...` id into a routed `BoardEffect`.

    Raises `BoardError` on anything unrecognised rather than returning a
    zero-valued effect. Memoised per `(effect_id, branch)` in `_PARSE_CACHE`."""
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
        return BoardEffect(family=family, kind="ignored", stat="", value=0.0, scope="account")

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

    `_recipients`, `_reaches_one` and `BoardDigest.stat_ops` all use this, so the
    per-card and five-member walks cannot drift apart. `"leader"` is handled by
    the callers, where the leader is."""
    if effect.scope == "member":
        return card.member == holomem
    if effect.scope == "grouping":
        # The effect names a group TOKEN (`grp-myth`); a card carries either that
        # token (board tests) or the catalogue's DISPLAY name (`Myth`) — accept both.
        return (
            effect.grouping in card.generations
            or _GENERATION_BY_GROUPING.get(effect.grouping) in card.generations  # type: ignore[operator, arg-type]
        )
    if effect.scope == "account":
        return True
    raise BoardError(f"unknown board scope {effect.scope!r}")


def _recipients(
    effect: BoardEffect, holomem: str, leader: Card | None, members: tuple[Card, ...]
) -> tuple[Card, ...]:
    """Which of the five member cards this effect actually reaches.

    The leader is never a recipient of stats: it contributes its outfit skill and
    nothing else. The R branch reaching exactly the five members reproduces the
    game's figures; reaching six overshoots them by 20 %."""
    if effect.scope == "leader":
        if leader is None or leader.member != holomem:
            return ()
        return members
    if effect.scope == "account":
        return members
    return tuple(card for card in members if _reaches(effect, holomem, card))


def _reaches_one(effect: BoardEffect, holomem: str, leader_holomem: str | None, card: Card) -> bool:
    """`card in _recipients(effect, holomem, leader, members)`, for one card.

    Every branch mirrors one in `_recipients`. `leader.member` is the only thing
    read off the leader, so taking the holomem is the exact dependency, which
    makes `skill_effect_up_for`'s memo sound."""
    if effect.scope == "leader":
        return leader_holomem is not None and leader_holomem == holomem
    if effect.scope == "account":
        return True
    return _reaches(effect, holomem, card)


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

    No board data is an expected state (`data/` ships none: a board is account
    state), and an absent board makes `scoring._board_bonus` the identity.

    Keyed by holomem, not card id. `effects` holds the game's own effect ids
    verbatim, so the file stays checkable against `SkillTreeNode` by eye;
    `connect_multiplier` is `1 + permil/1000` for the Connect shape covering the
    node (an input, module docstring)."""
    file_path = Path(path)
    if not file_path.exists():
        return None
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BoardError(f"{file_path}: unreadable board file: {exc}") from exc
    return board_from_mapping(raw, source=str(file_path))


def board_from_mapping(raw: object, *, source: str = "<mapping>") -> BoardModel:
    """The parsing half of `load_board`, for a caller holding an already-decoded
    mapping."""
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
                raise BoardError(f"{source}: characters[{holomem!r}] has a row with no 'node' id")
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
                    effects=tuple(parse_effect_id(effect_id, branch=node_id[:1]) for effect_id in effect_ids),
                    connect_multiplier=float(multiplier),
                )
            )
        characters[holomem] = CharacterBoard(holomem=holomem, nodes=tuple(nodes))
    name_by_chr = raw.get("name_by_chr") or {}
    if not isinstance(name_by_chr, dict):
        raise BoardError(f"{source}: 'name_by_chr' must be an object of chr-id -> name")
    return BoardModel(characters=characters, name_by_chr=dict(name_by_chr))
