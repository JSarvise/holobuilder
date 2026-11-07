"""master_json.Renderer: proto3 JSON exactly as the community tool holodori.best's exports write
it (no master needed)."""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import master_json as mj  # noqa: E402


def _v(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(fn: int, wt: int) -> bytes:
    return _v(fn << 3 | wt)


def _ld(fn: int, payload: bytes) -> bytes:
    return _tag(fn, 2) + _v(len(payload)) + payload


SCHEMA = {
    "messages": {
        "Row": [
            {"n": 1, "name": "id", "json": "id", "type": "string", "repeated": False, "pk": True},
            {"n": 2, "name": "start_time", "json": "startTime", "type": "int64", "repeated": False, "pk": False},
            {"n": 3, "name": "kind", "json": "kind", "type": "Kind", "repeated": False, "pk": False},
            {"n": 4, "name": "values", "json": "values", "type": "int32", "repeated": True, "pk": False},
            {"n": 5, "name": "delta", "json": "delta", "type": "int32", "repeated": False, "pk": False},
            {"n": 6, "name": "ratio", "json": "ratio", "type": "float", "repeated": False, "pk": False},
            {"n": 7, "name": "flag", "json": "flag", "type": "bool", "repeated": False, "pk": False},
            {"n": 8, "name": "costs", "json": "costs", "type": "Cost", "repeated": True, "pk": False},
            {"n": 9, "name": "names", "json": "names", "type": "string", "repeated": True, "pk": False},
        ],
        "Cost": [
            {"n": 1, "name": "resource_id", "json": "resourceId", "type": "string", "repeated": False, "pk": False},
            {"n": 3, "name": "quantity", "json": "quantity", "type": "int64", "repeated": False, "pk": False},
        ],
    },
    "enums": {"Kind": {"0": "KIND_UNKNOWN", "2": "KIND_FLICK"}},
}


def test_renders_like_holodori():
    rec = (_ld(1, b"m0001") + _tag(2, 0) + _v(1672498800000) + _tag(3, 0) + _v(2)
           + _ld(4, _v(5) + _v(300)) + _tag(5, 0) + _v(-7) + _tag(6, 5) + struct.pack("<f", 1.1)
           + _tag(7, 0) + _v(1) + _ld(8, _ld(1, b"item-gold") + _tag(3, 0) + _v(20))
           + _ld(8, _ld(1, b"item-x") + _tag(3, 0) + _v(1)) + _ld(9, b"a") + _ld(9, b"b"))
    out = mj.Renderer(SCHEMA).message("Row", rec)
    assert out == {
        "id": "m0001",
        "startTime": "1672498800000",            # int64 -> string
        "kind": "Kind_KIND_FLICK",                # enum -> <Type>_<VALUE>
        "values": [5, 300],                       # packed repeated int32
        "delta": -7,                              # negative int32 (10-byte varint)
        "ratio": 1.1,                             # float32, shortest round-trip decimal
        "flag": True,
        "costs": [{"resourceId": "item-gold", "quantity": "20"},
                  {"resourceId": "item-x", "quantity": "1"}],
        "names": ["a", "b"],
    }


def test_unknown_enum_value_is_the_number_and_unnamed_field_is_reported():
    r = mj.Renderer(SCHEMA)
    out = r.message("Row", _tag(3, 0) + _v(9) + _tag(40, 0) + _v(1))
    assert out == {"kind": 9}
    assert r.unnamed == {"Row": {40}}


def test_lang_tables_share_one_shape():
    schema = {"messages": {"Lang": [], "Music": []}, "enums": {}}
    assert mj.message_for("LangMusic_Eng", schema) == "Lang"
    assert mj.message_for("Music", schema) == "Music"
    assert mj.message_for("Nope", schema) is None


def test_compare_names_the_difference():
    a = [{"id": "x", "data": {"id": "x", "v": 1}}, {"id": "y", "data": {"id": "y"}}]
    b = [{"id": "x", "data": {"id": "x", "v": 2}}]
    verdict, detail = mj.compare(a, b)
    assert verdict == "DIFF" and "1 differ" in detail and "1 only ours" in detail
    assert mj.compare(a, a)[0] == "EXACT"
