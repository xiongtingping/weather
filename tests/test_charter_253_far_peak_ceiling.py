"""Charter 253: past 36 hours a P0 vector claims no more than 0.45 on any one
bucket (user ruling 2026-10-04, 「先查峰，修概率」).

A fixed rule on the table layer, last of its steps: the top bucket's excess
over the ceiling goes to its neighbours in proportion to their mass.  One
resolver for serving and the gate; serving applies it exactly where the
artifact's combination method carries the tag; the receipt says what moved;
the width lane keeps fitting on the pre-ceiling vector and journals a nightly
reading.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bayes_basket_target import center_width_calibration as cwc
from bayes_basket_target import center_width_store as store
from bayes_basket_target import native_ensemble_oos as oos
from bayes_basket_target.center_width_calibration import (
    FAR_PEAK_CEILING,
    FAR_PEAK_CEILING_METHOD_TAG,
    FAR_PEAK_MIN_HOURS_TO_SETTLEMENT,
    THIN_FAR_SLICE_NAME,
    apply_center_width_calibration,
    apply_peak_ceiling,
    apply_tail_mix,
    far_peak_ceiling_for,
    far_peak_ceiling_method_tag_suffix,
)
from bayes_basket_target.served_shape import (
    CenterWidthLayer,
    apply_served_table_layers,
    with_peak_ceiling,
    with_width_slice,
)

KEYS = ["23C or below", "24C", "25C", "26C", "27C", "28C", "29C or higher"]
CENTERS = [22.5, 24.0, 25.0, 26.0, 27.0, 28.0, 29.5]
NARROW = dict(zip(KEYS, [0.0, 0.02, 0.16, 0.64, 0.16, 0.02, 0.0]))
FLAT = dict(zip(KEYS, [0.05, 0.10, 0.20, 0.30, 0.20, 0.10, 0.05]))
FOUR = ["a", "b", "c", "d"]


def _vec(values):
    return dict(zip(FOUR, values))


def test_the_rule_and_who_it_reaches() -> None:
    assert FAR_PEAK_CEILING == 0.45 and FAR_PEAK_MIN_HOURS_TO_SETTLEMENT == 36.0
    assert FAR_PEAK_CEILING_METHOD_TAG == "far_peak_ceiling_v1"
    assert far_peak_ceiling_for(hours_to_settlement=36.01) == 0.45
    assert far_peak_ceiling_for(hours_to_settlement=60.0) == 0.45
    assert far_peak_ceiling_for(hours_to_settlement=36.0) is None        # strictly past 36 h
    assert far_peak_ceiling_for(hours_to_settlement=12.0) is None
    # Fails closed on a horizon nobody can state, and where the artifact does not carry the tag.
    assert far_peak_ceiling_for(hours_to_settlement=None) is None
    assert far_peak_ceiling_for(hours_to_settlement=float("nan")) is None
    assert far_peak_ceiling_for(hours_to_settlement=48.0, enabled=False) is None
    assert far_peak_ceiling_method_tag_suffix() == "::far_peak_ceiling_v1"


def test_the_switch_takes_the_rule_off_everywhere(monkeypatch) -> None:
    monkeypatch.setattr(cwc, "FAR_PEAK_CEILING_MODE", "off")
    assert far_peak_ceiling_for(hours_to_settlement=48.0) is None
    assert far_peak_ceiling_method_tag_suffix() == ""
    assert with_peak_ceiling(CenterWidthLayer(0.0, 0.75), hours_to_settlement=48.0, enabled=True).peak_ceiling is None


def test_the_excess_goes_to_the_neighbours_in_proportion() -> None:
    out, receipt = apply_peak_ceiling(_vec([0.10, 0.30, 0.55, 0.05]), ceiling=0.45, ordered_keys=FOUR)
    assert out["c"] == pytest.approx(0.45)
    assert out["b"] == pytest.approx(0.30 + 0.10 * 0.30 / 0.35)
    assert out["d"] == pytest.approx(0.05 + 0.10 * 0.05 / 0.35)
    assert out["a"] == pytest.approx(0.10) and sum(out.values()) == pytest.approx(1.0)
    assert receipt == {
        "schema": "peak_ceiling_receipt_v1", "state": "APPLIED", "peak_ceiling": 0.45,
        "top_bucket": "c", "top_probability_before": pytest.approx(0.55), "moved_mass": pytest.approx(0.10),
    }


def test_nothing_moves_under_the_ceiling() -> None:
    out, receipt = apply_peak_ceiling(_vec([0.2, 0.3, 0.3, 0.2]), ceiling=0.45, ordered_keys=FOUR)
    assert out == _vec([0.2, 0.3, 0.3, 0.2])
    assert receipt["state"] == "IDENTITY" and receipt["moved_mass"] == 0.0
    # The first maximum in axis order is the top.
    assert receipt["top_bucket"] == "b"
    exact, _ = apply_peak_ceiling(_vec([0.25, 0.45, 0.2, 0.1]), ceiling=0.45, ordered_keys=FOUR)
    assert exact == pytest.approx(_vec([0.25, 0.45, 0.2, 0.1]))


def test_a_neighbour_stops_at_the_ceiling_and_the_rest_goes_on() -> None:
    # The left neighbour has room for 0.05 only; the right one, holding nothing, takes the rest.
    out, _ = apply_peak_ceiling(_vec([0.05, 0.40, 0.55, 0.0]), ceiling=0.45, ordered_keys=FOUR)
    assert [out[k] for k in FOUR] == pytest.approx([0.05, 0.45, 0.45, 0.05])
    # Both neighbours full: the next ring out takes it.
    keys = ["a", "b", "c", "d", "e"]
    out, _ = apply_peak_ceiling(dict(zip(keys, [0.0, 0.40, 0.60, 0.0, 0.0])), ceiling=0.45, ordered_keys=keys)
    assert [out[k] for k in keys] == pytest.approx([0.0, 0.45, 0.45, 0.10, 0.0])
    assert max(out.values()) <= 0.45 + 1e-12 and sum(out.values()) == pytest.approx(1.0)


def test_a_ladder_too_short_keeps_the_remainder_on_top() -> None:
    out, receipt = apply_peak_ceiling({"a": 0.9, "b": 0.1}, ceiling=0.45, ordered_keys=["a", "b"])
    assert out["b"] == pytest.approx(0.45) and out["a"] == pytest.approx(0.55)
    assert receipt["moved_mass"] == pytest.approx(0.35)


def test_a_ruled_out_bucket_receives_nothing() -> None:
    out, _ = apply_peak_ceiling(_vec([0.10, 0.30, 0.55, 0.05]), ceiling=0.45, ordered_keys=FOUR,
                                excluded_buckets=("b",))
    assert out["b"] == pytest.approx(0.30) and out["d"] == pytest.approx(0.15)


def test_what_the_transform_refuses() -> None:
    with pytest.raises(ValueError, match="center_width_peak_ceiling_out_of_range"):
        apply_peak_ceiling(_vec([0.1, 0.3, 0.55, 0.05]), ceiling=0.2, ordered_keys=FOUR)
    with pytest.raises(ValueError, match="center_width_bucket_mismatch"):
        apply_peak_ceiling(_vec([0.1, 0.3, 0.55, 0.05]), ceiling=0.45, ordered_keys=["a", "b", "c"])
    with pytest.raises(ValueError, match="center_width_probability_empty"):
        apply_peak_ceiling(_vec([0.0, 0.0, 0.0, 0.0]), ceiling=0.45, ordered_keys=FOUR)


def test_serving_caps_after_the_pair_and_only_past_thirty_six_hours() -> None:
    plain, plain_receipt = apply_served_table_layers(
        NARROW, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=CenterWidthLayer(0.0, 0.75))
    assert max(plain.values()) > 0.45 and "peak_ceiling" not in plain_receipt
    far = with_peak_ceiling(CenterWidthLayer(0.0, 0.75), hours_to_settlement=48.0, enabled=True)
    assert far.peak_ceiling == 0.45
    served, receipt = apply_served_table_layers(NARROW, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=far)
    expected, _ = apply_peak_ceiling(plain, ceiling=0.45, ordered_keys=KEYS)
    assert served == pytest.approx(expected)
    assert max(served.values()) == pytest.approx(0.45) and sum(served.values()) == pytest.approx(1.0)
    assert receipt["peak_ceiling"] == 0.45
    assert receipt["peak_ceiling_receipt"]["state"] == "APPLIED"
    assert receipt["peak_ceiling_receipt"]["top_bucket"] == "26C"
    # The receipt's input is still the width layer's, so the lane re-serves the row to the pre-ceiling vector.
    assert receipt["input_probability"] == pytest.approx(NARROW) and receipt["width_lambda"] == 0.75
    # Inside 36 h, under an artifact without the tag, or with no table layer: untouched.
    near = with_peak_ceiling(CenterWidthLayer(0.0, 0.75), hours_to_settlement=30.0, enabled=True)
    untagged = with_peak_ceiling(CenterWidthLayer(0.0, 0.75), hours_to_settlement=48.0, enabled=False)
    for layer in (near, untagged):
        assert layer.peak_ceiling is None
        same, same_receipt = apply_served_table_layers(NARROW, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=layer)
        assert same == plain and "peak_ceiling" not in same_receipt
    assert with_peak_ceiling(None, hours_to_settlement=48.0, enabled=True) is None
    # A vector already under the ceiling is served bit for bit as the pair serves it.
    flat_plain, _ = apply_served_table_layers(FLAT, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=CenterWidthLayer(0.0, 0.75))
    flat_far, flat_receipt = apply_served_table_layers(FLAT, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=far)
    if max(flat_plain.values()) <= 0.45:
        assert flat_far == flat_plain and flat_receipt["peak_ceiling_receipt"]["state"] == "IDENTITY"


def test_the_ceiling_follows_the_tail_mix() -> None:
    slices = {THIN_FAR_SLICE_NAME: {"max_seated_systems": 3, "min_hours_to_settlement": 36.0,
                                    "tail_mix_weight": 0.2, "tail_mix_scale": 1.5}}
    layer = with_width_slice(CenterWidthLayer(0.0, 0.75), slices, seated_system_count=2, hours_to_settlement=48.0)
    layer = with_peak_ceiling(layer, hours_to_settlement=48.0, enabled=True)
    served, receipt = apply_served_table_layers(NARROW, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=layer)
    pair, _ = apply_center_width_calibration(NARROW, delta_c=0.0, width_lambda=0.75, ordered_keys=KEYS, key_centers_c=CENTERS)
    mixed, _ = apply_tail_mix(pair, weight=0.2, scale=1.5, ordered_keys=KEYS, key_centers_c=CENTERS)
    expected, _ = apply_peak_ceiling(mixed, ceiling=0.45, ordered_keys=KEYS)
    assert served == pytest.approx(expected)
    assert receipt["width_slice"] == THIN_FAR_SLICE_NAME and receipt["peak_ceiling"] == 0.45


def test_the_gate_resolves_the_ceiling_off_the_days_own_horizon() -> None:
    layer = CenterWidthLayer(0.0, 0.75)
    predictions = {
        "A": {"2026-09-10": {"decision_at_utc": "2026-09-08T06:00:00Z"},       # 58 h before the day ends in UTC
              "2026-09-11": {"decision_at_utc": "2026-09-11T06:00:00Z"}},      # 18 h
    }
    far = oos._day_center_width_layer(layer, None, predictions_by_system=predictions, weights={"A": 1.0},
                                      day="2026-09-10", settlement_timezone="UTC")
    near = oos._day_center_width_layer(layer, None, predictions_by_system=predictions, weights={"A": 1.0},
                                       day="2026-09-11", settlement_timezone="UTC")
    unknown = oos._day_center_width_layer(layer, None, predictions_by_system=predictions, weights={"A": 1.0},
                                          day="2026-09-10", settlement_timezone=None)
    assert far.peak_ceiling == 0.45 and near.peak_ceiling is None and unknown.peak_ceiling is None
    assert oos._day_center_width_layer(None, None, predictions_by_system=predictions, weights={"A": 1.0},
                                       day="2026-09-10", settlement_timezone="UTC") is None


def _cell(pre, settled, receipt, *, station="S00", day="2026-09-01", reserved=True):
    return {
        "probabilities": dict(pre), "ordered_keys": KEYS, "key_centers_c": CENTERS,
        "settled_bucket": settled, "excluded_buckets": (), "event_date": day, "station_id": station,
        "decision_at_utc": "2026-08-30T12:00:00Z", "seated_system_count": 5, "hours_to_settlement": 48.0,
        "center_width_receipt": dict(receipt), "reserved_under_current_width": reserved,
    }


def test_the_lane_journals_what_the_ceiling_did() -> None:
    far = with_peak_ceiling(CenterWidthLayer(0.0, 0.75), hours_to_settlement=48.0, enabled=True)
    _served, receipt = apply_served_table_layers(NARROW, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=far)
    pre, plain_receipt = apply_served_table_layers(NARROW, ordered_keys=KEYS, key_centers_c=CENTERS,
                                                   center_width=CenterWidthLayer(0.0, 0.75))
    by_day = {}
    for index in range(4):
        day = f"2026-09-{index + 1:02d}"
        # Truth on a shoulder three times in four: the top lands a quarter of the time.
        by_day[day] = [_cell(pre, "26C" if s % 4 == 0 else "27C", receipt, station=f"S{s:02d}", day=day) for s in range(20)]
        by_day[day].append(_cell(pre, "26C", plain_receipt, station="NEAR", day=day))      # served no ceiling
    reading = store._far_peak_ceiling_reading(by_day)
    assert reading["schema"] == "center_width_far_peak_ceiling_reading_v1"
    assert reading["cell_count"] == 80 and reading["day_count"] == 4 and reading["top_cell_count"] == 80
    assert reading["top_claim_capped"] == pytest.approx(0.45)
    assert reading["top_claim_uncapped"] == pytest.approx(max(pre.values()))
    assert reading["top_hit_rate"] == pytest.approx(0.25)
    gate = reading["gate_capped_vs_uncapped"]
    assert gate["rule"] == store.GATE_RULE and gate["d_logscore"] < 0.0 and gate["lower"] is True
    unreserved = {d: [{**c, "reserved_under_current_width": False} for c in cells] for d, cells in by_day.items()}
    skipped = store._far_peak_ceiling_reading(unreserved)
    assert skipped["cell_count"] == 0 and skipped["skipped"] == {"no_recorded_input": 80}


def test_a_capped_identity_row_is_re_served_to_the_ceilings_input() -> None:
    far = with_peak_ceiling(CenterWidthLayer(0.0, 1.0), hours_to_settlement=48.0, enabled=True)
    _served, receipt = apply_served_table_layers(NARROW, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=far)
    assert receipt["state"] == "IDENTITY" and receipt["peak_ceiling"] == 0.45
    back = store._reserved_under_current_width(receipt, keys=KEYS, centers=CENTERS, open_edge_bounds=None, excluded=())
    assert back == pytest.approx(NARROW)
    plain = {**receipt}
    plain.pop("peak_ceiling")
    assert store._reserved_under_current_width(plain, keys=KEYS, centers=CENTERS, open_edge_bounds=None, excluded=()) is None


def test_serving_the_gate_the_certificate_and_the_charter_say_the_same_thing() -> None:
    root = Path(__file__).resolve().parents[1]
    read = lambda rel: (root / rel).read_text(encoding="utf-8")  # noqa: E731
    p0 = read("bayes_basket_target/native_ensemble_p0.py")
    assert "center_width_layer = with_peak_ceiling(" in p0
    assert "enabled=FAR_PEAK_CEILING_METHOD_TAG in artifact_combination_method" in p0
    # The ceiling is resolved after the slice and before the layers are applied.
    assert p0.index("center_width_layer = with_width_slice(") < p0.index("center_width_layer = with_peak_ceiling(") \
        < p0.index("probability, center_width_receipt = apply_served_table_layers(")
    gate = read("bayes_basket_target/native_ensemble_oos.py")
    assert "+ far_peak_ceiling_method_tag_suffix()" in gate
    assert "return with_peak_ceiling(" in gate
    served = read("bayes_basket_target/served_shape.py")
    assert served.index("apply_tail_mix(") < served.index("shaped, ceiling_receipt = apply_peak_ceiling(")
    from bayes_basket_target import native_ensemble_p0 as P0
    from bayes_basket_target import serving_arithmetic as sa
    assert P0.POSTERIOR_VERSION == "calibrated_native_cross_ensemble_p0_v27"
    declared = sa.version_effective_from("P0", P0.POSTERIOR_VERSION)
    assert declared and declared == sa.version_effective_from("P0-M", "p0m_taf_likelihood_v30") \
        == sa.version_effective_from("P1", "p1_joint_likelihood_v38")
    charter = read("docs/constitution/SYSTEM_CHARTER.md")
    line = next(l for l in charter.splitlines() if l.startswith("253. "))
    for word in ("apply_peak_ceiling", "far_peak_ceiling_v1", "0.45", "36", "with_peak_ceiling",
                 "PM_P0_FAR_PEAK_CEILING", "v27", "v30", "v38", "裁决人：熊先生"):
        assert word in line, word
