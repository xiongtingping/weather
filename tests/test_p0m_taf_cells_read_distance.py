"""P0-M's TAF width cells are keyed on how far the TX sits, not which side.

Measured 2026-09-13 on 397 bias-clean P0 decisions at TAF arrival: the TX
bucket settles 1.5x as often as P0 prices it one rung out and 2.3x as often
two or more rungs out, and the warm and cold sides read the same at each
distance.  A direction cell averaged near and far days and split each
distance in two, so none ever reached the layer's hurdles.  Direction stays
as the sign the cell's shift is served along.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import center_width_store as store  # noqa: E402
from bayes_basket_target.contracts import ProbabilityEnvelope  # noqa: E402

POOLED = (0.05, 0.9, "p" * 64)
KEYS = ["26", "27", "28", "29", "30", "31", "32"]
CENTRES = [26.0, 27.0, 28.0, 29.0, 30.0, 31.0, 32.0]


def test_distance_is_counted_in_rungs_of_the_ladder_on_either_side() -> None:
    assert store.taf_cell_for_gap(0.4, 1.0) == ("same", 1)
    assert store.taf_cell_for_gap(-1.2, 1.0) == ("adjacent", -1)
    assert store.taf_cell_for_gap(1.2, 1.0) == ("adjacent", 1)
    assert store.taf_cell_for_gap(-4.4, 1.0) == ("far", -1)
    # A Fahrenheit ladder's rung is 10/9 C: 1.6 C is 1.44 rungs, not far.
    assert store.taf_cell_for_gap(1.6, 10 / 9) == ("adjacent", 1)
    assert store.taf_cell_for_gap(1.6, 1.0) == ("far", 1)


def test_a_tx_less_forecast_is_its_own_cell_and_an_unreadable_ladder_is_none() -> None:
    assert store.taf_cell_for_gap(None, 1.0) == ("none", 0)
    assert store.taf_cell_for_gap(2.0, None) == (None, 0)
    assert store.taf_cell_for_gap(2.0, 0.0) == (None, 0)


def test_a_cells_shift_is_served_along_the_forecasters_direction() -> None:
    cells = {"far": (0.05, 0.4, 1.1)}
    cold, name = store.serve_taf_cell(
        POOLED, cells, tx_c=25.0, root_center_c=29.4, key_centers_c=CENTRES,
    )
    assert name == "far"
    assert cold == (-0.35, 1.1, POOLED[2])
    warm, _ = store.serve_taf_cell(
        POOLED, cells, tx_c=33.0, root_center_c=29.4, key_centers_c=CENTRES,
    )
    # Same width both ways; only the shift turns round.
    assert warm == (0.45, 1.1, POOLED[2])


def test_a_cell_not_yet_admitted_serves_the_pooled_pair_and_still_names_the_day() -> None:
    served, name = store.serve_taf_cell(
        POOLED, {"far": (0.05, 0.4, 1.1)},
        tx_c=29.0, root_center_c=29.4, key_centers_c=CENTRES,
    )
    assert (served, name) == (POOLED, "same")
    # No table cells at all: nothing is classified, nothing is recorded.
    assert store.serve_taf_cell(
        POOLED, None, tx_c=25.0, root_center_c=29.4, key_centers_c=CENTRES,
    ) == (POOLED, None)


def _cell(settled: str, tx: float | None, day: str = "2026-09-10") -> dict:
    return {
        "station_id": "ZUCK",
        "event_date": day,
        "decision_at_utc": f"{day}T01:00:00Z",
        "model_center_c": "29.0",
        "taf_observed_tx_c": "NONE" if tx is None else f"{tx}",
        "probabilities": {
            "26": 0.02, "27": 0.08, "28": 0.2, "29": 0.4,
            "30": 0.2, "31": 0.08, "32": 0.02,
        },
        "ordered_keys": list(KEYS),
        "key_centers_c": list(CENTRES),
        "excluded_buckets": (),
        "settled_bucket": settled,
    }


def test_the_fit_classifies_a_day_from_the_tx_its_row_served() -> None:
    conn = sqlite3.connect(":memory:")  # no capture table: must not be read
    cells = [_cell("29", 25.0), _cell("29", 30.0), _cell("29", 29.2),
             _cell("29", None)]
    store._annotate_taf_cells(conn, cells)
    assert [(c["taf_cell"], c["taf_sign"]) for c in cells] == [
        ("far", -1), ("adjacent", 1), ("same", 1), ("none", 0),
    ]
    for cell in cells[:3]:
        _, name = store.serve_taf_cell(
            POOLED, {"x": (0.0, 0.0, 1.0)},
            tx_c=float(cell["taf_observed_tx_c"]), root_center_c=29.0,
            key_centers_c=CENTRES,
        )
        assert name == cell["taf_cell"]


def test_a_signed_shift_finds_what_an_unsigned_one_averages_away() -> None:
    # Far days where truth follows the forecaster by one rung, half warm
    # and half cold: any unsigned shift helps one half exactly as much as it
    # hurts the other, so only widening is left to it.
    cells = []
    for index in range(20):
        warm = index % 2 == 0
        cell = _cell("30" if warm else "28", 33.0 if warm else 25.0)
        cells.append(cell)
    store._annotate_taf_cells(sqlite3.connect(":memory:"), cells)
    _, _, unsigned_nll = store._search(cells)
    signed_delta, _, signed_nll = store._search(cells, taf_signed=True)
    assert signed_delta > 0.3
    assert signed_nll < unsigned_nll - 0.05


def test_a_retired_two_number_cell_is_never_served() -> None:
    conn = sqlite3.connect(":memory:")
    store.ensure_center_width_schema(conn)
    store.persist_and_activate_center_width(
        conn, stage="P0-M", delta_c=0.05, width_lambda=0.9, evidence={},
        serving_day_count=8, taf_cells={"far": [0.05, 0.4, 1.1]},
    )
    assert store.load_center_width_cells(conn, stage="P0-M") == {
        "far": (0.05, 0.4, 1.1)
    }
    conn.execute(
        "UPDATE calibration_center_width_table SET taf_cells_json=?",
        (json.dumps({"warm": [0.9, 0.8]}),),
    )
    assert store.load_center_width_cells(conn, stage="P0-M") is None


def test_the_digest_names_the_rule_the_cells_were_fitted_under() -> None:
    bare = store.center_width_sha256(stage="P0-M", delta_c=0.05, width_lambda=0.9)
    with_cells = store.center_width_sha256(
        stage="P0-M", delta_c=0.05, width_lambda=0.9,
        taf_cells={"far": [0.05, 0.4, 1.1]},
    )
    assert bare != with_cells
    # A table with no cells keeps its historical digest.
    assert bare == store.center_width_sha256(
        stage="P0-M", delta_c=0.05, width_lambda=0.9, taf_cells=None,
    )
