"""Charter 240: the ranked verdict -- LogScore primary, CRPS structure, Brier
guardrail -- across the whole calibration chain (user ruling 2026-10-01).

The model's target is P(Y=k|X).  No score holds an equal veto: the LogScore
must improve; the ladder CRPS and the bucket Brier must only not get
significantly worse.  One arithmetic for the CRPS everywhere it is read.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target import center_width_store as cws  # noqa: E402
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402

KEYS = ["20C or below", "21C", "22C", "23C", "24C or higher"]
CENTRES = [19.5, 21.0, 22.0, 23.0, 24.5]
P = dict(zip(KEYS, [0.05, 0.20, 0.50, 0.20, 0.05]))


def _reference_crps(served, keys, centres, truth):
    hit = keys.index(truth)
    cumulative = total = 0.0
    for index in range(len(keys) - 1):
        cumulative += served[keys[index]]
        total += (cumulative - (1.0 if index >= hit else 0.0)) ** 2 * (centres[index + 1] - centres[index])
    return total


def test_the_width_lane_scores_the_ladder_crps_on_the_served_vector() -> None:
    log, brier, crps = cws._served_scores(P, ordered_keys=KEYS, settled_bucket="23C", key_centers_c=CENTRES)
    served, _ = floor_probabilities(dict(P))
    assert log == pytest.approx(-math.log(served["23C"]))
    assert crps == pytest.approx(_reference_crps(served, KEYS, CENTRES, "23C"))
    _l, _b, missing = cws._served_scores(P, ordered_keys=KEYS, settled_bucket="23C")
    assert math.isnan(missing)


def test_the_likelihood_lane_shares_the_arithmetic() -> None:
    import fit_p1_observation_likelihood as p1

    served, _ = floor_probabilities(dict(P))
    brier_ref = sum((served[k] - (1.0 if k == "21C" else 0.0)) ** 2 for k in KEYS)
    crps_ref = _reference_crps(served, KEYS, CENTRES, "21C")
    vectors = np.array([[served[k] for k in KEYS]])
    got = p1._guard_scores(vectors, np.array([1]), np.array([CENTRES]))
    assert got[0].tolist() == pytest.approx([brier_ref, crps_ref])


def test_the_oos_gate_ranks_the_scores() -> None:
    climate = {"brier": 0.80, "crps": 0.80, "nll": 2.00}
    assert oos._cross_oos_gate({"brier": 0.81, "crps": 0.81, "nll": 1.99}, climate)["pass"] is True
    assert oos._cross_oos_gate({"brier": 0.50, "crps": 0.50, "nll": 2.00}, climate)["path"] == "REJECTED_PRIMARY"
    assert oos._cross_oos_gate({"brier": 0.70, "crps": 0.83, "nll": 1.50}, climate)["path"] == "REJECTED_CRPS_GUARD"
    assert oos._cross_oos_gate({"brier": 0.83, "crps": 0.70, "nll": 1.50}, climate)["path"] == "REJECTED_BRIER_GUARD"


def test_the_width_gate_ranks_the_scores() -> None:
    clusters = [(f"S{i:02d}", "d") for i in range(30)]
    base = [(1.5, 0.7, 0.6)] * 30
    assert cws._proper_score_gate(base, [(1.4, 0.7, 0.6)] * 30, clusters)["lower"] is True
    assert cws._proper_score_gate(base, [(1.5, 0.6, 0.5)] * 30, clusters)["lower"] is False
    assert cws.GATE_RULE == "logscore_primary_crps_brier_guardrail_station_day_bootstrap_v1"


def test_the_charter_states_the_hierarchy() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    line = next(l for l in charter.splitlines() if l.startswith("240. "))
    for word in ("LogScore", "CRPS", "Brier", "P(Y=k|X)", "诊断指标负责找病",
                 "LOGSCORE_PRIMARY_CRPS_BRIER_GUARD_V3", "logscore_primary_crps_brier_guardrail_station_day_bootstrap_v1"):
        assert word in line, word
