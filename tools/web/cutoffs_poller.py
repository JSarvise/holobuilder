"""Event cutoffs: an OPTIONAL local poller that re-reads the ranking borders while the server runs.

Predictions need a time series of the borders. This is that, kept small and local:

* **On when the server starts** (otherwise no event would ever get a series): `server.main`
  enables it every DEFAULT_INTERVAL_MIN minutes, or
  `HBUILDER_CUTOFFS_POLL_MIN=<minutes>`; `=0` keeps it off. The Event page's toggle
  (`POST /api/event/cutoffs/poller`) switches it at runtime; that choice is not persisted.
* **Guests only.** Each cycle runs `event_cutoffs.py --region all` WITHOUT `--self`, so every
  region is read with its own disposable guest account (`state/ranking_accounts.json`) and the
  user's own account is never logged in by the poller (`build_args` is the one place the
  arguments are made, and a test pins that `--self` is not among them).
* **<= 1 request per second.** The script is sequential with 1 s between calls, and one cycle
  at a time: the poller shares the manual sync's lock, so a cycle is skipped (not queued) while
  a manual sync runs, and a manual sync gets its 409 while a cycle runs.
* **Backs off on errors.** A cycle that appends nothing because every region failed doubles the
  wait (interval x 2, x 4, ... capped at 60 min); one success resets it. A cycle that finds no
  open chapter waits 60 min: there is nothing to track between events.
* **Stops with the server.** `stop()` wakes the loop, terminates a running child and joins.

Cost: one cycle = Master/Get + Auth/Login + ListEventInfo + one Grade per region = 12 requests
(+1 Home/Login on the US guest's first read of a game day), ~12-15 s. At the default 5 min
that is ~144 requests/hour; at the 1-min floor ~720/hour. Gaps: nothing is read while the PC or
the server is off; the series simply has a hole there, which the pace model tolerates (it uses
the last read and the oldest read inside its window) and a template only needs a read within
an hour of the chapter end.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

DEFAULT_INTERVAL_MIN = 5
MIN_INTERVAL_MIN = 1
MAX_INTERVAL_MIN = 24 * 60
MAX_BACKOFF_MIN = 60
IDLE_INTERVAL_MIN = 60          # no chapter open: look again in an hour
REQUESTS_PER_CYCLE = 12         # 3 regions x (Master/Get, Auth/Login, ListEventInfo, Grade)


def build_args() -> list[str]:
    """The script arguments for one poll. GUESTS ONLY: never `--self` (the user's own account)."""
    return ["--region", "all"]


def parse_output(stdout: str) -> tuple[int | None, list[str]]:
    """(series points appended, failed regions) from event_cutoffs.py's own summary lines."""
    appended = None
    for line in stdout.splitlines():
        if line.startswith("cutoffs: "):
            try:
                appended = int(line.split()[1])
            except (IndexError, ValueError):
                appended = None
    failed = [line.split("]", 1)[0].lstrip("[") for line in stdout.splitlines()
              if line.startswith("[") and "] FAILED:" in line]
    return appended, failed


def clamp_interval(minutes) -> int:
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        raise ValueError("interval_min must be an integer number of minutes") from None
    if m < MIN_INTERVAL_MIN:
        raise ValueError(f"interval_min must be at least {MIN_INTERVAL_MIN}")
    return min(m, MAX_INTERVAL_MIN)


class CutoffsPoller:
    """A daemon thread calling `runner()` every `interval_min` minutes while enabled.

    `runner()` returns (returncode, stdout) or raises; it is injected so tests never spawn a
    process or touch the network. `lock` is the manual sync's lock (shared, never queued)."""

    def __init__(self, runner: Callable[[], tuple[int, str]], lock: threading.Lock, *,
                 interval_min: int = DEFAULT_INTERVAL_MIN, clock: Callable[[], float] = time.time):
        self._runner = runner
        self._lock = lock
        self._clock = clock
        self._mu = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.enabled = False
        self.interval_min = clamp_interval(interval_min)
        self.failures = 0           # consecutive all-failed cycles (drives the back-off)
        self.cycles = 0
        self.skipped = 0
        self.last_started: float | None = None
        self.last_finished: float | None = None
        self.last_ok: float | None = None
        self.last_appended: int | None = None
        self.last_failed_regions: list[str] = []
        self.last_error: str | None = None
        self.next_run: float | None = None

    # -------------------------------------------------------------- policy (pure)
    def next_delay_min(self) -> int:
        if self.failures:
            return min(self.interval_min * (2 ** self.failures), max(MAX_BACKOFF_MIN,
                                                                    self.interval_min))
        if self.last_appended == 0 and not self.last_failed_regions:
            return max(self.interval_min, IDLE_INTERVAL_MIN)
        return self.interval_min

    # -------------------------------------------------------------- one cycle
    def run_cycle(self) -> dict:
        """Run one poll now (in the caller's thread). Skipped while a manual sync holds the lock."""
        if not self._lock.acquire(blocking=False):
            self.skipped += 1
            return {"skipped": True}
        try:
            self.last_started = self._clock()
            try:
                code, out = self._runner()
                appended, failed = parse_output(out)
                err = None
                if code != 0 and not appended:
                    err = (f"exit {code}; failed: {', '.join(failed) or 'unknown'}")
            except Exception as exc:  # noqa: BLE001  a poller must survive any one cycle
                appended, failed, err = None, [], f"{type(exc).__name__}: {exc}"
            self.cycles += 1
            self.last_finished = self._clock()
            self.last_appended = appended
            self.last_failed_regions = failed
            if err:
                self.failures += 1
                self.last_error = err
            else:
                self.failures = 0
                self.last_ok = self.last_finished
                self.last_error = (f"partial: {', '.join(failed)} failed" if failed else None)
            return {"skipped": False, "appended": appended, "failed_regions": failed,
                    "error": err}
        finally:
            self._lock.release()

    # -------------------------------------------------------------- thread
    def _loop(self) -> None:
        while True:
            with self._mu:
                if not self.enabled:
                    self.next_run = None
                    return
            self.run_cycle()
            delay_s = self.next_delay_min() * 60
            self.next_run = self._clock() + delay_s
            self._wake.wait(delay_s)
            self._wake.clear()

    def configure(self, *, enabled: bool | None = None, interval_min=None) -> dict:
        if interval_min is not None:
            self.interval_min = clamp_interval(interval_min)
        if enabled is True:
            self.start()
        elif enabled is False:
            self.stop(wait=False)
        elif interval_min is not None and self.enabled:
            self._wake.set()        # a shorter interval applies now, not after the old wait
        return self.status()

    def start(self) -> None:
        with self._mu:
            self.enabled = True
            if self._thread and self._thread.is_alive():
                return
            self.failures = 0
            self._wake.clear()
            self._thread = threading.Thread(target=self._loop, name="cutoffs-poller",
                                            daemon=True)
            self._thread.start()

    def stop(self, *, wait: bool = True, timeout: float = 20.0) -> None:
        with self._mu:
            self.enabled = False
            thread = self._thread
        self._wake.set()
        stop_child = getattr(self._runner, "stop", None)
        if callable(stop_child):
            stop_child()
        if wait and thread and thread is not threading.current_thread():
            thread.join(timeout)

    def status(self) -> dict:
        running = bool(self._thread and self._thread.is_alive())
        return {
            "enabled": self.enabled,
            "running": running,
            "interval_min": self.interval_min,
            "min_interval_min": MIN_INTERVAL_MIN,
            "next_delay_min": self.next_delay_min(),
            "backing_off": self.failures > 0,
            "consecutive_failures": self.failures,
            "cycles": self.cycles,
            "skipped": self.skipped,
            "last_started": _ms(self.last_started),
            "last_finished": _ms(self.last_finished),
            "last_ok": _ms(self.last_ok),
            "last_appended": self.last_appended,
            "last_failed_regions": self.last_failed_regions,
            "last_error": self.last_error,
            "next_run": _ms(self.next_run) if self.enabled else None,
            "requests_per_hour": round(REQUESTS_PER_CYCLE * 60 / self.interval_min),
            "accounts": "guests",
        }


def _ms(t: float | None) -> int | None:
    return None if t is None else int(t * 1000)


class ScriptRunner:
    """Runs event_cutoffs.py once as a child process (the server's own interpreter, like the
    manual sync); `stop()` terminates a child still running when the server shuts down."""

    def __init__(self, script: Path, cwd: Path, *, timeout_s: float = 600.0):
        self.script, self.cwd, self.timeout_s = script, cwd, timeout_s
        self._proc: subprocess.Popen | None = None

    def __call__(self) -> tuple[int, str]:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self._proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-u", str(self.script), *build_args()], cwd=str(self.cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace")
        try:
            out, _ = self._proc.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            out, _ = self._proc.communicate()
            return 124, (out or "") + "\ncutoffs poller: child timed out\n"
        finally:
            code = self._proc.returncode
            self._proc = None
        for line in (out or "").splitlines():
            if line.startswith(("[", "cutoffs:")):
                print(f"  [cutoffs-poll] {line.encode('ascii', 'replace').decode('ascii')}",
                      flush=True)
        return code, out or ""

    def stop(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
