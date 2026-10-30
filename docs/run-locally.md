# Run it yourself

holobuilder can run entirely on your own PC: your game install provides the game's data, your
account is read straight from the game's servers, and the pages are served on `127.0.0.1`. There
is no holobuilder server in the way, nothing to sign in to and nobody to approve you.

Prefer an installer? See [Getting started with the app](getting-started.md) instead.

## What you need

- **Windows 10 or 11.**
- **hololive Dreams**, installed from Steam on this PC and started at least once (that is when the
  game writes your account file). It does not need to be running.
- **Python 3.13** from [python.org](https://www.python.org/downloads/windows/), with *Add
  python.exe to PATH* ticked. 3.11 and newer work; 3.13 is the version the project is tested on.
- **gcc**, a C compiler. The team search runs three small C kernels that are compiled on your PC.
  The easiest is WinLibs: `winget install BrechtSanders.WinLibs.POSIX.UCRT` (or download it from
  [winlibs.com](https://winlibs.com/) and add its `mingw64\bin` folder to the PATH). Open a new
  window afterwards so the PATH is read again.
- **Git**, to clone and update (optional: the ZIP from the green *Code* button works too).
- Time and disk: the first refresh takes about **20 minutes** (most of it downloading every
  song's charts at a polite pace), then the pictures keep downloading in the background for about
  an hour. The data takes a few GB.

## 1. Get the code

```
git clone https://github.com/JSarvise/holobuilder
cd holobuilder
```

Any folder works. The commands below are typed in that folder (in PowerShell, Windows Terminal or
the classic Command Prompt); each script can also be double-clicked in Explorer.

## 2. Set up: `setup.cmd`

```
.\setup.cmd
```

It finds Python, creates a private environment in `.venv`, installs the packages listed in
`requirements.txt`, compiles the search kernels, and then checks the rest:

```
  [ok] Python          3.13.x (...\.venv\Scripts\python.exe)
  [ok] packages        installed
  [ok] C compiler      gcc (...\gcc.exe): search kernels built
  [ok] game install    C:\Program Files (x86)\Steam\steamapps\common\hololiveDreams
  [ok] account file    found in the game's own folder (read in place, never copied)
  [--] game constants  not yet: refresh.cmd finds them in your game install
  [--] region          not yet: the first refresh.cmd finds it
  [--] game data       not yet: run refresh.cmd
```

`[X]` lines block the local route and say what to do; `[--]` lines are done by the first refresh.
Run `setup.cmd` again whenever you like: it only installs what is missing.

## 3. Read the game and your account: `refresh.cmd`

```
.\refresh.cmd
```

In order, it:

1. **finds the game install** in your Steam libraries (or the folder `HOLODREAMS_DIR` names);
2. **finds your account file** in the game's own folder,
   `%USERPROFILE%\AppData\LocalLow\QualiArts, Inc_\hololive-Dreams`. The file *is* your account,
   so it is read in place at every refresh, never copied, and sent only to the game's own servers.
   If that folder has several, set `HB_CREDENTIAL_FILE` to yours;
3. **writes the client profile**: the headers the game's own client sends, from this PC's hardware
   and your Steam ID (`state\client_profile.json`);
4. **finds the game's build constants** in your game install, the first time and after a game
   update changes them: [what they are](dev/game-keys.md);
5. **finds your account's region** the first time: it logs in on the US, Asia and Japan servers in
   turn until one accepts, and keeps the answer in `state\local.json`. To set it yourself:
   `refresh.cmd --region as` (`us`, `as` or `jp`);
6. **runs the refresh** (`tools\recon\proxy\refresh_all.py --merge-collection`): the game's master
   tables, the card catalogue, every song's charts, the Holomem Boards, the open event, and your
   cards, boards and items. It prints what changed, step by step;
7. **reads your pull history** for the Gacha page, with its banners (`gacha_sync.py`), as the
   pages' Refresh button does.

The pictures (card art, song jackets, Live2D figures, chibis, Parkview maps) are fetched in the
background after everything else, so holobuilder is usable as soon as the window says *Done*. The
first time that takes about an hour; afterwards only what is new. Their progress goes to
`tools\recon\proxy\dumps\pictures_refresh.log` and the pages fill them in as they arrive; a refresh
started meanwhile leaves them to that run. Pictures the game has already shown on this PC are
copied from its own cache instead of downloaded.

**Open the game once a day before refreshing.** When the in-game day has changed (04:00 JST) and
the game has not been opened since, the server refuses part of the account read with
`DATE_CHANGED` until the game runs its daily reset. The refresh says so, keeps what it could read,
and the next refresh after opening the game completes it.

Every download from the game's servers is polite: one request per second, cached on disk, and
never asked for again once it is there.

Any other option goes to `refresh_all.py`, for example `refresh.cmd --skip chibi`;
`.venv\Scripts\python tools\recon\proxy\refresh_all.py --help` lists them all.

## 4. Start it: `start.cmd`

```
.\start.cmd
```

It starts holobuilder's local web server and opens <http://127.0.0.1:8731/optimizer> once it
answers. The window shows everything the server does; close it, or press Ctrl+C, to stop
holobuilder. The server listens on `127.0.0.1` only: nothing outside this PC can reach it.

- If port 8731 is still held by an earlier holobuilder of this folder, that one is stopped. Any
  other program keeps its port and the next free one is used. `start.cmd --port 8740` picks one.
- `start.cmd --no-browser` does not open the browser.
- The pages' **Refresh** button runs the same refresh as `refresh.cmd`, with the same account.

How to use the pages: [Using holobuilder](guide.md).

## Updating

- **After a game update**: run `refresh.cmd` (or press Refresh). New cards, songs and events are
  read in; the game's build constants are found again if the update changed them.
- **A new holobuilder version**: `git pull` (or a new ZIP), then `setup.cmd` if
  `requirements.txt` changed, then `refresh.cmd`.

## What is where

| Folder | What it holds | In git? |
|---|---|---|
| `.venv\` | the Python environment `setup.cmd` made | no |
| `state\` | this PC's own: your collection, client profile, the game's constants, the region | no, private |
| `data\owned.json`, `data\gacha\` | your cards and your pull history | no, private |
| `tools\recon\proxy\dumps\` | everything the refresh builds from the game | no |
| `tools\explorer\cards_explorer_art\` | the card pictures | no |

None of these are ever uploaded anywhere. Never share the `state` folder: it holds what the
refresh needs to read your account.

## Optional: the other regions' event borders

The refresh reads the event borders of **your own** region with your account. Each region (US,
Asia, JP) is a separate world, and a US account is refused by the JP and Asia servers, so the
other regions' borders need **one spare account per region**. Without them those regions are
skipped with a message; nothing else is affected.

How the game picks a region: the worldwide client measures the ping to the US and Asia servers
the first time it creates an account and keeps the faster one; the Japanese client always uses
JP. So a spare account is made in the game itself, from a client that lands on that region.

1. **Keep your own account safe first.** Starting a new account replaces the one the client is
   signed in to. Set a link ID and password on your main account in the game (account transfer)
   so you can get it back, or use a second PC or an emulator for the spare.
2. Start a fresh account on a client that lands on the region you want, and get past the
   opening screens.
3. Copy that account's credential file (the same kind of file `HB_CREDENTIAL_FILE` points at)
   and add it to `state\ranking_accounts.json`:

```json
{
  "jp": {"host": "jp.game-hololive-dreams.com", "credential_b64": "<base64 of the file>"},
  "as": {"host": "as.game-hololive-dreams.com", "credential_b64": "<base64 of the file>"}
}
```

   (`[Convert]::ToBase64String([IO.File]::ReadAllBytes("<file>"))` in PowerShell prints it.)
4. Switch the client back to your main account.

**Alternative: create the spare account without the client.** The game has no username or
password: an account *is* one opaque credential. `rpc.api.Auth/Create` (an empty request, no
session needed) returns a new credential, and **the host you call decides the region**, so a
`Create` sent to `jp.game-hololive-dreams.com` makes a JP account. `Auth/Login` with that
credential then gives the session token, as for your own account. The transport, headers and
message encoding are the ones `tools/recon/proxy/account_snapshot.py` uses for `Auth/Login`
(the schemas are in `tools/recon/protobufs/`); put the returned credential, base64-encoded, in
`state\ranking_accounts.json` as above. This repository deliberately ships no tool that sends
`Create`.

> **Your responsibility.** Creating accounts outside the official client is against the game's
> terms of service, and the server limits it per IP address
> (`ERROR_CODE_AUTH_CREATE_IP_RATE_LIMIT_EXCEEDED`). One account per region is all this needs;
> do not create more.

The spare accounts are only ever used to **read** the rankings: the tools refuse to send them
anything but the reads, plus the daily home login the server requires before it answers. Like
everything in `state\`, the file is ignored by git: never commit it.

## Without the scripts

The scripts only gather what the tools below need; everything can be run by hand:

```
python -m venv .venv
.venv\Scripts\pip install -e . -r requirements.txt
.venv\Scripts\python tools\app\client_profile.py --write
.venv\Scripts\python tools\recon\proxy\game_keys.py --find
set HB_CREDENTIAL_FILE=C:\path\to\the\game\account\file
set HB_ACCOUNT_HOST=us.game-hololive-dreams.com
.venv\Scripts\python tools\recon\proxy\refresh_all.py --merge-collection --pictures-later
.venv\Scripts\python tools\web\server.py --port 8731
```

(`set` is the Command Prompt's; in PowerShell it is `$env:HB_CREDENTIAL_FILE = "..."`.)

## Tests

`.venv\Scripts\python -m pytest` runs the test suite. Without the game data the tests that need
it are skipped, with the reason; after a refresh they run too.

Something not working? [Troubleshooting](troubleshooting.md#running-it-yourself).
