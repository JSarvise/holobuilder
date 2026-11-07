"""Account-side scripts the routes run as child processes (`refresh_all.py`,
`gacha_sync.py`, `gacha_export.py`). Each owns its own writes and data-authority rules;
the server only starts it, echoes its output and reports its exit code.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hbweb import config


def run_recon_script(h, script: Path, tag: str,
                     args: list[str] | None = None) -> tuple[int, str] | None:
    """Run a recon-side script as a subprocess, echoing each line of its output to this
    server's console AS IT ARRIVES (the operator watches the console; a captured pipe
    would leave it blank for minutes) and collecting it for the reply.

    Returns `(returncode, stdout)`, or `None` having ALREADY answered 500 when the
    script is missing or will not start -- the two failures where there is no exit
    code to report and the caller has nothing left to say.
    """
    if not script.is_file():
        h.reply_json(500, {"error": f"missing {script}"})
        return None
    print(f"\n--- {script.name} starting ---", flush=True)
    captured: list[str] = []
    # PYTHONIOENCODING: the pipe is READ as utf-8, but a Windows child defaults its
    # stdout to the console codepage, so a non-ASCII character in a step's output or in
    # a path it reports would not survive the round trip.
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        process = subprocess.Popen(  # noqa: S603
            [sys.executable, "-u", str(script), *(args or [])], cwd=str(config.REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace")
    except OSError as exc:
        h.reply_json(500, {"error": f"could not start {script.name}: {exc}"})
        return None
    assert process.stdout is not None
    for line in process.stdout:
        try:
            print(f"  [{tag}] {line.rstrip()}", flush=True)
        except UnicodeEncodeError:
            # This server's OWN console may be cp1252. Echoing is a convenience;
            # letting it raise would abort the request and lose the exit code.
            print(f"  [{tag}] {line.rstrip().encode('ascii', 'replace').decode('ascii')}",
                  flush=True)
        captured.append(line)
    code = process.wait()
    print(f"--- {script.name} finished, exit {code} ---\n", flush=True)
    return code, "".join(captured)
