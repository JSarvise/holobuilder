# `tools/web` — the local web view

One page: press **Solve**, watch real progress, get **the whole top-N ranking** —
every team as six card faces with its gate resolution and its per-skill score
attribution, and, for each rank, what it costs against the rank above and which
card it swapped.

The server does not reimplement anything: it calls `load_inputs`,
`resolve_selectors`, `project_arrangements`, `project_duration`,
`should_use_heuristic`, `solve_heuristic_labelled`, `score_with_breakdown` and
`build_payload` — the CLI's own functions, in-process, imported rather than
shelled out to. So a team the page shows is the team the CLI returns, with the
same score to the bit; a second scoring path would be free to drift.

---

## Run it

```
python tools/web/server.py
# -> http://127.0.0.1:8731/
```

From the repo root. `src/` goes on the path by the server itself, so no
`PYTHONPATH` is needed and there is nothing to install — stdlib only.
`main()` changes to the repo root, because `cli.apply_collection` resolves the
level curve, caps and `skill_ladder.json` by paths relative to the working
directory; launched from elsewhere it would silently score maxed skills.

| flag | default | what it does |
|---|---|---|
| `--port` | `8731` | — |
| `--art-dir` | `tools/explorer/cards_explorer_art` | where the `<card-id>.webp` portraits are |

**The bind address is not a flag.** `127.0.0.1` is hardcoded: the page serves
account data and game assets, so it is loopback-only by design.

### Card art

The 512×1024 portraits live in `tools/explorer/cards_explorer_art/`, each with a
`<id>.sq.webp` square beside it. The **portrait** is the one to draw: the game's
own `VerticalCardThumbnail::SetCardTexture` builds `img_card_vert_{0}` and
stretches it full-bleed onto a 5:7 rect — `object-fit:fill`, no crop.
`CardImage` is a `RawImage`, `RawImageTextureSetter::SetToImage` writes `uvRect`
from a constant `(0,0,1,1)`, and `RawImage` has no `m_PreserveAspect` field, so
there is never a crop: a 1:2 source into a 5:7 frame is a ×1.400 horizontal
stretch. The rule is copied from `build_explorer.py` verbatim, with the reasoning.

The art is gitignored and **not copied here** — the server reads it in place. A
fresh checkout has none; point `--art-dir` at a directory that does:

```
python tools/web/server.py --art-dir <path to>/tools/explorer/cards_explorer_art
```

A missing directory, or a card with no portrait, degrades to the type-tinted
gradient — `build_explorer.py`'s own `.face:not(.has)` placeholder, so it looks
deliberate rather than broken.

---

## The ranking

`static/ranking.js` draws all N teams. It is not a bare score column, because
adjacent ranks can be nearly identical: on a 91-card pool, ranks 1 and 2 can
differ **by one member card and by 1.7 points on 459 thousand**. So each row
carries both deltas (against #1 and against the previous rank), a bar scaled to
the ranking's **own** spread, and roster-change chips by name
(`−Houshou Marine +Momosuzu Nene`). Every rank expands to its six faces, its
gates and its full `--explain` attribution. The Δ-vs-previous column is drawn
only where it differs from Δ-vs-#1.

**Every delta is quoted against the noise it lives in.** Four AUTO runs of one
song with an identical team can score 578,424 / 617,974 / 650,951 / 702,485 —
the active-skill RNG is worth about **21 %**, while a whole top-5 spread can be
under 1 %. The page says so under the banner: this ranking orders *the model*;
no single clear can tell such teams apart.

The leader may also appear among the members. That is correct: `MEMBER_COUNT`
is **5** and `Team.leader` is a separate slot that may name a card which is also
a member (`domain/team.py`).

### Search diagnostics

The APPROXIMATE banner stays loud and first — "the true optimum may not be in
this list at all" is true of every beam result. The beam's failure-mode labels
are a folded **diagnostics** panel, not part of the banner: over real misses
none of the seven labels has lift above 1, and three fire more often on correct
answers, so attached to every result they carry no information.

### Latency and cancellation

A solve on a ~90-card pool takes ~10 s — four seeds, the one-card-swap polish
and the failure-label diagnostics on top of the beam — so it runs as a job with
streamed progress rather than behind a spinner. The first progress tick arrives
after load + the first 5,000 arrangements (`PROGRESS_INTERVAL_TEAMS`); the page
shows the projection line immediately and starts its clock at the click.

Cancellation actually cancels: the progress callback raises `_Cancelled`, which
unwinds the beam because `solver/heuristic.py` catches nothing broad on the way
out, so cancel latency is about one progress interval. Closing the tab cancels
too: a dropped SSE connection sets the same flag, so a solve nobody is watching
does not keep a core busy. `ThreadingHTTPServer` is what lets `POST /cancel` be
answered while the SSE stream for the same job is still open.

---

## Guards

### The write prohibition

`tools/web/test_no_writes.py`. Three independent AST checks over every `.py`
in `tools/web/`, in the shape of `tests/test_fixture_isolation.py`'s guard:

1. **Taint tracking with an allowlist.** Anything evaluating to
   `data/cards.json` or `data/owned.json` — literal, `REPO_ROOT / "data" /
   "cards.json"` join, or a name bound to either — may only reach a
   whitelisted read sink. A write function nobody anticipated is rejected by
   default.
2. **No mutator method** (`write_text`, `unlink`, `.open(...)`, …) on a
   protected path.
3. **Blanket: every `open()` in `tools/web/` names a read mode.** Not
   taint-scoped, so it cannot be defeated by laundering the path through an
   untracked local.

Taint deliberately does **not** propagate through arbitrary calls:
`load_inputs(CARDS_PATH, …)` returns card *data*, not a path; propagating would
mark every downstream variable and fire on the solver call itself.

A guard that has silently stopped working passes just like one that works, so
it has a negative control: an offender file dropped into `tools/web/` makes all
three checks fire, each naming the right line. `_parse` reads files as
`utf-8-sig`, because a UTF-8 BOM otherwise raises a `SyntaxError` on line 1 that
looks like a caught write.

The checks are parametrised over the `.py` files in `tools/web/`, so a new
test file is itself scanned. `pyproject.toml`'s `testpaths` includes
`tools/web`, so the root `pytest` run collects them.

### The modules are served

The static route serves by extension (one level deep, no `..`, re-checked for
containment under `static/` after resolving). A module that 404s is **not a
loud error — it is a blank page**, because an ES-module import that fails
aborts the whole module graph. So `test_routes.py` **reads `app.js`'s own import
statements** and demands a 200 for each, rather than restating an allowlist.

```
python -m pytest tools/web/test_routes.py tools/web/test_no_writes.py
```

---

## Layout

| file | what it is |
|---|---|
| `server.py` | the whole server: routes, job registry, the in-process solve |
| `static/index.html` | the page |
| `static/style.css` | chrome + **the ported card face** + the ranking rows |
| `static/app.js` | vanilla ES module; transport, progress, chrome |
| `static/ranking.js` | **the ranking**: rows, deltas, roster diffs, faces, gates, attribution |
| `test_no_writes.py` | the write guard |
| `test_routes.py` | every module `app.js` imports must be served |

The UI is plain HTML/CSS/ES modules with self-hosted fonts under
`static/fonts/`. Other features served here: per-bloom skills in the collection
editor (each card's `skills` and its `skill_ladder` rungs, rendered at the level
the card's bloom buys); song titles, metadata and jackets
(`dumps/holodori/main_LangMusic_Eng.json` and `main_Music.json`; jackets from
`song_jackets.py --fetch --render`); a 5★-only pool filter; the board plan and
the per-song timeline inside EACH ranked team's block; compact multi-select
collection filters.

### Routes

| | |
|---|---|
| `GET /` | the page |
| `GET /<name>.js` `GET /<name>.css` | one level deep, no `..`, resolved under `static/` |
| `GET /art/<card-id>.webp` | a portrait, served in place (404 → the client's gradient; a `..` path → 400) |
| `POST /solve {top,basis,board,song,pool}` → `202 {"job_id": …}` | starts a job; `pool` = "all" \| "5star" (a pool restriction, not a scoring change) |
| `GET /events/<job_id>` | `text/event-stream`: `status`, `projection`, `progress`×N, `result` \| `cancelled` \| `error`, `end` |
| `POST /cancel/<job_id>` | sets the flag the progress callback reads |
| `GET /cardart/[full\|vert\|thumb/]<card-id>?w=N` | card art, server-side downscale |
| `GET /chricon/chr-XXXXX` | a character's round face icon |
| `GET /jacket/<music-id>?w=N` | a song's cover art (`song_jackets.py`) |
| `GET /fonts/<face>.woff2` | the self-hosted UI faces |
| `GET /api/collection` | cards + bounds + `skills`/`skill_ladder` for the editor |
| `POST /api/timeline {song, cards, basis, board_state?, member_order?, distribution?, deck_power?, auto?, uptime_model?, platform?}` | one team's skill-activation windows on a chart, on the ROW's basis: `board_state` is rebuilt exactly as the develop assembled it (`row_detail.board_from_state`), figures through `score_team_on_chart` → `song_est` (= `total_score`), `song_max` (= `max_score`), `deck_power`, `deck_power_basis`, `member_order`, `board_rebuild`, `member_order_requested_matches`, and with `distribution:true` the exact run law (`distribution`, below) |
| `POST /api/compare {teams:[{cards, board_state?, member_order?}]×2, board:'captured'\|'develop'\|'off'\|'state', song, distribution?, …}` | two teams side by side; `board:'state'` scores each team on its row's `board_state`. Each team: `score.{song_est, song_max, song_off, deck_power, deck_power_basis, member_order, member_order_requested_matches}`, `panel` (Overall Power lines), `score_bonus` (the five Score Bonus lines: `lines[{key,label,permil}]` active/outfit/passive/board/special, `score_bonus_permil`, `unit_score`, `member_order`, `unmodelled`), `distribution` on request |
| `POST /api/row-detail {song, basis:'account'\|'maxed'\|'account-max', cards, refine?, member_order?, timeline?, distribution?, as_is?}` | a two-stage (or event) row re-developed on ITS leaf: `song_est`, `song_max`, `song_off`, `member_order`, `deck_power`, `board_state`, `points`, `points_max`, `refined`, `panel`, `score_bonus`, `timeline`?, `distribution`?, and `row_best` / `row_matches` against the cached row. Unless `as_is:false`: `song_est_as_is` (the team on the account's boards AS CAPTURED, `board_state.json`, assembled like the develop's board; `as_is{available, song_est, member_order, board}` or `{available:false, reason}`) and `plan_cost` = the planned boards against that capture and the wallet: `per_character[{chr, name, buy[], reset[], points_needed, points_refunded, points_unspent, points_shortfall}]`, `buys`, `points`, `points_refunded`, `materials{item: qty}`, `refund{}`, `wallet{}`, `shortfall{}` (positive only), `resets`, `hologold` (1,000 per node taken off), `hologold_wallet`, `hologold_shortfall`, `affordable`, `unpriced`, `reset_rule`; an unmeasured wallet / point figure is `null`, never 0 |
| `GET /api/two-stage?song=&basis=&difficulty=&top=` | the cached two-stage answer (`two_stage_view.payload`): `state` fresh/stale/missing, `build{running, stage, done, total}`, `rows[]` = one per leader (top 8: `leader`, `members`, `best`, `best_plain`, `refined`, `member_order`, `p10`/`p50`/`p90`/`song_max`/`dist_mean`/`dist_verified`, `alternatives[{members, member_names, best, refined:false}]` = that leader's next sets), and `ranking[]` = the GLOBAL top teams across leaders (default/max 50, `?top=` to trim; several entries may share a leader; the same fields, quantiles + order on the first 8). Files built by an older builder serve `ranking:null` and null quantiles. `scope=singers` → per-singer answers |
| `GET /api/ranks` | grade thresholds from the master: `unit_score[{grade,letter,plus,threshold,order}]`, `song.{default_group, groups{group:[{grade,letter,plus,threshold}]}, exceptions{music_id:group}}` |
| `POST /api/boardplan` | the optimised board for one exact team (beam, ~1-2 min) |
| `POST /api/optimize-rerank {basis,teams,song,across,refine,budget,pool,leader_diverse,…}` | develop + (refine) + re-rank the posted rosters — a thin wrapper over `develop_pipeline`; song rows carry `certificate`, `board_plan_summary`, and the response `model_version` / `inputs_hash` / `certificate_basis` |
| `POST /api/run {…/solve fields, board:'develop'|'current'|'off', budget:'real'|'full'|int, leaders, refine, develop_top, top, materials:'infinite'?, leader_member?, leader_card?, no_leader_overlap?}` → `202 {job_id, plan, opts}` | the ONE job: search → (leaders) → develop → (refine) → rank; events `plan`, `stage{stage,state}`, `progress{stage,…}`, `fact`, `board`, `collection`, `result` (payload with `explain` keyed by final roster, `warnings[]`, `notice[]`, `run{}`), `cancelled{stage}`, `error{stage,message}`, `end`; every `progress` event also carries `elapsed_s`, `stage_s`, `eta_s` (seconds left in that stage: from done/total, or the exact search's projection; `null` when nothing measured says), `eta_basis` (`rate`/`projection`/null) and `best_so_far{cards, value, metric:'song_est'\|'unit_score'\|'score', stage, provisional:true}` (`job_progress.py`); song-mode develop progress carries `song_est`; the top 8 song rows carry `p10`/`p50`/`p90`/`dist_mean`/`dist_verified`; cancel with `POST /cancel/<id>` (between stages / between teams). The page uses it when `localStorage.newrun === "1"` |
| `POST /api/board-doc {board_state, cards, basis, budget}` | the board-export doc for a team's characters with the GIVEN developed board overlaid (`to_buy`/`to_sell`/`optimised_nodes`/slots, `point_budget` labelled by kind) — no optimisation; the row's board section fetches it on expand |
| `GET /api/gacha` | the Gacha tab's view model: banners, expected-vs-observed, tutorial exclusions (`gacha_view.build_view`) |
| `GET /gachaart/<gacha_id>/<banner\|logo\|icon>[.png\|.webp]?w=N` | banner art from `data/gacha/art/`, server-side downscale like `/cardart/`. The **extension-less** form is what the page and the export ask for: some banners are PNG masters, others WEBP (`gacha_art`), and the route serves whichever file is on disk |
| `POST /api/gacha/sync` | runs `gacha_sync.py`; manual only, spends a login, appends to `data/gacha/*.jsonl` |
| `POST /api/gacha/export` | runs `gacha_export.py`; writes one self-contained HTML to `dumps/gacha_export_<ts>.html` |

**One number set per row (`row_detail.py`).** Every per-song figure a route
returns — rerank rows, `/solve` song rows (with `song_max`), compare, timeline,
row detail, event rows — goes through `song_rank.score_team_on_chart` (Overall
Power deck power by default). Song rows carry `deck_power`; across rows (rerank
and `/solve`) carry `member_order` = the slot order best on the most charts and
`member_order_share`; cached two-stage song rows carry `member_order` and
`refined`. The run law (`distribution`): `mean`, `sd`, `min`, `max`,
`p1 p5 p10 p25 p50 p75 p90 p95 p99`, `pmf{start,width,below,mass[]}`, `exact`,
`bin_width`, `expected_total`, `verified` (the law's expectation equals the
row's figure), `deck_power`, `member_order`.

**The Max is at the row's own slot order (`song_rank.score_team_ceiling`).** The
ceiling (`active_all_fire`) is the all-fire score at the row's `member_order`
(the law's expectation with every ACTIVE firing), not at a re-chosen slot order:
re-choosing the SPECIAL cue assignment could put the Max above every run the
row's own law allows. Exact laws end at the Max to the point. A gap of 7-26
points on BINNED laws (`exact:false`, support over 400,000) is the floor binning
of the top bin, not the ceiling.

### The objective leaf (song / across develop rows)

A developed song-mode (or across-songs) row's headline is the team scored on the
FIXED per-(character, role, budget, singer-context) plans of ONE `PlanCache`
seeded for the whole run — the same leaf the refine's workers and the EXACT
per-leader certificates score on. The seed is the per-leader table's own
`plan_cache_seed` when the run's `inputs_hash` + budget/uptime/platform/universe
match that table (so the rows and the certificates are literally the same
objective), else the members-first enumeration's, else the run's #1 developed
team; the response's `objective: {leaf, plan_cache_seed, seed_source, meaning}`
says which.

The per-team greedy re-develop (`optimize_and_rerank` per team) rides on every
row as a LABELLED what-if: `song_est_develop` / `across_score_develop`,
`develop_whatif: {label, leaf, <metric>, delta_pct}`, `board_state_develop`,
`board_plan_summary_develop` (`leaf: "develop"`), `points_develop`; the row's
own `board_state` / `board_plan_summary` (`leaf: "plan_cache"`) are the fixed
plan the score used. The ranking sorts by the objective; `certificate_basis.leaf`
names the leaf and `certindex.attach` refuses the ERROR verdict for a
develop-leaf row above a certified maximum (HEURISTIC + note instead — a
different objective, not a bug: the re-develop can beat the fixed plan by
0.2-0.7 %). Body `objective_leaf: "plan_cache"` (default) | `"develop"` (the
single-number rows; `tools/web/test_objective_leaf.py` pins it). The Compare
view (`board: "develop"`) carries the same pair and its `diff.objective_note`
says which number is which. Unit-Score (song-agnostic) rows have no plan-cache
objective and are labelled `leaf: "develop"`.

### Members-first seeds (song mode)

A song-mode run that generates candidates (`leader_diverse`) or refines, on the
5★ maxed universe (`basis` maxed / maxed-account, `pool` 5star), seeds every
leader's **members-first** best roster into the develop set when a cached
bare-proxy enumeration of that song exists on the run's own `inputs_hash`
(`tools/web/members_first_index.py`). The rows then develop / refine / rank like
any other candidate; a seeded row carries `provenance: "seed:members-first
K=2000"` and the response carries `members_first: {available, k, song,
enumeration_hash, proxy, leaders, leaders_seeded, added, already_seeded,
already_in_beam, wall_seconds, rows[]}` — or `{available: false, reason}` naming
the first difference (`enumeration hash 044e… ≠ run 9f1a…`, a different pool, an
unfinished enumeration, a non-song run). Body `members_first_k` sets K (default
2000, 0 disables); `HBUILDER_WEB_WORKERS` caps the candidate-scoring pool;
`HBUILDER_MEMBERS_FIRST_DIR` (os.pathsep-separated) adds directories to scan.

The enumerations (`members_first_<song>_nonsinger.{meta.json,scores.bin}`, one
per song, tens of MB each, gitignored) are precomputed outside the server. A
running server picks a newly finished enumeration up on its next request
(directory listings are cached for 2 s), loads it once per process (~12 s: read +
sort order + per-pool tables; ~310 MB resident for an 8.4 M-set pool), and
answers each leader from it in a few seconds. An enumeration can be updated
incrementally when a card lands: every old score is reused and only the sets
that contain the new card are scored, with per-file and per-card digests in the
meta so the next update verifies the old cards are unchanged.

The web uses the song's **own** enumeration when it exists (`mode: "own"`,
K=2,000) and otherwise **transfers** from every reference enumeration on the
same `inputs_hash` / pool / budget (`mode: "transfer"`, `sources: [...]`,
K=500 — the member-set ranking transfers across charts), and says so: the
response's `members_first` carries `mode`, `sources`, `source_songs`, `k`,
`label` and `measured_recall` (the summaries of every
`members_first_<song>_transfer.json` on file — what was measured, never a
guess; empty with a `measured_recall_note` when no report exists). The seeds
are HEURISTIC either way; a row reads EXACT only through the certificate guard.
Body `members_first_k` overrides K in both modes; absent, the mode picks its
default. The hash can serve as the cross-song guard because the recipe hashes
`charts.json` as a whole — the flip side is that a `charts.json` that gains a
song retires every enumeration until re-keyed.

### The card CSS is ported, not designed

Every value in `style.css`'s card-face block traces to a measurement of the
game's own `VerticalCardThumbnail_224` prefab, and reached this file through
`build_explorer.py`'s `PAGE` string — `aspect-ratio: 5/7`, `border-radius:
17.857% / 12.5%` (both terms, or it is an ellipse), `inset 0 0 0 1.79cqw` for
the stroke-4 frame, `0 3.57cqw 7.14cqw` for `ui_999_shadow_b16y8_a35_r40_9s`.
**No CSS value is retyped from taste**, and the comment naming the prefab number
is copied with the number.

Three elements are not the game's and are labelled as such in the stylesheet so
a reader does not go hunting for them in the dump: the `LEADER`/`MEMBER` role
chip, the rarity corner, and the green `NEW` pip on a face the rank above did
not have (an answer to "why is this team different", not a game element). The
game's `ico_element` badge and its `CharacterGroupingLabel` generation ribbons
are **not** ported; generations are shown as text instead.

---

## Known limits

| | |
|---|---|
| Selectors (`--require` / `--leader` / `--exclude`) | `/solve` runs an unconstrained pool; the plumbing takes them |
| `--exact`, `--acquire`, mode B | the exact branch is wired, but `should_use_heuristic` picks the beam on a ~90-card pool; it is not known whether `multiprocessing` spawn behaves under a request thread on Windows |
| A "why is #1 better than #2" answer | the page shows *that* ranks differ and *by how much*; the per-skill tables are side by side but not diffed |
| Number formatting | `toLocaleString` follows the *browser's* locale, so a Spanish-locale browser shows `459.427,6` where the CLI prints `459,427.6`. Same number, different separators |
