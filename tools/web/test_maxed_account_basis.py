"""The `maxed-account` card basis: "best team on a FULLY MAXED ACCOUNT", on
the same basis the community tool holodori.best uses — the maxed catalogue
(as `maxed`) plus a reference scenario:
every one of the 54 holomem boards developed at the full 453 points with
level-2 Connect cards socketed (`precompute.maxed_account_bench`), Memory
posters = the master's 37, Member Power-Up = the 11.58 % roster ceiling
(`precompute.maxed_account_bonuses`), Connect pool = every card at level 2,
team boards developed at the flat 453 by default.

What is checked here, against REAL data (the gitignored dumps; the endpoint's
own 503 skips when they are absent):

* `/solve`, `/api/optimize-rerank` and `/api/compare` accept the basis;
* every response on it carries `certificate_basis.basis == "maxed-account"`
  and a non-null `inputs_hash` computed by `certificate.scenario_hash`
  (scenario literal + the generated bench's digest);
* the bench the scoring runs on has 54 characters at the full tree (asserted
  through the server's own scenario cache — the hook every route reads);
* the two across routes (`/api/optimize-rerank` and `/api/compare`) give the
  SAME number for the same team on this basis.

No absolute score is pinned here. A cross-check against another tool is only
meaningful against that tool's own figure on the same inputs (catalogue,
charts, tree, posters and Connect cards all change with game updates), never
against a number re-derived from this tool's own output.

Writes nothing (tools/web/test_no_writes.py walks this file too).

    python -m pytest tools/web/test_maxed_account_basis.py
"""
from __future__ import annotations

import json
import queue
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import server as web_server

REPO = Path(__file__).resolve().parents[2]

# a reference roster recommended on this basis by an external tool
HD_LEADER = "card-00022-5-uniq-0063-00"
HD_MEMBERS = ["card-00007-5-uniq-0008-00", "card-00022-5-uniq-0063-00",
              "card-00021-5-uniq-0017-00", "card-00006-5-uniq-0007-00",
              "card-06005-5-uniq-0061-00"]
HD_TEAM = [HD_LEADER, *HD_MEMBERS]
# the Noel + Kobo Kanaeru "Rain-Dance Oracle" variant (a 1-swap variant of
# the reference roster)
KOBO_TEAM = [HD_LEADER, *HD_MEMBERS[:4], "card-03009-5-uniq-0041-00"]

# The across requests below ask for the MODELLED stat-pool deck power (the
# per-song default is the server's own Overall Power, +1.53 % on this maxed
# team), so an external figure can be checked on the same deck-power basis.
#
# Do NOT pin a reference figure re-derived from this tool's own output: an
# external implementation is what makes a comparison non-circular.
PARITY_BASIS = {"deck_power": "modelled"}
TOLERANCE = 5           # the across mean is a float over every chart


def _dumps_present() -> bool:
    d = REPO / "tools/recon/proxy/dumps"
    return all((d / n).is_file() for n in (
        "skilltree.json", "master_SkillTreeNode.json", "charts.json",
        "board.json", "holodori/main_Poster.json"))


pytestmark = pytest.mark.skipif(
    not _dumps_present(), reason="gitignored dumps/ absent (skilltree, master, charts, poster)")


# ---------------------------------------------------------------------------
# a live server on a free port (as test_compare_route.py does)
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def web_base():
    port = _free_port()
    server = web_server.ThreadingServer((web_server.BIND_HOST, port), web_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{web_server.BIND_HOST}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _post(url: str, obj: dict, timeout: float = 900.0) -> tuple[int, dict]:
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _skip_if_503(status: int, doc: dict) -> None:
    if status == 503:
        pytest.skip(f"develop dumps absent: {doc.get('error')}")


# ---------------------------------------------------------------------------
# the basis parser + the scenario hook every route reads
# ---------------------------------------------------------------------------
def test_parse_basis_accepts_the_third_basis_and_falls_back_to_account():
    assert web_server._parse_basis("maxed-account") == "maxed-account"
    assert web_server._parse_basis("MAXED-ACCOUNT") == "maxed-account"
    assert web_server._parse_basis("maxed") == "maxed"
    assert web_server._parse_basis("account") == "account"
    assert web_server._parse_basis(None) == "account"
    assert web_server._parse_basis("owned-max") == "account"
    assert web_server._cards_maxed("maxed-account") and web_server._cards_maxed("maxed")
    assert not web_server._cards_maxed("account")
    assert web_server._SCENARIO_BY_BASIS["maxed-account"] == "full-collection-maxed"


def _scenario():
    """The scenario the routes read, built the way `_apply_basis` builds it."""
    cards, owned, cfg, _prov = web_server.load_inputs(
        web_server.CARDS_PATH, web_server.OWNED_PATH, False)
    try:
        cards, collection_basis, cfg, scenario = web_server._apply_basis(
            cards, owned, cfg, "maxed-account")
    except web_server.MaxedAccountUnavailable as exc:
        pytest.skip(str(exc))
    return cards, cfg, collection_basis, scenario


def test_bench_is_54_boards_at_the_full_tree_with_level_2_sockets():
    """The bench passed to scoring: every one of the 54 holomems, EVERY node of
    its model (the 453-point tree), 54 sockets filled — the same
    `bench_characters` mapping `optimize_and_rerank` is handed on this basis."""
    cards, cfg, collection_basis, scenario = _scenario()
    skilltree = web_server._read_json(web_server.SKILLTREE_PATH)
    models = skilltree["models"]
    model_by_name = {}
    for m in skilltree["members"]:
        chr_id = m["chr"]
        name = next((c.member for c in cards if "chr-" + c.id.split("-")[1] == chr_id), None)
        if name is not None and m.get("model") in models:
            model_by_name[name] = m["model"]
    assert len(scenario.bench) == 54, len(scenario.bench)
    for name, rows in scenario.bench.items():
        assert name in model_by_name, name
        nodes = {r["node"] for r in rows}
        # every EFFECT node of the model; the four Connect sockets (S-00x) hold
        # a card and are written as `connect_multiplier` on the nodes they cover
        expected = {n for n in models[model_by_name[name]] if not n.startswith("S-")}
        assert nodes == expected, (
            f"{name}: {len(nodes)} nodes on the bench vs {len(expected)} in its model")
        assert any(r.get("connect_multiplier", 1) > 1 for r in rows), f"{name}: no socket reached a node"
    assert scenario.bench_sockets == 54, scenario.bench_sockets
    # the account bonuses of the scenario, not the account's own
    assert cfg.account.posters == 42
    assert cfg.account.power_up_permyriad == 1230
    assert scenario.bonuses.posters == 42 and scenario.bonuses.power_up_permyriad == 1230
    # the "current" board of this basis is the bench as a BoardModel
    assert scenario.board_model is not None
    assert len(scenario.board_model.characters) == 54
    # the same catalogue-wide, level-2 Connect pool the parity probe used
    assert len(scenario.connect_cards) == 147   # every 4★/5★ of the 201 (201 - 54 three-stars)
    # cached per process: a second build at the same cfg IS the same object
    _c2, _cfg2, _b2, again = web_server._apply_basis(
        *web_server.load_inputs(web_server.CARDS_PATH, web_server.OWNED_PATH, False)[:3],
        "maxed-account")
    assert again is scenario


def test_certificate_meta_hashes_the_scenario_the_way_the_probes_do():
    """`_certificate_meta(..., scenario=)` = `certificate.scenario_hash`."""
    import certificate as cert_mod
    import dataclasses
    cards, cfg, collection_basis, scenario = _scenario()
    meta = web_server._certificate_meta(cfg, "maxed-account", scenario=scenario)
    assert meta.get("inputs_hash_error") is None, meta
    h, per = cert_mod.scenario_hash(
        dataclasses.replace(cfg, board=None), scenario.bench, scenario.literal)
    assert meta["inputs_hash"] == h
    assert "bench" in per
    # and it is NOT the plain maxed hash: the scenario line differs
    plain, _p, _e = cert_mod.try_inputs_hash(dataclasses.replace(cfg, board=None), "maxed")
    assert plain != h
    assert cert_mod.universe_name(basis="maxed-account", pool="all") == "maxed-catalogue"


# ---------------------------------------------------------------------------
# /solve accepts the basis (run_solve_job driven directly, pinned roster)
# ---------------------------------------------------------------------------
def test_solve_accepts_the_basis_and_states_the_scenario():
    """A song-mode solve on the basis: the collection event states the
    scenario (not the account's counters), the board event names the bench,
    the payload carries `scenario` and `certificate_basis.basis`."""
    library = web_server.load_charts(str(web_server.CHARTS_PATH))
    song = "m0334" if library is not None and library.get("m0334") is not None else (
        library.music_ids[0] if library is not None and library.music_ids else "")
    if not song:
        pytest.skip("no chart library on disk")
    job = web_server.Job(job_id="test-maxed-account")
    web_server.run_solve_job(
        job, top_n=2, art_dir=Path("does-not-exist"), basis="maxed-account",
        use_board=True, song=song, pool_filter="all",
        require=tuple(HD_MEMBERS[:1] + HD_MEMBERS[2:5]), leader=(HD_LEADER,),
        uptime_model="finite")
    events = []
    while True:
        try:
            events.append(job.events.get_nowait())
        except queue.Empty:
            break
    errors = [e for e in events if e.get("type") == "error"]
    if errors and "maxed-account" in str(errors[0].get("message")):
        pytest.skip(errors[0]["message"])
    assert not errors, errors
    coll = [e for e in events if e.get("type") == "collection"]
    assert coll and coll[0]["maxed"] is True
    assert any("MAXED ACCOUNT" in line for line in coll[0]["lines"]), coll[0]["lines"]
    assert coll[0]["scenario"]["bench_boards"] == 54
    assert coll[0]["scenario"]["posters"] == 42
    board = [e for e in events if e.get("type") == "board"]
    assert board and board[0]["loaded"] is True
    assert any("maxed-account bench" in line for line in board[0]["lines"]), board[0]["lines"]
    results = [e for e in events if e.get("type") == "result"]
    assert results, [e.get("type") for e in events]
    payload = results[-1]["payload"]
    assert payload["certificate_basis"]["basis"] == "maxed-account"
    assert payload["certificate_basis"]["universe"] == "maxed-catalogue"
    assert payload.get("inputs_hash"), payload.get("inputs_hash_error")
    assert payload["scenario"]["label"].startswith("maxed account")
    assert payload["teams"], "no teams"
    assert payload["teams"][0]["leader_id"] == HD_LEADER


# ---------------------------------------------------------------------------
# /api/optimize-rerank
# ---------------------------------------------------------------------------
def test_optimize_rerank_accepts_the_basis_and_defaults_to_the_full_tree(web_base):
    status, doc = _post(web_base + "/api/optimize-rerank",
                        {"teams": [HD_TEAM], "basis": "maxed-account",
                         "platform": "mobile", "uptime_model": "finite"})
    _skip_if_503(status, doc)
    assert status == 200, doc
    assert doc["basis"] == "maxed-account"
    assert doc["budget"] == 453                     # the default on this basis
    assert doc["certificate_basis"]["basis"] == "maxed-account"
    assert doc["certificate_basis"]["budget_kind"] == "flat"
    assert doc["certificate_basis"]["budget"] == 453
    assert doc.get("inputs_hash"), doc.get("inputs_hash_error")
    assert doc["scenario"]["bench_boards"] == 54
    row = doc["ranking"][0]
    summ = row["board_plan_summary"]
    # every team character was developed against a 453 budget
    assert all(pc["points_max"] == 453 for pc in summ["per_character"]), summ["per_character"]
    # the baseline is the bench: every node owned before the respec
    assert all(pc["current_owned"] >= 100 for pc in summ["per_character"]), summ["per_character"]


def _across_rerank(web_base):
    status, doc = _post(web_base + "/api/optimize-rerank",
                        {"teams": [HD_TEAM, KOBO_TEAM], "basis": "maxed-account",
                         "across": True, "budget": 453,
                         "platform": "mobile", "uptime_model": "finite",
                         **PARITY_BASIS})
    _skip_if_503(status, doc)
    assert status == 200, doc
    return doc


def _across_compare(web_base):
    status, doc = _post(web_base + "/api/compare",
                        {"teams": [HD_TEAM, KOBO_TEAM], "basis": "maxed-account",
                         "board": "develop", "budget": 453, "across": True,
                         "platform": "mobile", "uptime_model": "finite",
                         **PARITY_BASIS})
    _skip_if_503(status, doc)
    assert status == 200, doc
    return doc


def test_optimize_rerank_across_runs_on_the_basis(web_base):
    """The develop route in across mode (the mean over every chart on each
    team's developed board): both teams scored, on the basis, hashed."""
    doc = _across_rerank(web_base)
    assert doc["certificate_basis"]["basis"] == "maxed-account"
    assert doc.get("inputs_hash"), doc.get("inputs_hash_error")
    by_cards = {tuple(r["cards"]): r["across_score"] for r in doc["ranking"]}
    assert set(by_cards) == {tuple(HD_TEAM), tuple(KOBO_TEAM)}, by_cards
    assert all(v > 0 for v in by_cards.values()), by_cards


# ---------------------------------------------------------------------------
# /api/compare -- across, and agreement with the develop route
# ---------------------------------------------------------------------------
def test_compare_across_agrees_with_optimize_rerank_on_the_maxed_account_basis(web_base):
    doc = _across_compare(web_base)
    assert doc["mode"] == "across"
    assert doc["assumptions"]["basis"] == "maxed-account"
    assert doc["assumptions"]["budget"] == 453
    assert doc["assumptions"]["scenario"]["bench_boards"] == 54
    assert doc["certificate_basis"]["basis"] == "maxed-account"
    assert doc["certificate_basis"]["budget_kind"] == "flat"
    assert doc.get("inputs_hash"), doc.get("inputs_hash_error")
    a, b = doc["teams"]
    assert a["cards"] == HD_TEAM and b["cards"] == KOBO_TEAM
    # the two routes develop and score the same teams on the same basis: one
    # number each
    rerank = _across_rerank(web_base)
    assert rerank["inputs_hash"] == doc["inputs_hash"]
    by_cards = {tuple(r["cards"]): r["across_score"] for r in rerank["ranking"]}
    assert abs(a["score"]["across_mean"] - by_cards[tuple(HD_TEAM)]) <= TOLERANCE,         (a["score"], by_cards)
    assert abs(b["score"]["across_mean"] - by_cards[tuple(KOBO_TEAM)]) <= TOLERANCE,         (b["score"], by_cards)


def test_compare_default_budget_is_the_full_tree_and_captured_is_the_bench(web_base):
    """No `budget` -> 453 on this basis; board "captured" -> the bench (every
    holomem at 453), which is the scenario's own "current" board."""
    status, doc = _post(web_base + "/api/compare",
                        {"teams": [HD_TEAM, KOBO_TEAM], "basis": "maxed-account",
                         "board": "develop", "platform": "mobile", "uptime_model": "finite"})
    _skip_if_503(status, doc)
    assert status == 200, doc
    assert doc["assumptions"]["budget"] == 453
    assert doc["certificate_basis"]["basis"] == "maxed-account"
    status, cap = _post(web_base + "/api/compare",
                        {"teams": [HD_TEAM, KOBO_TEAM], "basis": "maxed-account",
                         "board": "captured", "platform": "mobile", "uptime_model": "finite"})
    assert status == 200, cap
    assert cap["certificate_basis"]["basis"] == "maxed-account"
    assert cap["certificate_basis"]["budget_kind"] == "captured"
    # a maxed bench as the board scores well above board-off
    a = cap["teams"][0]
    assert a["panel"]["unit_score"] > a["score"]["unit_score_board_off"] * 1.2, a["score"]
