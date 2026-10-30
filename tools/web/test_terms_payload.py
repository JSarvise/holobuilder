"""The score-term panel must reach the browser for EVERY rank.

`build_score_terms` is unit-tested in `tests/test_score_terms_panel.py`. This
file guards the wiring instead: that `run_solve_job` actually puts the terms in
the payload, under the key `ranking.js` reads, for every rank and not only the
one `--explain` names. Otherwise data could reach the payload for a single
rank while the page renders N teams, and N-1 of them would show nothing.

It also pins the boundary the server is responsible for and the formatter is
not: `board_modelled` comes from `cfg.board is not None`, so "the board
contributed nothing" stays distinguishable from "no board was loaded".

Fast by construction: the pool is six synthetic cards, so the solve is
milliseconds rather than the ~10 s of the real pool.

⚠ **Why the pool is built in memory and no file is named here.** Two obvious
substitutions are both rejected by `test_no_writes.py`, and it is right both
times: `monkeypatch.setattr(server, "CARDS_PATH", fixtures / "cards.json")`
sends a protected path into a sink that guard cannot prove is a read, and
aliasing `load_inputs` to smuggle the path past the sink whitelist only defeats
the check by renaming. Either would be evasion of a guard this project put
there deliberately, to make a test go green. So this file names no protected
path at all: the cards are constructed with the same `conftest` helpers the
domain tests use, and `data/` is never involved.

    python -m pytest tools/web/test_terms_payload.py
"""

from __future__ import annotations

import dataclasses
import queue
import sys
from pathlib import Path

import pytest

import server as web_server
from hbuilder.domain.collection import AccountBonuses

# `conftest.py`'s card builders, so the pool here is made the same way every
# domain test makes one.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from conftest import make_card  # noqa: E402


@pytest.fixture
def payload(monkeypatch, tmp_path):
    """The payload `run_solve_job` emits, over a six-card synthetic pool."""
    pool = tuple(
        make_card(f"card_{index}", member=f"Holomem{index}") for index in range(6)
    )
    from conftest import make_scoring_config

    config = make_scoring_config()

    def _synthetic_inputs(*_args, **_kwargs):
        # (cards, owned, cfg, provenance) -- `load_inputs`' contract. Every
        # card is owned, and provenance is None (the maxed mode-A default).
        return pool, pool, config, None

    monkeypatch.setattr(web_server, "load_inputs", _synthetic_inputs)
    # No board: `load_board_basis` returns the explicit "no board" state,
    # which is what makes the not-modelled assertions below meaningful rather
    # than accidental.
    monkeypatch.setattr(web_server, "BOARD_DISABLED", True)
    # And no collection, for the same reason plus determinism: the solve path
    # applies `state/collection.json`, so leaving it live would make these
    # assertions depend on whatever was last typed into the editor. An
    # absent path is a first-class state (`apply_collection`'s own contract),
    # not an error.
    monkeypatch.setattr(
        web_server, "COLLECTION_PATH", tmp_path / "no-such-collection.json"
    )

    job = web_server.Job(job_id="test")
    web_server.run_solve_job(job, top_n=3, art_dir=Path("does-not-exist"))

    events = []
    while True:
        try:
            events.append(job.events.get_nowait())
        except queue.Empty:
            break

    done = [e for e in events if e.get("type") == "result"]
    assert done, f"no result event; got {[e.get('type') for e in events]}"
    return done[-1]["payload"]


def test_every_rank_carries_its_terms(payload):
    """Not just rank 1. The page renders N teams and a reader comparing two
    of them needs the panel on both."""
    blocks = payload["explain_by_rank"]
    assert blocks, "explain_by_rank is empty"
    assert len(blocks) == len(payload["teams"])
    for block in blocks:
        assert "terms" in block, f"rank {block['rank']} has no terms"
        assert block["terms"]["power"], f"rank {block['rank']} has no power rows"
        assert block["terms"]["bonus"], f"rank {block['rank']} has no bonus rows"


def test_every_explain_entry_names_its_roster(payload):
    """A client that re-orders (develop) or re-composes (refine) teams finds
    the breakdown by ROSTER, never by stage-1 rank number — otherwise a
    developed #1 would show another team's breakdown."""
    by_rank = {t["rank"]: t for t in payload["teams"]}
    for block in payload["explain_by_rank"]:
        team = by_rank[block["rank"]]
        assert block["roster"] == [team["leader_id"], *team["member_ids"]]


def test_the_terms_rebuild_each_teams_score(payload):
    """The identity, end to end: what the panel shows must recombine into the
    score printed beside it. A panel that explains a different number than the
    one on screen is worse than no panel."""
    by_rank = {t["rank"]: t for t in payload["teams"]}
    for block in payload["explain_by_rank"]:
        terms = block["terms"]
        powers = [r["value"] for r in terms["power"] if r["value"] is not None]
        assert sum(powers) == pytest.approx(terms["overall_power"])

        factor = 1.0
        for row in terms["bonus"]:
            factor *= 1.0 + (row["value"] or 0.0) / 1000.0
        rebuilt = terms["overall_power"] * factor

        assert rebuilt == pytest.approx(terms["total_score"])
        # ... and that total is the score the ranking itself shows.
        assert terms["total_score"] == pytest.approx(by_rank[block["rank"]]["score"])


def test_an_unloaded_board_reads_as_not_modelled_not_as_zero(payload):
    """`BOARD_DISABLED` is on, so there is no board at all. Both board rows
    must be `None`. A `0` here would tell the player their board is worth
    nothing, which is a different and false claim."""
    terms = payload["explain_by_rank"][0]["terms"]
    board_power = next(r for r in terms["power"] if r["field"] == "up_by_skill_tree")
    board_bonus = next(r for r in terms["bonus"] if r["field"] == "by_skill_tree")

    assert board_power["value"] is None
    assert board_power["basis"] == "not-modelled"
    assert board_bonus["value"] is None
    assert board_bonus["basis"] == "not-modelled"


def test_the_panel_never_claims_to_be_the_games_unit_score(payload):
    """The not-the-game's-Unit-Score flag, carried all the way to the wire — the flag the
    renderer keys its disclaimer off must actually arrive."""
    for block in payload["explain_by_rank"]:
        assert block["terms"]["is_unit_score"] is False
        assert block["terms"]["total_label"] == "our ranking score"


def test_the_solve_path_applies_the_collection(monkeypatch, tmp_path):
    """⚠ The Solve button must honour the collection editor.

    If `run_solve_job` loaded only `cards.json` / `owned.json` and never
    touched `state/collection.json`, every web solve would score a maxed
    collection with no account bonuses — while `hbuilder solve` scores the
    real levels from that same file — and the score-term panel would show
    Memory Bonus and Member Power-Up as "not modelled" with the posters
    sitting on disk two panels away.

    So: given a collection that declares account bonuses, the two account
    terms must arrive computed. Against a real file: Memory Bonus 5717 =
    ceil(129,924 x 44/1000) and Member Power-Up 848 = ceil(235,373 x 36/10000),
    both exact against the game's own forms, with the ranking score unchanged
    at 501,626.8 — the two terms are ranking-safe and must stay out of the
    total."""
    pool = tuple(
        make_card(f"card_{index}", member=f"Holomem{index}") for index in range(6)
    )
    from conftest import make_scoring_config

    config = make_scoring_config()
    monkeypatch.setattr(
        web_server, "load_inputs", lambda *a, **k: (pool, pool, config, None)
    )
    monkeypatch.setattr(web_server, "BOARD_DISABLED", True)

    # Stubbed at the LOADER seam rather than by writing a collection file.
    # `test_no_writes.py`'s fourth guard requires every filesystem-mutating
    # call in this package to be rooted at `COLLECTION_PATH`, and it is blunt
    # on purpose — naming a local variable `COLLECTION_PATH` to satisfy it
    # would be precisely the hole it exists to close. What this test is for is
    # the WIRING (does the solve path ask for the collection, and does it put
    # the bonuses on the config), not `apply_collection`'s own correctness,
    # which `tests/test_collection.py` owns.
    real_apply = web_server.apply_collection

    def _apply_with_bonuses(cards, _path, **kwargs):
        restated, basis = real_apply(cards, None, **kwargs)
        return restated, dataclasses.replace(
            basis, bonuses=AccountBonuses(posters=22, power_up_permyriad=36)
        )

    monkeypatch.setattr(web_server, "apply_collection", _apply_with_bonuses)

    job = web_server.Job(job_id="test-collection")
    web_server.run_solve_job(job, top_n=1, art_dir=Path("does-not-exist"))

    events = []
    while True:
        try:
            events.append(job.events.get_nowait())
        except queue.Empty:
            break

    # The state is emitted, never left implicit -- the same contract the
    # `board` event has.
    applied = [e for e in events if e.get("type") == "collection"]
    assert applied, f"no collection event; got {sorted({e.get('type') for e in events})}"
    assert applied[0]["applied"] is True

    payload = [e for e in events if e.get("type") == "result"][-1]["payload"]
    outside = {
        row["field"]: row
        for row in payload["explain_by_rank"][0]["terms"]["outside_total"]
    }
    assert outside["up_by_poster_collect"]["value"] is not None
    assert outside["up_by_poster_collect"]["basis"] == "computed"
    assert outside["card_training_evaluation"]["value"] is not None
    assert outside["card_training_evaluation"]["basis"] == "computed"


def test_the_cli_teams_contract_is_not_widened(payload):
    """`teams[]` is what `--json` consumers diff against, so the panel goes in
    the side key and never into the team objects. The server's own comment
    makes this promise about `contributions`; it holds for `terms` too."""
    for team in payload["teams"]:
        assert "terms" not in team
