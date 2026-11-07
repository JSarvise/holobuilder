#!/usr/bin/env bash
# Reads the game and your account into this folder (tools/local/run_local.py refresh).
#   ./refresh.sh                  the account's region is found by itself the first time
#   ./refresh.sh --region as      us, as or jp: the server your account lives on
#   ./refresh.sh --skip chibi     any other option goes to tools/refresh/refresh_all.py

cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "  [X] This folder is not set up yet: run ./setup.sh first."
  exit 1
fi
exec .venv/bin/python -u tools/local/run_local.py refresh "$@"
