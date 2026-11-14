# Architecture

holobuilder answers one question: *which leader and five members score the most, with the cards
this account really has?* Everything else feeds that answer or shows it.

```
 the game's install + the game's servers
        │  tools/refresh/refresh_all.py   (one command, about 30 steps)
        ▼
 tools/recon/proxy/dumps/   data/cards.json   data/owned.json   state/collection.json
        │  read by
        ▼
 src/hbuilder        the model: cards, skill gates, scoring, Holomem Boards, searches
        │  called by                         ▲ same results, faster
        ▼                                    │
 tools/web           searches + server  ── tools/web/kernel/*.c
        │  http://127.0.0.1:8731
        ▼
 tools/web/static    the pages (Optimizer, Event, Collection, Holomem Board, Compare, Gacha, Park, Inventory)
        ▲
        │  started by either
 tools/local   setup / refresh / start, from a clone on a PC with the game (Windows or Linux)
 tools/app     the Windows desktop app: installer, launcher, signed updates, its server
```

One rule runs through all of it: **a speed-up never changes a result.** The C kernels are
compiled with flags that keep floating point bit-identical to the Python they replace, tests
compare the two, and a golden harness (`tests/golden/`) replays whole searches and compares
every number.

## The data: `tools/refresh`

`refresh_all.py` runs every step in order and prints what changed; the step table (what each
step runs, and which need the network or the account) is `refresh_steps.py`. Each step is one
script and can be run alone. Everything lands in `tools/recon/proxy/dumps/` (the data folder,
several GB, never in git), except the card catalogue (`data/cards.json`, to which new cards are
only ever **appended**) and the owned list (`data/owned.json`, also append-only).

| Steps | What they make |
|---|---|
| `master_index`, `level_tables`, `node_master` | the game's master tables, decoded to JSON |
| `account`, `inventory`, `board` | your account: cards with level, bloom and SP training, board nodes bought, items, the Park's day and every Free Chat read so far |
| `cards`, `import`, `add_cards` | the card catalogue |
| `charts`, `songs`, `chart_lib` | every song's notes and timing (`dumps/charts.json`) |
| `card_art`, `jackets`, `live2d`, `chibi`, `*_icons`, `park_export` | the pictures the pages draw |
| `event_*` | open events, bonuses, borders and history |

Reading the game's API needs the client's payload seed, which is not in this repository: the
refresh finds it in the PC's own game install ([docs/dev/game-keys.md](docs/dev/game-keys.md)).

## The model: `src/hbuilder`

Standard library only, no network. Every path the project uses comes from `paths.py`; the inputs
every scorer needs are read by `inputs.py`.

| Module (`domain/`) | What it does |
|---|---|
| `card.py`, `team.py`, `schema.py`, `loader.py` | cards and teams as values; the `cards.json` contract |
| `collection.py` | a card's stats from its level, bloom and SP training (the game's 32-bit formulas) |
| `gates.py`, `targets.py` | when a skill applies, and to whom |
| `scoring.py`, `score_breakdown.py` | a team's song-agnostic value and the deck screen's Unit Score |
| `song_chart.py`, `song.py` | a chart, and a team's score on it note by note (`score_song_detailed`) |
| `song_distribution.py` | the exact distribution of a run's score |
| `board.py`, `skilltree.py` | Holomem Boards: what nodes give, and the board as an editable selection |
| `board_node_values.py`, `board_plan.py`, `board_refine.py` | boards planned for a team: node values, the plan and its assembly, the refinements |
| `deck_panel.py`, `deck_panel_team.py` | the deck screen's Unit Score Details panel |
| `marathon.py` | event bonuses and Event Points |

The score model, formula by formula with the function behind each: [docs/score-model.md](docs/score-model.md).

`solver/` holds the search pieces that are not web-specific: `members_first.py` (members first,
leader second) and `parallel_refine.py` (the exact per-song score across worker processes).

## The searches and the server: `tools/web`

- `ondemand.py` runs a search when the Optimizer asks: it builds the inputs (`leaf_inputs.py`),
  searches leader and members together (`joint_search.py`, or `two_stage.py`: a fitted screen
  proposes member sets and the exact scorer decides), plans boards within your materials when
  asked (`materials_plan.py`) and scores the final rows exactly.
- `event_mode.py`: the same for Event Points; `row_detail.py`: everything a row's details show;
  `compare_view.py`: Compare. (Some routes keep older page names: Compare is `/versus`,
  Collection `/cards`, Gacha `/pulls`, Holomem Board `/holoboard`, Park `/parkview`.)
- `kernel/`: the C kernels (`notewalk.c` the note walk, `cdvar.c` cooldown variants, `connect.c`
  Connect sockets and board assembly, `jointmatch.c` the team and bench Connect matching re-solved
  per team, `surrogate.c` the screen), compiled on first use by
  `cbuild.py` as `.dll` on Windows and `.so` on Linux.
- `hbweb/`: the HTTP server (standard library). `router.py` maps each request to a feature module
  (`search`, `event`, `board`, `boardplan`, `collection`, `songs`, `gacha`, `park`, `inventory`,
  `admin`, `static`); searches run as jobs (`jobs.py`) that stream progress to the page.
  `server.py` is the entry point.
- The board planner proper is an integer program solved with HiGHS:
  `tools/refresh/board_plan_solver.py`.

The server writes only the collection file; downloads and caches go through
`tools/app/content_store.py` (a test enforces this).

## Running from a clone: `tools/local`

`run_local.py` is what `setup`, `refresh` and `start` (`.cmd` on Windows, `.sh` on Linux) call.
It does what the desktop app does before reading an account, with the PC's own game install in
place of the server: it finds the account file and the region (on Linux inside Proton's prefix),
writes the client profile, finds the game's build constants in the install
(`tools/refresh/game_keys.py --find`), and runs `refresh_all.py` and `server.py` with those
settings. Guide: [docs/run-locally.md](docs/run-locally.md).

## The desktop app: `tools/app` (Windows)

- `installer.py` → `holobuilder-setup.exe`: installs a private Python and the app.
- `launcher.py`: checks for a signed update, syncs the shared data, reads the account, starts the
  server and opens the window.
- `content_store.py`: downloads the shared data by content hash, against a manifest signed with
  the release key (`signing.py`, Ed25519).
- `server/hb_auth.py`: the server side (sign-in, per-PC access keys, admin).

Serving your own installer: [docs/self-hosting.md](docs/self-hosting.md).

## Tests and checks

`python -m pytest` runs everything (`tests/` plus the `test_*.py` files beside the code); tests
that need game data are skipped until a refresh has built it, and none may write into the real
`state/` or `dumps/` (`tests/support/write_guard.py`). `ruff check`, `ruff format --check` and
`mypy` (configured in `pyproject.toml`; `pip install -r requirements-dev.txt`) run in CI with the
tests on Windows and Linux.
