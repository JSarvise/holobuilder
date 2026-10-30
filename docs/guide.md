# Using holobuilder

The pages are the same whether you use the app or run it yourself.

## Optimizer

Finds the best teams for a goal. Every option of the Options panel, one by one:
[options.md](options.md).

- **Goal**
  - **Score**: the highest live score on one song, or *In general* (every song, on average).
  - **Event**: the most Event Points per live (or the best Event Song Score) for the open event.
    Set your **Live Boosts** and **HoloPass**. *Highlight* is the event's multi-live pick: a random
    whole song of the event holomems. See [Event](#event).
- **Song and difficulty**: pick a song (type to search; filter by talent or group) or *In general*.
- **Cards**
  - *My cards*: your cards as they are (the default).
  - *Upgraded*: your cards at a level, bloom and SP training you choose (*Do not lower* keeps what is
    already higher).
  - *Maxed account*: every 5★ card at max level and bloom, complete boards (a what-if).
- **Holomem Boards**: planned for each team by default (turn it off to use the boards as they are).
  Plan options: maximum Board Points, every holomem invited, unlimited Cubes/Hexas, resets allowed
  (each costs 1,000 Hologold), the minimum gain a reset must bring, which branches may change, and
  nodes to buy if possible or never buy.
- **Leader and pins**: fix the leader's holomem or outfit, forbid the leader's holomem among the
  members, require cards (*Must include*) or exclude them (*Ban*).

**Results**

- **Expected score**: the average over many runs; Active skills fire at random. **Max** is the score
  if every Active fires.
- **Unit Score Details**: the deck screen's Overall Power and Score Bonus, line by line.
- **Score distribution**: the spread of the score from run to run (percentiles).
- **Skill uptime**: when each member's skills are live during the song.
- **Holomem Board plan**: the nodes to buy and reset, per holomem, and the materials it needs
  against what you hold.
- The slot order matters (it decides which member each SPECIAL cue fires); the cards are shown in it.

## Event

The open event: your rank, the borders and their forecast, the top 100, the event's bonus cards and
rules, and past events.

**Highlight** draws a random song of the event holomems after the team is set, so one board and one
slot order play every song. Each result row opens on *The whole pool*: one Holomem Board and one slot
order for every song, its mean score and Event Points, and the score by song drawn. The row's song
selector shows the team on any single song instead. The Highlight Bonus follows the Live Boosts
like a song's length bonus does (+54 % at 1, +50 % at 2–3, +42 % at 4 or more).

The **Multi Live calculator** gives the Event Points of a live you played in a room: type the room's
MULTI SCORE (the total at the top of the multi result screen) and your score (empty = this team's
expected score).

## Other pages

- **Holomem Board**: every holomem's board, the nodes you own, Board Points spent and unspent.
- **Collection**: every card, owned or not, with filters and the card's details.
- **Compare**: put teams side by side, change a card's level or bloom, and compare their scores.
- **Gacha**: your pull history against the published rates (synced only when you press the button).
- **Park** and **Inventory**: today's park and your items and board materials.

## How the scores are computed

holobuilder computes scores with its own model of the game's scoring, built from the game's data
and checked against the figures the game itself shows (the Unit Score panel and real results). When
the #1 team carries a **Checked** note, the search proved that no other team can beat it under the
conditions it states; otherwise it is the best team the search found, not a proof.
