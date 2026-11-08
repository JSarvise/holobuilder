# Changelog

Every app version, newest first. The app updates itself; the same notes show in its update banner.

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
