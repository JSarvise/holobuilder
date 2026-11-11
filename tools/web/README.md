# `tools/web` — the local web server and its pages

The server behind the pages of the rail: Optimizer, Event, Collection, Holomem Board,
Compare, Gacha, Park and Inventory. Every score a page shows comes from this server; the
pages only lay it out.

## Run it

`start.cmd` in the repo root (after `setup.cmd` once) starts it and opens the Optimizer. By
hand, from the repo root:

```
.venv\Scripts\python.exe tools\web\server.py [--port 8731] [--art-dir <dir>]
```

`/` opens `/optimizer`. The server binds `127.0.0.1` only: it serves account data and game
assets, so loopback is a constant, not a flag. `--art-dir` (default
`tools/explorer/cards_explorer_art`) is where the `<card-id>.webp` portraits are; a missing
directory or card degrades to the type-tinted placeholder.

## How a search runs

`POST /api/search` (Optimizer) and `POST /api/event/run` (Event) start a job and return its
id; the page reads progress and the result from `GET /events/<id>` (Server-Sent Events) and
can stop it with `POST /cancel/<id>`. The search itself is `ondemand.py` (the account basis,
the joint leader + members search of `joint_search.py` / `two_stage.py`, the activation
refinement) and, for the Event page, `event_mode.py`. With "infinite cubes and hexas" off, the
best teams' boards are re-planned exactly within the account's materials by
`materials_plan.py` (one HiGHS MILP per team, `tools/refresh/board_plan_solver.py`); every
row says whether its plan solved to optimality (`plan_optimal`).

Opening a row asks `POST /api/row-detail` (figures, timeline, distribution on the row's own
board), `POST /api/board-plan-account` (what to buy and reset) and, on Compare,
`POST /api/compare` / `POST /api/timeline`.

| module | what it is |
|---|---|
| `server.py` | the entry point (`--port`, `--art-dir`): starts `hbweb/router.py` |
| `hbweb/router.py` | the request handler and the route tables (exact paths, then prefixes) |
| `hbweb/config.py`, `hbweb/basis.py` | every file the routes read; the request parsers and the three card bases |
| `hbweb/jobs.py` | the job registry, the search and event job runners, the event stream |
| `hbweb/search.py`, `hbweb/songs.py` | the Optimizer's routes: search, row detail, timeline, Compare, meta; the song list |
| `hbweb/board.py`, `hbweb/boardplan.py` | the board editor; planned boards drawn as the board export |
| `hbweb/event.py`, `hbweb/gacha.py`, `hbweb/park.py` | the Event, Gacha and Park pages |
| `hbweb/collection.py`, `hbweb/inventory.py` | the collection editor (the one write); the Inventory page |
| `hbweb/admin.py`, `hbweb/scripts.py` | the account refresh and the Users console; the account-side scripts run as child processes |
| `hbweb/static.py` | the pages, their modules and every image, figure and font |
| `ondemand.py`, `joint_search.py`, `two_stage.py` | the Optimizer's search |
| `event_mode.py`, `cutoffs_poller.py`, `cutoffs_view.py`, `top_view.py` | the Event page: search, borders, top 100 |
| `materials_plan.py` | exact board plans within the account's materials |
| `row_detail.py`, `compare_view.py`, `board_values.py` | a row's detail, Compare, board node values |
| `gacha_view.py` | the Gacha page |
| `certificate.py`, `job_progress.py` | input hashing, job progress events |
| `kernel/` | the four C kernels (note walk, cooldown variants, Connect and board assembly, the surrogate screen), built with gcc on first use |
| `static/` | the pages (`<name>.html`, served at `/<name>`) and their modules |

## Guards

- **The write prohibition** (`test_no_writes.py`): AST checks over every `.py` here. Anything
  that evaluates to `data/cards.json` or `data/owned.json` may only reach a whitelisted read
  sink, no mutator method may touch them, and every `open()` names a read mode. The one path
  this package writes is `hbweb.config.COLLECTION_PATH` (`state/collection.json`), from
  `hbweb/collection.py` only.
- **Every module is served** (`test_routes.py`): the static route serves by extension, one
  level deep, so a module that falls off it would be a blank page. The test reads each page's
  scripts and walks their imports, demanding a 200 for each.

```
python -m pytest tools/web/test_routes.py tools/web/test_no_writes.py
```
