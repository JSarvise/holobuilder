"""The Holomem Board's Score Bonus is a SEPARATE multiplicative factor.

`Vision.LiveCore!LiveScoreCalculator::CalcScore` in the game client ends in

    ... x (1 + SkillScoreUpRatio) x (1 + ScoreUpBonusRatioBySkillTree)

The deck screen *displays* the two as summed percentage-point lines; the score
computation multiplies them. Summing them into one Score Bonus understates
every team.

Each test below pins a number computed by hand in its own docstring, never
copied from the implementation, and asserts against the summed reading too.
A direction-only test would pass under either rule: at a typical board share
the two differ by roughly one percent of the total, so only pinned numbers
separate them.

The per-team AGGREGATION (a plain sum over the five members) is inferred, not
read from the client. The tests that pin it say so.
"""

from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.scoring import score_card_set, score_fast, score_with_breakdown

from conftest import make_card, make_scoring_config, make_skill, make_team

# conftest's STATS baseline is 300 total per card, so five members are 1500.0.
BASE_STAT_SUM = 1500.0

# The ACTIVE uptime is the finite activation lattice over the 128.8 s reference
# run (`scoring._finite_window_uptime`), never the steady-state `duration /
# interval`. A 10 s effect on a 10 s cooldown abuts itself: the first check is
# one whole cooldown in, at t = 10, and from there the windows tile the rest of
# the run, so the union is everything except the opening dead zone —
#
#     (128.8 - 10) / 128.8 = 118.8/128.8 = 0.922360248447205   (steady state: 1.0)
#
# * Medium(0.46) * _as_rate(50.0) = 0.2121428571428571   (steady state: 0.23)
ACTIVE_RATE = (128.8 - 10.0) / 128.8 * 0.46 * 0.50


def _cfg(board_by_holomem: dict | None = None):
    return dataclasses.replace(
        make_scoring_config(), board_score_bonus=board_by_holomem or {}
    )


def _scorer(card_id: str = "member_active", *, member: str = "Nene"):
    return make_card(
        card_id,
        member=member,
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=10.0,
            interval=10.0,
            probability_tier="Medium",
            magnitude=50.0,
        ),
    )


def _members(*extra):
    filler = [make_card(f"m{index}") for index in range(len(extra), 5)]
    return (*extra, *filler)


def _team(*extra):
    return make_team([make_card("outfit"), *_members(*extra)])


def test_the_board_multiplies_the_skill_bonus_and_is_never_summed_into_it():
    """One ACTIVE at 0.2121428571... (ACTIVE_RATE above carries the uptime
    derivation) and a board contributing 0.40 on the same holomem.

    Multiplicative (client)   : 1500 * 1.2121428571... * 1.40 = 2545.5
    Summed (what one combined  : 1500 * (1 + 0.2121428571... + 0.40)
    Score Bonus would give)                = 1500 * 1.6121428571... = 2418.214285...

    127.285714... points apart — 5.0 % of the total, well clear of a rounding
    argument."""
    team = _team(_scorer())
    total = score_fast(team, _cfg({"Nene": 0.40}))

    assert total == pytest.approx(2545.5)
    assert total != pytest.approx(2418.214285)
    # 2583.0 / 2445.0 is the steady-state pair, computed from
    # `min(1, duration / interval) = 1.0` rather than the finite window.
    assert total != pytest.approx(2583.0)
    assert total != pytest.approx(2445.0)


def test_the_board_factor_multiplies_the_whole_score_not_only_the_skill_pool():
    """A team with no score-pool skill at all still gets the board factor:
    `CalcScore` multiplies `DeckPower` by both trailing factors, so a bare
    1500.0 stat sum becomes 1500 * 1.0 * 1.40 = 2100.0.

    This is the structural half of the claim. If the board were folded into
    `combined_score_rate` the answer would be the same here (1500 * 1.40) —
    which is exactly why the previous test, where a skill bonus is also
    present, is the one that separates the two readings, and why both are
    needed."""
    assert score_fast(_team(), _cfg({"Member m0": 0.40})) == pytest.approx(2100.0)


def test_an_empty_board_dict_is_exactly_the_identity_and_reports_zero():
    """The shipped default. `data/` holds no board data, so the new
    stage must be a bit-exact no-op — `==`, not `approx`: a structural
    identity that only holds to a tolerance is not an identity.

    1500 × 1.2121428571... = 1818.214285..., the whole of it from the cards."""
    team = _team(_scorer())

    assert score_fast(team, _cfg()) == BASE_STAT_SUM * (1.0 + ACTIVE_RATE)
    # 1845.0 (= 1500 × 1.23) is the steady-state answer.
    assert score_fast(team, _cfg()) != pytest.approx(1845.0)
    assert score_with_breakdown(team, _cfg()).board_score_rate == 0.0


def test_the_board_is_keyed_by_holomem_never_by_card_id():
    """The board is per-character, so it reaches every skin of that character
    (the same keying `active_cooldown_reduction` uses). An entry
    keyed by the CARD id must therefore do nothing at all — silently reading
    a card-keyed dict would look like it worked on single-skin holomem and
    fail on exactly the cards a collection has several of."""
    team = _team(_scorer("member_active", member="Nene"))
    by_card_id = score_fast(team, _cfg({"member_active": 0.40}))

    assert by_card_id == BASE_STAT_SUM * (1.0 + ACTIVE_RATE)
    assert by_card_id != pytest.approx(2545.5)
    # 2583.0 is the steady-state value of that board-applied total; 1845.0
    # the steady-state value of the answer this test expects.
    assert by_card_id != pytest.approx(2583.0)
    assert by_card_id != pytest.approx(1845.0)


def test_the_leader_slot_contributes_no_board_term():
    """The leader contributes its outfit skill and nothing else. A
    board entry for a holomem who leads but is not among the five is not in
    the deck's five boards, so it contributes nothing.

    ⚠ Inferred, like the sum rule below: the client states the term, not
    which slots feed it. If a leader-scoped board term is ever confirmed this
    test is the one that must change."""
    leader = make_card("outfit", member="Ina")
    team = make_team([leader, *_members(_scorer())])
    total = score_fast(team, _cfg({"Ina": 0.40}))

    assert total == BASE_STAT_SUM * (1.0 + ACTIVE_RATE)
    # 1845.0 (= 1500 × 1.23) is the steady-state answer.
    assert total != pytest.approx(1845.0)


def test_several_members_board_entries_sum_into_one_factor():
    """⚠ Inferred aggregation. Three members at 0.10, 0.25 and 0.05 give a
    single factor of 1.40, not three nested factors:

        summed   (implemented) : 1500 * 1.2121428571... * 1.40  = 2545.5
        compounded (rejected)  : 1500 * 1.2121428571...
                                      * 1.10 * 1.25 * 1.05      = 2625.046875

    Nothing read from the game says which; the sum is the natural reading of
    a single deck-level float and is what is implemented. Pinned here so that
    changing it is a deliberate act with a failing test attached, not a
    silent drift."""
    team = _team(
        _scorer("member_active", member="Nene"),
        make_card("m_b", member="Ina"),
        make_card("m_c", member="Kobo"),
    )
    total = score_fast(team, _cfg({"Nene": 0.10, "Ina": 0.25, "Kobo": 0.05}))

    assert total == pytest.approx(2545.5)
    assert total != pytest.approx(2625.046875)
    # 2583.0 is the steady-state answer, and the expression below its own
    # compounded twin.
    assert total != pytest.approx(2583.0)
    assert total != pytest.approx(1500.0 * 1.23 * 1.10 * 1.25 * 1.05)


def test_the_breakdown_reports_the_two_factors_separately_and_they_reconstruct_the_total():
    """Inspectability: a reader must be able to recompute the total
    from the reported parts. Reported as two factors because the game keeps
    them as two — one combined Score Bonus of 0.6121428571... would
    reconstruct 2418.214285..., a number the scorer never produced."""
    breakdown = score_with_breakdown(_team(_scorer()), _cfg({"Nene": 0.40}))

    assert breakdown.combined_score_rate == pytest.approx(0.212142857)
    # 0.23 is the steady-state rate (uptime 10/10 = 1.0).
    assert breakdown.combined_score_rate != pytest.approx(0.23)
    assert breakdown.board_score_rate == pytest.approx(0.40)
    assert breakdown.total_score == pytest.approx(
        breakdown.effective_stat_sum
        * (1.0 + breakdown.combined_score_rate)
        * (1.0 + breakdown.board_score_rate)
    )


def test_batch_and_single_paths_stay_bit_identical_with_a_board_bonus():
    """`score_card_set` hoists `_board_score_rate` out of its
    per-leader tail because it is member-set-invariant; that hoist must not
    cost bit-identical parity, so this is `==` and never `approx`."""
    outfit = make_card("outfit")
    members = _members(
        _scorer("member_active", member="Nene"), make_card("m_b", member="Ina")
    )
    cfg = _cfg({"Nene": 0.13, "Ina": 0.27})

    batch = score_card_set(members, (outfit,), cfg)
    single = score_fast(make_team([outfit, *members]), cfg)

    assert len(batch) == 1
    assert batch[0].total_score == single


# ==========================================================================
# The per-song Y buckets
# ==========================================================================


def _y(scope: str, value: int) -> str:
    return (
        "skill_tree_effect-live_score_bonus_add_permil_up_by_music_skill_tree"
        f"_character_and_music_singer_type-{value}-skill_tree_effect_passive"
        f"_trigger-music_skill_tree_character_and_music_singer_type-{scope}"
        f"-always-{scope}"
    )


def _board_with(rows):
    """`{holomem: [(scope, permil), ...]}` -> a BoardModel."""
    from hbuilder.domain.board import BoardModel, BoardNode, CharacterBoard, parse_effect_id

    return BoardModel(
        characters={
            holomem: CharacterBoard(
                holomem=holomem,
                nodes=tuple(
                    BoardNode(
                        node_id=f"Y-{index:03d}",
                        effects=(parse_effect_id(_y(scope, value), branch="Y"),),
                    )
                    for index, (scope, value) in enumerate(entries, start=1)
                ),
            )
            for holomem, entries in rows.items()
        }
    )


def _contribution(model):
    from hbuilder.domain import board as board_module

    return board_module.resolve(model, None, ())


def test_a_solo_song_counts_only_its_one_singer():
    """`resolve` must not pool the song buckets across every character:
    type 19 aggregates PER SONG over that song's own singers — and a solo song
    has exactly one.

    Three holomem with 40 permil each pool to 120, which would read as a cap
    violation against the limit of 100. Per singer it is 40, nowhere near the
    cap."""
    contribution = _contribution(
        _board_with({"A": [("solo", 40)], "B": [("solo", 40)], "C": [("solo", 40)]})
    )

    assert contribution.song_score_bonus["solo"] == pytest.approx(0.120)  # pooled
    assert contribution.song_score_bonus_for(["A"], scope="solo") == pytest.approx(0.040)
    assert contribution.song_score_bonus_for(["B"], scope="solo") == pytest.approx(0.040)


def test_a_group_song_counts_exactly_its_own_singers():
    contribution = _contribution(
        _board_with({"A": [("group", 40)], "B": [("group", 40)], "C": [("group", 40)]})
    )
    assert contribution.song_score_bonus_for(["A", "B"], scope="group") == pytest.approx(0.080)
    # A holomem who is not singing contributes nothing, however invested.
    assert contribution.song_score_bonus_for(["A"], scope="group") == pytest.approx(0.040)


def test_the_cap_is_per_song_and_per_cast_never_account_wide():
    """100 permil, the client's cap. Three singers at 40 each cross it; two do not."""
    contribution = _contribution(
        _board_with({n: [("group", 40)] for n in ("A", "B", "C")})
    )
    assert contribution.song_score_bonus_for(["A", "B"], scope="group") == pytest.approx(0.080)
    assert contribution.song_score_bonus_for(["A", "B", "C"], scope="group") == pytest.approx(
        0.100
    )


def test_song_bonus_accepts_the_games_chr_ids_not_only_names():
    """The real-data case the abstract-key tests above cannot catch: they key
    the board and the singers with the SAME names, so they never cross the
    boundary where `charts.json` singers are chr-ids ('chr-00012') and the board
    is name-keyed ('Oozora Subaru'). Fed chr-ids, a name-keyed lookup misses and
    `score_song` would score the per-song board at 0 on every real chart. A BoardModel
    carrying `name_by_chr` must translate, so a chr-id singer scores exactly as its
    name -- and an unknown id misses without inventing a match."""
    from hbuilder.domain.board import BoardModel, BoardNode, CharacterBoard, parse_effect_id

    model = BoardModel(
        characters={
            "Oozora Subaru": CharacterBoard(
                holomem="Oozora Subaru",
                nodes=(BoardNode(node_id="Y-001",
                                 effects=(parse_effect_id(_y("solo", 40), branch="Y"),)),),
            )
        },
        name_by_chr={"chr-00012": "Oozora Subaru"},
    )
    assert model.song_score_bonus_for(["Oozora Subaru"], scope="solo") == pytest.approx(0.040)
    assert model.song_score_bonus_for(["chr-00012"], scope="solo") == pytest.approx(0.040), \
        "chr-id singers (charts.json's format) must translate to the board's name key"
    # an unknown id passes through and simply misses -- never a wrong match
    assert model.song_score_bonus_for(["chr-99999"], scope="solo") == pytest.approx(0.0)
    # a name-only caller with no map is unchanged: the name still works, and a
    # chr-id (which nothing translates) misses.
    plain = BoardModel(characters=model.characters)
    assert plain.song_score_bonus_for(["Oozora Subaru"], scope="solo") == pytest.approx(0.040)
    assert plain.song_score_bonus_for(["chr-00012"], scope="solo") == pytest.approx(0.0)


def test_a_repeated_singer_is_not_counted_twice():
    contribution = _contribution(_board_with({"A": [("solo", 40)]}))
    assert contribution.song_score_bonus_for(["A", "A"], scope="solo") == pytest.approx(0.040)


def test_the_all_scope_agrees_with_the_pooled_reading_by_construction():
    """A whole-roster song's cast IS every character, so the per-song answer
    and `score_bonus_rate()`'s pooled one must coincide — not by luck, but
    because the two are the same sum over the same set."""
    contribution = _contribution(
        _board_with({"A": [("all", 3)], "B": [("all", 4)]})
    )
    assert contribution.score_bonus_rate() == pytest.approx(0.007)
    assert contribution.song_score_bonus_for(["A", "B"], scope="all") == pytest.approx(0.007)


def test_an_absent_breakdown_returns_zero_rather_than_inventing_one():
    """A `BoardContribution` built without the per-holomem mapping must not
    fall back to the pooled total — that would silently reintroduce exactly
    the over-count of pooling across singers."""
    from hbuilder.domain.board import BoardContribution

    bare = BoardContribution(
        flat_by_card={}, rate_by_card={}, skill_effect_up=0.0,
        conditional_skill_effect_up=0.0, activation_rate_up={},
        cooldown_shorten={}, song_score_bonus={"solo": 0.120},
    )
    assert bare.song_score_bonus_for(["A"], scope="solo") == 0.0
