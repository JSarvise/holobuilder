"""The plain reference walk of a board: `resolve` routes every unlocked node of every
captured board to the five members and returns a `BoardContribution`. The product scores
through `BoardModel`'s memoised readers (`power_bonus_for`, `score_bonus_rate`,
`skill_effect_up_for`, `song_score_bonus_for`); the tests check those against this walk,
which keeps the same accumulation order so the two stay bit-identical.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from hbuilder.domain.board import (
    _GROUPING_KEY,
    SONG_ALL_CAP_PERMIL,
    STAT_NAMES,
    BoardError,
    BoardModel,
    _close_grouping,
    _is_grouping_all,
    _recipients,
)
from hbuilder.domain.card import Card


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
