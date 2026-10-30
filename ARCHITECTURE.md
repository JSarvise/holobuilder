# Architecture

holobuilder answers one question: *which leader and five members score the most, with the cards
this account really has?* Everything else feeds that answer or shows it.

```
 the game's servers + the local install
        │  tools/recon/proxy/refresh_all.py   (one command, ~30 steps)  
        ▼
 tools/recon/proxy/dumps/  +  state/collection.json  +  data/owned.json
        │  read by
        ▼
 src/hbuilder  (the model: cards, gates, scoring, boards)  ◄── tools/web/kernel (C, same results)
        │  called by
        ▼
 tools/web/server.py  (local HTTP, 127.0.0.1)  ──►  tools/web/static/*.html  (the pages)
        ▲
        │  started by either
 tools/app    (desktop app: launcher, installer, content store, auth server)
 tools/local  (setup.cmd, refresh.cmd, start.cmd: a clone on a PC with the game)
```

## The data: `tools/recon/proxy`

`refresh_all.py` runs every step in order and prints what changed. Each step is one script and can
be run alone. The main groups:

| Steps | What they make |
|---|---|
| `master_index`, `level_tables`, `node_master` | the game's master tables, decoded (`dumps/holodori/main_*.json` and friends) |
| `account`, `inventory`, `board` | your account: cards with level, bloom and limit break, board nodes bought, items |
| `cards`, `import`, `add_cards` | the card catalogue; new cards are **appended** to `data/cards.json`, never edited |
| `charts`, `songs`, `chart_lib` | every song's notes and timing (`dumps/charts.json`) |
| `card_art`, `jackets`, `live2d`, `chibi`, `*_icons`, `park_export` | pictures the pages draw |
| `event_*` | open events, bonuses, borders and history |

Reading the game's API needs the client's payload seed, which is not in this repository: the
refresh finds it in the PC's own game install ([docs/dev/game-keys.md](docs/dev/game-keys.md)).

## The model: `src/hbuilder`

Standard library only, no network. `domain/` holds the rules: `gates.py` (when a skill applies),
`scoring.py` (a team's value), `song.py` (its score on one chart, note by note), `board*.py` and
`skilltree.py` (Holomem Boards). `solver/` searches teams. Tests live in `tests/`.

The rule that matters most: **a speed-up never changes a result.** The C kernels in
`tools/web/kernel` are compiled with flags that keep floating point bit-identical to the Python
they replace, and tests compare the two.

## The searches: `tools/web`

- `ondemand.py` + `joint_search.py`: the best team for one song, searched when asked; the leader is
  chosen inside the search.
- `two_stage.py`: a learned screen (`kernel/surrogate.c`) proposes member sets, the exact scorer
  decides. Every reported number comes from the exact scorer.
- `tools/recon/proxy/board_plan_solver.py`: the board plan as an integer program (HiGHS).

`server.py` serves the pages and a JSON API under `/api/`. It writes only the collection file;
downloads and caches go through `tools/app/content_store.py` (a test enforces this).

## The desktop app: `tools/app`

- `installer.py` → `holobuilder-setup.exe`: installs a private Python and the app.
- `launcher.py`: checks for a signed update, syncs the shared data, reads the account, starts
  `server.py` and opens the window.
- `content_store.py`: downloads the shared data by content hash from the server, against a manifest
  signed with the release key (`signing.py`, Ed25519).
- `server/hb_auth.py`: the server side (sign-in, per-PC access keys, admin).

## Running from a clone: `tools/local`

`run_local.py` is what `setup.cmd`, `refresh.cmd` and `start.cmd` call. It does what the desktop
app does before reading an account, with the PC's own game install in place of the server: it
finds the account file and the region, writes the client profile, finds the game's build
constants in the install (`tools/recon/proxy/game_keys.py --find`), and runs `refresh_all.py` and
`server.py` with those settings. Guide: [docs/run-locally.md](docs/run-locally.md).

Serving your own installer: [docs/self-hosting.md](docs/self-hosting.md).
