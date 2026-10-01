"""Charter 233: pool seats rank breadth, then the pool weight they keep, before the mint clock.

When no pool seat matches the packet exactly -- a system the packet carries
holds no certificate at the scope yet, or the exact view is dark under the
window gate -- the compile used to rank the activation instant ahead of
breadth, so a pair
re-certified this morning outranked the five-system view certified last
night, and two same-size views minted together fell to the activation id.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_p0  # noqa: E402
from bayes_basket_target.native_ensemble_p0 import (  # noqa: E402
    _activation_rank,
    _activations_ambiguous,
    _selection_weights_by_sha,
    pool_retained_weights,
)
from bayes_basket_target.stable_pool import STABLE_POOL_WEIGHT_RULE  # noqa: E402

POOL = STABLE_POOL_WEIGHT_RULE + "::stable_pool_kappa=0.5::x"
CONTEST = "minimum_variance_shrunk_covariance_v1::x"
IFS = "ECMWF_IFS_ENS_OPEN_PF50"
AIFS = "ECMWF_AIFS_ENS_GLOBAL"
GEFS = "NOAA_GEFS_05"
AIGEFS = "NOAA_AIGEFS_025"
GEM = "ECCC_GEM_GLOBAL_ENS"
ICON = "DWD_ICON_GLOBAL_EPS"
# The packet carries all six; one of them (here ICON) holds no certificate
# at this scope yet, so no pool seat can equal the packet.
PACKET = {IFS, AIFS, GEFS, AIGEFS, GEM, ICON}
FIVE = sorted([IFS, AIFS, GEFS, AIGEFS, GEM])
LAST_NIGHT = "2026-09-26T22:00:00Z"
THIS_MORNING = "2026-09-27T08:00:00Z"


def _activation(activation_id, *, valid_from, method=POOL, generation=3,
                selection="s" * 64):
    row = [None] * 16
    row[0] = activation_id
    row[3] = generation
    row[4] = valid_from
    row[13] = selection
    row[15] = method
    return tuple(row)


def _ranked(items, *, observed=PACKET, pool_first=True, retained=None):
    return sorted(
        items,
        key=lambda item: _activation_rank(
            item,
            observed_set=observed,
            pool_first=pool_first,
            retained_weight=retained,
        ),
        reverse=True,
    )


def test_a_pair_minted_this_morning_no_longer_outranks_last_nights_five() -> None:
    five = (_activation("five", valid_from=LAST_NIGHT), FIVE, [], 2)
    pair = (_activation("pair", valid_from=THIS_MORNING), sorted([IFS, GEFS]), [], 2)
    assert _ranked([pair, five])[0][0][0] == "five"
    assert _ranked([five, pair])[0][0][0] == "five"
    # A three-system view minted in between sits between them.
    three = (_activation("three", valid_from="2026-09-27T02:00:00Z"),
             sorted([IFS, GEFS, GEM]), [], 2)
    assert [item[0][0] for item in _ranked([pair, three, five])] == [
        "five", "three", "pair",
    ]


def test_a_same_size_tie_keeps_the_heavier_systems_whatever_the_id() -> None:
    """Two four-system views minted together: one leaves GEM out, one AIGEFS.

    GEM carries more pool weight than AIGEFS, so the view that keeps GEM is
    served -- under either id order, and without the ambiguity guard refusing.
    """

    drop_gem = [s for s in FIVE if s != GEM]
    drop_aigefs = [s for s in FIVE if s != AIGEFS]
    weights = {
        "a" * 64: {IFS: 0.30, AIFS: 0.30, GEFS: 0.30, AIGEFS: 0.10},
        "b" * 64: {IFS: 0.25, AIFS: 0.30, GEFS: 0.25, GEM: 0.20},
    }
    for gem_id, aigefs_id in (("aaa", "zzz"), ("zzz", "aaa")):
        keeps_aigefs = (_activation(gem_id, valid_from=LAST_NIGHT, selection="a" * 64),
                        drop_gem, [], 2)
        keeps_gem = (_activation(aigefs_id, valid_from=LAST_NIGHT, selection="b" * 64),
                     drop_aigefs, [], 2)
        items = [keeps_aigefs, keeps_gem]
        retained = pool_retained_weights(items, weights)
        ranked = _ranked(items, retained=retained)
        assert ranked[0][1] == drop_aigefs, (gem_id, aigefs_id)
        assert _activations_ambiguous(ranked, pool_first=True) is False
    # Breadth still outranks the weight a smaller view keeps.
    five = (_activation("five", valid_from=LAST_NIGHT, selection="c" * 64), FIVE, [], 2)
    retained = pool_retained_weights([five, keeps_gem], {**weights, "c" * 64: {s: 0.2 for s in FIVE}})
    assert _ranked([keeps_gem, five], retained=retained)[0][0][0] == "five"


def test_riders_and_the_fresh_count_still_rank_ahead_of_breadth() -> None:
    five_with_rider = (_activation("five", valid_from=LAST_NIGHT), FIVE, [IFS], 2)
    four_exact_cycles = (_activation("four", valid_from=LAST_NIGHT),
                         [s for s in FIVE if s != GEM], [], 2)
    assert _ranked([five_with_rider, four_exact_cycles])[0][0][0] == "four"
    five_older_runs = (_activation("five", valid_from=LAST_NIGHT), FIVE, [], 1)
    four_fresh_runs = (_activation("four", valid_from=LAST_NIGHT),
                       [s for s in FIVE if s != GEM], [], 2)
    assert _ranked([five_older_runs, four_fresh_runs])[0][0][0] == "four"


def test_an_exact_packet_match_still_ranks_first() -> None:
    observed = set(FIVE)
    exact = (_activation("exact", valid_from=LAST_NIGHT), FIVE, [], 2)
    newer_four = (_activation("four", valid_from=THIS_MORNING),
                  [s for s in FIVE if s != GEM], [], 2)
    assert _ranked([newer_four, exact], observed=observed)[0][0][0] == "exact"


def test_contest_rows_keep_their_order() -> None:
    """The mint clock still ranks ahead of breadth for contest artifacts."""

    five = (_activation("five", valid_from=LAST_NIGHT, method=CONTEST), FIVE, [], 2)
    pair = (_activation("pair", valid_from=THIS_MORNING, method=CONTEST),
            sorted([IFS, GEFS]), [], 2)
    assert _ranked([five, pair], pool_first=False)[0][0][0] == "pair"
    assert _ranked([five, pair], pool_first=True)[0][0][0] == "pair"
    # A pool seat still outranks every contest row.
    pool_pair = (_activation("pool-pair", valid_from=LAST_NIGHT), sorted([IFS, GEFS]), [], 2)
    assert _ranked([five, pair, pool_pair])[0][0][0] == "pool-pair"


def test_the_retained_weight_is_the_mean_weight_over_same_size_views() -> None:
    views = [
        (_activation("v1", valid_from=LAST_NIGHT, selection="1" * 64), sorted([IFS, GEFS, GEM]), [], 0),
        (_activation("v2", valid_from=LAST_NIGHT, selection="2" * 64), sorted([IFS, GEFS, AIGEFS]), [], 0),
        # Its evidence is missing: it is scored on the others' means.
        (_activation("v3", valid_from=LAST_NIGHT, selection="3" * 64), sorted([IFS, GEM, AIGEFS]), [], 0),
        (_activation("pair", valid_from=LAST_NIGHT, selection="4" * 64), sorted([IFS, GEFS]), [], 0),
    ]
    weights = {
        "1" * 64: {IFS: 0.5, GEFS: 0.3, GEM: 0.2, "OFFICIAL_AIRPORT_TAF_TX": 0.9},
        "2" * 64: {IFS: 0.3, GEFS: 0.3, AIGEFS: 0.4},
        "4" * 64: {IFS: 0.6, GEFS: 0.4},
    }
    retained = pool_retained_weights(views, weights)
    ifs, gefs, gem, aigefs = 0.4, 0.3, 0.2, 0.4
    assert retained["v1"] == pytest.approx(ifs + gefs + gem)
    assert retained["v2"] == pytest.approx(ifs + gefs + aigefs)
    assert retained["v3"] == pytest.approx(ifs + gem + aigefs)
    # A different size is averaged apart.
    assert retained["pair"] == pytest.approx(1.0)
    assert pool_retained_weights([], weights) == {}


def test_the_seat_weights_are_read_in_bounded_batches() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_cross_native_ensemble_selection_evidence ("
        " content_sha256 TEXT NOT NULL UNIQUE, selected_weights_json TEXT)"
    )
    rows = [(f"{index:064d}", json.dumps({IFS: 0.5, GEFS: 0.5, "note": "x"}))
            for index in range(1_203)]
    rows.append(("f" * 64, "not json"))
    conn.executemany(
        "INSERT INTO calibration_cross_native_ensemble_selection_evidence VALUES (?,?)",
        rows,
    )
    shas = [row[0] for row in rows] + ["", "e" * 64]
    weights = _selection_weights_by_sha(conn, shas)
    assert len(weights) == 1_203
    assert weights["0" * 64] == {IFS: 0.5, GEFS: 0.5}
    assert "f" * 64 not in weights and "e" * 64 not in weights


def test_the_compile_hands_the_read_weights_to_the_rank() -> None:
    source = Path(native_ensemble_p0.__file__).read_text(encoding="utf-8")
    call = source.split("compatible_activations.sort(", 1)[1].split("reverse=True", 1)[0]
    assert "retained_weight=retained_weight" in call
    assert "pool_retained_weights(" in source.split("compatible_activations.sort(", 1)[0]


def test_charter_233_names_the_rule_the_code_serves() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = charter.split("\n233. ", 1)[1]
    assert "占号" not in clause.split("\n", 1)[0]
    assert "席位数" in clause and "保留权重" in clause
    assert "_activation_rank" in clause and "pool_retained_weights" in clause
    assert "P0 v20 / P0-M v22 / P1 v31" in clause
    assert "裁决人：用户" in clause
    clause_134 = charter.split("\n134. ", 1)[1].split("\n135. ", 1)[0]
    assert "第 233 条补记" in clause_134
    clause_204 = charter.split("\n204. ", 1)[1].split("\n205. ", 1)[0]
    assert "第 233 条更正" in clause_204
