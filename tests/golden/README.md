# Golden outputs

A refactor must not change a single number the web server answers. These tools pin that down
on a frozen copy of the account, outside the live paths a refresh rewrites.

| File | What it does |
|---|---|
| `freeze.py` | Copies the inputs (catalogue, owned cards, collection, the small derived dumps, the event bundle, the master cache and the master JSON repo) into one snapshot directory, with a sha256 manifest. `--verify` checks the snapshot never changed. |
| `sandbox.py` | Builds a disposable copy of a checkout's code with the snapshot laid over it, and runs `tools/web/server.py` in it. |
| `site/sitecustomize.py` | Loaded by every process of a golden run: a fixed wall clock, a file-access log, and no network beyond loopback. Inert unless the runner sets its variables. |
| `golden.py` | The sample: per-song and every-song searches (account and maxed), the event search with a song and with Highlight, event points, row detail, the account board plan, the Versus compare and timelines, and the Optimizer's "Now" compare. Each search runs in a fresh server. |
| `refresh_offline.py` | Runs the offline refresh steps in a sandbox and hashes every file they leave. |

```
python tests/golden/freeze.py --source <live checkout> --out <snapshot>
python tests/golden/golden.py record  --frozen <snapshot>
python tests/golden/golden.py compare --frozen <snapshot>
python tests/golden/golden.py compare --frozen <snapshot> --serial    # one worker: search_song_account, event_song_account
python tests/golden/refresh_offline.py run --frozen <snapshot> --out <snapshot>/refresh_x.json --against <snapshot>/refresh_baseline.json
```

`golden.py` strips only what changes between two runs of the same code: run times, job ids, the
`written` stamp and the engine's warm-start count (`seeded`). A difference anywhere else is a
changed result: revert it or explain it, never re-record the golden. The one exception is the
input fingerprints a Compare answer reports (`inputs_hash`, `inputs_hash_short`,
`inputs_hash_files`): `golden.py refingerprint --frozen <snapshot> --run <dir>` re-records
only those, and refuses if any other field differs.
