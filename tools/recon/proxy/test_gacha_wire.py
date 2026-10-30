from __future__ import annotations
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gacha_wire as gw  # noqa: E402
import account_snapshot as snap  # noqa: E402


def _s(fn, s):  # length-delimited field
    return snap.pb_bytes_field(fn, s if isinstance(s, bytes) else s.encode())


def _v(fn, n):  # varint field
    return snap.write_varint((fn << 3) | 0) + snap.write_varint(n)


def test_decode_history_two_identical_lines_survive():
    h = _s(1, "Duo") + _s(2, "card-x") + _v(3, 1000)
    payload = _s(1, h) + _s(1, h)
    out = gw.decode(payload, gw.HIST_RESP)
    assert out == {"histories": [
        {"gacha_name": "Duo", "card_id": "card-x", "draw_time": 1000},
        {"gacha_name": "Duo", "card_id": "card-x", "draw_time": 1000}]}


def test_decode_list_flattens_groups_and_repeated_strings():
    g = _s(1, "gacha-1") + _s(3, "Name") + _s(11, "card-a") + _s(11, "card-b") + _v(6, 5)
    grp = _s(1, "grp") + _s(3, g)
    out = gw.decode(_s(1, grp), gw.LIST_RESP)
    banners = gw.live_banners(out)
    assert banners[0]["gacha_id"] == "gacha-1"
    assert banners[0]["pickup_card_ids"] == ["card-a", "card-b"]
    assert banners[0]["start_time"] == 5


def test_gacha_id_request_is_field_one_string():
    assert gw.gacha_id_request("abc") == b"\x0a\x03abc"


def test_widen_whitelist_adds_exactly_four_reads():
    before = set(snap.ALLOWED)
    gw.widen_whitelist()
    added = set(snap.ALLOWED) - before
    assert added <= {gw.LIST, gw.HISTORY, gw.NORMAL_PROB, gw.SELECT_PROB}
    assert "/rpc.api.Gacha/Read" not in snap.ALLOWED
    assert not any("Draw" in p for p in snap.ALLOWED)
