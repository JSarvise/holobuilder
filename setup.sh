#!/usr/bin/env bash
# Prepares this folder to run holobuilder on this PC (tools/local/run_local.py setup): creates
# .venv, installs the packages from requirements.txt, then checks what else the local route
# needs (the C compiler, the game install, the game's account file). Safe to run again.

cd "$(dirname "$0")" || exit 1

# Python 3.12 or newer, 3.13 first: the version the project is tested on.
for py in python3.13 python3.12 python3.14 python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
    exec "$py" -u tools/local/run_local.py setup
  fi
done
# only an older Python: name its version, so the message says why it was not used
old=""
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1; then
    old=$("$py" -c 'import platform; print(platform.python_version())' 2>/dev/null) && break
  fi
done
echo
if [ -n "$old" ]; then
  echo "  [X] Python $old is too old: holobuilder needs Python 3.12 or newer."
else
  echo "  [X] Python 3.12 or newer was not found."
fi
echo "      Install Python 3.13 (or 3.12+) from your distribution, then run ./setup.sh again."
exit 1
