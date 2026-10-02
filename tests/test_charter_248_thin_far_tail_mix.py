"""Charter 248: the thin-pool far-lead slice is a tail mix on the stage pair
(user ruling 2026-10-03, 「修薄池远端，方向是尾重不是整体宽度，且只限『≤3 家 × >36h』」).

One predicate for fit, serving and gate (charter 239's); the slice no longer
replaces the stage lambda -- the decision is served the pair like every
other and then p' = (1-w) p + w W_kappa(p) with kappa fixed at 1.5; the
receipt says so; the row's sha carries the weight and scale; the lane fits
the weight on the fixed grid and admits under the stage's ranked verdict and
a fold majority; the veto reads the same rule; the retired 239 form reads as
no slice.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import center_width_store as store
from bayes_basket_target.center_width_calibration import (
    MAX_TAIL_MIX_WEIGHT,
    TAIL_MIX_SCALE,
    TAIL_MIX_WEIGHT_GRID,
    THIN_FAR_MAX_SEATED_SYSTEMS,
    THIN_FAR_MIN_HOURS_TO_SETTLEMENT,
    THIN_FAR_SLICE_NAME,
    apply_center_width_calibration,
    apply_tail_mix,
    distribution_sigma_rungs_v1,
    thin_far_slice_applies,
    width_slice_for,
)
from bayes_basket_target.served_shape import (
    CenterWidthLayer,
    apply_served_table_layers,
    with_width_slice,
)

KEYS = ["23C or below", "24C", "25C", "26C", "27C", "28C", "29C or higher"]
CENTERS = [22.5, 24.0, 25.0, 26.0, 27.0, 28.0, 29.5]
NARROW = dict(zip(KEYS, [0.0, 0.02, 0.16, 0.64, 0.16, 0.02, 0.0]))
WIDE = dict(zip(KEYS, [0.12, 0.14, 0.16, 0.18, 0.16, 0.14, 0.10]))
SLICES = {THIN_FAR_SLICE_NAME: {
    "max_seated_systems": 3, "min_hours_to_settlement": 36.0,
    "tail_mix_weight": 0.2, "tail_mix_scale": 1.5,
}}
LEGACY = {THIN_FAR_SLICE_NAME: {
    "max_seated_systems": 3, "min_hours_to_settlement": 36.0, "width_lambda": 1.1,
}}


def test_the_constants_and_the_predicate() -> None:
    assert TAIL_MIX_SCALE == 1.5 and MAX_TAIL_MIX_WEIGHT == 0.3
    assert TAIL_MIX_WEIGHT_GRID == (0.0, 0.05, 0.10, 0.15, 0.20, 0.30)
    assert THIN_FAR_MAX_SEATED_SYSTEMS == 3 and THIN_FAR_MIN_HOURS_TO_SETTLEMENT == 36.0
    assert thin_far_slice_applies(seated_system_count=2, hours_to_settlement=40.0)
    assert not thin_far_slice_applies(seated_system_count=4, hours_to_settlement=60.0)
    assert not thin_far_slice_applies(seated_system_count=3, hours_to_settlement=36.0)
    assert width_slice_for(SLICES, seated_system_count=2, hours_to_settlement=48.0) == (THIN_FAR_SLICE_NAME, 0.2, 1.5)
    assert width_slice_for(SLICES, seated_system_count=6, hours_to_settlement=48.0) is None
    assert width_slice_for(None, seated_system_count=2, hours_to_settlement=48.0) is None
    # A zero weight is no slice; an out-of-range weight or scale is refused.
    zero = {THIN_FAR_SLICE_NAME: {**SLICES[THIN_FAR_SLICE_NAME], "tail_mix_weight": 0.0}}
    assert width_slice_for(zero, seated_system_count=2, hours_to_settlement=48.0) is None
    with pytest.raises(ValueError):
        width_slice_for({THIN_FAR_SLICE_NAME: {"tail_mix_weight": 0.5}}, seated_system_count=2, hours_to_settlement=48.0)
    with pytest.raises(ValueError):
        width_slice_for({THIN_FAR_SLICE_NAME: {"tail_mix_weight": 0.1, "tail_mix_scale": 3.0}}, seated_system_count=2, hours_to_settlement=48.0)
    # The retired 239 form resolves to no slice.
    assert width_slice_for(LEGACY, seated_system_count=2, hours_to_settlement=48.0) is None


def test_the_tail_mix_thickens_the_tails_and_keeps_the_body() -> None:
    mixed, receipt = apply_tail_mix(NARROW, weight=0.2, scale=1.5, ordered_keys=KEYS, key_centers_c=CENTERS)
    assert receipt["state"] == "APPLIED" and receipt["tail_mix_weight"] == 0.2 and receipt["tail_mix_scale"] == 1.5
    assert sum(mixed.values()) == pytest.approx(1.0)
    # Exactly the scale mixture of the input and its widened copy.
    widened, _ = apply_center_width_calibration(NARROW, delta_c=0.0, width_lambda=1.5, ordered_keys=KEYS, key_centers_c=CENTERS)
    for key in KEYS:
        assert mixed[key] == pytest.approx(0.8 * NARROW[key] + 0.2 * widened[key])
    # Tails up, mode down by at most the weight's share, mean kept.
    assert mixed["24C"] > NARROW["24C"] and mixed["28C"] > NARROW["28C"]
    assert mixed["26C"] >= 0.8 * NARROW["26C"]
    mean = lambda v: sum(c * v[k] for c, k in zip(CENTERS, KEYS))
    assert mean(mixed) == pytest.approx(mean(NARROW), abs=1e-9)
    identity, receipt0 = apply_tail_mix(NARROW, weight=0.0, scale=1.5, ordered_keys=KEYS, key_centers_c=CENTERS)
    assert receipt0["state"] == "IDENTITY" and all(identity[k] == pytest.approx(NARROW[k]) for k in KEYS)
    with pytest.raises(ValueError):
        apply_tail_mix(NARROW, weight=0.4, scale=1.5, ordered_keys=KEYS, key_centers_c=CENTERS)


def test_serving_keeps_the_stage_lambda_and_mixes_after_it() -> None:
    base = CenterWidthLayer(delta_c=0.0, width_lambda=0.75, width_tier_lambdas=[1.0, 0.8, 0.6])
    inside = with_width_slice(base, SLICES, seated_system_count=2, hours_to_settlement=48.0)
    outside = with_width_slice(base, SLICES, seated_system_count=6, hours_to_settlement=48.0)
    assert inside.width_slice == THIN_FAR_SLICE_NAME
    assert inside.width_slice_tail_weight == 0.2 and inside.width_slice_tail_scale == 1.5
    assert inside.width_lambda == 0.75                     # the stage lambda is not replaced
    assert outside is base and outside.width_slice is None
    assert with_width_slice(None, SLICES, seated_system_count=2, hours_to_settlement=48.0) is None
    assert with_width_slice(inside, SLICES, seated_system_count=6, hours_to_settlement=48.0).width_slice is None
    out_in, receipt_in = apply_served_table_layers(WIDE, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=inside)
    out_out, receipt_out = apply_served_table_layers(WIDE, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=outside)
    assert receipt_in["width_slice"] == THIN_FAR_SLICE_NAME
    assert receipt_in["width_slice_tail_weight"] == pytest.approx(0.2)
    assert receipt_in["width_slice_tail_scale"] == pytest.approx(1.5)
    assert receipt_in["width_slice_tail_mix"]["state"] == "APPLIED"
    assert receipt_in["stage_width_lambda"] == pytest.approx(0.75)
    assert receipt_in["width_lambda"] == receipt_out["width_lambda"]     # the pair served is the same inside and out
    assert receipt_in["input_probability"] == receipt_out["input_probability"]
    assert receipt_out["width_slice"] is None and receipt_out["width_slice_tail_weight"] is None
    assert receipt_out["width_slice_tail_mix"] is None
    # Inside: the pair's output mixed with its widened copy; outside: the pair's output.
    expected, _ = apply_tail_mix(out_out, weight=0.2, scale=1.5, ordered_keys=KEYS, key_centers_c=CENTERS)
    assert all(out_in[k] == pytest.approx(expected[k]) for k in KEYS)
    sigma = lambda v: distribution_sigma_rungs_v1(v, ordered_keys=KEYS, key_centers_c=CENTERS)
    assert sigma(out_in) > sigma(out_out)


def test_the_row_sha_carries_the_tail_mix_and_it_round_trips(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "cw.db")
    plain = store.center_width_sha256(stage="P0", delta_c=0.0, width_lambda=0.75)
    sliced = store.center_width_sha256(stage="P0", delta_c=0.0, width_lambda=0.75, width_slices=SLICES)
    other = store.center_width_sha256(stage="P0", delta_c=0.0, width_lambda=0.75, width_slices={
        THIN_FAR_SLICE_NAME: {**SLICES[THIN_FAR_SLICE_NAME], "tail_mix_weight": 0.1}})
    assert len({plain, sliced, other}) == 3
    committed = store.persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.0, width_lambda=0.75, evidence={"schema": "t"}, serving_day_count=9, width_slices=SLICES,
    )
    assert committed["state"] == "ACTIVATED" and committed["center_width_sha256"] == sliced
    loaded = store.load_center_width_slices(conn, stage="P0")
    assert loaded == {THIN_FAR_SLICE_NAME: {"max_seated_systems": 3, "min_hours_to_settlement": 36.0, "tail_mix_weight": 0.2, "tail_mix_scale": 1.5}}
    assert store.load_center_width_slices(conn, stage="P0", table_sha256=sliced) == loaded
    # A row written in the retired 239 form reads as no slice.
    conn.execute(
        "UPDATE calibration_center_width_table SET width_slices_json=? WHERE center_width_sha256=?",
        ('{"thin_far": {"max_seated_systems": 3, "min_hours_to_settlement": 36.0, "width_lambda": 1.1}}', sliced),
    )
    assert store.load_center_width_slices(conn, stage="P0") is None


def _cell(dist, settled, *, seated=2, hours=48.0, station="S00", day="2026-09-01", decided="2026-08-30T12:00:00Z"):
    return {
        "probabilities": dict(dist), "ordered_keys": KEYS, "key_centers_c": CENTERS,
        "settled_bucket": settled, "excluded_buckets": (), "event_date": day,
        "station_id": station, "decision_at_utc": decided,
        "seated_system_count": seated, "hours_to_settlement": hours,
    }


def _days(narrow_days: int, pattern, dist=NARROW):
    """Thin-far cells served too thin in the tails: the truth lands two rungs
    out two days in five -- a tail mix pays.  Pooled cells ride along."""
    by_day = {}
    for index in range(narrow_days):
        day = f"2026-09-{index + 1:02d}"
        cells = []
        for station in range(12):
            settled = pattern[(index + station) % len(pattern)]
            cells.append(_cell(dist, settled, seated=2, hours=48.0, station=f"S{station:02d}", day=day))
            cells.append(_cell(WIDE, "26C", seated=6, hours=48.0, station=f"P{station:02d}", day=day))
        by_day[day] = cells
    return by_day


def test_the_lane_admits_a_tail_weight_and_refuses_the_reverse() -> None:
    by_day = _days(12, ["24C", "28C", "26C", "25C", "27C"])
    pooled, sliced = store._split_slice_cells(by_day)
    lane = store._thin_far_slice_lane(
        pooled_by_day=pooled, slice_by_day=sliced, days=sorted(by_day), cut=8,
        pooled_admitted=False, served_residual=store.RESIDUAL_IDENTITY,
        sigma_floor_rungs=0.0, incumbent_slices=None,
    )
    assert lane["schema"] == "center_width_thin_far_slice_lane_v2" and lane["transform"] == "tail_mix"
    assert lane["tail_mix_scale"] == TAIL_MIX_SCALE and lane["tail_mix_weight_grid"] == list(TAIL_MIX_WEIGHT_GRID)
    assert lane["gate_rule"] == store.GATE_RULE
    assert lane["slice_day_count"] == 12 and lane["slice_cell_count"] == 144
    assert lane["candidate_tail_mix_weight"] in TAIL_MIX_WEIGHT_GRID and lane["candidate_tail_mix_weight"] > 0.0
    assert lane["gate"]["lower"] and lane["majority_positive"]
    assert lane["admitted"] is True and lane["state"] == "ADMITTED"
    assert all(fold["slice_tail_mix_weight"] > 0.0 for fold in lane["folds"])
    # Truth always in the mode of a vector with no floored bucket: every mix
    # only moves mass off the mode -> weight 0, refused.  (On the NARROW
    # vector the zero end buckets are lifted by the charter 224 floor, and a
    # mix that puts real mass there returns a little of the floor's
    # renormalisation to the mode -- a true, tiny gain the lane may take.)
    sharp = _days(12, ["26C"], dist=WIDE)
    pooled, sliced = store._split_slice_cells(sharp)
    refused = store._thin_far_slice_lane(
        pooled_by_day=pooled, slice_by_day=sliced, days=sorted(sharp), cut=8,
        pooled_admitted=False, served_residual=store.RESIDUAL_IDENTITY,
        sigma_floor_rungs=0.0, incumbent_slices=None,
    )
    assert refused["candidate_tail_mix_weight"] == 0.0 and refused["admitted"] is False
    # Too little evidence accumulates rather than judges.
    thin = {d: c for d, c in list(by_day.items())[:5]}
    pooled, sliced = store._split_slice_cells(thin)
    acc = store._thin_far_slice_lane(
        pooled_by_day=pooled, slice_by_day=sliced, days=sorted(thin), cut=3,
        pooled_admitted=False, served_residual=store.RESIDUAL_IDENTITY,
        sigma_floor_rungs=0.0, incumbent_slices=None,
    )
    assert acc["state"] == "ACCUMULATING" and acc["admitted"] is False


def test_the_veto_remixes_the_recorded_input_and_reads_the_ranked_rule() -> None:
    # Cells served under the mix (weight 0.3) whose truth sits on a shoulder:
    # the mix moved that shoulder's mass outward, so the plain pair would
    # have served them better on all three scores.  (Truth in the mode is
    # not a clean veto: the mix leaves the mode's mass where it is and
    # spreading the shoulders lowers the Brier -- the guardrail holds.)
    layer = with_width_slice(
        CenterWidthLayer(0.0, 0.75),
        {THIN_FAR_SLICE_NAME: {**SLICES[THIN_FAR_SLICE_NAME], "tail_mix_weight": 0.3}},
        seated_system_count=2, hours_to_settlement=48.0,
    )
    served, receipt = apply_served_table_layers(WIDE, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=layer)
    pre_mix, _ = apply_center_width_calibration(WIDE, delta_c=0.0, width_lambda=0.75, ordered_keys=KEYS, key_centers_c=CENTERS)
    by_day = {}
    for index in range(4):
        day = f"2026-09-{index + 1:02d}"
        by_day[day] = [
            {**_cell(pre_mix, "27C", station=f"S{s:02d}", day=day, decided="2026-09-05T00:00:00Z"),
             "center_width_receipt": dict(receipt), "reserved_under_current_width": True}
            for s in range(20)
        ]
    veto = store._thin_far_slice_veto(by_day, slices=SLICES, activated_at_utc="2026-09-01T00:00:00Z")
    assert veto["schema"] == "center_width_thin_far_slice_veto_v2"
    assert veto["cell_count"] == 80 and veto["day_count"] == 4
    assert veto["gate"]["rule"] == store.GATE_RULE
    assert veto["undo_gain"] > 0 and veto["vetoed"] is True
    none = store._thin_far_slice_veto(by_day, slices=SLICES, activated_at_utc="2026-09-30T00:00:00Z")
    assert none["cell_count"] == 0 and none["vetoed"] is False
    # A cell whose vector could not be re-served from its input is not judged.
    unreserved = {d: [{**c, "reserved_under_current_width": False} for c in cells] for d, cells in by_day.items()}
    skipped = store._thin_far_slice_veto(unreserved, slices=SLICES, activated_at_utc="2026-09-01T00:00:00Z")
    assert skipped["cell_count"] == 0 and skipped["skipped"] == {"no_recorded_input": 80}


def test_a_row_served_under_the_retired_slice_is_re_served_under_the_stage_lambda() -> None:
    # A 239-era receipt: lambda 1.1 replaced the stage 0.75 and the input was recorded.
    legacy_served, legacy_receipt = apply_center_width_calibration(
        NARROW, delta_c=0.0, width_lambda=1.1, ordered_keys=KEYS, key_centers_c=CENTERS,
    )
    legacy_receipt = {**legacy_receipt, "width_slice": THIN_FAR_SLICE_NAME, "width_slice_lambda": 1.1, "stage_width_lambda": 0.75}
    back = store._reserved_under_current_width(legacy_receipt, keys=KEYS, centers=CENTERS, open_edge_bounds=None, excluded=())
    stage_served, _ = apply_center_width_calibration(NARROW, delta_c=0.0, width_lambda=0.75, ordered_keys=KEYS, key_centers_c=CENTERS)
    assert back is not None and all(back[k] == pytest.approx(stage_served[k], abs=1e-12) for k in KEYS)
    # A 248-era receipt re-serves to the mix's input: the pair's own output.
    layer = with_width_slice(CenterWidthLayer(0.0, 0.75), SLICES, seated_system_count=2, hours_to_settlement=48.0)
    _mixed, receipt = apply_served_table_layers(NARROW, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=layer)
    back = store._reserved_under_current_width(receipt, keys=KEYS, centers=CENTERS, open_edge_bounds=None, excluded=())
    assert back is not None and all(back[k] == pytest.approx(stage_served[k], abs=1e-12) for k in KEYS)


def test_a_slice_change_is_committed_even_when_the_pooled_pair_stands(tmp_path, monkeypatch) -> None:
    conn = sqlite3.connect(tmp_path / "cw.db")
    store.persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.0, width_lambda=0.75, evidence={"schema": "t"}, serving_day_count=9,
    )
    fitted = {
        "stage": "P0", "delta_c": 0.0, "width_lambda": 0.75, "sigma_floor_rungs": 0.0,
        "width_tiers": None, "width_slices": SLICES, "candidate_width_slices": SLICES,
        "forward_nll_gain": 0.0, "accumulating": False, "vetoed": False, "admitted": False,
        "serving_day_count": 9, "serving_cell_count": 200,
        "evidence": {"width_slices": {"state": "ADMITTED", "candidate_tail_mix_weight": 0.2}},
    }
    monkeypatch.setattr(store, "fit_center_width", lambda conn, stage, min_decision_at_utc=None: fitted)
    monkeypatch.setattr(store, "fit_station_width", lambda conn, stage, min_decision_at_utc=None: {
        "station_count": 0, "forward_nll_gain": 0.0, "accumulating": True, "evidence": {},
    })
    results: dict = {}
    store._refit_stages(conn, ("P0",), results, True, None)
    assert results["P0"]["outcome"] == "ACTIVATED" and results["P0"]["width_slices"] == SLICES
    assert store.load_center_width_slices(conn, stage="P0") == {THIN_FAR_SLICE_NAME: {"max_seated_systems": 3, "min_hours_to_settlement": 36.0, "tail_mix_weight": 0.2, "tail_mix_scale": 1.5}}
    assert store.load_center_width(conn, stage="P0")[:2] == (0.0, 0.75)
    results = {}
    store._refit_stages(conn, ("P0",), results, True, None)
    assert results["P0"]["outcome"] == "REFUSED_FORWARD"
    fitted["width_slices"] = None
    results = {}
    store._refit_stages(conn, ("P0",), results, True, None)
    assert results["P0"]["outcome"] == "ACTIVATED"
    assert store.load_center_width_slices(conn, stage="P0") is None


def test_an_active_row_in_the_retired_form_is_rewritten_on_the_first_run(tmp_path, monkeypatch) -> None:
    conn = sqlite3.connect(tmp_path / "cw.db")
    store.persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.0, width_lambda=0.75, evidence={"schema": "t"}, serving_day_count=9,
    )
    # A 239-era row: its sha digested the lambda slice, so it differs from
    # the plain pair's row the lane will write.
    conn.execute(
        "UPDATE calibration_center_width_table SET width_slices_json=?, center_width_sha256=?"
        " WHERE activated=1 AND stage='P0'",
        ('{"thin_far": {"max_seated_systems": 3, "min_hours_to_settlement": 36.0, "width_lambda": 1.1}}', "f" * 64),
    )
    conn.commit()
    assert store._active_slices_in_retired_form(conn, stage="P0") is True
    assert store.load_center_width_slices(conn, stage="P0") is None       # reads as no slice ...
    fitted = {
        "stage": "P0", "delta_c": 0.0, "width_lambda": 0.75, "sigma_floor_rungs": 0.0,
        "width_tiers": None, "width_slices": None, "candidate_width_slices": None,
        "forward_nll_gain": 0.0, "accumulating": False, "vetoed": False, "admitted": False,
        "serving_day_count": 9, "serving_cell_count": 200,
        "evidence": {"width_slices": {"state": "REFUSED_FORWARD", "candidate_tail_mix_weight": 0.2}},
    }
    monkeypatch.setattr(store, "fit_center_width", lambda conn, stage, min_decision_at_utc=None: fitted)
    monkeypatch.setattr(store, "fit_station_width", lambda conn, stage, min_decision_at_utc=None: {
        "station_count": 0, "forward_nll_gain": 0.0, "accumulating": True, "evidence": {},
    })
    results: dict = {}
    store._refit_stages(conn, ("P0",), results, True, None)
    assert results["P0"]["outcome"] == "ACTIVATED"                     # ... but the row is rewritten without it
    row = conn.execute("SELECT width_slices_json FROM calibration_center_width_table WHERE activated=1 AND stage='P0'").fetchone()
    assert row[0] is None
    assert store._active_slices_in_retired_form(conn, stage="P0") is False
    results = {}
    store._refit_stages(conn, ("P0",), results, True, None)
    assert results["P0"]["outcome"] == "REFUSED_FORWARD"                # and stays put after that


def test_every_serving_face_resolves_the_slice_through_one_resolver() -> None:
    root = Path(__file__).resolve().parents[1]
    read = lambda rel: (root / rel).read_text(encoding="utf-8")
    p0 = read("bayes_basket_target/native_ensemble_p0.py")
    assert 'width_slices = load_center_width_slices(' in p0
    assert 'center_width_layer = with_width_slice(' in p0
    oos = read("bayes_basket_target/native_ensemble_oos.py")
    assert 'load_center_width_slices(conn, stage="P0")' in oos
    assert oos.count("_day_center_width_layer(") >= 3
    served = read("bayes_basket_target/served_shape.py")
    assert "width_slice_lambda" not in served               # the replacing form is gone from serving
    assert "apply_tail_mix(" in served
    cells = read("bayes_basket_target/center_width_store.py")
    assert "_search_slice_lambda" not in cells
    assert "_search_slice_tail(" in cells and "apply_tail_mix(" in cells
    charter = read("docs/constitution/SYSTEM_CHARTER.md")
    line = next(l for l in charter.splitlines() if l.startswith("248. "))
    for word in ("apply_tail_mix", "TAIL_MIX_SCALE", "TAIL_MIX_WEIGHT_GRID", "tail_mix_weight", "尾重", "v26", "v29", "v37", "裁决人：熊先生"):
        assert word in line, word
