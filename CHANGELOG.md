# Changelog

Every app version, newest first. The app updates itself; the same notes show in its update banner.

## 0.2.14 (2026-11-19)
- **Fix: "the wallet cannot fund a plan for this team"** showed on a team's details when another team's details were still being computed (opening one team while another was loading, most often on a Highlight pool). It was never the materials: two board plans at once evicted each other's scorer. Plans now run side by side with the same numbers as one at a time, and a plan that could not be scored says so instead of blaming the wallet.
- **Event Pt view: the Multi Live calculator in its own window.** It was a block between the figures and the cards that made the view taller than Score's and left the five cards below the leader. It is now a tile under the big Event Pt that shows the last result and opens the calculator in a window: the room's MULTI SCORE + your score = the Event Pt, with what it counts (Event Bonus, Live Boosts, HoloPass, Highlight or length bonus).
- **Fix: a Highlight team's Unit Score** stayed a loading bar; it now shows the Unit Score of the board its details show. On Event Pt the Highlight row no longer shows a Max tile, which a pool cannot have.
- **A smaller window:** the figures beside the big number move under it instead of being cut off.

## 0.2.13 (2026-11-19)
- **Board plan with your materials: the interval nodes split across the team.** When the planner chose how many interval (Act.CD) nodes each member's Holomem Board takes, it ranked each board's counts on its own, and missed the splits that only win together (one member giving up its interval nodes so that another's skill fires more often). It now screens every combination and scores the best ones exactly. On our test account, 8 of the 31 rows of a one-song search rise (up to +0.9 %), 2 move down by 0.02 % and the first is unchanged; the search is about 5 % faster, and a Highlight pool plan gains up to 0.2 % in about 30 % less time. Contributed by namirin1 ([pull request #1](https://github.com/JSarvise/holobuilder/pull/1)), the project's first outside contribution.

## 0.2.12 (2026-11-18)
- **Event: plan a later chapter.** In a Spotlight event the Optimizer's header shows one face per chapter, the running one ringed. Pick another chapter and the search plans for it: its Event Bonus holomem and cards, its event song and its Highlight pool. The choice lasts while the page is open; the next visit starts on the running chapter.
- **Highlight: the board plan and the Connect cards for the whole pool.** A Highlight team's details planned the Holomem Board and dealt the Connect cards on one song of the pool. They now value the mean over the pool (up to six charts of a larger one) and deal the Connect cards for each song's singers, mixing them while the pool's mean rises: on our test account +4.3 % on a 4-song pool and +3.1 % on a 15-song one. The details take about 9-13 s. The search's list of teams is unchanged.
- **Fix: a first refresh** on a new install read Holomem Rating's songs before it had fetched them; the song table now comes with the first master tables.
- **Fix: the Optimizer's song header** cut the facts under the title (in Event, the song's length and its length bonus never showed); they now wrap onto a second line. An empty box no longer shows beside the goal outside Event.

## 0.2.11 (2026-11-17)
- **Running from source on Python 3.15:** the searches run about 7-17 % faster, with the same answers, as in the app. Install Python 3.15 beside 3.13 and run `setup.cmd` (or `./setup.sh`): holobuilder moves to 3.15, and the refresh steps that read the game's pictures, Live2D and chibi models and the game client's version run in a second environment, `.venv-assets`, on 3.13 (UnityPy, which reads them, has no 3.15 build yet). The old `.venv` stays aside until the new one works, and comes back if anything fails. Python 3.13 alone works as before. Already running from source: `update.cmd` brings this version and keeps your Python, and at its end says whether Python 3.15 is installed and what to run. See "Two environments" in docs/run-locally.md.

## 0.2.10 (2026-11-17)
- **Faster searches, the same answers.** "In general" on a maxed account is about 14 % faster (71 → 61 s on our PC, before the newer Python below). A search no longer scores again the teams it already scored (its second start and its final settle repeated up to 40 % and 95 % of theirs), the leaders' every-song reference teams are scored once, and Python's garbage collector no longer walks the workers' caches over and over (about a fifth of each team's score on the search's sample). A song search's Max now comes with its refinement (about 4 % faster). The answers are the same, to the last digit.
- **Team details with the score distribution about 7 times faster** (9.4 → 1.3 s on a maxed song): the exact law of the run score is merged in C, with the same numbers. The Event search scores its first row's distribution too: an Event search on your account is about 25 % faster.
- **The app runs on Python 3.15:** its Windows build runs this code about 7 % faster on "In general" and 13 % on one song, with the same numbers. Running from source stays on Python 3.13: the data refresh needs UnityPy, which has no Python 3.15 build yet, so it gives the same answers, a little slower.
- **Board planner on HiGHS 1.16.** Between board plans the planner finds equally good, it now and then picks another node: on our test account one "In general" best team moved by 0.04 %.

## 0.2.9 (2026-11-15)
- **Faster "In general" on a maxed account:** about 10 s less (about 12 %), close to its time before 0.2.8's combination search. Each team's songs are now spread over every worker instead of one team a worker, the songs the team list already scored are not scored again, and the C core screens the frequency-count combinations about 1.5 times faster. The answers are the same, to the last digit.

## 0.2.8 (2026-11-15)
- **Better teams in "In general".** For each song, every combination of the members' activation-frequency node counts is now tried (before, a few rounds of the best single change) and the best one is checked on its real board. The best teams score about 0.17 % more on average (up to 0.36 %), none less, and some move up the list (one from #13 to #7). An "In general" search on a maxed account takes about 80 s instead of 70.
- **Faster song and Highlight searches** (about 10 %): the cooldown screen reuses its work across the combinations it tries; the answers are the same.

## 0.2.7 (2026-11-14)
- **Fix: Holomem Rating, "One team for every song" on one Holomem Board setup.** A team's details planned a board of the shown song's own, so on every song but one they read 2.5 to 6.8 % above the team's row, with another slot order. They now show the team on its one board, as the row was scored: the same Expected Score, Max, Unit Score, slot order and board plan on every song.
- **Fix: Holomem Rating, switching songs.** When the same team was found for two songs, picking the other song showed the first song's details for a moment, with a false warning that the details differ from the row (by up to about 5 %). The details now load for the song picked.
- **Export search:** a small button beside an answer (Score's status line, Holomem Rating's heading and the Event goal's list of teams) saves a .json file with your options, the page's last requests and what came back. Attach it when you report a problem; it holds no login or player ID.
- The Event search's progress no longer reads "undefined / 3" while it reads the best teams' boards.

## 0.2.6 (2026-11-14)
- **Better teams on a maxed account: every Connect card in one place.** The team's Connect cards and the bench's are now dealt together on every song, so a team can take a bench card that lifts it more, a bench singer keeps the card her song bonus wants, and no card counts twice. The #1 rises on one song (+0.17 % on EXPERT) and on the every-song average (+0.69 %); a team's details show the bench's Connect cards too.
- **Searches that miss fewer leaders.** Every class of leader is searched (an outfit that raises one stat gets the sets it favours), each leader's song sample is weighed for her, and each leader's best team is re-scored on every song: every-song searches now find teams they used to miss (+0.2 % at levels 25-30).
- **Score Support ties as the game breaks them:** when two members tie for a Score Support, the slot order the team plays decides (it moved 35 of 300 strong teams by up to 0.45 %).
- **Your account: freed Board Points spent again.** When a search drops an activation-frequency node, the points it frees buy other nodes when that scores more (+0.03-0.19 %).
- **Speed:** a song's chart is prepared in one call of the C core (one song ~15 % faster). Every-song and Highlight searches on a maxed account take about 70 s and 13 s, a little longer than 0.2.5 for the dealing above.

## 0.2.5 (2026-11-13)
- **Running from source: `update.cmd`** (`./update.sh` on Linux) brings each new version from GitHub, runs setup when the packages changed, then refreshes. A plain `git pull` stops with "Your local changes ... would be overwritten by merge: data/cards.json" once a refresh has added cards; `update.cmd` sets those cards aside and the refresh adds them back. **This first time, update by hand** (the folder has no `update.cmd` yet): `git checkout -- data/cards.json`, `git pull`, `setup.cmd`, `refresh.cmd` ([Run it from source, Updating](docs/run-locally.md#updating)).
- **Refresh about four times faster** (35 s to 9 s on a typical account). It signs in once instead of five times, keeps one connection to the game open, reads your own event rank without pauses, and fetches the 54 Holomem Rating reward borders four at a time. What it brings in is the same as before.
- **Fix:** opening a page (Collection, for example) while the Optimizer was still working could leave it stuck on "Loading the catalogue..." with nothing clickable. The pages now load whatever the Optimizer is doing.
- **Holomem Rating:** the last answer stays when you move to another page and come back, with the song and team you picked. In "one team for every song", the strip shows the five members and the title names the leader.
- **Member Power-Up Bonus:** when a search raises your cards' levels ("My cards, upgraded", "Maxed account"), the line in the team's Unit Score Details is underlined; hovering says how much the search's levels raise the rate and about what the line is with your cards as they are.

## 0.2.4 (2026-11-12)
- **Event Song Score: sort by Expected or Max.** Options has a Sort by control on the Event Song Score goal; every team of a one-song search now carries its Max (every Active fires) and its Max Event Song Score, and sorting by Max ranks and shows them by it (not for Highlight, a random pool with no single best run).
- **Click a team's card for its full detail.** In the Optimizer (Score, Event and Holomem Rating), clicking any of a team's six cards opens the same card screen as Collection: its level, Bloom and Limit Break as the search scored them beside your own, its skills, Outfit Skill, stats and event bonus; the arrows step through the six cards.

## 0.2.3 (2026-11-12)
- **Score pace, redesigned.** Clicking a bar of the Score distribution now shows a clean curve: each Special's score in one row above it, the grades at its left edge, and under it a strip that shows, stretch by stretch between Specials, what those runs gained or lost against an average run. The lane's title says the gap at the end and the stretch that decided it; hovering gives the gap, the 8-in-10 band and that stretch's gain at any moment. With no bar picked, the strip shows where runs spread apart.

## 0.2.2 (2026-11-12)
- **Fix (update now):** in 0.2.1 a team's details in the Optimizer (Unit Score, Skill uptime, the Holomem Board plan) could stay loading after a search. They load again.
- Skill uptime: nothing runs past the right edge any more (the last Active chances, the song's end time, the Score pace's last figures), and a Special's badge no longer covers the Active chances under it.

## 0.2.1 (2026-11-12)
- **Holomem Rating, a new goal in the Optimizer.** Climb one holomem's high-score ranking: your rating on her (your three best songs of hers, each worth its highest score / 5,000, rounded up), your rank and the next reward border, and the songs that count for her, each at its hardest chart up to the level you Full Combo. Two searches, with her leading (a play counts for the leader): **a team per song**, ranked by Max (every Active fires: the ranking keeps your best run) or by Expected, with the rating each song would reach and what it adds to your top three; or **one team for every song**, ranked by the rating it leaves, on one board setup for every song or a setup per song. Refresh reads your records, the songs you own and your rank on every holomem.
- The score headers keep their figures in fixed places from the first paint, and every-song rows no longer show an empty Max slot.
- A refresh on a fresh install creates the folders it writes into.

## 0.2.0 (2026-11-11)
Every update since 0.1.45 together, on a renewed code base; the first seven points are new since 0.1.62.
- **Safer by default (update now).** The local server answers only its own pages: every request must name it as 127.0.0.1 or localhost, every change needs the page's own session key, and the scripts it loads from a CDN are checked against their hashes. Uninstalling removes only what the install put there, never a folder it did not create; the installer that runs updates is replaced with every update; the collection editor saves only a collection the app can read, and never empties one; the refresh writes its files whole.
- **Better teams within your materials.** A one-song search whose best teams are re-planned within your Cubes and Hexas now weighs each team's activation-frequency nodes while it plans them, and keeps every team the plain order would have planned (#1 on We are GAMERS !!!! EXPERT +0.85 %).
- **Activation chances as the game rolls them.** When two Specials that raise the Activation Rate are live on the same check, the larger one counts (the score used to combine them), and Skill uptime shows each chance as the game holds it, in tenths of a percent (76.7 %, not 77 %).
- **Score pace.** Click a bar of the Score distribution and Skill uptime shows the score the runs that end there have by each Special and at the end, against an average run, with the grades they pass and about when. Exact, from the same calculation as the bars. The row's p10 / p50 / p90 line is back under its score.
- **Compare keeps your nodes** where it costs nothing: the plan made from scratch prefers the nodes you already have, so it sells far fewer.
- **Your player level counts:** the planners no longer buy board nodes above it.
- **Sturdier searches:** a worker process that dies no longer hangs a search, and a search names every file it needs that is missing and what builds it.
- **Linux.** holobuilder runs from source on Linux, with the game under Proton: `setup.sh`, `refresh.sh` and `start.sh` find Steam, the game's Proton prefix and your account by themselves, and the C kernels build there too. The desktop app stays Windows-only.
- **Much faster searches, with much less memory.** One song with your account takes about 22 s instead of 37 s, every song on a maxed account about 53 s instead of 4 min, Highlight with your account about 15 s instead of 101 s (measured on an 8-core PC, 16 workers). A search's peak memory drops from about 12 GB to about 4.5 GB, and the number of worker processes follows the PC's RAM.
- **Searches that find better teams.** Boards planned within your materials try three plans per team and keep the best (up to +1.9 %); every-song searches also try each leader on the team its outfit favours, so leaders such as Noel / Soleil Kiss are no longer missed; and the searches weigh each team's activation-frequency nodes song by song, which raises the #1 on some songs and every-song averages by 0.1-1.9 %.
- **Maxed account with the game's real Board Points:** 361 per holomem, what the game gives at max level, instead of the cost of the whole board (453).
- **Park: Free Chats.** Every Free Chat you have read, where each conversation stands and who is in it, kept across refreshes; *Scan all areas* (Settings) reads every area at once.
- **Export a team** as a `.hbteam` file, with its score note by note in a `.csv` beside it, to send to someone who checks a result or a possible bug.
- **Renewed code.** Lint, formatting and type checks on every change, tests on Windows and Linux, comments that describe the code as it is, the largest modules split, and a test guard that keeps tests from writing into your data.

## 0.1.62 (2026-11-10)
- Every-song searches with boards the app plans (a maxed account, or yours with unlimited materials) now choose each team's activation-frequency nodes song by song, as the one-song search does: until now every board bought all of them on every song. The 50 teams served are scored that way on every chart, so their averages rise 0.1-1.9 % (the plain average stays beside each) and the order changes: on a maxed account over every EXPERT chart another team is #1 (3,969,840; the old #1 is now 4th at 3,968,517). A team's details on a song use the same choice. Searches on your boards as they are, or planned within your materials, give the same results as before.
- Faster searches with the same results: a team's boards for the different songs now share the work that does not depend on the song. An every-song search with your account and planned boards takes about 29 s instead of 31 s, and on a maxed account about 53 s instead of 55 s even with the new step (measured on an 8-core PC, 16 workers, with the game open).
- A team's score no longer depends on which of the app's processes computed it: with planned boards the same team could score a few points differently from one run to the next (1-7 points on a song, on about one team in a hundred), because a board's nodes were added up in an order that changed between processes. They are now added in the game's own order.

## 0.1.61 (2026-11-10)
- Same results as 0.1.60, faster with your account and planned boards: the search kept rebuilding the off-team boards for each song's singers, team after team. An every-song search takes about 28 s instead of 48-54 s and Highlight about 15 s instead of 56 s (measured on an 8-core PC, 16 workers); one-song searches and the maxed account are unchanged. The boards planned within your Cubes and Hexas are now most of an every-song search's time.
- Every song on a maxed account runs two searches and merges them; the progress line now says which one is running ("search 2 of 2") instead of looking like the search started over.

## 0.1.60 (2026-11-09)
- Faster searches, and the one-song search now weighs each team's activation-frequency nodes while it searches, so it finds teams it used to miss (a higher #1 on some songs).

## 0.1.59 (2026-11-09)
- Fixes Settings showing the previous version after an update (the Park section was missing).

## 0.1.58 (2026-11-09)
- Park: Scan all areas (turn it on in Settings) reads every area's Free Chats; a plainer Park page.

## 0.1.57 (2026-11-08)
- Song scores now match the game on charts with curved slides: the game also counts each slide's invisible points (hidden relay and curve points) when it shares a team's score among the notes, although they are never scored, so every real note is worth a little less. Scores drop on almost every chart, by 0.7 % on a typical one and up to 12.5 % on charts full of curved slides; a run's first note now matches the game exactly (e.g. 1,301 on Multiverse EXPERT instead of 1,336). The best team on one song is the same; "every song" and Highlight can recommend another, because charts with many curved slides count less in the average.

## 0.1.56 (2026-11-08)
- A team's details on a PC or AUTO search now give the same score as its row: they were scored as a manual run on mobile, so an AUTO row's details read about 30 % high (on one song with the account: 2,491,370 against the row's 1,895,080).
- Export: a new button on every team saves it as a .hbteam file in your Downloads folder, with a .csv of its score note by note beside it, to send to someone who checks a result or a possible bug. The file holds the song and options, the six cards, the numbers shown, and the account data the score uses: your card levels and blooms, Holomem Boards, Cubes/Hexas and Board Points. It holds no login or player ID.

## 0.1.55 (2026-11-07)
- Searches use much less memory: the peak over the measured searches drops from about 12 GB to about 4.5 GB, and the number of worker processes now adjusts to the PC's RAM (14 on a 16 GB PC, 6 on 8 GB), so a normal search fits comfortably on a 16 GB PC. The results are the same.

## 0.1.54 (2026-11-07)
- AUTO: scores are 2.6-5.1 % lower, because they now follow the game's rule: on AUTO the combo never builds up, so no note gets the combo multiplier, and a Special whose Activation Rate UP needs a combo of 100 no longer raises it there. The order of the teams barely changes. Manual play is unchanged: the combo still counts note by note.
- Sort by Max works: each team of a one-song search carries the score it reaches if every Active skill fires, and the list shows and sorts by it (an every-song search is an average and has no Max, so the option is off there). Teams to show shows that many teams, and One per leader picks from every team before the cut.
- With Unlimited Cubes and Hexas, a team's details now give the same score as its row: they showed about 3.6 % less, because the boards of the song's singers outside the team were not planned there.
- The score distribution's percentiles are no longer a little low on long songs (15 to 29 points), and its Max is the team's real ceiling. The Skill uptime chart shows each Active check's chance as the score uses it, with the board's and the team's Activation Rate UP (78 % where it said 55 %) and higher inside a Special that raises it, which it names.
- Lighter and quicker to change options: resets, refunds, branches, targets and a reset's minimum gain no longer restart the search, going back to an option reuses the boards it planned, and every search shares one set of worker processes, so two kinds of search kept ready use about a third less memory (about 16 GB instead of 24 GB, measured with 16 worker processes). The app window at rest uses almost no CPU instead of one to two cores: the leader's figure is drawn at 30 frames a second and, like the cards' shine, stops after 30 seconds without input.

## 0.1.53 (2026-11-07)
- Same results as 0.1.52, faster: the step that places the Connect cards on each team's boards under the song's score-bonus cap now runs compiled, for every team a search scores and every board the refinement tries. With every card maxed, a search on one song takes about 10 s instead of 19 s, on every song about 1 min instead of 2 min 12 s, Highlight about 16 s instead of 40 s; with your account, one song about 22 s instead of 24 s, every song about 49 s instead of 55 s, Highlight about 58 s instead of 67 s (measured on an 8-core PC). Running from source now needs Python 3.12 or newer (3.13 is the tested one): setup names an older one it finds and makes a .venv made with one again.

## 0.1.52 (2026-11-07)
- Same results as 0.1.51, faster event searches: event points on one song about 30 s instead of 33 s, Highlight about 84 s instead of 101 s with your account and 49 s instead of 55 s with every card maxed (measured on an 8-core PC). The search now sends every leader's teams of a round to the workers at once, and the quick screen that picks which teams to score runs on every core while the workers wait.

## 0.1.51 (2026-11-06)
- Maxed account: every holomem now gets the Holomem Board points the game gives at max level (361, read from the game's data) instead of the cost of the whole board (453), which no account can hold. The answers are the same, because every node that adds score fits in 361, and maxed searches are faster: one song about 21 s instead of 29 s, every song about 2.5 min instead of 4 min (measured on an 8-core PC). The maxed board plan now warns when your boards already hold more than that, from nodes that add no score.

## 0.1.50 (2026-11-06)
- A search on every song now also tries each leader on the kind of team its outfit favours: the members strong in the stat it raises, holding the cards it needs. With every card maxed it no longer misses a leader such as Noel / Soleil Kiss, so the recommended team can change and score higher (up to about +1 % on the measured cases); those searches take up to about 15 % longer. On every song with an account's own cards, the test account's answer did not change.

## 0.1.49 (2026-11-06)
- Same results as 0.1.48, faster: a search on one song with your account takes about 25 s instead of 37 s, one with every card maxed about 25 s instead of 38 s, an event search about 27 s instead of 34 s (measured on an 8-core PC). An available update now shows as a band at the top of every page until you update or close it; before, the start screen opened the Optimizer before you could click Update.

## 0.1.48 (2026-11-06)
- Boards planned within your materials: the planner tries two more plans for every team it shows and keeps whichever scores best, so a planned team never scores lower than before and some score higher (up to about +0.2 % on one song, up to about +1.9 % on every song). A row's details open on the very plan it was ranked with, and re-plan when the account or the options change. Searches that plan boards take a few seconds more.

## 0.1.47 (2026-11-05)
- Same results as 0.1.46. After a game update, the account refresh no longer guesses the game's new version: it retries once with the published one and otherwise says the app is waiting for it. The Event page warns when the live borders stop updating during an event.

## 0.1.46 (2026-11-04)
- Same results as 0.1.45. Running from source: the refresh scripts are in `tools/refresh` and `start.cmd` opens the app. A search that hits a missing account file now ends with an error naming the file instead of waiting forever, and the Inventory page says why it is empty. Sign-in and downloads no longer fail with "CERTIFICATE_VERIFY_FAILED" on a Windows that has not yet fetched the needed root certificate (a fresh install, Windows Sandbox): the app and the installer now let Windows check HTTPS certificates. On a PC without the game installed, a search with boards no longer fails with "SkillTreeNode is not in the local master cache": the board prices and point budgets come from the synced data. The Park page no longer logs into the game (which ended the game's own session): it shows the pick saved by the last Refresh. The Free-Style Cosplay Night banners have their art.

## 0.1.45 (2026-11-04)
- Unit Score Details on a chosen song now match the game: Overall Power and Score Bonus include the singing leader's red nodes and the song's yellow bonus; rankings shift slightly for teams whose leader sings.

## 0.1.44 (2026-11-03)
- Board plan: click a socket to read its card and see its whole range on the board.

## 0.1.43 (2026-11-03)
- Score Support passives now go to the members with the strongest Active Skill, as in the game; the Options sheet shows the bonuses each search uses.

## 0.1.42 (2026-10-30)
- Requests name the app (the server is now behind Cloudflare); Live2D's engine downloads from Live2D itself; safer update checks.

## 0.1.41 (2026-10-30)
- Highlight: cleaner whole-pool view (score by song drawn), no explanation list.

## 0.1.40 (2026-10-30)
- Highlight: the bonus follows Live Boosts (+54/50/42 %); 'The whole pool' view with one board and slot order for every song, yellows planned across the pool; clearer Multi Live calculator.

## 0.1.39 (2026-10-29)
- Board plan: hovering a Connect socket no longer flickers.
