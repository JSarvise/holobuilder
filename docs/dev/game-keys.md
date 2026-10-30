# The game's build constants

Reading the game's API uses a constant that belongs to the **game client**, not to this project:
the build seed of its payload cipher. It is the same for every player of the same game build,
anyone with the game installed has it, and this repository deliberately does not carry it. The
refresh finds it in **your own game install**; you never have to look for it by hand.

## What needs it, and what does not

| | Needs the seed |
|---|---|
| The refresh steps that talk to the game's API: `master_index`, `account`, `inventory`, `event_cutoffs` | yes |
| The page buttons that run those: Refresh, the event's Sync, the gacha's Sync | yes |
| The downloads from the game's asset CDN (charts, card art, jackets, Live2D, chibis) | no |
| Everything built locally from what the refresh wrote | no |
| `tools/web/server.py` and every page: optimizer, boards, collection, event, compare | no |

So once a refresh has built the data, holobuilder works without the seed; only reading the
account (or new game data) again needs it. A refresh that needs it and finds none stops before
running anything and says how to get it.

## How the refresh finds it

`refresh.cmd` runs this whenever the PC has no seed, or when the one it has no longer opens the
server's answers (a game update can change it):

```
.venv\Scripts\python tools\recon\proxy\game_keys.py --find
```

1. It reads your installed client's `global-metadata.dat` and decrypts it
   (`decrypt_metadata.py` recovers the file's XOR key from the file itself), which exposes the
   client's string literals.
2. It asks the game's server for one anonymous `Master/Get`: the first request the client itself
   makes, with no account and no session.
3. The payload cipher is AES-128-CBC with KEY = MD5(seed) and IV = MD5(seed ‖ nonce), the nonce
   travelling in each message's envelope (`account_snapshot.py`, `seal`/`unseal`). Of the ~50,000
   literals, exactly one opens that answer into well-formed protobuf carrying the master version:
   that literal is the seed. If none or more than one does, nothing is written.

It writes `state\game_keys.json`, and never prints the values:

```json
{
  "grpc_seed": "<the payload cipher's build seed>",
  "metadata_key_hex": "<the global-metadata XOR key, 256 hex characters>"
}
```

`metadata_key_hex` is optional: it only saves `decrypt_metadata.py` re-deriving the key.

`game_keys.py` alone says what this PC has; `game_keys.py --check` asks the server (one anonymous
request) whether the seed still opens its answers.

## Where they are looked up

`tools/recon/proxy/game_keys.py`, in order:

1. a private `_game_keys.py` module beside it (the official app's build ships one);
2. the JSON file named by `HB_GAME_KEYS`;
3. `state\game_keys.json`.

Only use this with your own client and your own account. `state\` is ignored by git; do not
publish the values.
