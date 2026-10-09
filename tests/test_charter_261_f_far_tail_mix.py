"""Charter 261: past 36 hours an F-station P0 vector is served a fixed tail mix
(weight 0.10, scale 1.5) on the pair's output, before the far-peak ceiling
(user ruling 2026-10-09 「落地」).

A fixed rule like the ceiling: one resolver for serving and the gate, applied
exactly where the artifact's combination method carries the tag; the lane's
own admitted slice stands where it applies; the width lane journals a reading.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bayes_basket_target import center_width_calibration as cwc
from bayes_basket_target.center_width_calibration import (
    F_FAR_TAIL_METHOD_TAG,
    F_FAR_TAIL_MIN_HOURS_TO_SETTLEMENT,
    F_FAR_TAIL_MIX_WEIGHT,
    F_FAR_TAIL_SLICE_NAME,
    TAIL_MIX_SCALE,
    THIN_FAR_SLICE_NAME,
    apply_center_width_calibration,
    apply_peak_ceiling,
    apply_tail_mix,
    f_far_tail_method_tag_suffix,
    f_far_tail_weight_for,
    is_f_ladder,
)
from bayes_basket_target.served_shape import (
    CenterWidthLayer,
    apply_served_table_layers,
    with_f_far_tail,
    with_peak_ceiling,
    with_width_slice,
)

F_KEYS = ["69°F or below", "70-71°F", "72-73°F", "74-75°F", "76-77°F", "78-79°F", "80-81°F or higher"]
F_CENTERS = [20.3, 21.4, 22.5, 23.6, 24.7, 25.8, 26.9]
C_KEYS = ["23°C or below", "24°C", "25°C", "26°C", "27°C", "28°C", "29°C or higher"]
C_CENTERS = [22.5, 24.0, 25.0, 26.0, 27.0, 28.0, 29.5]
NARROW = [0.0, 0.02, 0.16, 0.64, 0.16, 0.02, 0.0]


def test_the_rule_and_who_it_reaches() -> None:
    assert (F_FAR_TAIL_MIX_WEIGHT, F_FAR_TAIL_MIN_HOURS_TO_SETTLEMENT, F_FAR_TAIL_SLICE_NAME) == (0.10, 36.0, "f_far")
    assert F_FAR_TAIL_METHOD_TAG == "f_far_tail_mix_v1" and f_far_tail_method_tag_suffix() == "::f_far_tail_mix_v1"
    assert is_f_ladder(F_KEYS) and not is_f_ladder(C_KEYS)
    assert f_far_tail_weight_for(hours_to_settlement=48.0, f_ladder=True) == 0.10
    assert f_far_tail_weight_for(hours_to_settlement=36.01, f_ladder=True) == 0.10
    assert f_far_tail_weight_for(hours_to_settlement=36.0, f_ladder=True) is None, "strictly past 36 h"
    assert f_far_tail_weight_for(hours_to_settlement=48.0, f_ladder=False) is None, "C stations are not reached"
    assert f_far_tail_weight_for(hours_to_settlement=None, f_ladder=True) is None
    assert f_far_tail_weight_for(hours_to_settlement=float("nan"), f_ladder=True) is None
    assert f_far_tail_weight_for(hours_to_settlement=48.0, f_ladder=True, enabled=False) is None


def test_the_switch_takes_the_rule_off_everywhere(monkeypatch) -> None:
    monkeypatch.setattr(cwc, "F_FAR_TAIL_MODE", "off")
    assert not cwc.f_far_tail_mix_serves() and f_far_tail_method_tag_suffix() == ""
    assert f_far_tail_weight_for(hours_to_settlement=48.0, f_ladder=True) is None
    assert with_f_far_tail(CenterWidthLayer(0.0, 0.75), hours_to_settlement=48.0, f_ladder=True, enabled=True).width_slice is None


def test_serving_mixes_the_f_ladder_past_thirty_six_hours_before_the_ceiling() -> None:
    narrow = dict(zip(F_KEYS, NARROW))
    plain, plain_receipt = apply_served_table_layers(narrow, ordered_keys=F_KEYS, key_centers_c=F_CENTERS, center_width=CenterWidthLayer(0.0, 0.75))
    assert plain_receipt["width_slice"] is None
    layer = with_f_far_tail(CenterWidthLayer(0.0, 0.75), hours_to_settlement=48.0, f_ladder=True, enabled=True)
    assert (layer.width_slice, layer.width_slice_tail_weight, layer.width_slice_tail_scale) == ("f_far", 0.10, TAIL_MIX_SCALE)
    layer = with_peak_ceiling(layer, hours_to_settlement=48.0, enabled=True)
    served, receipt = apply_served_table_layers(narrow, ordered_keys=F_KEYS, key_centers_c=F_CENTERS, center_width=layer)
    pair, _ = apply_center_width_calibration(narrow, delta_c=0.0, width_lambda=0.75, ordered_keys=F_KEYS, key_centers_c=F_CENTERS)
    mixed, _ = apply_tail_mix(pair, weight=0.10, scale=TAIL_MIX_SCALE, ordered_keys=F_KEYS, key_centers_c=F_CENTERS)
    expected, _ = apply_peak_ceiling(mixed, ceiling=0.45, ordered_keys=F_KEYS)
    assert served == pytest.approx(expected)
    assert receipt["width_slice"] == "f_far" and receipt["width_slice_tail_weight"] == 0.10 and receipt["peak_ceiling"] == 0.45
    assert receipt["input_probability"] == pytest.approx(narrow), "the lane re-serves the row to the pre-mix vector"
    far = [k for i, k in enumerate(F_KEYS) if abs(i - 3) >= 2]
    assert sum(served[k] for k in far) > sum(plain[k] for k in far), "the far rungs gained mass"
    # C ladder, inside 36 h, untagged, no layer: untouched.
    for layer in (
        with_f_far_tail(CenterWidthLayer(0.0, 0.75), hours_to_settlement=48.0, f_ladder=False, enabled=True),
        with_f_far_tail(CenterWidthLayer(0.0, 0.75), hours_to_settlement=30.0, f_ladder=True, enabled=True),
        with_f_far_tail(CenterWidthLayer(0.0, 0.75), hours_to_settlement=48.0, f_ladder=True, enabled=False),
    ):
        assert layer.width_slice is None
        same, same_receipt = apply_served_table_layers(narrow, ordered_keys=F_KEYS, key_centers_c=F_CENTERS, center_width=layer)
        assert same == plain and same_receipt["width_slice"] is None
    assert with_f_far_tail(None, hours_to_settlement=48.0, f_ladder=True, enabled=True) is None


def test_the_lanes_own_slice_stands_and_the_rule_fills_only_where_none_applies() -> None:
    slices = {THIN_FAR_SLICE_NAME: {"max_seated_systems": 3, "min_hours_to_settlement": 36.0,
                                    "tail_mix_weight": 0.2, "tail_mix_scale": 1.5}}
    thin = with_width_slice(CenterWidthLayer(0.0, 0.75), slices, seated_system_count=2, hours_to_settlement=48.0)
    assert thin.width_slice == THIN_FAR_SLICE_NAME
    both = with_f_far_tail(thin, hours_to_settlement=48.0, f_ladder=True, enabled=True)
    assert both is thin, "the admitted thin-far slice stands; the two never stack"
    wide = with_width_slice(CenterWidthLayer(0.0, 0.75), slices, seated_system_count=6, hours_to_settlement=48.0)
    assert wide.width_slice is None
    filled = with_f_far_tail(wide, hours_to_settlement=48.0, f_ladder=True, enabled=True)
    assert filled.width_slice == "f_far" and filled.width_slice_tail_weight == 0.10
    # A rule that no longer reaches the decision clears only its own slice.
    cleared = with_f_far_tail(filled, hours_to_settlement=30.0, f_ladder=True, enabled=True)
    assert cleared.width_slice is None
    zero = {THIN_FAR_SLICE_NAME: {"max_seated_systems": 3, "min_hours_to_settlement": 36.0, "tail_mix_weight": 0.0, "tail_mix_scale": 1.5}}
    unfit = with_width_slice(CenterWidthLayer(0.0, 0.75), zero, seated_system_count=2, hours_to_settlement=48.0)
    assert with_f_far_tail(unfit, hours_to_settlement=48.0, f_ladder=True, enabled=True).width_slice == "f_far", "a slice at weight zero is no slice"


def test_the_gate_resolves_the_mix_off_the_days_own_ladder_and_horizon() -> None:
    from bayes_basket_target import native_ensemble_oos as oos

    base = CenterWidthLayer(0.0, 0.75)
    preds = {"A": {"2026-10-12": {"decision_at_utc": "2026-10-10T06:00:00Z"}}}
    far_f = oos._day_center_width_layer(base, None, predictions_by_system=preds, weights={"A": 1.0}, day="2026-10-12",
                                        settlement_timezone="America/Chicago", f_ladder=True)
    assert far_f.width_slice == "f_far" and far_f.peak_ceiling == 0.45
    far_c = oos._day_center_width_layer(base, None, predictions_by_system=preds, weights={"A": 1.0}, day="2026-10-12",
                                        settlement_timezone="Asia/Shanghai", f_ladder=False)
    assert far_c.width_slice is None
    near = oos._day_center_width_layer(base, None, predictions_by_system={"A": {"2026-10-10": {"decision_at_utc": "2026-10-10T06:00:00Z"}}},
                                       weights={"A": 1.0}, day="2026-10-10", settlement_timezone="America/Chicago", f_ladder=True)
    assert near.width_slice is None


def test_the_lane_journals_what_the_mix_did() -> None:
    from bayes_basket_target import center_width_store as store

    narrow = dict(zip(F_KEYS, NARROW))
    pair, _ = apply_center_width_calibration(narrow, delta_c=0.0, width_lambda=0.75, ordered_keys=F_KEYS, key_centers_c=F_CENTERS)
    cell = {"station_id": "KDAL", "event_date": "2026-10-12", "probabilities": pair, "ordered_keys": F_KEYS,
            "key_centers_c": F_CENTERS, "excluded_buckets": (), "settled_bucket": "78-79°F", "reserved_under_current_width": True,
            "center_width_receipt": {"width_slice": "f_far", "width_slice_tail_weight": 0.10, "width_slice_tail_scale": 1.5, "peak_ceiling": 0.45}}
    other = dict(cell, center_width_receipt={"width_slice": None}, station_id="KHOU")
    reading = store._f_far_tail_reading({"2026-10-12": [cell, other]})
    assert reading["schema"] == "center_width_f_far_tail_reading_v1" and reading["cell_count"] == 1
    assert reading["tail_said_mixed"] > reading["tail_said_unmixed"] and reading["tail_landed"] == 1.0
    assert "d_logscore" in reading["gate_mixed_vs_unmixed"]


def test_serving_the_gate_the_certificate_and_the_charter_say_the_same_thing() -> None:
    root = Path(__file__).resolve().parents[1]
    read = lambda rel: (root / rel).read_text(encoding="utf-8")  # noqa: E731
    p0 = read("bayes_basket_target/native_ensemble_p0.py")
    assert "center_width_layer = with_f_far_tail(" in p0 and "enabled=F_FAR_TAIL_METHOD_TAG in artifact_combination_method" in p0
    assert p0.index("center_width_layer = with_width_slice(") < p0.index("center_width_layer = with_f_far_tail(") \
        < p0.index("center_width_layer = with_peak_ceiling(")
    gate = read("bayes_basket_target/native_ensemble_oos.py")
    assert "+ f_far_tail_method_tag_suffix()" in gate and gate.count("f_ladder=is_f_ladder(ordered_keys)") == 2
    from bayes_basket_target import native_ensemble_p0 as P0
    from bayes_basket_target import serving_arithmetic as sa
    assert P0.POSTERIOR_VERSION == "calibrated_native_cross_ensemble_p0_v28"
    declared = sa.version_effective_from("P0", P0.POSTERIOR_VERSION)
    assert declared and declared == sa.version_effective_from("P0-M", "p0m_taf_likelihood_v31") \
        == sa.version_effective_from("P1", "p1_joint_likelihood_v39")
    charter = read("docs/constitution/SYSTEM_CHARTER.md")
    clause = charter[charter.index("\n261. ") + 1:]
    for word in ("f_far_tail_mix_v1", "0.10", "36", "with_f_far_tail", "PM_P0_F_FAR_TAIL_MIX", "v28", "v31", "v39", "裁决人：熊先生"):
        assert word in clause, word
