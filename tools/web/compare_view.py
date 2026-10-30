"""`POST /api/compare` — two hand-picked teams, scored side by side under ONE
set of assumptions, with the differences explained.

READS ONLY (tools/web/test_no_writes.py). Everything here is the same
machinery the Solve page's routes already use — `load_inputs` /
`apply_collection` for the cards, `optimize_and_rerank` for a developed board,
`score_song_detailed` / `deck_screen_unit_score` for the numbers, `certindex`
for the same-instant-same-basis certificate — glued together for exactly two
teams. There is deliberately NO second definition of what a card is worth:
the only web-side computation is `override_card`, which restates ONE card at
a chosen level/bloom through the domain's own `resolve_card_state` /
`StatBasis.stats_at` / `restate_card_skills` (the per-card what-if that
`apply_collection` has no switch for), and the panel-line derivation
(`panel_lines`, below).

The server module is imported lazily (`_srv()`): `server.py` imports this
module at load, so a top-level `import server` here would be circular. By the
time `build_compare` runs the server is fully loaded.

Request body::

    {"teams": [{"cards": [leader, m1..m5],
                "overrides": [null | {"level": int|null, "bloom": int|null}] * 6},
               {...}],                       # exactly two; a bare id list is accepted
     "basis": "account" | "maxed" | "maxed-account",
     "assume_level": null | int 1..80 | "max",   "assume_bloom": null | int 0..6 | "max",
     "board": "captured" | "develop" | "off",  "budget": null (real) | int 0..447,
     "song": "m0334" | "",  "across": bool,       # song > across > Unit Score
     "platform": "mobile" | "pc",  "uptime_model": "finite" | "steady",
     "objective_leaf": "plan_cache" (default) | "develop",
     "distribution": bool}                      # song mode: the run-score law per team

`board: "state"` scores each team on the board a RANKED ROW was scored on: every
team entry then carries that row's `board_state` ({chr: {nodes, slots}}) and,
optionally, its `member_order`; the board is rebuilt exactly as the develop
assembled it (`row_detail.board_from_state`) so the panel, the split, the law
and the song figures are the row's own numbers.

Every per-song figure goes through `song_rank.score_team_on_chart` (the
ranker's own entry point: Overall Power deck power by default) via
`row_detail.figures`; each row also carries `score.deck_power`, the Score Bonus
five-line split (`score_bonus`, `row_detail.score_bonus_lines`) in the row's
slot order, and with `distribution: true` the exact run-score law
(`distribution`, song mode).

`board: "develop"` scores each team on TWO boards and says which is which:
the OBJECTIVE — the team on the FIXED plans of one PlanCache
(`optimize_and_rerank(plan_cache=)`, the objective the EXACT certificates
certify; seeded from the per-leader table's
own seed when this request's inputs_hash + budget/uptime/platform match it,
else from team A) — is the headline, `score.song_est` / `score.across_mean`
and `board_state`; the per-team greedy re-develop is the LABELLED what-if
(`score.song_est_develop` / `score.across_mean_develop`, `develop_whatif`,
`board_state_develop`, `board_plan_summary_develop`). `objective_leaf:
"develop"` gives single-number rows (headline = the re-develop).
Unit-Score mode (no song, no across) has no plan-cache objective and stays on
the develop leaf, labelled.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any, Mapping

from hbuilder.cli import (
    COLLECTION_CAPS_PATH,
    COLLECTION_CURVE_PATH,
    COLLECTION_LADDER_PATH,
    load_board_basis,
    load_inputs,
    _LoadFailure,
)
from hbuilder.domain.board import BoardError
from hbuilder.domain.board_plan import (
    PlanCache,
    SongCast,
    song_cast,
    develop_refined_team,
    model_for_character,
    node_effect_ids_from_master,
    optimize_and_rerank,
    skill_tree_from_dumps,
)
from hbuilder.domain.card import Stats
from hbuilder.domain.collection import (
    CollectionError,
    caps_by_rarity_from_mapping,
    resolve_card_state,
    restate_card_skills,
    skill_ladder_from_mapping,
    stat_basis_from_mapping,
)
from hbuilder.domain.scoring import deck_screen_unit_score, score_with_breakdown
from hbuilder.domain.scoring_config import ACTIVE_UPTIME_MODELS
from hbuilder.domain.song import SongError, load_charts
from hbuilder.domain.song_rank import score_team_on_chart
from hbuilder.domain.team import MEMBER_COUNT, Team, TeamError

import certificate as cert_mod
import certindex
import row_detail

TEAM_LABELS = ("A", "B")
BOARD_MODES = ("captured", "develop", "off", "state")
LEVEL_RANGE = (1, 80)
BLOOM_RANGE = (0, 6)
FULL_TREE = 453

# The panel lines, in the order the deck screen lists them. `kind` says how
# the Δ is read: a POWER line is absolute
# points of Overall Power; a RATE line is percentage points.
PANEL_LINES: tuple[tuple[str, str, str], ...] = (
    ("member_parameter", "Member Parameter", "power"),
    ("passive", "Passive", "power"),
    ("outfit", "Outfit", "power"),
    ("board", "Board", "power"),
    ("memory", "Memory", "power"),
    ("power_up", "Power-Up", "power"),
    ("overall_power", "Overall Power", "power"),
    ("combined_score_rate_pct", "Score bonus (pooled)", "rate"),
    ("support_B_pct", "Support B (SPECIAL + support)", "rate"),
    ("board_score_rate_pct", "Board score rate", "rate"),
    ("score_bonus_permil", "Score bonus (deck screen, permil)", "permil"),
    ("unit_score", "Unit Score (deck screen)", "power"),
)


class CompareError(Exception):
    """A typed HTTP answer (400 bad request / 503 missing dump)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _srv():
    import server  # noqa: PLC0415 — see the module docstring (circular at load)

    return server


# ---------------------------------------------------------------------------
# request parsing
# ---------------------------------------------------------------------------
def _parse_override(raw: object) -> dict | None:
    """`{"level": int|None, "bloom": int|None}` or None. Values are clamped
    to the game's ranges the same way `_parse_assume` clamps the what-if."""
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, int | None] = {"level": None, "bloom": None}
    for key, (lo, hi) in (("level", LEVEL_RANGE), ("bloom", BLOOM_RANGE)):
        value = raw.get(key)
        if value is None or value == "" or value == "account":
            continue
        try:
            out[key] = max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            continue
    if out["level"] is None and out["bloom"] is None:
        return None
    return out


def parse_teams(body: Mapping) -> list[dict]:
    """Two `{cards: [6 ids], overrides: [6 x (dict|None)]}` entries, or a
    CompareError(400) naming what is wrong. Card EXISTENCE and holomem
    duplicates are checked later against the loaded catalogue (`build_teams`)."""
    raw = body.get("teams")
    if not isinstance(raw, list) or len(raw) != 2:
        raise CompareError(400, "need exactly two teams in 'teams' (A and B)")
    teams: list[dict] = []
    for label, entry in zip(TEAM_LABELS, raw):
        board_state = member_order = None
        if isinstance(entry, Mapping):
            ids_raw = entry.get("cards")
            ov_raw = entry.get("overrides")
            if isinstance(entry.get("board_state"), Mapping):
                board_state = entry.get("board_state")
            if isinstance(entry.get("member_order"), list):
                member_order = [str(c) for c in entry.get("member_order")]
        else:
            ids_raw, ov_raw = entry, None
        ids = [str(c).strip() for c in (ids_raw or []) if c] if isinstance(ids_raw, list) else []
        if len(ids) != MEMBER_COUNT + 1:
            raise CompareError(400, (
                f"team {label}: need {MEMBER_COUNT + 1} card ids (leader + "
                f"{MEMBER_COUNT} members); got {len(ids)}"))
        overrides: list[dict | None] = [None] * (MEMBER_COUNT + 1)
        if isinstance(ov_raw, list):
            for i, o in enumerate(ov_raw[: MEMBER_COUNT + 1]):
                overrides[i] = _parse_override(o)
        elif isinstance(ov_raw, Mapping):
            # keyed by card id — applies to every slot holding that card
            for i, cid in enumerate(ids):
                overrides[i] = _parse_override(ov_raw.get(cid))
        teams.append({"label": label, "cards": ids, "overrides": overrides,
                      "board_state": board_state, "member_order": member_order})
    return teams


def parse_assumptions(body: Mapping) -> dict:
    """The SAME tokens the Solve page's routes read, through the server's own
    parsers where one exists (`_parse_assume`, `_parse_platform`)."""
    srv = _srv()
    basis = srv._parse_basis(body.get("basis"))
    board = str(body.get("board") or "develop").lower()
    if board not in BOARD_MODES:
        board = "develop"
    budget = None
    if body.get("budget") is not None and str(body.get("budget")).lower() not in ("", "real"):
        try:
            budget = max(0, min(FULL_TREE, int(body.get("budget"))))
        except (TypeError, ValueError):
            budget = None
    # The maxed-account basis develops at the full tree by default (a maxed
    # account has every point; "real" points are the user's own account's).
    if budget is None and basis == srv.MAXED_ACCOUNT_BASIS:
        budget = srv.MAXED_ACCOUNT_BUDGET
    uptime = str(body.get("uptime_model") or "").lower() or None
    if uptime not in ACTIVE_UPTIME_MODELS:
        uptime = None
    song = str(body.get("song") or "").strip()
    # chart difficulty (absent -> EXPERT); the server's own parser, so an
    # unknown one is the same 400 every other route answers
    try:
        difficulty = srv._parse_difficulty(body, song)
    except srv._HttpError as exc:
        raise CompareError(exc.code, exc.obj.get("error") if isinstance(exc.obj, dict) else str(exc))
    objective_leaf = str(body.get("objective_leaf") or "plan_cache").lower()
    if objective_leaf not in ("plan_cache", "develop"):
        objective_leaf = "plan_cache"
    return {
        "basis": basis,
        "assume_level": srv._parse_assume(body, "assume_level", *LEVEL_RANGE),
        "assume_bloom": srv._parse_assume(body, "assume_bloom", *BLOOM_RANGE),
        "board": board,
        "budget": budget,
        "song": song,
        "difficulty": difficulty,
        "across": bool(body.get("across")) and not song,
        "platform": srv._parse_platform(body),
        "uptime_model": uptime,
        # `server` (the default) | `modelled` — see `server.DECK_POWER_BASES`.
        # Comparisons with the community tool holodori.best use `modelled`, and
        # that comparison is one of the few non-circular checks available.
        "deck_power": srv._deck_power_basis(body),
        # `auto` is resolved against a cfg later (it needs run_state), so the
        # raw request flag travels and `_auto_premise` does the work once.
        "auto": body.get("auto") is True,
        "objective_leaf": objective_leaf,
        "distribution": body.get("distribution") is True,
    }


# ---------------------------------------------------------------------------
# the per-card what-if (web-side; apply_collection has no per-card switch)
# ---------------------------------------------------------------------------
def _rarity_digit(card) -> str | None:
    match = re.match(r"(\d+)", str(getattr(card, "rarity", "") or ""))
    return match.group(1) if match else None


def _whatif_entry(entry: object, assume_level: object, assume_bloom: object) -> dict:
    """One collection entry with the all-six what-if applied — the same rule
    as `cli._override_collection_levels._apply`: only the assumed field moves,
    and a level assumption unlocks the limit break so it is not clamped."""
    out = dict(entry) if isinstance(entry, Mapping) else {}
    if assume_level is not None:
        out["level"] = assume_level
        out["limit_break"] = "max"
    if assume_bloom is not None:
        out["bloom"] = assume_bloom
    return out


class OverrideContext:
    """The read-only inputs one per-card override needs: the curve, the caps,
    the ladder, and the collection document (what-if already applied) so the
    field NOT overridden keeps the account's own value. On the maxed basis
    the untouched field is the cap — that is what maxed means."""

    def __init__(self, *, basis_name: str, assume_level: object, assume_bloom: object) -> None:
        srv = _srv()
        root = srv.REPO_ROOT
        curve_raw = srv._read_json(root / COLLECTION_CURVE_PATH)
        caps_raw = srv._read_json(root / COLLECTION_CAPS_PATH)
        ladder_raw = srv._read_json(root / COLLECTION_LADDER_PATH)
        self.basis = stat_basis_from_mapping(curve_raw) if curve_raw else None
        self.caps = caps_by_rarity_from_mapping(caps_raw) if caps_raw else {}
        self.ladder = skill_ladder_from_mapping(ladder_raw)
        self.maxed = srv._cards_maxed(basis_name)
        if self.maxed:
            self.entries: dict = {}
            self.default: object = "max"
        else:
            doc = srv._read_json(srv.COLLECTION_PATH) or {}
            entries = doc.get("cards") or {}
            self.entries = {cid: _whatif_entry(e, assume_level, assume_bloom)
                            for cid, e in entries.items()} if isinstance(entries, dict) else {}
            self.default = _whatif_entry(doc.get("card_default", "max"),
                                         assume_level, assume_bloom)

    def override(self, raw_card, level: int | None, bloom: int | None):
        """`(card, state)`: `raw_card` (the catalogue's maxed card, BEFORE the
        collection was applied — `restate_card_skills` swaps slots from that
        level, so it must start from the catalogue's) restated at the given
        level and/or bloom. Stats through `StatBasis.stats_at`, skills through
        `restate_card_skills`; a card the curve cannot restate keeps its stats
        (the same "reported, never guessed" rule apply_collection follows)."""
        entry = dict(self.entries.get(raw_card.id) or {}) if not self.maxed else {}
        default = self.default if not self.maxed else {"level": "max", "bloom": "max",
                                                        "limit_break": "max"}
        if level is not None:
            entry["level"] = level
            entry["limit_break"] = "max"
        if bloom is not None:
            entry["bloom"] = bloom
        caps = self.caps.get(_rarity_digit(raw_card) or "")
        state = resolve_card_state(raw_card.id, entry, default, caps)
        card = raw_card
        stats = self.basis.stats_at(raw_card.id, state.level, state.bloom) if self.basis else None
        if stats is not None:
            card = dataclasses.replace(card, stats=Stats(
                performance=stats[0], technique=stats[1], sense=stats[2]))
        card, _notes = restate_card_skills(card, state.bloom, self.ladder.get(raw_card.id, {}))
        return card, state


# ---------------------------------------------------------------------------
# panel lines
# ---------------------------------------------------------------------------
def panel_lines(team: Team, cfg_board, cfg_off) -> dict:
    """The model's deck-screen lines for one team, in the deck screen's shape. `cfg_board`
    carries the board the team is scored on; `cfg_off` is board=None (same
    otherwise). Member Parameter = `base_stat_sum`; Outfit = the costume stat
    effect's own power (its rate x the stat it lifts — exact under additive
    stacking); Passive = the rest of `effective - base`; Board = `board_power`;
    Memory / Power-Up = the breakdown's own summands. The score bonus is ONE
    pooled `combined_score_rate` (not separable into the game's lines), so this
    reports the pooled rate, the SPECIAL/support `B` inside it, and the board
    rate."""
    bd = score_with_breakdown(team, cfg_board)
    ds = deck_screen_unit_score(bd)
    bd_off = score_with_breakdown(team, cfg_off)
    outfit_power = 0.0
    by_id = {m.id: m for m in team.members}
    for c in bd.contributions:
        if c.slot_kind == "costume" and c.pool == "stat" and c.weighted_rate:
            stat = c.effect_type
            targets = c.target_card_ids or tuple(m.id for m in team.members)
            base = 0.0
            for cid in targets:
                card = by_id.get(cid)
                if card is None:
                    continue
                stats = card.stats
                if stat in ("all", "all_stats"):
                    base += sum(getattr(stats, s) for s in ("performance", "technique", "sense"))
                else:
                    base += getattr(stats, stat, 0.0)
            outfit_power += c.weighted_rate * base
    stat_lift = bd.effective_stat_sum - bd.base_stat_sum
    return {
        "unit_score": ds.unit_score, "overall_power": ds.overall_power,
        "score_bonus_permil": ds.score_bonus_permil,
        "member_parameter": bd.base_stat_sum,
        "outfit": outfit_power, "passive": stat_lift - outfit_power,
        "board": bd.board_power, "memory": bd.memory_bonus, "power_up": bd.member_power_up,
        "combined_score_rate_pct": bd.combined_score_rate * 100.0,
        "combined_score_rate_board_off_pct": bd_off.combined_score_rate * 100.0,
        "support_B_pct": bd.combined_support_rate * 100.0,
        "board_score_rate_pct": bd.board_score_rate * 100.0,
        "total_score": bd.total_score,
        "outfit_gate_fired": [c.gate_fired for c in bd.contributions if c.slot_kind == "costume"],
        "_bd": bd,
    }


def _contribution_rows(bd) -> list[dict]:
    return [{
        "card_id": c.card_id, "slot_kind": c.slot_kind, "family": c.family,
        "effect_type": c.effect_type, "state": c.state, "pool": c.pool,
        "weighted_rate": c.weighted_rate, "gate_fired": c.gate_fired,
    } for c in bd.contributions]


# ---------------------------------------------------------------------------
# the difference column
# ---------------------------------------------------------------------------
def _pct(a: float, b: float) -> float | None:
    return round(100.0 * (b - a) / a, 2) if a else None


def _delta(a: float | None, b: float | None) -> dict:
    if a is None or b is None:
        return {"a": a, "b": b, "delta": None, "pct": None}
    return {"a": a, "b": b, "delta": b - a, "pct": _pct(a, b)}


def explain_difference(pa: dict, pb: dict, gates_a: list, gates_b: list,
                       score_a: float, score_b: float, mode: str) -> dict:
    """One sentence naming the biggest driver of B − A, from the panel lines.
    Attribution is a log-decomposition of the deck-screen product
    `overall_power x (1 + score bonus)`: each POWER line's share of Δln(power)
    and Δln(1 + rate) for the pooled bonus / board rate. Approximate for a
    per-song total (the chart adds its own timing) but the ordering of drivers
    is what the sentence needs, not their exact size."""
    import math  # noqa: PLC0415

    drivers: list[tuple[float, str]] = []
    pow_a, pow_b = pa.get("overall_power") or 0.0, pb.get("overall_power") or 0.0
    if pow_a > 0 and pow_b > 0:
        for key, label, kind in PANEL_LINES:
            if kind != "power" or key in ("overall_power", "unit_score"):
                continue
            va, vb = pa.get(key) or 0.0, pb.get(key) or 0.0
            if va == vb:
                continue
            # share of Δln(overall power) attributable to this line
            weight = (vb - va) / ((pow_a + pow_b) / 2.0)
            drivers.append((weight, key))
        for key in ("combined_score_rate_pct", "board_score_rate_pct"):
            ra, rb = (pa.get(key) or 0.0) / 100.0, (pb.get(key) or 0.0) / 100.0
            if ra != rb:
                drivers.append((math.log1p(rb) - math.log1p(ra), key))
    outfit_a = any(pa.get("outfit_gate_fired") or [])
    outfit_b = any(pb.get("outfit_gate_fired") or [])
    sentence: str
    if not drivers:
        sentence = "The two teams score identically under these assumptions."
    else:
        weight, key = max(drivers, key=lambda d: abs(d[0]))
        who = "B" if weight > 0 else "A"
        other = "A" if who == "B" else "B"
        pa_v, pb_v = pa.get(key) or 0.0, pb.get(key) or 0.0
        gain = abs(pb_v - pa_v)
        if key == "outfit" and outfit_a != outfit_b:
            sentence = f"outfit gate met by {'A' if outfit_a else 'B'} only"
        elif key == "combined_score_rate_pct":
            sentence = f"{who}'s ACTIVE/score-bonus pool +{gain:.2f} pp over {other}"
        elif key == "board_score_rate_pct":
            sentence = f"{who}'s board score rate +{gain:.2f} pp over {other}"
        else:
            label = dict((k, l) for k, l, _ in PANEL_LINES).get(key, key)
            base = (pa_v if who == "B" else pb_v)
            rel = f" (+{100.0 * gain / base:.1f} %)" if base else ""
            sentence = f"{who}'s {label} +{gain:,.0f}{rel} over {other}"
    ranked = sorted(drivers, key=lambda d: -abs(d[0]))
    return {
        "score": _delta(score_a, score_b),
        "sentence": sentence,
        "drivers": [{"line": k, "weight": round(w, 5)} for w, k in ranked[:5]],
        "gates": _gate_diff(gates_a, gates_b),
        "mode": mode,
    }


def _gate_key(g: dict) -> tuple:
    return (g["card_id"], g["slot_kind"], g["dimension"], g["value"], g["threshold"])


def _gate_diff(gates_a: list, gates_b: list) -> list[dict]:
    """Every gate either team carries, with each team's verdict (None when the
    team does not carry the skill at all)."""
    a_by = {_gate_key(g): g for g in gates_a}
    b_by = {_gate_key(g): g for g in gates_b}
    rows = []
    for key in sorted(set(a_by) | set(b_by), key=str):
        ga, gb = a_by.get(key), b_by.get(key)
        g = ga or gb
        rows.append({
            "card_id": g["card_id"], "slot_kind": g["slot_kind"],
            "dimension": g["dimension"], "value": g["value"], "threshold": g["threshold"],
            "a": None if ga is None else {"fired": ga["fired"], "actual_count": ga["actual_count"]},
            "b": None if gb is None else {"fired": gb["fired"], "actual_count": gb["actual_count"]},
        })
    return rows


def _skill_diff(rows_a: list, rows_b: list) -> list[dict]:
    key = lambda r: (r["card_id"], r["slot_kind"], r["family"], r["effect_type"])  # noqa: E731
    a_by = {key(r): r for r in rows_a}
    b_by = {key(r): r for r in rows_b}
    out = []
    for k in sorted(set(a_by) | set(b_by), key=str):
        ra, rb = a_by.get(k), b_by.get(k)
        r = ra or rb
        out.append({
            "card_id": r["card_id"], "slot_kind": r["slot_kind"], "family": r["family"],
            "effect_type": r["effect_type"], "pool": r["pool"],
            "a": None if ra is None else {"weighted_rate": ra["weighted_rate"], "state": ra["state"]},
            "b": None if rb is None else {"weighted_rate": rb["weighted_rate"], "state": rb["state"]},
        })
    return out


# ---------------------------------------------------------------------------
# the develop machinery (the same inputs develop_pipeline feeds optimize_and_rerank)
# ---------------------------------------------------------------------------
class _Developer:
    def __init__(self, cfg, cards, basis_name: str, budget: int | None, chart,
                 ma_scenario=None, assume_bloom=None) -> None:
        srv = _srv()
        self.skilltree = srv._read_json(srv.SKILLTREE_PATH)
        self.node_master = srv._read_json(srv.NODE_MASTER_PATH)
        if not self.skilltree or not self.node_master:
            raise CompareError(503, (
                "board develop needs skilltree.json + master_SkillTreeNode.json "
                "under the gitignored dumps/; run tools/recon/proxy/skilltree.py"))
        precompute = srv._load_precompute()
        if precompute is None or not hasattr(precompute, "board_export"):
            raise CompareError(503, (
                "could not import board_export via precompute_optimisations "
                "(needs the proxyenv interpreter this server runs under)."))
        self.effects = node_effect_ids_from_master(self.node_master)
        scenario = srv._SCENARIO_BY_BASIS.get(basis_name, "account")
        export_doc = precompute.board_export.build(gain=False, pool=1, scenario=scenario)
        self.granted = {c["chr"]: (c.get("points") or {}).get("granted")
                        for c in export_doc.get("characters", [])}
        self.current_by_chr = {
            c["chr"]: {"name": c.get("name") or c.get("member") or c["chr"],
                       "owned": sorted(n["id"] for n in c.get("nodes", [])
                                       if n.get("state") == "owned"),
                       "granted": (c.get("points") or {}).get("granted")}
            for c in export_doc.get("characters", [])}
        try:
            self.node_prices = precompute.to._prices()
        except Exception:  # noqa: BLE001 — materials stay unknown, never fatal
            self.node_prices = None
        try:
            # an assumed bloom lifts the Connect levels too (bloom 5 = level 2)
            self.connect_cards = precompute.connect_cards_for(scenario, assume_bloom=assume_bloom)
        except Exception:  # noqa: BLE001 — sockets stay idealised
            self.connect_cards = None
        self.bench_characters = srv._bench_characters()
        self.bench_sockets = srv._bench_sockets()       # what the off-team sockets hold
        # The maxed-account basis: the scenario's bench (every holomem at the
        # full tree, level-2 sockets) and its catalogue-wide Connect pool — the
        # same inputs the server's develop routes feed optimize_and_rerank.
        self.name_by_chr = {"chr-" + c.id.split("-")[1]: c.member for c in cards}
        if ma_scenario is not None:
            self.bench_characters = ma_scenario.bench
            self.bench_sockets = None                   # the maxed bench carries its own
            self.connect_cards = ma_scenario.connect_cards
            self.current_by_chr = ma_scenario.current_by_chr(self.name_by_chr)
            self.granted = {k: srv.MAXED_ACCOUNT_BUDGET for k in self.current_by_chr}
        self.cfg = cfg
        self.flat_budget = budget
        self._tree_cache: dict[str, object] = {}
        self.chart = chart
        # the song's cast, passed whole: every singing board is valued as a singer
        # (a member's yellows and yellow socket too), not only the leader's -- a bare
        # leader flag would score teams with a singing member several % under the
        # optimizer
        self.cast = song_cast(chart) if chart is not None else SongCast()

    def tree_for(self, card):
        model = model_for_character(self.skilltree, "chr-" + card.id.split("-")[1])
        if model is None:
            return None
        if model not in self._tree_cache:
            self._tree_cache[model] = skill_tree_from_dumps(
                self.skilltree, self.node_master, model)
        return self._tree_cache[model]

    def budget_for(self, card) -> int:
        if self.flat_budget is not None:
            return self.flat_budget
        g = self.granted.get("chr-" + card.id.split("-")[1])
        return int(g) if g else 0

    def develop(self, team: Team, plan_cache: PlanCache | None = None):
        """`team` developed: per-team greedy (`plan_cache=None`, the what-if)
        or on the FIXED plans of `plan_cache` (the objective) — the same
        `optimize_and_rerank` either way, so plan, sockets and board come back
        in one shape."""
        if plan_cache is not None and self.chart is not None:
            # song mode on the objective: the activation nodes chosen jointly
            # on this chart, as the rerank's `_objective_bt` does — or the panel
            # reads ~1 % under the row it explains
            chart = self.chart

            def _on_chart(board, _team=team):
                return score_team_on_chart(
                    _team, chart, dataclasses.replace(self.cfg, board=board)).total_score
            return develop_refined_team(
                team, self.cfg, _on_chart, tree_for=self.tree_for, budget_for=self.budget_for,
                node_effect_ids=self.effects, skilltree=self.skilltree,
                connect_cards=self.connect_cards, bench_characters=self.bench_characters,
                name_by_chr=self.name_by_chr, plan_cache=plan_cache,
                sings=self.cast, bench_sockets=self.bench_sockets)[0]
        return optimize_and_rerank(
            [team], self.cfg, tree_for=self.tree_for, budget_for=self.budget_for,
            node_effect_ids=self.effects, skilltree=self.skilltree,
            connect_cards=self.connect_cards, bench_characters=self.bench_characters,
            name_by_chr=self.name_by_chr, sings=self.cast, plan_cache=plan_cache,
            bench_sockets=self.bench_sockets)[0]

    def plan_cache_from(self, seed: Team) -> PlanCache:
        """ONE PlanCache for the whole request, seeded from `seed`'s board-off
        standing — exactly `parallel_refine.new_plan_cache` (cfg board=None)."""
        return PlanCache.from_team(seed, dataclasses.replace(self.cfg, board=None))

    def board_state_of(self, bt) -> dict:
        slots_by_chr: dict[str, dict[str, str]] = {}
        for p in bt.sockets:
            slots_by_chr.setdefault(p.chr, {})[p.socket] = p.card
        state: dict = {}
        for card in (bt.team.leader, *bt.team.members):
            cplan = bt.plan.per_character.get(card.id)
            if cplan is None:
                continue
            chr_id = "chr-" + card.id.split("-")[1]
            state[chr_id] = {"nodes": sorted(cplan.selected),
                             "slots": slots_by_chr.get(chr_id, {})}
        return state

    def summary(self, bt, board_state: dict, *, board_off: int, developed: int, metric: str) -> dict:
        return _srv()._board_plan_summary(
            bt, board_state,
            budget_kind="flat" if self.flat_budget is not None else "real",
            budget_for=self.budget_for, current_by_chr=self.current_by_chr,
            node_prices=self.node_prices, board_off=board_off, developed=developed,
            metric=metric)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def build_teams(specs: list[dict], by_id: dict, raw_by_id: dict,
                ctx: OverrideContext) -> list[dict]:
    """Team objects from the parsed specs; per-slot overrides restated through
    `ctx.override`. 400 on an unknown card or an illegal roster (a duplicated
    holomem, ... — whatever `Team` itself refuses)."""
    out = []
    for spec in specs:
        missing = [c for c in spec["cards"] if c not in by_id]
        if missing:
            raise CompareError(400, f"team {spec['label']}: unknown card id(s): {missing}")
        slots = []
        cards = []
        for cid, ov in zip(spec["cards"], spec["overrides"]):
            card = by_id[cid]
            slot = {"card_id": cid, "override": ov, "level": None, "bloom": None}
            if ov is not None:
                card, state = ctx.override(raw_by_id[cid], ov.get("level"), ov.get("bloom"))
                slot["level"], slot["bloom"] = state.level, state.bloom
            cards.append(card)
            slots.append(slot)
        try:
            team = Team(leader=cards[0], members=tuple(cards[1:]))
        except TeamError as exc:
            raise CompareError(400, f"team {spec['label']}: invalid roster: {exc}")
        out.append({"label": spec["label"], "team": team, "slots": slots,
                    "board_state": spec.get("board_state"),
                    "member_order": spec.get("member_order")})
    return out


def build_compare(body: Mapping, *, art_dir: Path) -> dict:
    """The response document. Raises CompareError for a typed HTTP answer."""
    srv = _srv()
    specs = parse_teams(body)
    A = parse_assumptions(body)
    basis_name, board_mode = A["basis"], A["board"]

    # song / across
    chart = None
    all_charts: list = []
    difficulty = A.get("difficulty") or "EXPERT"
    charts_file = srv._charts_path(difficulty)
    if A["song"]:
        library = load_charts(str(charts_file))
        chart = library.get(A["song"], difficulty) if library is not None else None
        if chart is None:
            raise CompareError(400, (
                f"song {A['song']!r}: no {difficulty} chart in {charts_file.name} "
                f"(build it with tools/recon/proxy/build_charts.py)"))
    elif A["across"]:
        library = load_charts(str(charts_file))
        all_charts = [c for c in ((library.get(m, difficulty) for m in library.music_ids)
                                  if library else ())
                      if c is not None]
        if not all_charts:
            raise CompareError(400, (
                f"across-songs compare needs the chart library ({charts_file.name}) "
                f"on disk; build it with tools/recon/proxy/build_charts.py"))
    mode = "song" if chart is not None else ("across" if all_charts else "unit_score")

    try:
        raw_cards, owned, cfg, _prov = load_inputs(srv.CARDS_PATH, srv.OWNED_PATH, False)
        if A["uptime_model"] in ACTIVE_UPTIME_MODELS:
            cfg = dataclasses.replace(cfg, active_uptime_model=A["uptime_model"])
        cfg = dataclasses.replace(cfg, platform=A["platform"], **A.get("deck_power", {}))
        if A.get("auto"):
            cfg = dataclasses.replace(cfg, **srv._auto_premise(cfg, {"auto": True}))
        cfg = dataclasses.replace(cfg, board=None)
        # the basis AFTER uptime/platform (the maxed-account bench is
        # linearised at, and cached by, the resolved config)
        cards, collection_basis, cfg, ma_scenario = srv._apply_basis(
            raw_cards, owned, cfg, basis_name,
            assume_level=A["assume_level"], assume_bloom=A["assume_bloom"])
    except _LoadFailure as exc:
        raise CompareError(503, f"input data error (exit {exc.exit_code})")
    except CollectionError as exc:
        raise CompareError(400, f"CollectionError: {exc}")
    except srv.MaxedAccountUnavailable as exc:
        raise CompareError(503, str(exc))

    by_id = {c.id: c for c in cards}
    raw_by_id = {c.id: c for c in raw_cards}
    ctx = OverrideContext(basis_name=basis_name, assume_level=A["assume_level"],
                          assume_bloom=A["assume_bloom"])
    teams = build_teams(specs, by_id, raw_by_id, ctx)

    # the board each team is scored on
    captured_model = None
    developer: _Developer | None = None
    if board_mode == "captured":
        if ma_scenario is not None:
            # the maxed-account "current" board IS the scenario's bench
            captured_model = None if srv.BOARD_DISABLED else ma_scenario.board_model
        else:
            try:
                captured_model = load_board_basis(
                    None if srv.BOARD_DISABLED else str(srv.REPO_ROOT / srv.BOARD_PATH)).model
            except BoardError as exc:
                raise CompareError(400, f"BoardError: {exc}")
    elif board_mode == "develop":
        developer = _Developer(cfg, cards, basis_name, A["budget"], chart, ma_scenario,
                               assume_bloom=A["assume_bloom"])
    rebuilder: _Developer | None = None
    if board_mode == "state":
        absent = [t["label"] for t in teams if not t.get("board_state")]
        if absent:
            raise CompareError(400, (
                "board \"state\" needs each team's board_state (the row's "
                f"{{chr: {{nodes, slots}}}}); missing for team(s) {absent}"))
        rebuilder = _Developer(cfg, cards, basis_name, A["budget"], chart, ma_scenario,
                               assume_bloom=A["assume_bloom"])

    # certificate meta + basis (the guard tuple; `leaf` added below)
    meta = srv._certificate_meta(cfg, basis_name, scenario=ma_scenario)
    budget_kind = ("none" if board_mode == "off" else "captured" if board_mode == "captured"
                   else "state" if board_mode == "state"
                   else "flat" if A["budget"] is not None else "real")
    cbasis_guard = cert_mod.certificate_basis(
        universe=cert_mod.universe_name(basis=basis_name, pool="all"),
        basis=basis_name, budget_kind=budget_kind,
        budget=A["budget"] if budget_kind == "flat" else None,
        uptime_model=cfg.active_uptime_model, platform=cfg.platform, difficulty=difficulty)

    # THE OBJECTIVE (module docstring): under board=develop, song / across
    # mode, `objective_leaf == "plan_cache"`: one PlanCache for the request,
    # seeded from the per-leader table that certifies this song on this
    # hash + budget/uptime/platform (so A's number and the certificate are
    # the same objective), else from team A.
    objective: dict[str, Any] = {"leaf": "develop"}
    plan_cache: PlanCache | None = None
    if developer is not None and mode in ("song", "across") and A["objective_leaf"] == "plan_cache":
        seed_team: Team = teams[0]["team"]
        seed_source = "team A"
        if mode == "song":
            found = certindex.default_index().plan_cache_seed_for(
                A["song"], meta.get("inputs_hash"), cbasis_guard, ignore=("universe",))
            if found is not None:
                try:
                    seed_team = Team(leader=by_id[found[0]],
                                     members=tuple(by_id[m] for m in found[1]))
                    seed_source = f"per-leader table ({found[2]})"
                except (KeyError, TeamError):
                    pass
        plan_cache = developer.plan_cache_from(seed_team)
        objective = {
            "leaf": "plan_cache",
            "plan_cache_seed": {"leader": seed_team.leader.id,
                                "members": [m.id for m in seed_team.members]},
            "seed_source": seed_source,
            "meaning": ("headline = each team on the FIXED plans of one PlanCache seeded "
                        "from this team (OBJECTIVE-v2 §3, the v2 objective the EXACT "
                        "certificates certify); the per-team greedy re-develop is the "
                        "labelled what-if (develop_whatif)")}
    leaf = objective["leaf"] if developer is not None else None

    cfg_off = dataclasses.replace(cfg, board=None)
    rows: list[dict] = []
    for entry in teams:
        team: Team = entry["team"]
        bt = None
        dev_bt = None          # the per-team greedy re-develop (what-if) under plan_cache
        board_state: dict = {}
        if developer is not None:
            if plan_cache is not None:
                bt = developer.develop(team, plan_cache)
                dev_bt = developer.develop(team)
            else:
                bt = developer.develop(team)
            board_state = developer.board_state_of(bt)
            cfg_team = dataclasses.replace(cfg, board=bt.board)
        elif rebuilder is not None:
            try:
                state_board, _placements = row_detail.board_from_state(
                    team, entry["board_state"], cfg=cfg, skilltree=rebuilder.skilltree,
                    node_effect_ids=rebuilder.effects, tree_for=rebuilder.tree_for,
                    connect_cards=rebuilder.connect_cards,
                    bench_characters=rebuilder.bench_characters,
                    name_by_chr=rebuilder.name_by_chr, sings=rebuilder.cast,
                    # a state that carries its sockets keeps them: re-assigning them
                    # could answer a different board from the row it came from
                    use_slots=any((v or {}).get("slots") for v in entry["board_state"].values()))
            except ValueError as exc:
                raise CompareError(400, f"team {entry['label']}: board_state: {exc}")
            board_state = entry["board_state"]
            cfg_team = dataclasses.replace(cfg, board=state_board)
        elif captured_model is not None:
            cfg_team = dataclasses.replace(cfg, board=captured_model)
        else:
            cfg_team = cfg_off

        panel = panel_lines(team, cfg_team, cfg_off)
        bd = panel.pop("_bd")
        us_off = deck_screen_unit_score(score_with_breakdown(team, cfg_off)).unit_score
        score: dict[str, Any] = {"unit_score": panel["unit_score"], "unit_score_board_off": us_off}
        headline: float
        try:
            if mode == "song":
                # the row's figures, through the ranker's own entry point
                fig = row_detail.figures(team, chart, cfg_team, cfg_off=cfg_off)
                est = fig["song_est"]
                score.update({
                    "song_est": est,
                    "song_off": fig["song_off"],
                    "song_max": fig["song_max"],
                    "member_order": fig["member_order"],
                    "deck_power": fig["deck_power"],
                    "deck_power_basis": fig["deck_power_basis"],
                })
                if entry.get("member_order") and sorted(entry["member_order"]) == sorted(
                        fig["member_order"]):
                    score["member_order_requested_matches"] = (
                        entry["member_order"] == fig["member_order"])
                headline = est
            elif mode == "across":
                per_chart = []
                for ch in all_charts:
                    per_chart.append(score_team_on_chart(team, ch, cfg_team).total_score)
                score.update({"across_mean": sum(per_chart) / len(per_chart),
                              "_per_chart": per_chart})
                headline = score["across_mean"]
            else:
                headline = panel["unit_score"]
        except (BoardError, SongError) as exc:
            raise CompareError(400, f"{type(exc).__name__}: {exc}")
        score["headline"] = headline
        # the deck screen's five Score Bonus lines, in the row's slot order
        split_order = score.get("member_order") or entry.get("member_order")
        if not (split_order and sorted(split_order) == sorted(m.id for m in team.members)):
            split_order = None
        # on a chosen song the deck screen's Board line includes its per-song bonus
        score_bonus = row_detail.score_bonus_lines(
            team, cfg_team, split_order, overall_power=panel["overall_power"],
            chart=chart if mode == "song" else None)
        law = (row_detail.distribution(team, chart, cfg_team, score["member_order"],
                                       score["deck_power"], row_score=score["song_est"])
               if mode == "song" and A.get("distribution") else None)

        row: dict[str, Any] = {
            "label": entry["label"],
            "cards": [team.leader.id, *[m.id for m in team.members]],
            "leader_id": team.leader.id,
            "member_ids": [m.id for m in team.members],
            "leader_member": team.leader.member,
            "members": [m.member for m in team.members],
            "slots": entry["slots"],
            "score": score,
            "panel": panel,
            "gates": srv._team_gates(team, cfg),
            "contributions": _contribution_rows(bd),
            "board_state": board_state,
            "board_mode": board_mode,
            "score_bonus": score_bonus,
        }
        if law is not None:
            row["distribution"] = law
        if bt is not None:
            row["points"] = sum(p.spent for p in bt.plan.per_character.values())
            metric = "song_est" if mode == "song" else "unit_score"
            off = round(score["song_off"]) if mode == "song" else round(us_off)
            dev = round(headline) if mode == "song" else round(panel["unit_score"])
            row["board_plan_summary"] = developer.summary(
                bt, board_state, board_off=off, developed=dev, metric=metric)
            row["board_off_us"] = round(bt.board_off_us)
            row["optimised_us"] = round(bt.optimised_us)
            if leaf is not None:
                row["board_plan_summary"]["leaf"] = leaf
                row["objective_leaf"] = leaf
        if dev_bt is not None:
            # the what-if: the same team on its OWN greedy re-develop
            cfg_dev = dataclasses.replace(cfg, board=dev_bt.board)
            dev_state = developer.board_state_of(dev_bt)
            try:
                if mode == "song":
                    dev_value = score_team_on_chart(team, chart, cfg_dev).total_score
                    score["song_est_develop"] = dev_value
                    dev_metric, off_dev = "song_est", round(score["song_off"])
                else:
                    per = []
                    for ch in all_charts:
                        per.append(score_team_on_chart(team, ch, cfg_dev).total_score)
                    dev_value = sum(per) / len(per)
                    score["across_mean_develop"] = dev_value
                    score["_per_chart_develop"] = per
                    dev_metric, off_dev = "unit_score", round(dev_bt.board_off_us)
            except (BoardError, SongError) as exc:
                raise CompareError(400, f"{type(exc).__name__}: {exc}")
            row["develop_whatif"] = {
                "label": "greedy re-develop for this team", "leaf": "develop",
                ("song_est" if mode == "song" else "across_mean"): round(dev_value),
                "delta_pct": (round(100.0 * (dev_value - headline) / headline, 2)
                              if headline else 0.0)}
            row["points_develop"] = sum(p.spent for p in dev_bt.plan.per_character.values())
            row["board_state_develop"] = dev_state
            row["board_plan_summary_develop"] = {**developer.summary(
                dev_bt, dev_state, board_off=off_dev,
                developed=round(dev_value) if mode == "song" else round(dev_bt.optimised_us),
                metric=dev_metric), "leaf": "develop"}
        rows.append(row)

    # across: wins between THESE two teams
    if mode == "across":
        pa, pb = rows[0]["score"].pop("_per_chart"), rows[1]["score"].pop("_per_chart")
        wa = sum(1 for x, y in zip(pa, pb) if x > y)
        wb = sum(1 for x, y in zip(pa, pb) if y > x)
        rows[0]["score"]["wins"] = wa
        rows[1]["score"]["wins"] = wb
        rows[0]["score"]["ties"] = rows[1]["score"]["ties"] = len(pa) - wa - wb
        for row in rows:
            row["score"].pop("_per_chart_develop", None)

    # per-team certificate (song mode: same-instant-same-basis); the basis
    # names the leaf the headline was scored on
    cbasis = dict(cbasis_guard)
    if leaf is not None:
        cbasis["leaf"] = leaf
    for row in rows:
        hc = certindex.heuristic_certificate(
            model_version=meta["model_version"], inputs_hash=meta.get("inputs_hash"),
            basis=cbasis, generator="hand-picked roster (Compare view)",
            stages=[f"score: {mode}, board {board_mode}"], candidates_evaluated=1)
        if mode == "song":
            row["certificate"] = certindex.attach(
                hc, song=A["song"], leader=row["leader_id"], member_ids=row["member_ids"],
                row_score=round(row["score"]["headline"]))
        else:
            row["certificate"] = hc

    # faces
    states = srv._card_states()
    faces: dict[str, Any] = {}
    for row in rows:
        # the roster, plus every Connect-socket card the developed board placed,
        # so boardstate.js can name the socket cards instead of printing ids
        socketed = [cid for st in row["board_state"].values()
                    for cid in (st.get("slots") or {}).values()]
        for cid in [*row["cards"], *socketed]:
            if cid not in faces and cid in by_id:
                faces[cid] = srv._card_view(by_id[cid], art_dir, srv._state_of(states, by_id[cid]))

    ra, rb = rows
    diff = explain_difference(ra["panel"], rb["panel"], ra["gates"], rb["gates"],
                              ra["score"]["headline"], rb["score"]["headline"], mode)
    diff["panel"] = {key: _delta(ra["panel"].get(key), rb["panel"].get(key))
                     for key, _label, _kind in PANEL_LINES}
    diff["skills"] = _skill_diff(ra["contributions"], rb["contributions"])
    diff["board_points"] = _delta(ra.get("points"), rb.get("points"))
    if mode == "song":
        diff["song_off"] = _delta(ra["score"]["song_off"], rb["score"]["song_off"])
        diff["song_max"] = _delta(ra["score"]["song_max"], rb["score"]["song_max"])
        if "song_est_develop" in ra["score"] and "song_est_develop" in rb["score"]:
            diff["song_est_develop"] = _delta(ra["score"]["song_est_develop"],
                                              rb["score"]["song_est_develop"])
    elif mode == "across":
        if "across_mean_develop" in ra["score"] and "across_mean_develop" in rb["score"]:
            diff["across_mean_develop"] = _delta(ra["score"]["across_mean_develop"],
                                                 rb["score"]["across_mean_develop"])
    if objective["leaf"] == "plan_cache":
        diff["objective_note"] = (
            "headline numbers (A, B) are the v2 objective: each team on the FIXED plans of "
            "one PlanCache (plan-cache leaf); the greedy re-develop what-if per team is "
            "develop_whatif / score.*_develop — a different board, shown second, never "
            "the one ranked by")

    return {
        "mode": mode,
        "song": A["song"] or None,
        "difficulty": difficulty,
        "chart_count": len(all_charts) if mode == "across" else None,
        "assumptions": {
            "basis": basis_name, "assume_level": A["assume_level"],
            "assume_bloom": A["assume_bloom"], "board": board_mode,
            "budget": "real" if A["budget"] is None else A["budget"],
            "platform": cfg.platform, "uptime_model": cfg.active_uptime_model,
            "collection_applied": collection_basis.applied,
            **({"scenario": ma_scenario.summary()} if ma_scenario is not None else {}),
        },
        "panel_lines": [{"key": k, "label": l, "kind": kind} for k, l, kind in PANEL_LINES],
        "teams": rows,
        "cards": faces,
        "diff": diff,
        "certificate_basis": cbasis,
        "objective": objective,
        **meta,
    }
