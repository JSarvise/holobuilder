# Optimizer options

Every option of the Optimizer's **Options** panel, in the panel's order. Generated from
the page itself (`tools/oss/options_glossary.py`): the wording is the app's own.

## Event *(Event goal only)*

- **Rank by**: Choices: Event Points (default); Event Song Score.
- **Live Boost**
- **HoloPass** *(Event Points only)*: Choices: On (default); Off.

> The leader adds no Event Bonus: it counts the five members only.

## Cards

- **Which cards**: Choices: My cards (default) — Your cards at their real level, bloom and SP training; My cards, upgraded — Your cards at a level, bloom or SP training you choose; Maxed account — Your account maxed: every card in the game at max level and bloom, every holomem at max level, complete Holomem Boards, no material limit (Score and Event).
- **Level**: Choices: Real; 20; 30; 40; 50; 60; 70; Max.
- **Bloom**: Choices: Real; 0; 1; 2; 3; 4; 5.
- **SP training**: The limit break: each card's level cap by rarity. Choices: Real; 0; 1; 2; 3; 4.
- **Do not lower cards** (switch): A card already above the chosen level, bloom or SP training keeps its own.
- **Card pool** *(Score goal only)*: Choices: All (default); 5★ only.
- **Platform**: Choices: Mobile (default); PC.

## Holomem Boards

- **Optimize Holomem Boards** (switch): On: every team's boards are planned for it, within the options below. Off: every team plays on your boards as they are now (bought nodes and socketed cards); nothing is planned.
- **Max Holomem Board Pt** (switch): Every holomem at level 50, 361 Pt each.
- **Every holomem invited** (switch): A holomem you have not invited plans at max level.
- **Unlimited Cubes and Hexas** (switch): Ignore cubes and hexas (the other holomems' spare points go to green too). Off: the best teams are re-planned within your cubes and hexas.
- **Don't refund resets** (switch): A reset returns no cubes or hexas.
- **Resets allowed (1,000 Hologold each)**: Choices: No limit; 0; 5; 10; 20; 50.
- **Minimum gain per reset**: Removing a node you own (a reset) costs 1,000 Hologold, and the node is lost for every other song and team. With this set, a node is reset only when what it frees raises this team's score by at least this much. Example: 0.05 % on a 2.4M team means about 1,200 points per node reset. Nothing = reset whenever it gains even 1 point. Choices: Nothing (resets are free); 0.01 % of the score; 0.05 % of the score; 0.2 % of the score.
- **Branches the plan may change**: Choices: Green (default); Blue (default); Red (default); Yellow (default).
- **Secondary targets**: Choices: Choose on the board.

## Song: in general *(Score goal only)*

- **Charts averaged**: Choices: Every chart at the chosen difficulty (default); A level range, any difficulty.
- **From level**
- **To level**

## Leader

- **Holomem**: Choices: Any holomem.
- **Outfit**: Choices: The best outfit she has.
- **No leader overlap** (switch): No member card of the leader’s own holomem.

## Pinned cards

- **Must include**
- **Ban**

## Search

- **Sort by** *(Score goal only)*: Choices: Expected (default); Max.
- **Teams to show** *(Score goal only)*: Choices: 3; 5 (default); 10; 20.
- **Teams to show** *(Event goal only)*: Choices: 5 (default); 10; 20.
- **Score for AUTO** (switch): Rank teams for auto play: notes score like a GREAT and combo-gated skills stay at their base value.
- **Exhaustive search** (switch) *(Score goal only)*: Slower; checks every combination in the first stage.
