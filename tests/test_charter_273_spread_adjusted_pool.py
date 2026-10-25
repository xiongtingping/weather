"""Charter 273: the spread-adjusted pool (SLP) replaces the P0 width table layer
(user ruling 2026-10-16 「上slp」「全部提前量都上」).

Each seated system's spread factor is multiplied by its horizon's c before
pooling (h0_24 0.6, h24_48 0.8, h48_plus 1.0); the gate pools the same
narrowed vectors, scores no table layer and tags the certificate; serving
narrows exactly under that tag and applies no table layer after the pool.
"""

from __future__ import annotations

import importlib
from datetime import date, timedelta
from pathlib import Path

import pytest

from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target import spread_adjusted_pool as slp


def test_the_rule_by_horizon() -> None:
    assert slp.SLP_METHOD_TAG == "spread_adjusted_pool_v1"
    assert slp.SLP_SHRINK_BY_BIN == {"h0_24": 0.6, "h24_48": 0.8, "h48_plus": 1.0}
    assert slp.slp_shrink_for(5.0) == 0.6
    assert slp.slp_shrink_for(30.0) == 0.8
    assert slp.slp_shrink_for(60.0) == 1.0
    assert slp.slp_shrink_for(None) == 1.0
    assert slp.slp_spread_factor(1.5, 0.8) == pytest.approx(1.2)
    # c = 1 is the identity, even outside the clip; the clip binds otherwise.
    assert slp.slp_spread_factor(7.0, 1.0) == 7.0
    assert slp.slp_spread_factor(0.3, 0.6) == 0.25
    assert slp.slp_serves() and slp.slp_method_tag_suffix() == "::spread_adjusted_pool_v1"


def test_the_switch_takes_the_rule_off(monkeypatch) -> None:
    monkeypatch.setenv("PM_P0_SLP", "off")
    module = importlib.reload(slp)
    try:
        assert not module.slp_serves() and module.slp_method_tag_suffix() == ""
    finally:
        monkeypatch.delenv("PM_P0_SLP")
        importlib.reload(slp)


def _cases(decision_offset_hours: int) -> list[dict]:
    rows = []
    start = date(2026, 7, 1)
    for index in range(20):
        day = start + timedelta(days=index)
        base = 25.0 + 0.1 * index
        decided = (
            f"{day.isoformat()}T00:00:00Z" if decision_offset_hours == 0
            else f"{(day - timedelta(days=1)).isoformat()}T12:00:00Z"
        )
        rows.append({
            "event_date": day.isoformat(),
            "values": [base - 1.0, base - 0.3, base + 0.4, base + 1.1],
            "actual_tmax_c": base + 0.4,
            "transport_provider_id": "OPEN_METEO",
            "holdout_eligible": True,
            "aggregator_snapshot_at_utc": decided,
            "timezone_name": "Europe/London",
            "member_set_sha256": "a" * 64,
            "member_run_content_shas": ["b" * 64],
            "historical_oos_contract_ids": [],
            "evidence_timing_class": oos.HISTORICAL_REPUBLISHER_RECEIPT,
            "historical_closed_day": True,
            "future_event_count": 0,
        })
    return rows


def _sd(vector: dict) -> float:
    keys = [int(k) for k in vector]
    mean = sum(int(k) * p for k, p in vector.items())
    return sum((k - mean) ** 2 * vector[str(k)] for k in keys) ** 0.5


@pytest.mark.parametrize("offset,shrink", [(0, 0.6), (1, 0.8)])
def test_each_holdout_day_carries_its_narrowed_vector_for_the_cross_gate(offset, shrink) -> None:
    result = oos._evaluate_system(
        system_id="OPEN_METEO_TEST", cases=_cases(offset),
        min_train_days=5, min_holdout_days=3,
    )
    assert result["state"] == "OOS_READY"
    for row in result["predictions"]:
        assert row["slp_shrink"] == shrink
        assert _sd(row["slp_probability"]) < _sd(row["probability"])
        # The system's own certificate keeps scoring the unnarrowed vector.
        assert row["metrics"] == oos._metrics(row["probability"], row["actual_tmax_c"], None)
        view = oos._slp_view(row)
        assert view["probability"] is row["slp_probability"]
        assert row["probability"] is not row["slp_probability"]
    with pytest.raises(oos.ContractError):
        oos._slp_view({"probability": {"25": 1.0}})


def test_serving_the_gate_the_certificate_and_the_charter_say_the_same_thing() -> None:
    root = Path(__file__).resolve().parents[1]
    read = lambda rel: (root / rel).read_text(encoding="utf-8")  # noqa: E731
    p0 = read("bayes_basket_target/native_ensemble_p0.py")
    # Narrowed exactly under the tag, before any system is dressed; both
    # dress calls narrow; an artifact naming a width table too fails closed.
    assert 'slp_active = SLP_METHOD_TAG in str(artifact[8] or "")' in p0
    assert p0.count("spread_parameters=_slp_spread(") == 2
    assert p0.index("slp_active = SLP_METHOD_TAG") < p0.index("_calibrated_system_probability(\n                values=")
    assert "spread_adjusted_pool_with_width_table" in p0
    assert '"spread_adjusted_pool_json"' in p0
    gate = read("bayes_basket_target/native_ensemble_oos.py")
    assert 'row["event_date"]: _slp_view(row)' in gate
    assert '"spread_adjusted_pool": slp_serves(),' in gate
    assert "slp_method_tag_suffix()" in gate
    # Under SLP the gate scores no table and pins none.
    block = gate[gate.index("    if slp_serves():\n        # Charter 273"):][:600]
    for name in ("center_width_sha = None", "station_width_sha = None",
                 "gate_center_width_layer = None", "gate_center_width_slices = None"):
        assert name in block
    from bayes_basket_target import native_ensemble_p0 as P0
    from bayes_basket_target import serving_arithmetic as sa
    assert P0.POSTERIOR_VERSION == "calibrated_native_cross_ensemble_p0_v33"
    declared = sa.version_effective_from("P0", "calibrated_native_cross_ensemble_p0_v30")
    assert declared and declared == sa.version_effective_from("P0-M", "p0m_taf_likelihood_v33") \
        == sa.version_effective_from("P1", "p1_joint_likelihood_v41")
    charter = read("docs/constitution/SYSTEM_CHARTER.md")
    line = next(l for l in charter.splitlines() if l.startswith("273. "))
    for word in ("spread_adjusted_pool_v1", "PM_P0_SLP", "0.6", "0.8", "1.0", "v30", "v33", "v41", "裁决人：熊先生"):
        assert word in line, word


def test_the_gate_tags_the_pool_and_the_compiler_serves_it_without_a_table(monkeypatch) -> None:
    """End to end on the mixed-cycle fixture: with the tables active, the
    gate under SLP pins none and tags the certificate; the compiler narrows
    every system and applies no table layer."""

    import sqlite3

    from bayes_basket_target import native_ensemble_p0, served_shape
    from tests.test_native_ensemble_p0 import (
        _install_mixed_cycle_calibrations,
        _mixed_packet,
    )
    from tests.test_served_shape_one_order import _activate_non_identity_tables

    monkeypatch.setattr(oos, "P0_MEMBERSHIP_MODE", "subset_contest")
    conn = sqlite3.connect(":memory:")
    _activate_non_identity_tables(conn)
    _install_mixed_cycle_calibrations(conn)
    methods = [str(row[0]) for row in conn.execute(
        "SELECT combination_method FROM calibration_cross_native_ensemble_artifact"
    )]
    assert methods and all(slp.SLP_METHOD_TAG in m for m in methods)
    assert not any("center_width_sha256=" in m or "station_width_sha256=" in m for m in methods)
    calls: list[object] = []
    real = served_shape.apply_served_table_layers

    def _spy(probability, **kwargs):
        calls.append(kwargs["center_width"])
        return real(probability, **kwargs)

    monkeypatch.setattr(native_ensemble_p0, "apply_served_table_layers", _spy)
    compiled = native_ensemble_p0.compile_native_ensemble_p0(
        conn, forward_packet=_mixed_packet(), decision_at_utc="2026-07-01T13:00:00Z",
    )
    assert calls == [None]
    lineage = compiled["packet"]["native_probability_envelope"]["lineage"]
    assert "center_width_json" not in lineage
    receipt = __import__("json").loads(lineage["spread_adjusted_pool_json"])
    assert receipt["schema"] == slp.SLP_METHOD_TAG
    assert receipt["shrink"] == slp.slp_shrink_for(receipt["hours_to_settlement"])
    assert receipt["bin"] == "h24_48" and receipt["shrink"] == 0.8
