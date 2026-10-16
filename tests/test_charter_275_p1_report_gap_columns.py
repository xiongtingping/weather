"""Charter 275: the P1 direct update's two report-gap columns (user ruling
2026-10-17 「上线报文缺失特征」).

``report_gap_h`` and ``report_missing_n`` come from ``p1_report_gap`` on the
AWC lane, strictly by arrival; the fitter writes them into the block as
``report_gap`` and serving reads them exactly where the block has them -- a
block without them serves as before.  The fitter's numpy x is the served
``state_vector`` row by row with them too.
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from bayes_basket_target import p1_direct_update as DU
from bayes_basket_target import p1_observation_likelihood as OL
from bayes_basket_target import p1_report_gap as RG
from bayes_basket_target.contracts import ContractError
from tests.test_charter_150_p1_joint_likelihood import _compile
from tests.test_charter_161_p1_direct_update import _block, _fitter, _with_block
from tests.test_charter_274_p1_evening_columns import PEAK, _evening_block


def _gap_block(seed: int = 11) -> dict:
    block = _evening_block(seed)
    block["report_gap"] = {"schema": DU.REPORT_GAP_SCHEMA, "fill": [1.0, 2.0],
                           "mean": [1.0, 2.0], "scale": [1.5, 3.0]}
    width = len(DU.x_layout(block))
    rng = np.random.default_rng(seed + 2)
    block["x_layout"] = DU.x_layout(block)
    block["theta_rise"] = [float(v) for v in rng.normal(0.0, 0.3, width)]
    block["theta_slope"] = [float(v) for v in rng.normal(0.0, 0.3, width)]
    return block


def _archive(rows) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE reported_trajectory_observations"
                 " (station_id TEXT, source_lane TEXT, valid_at TEXT, canonical_committed_at TEXT)")
    conn.executemany("INSERT INTO reported_trajectory_observations VALUES ('ZZZZ', ?, ?, ?)", rows)
    return conn


def test_the_features_count_only_what_had_arrived():
    rows = []
    for d in range(1, 15):                          # 14 prior days, 24 reports each
        for h in range(24):
            t = f"2026-10-{d:02d}T{h:02d}:50:00Z"
            rows.append(("awc", t, t))
    rows += [("awc", "2026-10-15T00:50:00Z", "2026-10-15T00:51:00Z"),
             ("awc", "2026-10-15T01:50:00Z", "2026-10-15T09:00:00Z"),   # backfilled late
             ("asos_1min", "2026-10-15T02:50:00Z", "2026-10-15T02:51:00Z")]
    context = RG.report_context(_archive(rows), station_id="ZZZZ", event_date="2026-10-15", timezone_name="UTC")
    assert context["rate_per_hour"] == pytest.approx(1.0) and context["rate_days"] == 14
    at_four = RG.report_gap_features(context, arrived_valid_utc=[], as_of_utc="2026-10-15T04:00:00Z")
    # the 01:50 report had not arrived by 04:00 and the ASOS row is not AWC
    assert at_four == {"report_gap_h": pytest.approx(3.1667, abs=1e-4), "report_missing_n": pytest.approx(3.0)}
    late = RG.report_gap_features(context, arrived_valid_utc=[], as_of_utc="2026-10-15T10:00:00Z")
    assert late["report_gap_h"] == pytest.approx(8.1667, abs=1e-4)
    assert late["report_missing_n"] == pytest.approx(8.0)
    # a report the caller holds counts even when the context read predates it
    held = RG.report_gap_features(context, arrived_valid_utc=["2026-10-15T03:50:00Z"],
                                  as_of_utc="2026-10-15T04:00:00Z")
    assert held["report_gap_h"] == pytest.approx(0.1667, abs=1e-4) and held["report_missing_n"] == pytest.approx(2.0)
    assert RG.report_gap_features(None, arrived_valid_utc=[], as_of_utc="2026-10-15T04:00:00Z") == {
        "report_gap_h": None, "report_missing_n": None}


def test_a_station_without_history_reads_the_fleet_rate():
    context = RG.report_context(_archive([]), station_id="ZZZZ", event_date="2026-10-15", timezone_name="UTC")
    assert context["rate_per_hour"] == RG.FLEET_RATE_PER_HOUR
    assert RG.report_gap_features(context, arrived_valid_utc=[], as_of_utc="2026-10-15T06:00:00Z") == {
        "report_gap_h": RG.GAP_CAP_H, "report_missing_n": pytest.approx(6 * RG.FLEET_RATE_PER_HOUR, abs=1e-4)}


def test_the_columns_and_the_layout():
    layout = DU.x_layout(_gap_block())
    assert layout[-2:] == list(DU.REPORT_GAP_COLUMNS)
    assert layout[-5:-2] == list(DU.EVENING_COLUMNS)
    assert DU.x_layout(_evening_block()) == layout[:-2]


def test_the_fitters_x_is_the_served_state_vector_with_the_report_gap_columns():
    fitter = _fitter()
    rng = np.random.default_rng(6)
    rows = 120
    raw = rng.normal(0.0, 2.0, (rows, len(DU.STATE_COLUMNS)))
    raw[rng.random((rows, len(DU.STATE_COLUMNS))) < 0.1] = np.nan
    raw[:, len(DU.FEATURES):] = rng.normal(0.0, 1.0, (rows, len(DU.ROOT_FEATURES)))
    hours = rng.uniform(0.0, 26.0, rows)
    gap = np.column_stack([rng.uniform(0.0, 24.0, rows), rng.uniform(0.0, 10.0, rows)])
    gap[rng.random((rows, 2)) < 0.1] = np.nan
    design = {"raw": raw, "hours": hours, "fleet_peak_local_h": PEAK, "report_gap": gap}
    scaling = fitter.direct_scaling(design, np.arange(rows))
    assert scaling["report_gap"]["schema"] == DU.REPORT_GAP_SCHEMA
    x = fitter.direct_x(design, np.arange(rows), scaling)
    assert x.shape[1] == len(DU.x_layout(scaling))
    for i in range(rows):
        features = {name: (None if np.isnan(raw[i, k]) else float(raw[i, k])) for k, name in enumerate(DU.FEATURES)}
        features.update({name: (None if np.isnan(gap[i, k]) else float(gap[i, k]))
                         for k, name in enumerate(DU.REPORT_GAP_COLUMNS)})
        root = {name: float(raw[i, len(DU.FEATURES) + k]) for k, name in enumerate(DU.ROOT_FEATURES)}
        assert np.allclose(x[i], DU.state_vector(features, root, float(hours[i]), scaling), atol=1e-12), i


def test_the_switch_and_missing_samples_fit_the_form_without_them(monkeypatch):
    fitter = _fitter()
    rows = 10
    design = {"raw": np.zeros((rows, len(DU.STATE_COLUMNS))), "hours": np.ones(rows), "fleet_peak_local_h": PEAK,
              "report_gap": np.ones((rows, 2))}
    monkeypatch.setattr(fitter, "REPORT_GAP_COLUMNS_ON", False)
    assert "report_gap" not in fitter.direct_scaling(design, np.arange(rows))
    monkeypatch.setattr(fitter, "REPORT_GAP_COLUMNS_ON", True)
    assert "report_gap" in fitter.direct_scaling(design, np.arange(rows))
    # samples built before charter 275 carry no values
    empty = {**design, "report_gap": np.full((rows, 2), np.nan)}
    assert "report_gap" not in fitter.direct_scaling(empty, np.arange(rows))


def test_serving_reads_the_report_gap_block_and_records_it():
    root, packets = _compile(_with_block(_gap_block()))
    receipt = json.loads(packets[-1]["expected_envelope"]["lineage"][OL.LINEAGE_KEY])
    assert receipt["update"] == DU.UPDATE
    assert receipt["report_gap"]["schema"] == DU.REPORT_GAP_SCHEMA
    assert set(receipt["report_gap"]["values"]) == set(DU.REPORT_GAP_COLUMNS)
    plain = json.loads(_compile(_with_block(_block()))[1][-1]["expected_envelope"]["lineage"][OL.LINEAGE_KEY])
    assert "report_gap" not in plain


def test_a_malformed_report_gap_block_is_refused():
    block = _gap_block()
    block["report_gap"]["scale"] = [1.0, 0.0]
    with pytest.raises(ContractError):
        DU.validate_block(block)
    block = _gap_block()
    block["x_layout"] = DU.x_layout(_evening_block())
    with pytest.raises(ContractError):
        DU.validate_block(block)
