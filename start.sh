#!/usr/bin/env bash
# Starts holobuilder on this PC (tools/local/run_local.py start) and opens it in the browser.
# The server runs in this terminal and prints what it does; press Ctrl+C to stop it.
#   ./start.sh                    http://127.0.0.1:8731/optimizer
#   ./start.sh --port 8740        another port
#   ./start.sh --no-browser       do not open the browser

cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "  [X] This folder is not set up yet: run ./setup.sh first."
  exit 1
fi
.venv/bin/python -u tools/local/run_local.py start "$@"
code=$?
echo
echo "  holobuilder stopped (exit code $code)."
exit $code
