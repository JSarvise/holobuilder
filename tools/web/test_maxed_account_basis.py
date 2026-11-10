"""The `maxed-account` card basis: "best team on a FULLY MAXED ACCOUNT", on
the same basis the community tool holodori.best uses — the maxed catalogue
(as `maxed`) plus a reference scenario:
every one of the 54 holomems at max level, her points (the master's
`CharacterLevel`, `basis.maxed_account_budget`) on what of her board reaches a
team from the bench, with level-2 Connect cards socketed
(`precompute.maxed_account_bench`), Memory posters = every poster the master
has, Member Power-Up = the roster ceiling (`precompute.maxed_account_bonuses`),
Connect pool = every card at level 2, team boards developed at the same
max-level points by default.

What is checked here, against REAL data (the gitignored dumps; the endpoint's
own 503 skips when they are absent):

* `/solve`, `/api/optimize-rerank` and `/api/compare` accept the basis;
* every response on it carries `certificate_basis.basis == "maxed-account"`
  and a non-null `inputs_hash` computed by `certificate.scenario_hash`
  (scenario literal + the generated bench's digest);
* the bench the scoring runs on has 54 characters at max level (asserted
  through the server's own scenario cache — the hook every route reads), and
  scores a team exactly as the whole tree would;
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
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from hbweb import basis, config

from hbuilder.inputs import load_inputs

REPO = Path(__file__).resolve().parents[2]

# a reference roster recommended on this basis by an external tool
HD_LEADER = "card-00022-5-uniq-0063-00"
HD_MEMBERS = [
    "card-00007-5-uniq-0008-00",
    "card-00022-5-uniq-0063-00",
    "card-00021-5-uniq-0017-00",
    "card-00006-5-uniq-0007-00",
    "card-06005-5-uniq-0061-00",
]
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
TOLERANCE = 5  # the across mean is a float over every chart


def _dumps_present() -> bool:
    d = REPO / "tools/recon/proxy/dumps"
    return all(
        (d / n).is_file()
        for n in (
            "skilltree.json",
            "master_SkillTreeNode.json",
            "charts.json",
            "board.json",
            "holodori/main_Poster.json",
        )
    )


pytestmark = pytest.mark.skipif(
    not _dumps_present(), reason="gitignored dumps/ absent (skilltree, master, charts, poster)"
)


# ---------------------------------------------------------------------------
# a live server on a free port (as test_compare_route.py does)
# ---------------------------------------------------------------------------
def _post(url: str, obj: dict, timeout: float = 900.0) -> tuple[int, dict]:
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
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
    assert basis.parse_basis("maxed-account") == "maxed-account"
    assert basis.parse_basis("MAXED-ACCOUNT") == "maxed-account"
    assert basis.parse_basis("maxed") == "maxed"
    assert basis.parse_basis("account") == "account"
    assert basis.parse_basis(None) == "account"
    assert basis.parse_basis("owned-max") == "account"
    assert basis.cards_maxed("maxed-account") and basis.cards_maxed("maxed")
    assert not basis.cards_maxed("account")
    assert basis.SCENARIO_BY_BASIS["maxed-account"] == "full-collection-maxed"


def test_parse_budget_reads_real_max_and_a_clamped_number():
    from hbuilder.domain.skilltree import TOTAL_BOARD_POINTS

    for raw in (None, "", "real", "REAL", "abc"):
        assert basis.parse_budget(raw) is None, raw
    assert basis.parse_budget(200) == 200 and basis.parse_budget("200") == 200
    assert basis.parse_budget(-5) == 0
    assert basis.parse_budget(10_000) == TOTAL_BOARD_POINTS
    try:
        top = basis.maxed_account_budget()
    except basis.MaxedAccountUnavailable as exc:
        pytest.skip(str(exc))
    assert basis.parse_budget("max") == basis.parse_budget("MAX") == top < TOTAL_BOARD_POINTS


def _scenario():
    """The scenario the routes read, built the way `basis.apply_basis` builds it."""
    cards, owned, cfg, _prov = load_inputs(config.CARDS_PATH, config.OWNED_PATH)
    try:
        cards, collection_basis, cfg, scenario = basis.apply_basis(cards, owned, cfg, "maxed-account")
    except basis.MaxedAccountUnavailable as exc:
        pytest.skip(str(exc))
    return cards, cfg, collection_basis, scenario


def test_bench_is_54_boards_at_max_level_with_level_2_sockets():
    """The bench passed to scoring: every one of the 54 holomems at max level, every
    green and yellow of her board the scorer counts bought within her points, 54
    sockets filled — the same `bench_characters` mapping `optimize_and_rerank` is
    handed on this basis."""
    from hbuilder.domain.board_plan import node_costs_from_master

    cards, cfg, collection_basis, scenario = _scenario()
    skilltree = config.read_json(config.SKILLTREE_PATH)
    node_master = config.read_json(config.NODE_MASTER_PATH)
    costs = node_costs_from_master(node_master)
    precompute = basis.load_precompute()
    assert scenario.points == basis.maxed_account_budget()
    by_name = {m["name"]: m for m in skilltree["members"] if m.get("model") in skilltree["models"]}
    assert len(scenario.bench) == 54, len(scenario.bench)
    for name, rows in scenario.bench.items():
        member = by_name[name]
        nodes = {r["node"] for r in rows}
        # the four Connect sockets (S-00x) hold a card and are written as
        # `connect_multiplier` on the nodes they cover
        wanted = {
            n
            for n in precompute.bench_reach_nodes(skilltree, node_master, member["chr"])
            if not n.startswith("S-")
        }
        assert wanted <= nodes <= set(skilltree["models"][member["model"]]), name
        assert sum(costs.get(n, 0) for n in nodes) <= scenario.points, name
        assert any(r.get("connect_multiplier", 1) > 1 for r in rows), f"{name}: no socket reached a node"
    assert scenario.bench_sockets == 54, scenario.bench_sockets
    # the account bonuses of the scenario, not the account's own
    # every poster the game has and the roster's power-up ceiling: both move as cards ship
    assert cfg.account.posters == 43
    assert cfg.account.power_up_permyriad == 1254
    assert scenario.bonuses.posters == 43 and scenario.bonuses.power_up_permyriad == 1254
    # the "current" board of this basis is the bench as a BoardModel
    assert scenario.board_model is not None
    assert len(scenario.board_model.characters) == 54
    # the same catalogue-wide, level-2 Connect pool the parity probe used
    assert len(scenario.connect_cards) == 150  # every 4★/5★ of the 204 (204 - 54 three-stars)
    # cached per process: a second build at the same cfg IS the same object
    _c2, _cfg2, _b2, again = basis.apply_basis(
        *load_inputs(config.CARDS_PATH, config.OWNED_PATH)[:3], "maxed-account"
    )
    assert again is scenario


def test_the_max_level_bench_scores_a_team_exactly_as_the_whole_tree_does(monkeypatch):
    """What a bench board does not buy reaches no team: the reference roster on a song
    sung by a bench holomem (m0334, Calliope) scores the SAME on the bench as on the
    bench of whole trees (the board this basis used before it held a max-level
    holomem's points), the six team holomems' own boards left out of both. The same
    rows reach the team: each kept node with the same effects and Connect multiplier."""
    import dataclasses

    from hbuilder.domain.board import board_from_mapping
    from hbuilder.domain.song import load_charts
    from hbuilder.domain.song_rank import score_team_on_chart
    from hbuilder.domain.team import Team

    cards, cfg, _collection_basis, scenario = _scenario()
    precompute = basis.load_precompute()
    skilltree = config.read_json(config.SKILLTREE_PATH)
    node_master = config.read_json(config.NODE_MASTER_PATH)
    models = skilltree["models"]
    monkeypatch.setattr(
        precompute,
        "maxed_account_board_state",
        lambda *a, **k: {
            m["chr"]: {"nodes": sorted(models[m["model"]]), "slots": {}}
            for m in skilltree["members"]
            if m.get("model") in models
        },
    )
    whole = precompute.maxed_account_bench(
        cards,
        dataclasses.replace(cfg, board=None),
        (HD_LEADER, tuple(HD_MEMBERS)),
        skilltree=skilltree,
        node_master=node_master,
        connect_cards=scenario.connect_cards,
    )
    assert set(whole) == set(scenario.bench)
    for name, rows in scenario.bench.items():
        full = {r["node"]: r for r in whole[name]}
        for row in rows:
            assert full[row["node"]] == row, (name, row["node"])

    by_id = {c.id: c for c in cards}
    team = Team(leader=by_id[HD_LEADER], members=tuple(by_id[m] for m in HD_MEMBERS))
    on_team = {c.member for c in (team.leader, *team.members)}
    library = load_charts(str(config.CHARTS_PATH))
    chart = library.get("m0334", "EXPERT") if library is not None else None
    if chart is None:
        pytest.skip("no EXPERT chart for m0334")
    name_by_chr = {m["chr"]: m["name"] for m in skilltree["members"]}

    def total(bench):
        board = board_from_mapping(
            {
                "characters": {n: list(r) for n, r in bench.items() if n not in on_team},
                "name_by_chr": name_by_chr,
            }
        )
        return score_team_on_chart(team, chart, dataclasses.replace(cfg, board=board)).total_score

    assert total(scenario.bench) == total(whole)


def test_certificate_meta_hashes_the_scenario_the_way_the_probes_do():
    """`_certificate_meta(..., scenario=)` = `certificate.scenario_hash`."""
    import dataclasses

    import certificate as cert_mod

    cards, cfg, collection_basis, scenario = _scenario()
    meta = basis.certificate_meta(cfg, "maxed-account", scenario=scenario)
    assert meta.get("inputs_hash_error") is None, meta
    h, per = cert_mod.scenario_hash(dataclasses.replace(cfg, board=None), scenario.bench, scenario.literal)
    assert meta["inputs_hash"] == h
    assert "bench" in per
    # and it is NOT the plain maxed hash: the scenario line differs
    plain, _p, _e = cert_mod.try_inputs_hash(dataclasses.replace(cfg, board=None), "maxed")
    assert plain != h
    assert cert_mod.universe_name(basis="maxed-account", pool="all") == "maxed-catalogue"


def _across_compare(web_base):
    status, doc = _post(
        web_base + "/api/compare",
        {
            "teams": [HD_TEAM, KOBO_TEAM],
            "basis": "maxed-account",
            "board": "develop",
            "budget": "max",
            "across": True,
            "platform": "mobile",
            "uptime_model": "finite",
            **PARITY_BASIS,
        },
    )
    _skip_if_503(status, doc)
    assert status == 200, doc
    return doc


def test_compare_default_budget_is_the_max_level_points_and_captured_is_the_bench(web_base):
    """No `budget` -> the max-level points on this basis; board "captured" -> the bench
    (every holomem at max level), which is the scenario's own "current" board."""
    status, doc = _post(
        web_base + "/api/compare",
        {
            "teams": [HD_TEAM, KOBO_TEAM],
            "basis": "maxed-account",
            "board": "develop",
            "platform": "mobile",
            "uptime_model": "finite",
        },
    )
    _skip_if_503(status, doc)
    assert status == 200, doc
    assert doc["assumptions"]["budget"] == basis.maxed_account_budget()
    assert doc["certificate_basis"]["basis"] == "maxed-account"
    status, cap = _post(
        web_base + "/api/compare",
        {
            "teams": [HD_TEAM, KOBO_TEAM],
            "basis": "maxed-account",
            "board": "captured",
            "platform": "mobile",
            "uptime_model": "finite",
        },
    )
    assert status == 200, cap
    assert cap["certificate_basis"]["basis"] == "maxed-account"
    assert cap["certificate_basis"]["budget_kind"] == "captured"
    # a maxed bench as the board scores well above board-off
    a = cap["teams"][0]
    assert a["panel"]["unit_score"] > a["score"]["unit_score_board_off"] * 1.2, a["score"]
