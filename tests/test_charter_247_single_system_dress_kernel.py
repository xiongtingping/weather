"""Charter 247: a native ensemble's calibrated width is laid onto its members
by widening the kernel (dress_v1), not by stretching the member spacing.

The stretch multiplied every member's distance from the mean by the spread
factor under a fixed 0.5C kernel, so a two-cluster run served two humps.
The dress keeps the members at their own spacing and widens the kernel to
sqrt(s^2 - 1) * d: the same total variance, one smooth shape.  The rule is a
property of the artifact; the trainer's objective, the gate and the server
read it through one resolver.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target import native_ensemble_p0 as p0

ROOT = Path(__file__).resolve().parents[1]
# Two clusters of members: the shape the stretch turns into two humps.
TWO_CLUSTERS = [21.0, 21.2, 21.4, 21.6, 24.8, 25.0, 25.2, 25.4]


def _cases(count=30, *, transport="ECMWF_OPEN_DATA_S3"):
    """Native-shaped cases whose members sit in two clusters, under-dispersed."""

    start = date(2026, 8, 1)
    out = []
    for index in range(count):
        center = 22.0 + 0.2 * index + (0.5 if index % 3 == 0 else -0.3)
        values = [center - 1.0, center - 0.8, center - 0.6, center + 0.6, center + 0.8, center + 1.0]
        out.append({
            "event_date": (start + timedelta(days=index)).isoformat(),
            "values": values,
            "actual_tmax_c": round(center + 0.6 + (2.4 if index % 4 == 0 else -1.6), 1),
            "transport_provider_id": transport,
        })
    return out


def _pstd(values):
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def _modes(probability):
    keys = sorted(probability, key=int)
    masses = [probability[k] for k in keys]
    count = 0
    for i, mass in enumerate(masses):
        left = masses[i - 1] if i else -1.0
        right = masses[i + 1] if i + 1 < len(masses) else -1.0
        if mass >= 0.02 and mass > left and mass >= right:
            count += 1
    return count


def _moments(probability):
    mean = sum(int(k) * v for k, v in probability.items())
    return mean, math.sqrt(sum((int(k) - mean) ** 2 * v for k, v in probability.items()))


def test_the_served_default_is_the_dress_kernel():
    assert oos.SINGLE_SYSTEM_KERNEL_RULE == oos.SINGLE_SYSTEM_KERNEL_RULE_DRESS == "dress_v1"
    assert oos.SINGLE_SYSTEM_KERNEL_RULES == ("stretch_v1", "dress_v1")
    assert oos.KERNEL_BANDWIDTH_C == 0.5


def test_the_dress_widens_the_kernel_by_the_factor_over_the_member_sd():
    d = _pstd(TWO_CLUSTERS)
    dress = {"kernel_rule": "dress_v1"}
    assert oos._member_kernel_bandwidth_c(TWO_CLUSTERS, 2.0, dress) == pytest.approx(math.sqrt(3.0) * d)
    assert oos._member_kernel_bandwidth_c(TWO_CLUSTERS, 1.0, dress) == 0.5
    assert oos._member_kernel_bandwidth_c(TWO_CLUSTERS, 0.8, dress) == 0.5
    # A factor barely over one asks for a kernel under the floor; the floor holds.
    assert oos._member_kernel_bandwidth_c(TWO_CLUSTERS, 1.01, dress) == 0.5
    # The stretch rule, and an artifact minted before the clause (no key), keep the fixed kernel.
    assert oos._member_kernel_bandwidth_c(TWO_CLUSTERS, 2.0, {"kernel_rule": "stretch_v1"}) == 0.5
    assert oos._member_kernel_bandwidth_c(TWO_CLUSTERS, 2.0, {}) == 0.5
    # A lone member is dressed with its training residual under either rule.
    lone = {"kernel_rule": "dress_v1", "train_residual_sigma_c": 1.7}
    assert oos._member_kernel_bandwidth_c([23.0], 3.0, lone) == 1.7
    assert oos._member_kernel_bandwidth_c([23.0], 3.0, {"train_rmse_c": 0.2}) == 0.5


def test_the_dress_keeps_the_total_width_and_removes_the_humps():
    mean = sum(TWO_CLUSTERS) / len(TWO_CLUSTERS)
    spread = 2.0
    corrected = [mean + spread * (v - mean) for v in TWO_CLUSTERS]
    stretched = oos._probabilities(corrected, bandwidth_c=0.5)
    dressed = oos._probabilities(
        corrected, bandwidth_c=oos._member_kernel_bandwidth_c(TWO_CLUSTERS, spread, {"kernel_rule": "dress_v1"}),
    )
    (m_s, sd_s), (m_d, sd_d) = _moments(stretched), _moments(dressed)
    assert m_d == pytest.approx(m_s, abs=0.05)
    assert sd_d == pytest.approx(sd_s, rel=0.03)
    assert _modes(stretched) == 2
    assert _modes(dressed) == 1


def test_an_unknown_rule_is_refused_by_trainer_gate_and_server():
    bad = {"kernel_rule": "wider_v9"}
    with pytest.raises(ContractError, match="kernel_rule_unsupported"):
        oos._member_kernel_bandwidth_c(TWO_CLUSTERS, 2.0, bad)
    fitted = dict(oos._fit_parameters(_cases()))
    fitted["kernel_rule"] = "wider_v9"
    with pytest.raises(ContractError, match="kernel_rule_unsupported"):
        oos._apply_parameters(TWO_CLUSTERS, fitted)
    with pytest.raises(ContractError, match="kernel_rule_unsupported"):
        oos._refit_case_arrays(_cases(), fitted)
    intervals = {str(k): (k - 0.5, k + 0.5) for k in range(15, 35)}
    intervals["14"] = (None, 14.5)
    intervals["35"] = (34.5, None)
    with pytest.raises(ContractError, match="kernel_rule_unsupported"):
        p0._calibrated_system_probability(
            values=TWO_CLUSTERS, intervals=intervals, bias_parameters=fitted,
            spread_parameters=fitted, reliability_parameters=fitted,
        )


def test_the_fit_records_the_rule_and_its_objective_is_the_served_distribution():
    cases = _cases()
    fitted = oos._fit_parameters(cases)
    assert fitted["kernel_rule"] == "dress_v1"
    assert fitted["fit_rule"] == "logloss_shrunk_v1"
    for case in cases:
        served = oos._apply_parameters([float(v) for v in case["values"]], fitted)
        key = oos._temperature_bucket(float(case["actual_tmax_c"]))
        ours = oos._truth_bucket_probability(
            case["values"], case["actual_tmax_c"], fitted, delta_c=0.0, spread=fitted["spread_factor"],
        )
        assert ours == pytest.approx(served.get(key, 0.0), abs=1e-12)
    arrays = oos._refit_case_arrays(cases, fitted)
    for delta, spread in ((0.0, fitted["spread_factor"]), (0.3, 1.7), (-0.6, 0.9), (0.2, 3.5)):
        vec = oos._truth_bucket_probabilities(arrays, delta_c=delta, spread=spread)
        ref = [oos._truth_bucket_probability(c["values"], c["actual_tmax_c"], fitted, delta_c=delta, spread=spread)
               for c in cases]
        assert list(vec) == pytest.approx(ref, abs=1e-14)


def test_serving_and_gate_agree_on_the_ladder_under_either_rule(monkeypatch):
    intervals = {str(k): (k - 0.5, k + 0.5) for k in range(15, 35)}
    intervals["14"] = (None, 14.5)
    intervals["35"] = (34.5, None)
    dress = oos._fit_parameters(_cases())
    monkeypatch.setattr(oos, "SINGLE_SYSTEM_KERNEL_RULE", oos.SINGLE_SYSTEM_KERNEL_RULE_STRETCH)
    stretch = oos._fit_parameters(_cases())
    assert dress["kernel_rule"] == "dress_v1" and stretch["kernel_rule"] == "stretch_v1"
    for fitted in (dress, stretch):
        served = p0._calibrated_system_probability(
            values=TWO_CLUSTERS, intervals=intervals, bias_parameters=fitted,
            spread_parameters=fitted, reliability_parameters=fitted,
        )
        assert served == oos._apply_parameters(TWO_CLUSTERS, fitted, intervals=intervals)
    # The two rules serve different shapes for the same members and the same width.
    stretch_like_dress = dict(stretch, spread_factor=dress["spread_factor"])
    a = oos._apply_parameters(TWO_CLUSTERS, dress)
    b = oos._apply_parameters(TWO_CLUSTERS, stretch_like_dress)
    assert dress["spread_factor"] > 1.0
    assert max(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in set(a) | set(b)) > 0.01


def test_the_research_arm_is_the_stretch_arithmetic(monkeypatch):
    monkeypatch.setattr(oos, "SINGLE_SYSTEM_KERNEL_RULE", oos.SINGLE_SYSTEM_KERNEL_RULE_STRETCH)
    fitted = oos._fit_parameters(_cases())
    mean = sum(TWO_CLUSTERS) / len(TWO_CLUSTERS)
    center = oos._corrected_center(mean, fitted)
    expected = oos._probabilities(
        [center + fitted["spread_factor"] * (v - mean) for v in TWO_CLUSTERS], bandwidth_c=0.5,
    )
    served = oos._apply_parameters(TWO_CLUSTERS, fitted)
    # The gate also names the climate sample's buckets at weight zero.
    for key in set(served) | set(expected):
        assert served.get(key, 0.0) == pytest.approx(expected.get(key, 0.0), abs=1e-12)


def test_charter_247_records_the_rule():
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = charter.split("\n247. ", 1)[1]
    for needle in ("dress_v1", "stretch_v1", "PM_SINGLE_SYSTEM_KERNEL_RULE", "kernel_rule",
                   "_member_kernel_bandwidth_c", "v25", "v28", "v36", "裁决人：熊先生"):
        assert needle in clause
