# Getting started with the app

This page is about the installer, `holobuilder-setup.exe`: the easy way, with the game data and
pictures served by the holobuilder server. To run holobuilder from this repository on your own
PC instead, with no server and no sign-in, see [Run it from source](run-locally.md).

## Install

1. Download **holobuilder-setup.exe** from [Releases](../../../releases/latest) and run it.
   Windows SmartScreen may warn that the file is not commonly downloaded: choose *More info → Run anyway*.
2. **Sign in with Google** and choose a **username** (3–20 characters). Your real name is not kept.
3. Wait for **approval**. Access is approved by hand; the window tells you when you can continue.
4. Choose a folder (default `%LOCALAPPDATA%\Programs\holobuilder`) and install. No administrator
   rights are needed. The installer adds a Desktop and a Start-menu shortcut and an entry in
   Windows' *Apps* list, and installs Microsoft WebView2 if the PC lacks it.

The installer always installs the **latest** version. You only need it once: the app updates itself.

**Checking the download (optional).** The release also has `SHA256SUMS` and `SHA256SUMS.sig`. The
quick check, in PowerShell: `Get-FileHash holobuilder-setup.exe` must print the hash listed in
`SHA256SUMS`. The full check also proves the list itself comes from us (it is signed with the
release key in this repository): `python tools/app/signing.py --verify-sums SHA256SUMS holobuilder-setup.exe`
from a copy of the source.

## First start

The start screen runs six steps and writes each one out as it happens:

| Step | What it does |
|---|---|
| This PC | Reads the PC's cores, memory and graphics card (to size the search, nothing is sent) |
| Update | Asks for the latest version; an update is installed before the app opens |
| Game data | Downloads the shared game data (charts, cards, art indexes) the first time, then only what changed |
| Client profile | Prepares the headers the game's own client sends, from this PC's hardware |
| Account | Logs into **your** game account and reads your cards, boards and items |
| Engine | Starts the local optimizer and opens the app |

### Settings

Open **Settings** before the first refresh:

- **Account file** — the game's own credential file on this PC (in the game's `LocalLow` folder).
  *Find* looks for it; *Browse* lets you pick it. It is read on every refresh and never copied or sent
  anywhere except to the game's own servers.
- **Region** — where your account lives: US, JP or Asia.
- **Steam ID** — leave empty to read it from Steam on this PC.
- **Search workers** — 0 uses every core (fastest).
- **Art cache** — how much card art and Live2D to keep on disk; 0 = unlimited.

## Refresh

**Refresh** (top right of the pages) logs into your account and reads your cards, levels, blooms,
Holomem Boards and items again. Run it after you pull cards, level up or change boards; results
always use the account as it was at the last refresh (the time is shown next to the button).

## Updates

Updates are installed automatically when you open the app, and an open app tells you when a new
version is out. Every release is signed; the app refuses anything that is not.

Next: [Using holobuilder](guide.md) · [Troubleshooting](troubleshooting.md#the-app) · [Privacy](privacy.md)
