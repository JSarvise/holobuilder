"""`_payable_sockets` keeps only sockets whose Connect shape PAYS on the holder.

⛔ The wasted red socket: without this gate, `fill_boards` buys S-002 (a RED-region
socket) plus its ~10-point red corridor on EVERY team board — but red is
leader-scoped, so on a member the socket can never carry a value-adding card and the
corridor is dead points (6 red + S-002 + S-004 on each of 5 members, removable with
ZERO score change). The gate: a socket is payable only where its shape reaches a
scoring branch — R only on the leader, B on any team board, Y only on a singer/in
song mode, G (via the root S-001) always.

Reads only gitignored master data, so it skips on a fresh checkout.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PROXY = Path(__file__).resolve().parent
_DUMPS = _PROXY / "dumps"


def _load():
    spec = importlib.util.spec_from_file_location("tree_optimise", _PROXY / "tree_optimise.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["tree_optimise"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    not (_DUMPS / "skilltree.json").exists(),
    reason="dumps/ is gitignored account data",
)
def test_red_socket_is_payable_only_on_the_leader():
    to = _load()
    sk = to._load(_DUMPS / "skilltree.json")
    leader = "chr-04001"
    members = ["chr-00002", "chr-00016", "chr-00028", "chr-00031", "chr-00034"]
    scope = to._payable_sockets(sk, leader, members, song_mode=False, singer_chars=frozenset())

    # The root is always payable (its shape reaches green, which always scores).
    for cid, socks in scope.items():
        assert "S-001" in socks

    # S-002 is the RED-region socket: the leader keeps it, every member drops it.
    assert "S-002" in scope[leader], "the leader's red socket must stay"
    for m in members:
        assert "S-002" not in scope[m], f"{m}: a red socket is dead on a non-leader"
        # ...but the BLUE socket (S-003) stays, since blue scores on its own board.
        assert "S-003" in scope[m], f"{m}: the blue socket must stay on a member"

    # S-004 is the YELLOW-region socket: in GENERAL (song-agnostic) mode yellow does
    # not pay on any team board, so nobody buys it.
    for cid, socks in scope.items():
        assert "S-004" not in socks, f"{cid}: yellow socket is dead on a team board in general mode"


@pytest.mark.skipif(
    not (_DUMPS / "skilltree.json").exists(),
    reason="dumps/ is gitignored account data",
)
def test_yellow_socket_becomes_payable_for_a_singer_in_song_mode():
    to = _load()
    sk = to._load(_DUMPS / "skilltree.json")
    leader = "chr-04001"
    members = ["chr-00002", "chr-00016", "chr-00028", "chr-00031", "chr-00034"]
    # Make one member a singer: its yellow socket (S-004) now pays.
    singer = "chr-00002"
    scope = to._payable_sockets(sk, leader, members, song_mode=True,
                                singer_chars=frozenset({singer}))
    assert "S-004" in scope[singer], "a singer's yellow socket must be payable in song mode"
    # A non-singer member still does not get the yellow socket.
    assert "S-004" not in scope["chr-00016"]


@pytest.mark.skipif(
    not (_DUMPS / "skilltree.json").exists(),
    reason="dumps/ is gitignored account data",
)
def test_blue_socket_is_dead_on_a_leader_with_no_member_card():
    """Lui leading (outfit only, no Lui card among the members) must not get
    B-001..B-008 + S-003 bought -- S-003's shape covers only blues, and a blue reaches
    only the holder's own member cards, so on her it pays nothing."""
    to = _load()
    sk = to._load(_DUMPS / "skilltree.json")
    leader = "chr-00036"
    members = ["chr-00031", "chr-03001", "chr-00034", "chr-03003", "chr-03002"]
    scope = to._payable_sockets(sk, leader, members, song_mode=True,
                                singer_chars=frozenset({"chr-00006"}))
    assert "S-003" not in scope[leader]
    assert "S-002" in scope[leader], "her red socket still pays: she leads"
    for m in members:
        assert "S-003" in scope[m]
    # a leader who ALSO fields a member card keeps her blue socket
    scope = to._payable_sockets(sk, leader, [leader, *members[:4]], song_mode=True,
                                singer_chars=frozenset())
    assert "S-003" in scope[leader]
