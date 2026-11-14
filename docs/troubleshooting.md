# Troubleshooting

Find the message you see, then the fix.

- [The app](#the-app) (installed with `holobuilder-setup.exe`)
- [Running from source](#running-from-source) (`setup`, `refresh`, `start`: `.cmd` on Windows, `.sh` on Linux)

## The app

The app's log is `app\state\server.log` inside the install folder; attach it (or its last lines)
when you report a problem.

| Message | What it means | What to do |
|---|---|---|
| **Failed to fetch** | The page could not reach the app's local engine: it stopped, or the app was restarting (often right after an update). | Close the app and open it again. If it keeps happening, check `server.log`. |
| **The event search could not start** | Same as above, when starting a search. | Reopen the app and run the search again. |
| **account could not be read** | The game refused the login, or the account file could not be used. | In **Settings** check the **Account file** and the **Region**. Open the game once on this PC (that refreshes the credential), then press **Refresh**. |
| **access is waiting for an admin's approval** | Your sign-in is registered but not approved yet. | Wait for approval, then open the app again. |
| **this PC's access key is not known to the server** | The app's access key was removed or revoked. | Run `holobuilder-setup.exe` again and sign in. |
| **Update required** / update failed | A newer version is needed and could not be installed. | Run the installer again: it installs the latest version and keeps your settings and data. |
| **The engine did not answer in 90 s** | The local optimizer did not start. | Check that no antivirus blocked `runtime\python.exe` in the install folder; reopen the app. |
| The leader is shown as a card instead of an animated figure | The Live2D engine could not be downloaded from Live2D's site. | Check the internet connection; it is downloaded once and kept. |
| Scores look out of date | Results use your account as of the last refresh. | Press **Refresh**. |

## Running from source

Everything prints to the script's own window; copy the last lines from there when you report a
problem. `setup.cmd` can be run again at any time to see what is missing.

| Message | What it means | What to do |
|---|---|---|
| **Python 3.12 or newer was not found** (`setup.cmd`) | No usable Python on this PC. The `python` that opens the Microsoft Store does not count. | Install Python 3.13 from python.org with *Add python.exe to PATH* ticked, open a new window, run `setup.cmd` again. |
| **Python 3.x is too old: holobuilder needs Python 3.12 or newer** (`setup.cmd`, `setup.sh`) | Only an older Python is on this PC. | Install Python 3.13 (python.org on Windows, your distribution's package on Linux), open a new window, run `setup` again. |
| **This folder's .venv runs Python 3.x: holobuilder needs 3.12 or newer** (`start`, `refresh`) | The folder was set up with an older Python. | Install Python 3.13 and run `setup` again: it makes `.venv` again with it. |
| **The packages did not install** | `pip` failed: no internet, or a package has no ready-made build for this Python version. | Check the connection; use Python 3.13; run `setup.cmd` again. |
| **gcc is not on the PATH** / **no gcc on PATH; the ... kernel cannot be built** | The C compiler the team search needs is missing. | Windows: `winget install BrechtSanders.WinLibs.POSIX.UCRT` (or WinLibs from winlibs.com, its `mingw64\bin` on the PATH), open a new window, run `setup.cmd` again. Linux: `sudo apt install gcc` (or your distribution's), run `./setup.sh` again. |
| **.venv could not be created** (Linux) | Python's `venv` module is a separate package on Debian and Ubuntu. | `sudo apt install python3-venv`, delete the `.venv` folder, run `./setup.sh` again. |
| **libsqlcipher not found** (Linux) | The SQLCipher library that opens the game's master tables is missing. | `sudo apt install libsqlcipher1` (Fedora, Arch: `sqlcipher`), or set `HOLODREAMS_SQLCIPHER` to the library file. |
| **hololive Dreams is not installed where Steam keeps games** / **hololiveDreams install not found** | The game is in none of this PC's Steam libraries. | Install it from Steam, or set `HOLODREAMS_DIR` to the folder that holds `hololive-Dreams_Data`. |
| **no account file in ...** | The game has not written its account file on this PC yet. | Start the game once and get past its title screen, then run `refresh.cmd` again. |
| **N files ... look like an account file** | More than one account has played on this PC. | Set `HB_CREDENTIAL_FILE` to the file of the account you play (the most recently changed one, usually) and run `refresh.cmd` again. |
| **no literal opens the server's answer** | The game's build constants could not be found: the installed client is older than the one the server expects. | Update the game in Steam, start it once, run `refresh.cmd` again. |
| **THE GAME CLIENT'S BUILD CONSTANTS ARE NOT ON THIS PC** | `refresh_all.py` was run directly, before the constants were found. | Run `refresh.cmd` once, or `.venv\Scripts\python tools\refresh\game_keys.py --find`. See [game-keys.md](dev/game-keys.md). |
| **no region accepted this account** / **login rejected** | The account's server refused the login. | Start the game once (that refreshes the account file) and try again; set the region yourself with `refresh.cmd --region us` (or `as`, `jp`). |
| **GAME IN MAINTENANCE** | The game's servers refuse every call during maintenance. Nothing was written. | Refresh again when it ends. |
| **... refused: DATE_CHANGED** / **The game's day has changed** | The in-game day rolled over (05:00 JST, the game's own `resetTimingHour`) and the game has not been opened since; the server wants the client's daily reset first. Your cards, boards and collection were still read. | Open the game once, then refresh again. |
| **port 8731 is in use by another program** | Another program holds the port. | Nothing: `start` uses the next free port and says which. To choose one: `start.cmd --port 8740` (`./start.sh --port 8740`). |
| The pages are empty, or say there is no data | No refresh has run in this folder yet. | Run `refresh.cmd`, then reload the page. |
| Card art, jackets or figures missing after the first refresh | The pictures are still being fetched in the background. | Wait; progress is in `tools\recon\proxy\dumps\pictures_refresh.log`. |
| Scores look out of date | Results use your account as of the last refresh. | Run `refresh.cmd` or press **Refresh**. |
| **Your local changes to the following files would be overwritten by merge: data/cards.json** (`git pull`) | A refresh added the game's new cards to `data/cards.json`, and the new version changes that file too. | Run `update.cmd` (`./update.sh`) instead of `git pull`: it sets those cards aside, pulls, and the refresh adds back what the new version lacks. No `update.cmd` in the folder yet: `git checkout -- data/cards.json`, `git pull`, `setup.cmd`, `refresh.cmd` ([Updating](run-locally.md#updating)). |
| **These files have changes of your own** (`update`) | A tracked file was edited by hand, not by a refresh; an update never touches it. | `git stash`, run `update.cmd`, then `git stash pop`; or undo the change with `git checkout -- <file>`. |
| **This folder was not cloned with git** (`update`) | The folder came from a ZIP, or Git is not installed. | Download the newest ZIP, unpack it over this folder, then run `setup.cmd` and `refresh.cmd`. |
| **git could not update this folder** (`update`) | The folder has commits of its own that GitHub's version does not have. | Bring them in line with GitHub's version yourself (for example `git pull --rebase`), then run `update.cmd` again. |

## Reporting a problem

Open an issue on this repository with:

- what you did and what you expected,
- the message, word for word,
- the app version (Settings) and the last lines of `server.log`, or, when you run from source,
  the last lines of the script's window and the commit (`git log -1 --oneline`).

Never post your account file, access key or anything from the `state` folder: they are private.
