"""Charter 242: the P0 fusion layer's station x model weights (charter 199's
layer, new rule).  The rule: a station's log-score-optimal mixture of the
seated systems, each system shrunk to its seat weight by its own station-days
(a_j = n_j / (n_j + tau)); parameters fitted daily into the training file and
served as of the decision; a forward veto returns the seat weights.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bayes_basket_target import fusion_blend as FB
from bayes_basket_target.native_ensemble_contract import (
    validate_cross_native_ensemble_weights,
)

SEAT = {"A": 0.5, "B": 0.5}
FAMILIES = {"A": "FA", "B": "FB"}
CAPS = {"FA": 0.75, "FB": 0.75}


def _fact_record(station, day, *, at, known, py, revision=0, horizon=0, seat=None,
                 served=None, rule="REPLAY_SEAT", state="REPLAY"):
    pool = sorted(py)
    seat = seat or {s: 1.0 / len(pool) for s in pool}
    return {
        "station_id": station, "event_date": day, "horizon_bin": horizon,
        "truth_revision": revision, "decision_at_utc": at, "truth_c": 20.0,
        "truth_known_at_utc": known, "truth_source": "FORMAL",
        "probability_source": "ENVELOPE", "probability_grid": "MARKET_LADDER",
        "systems_json": json.dumps(pool), "truth_probability_json": json.dumps(py),
        "seat_weights_json": json.dumps(seat), "served_weights_json": json.dumps(served or seat),
        "fusion_rule_version": rule, "fusion_state": state, "source_ref": "test",
        "schema": FB.STATION_WEIGHT_FACT_SCHEMA, "created_at_utc": "2026-10-01T00:00:00Z",
        "content_sha256": f"{station}{day}{horizon}{revision}",
    }


def _db(path: Path, *, facts=(), params=(), vetoes=()) -> Path:
    conn = sqlite3.connect(path)
    for statement in FB.TRAINING_DDL + FB.STATION_WEIGHT_DDL:
        conn.execute(statement)
    for record in facts:
        conn.execute("INSERT INTO station_model_weight_fact (" + ",".join(record) + ") VALUES ("
                     + ",".join("?" * len(record)) + ")", tuple(record.values()))
    for i, (station, at, tau, theta, days) in enumerate(params):
        conn.execute(
            "INSERT INTO station_model_weight_params VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"p{i}", station, at, FB.FUSION_BLEND_RULE_VERSION, tau, json.dumps(theta),
             json.dumps(days), 10, "sha", "{}", FB.STATION_WEIGHT_PARAMS_SCHEMA, f"c{i}"))
    for i, (at, vetoed) in enumerate(vetoes):
        conn.execute(
            "INSERT INTO station_model_weight_veto VALUES (?,?,?,?,?,?,?)",
            (f"v{i}", at, FB.FUSION_BLEND_RULE_VERSION, int(vetoed), "{}", FB.STATION_WEIGHT_VETO_SCHEMA, f"vc{i}"))
    conn.commit()
    conn.close()
    return path


def _overlay(path, *, at="2026-10-02T12:00:00Z", mode=FB.FUSION_BLEND_MODE_BLEND, station="ZZZZ",
             seat=SEAT, families=FAMILIES):
    return FB.overlay_system_weights(
        station_id=station, event_date="2026-10-03", decision_at_utc=at, timezone_name="UTC",
        system_weights=seat, family_by_system=families, family_caps=CAPS,
        lead_by_system={}, training_db=path, mode=mode,
    )


def test_each_system_shrinks_by_its_own_station_days():
    theta = {"A": math.log(3.0), "B": 0.0}          # station fit 0.75 / 0.25
    weights, detail = FB.mixture_weights(SEAT, theta, {"A": 30, "B": 0}, tau=10.0)
    assert detail["station_fit_weights"] == pytest.approx({"A": 0.75, "B": 0.25})
    assert detail["alpha_by_system"] == pytest.approx({"A": 0.75, "B": 0.0})
    mixed = {"A": 0.25 * 0.5 + 0.75 * 0.75, "B": 0.5}  # B never scored here: its seat weight
    total = sum(mixed.values())
    assert weights == pytest.approx({k: v / total for k, v in mixed.items()})


def test_equal_station_days_reduce_to_one_shrinkage():
    theta = {"A": math.log(3.0), "B": 0.0}
    weights, _ = FB.mixture_weights(SEAT, theta, {"A": 20, "B": 20}, tau=20.0)
    assert weights == pytest.approx({"A": 0.5 * 0.5 + 0.5 * 0.75, "B": 0.5 * 0.5 + 0.5 * 0.25})
    unscored, _ = FB.mixture_weights(SEAT, theta, {"A": 0, "B": 0}, tau=20.0)
    assert unscored == pytest.approx(SEAT)


def test_the_fit_follows_the_system_that_put_mass_on_the_truth():
    facts = [FB.WeightFact(_fact_record("S", f"2026-09-{d:02d}", at=f"2026-09-{d:02d}T00:00:00Z",
                                        known=f"2026-09-{d:02d}T23:00:00Z", py={"A": 0.6, "B": 0.1}))
             for d in range(1, 21)]
    theta = FB.fit_theta(facts, ["A", "B"])
    assert theta["A"] > theta["B"]
    # Mixed seated sets are scored on their own systems; C never scored, its logit only centred.
    facts.append(FB.WeightFact(_fact_record("S", "2026-09-21", at="2026-09-21T00:00:00Z",
                                            known="2026-09-21T23:00:00Z", py={"A": 0.5, "B": 0.5})))
    theta3 = FB.fit_theta(facts, ["A", "B", "C"])
    assert set(theta3) == {"A", "B", "C"} and sum(theta3.values()) == pytest.approx(0.0, abs=1e-9)
    assert FB.days_by_system(facts) == {"A": 21, "B": 21}


def test_only_known_truths_and_the_newest_revision_are_read():
    early = FB.WeightFact(_fact_record("S", "2026-09-10", at="2026-09-09T00:00:00Z",
                                       known="2026-09-11T00:00:00Z", py={"A": 0.5, "B": 0.5}, revision=0))
    late = FB.WeightFact(_fact_record("S", "2026-09-10", at="2026-09-09T00:00:00Z",
                                      known="2026-09-15T00:00:00Z", py={"A": 0.4, "B": 0.6}, revision=1))
    at = FB._stamp("2026-09-12T00:00:00Z")
    assert [f.revision for f in FB.knowable_facts([early, late], at)] == [0]
    assert [f.revision for f in FB.knowable_facts([early, late], FB._stamp("2026-09-16T00:00:00Z"))] == [1]
    assert FB.knowable_facts([early], FB._stamp("2026-09-11T00:00:00Z")) == []          # strict
    assert len(FB.knowable_facts([early], FB._stamp("2026-09-11T00:00:00Z"), strict=False)) == 1


def test_serving_reads_the_params_as_of_the_decision(tmp_path):
    path = _db(tmp_path / "t.db", params=[
        ("ZZZZ", "2026-10-01T01:00:00Z", 10.0, {"A": math.log(3.0), "B": 0.0}, {"A": 30, "B": 30}),
        ("ZZZZ", "2026-10-02T01:00:00Z", 10.0, {"A": 0.0, "B": math.log(3.0)}, {"A": 30, "B": 30}),
    ])
    before, receipt = _overlay(path, at="2026-09-30T12:00:00Z")
    assert before == SEAT and receipt["state"] == "SEAT_NO_STATION_PARAMS"
    first, receipt = _overlay(path, at="2026-10-01T12:00:00Z")
    assert receipt["state"] == "APPLIED" and receipt["params_id"] == "p0" and first["A"] > first["B"]
    second, receipt = _overlay(path, at="2026-10-02T12:00:00Z")
    assert receipt["params_id"] == "p1" and second["B"] > second["A"]
    assert receipt["rule_version"] == FB.FUSION_BLEND_RULE_VERSION and receipt["tau"] == 10.0
    assert receipt["seat_weights"] == SEAT and receipt["served_weights"] == second
    validate_cross_native_ensemble_weights(
        system_weights=second, systems_by_id={s: {"independence_family_id": f} for s, f in FAMILIES.items()},
        family_weight_caps=CAPS,
    )
    other, receipt = _overlay(path, station="YYYY")
    assert other == SEAT and receipt["state"] == "SEAT_NO_STATION_PARAMS"


def test_the_veto_serves_the_seat_while_it_stands(tmp_path):
    params = [("ZZZZ", "2026-10-01T01:00:00Z", 10.0, {"A": math.log(3.0), "B": 0.0}, {"A": 30, "B": 30})]
    path = _db(tmp_path / "t.db", params=params, vetoes=[("2026-10-01T02:00:00Z", True), ("2026-10-02T02:00:00Z", False)])
    vetoed, receipt = _overlay(path, at="2026-10-01T12:00:00Z")
    assert vetoed == SEAT and receipt["state"] == "SEAT_VETOED" and receipt["veto_id"] == "v0"
    lifted, receipt = _overlay(path, at="2026-10-02T12:00:00Z")
    assert receipt["state"] == "APPLIED" and lifted != SEAT


def test_shadow_off_and_the_fallbacks_serve_the_seat(tmp_path):
    params = [("ZZZZ", "2026-10-01T01:00:00Z", 10.0, {"A": math.log(3.0), "B": 0.0}, {"A": 30, "B": 30})]
    path = _db(tmp_path / "t.db", params=params)
    served, receipt = _overlay(path, mode=FB.FUSION_BLEND_MODE_SHADOW)
    blended, blend_receipt = _overlay(path)
    assert served == SEAT and receipt["state"] == "SHADOW" and receipt["shadow_state"] == "APPLIED"
    assert receipt["shadow_weights"] == blend_receipt["served_weights"] == blended
    off, receipt = _overlay(path, mode=FB.FUSION_BLEND_MODE_OFF)
    assert off == SEAT and receipt["state"] == "OFF"
    missing, receipt = _overlay(tmp_path / "absent.db")
    assert missing == SEAT and receipt["state"] == "SEAT_TRAINING_UNAVAILABLE"
    one, receipt = _overlay(path, families={"A": "FA", "B": "FA"})
    assert one == SEAT and receipt["state"] == "SEAT_FEWER_THAN_TWO_FAMILIES"
    unscored = _db(tmp_path / "u.db", params=[("ZZZZ", "2026-10-01T01:00:00Z", 10.0, {"A": 1.0, "B": 0.0}, {})])
    zero, receipt = _overlay(unscored)
    assert zero == SEAT and receipt["state"] == "SEAT_NO_TRAINING_DAYS"


def test_tau_is_chosen_on_known_truths_only():
    as_of = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    facts = []
    for d in range(5, 30):
        day = (as_of.date() - timedelta(days=d)).isoformat()
        facts.append(FB.WeightFact(_fact_record("S", day, at=f"{day}T00:00:00Z", known=f"{day}T23:00:00Z",
                                                py={"A": 0.6, "B": 0.05})))
    late_day = (as_of.date() - timedelta(days=3)).isoformat()
    facts.append(FB.WeightFact(_fact_record("S", late_day, at=f"{late_day}T00:00:00Z",
                                            known="2026-10-05T00:00:00Z", py={"A": 0.6, "B": 0.05})))
    tau, evidence = FB.choose_tau(facts, ["A", "B"], as_of)
    assert evidence["window_decisions"] == 18          # event days d-22..d-5 known; d-3 not yet
    assert tau == FB.TAU_GRID[0]                       # one system always better: trust the fit fastest


def test_the_veto_reads_served_against_seat_on_this_rule_only():
    as_of = datetime(2026, 10, 20, tzinfo=timezone.utc)
    facts = []
    for d in range(1, 11):
        for station in ("S1", "S2", "S3", "S4", "S5", "S6", "S7"):
            day = f"2026-10-{d:02d}"
            facts.append(FB.WeightFact(_fact_record(
                station, day, at=f"{day}T00:00:00Z", known=f"{day}T23:00:00Z", py={"A": 0.6, "B": 0.05},
                seat={"A": 0.5, "B": 0.5}, served={"A": 0.1, "B": 0.9},
                rule=FB.FUSION_BLEND_RULE_VERSION, state="APPLIED")))
    out = FB.veto_evidence(facts, as_of)
    assert out["station_days"] == 70 and out["enough_evidence"] and out["vetoed"]
    assert not FB.veto_evidence(facts[:14], as_of)["enough_evidence"]
    other_rule = [FB.WeightFact({**_fact_record("S", "2026-10-01", at="2026-10-01T00:00:00Z",
                                                known="2026-10-01T23:00:00Z", py={"A": 0.6, "B": 0.05}),
                                 "fusion_rule_version": "center_crps_blend_v1"})]
    assert FB.veto_evidence(other_rule, as_of)["station_days"] == 0


def test_the_fit_script_appends_params_once_per_day(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fit_smw", Path(__file__).resolve().parents[1] / "scripts" / "fit_station_model_weights.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    facts = [_fact_record("S", f"2026-09-{d:02d}", at=f"2026-09-{d:02d}T00:00:00Z",
                          known=f"2026-09-{d:02d}T23:00:00Z", py={"A": 0.6, "B": 0.1}) for d in range(1, 29)]
    path = _db(tmp_path / "t.db", facts=facts)
    now = datetime(2026, 9, 30, 6, tzinfo=timezone.utc)
    first = module.run(path, now=now, dry_run=False)
    assert first["params_rows"] == 1 and first["tau"] in FB.TAU_GRID
    assert module.run(path, now=now + timedelta(hours=1), dry_run=False)["skipped"] == "already_fitted_today"
    tables = FB.load_serving_tables(path)
    assert len(tables["params"]["S"]) == 1
    assert tables["vetoes"] and tables["vetoes"][0]["vetoed"] is False


def test_feasible_pulls_a_cap_breach_of_optimiser_size_back_exactly():
    families = {"A1": "A", "A2": "A", "B": "B"}
    caps = {"A": 0.75, "B": 0.75}
    served = FB.feasible({"A1": 0.5, "A2": 0.25 + 3e-10, "B": 0.25 - 3e-10}, families, caps)
    assert served["A1"] + served["A2"] <= 0.75 + 1e-12
    validate_cross_native_ensemble_weights(
        system_weights=served,
        systems_by_id={s: {"independence_family_id": f} for s, f in families.items()},
        family_weight_caps=caps,
    )


def test_the_retired_rule_is_gone_and_the_default_is_blend():
    for name in ("blend_weights", "fit_stack", "TrainingRow", "load_training_rows", "as_of_rows",
                 "FUSION_BLEND_WINDOW_DAYS", "FUSION_BLEND_CENTER_FRACTION"):
        assert not hasattr(FB, name), name
    assert FB.FUSION_BLEND_RULE_VERSION == "station_logscore_mixture_per_system_shrink_v1"
    assert FB.FUSION_BLEND_MODE == FB.FUSION_BLEND_MODE_BLEND
