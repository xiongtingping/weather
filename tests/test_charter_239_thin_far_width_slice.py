"""Charter 239: the thin-pool far-lead width slice, judged on the LogScore
alone (user ruling 2026-10-01, 「按照logscore单独判」).

One predicate for fit, serving and gate; the slice lambda replaces the stage
lambda for the decision it applies to and the receipt says so; the row's sha
carries the slice; the lane admits on the LogScore station-day gate and a fold
majority; the veto reads the same rule; a slice change is persisted even when
the pooled pair stands.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import center_width_store as store
from bayes_basket_target.center_width_calibration import (
    THIN_FAR_MAX_SEATED_SYSTEMS,
    THIN_FAR_MIN_HOURS_TO_SETTLEMENT,
    THIN_FAR_SLICE_NAME,
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
    "max_seated_systems": 3, "min_hours_to_settlement": 36.0, "width_lambda": 1.2,
}}


def test_the_predicate_is_one_function_and_fails_closed() -> None:
    assert THIN_FAR_MAX_SEATED_SYSTEMS == 3 and THIN_FAR_MIN_HOURS_TO_SETTLEMENT == 36.0
    assert thin_far_slice_applies(seated_system_count=2, hours_to_settlement=40.0)
    assert thin_far_slice_applies(seated_system_count=3, hours_to_settlement=36.01)
    assert not thin_far_slice_applies(seated_system_count=3, hours_to_settlement=36.0)
    assert not thin_far_slice_applies(seated_system_count=4, hours_to_settlement=60.0)
    assert not thin_far_slice_applies(seated_system_count=None, hours_to_settlement=60.0)
    assert not thin_far_slice_applies(seated_system_count=2, hours_to_settlement=None)
    assert not thin_far_slice_applies(seated_system_count=2, hours_to_settlement=float("nan"))
    assert width_slice_for(SLICES, seated_system_count=2, hours_to_settlement=48.0) == (THIN_FAR_SLICE_NAME, 1.2)
    assert width_slice_for(SLICES, seated_system_count=6, hours_to_settlement=48.0) is None
    assert width_slice_for(None, seated_system_count=2, hours_to_settlement=48.0) is None
    with pytest.raises(ValueError):
        width_slice_for({THIN_FAR_SLICE_NAME: {"width_lambda": 3.0}}, seated_system_count=2, hours_to_settlement=48.0)
    # The row's own bounds travel with it.
    strict = {THIN_FAR_SLICE_NAME: {"max_seated_systems": 2, "min_hours_to_settlement": 48.0, "width_lambda": 1.1}}
    assert width_slice_for(strict, seated_system_count=3, hours_to_settlement=60.0) is None
    assert width_slice_for(strict, seated_system_count=2, hours_to_settlement=49.0) == (THIN_FAR_SLICE_NAME, 1.1)


def test_serving_replaces_the_stage_lambda_and_the_receipt_says_so() -> None:
    base = CenterWidthLayer(delta_c=0.0, width_lambda=0.75, width_tier_lambdas=[1.0, 0.8, 0.6])
    inside = with_width_slice(base, SLICES, seated_system_count=2, hours_to_settlement=48.0)
    outside = with_width_slice(base, SLICES, seated_system_count=6, hours_to_settlement=48.0)
    assert inside.width_slice == THIN_FAR_SLICE_NAME and inside.width_slice_lambda == 1.2
    assert outside is base and outside.width_slice is None
    assert with_width_slice(None, SLICES, seated_system_count=2, hours_to_settlement=48.0) is None
    # A layer resolved for one decision and handed another forgets the slice.
    assert with_width_slice(inside, SLICES, seated_system_count=6, hours_to_settlement=48.0).width_slice is None
    out_in, receipt_in = apply_served_table_layers(
        NARROW, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=inside,
    )
    out_out, receipt_out = apply_served_table_layers(
        NARROW, ordered_keys=KEYS, key_centers_c=CENTERS, center_width=outside,
    )
    assert receipt_in["width_slice"] == THIN_FAR_SLICE_NAME
    assert receipt_in["width_slice_lambda"] == pytest.approx(1.2)
    assert receipt_in["stage_width_lambda"] == pytest.approx(0.75)
    assert receipt_in["width_lambda"] == pytest.approx(1.2)
    assert receipt_in["width_tier"] is None          # the tiers stand aside in the slice
    assert receipt_out["width_slice"] is None and receipt_out["width_slice_lambda"] is None
    assert receipt_out["stage_width_lambda"] == pytest.approx(0.75)
    # Wider inside the slice (the mode's mass sits on the mean and does not
    # move; the shoulders do); outside it the narrow tier's lambda 1.0 is an
    # identity, so the vector is served as is.
    from bayes_basket_target.center_width_calibration import distribution_sigma_rungs_v1
    sigma = lambda v: distribution_sigma_rungs_v1(v, ordered_keys=KEYS, key_centers_c=CENTERS)
    assert sigma(out_in) > sigma(NARROW)
    assert all(out_out[k] == pytest.approx(NARROW[k]) for k in KEYS)


def test_the_row_sha_carries_the_slice_and_it_round_trips(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "cw.db")
    plain = store.center_width_sha256(stage="P0", delta_c=0.0, width_lambda=0.75)
    sliced = store.center_width_sha256(stage="P0", delta_c=0.0, width_lambda=0.75, width_slices=SLICES)
    assert plain != sliced
    committed = store.persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.0, width_lambda=0.75, evidence={"schema": "t"},
        serving_day_count=9, width_slices=SLICES,
    )
    assert committed["state"] == "ACTIVATED" and committed["center_width_sha256"] == sliced
    assert store.load_center_width(conn, stage="P0")[:2] == (0.0, 0.75)
    loaded = store.load_center_width_slices(conn, stage="P0")
    assert loaded == {THIN_FAR_SLICE_NAME: {"max_seated_systems": 3, "min_hours_to_settlement": 36.0, "width_lambda": 1.2}}
    assert store.load_center_width_slices(conn, stage="P0", table_sha256=sliced) == loaded
    assert store.load_center_width_slices(conn, stage="P1") is None
    # The same pair without the slice is a different, older row.
    again = store.persist_and_activate_center_width(
        conn, stage="P0", delta_c=0.0, width_lambda=0.75, evidence={"schema": "t"}, serving_day_count=9,
    )
    assert again["center_width_sha256"] == plain
    assert store.load_center_width_slices(conn, stage="P0") is None
    assert store.load_center_width_slices(conn, stage="P0", table_sha256=sliced) == loaded
    # Retired rows read as no slice, like the tiers.
    store.retire_center_width(conn, stage="P0")
    assert store.load_center_width_slices(conn, stage="P0", table_sha256=plain) is None
    from bayes_basket_target.table_as_of import center_width_as_of
    assert "width_slices" in (center_width_as_of(conn, stage="P0", as_of_utc="2099-01-01T00:00:00Z") or {"width_slices": None})


def _cell(dist, settled, *, seated=2, hours=48.0, station="S00", day="2026-09-01", decided="2026-08-30T12:00:00Z"):
    return {
        "probabilities": dict(dist), "ordered_keys": KEYS, "key_centers_c": CENTERS,
        "settled_bucket": settled, "excluded_buckets": (), "event_date": day,
        "station_id": station, "decision_at_utc": decided,
        "seated_system_count": seated, "hours_to_settlement": hours,
    }


def test_the_logscore_gate_needs_only_the_logscore() -> None:
    base = [(1.0, 0.5)] * 40
    candidate = [(0.8, 0.52)] * 40            # LogScore better, Brier worse
    clusters = [(f"S{i:02d}", "2026-09-01") for i in range(40)]
    both = store._proper_score_gate(base, candidate, clusters)
    log_only = store._logscore_gate(base, candidate, clusters)
    assert both["lower"] is False and log_only["lower"] is True
    assert log_only["rule"] == store.GATE_RULE_LOGSCORE
    assert log_only["ci_brier"] is not None and log_only["d_brier"] > 0


def test_the_cells_are_split_by_the_predicate() -> None:
    by_day = {"2026-09-01": [_cell(WIDE, "26C", seated=2, hours=48.0), _cell(WIDE, "26C", seated=6, hours=48.0), _cell(WIDE, "26C", seated=2, hours=20.0), _cell(WIDE, "26C", seated=None, hours=48.0)]}
    pooled, sliced = store._split_slice_cells(by_day)
    assert len(sliced["2026-09-01"]) == 1 and len(pooled["2026-09-01"]) == 3


def _days(narrow_days: int, pattern):
    """Thin-far cells served too narrow: the truth scatters beyond the
    shoulders the narrow vector gives 0.02 -- widening pays on the LogScore.
    Pooled cells ride along untouched."""

    by_day = {}
    for index in range(narrow_days):
        day = f"2026-09-{index + 1:02d}"
        cells = []
        for station in range(12):
            settled = pattern[(index + station) % len(pattern)]
            cells.append(_cell(NARROW, settled, seated=2, hours=48.0, station=f"S{station:02d}", day=day))
            cells.append(_cell(WIDE, "26C", seated=6, hours=48.0, station=f"P{station:02d}", day=day))
        by_day[day] = cells
    return by_day


def test_the_lane_admits_a_wider_slice_on_the_logscore_and_refuses_the_reverse() -> None:
    # Truth lands two rungs out two days in five: on the histogram transport
    # a widening moves the shoulders outward (the mode's mass stays on the
    # mean), so it pays where the truth sits beyond the shoulders.
    by_day = _days(12, ["24C", "28C", "26C", "25C", "27C"])
    days = sorted(by_day)
    pooled, sliced = store._split_slice_cells(by_day)
    lane = store._thin_far_slice_lane(
        pooled_by_day=pooled, slice_by_day=sliced, days=days, cut=8,
        pooled_admitted=False, served_residual=store.RESIDUAL_IDENTITY,
        sigma_floor_rungs=0.0, served_slice_lambda=0.75, incumbent_slices=None,
    )
    assert lane["gate_rule"] == store.GATE_RULE_LOGSCORE
    assert lane["slice_day_count"] == 12 and lane["slice_cell_count"] == 144
    assert lane["candidate_residual_width_lambda"] > 1.0
    assert lane["gate"]["lower"] and lane["majority_positive"]
    assert lane["admitted"] is True and lane["state"] == "ADMITTED"
    assert lane["candidate_width_lambda"] == pytest.approx(0.75 * lane["candidate_residual_width_lambda"], abs=1e-4)
    # Truth always in the mode: narrowing, not widening, and the lane that
    # only ever narrows this vector cannot beat identity -> refused.
    sharp = _days(12, ["26C"])
    pooled, sliced = store._split_slice_cells(sharp)
    refused = store._thin_far_slice_lane(
        pooled_by_day=pooled, slice_by_day=sliced, days=sorted(sharp), cut=8,
        pooled_admitted=False, served_residual=store.RESIDUAL_IDENTITY,
        sigma_floor_rungs=0.0, served_slice_lambda=0.75, incumbent_slices=None,
    )
    assert refused["candidate_residual_width_lambda"] < 1.0
    # Too little evidence accumulates rather than judges.
    thin = {d: c for d, c in list(by_day.items())[:5]}
    pooled, sliced = store._split_slice_cells(thin)
    acc = store._thin_far_slice_lane(
        pooled_by_day=pooled, slice_by_day=sliced, days=sorted(thin), cut=3,
        pooled_admitted=False, served_residual=store.RESIDUAL_IDENTITY,
        sigma_floor_rungs=0.0, served_slice_lambda=0.75, incumbent_slices=None,
    )
    assert acc["state"] == "ACCUMULATING" and acc["admitted"] is False


def test_the_veto_reads_the_recorded_input_and_the_logscore_rule() -> None:
    # Cells served under the slice (lambda 1.2 on a 0.75 stage) whose truth
    # sits in the mode: the stage lambda would have served them better.
    served, receipt = apply_served_table_layers(
        NARROW, ordered_keys=KEYS, key_centers_c=CENTERS,
        center_width=with_width_slice(CenterWidthLayer(0.0, 0.75), SLICES, seated_system_count=2, hours_to_settlement=48.0),
    )
    receipt["state"] = "APPLIED"
    by_day = {}
    for index in range(4):
        day = f"2026-09-{index + 1:02d}"
        by_day[day] = [
            {**_cell(served, "26C", station=f"S{s:02d}", day=day, decided="2026-09-05T00:00:00Z"), "center_width_receipt": dict(receipt)}
            for s in range(20)
        ]
    veto = store._thin_far_slice_veto(by_day, slices=SLICES, activated_at_utc="2026-09-01T00:00:00Z")
    assert veto["cell_count"] == 80 and veto["day_count"] == 4
    assert veto["gate"]["rule"] == store.GATE_RULE_LOGSCORE
    assert veto["undo_gain"] > 0 and veto["vetoed"] is True
    # Nothing since activation: nothing judged.
    none = store._thin_far_slice_veto(by_day, slices=SLICES, activated_at_utc="2026-09-30T00:00:00Z")
    assert none["cell_count"] == 0 and none["vetoed"] is False


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
        "evidence": {"width_slices": {"state": "ADMITTED", "candidate_width_lambda": 1.2}},
    }
    monkeypatch.setattr(store, "fit_center_width", lambda conn, stage, min_decision_at_utc=None: fitted)
    monkeypatch.setattr(store, "fit_station_width", lambda conn, stage, min_decision_at_utc=None: {
        "station_count": 0, "forward_nll_gain": 0.0, "accumulating": True, "evidence": {},
    })
    results: dict = {}
    store._refit_stages(conn, ("P0",), results, True, None)
    entry = results["P0"]
    assert entry["outcome"] == "ACTIVATED" and entry["width_slices"] == SLICES
    assert store.load_center_width_slices(conn, stage="P0") == {THIN_FAR_SLICE_NAME: {"max_seated_systems": 3, "min_hours_to_settlement": 36.0, "width_lambda": 1.2}}
    assert store.load_center_width(conn, stage="P0")[:2] == (0.0, 0.75)
    # The same fit again: the pair refused, the slice unchanged -> no new row.
    results = {}
    store._refit_stages(conn, ("P0",), results, True, None)
    assert results["P0"]["outcome"] == "REFUSED_FORWARD"
    assert conn.execute("SELECT COUNT(*) FROM calibration_center_width_table").fetchone()[0] == 2
    # The slice vetoed: the pair stays, the slice comes off.
    fitted["width_slices"] = None
    results = {}
    store._refit_stages(conn, ("P0",), results, True, None)
    assert results["P0"]["outcome"] == "ACTIVATED"
    assert store.load_center_width_slices(conn, stage="P0") is None
    journal = conn.execute("SELECT COUNT(*) FROM calibration_center_width_fit_journal").fetchone()[0]
    assert journal == 3


def test_every_serving_face_resolves_the_slice_through_one_resolver() -> None:
    root = Path(__file__).resolve().parents[1]
    read = lambda rel: (root / rel).read_text(encoding="utf-8")
    p0 = read("bayes_basket_target/native_ensemble_p0.py")
    assert 'width_slices = load_center_width_slices(' in p0
    assert 'center_width_layer = with_width_slice(' in p0
    assert 'center_width_receipt["seated_system_count"]' in p0
    oos = read("bayes_basket_target/native_ensemble_oos.py")
    assert 'load_center_width_slices(conn, stage="P0")' in oos
    assert oos.count("_day_center_width_layer(") >= 3      # the definition and both scoring sites
    assert 'center_width_slices=gate_center_width_slices' in oos
    replay = read("bayes_basket_target/table_as_of.py")
    assert '"width_slices": load_center_width_slices(' in replay
    cells = read("bayes_basket_target/center_width_store.py")
    assert '"seated_system_count": seated_system_count' in cells
    assert 'hours_to_settlement_if_known(' in cells
    charter = read("docs/constitution/SYSTEM_CHARTER.md")
    line = next(l for l in charter.splitlines() if l.startswith("239. "))
    for word in ("thin_far_slice_applies", "logscore_lower_station_day_bootstrap_v1", "width_slices_json", "按照logscore单独判"):
        assert word in line, word
