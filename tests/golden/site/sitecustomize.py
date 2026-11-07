"""Process setup for golden runs, loaded by every Python process the golden server starts.

It only acts when the golden runner sets its variables, so it is inert anywhere else:

* ``HB_GOLDEN_CLOCK=<unix seconds>``: the wall clock starts at that instant and advances in
  real time from process start, so date-dependent views (the open event chapter) see the same
  day on every run while timeouts still elapse.
* ``HB_GOLDEN_AUDIT=<log path>``: every file open and directory listing is appended to
  ``<log path>.<pid>``,
  so the runner can check that a run read only the frozen snapshot.
* ``HB_GOLDEN_OFFLINE=1``: any network connection other than loopback raises, so a golden run
  can never reach the game's servers or the CDN.
"""
from __future__ import annotations

import os
import sys


def _install_clock(start: float) -> None:
    import datetime as _dt
    import time as _time

    real_time, real_perf = _time.time, _time.perf_counter
    origin = real_perf()

    def fake_time() -> float:
        return start + (real_perf() - origin)

    def fake_time_ns() -> int:
        return int(fake_time() * 1e9)

    real_localtime, real_gmtime, real_ctime = _time.localtime, _time.gmtime, _time.ctime
    real_strftime = _time.strftime

    _time.time = fake_time
    _time.time_ns = fake_time_ns
    _time.localtime = lambda secs=None: real_localtime(fake_time() if secs is None else secs)
    _time.gmtime = lambda secs=None: real_gmtime(fake_time() if secs is None else secs)
    _time.ctime = lambda secs=None: real_ctime(fake_time() if secs is None else secs)
    _time.strftime = lambda fmt, t=None: real_strftime(fmt, real_localtime(fake_time()) if t is None else t)

    real_datetime, real_date = _dt.datetime, _dt.date

    class datetime(real_datetime):  # noqa: N801 - stands in for the stdlib class
        @classmethod
        def now(cls, tz=None):
            return real_datetime.fromtimestamp(fake_time(), tz)

        @classmethod
        def utcnow(cls):
            return real_datetime.fromtimestamp(fake_time(), _dt.timezone.utc).replace(tzinfo=None)

        @classmethod
        def today(cls):
            return real_datetime.fromtimestamp(fake_time())

    class date(real_date):  # noqa: N801
        @classmethod
        def today(cls):
            return real_date.fromtimestamp(fake_time())

    _dt.datetime = datetime
    _dt.date = date
    del real_time


def _install_audit(log_path: str, offline: bool) -> None:
    import socket

    pid = os.getpid()
    # one file per process: concurrent appends to one file interleave mid-line
    log = open(f"{log_path}.{pid}", "a", encoding="utf-8", buffering=1)  # noqa: SIM115

    def hook(event: str, args: tuple) -> None:
        if event == "open":
            path = args[0]
            if isinstance(path, (str, bytes, os.PathLike)) and not isinstance(path, int):
                mode = args[1] if len(args) > 1 else ""
                log.write(f"{pid}\topen\t{mode}\t{os.fsdecode(path)}\n")
        elif event in ("os.listdir", "os.scandir"):
            path = args[0] if args else "."
            if isinstance(path, (str, bytes, os.PathLike)):
                log.write(f"{pid}\tlist\t\t{os.fsdecode(path)}\n")
        elif offline and event == "socket.connect":
            address = args[1]
            host = address[0] if isinstance(address, tuple) else address
            if host not in ("127.0.0.1", "::1", "localhost"):
                log.write(f"{pid}\tBLOCKED\t\t{address!r}\n")
                raise ConnectionRefusedError(f"golden run is offline: refused {address!r}")
        elif offline and event == "socket.getaddrinfo":
            host = args[0]
            if host not in (None, "127.0.0.1", "::1", "localhost", b"127.0.0.1", b"localhost"):
                log.write(f"{pid}\tBLOCKED\t\t{host!r}\n")
                raise socket.gaierror(f"golden run is offline: refused {host!r}")

    import time as _time

    log.write(f"{pid}\tstart\t\t{os.environ.get('HB_GOLDEN_CLOCK', '')}\t{_time.time():.0f}\n")
    sys.addaudithook(hook)


_clock = os.environ.get("HB_GOLDEN_CLOCK")
if _clock:
    _install_clock(float(_clock))
_audit = os.environ.get("HB_GOLDEN_AUDIT")
if _audit:
    _install_audit(_audit, os.environ.get("HB_GOLDEN_OFFLINE") == "1")
