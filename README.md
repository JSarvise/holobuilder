<div align="center">

<img src="docs/nene.png" width="88" alt="">

# holobuilder

**Team optimizer for hololive Dreams.**

Find the best teams for every song, plan your Holomem Boards and keep an eye on the event rankings.

[**Download for Windows**](../../releases/latest) · [**Run it yourself**](docs/run-locally.md)

<br>

<img src="docs/optimizer.webp" alt="holobuilder">

</div>

## Features

| | |
|---|---|
| <img src="docs/optimizer.webp" alt=""><br>**Team optimizer**<br>The best leader and members for any song, using your own cards. | <img src="docs/unitscore.webp" alt=""><br>**Score breakdown**<br>Unit Score and score distribution, line by line. |
| <img src="docs/boardplan.webp" alt=""><br>**Board planner**<br>Which Holomem Board nodes to unlock next. | <img src="docs/holoboard.webp" alt=""><br>**Holomem Boards**<br>Every board and its points at a glance. |
| <img src="docs/event.webp" alt=""><br>**Event tracker**<br>Live borders, forecasts and the top 100. | <img src="docs/gacha.webp" alt=""><br>**Gacha tracker**<br>Your pulls compared with the official rates. |

## Two ways to use it

| | **Use the app** | **Run it yourself** |
|---|---|---|
| What you install | `holobuilder-setup.exe` | this repository, Python and a C compiler |
| The game on the same PC | not needed | needed: the data is built from your own install |
| Access | sign in with Google; access is approved by hand | nothing to ask for: no server, no sign-in |
| Game data and pictures | downloaded from the holobuilder server | built on your PC, from your game and the game's own servers |
| Updates | automatic | `git pull`, then `refresh.cmd` |
| Your account | read on your PC, straight from the game | the same |

Both run the same optimizer on your PC, with your own cards.

### Use the app

1. Download `holobuilder-setup.exe` from [Releases](../../releases/latest) and run it.
2. Sign in with Google. Access is approved by hand; the installer continues by itself once you're in.
3. Open holobuilder and point it at your game's account file (it usually finds it for you).

The app updates itself when a new version is out. More: [Getting started with the app](docs/getting-started.md).

### Run it yourself

Everything on your own PC, without the holobuilder server and without anyone's approval. You need
Windows, hololive Dreams installed and started at least once, [Python 3.13](https://www.python.org/downloads/windows/)
and `gcc` ([WinLibs](https://winlibs.com/): `winget install BrechtSanders.WinLibs.POSIX.UCRT`).

```
git clone https://github.com/JSarvise/holobuilder
cd holobuilder
.\setup.cmd
.\refresh.cmd
.\start.cmd
```

`setup.cmd` creates the Python environment and says what is missing; `refresh.cmd` reads the game
and your account (the first time takes a while); `start.cmd` opens
<http://127.0.0.1:8731/optimizer>. Each one can also be double-clicked, and the repository's ZIP
(the green *Code* button) works as well as a clone. Step by step: [Run it yourself](docs/run-locally.md).

## Privacy

- Your game account file stays on your PC: it is read in place and used only to log into the game's own servers, as the game itself does.
- Your account data is read straight from the game, never through our server.
- **The app:** our server keeps only your Google email, the username you choose (not your real
  name) and which app version you use. We store no IP addresses; requests pass through Cloudflare.
- **Running it yourself:** nothing is sent to us at all.

Details: [Privacy](docs/privacy.md).

<br>

<sub>Fan-made and unofficial. Not affiliated with COVER Corporation or QualiArts. hololive, hololive Dreams and all related assets belong to their respective owners.</sub>

## Documentation

**Use the app**

- [Getting started](docs/getting-started.md): install, first start, settings, refresh, updates

**Run it yourself**

- [Run it yourself](docs/run-locally.md): setup, the first refresh, starting it, updating
- [The game's build constants](docs/dev/game-keys.md): what the refresh needs from your game install
- [Your own server](docs/self-hosting.md): for building and serving your own installer

**Both**

- [Using holobuilder](docs/guide.md): every page and option ([options glossary](docs/options.md))
- [Troubleshooting](docs/troubleshooting.md): messages and what to do
- [Privacy](docs/privacy.md): what stays on your PC, what goes where
- [Contributing](CONTRIBUTING.md) · [Architecture](ARCHITECTURE.md) · [Security](SECURITY.md)

## Source code

This repository holds all of holobuilder: the optimizer (`src/hbuilder`), the local web app
(`tools/web`), the scripts that read the game's data and your account (`tools/recon`), the
local launchers (`setup.cmd`, `refresh.cmd`, `start.cmd`, `tools/local`), and the desktop app
with its installer and server (`tools/app`).

```
.\setup.cmd
.venv\Scripts\python -m pytest
```

Tests that need the game's data are skipped, with the reason, until a refresh has built it.

The game's own build constants are not in this repository: the refresh finds them in your game
install ([how](docs/dev/game-keys.md)).

## License

[GNU AGPL-3.0](LICENSE). You may use, study, change and share this code. If you distribute it, or
run a modified version as a service for others, you must publish your changes under the same
license and keep the copyright and attribution notices.

## Disclaimer

holobuilder is an **unofficial fan-made tool**. It is not affiliated with, endorsed or sponsored by
COVER Corporation, QualiArts or hololive production. hololive and hololive Dreams are trademarks
of their respective owners; all game assets remain theirs and are not included in this repository.
Using third-party tools may go against the game's terms of service; use it at your own risk.
See [NOTICE](NOTICE).
