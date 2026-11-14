#!/usr/bin/env bash
# Brings this folder to the newest holobuilder on GitHub, then refreshes
# (tools/local/run_local.py update).
#   ./update.sh                   git pull (safe for the cards a refresh added), ./setup.sh's
#                                 packages when they changed, then ./refresh.sh
#   ./update.sh --no-refresh      the code only; refresh later

cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "  [X] This folder is not set up yet: run ./setup.sh first."
  exit 1
fi
# exec: bash stops reading this file here, so a pull that changes it changes nothing mid-run
exec .venv/bin/python -u tools/local/run_local.py update "$@"
