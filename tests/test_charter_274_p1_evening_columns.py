"""Charter 274: the P1 direct update's three evening columns (user ruling
2026-10-16 「按校准改善上线傍晚臂」).

The station clock times the root's headroom over the running high, the hours
past the fleet peak, and their product; the fitter writes them into the block
as ``evening`` and serving reads them exactly where the block has them -- a
block without them serves as before.  The fitter's numpy x is the served
``state_vector`` row by row with them too.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from bayes_basket_target import p1_direct_update as DU
from bayes_basket_target import p1_observation_likelihood as OL
from bayes_basket_target.contracts import ContractError
from tests.test_charter_150_p1_joint_likelihood import _compile
from tests.test_charter_161_p1_direct_update import _block, _fitter, _with_block

PEAK = 13.88


def _evening_block(seed: int = 11) -> dict:
    block = _block(seed=seed)
    block["evening"] = {"schema": DU.EVENING_SCHEMA, "fleet_peak_local_h": PEAK,
                        "mean": [5.0, 1.0, 1.0], "scale": [8.0, 2.0, 3.0]}
    width = len(DU.x_layout(block))
    rng = np.random.default_rng(seed + 1)
    block["x_layout"] = DU.x_layout(block)
    block["theta_rise"] = [float(v) for v in rng.normal(0.0, 0.3, width)]
    block["theta_slope"] = [float(v) for v in rng.normal(0.0, 0.3, width)]
    return block


def test_the_columns_and_the_layout():
    assert DU.x_layout(_evening_block())[-3:] == list(DU.EVENING_COLUMNS)
    assert len(DU.x_layout(_evening_block())) == len(DU.x_layout()) + 3
    assert DU.x_layout(_block()) == DU.x_layout()
    # 20:00 on the fleet clock with 4 h left: 24 - 13.88 - 4 = 6.12 h past the peak
    assert DU.evening_raw(4.0, 2.4, PEAK) == pytest.approx([9.6, 6.12, 6.12 * 2.4])
    assert DU.evening_raw(12.0, 2.4, PEAK)[1] == 0.0          # before the peak


def test_the_fitters_x_is_the_served_state_vector_with_the_evening_columns():
    fitter = _fitter()
    rng = np.random.default_rng(5)
    rows = 120
    raw = rng.normal(0.0, 2.0, (rows, len(DU.STATE_COLUMNS)))
    raw[rng.random((rows, len(DU.STATE_COLUMNS))) < 0.1] = np.nan
    raw[:, len(DU.FEATURES):] = rng.normal(0.0, 1.0, (rows, len(DU.ROOT_FEATURES)))
    hours = rng.uniform(0.0, 26.0, rows)
    design = {"raw": raw, "hours": hours, "fleet_peak_local_h": PEAK}
    scaling = fitter.direct_scaling(design, np.arange(rows))
    assert scaling["evening"]["schema"] == DU.EVENING_SCHEMA and scaling["evening"]["fleet_peak_local_h"] == PEAK
    x = fitter.direct_x(design, np.arange(rows), scaling)
    assert x.shape[1] == len(DU.x_layout(scaling))
    for i in range(rows):
        features = {name: (None if np.isnan(raw[i, k]) else float(raw[i, k])) for k, name in enumerate(DU.FEATURES)}
        root = {name: float(raw[i, len(DU.FEATURES) + k]) for k, name in enumerate(DU.ROOT_FEATURES)}
        assert np.allclose(x[i], DU.state_vector(features, root, float(hours[i]), scaling), atol=1e-12), i


def test_the_switch_fits_the_form_without_them(monkeypatch):
    fitter = _fitter()
    monkeypatch.setattr(fitter, "EVENING_COLUMNS_ON", False)
    rows = 10
    design = {"raw": np.zeros((rows, len(DU.STATE_COLUMNS))), "hours": np.ones(rows), "fleet_peak_local_h": PEAK}
    assert "evening" not in fitter.direct_scaling(design, np.arange(rows))
    # and a clock without a fleet peak fits without them too
    monkeypatch.setattr(fitter, "EVENING_COLUMNS_ON", True)
    assert "evening" not in fitter.direct_scaling({**design, "fleet_peak_local_h": None}, np.arange(rows))


def test_serving_reads_the_evening_block_and_records_it():
    root, packets = _compile(_with_block(_evening_block()))
    envelope = packets[-1]["expected_envelope"]
    receipt = json.loads(envelope["lineage"][OL.LINEAGE_KEY])
    assert receipt["update"] == DU.UPDATE
    assert receipt["evening"]["schema"] == DU.EVENING_SCHEMA
    assert set(receipt["evening"]["values"]) == set(DU.EVENING_COLUMNS)
    plain = json.loads(_compile(_with_block(_block()))[1][-1]["expected_envelope"]["lineage"][OL.LINEAGE_KEY])
    assert "evening" not in plain


def test_a_malformed_evening_block_is_refused():
    block = _evening_block()
    block["evening"]["scale"] = [1.0, 0.0, 1.0]
    with pytest.raises(ContractError):
        DU.validate_block(block)
    block = _evening_block()
    block["x_layout"] = DU.x_layout()                           # layout without the columns
    with pytest.raises(ContractError):
        DU.validate_block(block)
