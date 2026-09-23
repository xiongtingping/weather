"""Charter 199: the P0 fusion blend -- the study's rule, served as a shadow.

The fixture is the fusion-weight study's own input, reduced to what the rule
reads: 1,035 settled day-ahead station-days (their centre errors and CRPS
terms, recomputed here with ``row_statistics`` from the study's reconstructed
distributions) and the 515 targets it scored, with the ``blend50`` weights the
study froze (docs/research/fusion_weight_comparison_20260922/joint_objective).
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from bayes_basket_target import fusion_blend as FB
from bayes_basket_target.native_ensemble_contract import (
    validate_cross_native_ensemble_weights,
)

FIXTURE = Path(__file__).parent / "fixtures" / "charter199_research_fixture.json.gz"


@pytest.fixture(scope="module")
def research():
    data = json.loads(gzip.open(FIXTURE, "rt").read())
    return [FB.TrainingRow(row) for row in data["rows"]], data["targets"]


def _target(t):
    return FB.Target(
        station=t["station"], event_date=t["event_date"],
        decision_at=FB._stamp(t["decision_at"]),
        local_decision_day=datetime.fromisoformat(t["local_decision_day"]).date(),
        weights=t["weights"], families=t["families"], caps=t["caps"], lead=t["lead"],
    )


def test_the_served_functions_reproduce_the_frozen_candidate(research):
    rows, targets = research
    assert len(rows) == 1035 and len(targets) == 515
    worst = 0.0
    for t in targets:
        weights, detail = FB.blend_weights(rows, _target(t))
        worst = max(worst, max(abs(weights[k] - t["expected"][k]) for k in weights))
        worst = max(worst, max(
            abs(detail["center_weights"][k] - t["expected_center"][k]) for k in weights
        ))
        worst = max(worst, max(
            abs(detail["distribution_weights"][k] - t["expected_hier"][k]) for k in weights
        ))
    assert worst < 1e-8


def test_every_blend_on_the_study_sample_passes_the_served_contract(research):
    rows, targets = research
    for t in targets:
        weights, _ = FB.blend_weights(rows, _target(t))
        served = FB.feasible(weights, t["families"], t["caps"])
        assert min(served.values()) >= FB.FUSION_BLEND_MIN_WEIGHT
        validate_cross_native_ensemble_weights(
            system_weights=served,
            systems_by_id={s: {"independence_family_id": t["families"][s]} for s in served},
            family_weight_caps=t["caps"],
        )


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


def _fact(station, day, *, known, at, revision=1, error=(0.5, -1.0)):
    return {
        "station_id": station, "event_date": day, "truth_revision": revision,
        "root_decision_at_utc": at, "truth_known_at_utc": known,
        "systems_json": json.dumps(["A", "B"]),
        "lead_by_system_json": json.dumps({"A": "h12_24", "B": "h12_24"}),
        "center_error_json": json.dumps({"A": error[0], "B": error[1]}),
        "crps_systems_json": json.dumps(["A", "B"]),
        "crps_a_json": json.dumps({"A": 1.0, "B": 1.4}),
        "crps_b_json": json.dumps({"A": {"A": 0.8, "B": 1.1}, "B": {"A": 1.1, "B": 0.9}}),
        "content_sha256": f"{station}{day}{revision}",
    }


def _db(tmp_path, facts):
    path = tmp_path / "training.db"
    conn = sqlite3.connect(path)
    for statement in FB.TRAINING_DDL:
        conn.execute(statement)
    filler = {
        "settlement_timezone": "UTC", "root_checkpoint_id": "c", "root_envelope_sha256": "e",
        "root_posterior_version": "v", "truth_c": 20.0, "truth_id": "t",
        "probability_source": "ENVELOPE", "seat_weights_json": "{}", "family_by_system_json": "{}",
        "family_caps_json": "{}", "bucket_keys_json": "[]", "truth_bucket_index": 0,
        "probabilities_json": "{}", "fusion_blend_state": "NONE", "shadow_weights_json": "{}",
        "schema": FB.FUSION_BLEND_TRAINING_SCHEMA, "created_at_utc": "2026-09-23T00:00:00Z",
    }
    for fact in facts:
        row = {**filler, **fact}
        conn.execute(
            "INSERT INTO fusion_blend_training_fact (" + ",".join(row) + ") VALUES ("
            + ",".join("?" * len(row)) + ")", tuple(row.values()),
        )
    conn.commit()
    conn.close()
    return path


def _overlay(path, mode, station="ZZZZ", decided="2026-09-20T12:00:00Z"):
    return FB.overlay_system_weights(
        station_id=station, event_date="2026-09-21", decision_at_utc=decided,
        timezone_name="UTC", system_weights={"A": 0.5, "B": 0.5},
        family_by_system={"A": "FA", "B": "FB"}, family_caps={"FA": 0.75, "FB": 0.75},
        lead_by_system={"A": "h12_24", "B": "h12_24"}, training_db=path, mode=mode,
    )


@pytest.fixture
def training(tmp_path):
    facts = [
        _fact("ZZZZ", f"2026-09-{day:02d}", at=f"2026-09-{day - 1:02d}T12:00:00Z",
              known=f"2026-09-{day + 1:02d}T01:00:00Z")
        for day in range(10, 19)
    ]
    return _db(tmp_path, facts)


def test_shadow_serves_the_seat_and_records_what_blend_would_serve(training):
    served, receipt = _overlay(training, FB.FUSION_BLEND_MODE_SHADOW)
    blended, blend_receipt = _overlay(training, FB.FUSION_BLEND_MODE_BLEND)
    assert served == {"A": 0.5, "B": 0.5}
    assert receipt["state"] == "SHADOW" and receipt["shadow_state"] == "APPLIED"
    assert receipt["shadow_weights"] == blend_receipt["served_weights"] == blended
    # The centre errors favour A; the blend moves toward it.
    assert blended["A"] > 0.5


def test_off_and_the_unbound_label_stations_serve_the_seat(training):
    assert _overlay(training, FB.FUSION_BLEND_MODE_OFF)[1]["state"] == "OFF"
    served, receipt = _overlay(training, FB.FUSION_BLEND_MODE_BLEND, station="KSEA")
    assert served == {"A": 0.5, "B": 0.5} and receipt["state"] == "SEAT_LABELS_NOT_BOUND"


def test_a_missing_training_file_serves_the_seat(tmp_path):
    served, receipt = _overlay(tmp_path / "absent.db", FB.FUSION_BLEND_MODE_BLEND)
    assert served == {"A": 0.5, "B": 0.5} and receipt["state"] == "SEAT_TRAINING_UNAVAILABLE"


def test_a_truth_not_yet_knowable_does_not_train(tmp_path):
    facts = [
        _fact("ZZZZ", f"2026-09-{day:02d}", at=f"2026-09-{day - 1:02d}T12:00:00Z",
              known="2026-09-25T00:00:00Z")
        for day in range(10, 19)
    ]
    served, receipt = _overlay(_db(tmp_path, facts), FB.FUSION_BLEND_MODE_BLEND)
    assert served == {"A": 0.5, "B": 0.5} and receipt["state"] == "SEAT_NO_TRAINING_DAYS"


def test_the_newest_knowable_revision_is_the_one_read():
    early = FB.TrainingRow({**_fact("S", "2026-09-10", at="2026-09-09T00:00:00Z",
                                    known="2026-09-11T00:00:00Z", revision=1),
                            "content_sha256": "r1"})
    late = FB.TrainingRow({**_fact("S", "2026-09-10", at="2026-09-09T00:00:00Z",
                                   known="2026-09-15T00:00:00Z", revision=2),
                           "content_sha256": "r2"})
    assert [r.identity for r in FB.as_of_rows([early, late], FB._stamp("2026-09-12T00:00:00Z"))] == ["r1"]
    assert [r.identity for r in FB.as_of_rows([early, late], FB._stamp("2026-09-16T00:00:00Z"))] == ["r2"]


def test_the_served_default_is_shadow():
    assert FB.FUSION_BLEND_MODE == FB.FUSION_BLEND_MODE_SHADOW
