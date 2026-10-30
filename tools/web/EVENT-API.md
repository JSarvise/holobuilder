# Event Points mode — HTTP API (backend contract)

Logic in `tools/web/event_mode.py`; routes in `tools/web/server.py`. Everything uses the
**newest open event** in `tools/recon/proxy/dumps/marathon_bonus.json` (written by every
refresh, `Event/ListEventInfo`) unless `event=<key>` is given.

Rules the numbers follow (`src/hbuilder/domain/marathon.py`):

- `E` (Event Points bonus) = live rows summed per card, **summed over the 5 members**. The leader adds nothing.
- `P` (Event Song Score bonus) = rows of the played song, **MAX over the 5 members**.
- Event Points are computed on the **raw score**: whether `P` lifts Event Pt is not known.
- The song-length multiplier `L` is an **estimate** (the server computes it; this is a fitted formula).
- Searches use the **account basis**: every owned card (all rarities) at its real level and bloom, real board points.

## Routes

| method | path | what |
|---|---|---|
| GET | `/event` | the page (`static/event.html`) |
| GET | `/api/event[?event=<key>]` | current event + bonus rows + your bonus cards. 503 `{available:false,error}` if no `marathon_bonus.json` / nothing open |
| GET | `/eventart/<name>.webp[?w=N]` | an event banner / logo (`dumps/event_art/`, written by `event_art.py`); 404 when absent or the name is not `[A-Za-z0-9_-]+.webp` |
| GET | `/api/event/songs[?boosts=3&difficulty=EXPERT]` | every song ranked by `L` (team-independent). 400 on boosts outside 1..10 or unknown difficulty |
| POST | `/api/event/run` | start the search job. 202 `{job_id, plan, opts}`; 400 on bad body; 503 if no event file |
| GET | `/events/<job_id>` | the job's event stream (SSE, `data: {json}` frames, `: keepalive` comments) — shared with `/api/run` |
| POST | `/cancel/<job_id>` | cancel. 200 `{job_id, cancelling:true}`; the stream then emits `cancelled` + `end` |

## The card object (every card everywhere in this API)

```json
{"id": "card-00030-4-cmmn-0000-00", "member": "Yukihana Lamy", "costume_name": "Dreamy Drop",
 "chr": "chr-00030", "type": "Happy", "rarity": 4, "level": 60, "bloom": 2, "limit_break": 3,
 "owned": true, "live_permil": 370, "song_permil": 0, "name": "Yukihana Lamy / Dreamy Drop (4★)"}
```

- `type` is the card's attribute as in `data/cards.json`; `rarity` is the int 3/4/5.
- `level` / `bloom` / `limit_break` come from `state/collection.json`; `null` for a card you do not own.
- `live_permil`: the card's whole live bonus (sum of every matching live row — holomem + card + rarity×bloom).
- `song_permil`: in `/api/event`, the largest song bonus of the card over the event songs (see
  `song_permil_by_music: {music_id: permil}`); in a job row, the bonus on the searched song.
- Extra keys by context: `permil` (the single row's own value, in `live.cards`), `song_permil_by_music`
  (in `/api/event`), `counts` (in job rows: `false` for the leader — its bonus does not count).

## GET /api/event

```json
{"available": true,
 "event": {"key": "marathon_event-normal-007-chapter-01", "title": "Neon Sunshine Festival",
           "kind_label": "Point Rally Event",
           "starts_at": "2026-10-15T03:00:00+00:00", "ends_at": "2026-10-17T10:59:59+00:00",
           "art": "/eventart/marathon_event-normal-007.webp",
           "logo": "/eventart/marathon_event-normal-007_logo.webp", "chapter": "01"},
 "open_events": ["marathon_event-normal-006-chapter-01", "marathon_event-normal-007-chapter-01"],
 "fetched_at": 1792065362.15,
 "live": {
   "holomems": [{"character_id": "chr-00023", "name": "Houshou Marine", "permil": 300}],
   "cards": [{"id": "card-00023-5-uniq-0085-00", "...card object...": "", "permil": 300}],
   "rarity_bloom": {"3": {"1": 10, "2": 10, "3": 20, "4": 20, "5": 30},
                    "4": {"1": 60, "...": 0}, "5": {"1": 150, "...": 0}},
   "attributes": []},
 "music": [{"music_id": "m0354", "title": "...", "permil": 100, "card": {"...card object...": ""}}],
 "event_songs": ["m0354"],
 "owned_bonus_cards": [{"...card object...": "", "song_permil_by_music": {}}],
 "rules": {"E": "...", "P": "...", "event_points": "..."}}
```

- `title`, `kind_label`, `starts_at`, `ends_at`, `art`, `logo` come from `dumps/event_meta.json`
  (`tools/recon/proxy/event_art.py`, the refresh's `event_art` step; sources in
  `event_art.py`'s docstring). `title` = the event poster's English name in the master (the text the
  logo draws); `kind_label` = `EventInfo.name` off the wire ("Point Rally Event" / "Score Challenge
  Event"); times are ISO 8601 UTC from the wire (`ends_at` = the chapter's end; the exchange stays open
  later). `art` = the 2:1 key-visual banner with the logo and kind printed on it (<= 1280 px WEBP);
  `logo` = the transparent title logo alone. Each is `null` when its source is missing (no meta file,
  no master row, or the file is not on disk) -- never invented.
- `owned_bonus_cards`: every owned card with a non-zero live or song bonus, sorted by `live_permil`
  desc, then `song_permil` desc, then id.
- A `music` row keyed on a holomem instead of a card carries `character_id` + `name` instead of `card`.

## GET /api/event/songs

```json
{"boosts": 3, "difficulty": "EXPERT", "length_multiplier_estimated": true,
 "formula": "L = 1000 + 10*ceil((sec - 90) * 100 / 190 * (1 + up/1000)); ...",
 "note": "Ranked by L alone; ...",
 "songs": [{"rank": 1, "music_id": "m0131", "title": "KANADE", "singer": "Otonose Kanade",
            "notes": 867, "seconds": 166, "length_permil": 1480, "event_song": false,
            "event_song_cards": [], "chart_available": true, "note": ""}]}
```

`notes` is the note count at `difficulty` (`null` if no chart file); `chart_available:false` means the
song cannot be searched at that difficulty. `note` is a short remark (event song, <= 90 s, no chart).

## POST /api/event/run

Body (all optional except `song`):

```json
{"objective": "event_points", "song": "m0342", "difficulty": "EXPERT",
 "boosts": 3, "holopass": true, "workers": 16, "top": 10, "event": null}
```

- `objective`: `"event_points"` (default) | `"event_song"`. Anything else -> 400.
- `boosts`: int 1..10 (default 3), else 400. `holopass`: bool (default true), else 400.
- `difficulty`: EASY | NORMAL | HARD | EXPERT (default), unknown -> 400.
- `top`: rows returned, clamped 1..50 (default 10).
- `assumptions`: the Score search's account levers, the same object as
  `POST /api/search` (`ondemand.parse_assumptions`: cards level / bloom / SP training /
  keep higher; boards `as_is` | `plan` with max points, unlimited materials, every holomem
  invited, reset cap, no refund, branches, targets). A bad lever -> 400. Absent -> the default
  basis (each team's boards planned on its real points, bench as captured);
  the page always sends it (default: the account as it is). A board constraint the quick plan
  lacks re-plans the best 16 rows exactly (stage `replan`, rows `source: "replanned"`); the
  bound is then not computed (`bound: null`). `assumption_notes[]` says what was assumed.
- `leader_member`, `leader_card`, `no_leader_overlap`, `require[]`, `exclude[]`: the Score
  search's pins (`ondemand.parse_filters`), echoed under `opts.filters`.

202 answer: `{"job_id": "...", "plan": [{"id","label","detail"} x4], "opts": {...resolved...}}`.

### Stream events (`/events/<job_id>`)

```
{"type":"plan","stages":[{"id":"build",...},{"id":"score_search",...},{"id":"event_search",...},{"id":"bound",...}],"opts":{...}}
{"type":"stage","stage":"build","state":"active"}
{"type":"stage","stage":"build","state":"done","pool":114,"carriers":75,"song_carriers":0,"notes":1157,"seconds":0.3}
{"type":"stage","stage":"score_search","state":"active"}
{"type":"progress","stage":"score_search","evaluated":120000,"elapsed_s":12.0,"stage_s":11.7,
 "eta_s":null,"eta_basis":null,
 "best_so_far":{"cards":["leader","m1","m2","m3","m4","m5"],"value":1500000.0,"metric":"score",
                "stage":"score_search","provisional":true}}   (about once a second; eta_s stays
                null here — the search's length is not known in advance; best_so_far is the best
                RAW score scored so far, the event objective re-ranks it)
{"type":"stage","stage":"score_search","state":"done","best_score":1500000.0,"seconds":24.1,"exact_evals":...}
{"type":"stage","stage":"event_search","state":"active"} ... "done" with "leaders_searched"
{"type":"stage","stage":"bound","state":"active"} ... "done" with "sets_above","certified"
{"type":"result","payload":{...},"elapsed_ms":61000}
{"type":"end"}
```

Failure: `{"type":"error","message":"..."}` then `end`. Cancel: `{"type":"cancelled","elapsed_ms":...}` then `end`.

### `result.payload`

```json
{"event": "marathon_event-normal-007-chapter-01", "objective": "event_points", "song": "m0342",
 "song_info": {"music_id": "m0342", "title": "We are GAMERS !!!!", "seconds": 166,
               "singer": "hololive GAMERS", "notes": 1157},
 "difficulty": "EXPERT", "boosts": 3, "holopass": true,
 "basis": "account: every owned card at its real level and bloom, real board points",
 "pool": 114, "dominated": 0, "length_permil": 1480,
 "length_multiplier_estimated": true, "length_formula": "...",
 "notes": ["Event Points use the raw score: ...", "Event Pt per play = ..."],
 "rows": [{"rank": 1,
           "leader": {"...card object...": "", "counts": false},
           "members": [{"...card object...": "", "counts": true}],
           "score": 1234567, "live_permil": 1520, "song_permil": 0,
           "objective": 489.1,
           "event_points_base": 490, "event_points": 34810, "event_song_score": 1234567,
           "source": "event_search", "status": "searched",
           "member_order": ["card-..."], "deck_power": 281463.0, "song_max": 1400000.0,
           "board_state": {"chr-...": {"nodes": ["..."], "slots": {}}},
           "unit_score": 1415240, "overall_power": 281463.0,
           "score_bonus": {"lines": [{"key": "active", "label": "Active Skill", "permil": 685}],
                           "score_bonus_permil": 1468, "unit_score": 1415240},
           "detail_matches": true,
           "p10": 1310000, "p50": 1350000, "p90": 1390000, "dist_mean": 1352000.0,
           "dist_verified": true}],          (quantiles of the exact run law: first 8 rows)
 "best_score": 1300000.0,
 "bound": {"objective": "event_points", "e_min": 1430.2, "threshold": 1430.2,
           "f_star": 489.1, "s_max": 1300000.0, "sets_above": 1234, "overflow": false,
           "certify_limit": 400000, "leaders_checked": ["card-..."], "improved": false,
           "certified": true,
           "assumptions": ["S_max = ... is the true best score on this song (searched, not proven)",
                           "the best team's leader is one of the leaders exact-scored above the bound"],
           "seconds": 14.0},
 "stats": {"score_search": {...}, "event_search": {...}},
 "elapsed_s": 61.0}
```

- `objective`: the continuous ranking value — Event Points: `(120 + S/15000)(1 + E/1000)`;
  Event Song: `S(1 + P/1000)`.
- `event_points`: Event Pt per play = `ceil(event_points_base x provide(boosts) x L x (2 if holopass))`
  with the `L` estimate (`null` if the song has no `playingSeconds`). `provide(boosts)` is the
  master's `marathon_score_provide_count` for the boosts spent, **not** the boost count itself:
  8, 16, 24, 30, 37, 42, 46, 50, 53, 56 for boosts 1..10 (`marathon.live_stamina_score`).
  Worked check against a live run (m0131, boosts 3, HoloPass): base 604 x 24 x 1.480 x 2
  = 42,908.16 -> **42,909**, the server's `event_points` exactly (`marathon.event_points` gives the
  same). Reading "boost count" as the 3 boosts would give 5,364, 8x too low. `event_song_score` = `ceil(S(1 + P/1000))`.
- Row detail: each shown row is re-developed on the event leaf (plain, no
  activation refinement) for its slot order `member_order`, `deck_power`, `song_max`,
  `board_state` and Unit Score panel (`unit_score`, `overall_power`, `score_bonus` — the
  five Score Bonus lines). `detail_matches` says the re-developed figure equals `score`;
  on a failure the row carries `detail_error` instead. Stream: a `stage` `detail` runs
  after the search. `POST /api/row-detail` with `refine:false` returns the same figures
  plus timeline / distribution for any row.
- `source`: `event_search` (B), `score_search` (A, the naive re-sort) or `bound` (C found it).
  `status`: `searched` / `triage` (two-stage row kind), `null` for `bound`.
- `bound`: for `event_song` the key is `p_min` instead of `e_min`. `certified:false` with
  `overflow:true` means too many sets lie above the bound to enumerate (`sets_above: null`).
  `certified` is always conditional on the two `assumptions`.

## Not in scope

`/api/optimize-rerank` does NOT take `objective`: it re-ranks on the develop pipeline's basis
(maxed 5★ / flat budgets), and event mode must use the account basis — mixing the two would rank by a
bonus on the wrong cards' scores. Use `/api/event/run`.

## Cutoffs — ranking borders per region + prediction

Logic in `tools/web/cutoffs_view.py`; the store
(`tools/recon/proxy/dumps/cutoffs/<region>/<chapter>.jsonl`) is written only by
`tools/recon/proxy/event_cutoffs.py`.

| method | path | what |
|---|---|---|
| GET | `/api/event/cutoffs?region=us\|as\|jp[&chapter=<id>][&kind=<kind>]` | stored borders + prediction. Default region `us`, newest chapter, first kind (`points`, else `total`). 400 on a bad region; 503 `{available:false, region, error}` when that region was never synced |
| POST | `/api/event/cutoffs/sync` | body `{region?: "all"\|"us"\|"as"\|"jp" (default all), self?: bool (default true)}`. Logs in and APPENDS one read per tier. 200 `{returncode, appended, failed_regions[], stdout}`; 400 bad region; 409 while one is running (or while a poller cycle runs). ~4 s per region (1 req/s) |
| GET | `/api/event/cutoffs/poller` | the optional local poller's status (below) |
| POST | `/api/event/cutoffs/poller` | body `{enabled?: bool, interval_min?: int ≥ 1}` → its status. 400 on a non-bool `enabled` or an interval < 1. Not persisted: a restarted server starts with it OFF (`HBUILDER_CUTOFFS_POLL_MIN=<min>` turns it on at start-up) |

Every region is read with its own **guest** account. `self: true` additionally reads the
user's own region (US) with the user's account, for the user's rank; if that account is
refused `DATE_CHANGED` (the game has not been opened this game day) the borders are read with
the US guest instead and the rank is skipped. A guest refused `DATE_CHANGED` gets one
`Home/Login` (the client's daily login, guest-only) and a retry.

### The poller — status

`{enabled, running, interval_min, min_interval_min: 1, next_delay_min, backing_off,
consecutive_failures, cycles, skipped, last_started, last_finished, last_ok, last_appended,
last_failed_regions[], last_error, next_run, requests_per_hour, accounts: "guests"}` (times in
epoch ms). Each cycle runs `event_cutoffs.py --region all` — **never `--self`** — so the user's
account is never used by it. 12 requests per cycle at 1 req/s (144/h at the default 5 min, 720/h
at 1 min). All regions failing doubles the wait (cap 60 min); no open chapter → 60 min; a cycle
is skipped while a manual sync runs. Policy and cost: `tools/web/cutoffs_poller.py`.

### GET /api/event/cutoffs — response

```json
{"available": true, "region": "us", "regions": {"us": true, "as": true, "jp": true},
 "event_id": "marathon_event-normal-007", "event_name": "Point Rally Event",
 "chapter_id": "marathon_event-normal-007-chapter-01", "chapters": ["…"],
 "kind": "points", "kinds": ["points"],
 "start_time": 1792033200000, "end_time": 1792234799000, "reveal_time": 1792292400000,
 "now": 1792076000000, "ended": false, "elapsed_fraction": 0.2126,
 "captured_at": 1792075887983, "age_s": 112, "snapshots": 1,
 "self": {"rank": 1385, "score": 415427, "out_of_range": false, "captured_at": 1792075887983},
 "tiers": [
  {"rank": 1000, "grade_id": "marathon_ranking_grade-diamond", "grade_name": "Diamond",
   "color1": "CB94FA", "color2": "853EEF",
   "score": 558196, "captured_at": 1792075887983, "filled": true,
   "previous": null, "rate_per_hour": null,
   "series": [[1792075887983, 558196]],
   "prediction": {"method": "pace", "final": 2636146, "low": 2636146, "high": 2636146,
                  "plus_minus": 0, "confidence": "low",
                  "points": [[1792075887983, 558196], "… 25 points to end_time"], "band": [],
                  "inputs": {"elapsed_fraction": 0.2117, "rate_avg_per_hour": 47074,
                             "rate_recent_per_hour": null},
                  "note": "pace extrapolation from the average pace only …"}}
 ]}
```

- All times are **epoch milliseconds** (the game's own unit). `tiers` is in reward order (best
  first), one row per reward tier of THIS region (JP's tiers differ from US/AS).
- `score: null, filled: false`: the server returned no border for that rank (fewer players than
  the rank) — draw it as "not full yet". A tier at **1,598** appears to be the
  everybody-who-played floor (inferred), not a meaningful pace.
- `previous` / `rate_per_hour`: the read before the latest one and the per-hour change between
  them (`null` with a single read). This is NOT a fixed "last hour" delta: reads are only as
  frequent as manual syncs or the poller's interval.
- `self`: the user's latest rank on this ranking, `null` unless a `self` sync covered this region.
- `prediction` (`null` when the tier has no border): `method` `pace` | `template` | `final`;
  `final` / `low` / `high` / `plus_minus` = (high − low)/2; `points` = 25 `[t, v]` from the latest
  read to `end_time` (dashed line); `band` = `[t, lo, hi]` (shaded area; **empty when there is
  only one estimate — draw no band rather than invent one**); `confidence` `low` | `rough` |
  `medium` | `final`; `note` = one human sentence to show under the chart. The pace method
  under-predicts the end-of-event sprint by construction; show its `note`.

## GET /api/event/top — the top 100 of a chapter

`GET /api/event/top?region=us[&chapter=<id>][&uid=<public id>]`. Read from
`dumps/cutoffs/<region>/<chapter>.top.jsonl`, which only the **24/7 tracker** writes
(`tools/recon/proxy/event_tracker.py` on the VPS, pulled in by `tracker_pull.py`; with
`state/tracker_remote.json` present the server's poller runs the pull instead of
`event_cutoffs.py`). 400 on a bad region; 503 `{available:false,error}` when no board is stored.

`{region, chapter_id, event_id, event_name, chapter_number, start_time, end_time, now, kind,
captured_at, final, reads, first_read, chapters[{chapter_id, chapter_number}], rows[], player?}`.
Each row: `{rank, score, uid, name, gain_1h, rank_1h, reads_with_gain_1h, last_gain_at, pace_h}`
— `gain_1h`/`rank_1h` are null when the read an hour earlier did not have that player in the top
100 (`rank_1h` > 0 = climbed); `reads_with_gain_1h` counts READS that moved, not Lives (two Lives
between two reads count once); `pace_h` is Event Pt per hour from the oldest read inside the last
3 h, null under an hour of reads. With `uid`, `player = {uid, series: [[captured_at, score]]}`
while that player was in the top 100. Pure computation: `tools/web/top_view.py`.

**On demand:** each request (without `uid`) renews a 5-min viewer lease on the
VPS tracker for that region (`tracker_pull.py --watch <region>`, in the background, at most once per
50 s per region): while a lease holds the tracker reads that board every 5 min; with none it is not
read at all (the borders, every region, every 5 min regardless). The page re-asks every minute while it is
visible. `watching: true` in the body (also on the 503) says a lease was asked for.

## GET /api/event/history — every event on record

Pure read of `tools/recon/proxy/dumps/event_history.json`, which the refresh's `event_history` step
builds ADDITIVELY (an event once seen is never dropped) from three local sources: `event_meta.json`
(title, dates, banner, archived while the game lists the event), the cutoff store (the user's rank,
`self_points.jsonl` Event Pt from `Marathon/Top`, final borders) and the emblems the user holds
(`User/Get` `user_emblem_list`: the tier of events older than the stored reads). `{events: []}` before the
first build. Opened from the Event page's banner.

```
{events: [ {event_id, title, kind_label, starts_at, ends_at, banner,      // newest first
            chapters: [ {chapter_id, number, title, ranking: "points"|"total",
                         rank, outside, final, read_at, event_pt,
                         reached: {grade_id, grade_name, color1} | null,
                         from_emblem,                                      // tier only, no rank
                         borders: [{rank, grade_id, grade_name, color1, score|null}],
                         borders_final} ]} ]}
```
