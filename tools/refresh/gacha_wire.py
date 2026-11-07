"""Gacha wire decoding + fire, shared by gacha_probe.py and gacha_sync.py.

READ-ONLY. Whitelist widened IN MEMORY ONLY with four exact literals checked against
gacha.gen.proto:
    List                      (Empty)                            -> banners currently offered
    ListHistory               (Empty)                            -> our draw history
    ListNormalProbability     (GachaListNormalProbabilityRequest{gacha_id})
    ListCardSelectProbability (GachaListCardSelectProbabilityRequest{gacha_id})
NOT added, deliberately:
    Gacha/Read      -- despite the name it returns only common_response and its effect is to
                       stamp `read_time` (the "seen" marker in List). It is a write.
    Gacha/Draw*     -- spend stones. Never.
    SetSelectedCard -- writes the card-select choice.
"""
from __future__ import annotations

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import account_snapshot as snap  # noqa: E402

LIST = "/rpc.api.Gacha/List"
HISTORY = "/rpc.api.Gacha/ListHistory"
NORMAL_PROB = "/rpc.api.Gacha/ListNormalProbability"
SELECT_PROB = "/rpc.api.Gacha/ListCardSelectProbability"


def widen_whitelist() -> None:
    """Four exact READ literals, in memory only. Gacha/Read is a write (stamps read_time);
    Draw* spend stones; SetSelectedCard writes. None of them, ever."""
    snap.ALLOWED = frozenset(set(snap.ALLOWED) | {LIST, HISTORY, NORMAL_PROB, SELECT_PROB})


# ---------------------------------------------------------------- schema-driven decoder
# (name, kind) where kind is "s" string, "i" int/bool/enum, or a nested schema dict.
REWARD = {1: ("type", "i"), 2: ("id", "s"), 3: ("quantity", "i")}
CONSUMPTION = {1: ("type", "i"), 2: ("id", "s"), 3: ("quantity", "i")}
BUTTON = {1: ("gacha_button_id", "s"), 2: ("name", "s"), 3: ("description", "s"),
          4: ("is_disabled", "i"), 5: ("consumptions", CONSUMPTION), 6: ("bonus_rewards", REWARD),
          7: ("limit_count", "i"), 8: ("reset_interval_type", "i"),
          9: ("total_reward_pick_count", "i"), 10: ("fixed_reward_pick_count", "i"),
          11: ("drawn_count", "i"), 12: ("priority", "i")}
CARD_SELECT = {1: ("selectable_card_ids", "s"), 2: ("selectable_card_quantity", "i"),
               3: ("selected_card_ids", "s")}
CARD_BONUS = {1: ("card_id", "s"), 2: ("rewards", REWARD)}
GACHA = {1: ("gacha_id", "s"), 2: ("type", "i"), 3: ("name", "s"), 4: ("is_locked", "i"),
         5: ("unlock_condition_group_id", "s"), 6: ("start_time", "i"), 7: ("end_time", "i"),
         8: ("detail_notice_id", "s"), 9: ("precaution", "s"), 10: ("icon_asset_id", "s"),
         11: ("pickup_card_ids", "s"), 12: ("promotion_pickup_card_ids", "s"),
         16: ("gacha_buttons", BUTTON), 17: ("gacha_point", {1: ("gacha_point_id", "s"), 2: ("quantity", "i")}),
         18: ("card_bonuses", CARD_BONUS), 19: ("card_select", CARD_SELECT),
         20: ("is_end_time_hidden", "i"), 24: ("read_time", "i"), 27: ("fixed_slot_name", "s"),
         28: ("promotion_text", "s"), 29: ("promotion_sub_text", "s"), 30: ("term_limited_hours", "i")}
GROUP = {1: ("gacha_group_id", "s"), 2: ("icon_asset_id", "s"), 3: ("gachas", GACHA)}
LIST_RESP = {1: ("gacha_groups", GROUP)}
HIST = {1: ("gacha_name", "s"), 2: ("card_id", "s"), 3: ("draw_time", "i")}
HIST_RESP = {1: ("histories", HIST)}
CARD_PROB = {1: ("card_id", "s"), 2: ("ppm", "i"), 3: ("is_rate_up", "i")}
RARITY_PROB = {1: ("rarity", "i"), 2: ("ppm", "i"), 3: ("cards", CARD_PROB)}
PROB_RESP = {1: ("normal", RARITY_PROB), 2: ("fixed", RARITY_PROB)}
REPEATED = {"gacha_groups", "gachas", "gacha_buttons", "consumptions", "bonus_rewards",
            "card_bonuses", "rewards", "pickup_card_ids", "promotion_pickup_card_ids",
            "selectable_card_ids", "selected_card_ids", "histories", "normal", "fixed", "cards"}


def decode(payload: bytes, schema: dict) -> dict:
    out: dict = {}
    for fn, wt, v in snap.fields(payload):
        if fn == 9999:
            continue
        name, kind = schema.get(fn, (f"f{fn}", "?"))
        if isinstance(kind, dict):
            val = decode(v, kind)
        elif kind == "s":
            val = v.decode("utf8", "replace")
        elif kind == "i":
            val = v
        else:
            val = v.hex() if isinstance(v, bytes) else v
        if name in REPEATED:
            out.setdefault(name, []).append(val)
        elif name in out:
            out[name] = (out[name] if isinstance(out[name], list) else [out[name]]) + [val]
        else:
            out[name] = val
    return out


def fire(host, path, req, token, ident, mver):
    # google.protobuf.Empty goes on the wire as an EMPTY frame with no AES envelope (User/Get
    # does the same); sealing b"" yields a padding-only block the server rejects.
    body = snap.frame(b"") if req == b"" else snap.frame(snap.seal(req))
    rh, tr, payload = snap.call(host, path, body,
                                token=token, ident=ident, master_version=mver)
    st = tr.get("grpc-status", rh.get("grpc-status"))
    if st not in (None, "0"):
        return None, f"grpc-status {st} {tr.get('grpc-message', '')}"
    for msg in snap.unframe(payload):
        if msg:
            return snap.unseal(msg), None
    return b"", None


def gacha_id_request(gid: str) -> bytes:
    return snap.pb_bytes_field(1, gid.encode())


def live_banners(list_resp: dict) -> list[dict]:
    return [g for grp in list_resp.get("gacha_groups", []) for g in grp.get("gachas", [])]
