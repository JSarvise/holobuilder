"""Which local copy of a master table `master_cache` opens, when the seeds have rotated.

No sqlcipher.dll and no network: `_decrypts` is the only thing that knows whether a body is
good, so the tests replace it with a set of "these files decrypt" and assert the CHOICE.

What they pin down: a `dumps/master_refetched/` body that decrypted when it was written stops
decrypting the moment the game ships a new master version (the seeds rotate with it). If it
still won over the game's own, current copy, the whole refresh_all cards/songs/charts chain
would fail with `prepare failed: SELECT ... sqlite_master`.
"""
import os
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import master_cache  # noqa: E402

TABLE = "Music"
INDEX = {TABLE: {"seed_hex": "ab" * 32, "size": 8192, "url": "https://example/master/x"}}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """A refetched dir and a game-cache dir, both holding a copy of `Music`."""
    refetched, game = tmp_path / "refetched", tmp_path / "game"
    refetched.mkdir()
    game.mkdir()
    ours = refetched / f"{TABLE}.db"
    theirs = game / "47DCBD834E669233D7EB8A51456ED217_FA738091C86F3A15E3274A32A297818F"
    ours.write_bytes(b"ours")
    theirs.write_bytes(b"theirs")
    monkeypatch.setattr(master_cache, "REFETCHED", str(refetched))
    monkeypatch.setattr(master_cache, "CACHE", str(game))
    monkeypatch.setattr(master_cache, "_PICKED", {})
    monkeypatch.setattr(master_cache, "_VERIFIED", set())
    return str(ours), str(theirs)


def _decrypting(monkeypatch, *good):
    good = set(good)
    monkeypatch.setattr(master_cache, "_decrypts", lambda path, seed: path in good)


def test_the_refetched_copy_wins_when_both_decrypt(cache, monkeypatch):
    ours, theirs = cache
    _decrypting(monkeypatch, ours, theirs)
    assert master_cache.cached_path(TABLE, INDEX) == ours


def test_a_stale_refetched_copy_loses_to_the_games_own(cache, monkeypatch):
    """The regression. Same size, same name, simply not decryptable any more."""
    ours, theirs = cache
    _decrypting(monkeypatch, theirs)
    assert master_cache.cached_path(TABLE, INDEX) == theirs


def test_the_loser_is_parked_so_it_is_never_probed_again(cache, monkeypatch):
    ours, theirs = cache
    _decrypting(monkeypatch, theirs)
    master_cache.cached_path(TABLE, INDEX)
    assert not os.path.exists(ours)
    assert os.path.exists(ours + ".stale")


def test_the_games_copy_is_kept_when_it_is_the_broken_one(cache, monkeypatch):
    """`Condition`: the reason dumps/master_refetched/ exists at all."""
    ours, theirs = cache
    _decrypting(monkeypatch, ours)
    assert master_cache.cached_path(TABLE, INDEX) == ours
    assert os.path.exists(ours)


def test_open_table_redownloads_when_nothing_local_decrypts(cache, monkeypatch):
    """Both copies stale -- the game has not pulled the new master either. The file the
    index names is fetched rather than the read failing."""
    ours, _theirs = cache
    _decrypting(monkeypatch)  # nothing decrypts
    healed = []

    def fake_self_heal(table, index):
        healed.append(table)
        return ours

    monkeypatch.setattr(master_cache, "_self_heal", fake_self_heal)
    monkeypatch.setattr(master_cache.shutil, "copy", lambda src, dst: None)
    monkeypatch.setattr(master_cache, "MasterDB", lambda path, seed: ("db", path))
    kind, path = master_cache.open_table(TABLE, INDEX)
    # a private copy per open (concurrent readers race on one shared name)
    assert kind == "db" and os.path.dirname(path) == tempfile.gettempdir()
    assert os.path.basename(path).startswith(f"hbmaster_{TABLE}_") and path.endswith(".db")
    assert healed == [TABLE]


def test_the_self_heal_is_off_when_the_run_must_not_touch_the_network(monkeypatch):
    monkeypatch.setenv("HB_MASTER_NO_FETCH", "1")
    assert master_cache._self_heal(TABLE, INDEX) is None


def test_a_pick_another_process_parked_is_picked_again(cache, monkeypatch):
    """A long-lived server picks the refetched copy; another process finds it stale and parks
    it as `.stale`. The server must pick again rather than keep handing out the vanished path
    (FileNotFoundError on every open until a restart)."""
    ours, theirs = cache
    _decrypting(monkeypatch, ours, theirs)
    assert master_cache.cached_path(TABLE, INDEX) == ours
    os.replace(ours, ours + ".stale")                     # the other process's park
    assert master_cache.cached_path(TABLE, INDEX) == theirs
