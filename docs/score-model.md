# How holobuilder scores a team

This is the score model the optimizer ranks teams with, read from the code as it is. Each
section gives the idea in one sentence, then the formula, the file and function that compute
it, and a small worked example with round, made-up numbers.

Paths are under `src/hbuilder/domain/` unless another folder is named.

## Contents

1. [The whole thing in five lines](#1-the-whole-thing-in-five-lines)
2. [Card stats](#2-card-stats)
3. [Overall Power](#3-overall-power)
4. [The score of one note](#4-the-score-of-one-note)
5. [Skills](#5-skills)
6. [The skill multiplier at one instant](#6-the-skill-multiplier-at-one-instant)
7. [Slot order and the Expected score](#7-slot-order-and-the-expected-score)
8. [The score distribution](#8-the-score-distribution)
9. [Best of N runs](#9-best-of-n-runs)
10. [What the model leaves out](#10-what-the-model-leaves-out)

## 1. The whole thing in five lines

1. Each card's three stats come from its level, bloom and SP training; the team's **Overall
   Power** adds the five members' stats, the leader's outfit, passives, the Holomem Boards,
   the posters (Memory) and Member Power-Up.
2. Every note is worth a slice of Overall Power, times the note's judgement weight, the
   combo tier, the song bonus and **one plus the skill multiplier at that instant**,
   rounded up.
3. At an instant only the **strongest live ACTIVE** counts; SPECIALs and support skills
   multiply it, they do not add a score of their own.
4. The only luck in the model is whether each ACTIVE fires at each of its checks. The
   **Expected** score averages that luck; **Max** is the run where every check fires.
5. The **distribution** is the exact probability of every run score under that luck, every
   note played PERFECT (or AUTO).

## 2. Card stats

**Idea.** A card's Performance, Technique and Sense are its rarity's level curve times a
per-card ratio, then the bloom bonus, each step rounded up in 32-bit floats as the client does.

```
stat = ceil_f32( ceil_f32( ratio × L(level) / 1000 ) × (1 + bloom_permil / 1000) )
```

- `collection.py`, `effective_stats`. `ratio` and `L(level)` come from the master's
  `CardLevel` table; `bloom_permil` from `CardPotential` (for a 5★ card +10 % from bloom 2).
- **SP training** (limit break) only sets the level cap (`collection.py`, `level_cap_for`).
- **Bloom also raises skill levels** (`collection.py`, `SKILL_BLOOM_STEP`,
  `restate_card_skills`): the ACTIVE reaches level 2 at bloom 1, the SPECIAL at bloom 3, the
  PASSIVE at bloom 4. Below that the card uses its level-1 row of that skill. Bloom 5 also
  raises a 4★/5★ card's Connect to level 2.

**Example.** ratio 440, `L(80) = 23,512`, bloom 2 (+100 ‰):
`440 × 23512 / 1000 = 10,345.28 → 10,346`, then `× 1.1 = 11,380.6 → 11,381`.

## 3. Overall Power

**Idea.** The deck screen's Overall Power is a sum of six parts, each rounded up line by line;
the leader adds only its outfit, never its own stats.

```
OP = MemberParameter + Outfit + Passive + HolomemBoard + Memory + MemberPowerUp
```

- `score_breakdown.py`, `deck_screen_unit_score`; on a song, `song.py`, `overall_power_for`.
- **MemberParameter**: the 15 stats of the five members.
- **Outfit, Passive**: per (skill, card, stat) `ceil(rate × stat)`. A buff that reaches only k
  members goes to the top k **by the stat it raises** (`scoring_models.py`,
  `TopNByBuffedStat`).
- **HolomemBoard**: per card and stat `flat + ceil(rate × stat)` (`board.py`,
  `BoardModel.power_bonus_for`); the generation "grouping" greens add at most 900 to each stat
  (`board.py`, `GROUPING_ALL_CAP`). On a song, a singer's conditional red nodes add their part
  for that song (`song.py`, `song_overall_power`, `cast_power_up`).
- **Memory** (posters): `ceil(stat × R / 1000)` per member and stat, `R = min(2t, t + 30)`
  for `t` posters (`collection.py`, `memory_bonus_permil`, `memory_bonus_per_stat`).
- **Member Power-Up**: `ceil(base × P / 10000)` per member (`collection.py`, `member_power_up`).

The deck screen's **Unit Score** is a different number (its Score Bonus adds lines that the
song multiplies); the ranking never uses it, only the song score below.

## 4. The score of one note

**Idea.** A note is worth Overall Power spread over the chart's total note weight, scaled by
the difficulty, then by this note's weight, combo tier, the song bonus and the skills live on
it, and rounded up.

```
value(n) = ceil( OP × 2.300 × MC / PS × (1 + Y) × w(n) × (1 + ratio(t_n)) )
score    = Σ over notes of value(n)
```

| Symbol | Meaning | Where |
|---|---|---|
| `2.300` | the game's `live_deck_evaluation_coefficient_permil_multiply` | `song_chart.py`, `SCORE_COEFFICIENT` |
| `MC` | `1 + coef_permil/1000 × (level − 5)`: 0.968 to 1.200 over the real charts | `song_chart.py`, `SongChart.music_coefficient` |
| `PS` | the sum of every note's PERFECT weight / 1000, plus 0.1 for each of the slides' invisible points (hidden relay and curve points: the client builds a note for each and weighs it as a tick, but they are never judged, so they score nothing); it makes the chart's length cancel | `song_chart.py`, `SongChart.notes_perfect_coefficient_sum`, `hidden_points`; `tools/refresh/sus_parse.py`, `hidden_points` |
| `Y` | the song bonus from the singers' yellow board nodes, at most +10 % | `board.py`, `BoardModel.song_score_bonus_for`, `SONG_ALL_CAP_PERMIL` |
| `w(n)` | the note's judgement weight × (1 + its combo tier) | `song.py`, `_chart_weights` |
| `ratio(t)` | the skill multiplier at the note's time (section 6) | `song.py`, `_ratio_at` |

The sum runs in `song.py`, `score_song_detailed` (and its C kernel, `tools/web/kernel/notewalk.c`,
which gives the same numbers).

**Judgement.** Every note is played PERFECT: a tap or a long note's ends weigh 1000 ‰, a
flick 1050 ‰ on mobile and 1000 ‰ on PC, a hold tick 100 ‰ (`song_chart.py`, `ChartNote`,
`perfect_permil_for`). PERFECT+ weighs the same as PERFECT, so all-PERFECT is the top.

**Combo, counted in notes.** Under the all-PERFECT premise the combo never breaks, so the n-th
note carries the tier of combo n: +0 % below 100, +1 % from 100, +2 % from 200, up to +10 %
from 1000 (`song_chart.py`, `LIVE_COMBO_TIERS`, `SongChart.combo_coefficient`). It follows the notes,
not the clock: on a chart with 2 notes a second, combo 40 is the 40th note, wherever it falls.

**AUTO.** Every note scores as a GREAT: 800 ‰ instead of 1000 (hold ticks stay at 100, a flick
loses its mobile +50), and **no note gets the combo multiplier**, because on AUTO the combo
never builds up (`song_chart.py`, `ChartNote.auto_permil`; `song.py`, `_chart_weights`). The divisor `PS` stays
the PERFECT one. AUTO scores are therefore about 20 % lower from the judgement and a further
2.6-5.1 % from the combo.

**Rounding.** Every note is rounded up on its own; the stats and the Overall Power lines are
rounded up before that. No other rounding happens in a run.

**Example.** OP 300,000, `MC = 1.1`, a 1,000-note chart of taps (`PS = 1000`), `Y = +5 %`:

```
constant = 300000 × 2.3 × 1.1 / 1000 × 1.05        = 796.95
note 450, a tap at combo 450 (+4 %): w = 1.04
no skill live:            ceil(796.95 × 1.04 × 1)      = ceil(828.83)  = 829
an ACTIVE live, ratio 0.81: ceil(796.95 × 1.04 × 1.81) = ceil(1500.18) = 1,501
```

## 5. Skills

### 5.1 ACTIVE: checks, windows and the real activation chance

**Idea.** An ACTIVE gets a check every cooldown; at each check it fires with probability `p`
and then stays live for its duration.

```
interval   I = cooldown / (1 + board cooldown reduction)
checks     at k × I for k = 1, 2, …  while k × I ≤ the song's length (none at t = 0)
window     [k × I, k × I + duration)
chance     p = min(1, tier × (1 + team rate-up + board rate-up))
tiers      Low 0.37, Medium 0.46, High 0.55
```

- Windows: `song.py`, `_active_windows`. The clock starts at the chart's measure 0. The board's
  cooldown nodes (Act.CD, Connect included) divide the interval: `board.py`,
  `BoardModel.uptime_for`.
- Chance: `scoring_models.py`, `MultiplicativeRateUp.apply`; tiers in `scoring_config.py`
  (`probability_tiers`); the board's Activation Rate UP nodes (Act.Prob, Connect included)
  come from `BoardModel.uptime_for`; the team's from `scoring.py`.
- Two windows of one skill never overlap: a duration is always shorter than its cooldown.
- An ACTIVE's magnitude can change while a condition holds (life, combo ≥ N, the song's type,
  a generation…); a combo condition is read at each check with the combo reached by then
  (`song.py`, `_combo_split`).

The Skill uptime chart on the Optimizer shows exactly this `p` for each window
(`song.py`, `timeline`, `probability`), not the card's bare tier.

**Example.** A High card (0.55) whose board adds +40 % rate and +10 % cooldown reduction,
cooldown 22 s, duration 8 s: `I = 22 / 1.1 = 20 s`, checks at 20, 40, 60 s…, each window 8 s,
`p = 0.55 × 1.4 = 0.77`.

### 5.2 SPECIAL: fixed times, and the specials that raise the ACTIVE chance

**Idea.** The SPECIAL of slot i fires once, for sure, at the chart's i-th special cue; it
multiplies the ACTIVEs live under it and scores nothing alone.

- Every real chart has five cues (`song_chart.py`, `SPECIAL_CUE_COUNT`); slot i fires at cue i.
  Window `[cue, cue + duration)`, chance 1.
- Its score support goes into the boost `B` of section 6.
- **Activation Rate UP riders.** Some SPECIALs also raise the ACTIVE chance while they are
  live. A check that falls inside one fires with `p' = min(1, tier × (1 + rate + rider))`. The
  model keeps the window's own roll `p` and adds an independent second roll with
  `q = (p' − p) / (1 − p)`, which is exact: `1 − (1 − p)(1 − q) = p'` (`song.py`,
  `_rider_terms`, `_rider_hosts`).
- **Specials that need a combo.** Five SPECIALs raise the rate only at combo ≥ 100. The rider
  is live at a cue only if the combo at that cue has reached it, counted in notes (`song.py`,
  `_rider_gate_at`). So the first cue of a song can come too early (94 notes in) and the later
  ones count. On AUTO it never fires (no combo).

**Example.** A High card at `p = 0.66` (+20 % from the board) under a +55 % rider:
`p' = 0.55 × 1.75 = 0.9625`, `q = (0.9625 − 0.66) / 0.34 = 0.89`. Its check inside the special
fires 96 % of the time instead of 66 %.

### 5.3 PASSIVE and the leader's outfit

**Idea.** A stat PASSIVE or outfit adds to Overall Power; a support one adds to the boost `B`
for the whole song.

- `stat_up` → Overall Power (section 3); one that reaches only k members reaches the top k by
  the stat it raises. `score_support` → section 6 ("supports that reach the card"), always
  live; one that reaches only k members reaches the k with the strongest ACTIVE, since it
  raises no stat (`targets.py`, `resolve_target_card_ids` with
  `scoring.active_magnitude_by_card`; `song.py`, `_support_state`).
- The leader contributes only its outfit, never its stats or its member skills.

## 6. The skill multiplier at one instant

**Idea.** At each instant the strongest live ACTIVE, multiplied by everything that supports it,
is the note's skill multiplier; with chances, the model takes its exact expectation.

```
v_i       = m_i × (1 + SEU + B + supports that reach the card)
ratio(t)  = max over the ACTIVEs live at t of v_i
```

- `scoring.py`, `_score_up_pool`; per note, `song.py`, `_ratio_at`.
- `m_i`: the ACTIVE's score up (e.g. 0.60 for +60 %). `SEU`: the board's skill-effect-up.
  `B`: the strongest shared boost among the live ACTIVEs plus the strongest live SPECIAL
  boost (the game's structure; in today's data no ACTIVE carries a boost, so `B` is the SPECIAL
  one).
- ACTIVEs do not add up: only the largest one live at the instant counts. This is the game
  client's own rule: `ActiveSkillEffectDataCollection.CalcScoreUpRatio` keeps the largest
  multiplier among the live ACTIVE effects (`Math.Max`) and the largest among the SPECIAL
  ones, and returns the two maxima added; a SPECIAL brings a boost (`B`) and no multiplier of
  its own, so the ACTIVE maximum is the whole ratio. A trace of that function in game (two
  AUTO runs of one chart, three ACTIVEs on one cooldown) agrees note by note: on the 696
  notes with two or more ACTIVEs live, the total was the largest times the boost on 694 and
  their sum on none (the other two did not split cleanly into notes).
- If the live ACTIVEs added up instead, the Expected would be about 80 % higher (+77 % and
  +84 % on two teams on one chart), far above the every-ACTIVE-fires Max.
- With random windows the Expected uses the exact expectation of that maximum, sorting by
  `v` (`scoring_models.py`, `ExpectedMaximumActiveSkill`, `_expected_max_group`):

```
E[max] = Σ_i v_i × p_i × Π over larger j of (1 − p_j)
```

**Example.** Two ACTIVEs live at a note: `v = 0.81` with `p = 0.78`, and `v = 0.50` with
`p = 0.60`: `E[max] = 0.81 × 0.78 + 0.50 × 0.60 × (1 − 0.78) = 0.632 + 0.066 = 0.698`.

## 7. Slot order and the Expected score

**Idea.** The slot order decides which SPECIAL fires at which cue; the team's score is that of
its best order, and the Expected sums each note's expected value, rounded up.

```
Expected(team) = max over the 120 orders of Σ over notes of ceil( E[unrounded value(n)] )
```

- `song.py`, `score_song_detailed` (an exact 5 × 5 decomposition walks the 120 orders).
- The Holomem Boards are planned for the team and song first (`board_plan.py`; within the
  account's materials, `tools/refresh/board_plan_solver.py`), and the team is scored on them.
- The Expected rounds each note's expectation, so it is not the mean of the distribution
  (which averages rounded runs): the two differ by tens of points on a long chart.
- The Optimizer's Export writes these terms as a CSV, one row per note at the best order
  (`song.py`, `NoteRow`; `tools/web/row_detail.py`, `note_breakdown`): time, combo, the value
  with no skill, the live ACTIVEs and their chances, the boosts, the expected multiplier and
  `ceil(...)`. The last column adds up to the Expected exactly.

**Example.** The note above with the two ACTIVEs: `ceil(828.83 × (1 + 0.698)) = ceil(1407.35)
= 1,408`.

## 8. The score distribution

**Idea.** Each ACTIVE check is a coin with chance `p`; walking the notes in order and keeping
every combination of open windows that fired gives the exact law of the run score.

- `song_distribution.py`, `run_score_distribution`, on the same terms the Expected uses
  (`_terms`, `_term_for`, `_rider_terms`).
- A window opening splits each state in two (fired with `p`, not with `1 − p`); a window
  closing merges the states that differ only by it; each note adds its rounded value to each
  state. SPECIALs and supports are certain.
- **Exact or binned.** While the set of reachable scores stays under 400,000 values the law is
  exact. Beyond it the scores are put on a grid, each value's probability split between its
  two neighbours (so the mean stays within a fraction of a point), and each state keeps its exact lowest and highest
  score, so **Min and Max stay exact** (`exact: false` in the answer).
- **Max** is the run where every check fires, at the team's slot order
  (`song_rank.py`, `score_team_ceiling`); **p_max** its probability.
- The page shows the percentiles (p1 … p99), a histogram from p0.1 to Max, and the mean.

**Example.** A team with 40 checks at `p ≈ 0.77` has 2^40 possible runs, but the walk only
carries the windows open at the same time, so the law is exact in a few seconds. Its shape:
most runs within a few percent of the Expected, a short tail up to Max (every check firing,
probability `Π p`, here about `0.77^40 ≈ 3 × 10^-5`), a longer one down.

## 9. Best of N runs

**Idea.** The best of N independent runs is at most x only if every run is, so its law is the
one-run law raised to the N-th power.

```
P(best of N ≤ x) = F(x) ^ N              F = the one-run cumulative law
E[best of N]     = Σ_x x × ( F(x)^N − F(x⁻)^N )
P(N runs reach s at least once) = 1 − F(s⁻)^N
```

- `song_distribution.py`, `best_of_n` (on the exact law: `run_score_distribution(keep_law=True)`).
- From the command line: `tools/web/best_of_n.py --song <id> --cards <leader> <5 members>
  --runs N --score S` prints the Expected, Max, the one-run law, the expected best of N with
  its percentiles, and where S falls: the share of single runs below it, the share of
  best-of-N sessions below it, and the chance N runs reach it. `--basis maxed` (the default)
  scores every card maxed; `--auto` and `--platform pc` change the premise.

**Example.** A coin between 0 and 10 points: one run expects 5; the best of two is 10 three
times in four, so it expects 7.5. With a real law, the best of 26 sits close to Max: the
chance that 26 runs never reach the 90th percentile is `0.9^26 ≈ 6 %`.

## 10. What the model leaves out

- **Misses.** Every note is PERFECT (or every note AUTO). GREAT, GOOD and MISS, and the combo
  breaks they cause, are not modelled; a real best run also varies with them.
- **Life.** Life stays full, so a skill whose condition needs life never loses it to a
  mistake.
- **Timing.** Checks fall exactly on the cooldown lattice from measure 0; a live run showed
  them a few tenths of a second later, enough to move a window's edge by a note or two.
- **The rolls themselves** are taken as independent coins with the chances above; the game's
  own random number source is not reproduced, only its law.
- **The deck screen's Unit Score** is computed by a different formula (shown on the page,
  never ranked by).
- **Uncertain inputs.** The model is only as good as the account it reads: stats, blooms, board
  nodes and sockets as of the last refresh.
