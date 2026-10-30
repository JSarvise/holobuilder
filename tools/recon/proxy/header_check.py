"""Check a capture of the game client against what account_snapshot sends.

    proxyenv/Scripts/python header_check.py dumps/headers_capture.mitm [more.mitm ...]

For every rpc request of the real client in the capture, rebuild the same call with
`account_snapshot._request_headers` -- the client's own identity headers from that request, its
token / master version present or not as they were -- and compare the header NAMES in order,
plus the fixed wire values. Prints one line per rpc and a verdict. Identity values (Steam ID,
device, CPU, GPU, token) are never printed.
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_snapshot as A  # noqa: E402
from mitmproxy.io import FlowReader  # noqa: E402

PSEUDO = 4
TRANSPORT = {"te", "grpc-accept-encoding", "grpc-timeout", "content-type"}
ADDED = {"x-app-request-id", "x-app-master-version", "x-app-auth-token"}
SHOWN = {"te", "grpc-accept-encoding", "grpc-timeout", "content-type", "user-agent",
         "x-app-version", "x-app-lang-type", "x-app-os-type", "x-app-store-type",
         "x-app-bundle-id", "x-app-device-type", "x-app-operating-system-family",
         "x-app-graphics-device-type", "x-app-graphics-device-version", "x-app-graphics-shader-level"}


def requests(paths):
    for f in paths:
        with open(f, "rb") as fh:
            for fl in FlowReader(fh).stream():
                r = getattr(fl, "request", None)
                if r is not None and "game-hololive-dreams" in r.pretty_host and r.path.startswith("/rpc."):
                    yield r


def main(argv: list[str]) -> int:
    A.app_version = lambda: None                       # compare against the capture's own version
    per = collections.defaultdict(collections.Counter)
    values = collections.defaultdict(set)
    bad = 0
    n = 0
    for r in requests(argv):
        n += 1
        real = [(k.decode().lower(), v.decode()) for k, v in r.headers.fields]
        names = [k for k, _ in real]
        ident = [(k, v) for k, v in real if k not in TRANSPORT | ADDED]
        got = dict(real)
        ours = [k for k, _ in A._request_headers(
            r.pretty_host, r.path, token=got.get("x-app-auth-token"), ident=ident,
            master_version=got.get("x-app-master-version"))][PSEUDO:]
        wire = dict(A.WIRE_HEADERS)
        same = ours == names and all(got.get(k) == v for k, v in wire.items())
        if not same:
            bad += 1
            extra = [k for k in ours if k not in names]
            missing = [k for k in names if k not in ours]
            note = f"ours adds {extra} lacks {missing}" if extra or missing else "same names, other ORDER"
            diff_wire = {k: got.get(k) for k, v in wire.items() if got.get(k) != v}
            per[r.path][f"DIFF {note} {diff_wire or ''}".strip()] += 1
        else:
            per[r.path]["same"] += 1
        for k, v in real:
            if k in SHOWN:
                values[k].add(v)
    for path, c in sorted(per.items()):
        print(f"{path.replace('/rpc.api.', ''):48s} {dict(c)}")
    print()
    for k in sorted(values):
        print(f"  {k}: {sorted(values[k])}")
    print(f"\n{n} requests, {n - bad} identical to ours, {bad} different")
    return 1 if bad or not n else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
