# Troubleshooting

Find the message you see, then the fix.

- [The app](#the-app) (installed with `holobuilder-setup.exe`)
- [Running it yourself](#running-it-yourself) (`setup.cmd`, `refresh.cmd`, `start.cmd`)

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

## Running it yourself

Everything prints to the script's own window; copy the last lines from there when you report a
problem. `setup.cmd` can be run again at any time to see what is missing.

| Message | What it means | What to do |
|---|---|---|
| **Python 3.11 or newer was not found** (`setup.cmd`) | No usable Python on this PC. The `python` that opens the Microsoft Store does not count. | Install Python 3.13 from python.org with *Add python.exe to PATH* ticked, open a new window, run `setup.cmd` again. |
| **The packages did not install** | `pip` failed: no internet, or a package has no ready-made build for this Python version. | Check the connection; use Python 3.13; run `setup.cmd` again. |
| **gcc is not on the PATH** / **no gcc on PATH; the ... kernel cannot be built** | The C compiler the team search needs is missing. | `winget install BrechtSanders.WinLibs.POSIX.UCRT` (or WinLibs from winlibs.com, its `mingw64\bin` on the PATH), open a new window, run `setup.cmd` again. |
| **hololive Dreams is not installed where Steam keeps games** / **hololiveDreams install not found** | The game is in none of this PC's Steam libraries. | Install it from Steam, or set `HOLODREAMS_DIR` to the folder that holds `hololive-Dreams_Data`. |
| **no account file in ...** | The game has not written its account file on this PC yet. | Start the game once and get past its title screen, then run `refresh.cmd` again. |
| **N files ... look like an account file** | More than one account has played on this PC. | Set `HB_CREDENTIAL_FILE` to the file of the account you play (the most recently changed one, usually) and run `refresh.cmd` again. |
| **no literal opens the server's answer** | The game's build constants could not be found: the installed client is older than the one the server expects. | Update the game in Steam, start it once, run `refresh.cmd` again. |
| **THE GAME CLIENT'S BUILD CONSTANTS ARE NOT ON THIS PC** | `refresh_all.py` was run directly, before the constants were found. | Run `refresh.cmd` once, or `.venv\Scripts\python tools\recon\proxy\game_keys.py --find`. See [game-keys.md](dev/game-keys.md). |
| **no region accepted this account** / **login rejected** | The account's server refused the login. | Start the game once (that refreshes the account file) and try again; set the region yourself with `refresh.cmd --region us` (or `as`, `jp`). |
| **GAME IN MAINTENANCE** | The game's servers refuse every call during maintenance. Nothing was written. | Refresh again when it ends. |
| **... refused: DATE_CHANGED** / **The game's day has changed** | The in-game day rolled over (04:00 JST) and the game has not been opened since; the server wants the client's daily reset first. Your cards, boards and collection were still read. | Open the game once, then refresh again. |
| **port 8731 is in use by PID ...** | Another program holds the port. | Nothing: `start.cmd` uses the next free port and says which. To choose one: `start.cmd --port 8740`. |
| The pages are empty, or say there is no data | No refresh has run in this folder yet. | Run `refresh.cmd`, then reload the page. |
| Card art, jackets or figures missing after the first refresh | The pictures are still being fetched in the background. | Wait; progress is in `tools\recon\proxy\dumps\pictures_refresh.log`. |
| Scores look out of date | Results use your account as of the last refresh. | Run `refresh.cmd` or press **Refresh**. |

## Reporting a problem

Open an issue on this repository with:

- what you did and what you expected,
- the message, word for word,
- the app version (Settings) and the last lines of `server.log`, or, when you run it yourself,
  the last lines of the script's window and the commit (`git log -1 --oneline`).

Never post your account file, access key or anything from the `state` folder: they are private.
