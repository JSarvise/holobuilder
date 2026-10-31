# Contributing

Thanks for helping. Issues and pull requests are welcome.

## Setup

See [Run it from source](docs/run-locally.md): Python 3.13, `gcc` on the PATH (the C kernels in
`tools/web/kernel` are compiled on this PC), and `setup.cmd`, which makes the `.venv` and runs
`pip install -e . -r requirements.txt`. How the pieces fit: [ARCHITECTURE.md](ARCHITECTURE.md).

## Layout

| Folder | What it is |
|---|---|
| `src/hbuilder` | the optimizer: cards, gates, scoring, Holomem Boards, the searches. Standard library only. |
| `tools/web` | the local web app: `server.py` and the pages in `static/` |
| `tools/app` | the desktop app: launcher, installer, updater, settings, content store, server pieces |
| `tools/local`, `*.cmd` | running it from a clone: `setup.cmd`, `refresh.cmd`, `start.cmd` |
| `tools/recon/proxy` | reading the game: account refresh, master data, charts, card data |
| `tests`, `tools/*/test_*.py` | the tests |

## Rules that keep it trustworthy

- **Tests first for the model.** Gate resolution and score math are where correctness lives; a
  change there needs a test. Run `python -m pytest` before opening a pull request.
- **No precision loss for speed.** A refactor or speed-up must give the same results; anything that
  changes results is proposed on its own, with measurements before and after.
- **Comments explain the code as it is**: what it does and why, in present tense. A number that
  justifies a choice is welcome; dates, history and who decided what belong in the commit message.
  If something about the game is not known for sure, say so in words.
- **The local web app writes only the collection file**; downloads and caches belong in
  `tools/app/content_store.py` (a test checks this).
- **Never commit** anything from `state/`, account data, credentials, or the game's build constants
  (`docs/dev/game-keys.md`), and no game assets.

## License

By contributing you agree that your contribution is licensed under the [AGPL-3.0](LICENSE).
