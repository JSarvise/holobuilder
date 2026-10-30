"""live2d_build's pure parts: a Unity streamed cubic key becomes an EXACT motion3 Bezier
segment, and a motion3's point / segment counts match what the runtime expects.
    python -m pytest tools/recon/proxy/test_live2d_build.py
"""
from __future__ import annotations

import live2d_build as lb


def _bezier(p0, p1, p2, p3, u):
    v = 1 - u
    return v ** 3 * p0 + 3 * v * v * u * p1 + 3 * v * u * u * p2 + u ** 3 * p3


def test_a_cubic_key_is_an_exact_bezier_segment():
    # value(x) = ((a x + b) x + c) x + d on [t0, t1], as Unity stores a streamed key
    a, b, c, d = 0.8, -1.3, 2.0, 0.25
    t0, t1 = 0.5, 2.0
    h = t1 - t0
    end = ((a * h + b) * h + c) * h + d
    seg, n = lb.curve_segments([(t0, a, b, c, d), (t1, 0, 0, 0, end)], t1)
    assert n == 1 and seg[2] == 1                       # one Bezier segment
    _t0, v0, _k, x1, y1, x2, y2, x3, y3 = seg[:9]
    for u in (0.0, 0.25, 0.5, 0.9, 1.0):
        x = _bezier(t0, x1, x2, x3, u)                  # time is linear in u (thirds)
        want = ((a * (x - t0) + b) * (x - t0) + c) * (x - t0) + d
        assert abs(_bezier(v0, y1, y2, y3, u) - want) < 1e-9


def test_flat_and_linear_keys_and_the_held_tail():
    seg, n = lb.curve_segments([(0.0, 0, 0, 0, 1.0), (1.0, 0, 0, 0, 1.0)], 3.0)
    assert seg == [0.0, 1.0, 0, 1.0, 1.0, 0, 3.0, 1.0] and n == 2
    seg, _n = lb.curve_segments([(0.0, 0, 0, 0, 1.0), (1.0, 0, 0, 0, 2.0)], 1.0)
    assert seg[2] == 2                                  # a value jump is a stepped segment


def test_the_dummy_key_at_minus_infinity_is_dropped():
    seg, n = lb.curve_segments([(float("-inf"), 0, 0, 0, 9.0), (0.0, 0, 0, 0, 1.0),
                                (1.0, 0, 0, 0, 1.0)], 1.0)
    assert seg[:2] == [0.0, 1.0] and n == 1
