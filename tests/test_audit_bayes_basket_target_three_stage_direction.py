from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from bayes_basket_target.contracts import ProbabilityEnvelope
from bayes_basket_target.regime_materialization import build_regime_artifact
from bayes_basket_target.ssot import apply_production_ssot_schema
from scripts.audit_bayes_basket_target_three_stage_direction import (
    audit_three_stage_direction,
)


TZ = "Asia/Seoul"
STATION = "TEST"
EVENT_DATE = "2026-07-27"
BUCKETS = {"33C": 0, "34C": 1, "35C": 2}
DEFAULT_PROBS = {
    "P0": {"33C": 0.10, "34C": 0.60, "35C": 0.30},
    "P0-M": {"33C": 0.20, "34C": 0.55, "35C": 0.25},
    "P1": {"33C": 0.30, "34C": 0.40, "35C": 0.30},
}


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _lineage(stage: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    value = {
        "settlement_timezone": TZ,
        "model_envelope_lower_bucket": "33C",
        "model_envelope_upper_bucket": "35C",
        "model_envelope_span_c": "2.0",
        "stage_calculation_id": f"calc-{stage}",
    }
    if extra:
        value.update(extra)
    return value


def _envelope(
    stage: str,
    probabilities: dict[str, float],
    *,
    extra_lineage=None,
    event_date: str = EVENT_DATE,
    checkpoint_suffix: str = "",
):
    checkpoint_id = f"cp-{stage}{checkpoint_suffix}"
    value = ProbabilityEnvelope.from_mapping(
        {
            "schema": "bayes_basket_probability_envelope_v1",
            "stage": stage,
            "checkpoint_id": checkpoint_id,
            "station_id": STATION,
            "event_date": event_date,
            "settlement_timezone": TZ,
            "decision_at_utc": "2026-07-27T00:00:00Z",
            "fresh_until_utc": "2026-07-28T00:00:00Z",
            "bucket_ordinals": BUCKETS,
            "probabilities": probabilities,
            "posterior_version": f"posterior-{stage}",
            "posterior_sha256": _sha(f"posterior-{stage}"),
            "bridge_sha256": _sha("bridge"),
            "lineage": _lineage(stage, extra_lineage),
        }
    )
    return value


def _insert_envelope(conn, envelope: ProbabilityEnvelope, *, state="COMMITTED"):
    payload = envelope.to_dict()
    conn.execute(
        "INSERT INTO basket_probability_envelope("
        "envelope_id,stage,checkpoint_id,station_id,event_date,settlement_timezone,"
        "decision_at_utc,fresh_until_utc,bucket_ordinals_json,probabilities_json,"
        "posterior_version,posterior_sha256,bridge_sha256,lineage_json,content_sha256,"
        "state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            envelope.checkpoint_id,
            envelope.stage,
            envelope.checkpoint_id,
            envelope.station_id,
            envelope.event_date,
            envelope.settlement_timezone,
            envelope.decision_at_utc,
            envelope.fresh_until_utc,
            json.dumps(envelope.bucket_ordinals, sort_keys=True),
            json.dumps(envelope.probabilities, sort_keys=True),
            envelope.posterior_version,
            envelope.posterior_sha256,
            envelope.bridge_sha256,
            json.dumps(envelope.lineage, sort_keys=True),
            envelope.content_sha256,
            state,
            "2026-07-27T00:00:01Z",
        ),
    )


def _insert_regime(conn, envelope: ProbabilityEnvelope, *, storage_timezone=None):
    artifact = build_regime_artifact(envelope)
    payload = artifact.to_dict()
    conn.execute(
        "INSERT INTO basket_regime_artifact("
        "artifact_id,station_id,event_date,stage,checkpoint_id,settlement_timezone,"
        "probability_sha256,as_of_utc,valid_from_utc,valid_until_utc,artifact_json,"
        "content_sha256,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            artifact.artifact_id,
            artifact.station_id,
            artifact.event_date,
            artifact.stage,
            artifact.checkpoint_id,
            storage_timezone or artifact.settlement_timezone,
            artifact.probability_sha256,
            artifact.as_of_utc,
            artifact.valid_from_utc,
            artifact.valid_until_utc,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            artifact.content_sha256,
            "COMMITTED",
            "2026-07-27T00:00:02Z",
        ),
    )


def _db(
    tmp_path,
    probabilities=None,
    *,
    lineage_by_stage=None,
    storage_timezone_by_stage=None,
    add_regimes=True,
):
    path = tmp_path / "target.db"
    conn = sqlite3.connect(path)
    apply_production_ssot_schema(conn)
    vectors = probabilities or DEFAULT_PROBS
    for stage in ("P0", "P0-M", "P1"):
        extra = (lineage_by_stage or {}).get(stage)
        envelope = _envelope(stage, vectors[stage], extra_lineage=extra)
        _insert_envelope(conn, envelope)
        if add_regimes:
            _insert_regime(
                conn,
                envelope,
                storage_timezone=(storage_timezone_by_stage or {}).get(stage),
            )
    conn.commit()
    conn.close()
    return path


def test_passes_with_three_independent_probability_stages_and_exact_regimes(tmp_path):
    receipt = audit_three_stage_direction(_db(tmp_path))

    assert receipt["status"] == "PASS"
    assert receipt["query_only"] is True
    assert receipt["counts"]["envelopes_loaded"] == 3
    assert receipt["scopes"][0]["stages"]["P0"]["direction"]["expected"]["direction"] == "UP"
    assert receipt["scopes"][0]["stages"]["P1"]["direction"]["expected"]["direction"] == "NEUTRAL"


def test_missing_optional_p0m_uses_direct_p0_to_p1_path(tmp_path):
    path = tmp_path / "missing.db"
    conn = sqlite3.connect(path)
    apply_production_ssot_schema(conn)
    for stage in ("P0", "P1"):
        envelope = _envelope(stage, DEFAULT_PROBS[stage])
        _insert_envelope(conn, envelope)
        _insert_regime(conn, envelope)
    conn.commit()
    conn.close()

    receipt = audit_three_stage_direction(path)
    assert receipt["status"] == "PASS"
    assert receipt["scopes"][0]["stage_path"] == ["P0", "P1"]
    assert "P0-M" not in receipt["scopes"][0]["stages"]


def test_scope_before_first_regime_event_is_historical_not_applicable(
    tmp_path,
):
    path = tmp_path / "pre-regime.db"
    conn = sqlite3.connect(path)
    apply_production_ssot_schema(conn)
    for stage in ("P0", "P1"):
        _insert_envelope(
            conn,
            _envelope(
                stage,
                DEFAULT_PROBS[stage],
                event_date="2026-07-26",
                checkpoint_suffix="-old",
            ),
        )
    for stage in ("P0", "P0-M", "P1"):
        envelope = _envelope(
            stage,
            DEFAULT_PROBS[stage],
            checkpoint_suffix="-current",
        )
        _insert_envelope(conn, envelope)
        _insert_regime(conn, envelope)
    conn.commit()
    conn.close()

    receipt = audit_three_stage_direction(path)

    assert receipt["status"] == "PASS"
    historical = next(
        row for row in receipt["scopes"]
        if row["event_date"] == "2026-07-26"
    )
    assert historical["stage_path"] == ["P0", "P1"]
    assert {
        row["status"] for row in historical["stages"].values()
    } == {"NOT_APPLICABLE_PRE_REGIME"}


def test_center_lead_of_0_7_percent_is_neutral(tmp_path):
    vectors = dict(DEFAULT_PROBS)
    vectors["P0"] = {"33C": 0.20, "34C": 0.4035, "35C": 0.3965}
    receipt = audit_three_stage_direction(_db(tmp_path, vectors))

    assert receipt["status"] == "PASS"
    p0 = receipt["scopes"][0]["stages"]["P0"]["direction"]
    assert p0["expected"]["center_lead_probability"] == pytest.approx(0.007)
    assert p0["expected"]["center_stable"] is False
    assert p0["expected"]["direction"] == "NEUTRAL"


def test_market_anchor_lineage_is_fail_closed(tmp_path):
    path = _db(
        tmp_path,
        lineage_by_stage={"P0": {"market_anchor": "should-never-be-read"}},
    )
    receipt = audit_three_stage_direction(path)

    assert receipt["status"] == "BLOCKED"
    assert any(item["code"] == "probability_envelope_invalid" for item in receipt["blockers"])
    assert any("direction_market_field_forbidden" in str(item) for item in receipt["blockers"])


def test_market_registry_metadata_is_not_direction_dependency(tmp_path):
    path = _db(
        tmp_path,
        lineage_by_stage={
            "P0": {
                "market_registry_id": "registry-1",
                "market_slug": "test-market",
                "decision_anchor_utc": "2026-07-27T00:00:00Z",
                "price_unit": "probability_only_metadata",
            }
        },
    )
    receipt = audit_three_stage_direction(path)

    assert receipt["status"] == "PASS"


def test_same_probability_with_independent_stage_checkpoint_lineage_passes(tmp_path):
    same = {"33C": 0.20, "34C": 0.50, "35C": 0.30}
    vectors = {"P0": same, "P0-M": same, "P1": same}
    receipt = audit_three_stage_direction(_db(tmp_path, vectors))

    assert receipt["status"] == "PASS"
    binding = receipt["scopes"][0]["stage_binding"]
    assert binding["numeric_vector_reuse_allowed"] is True
    assert binding["stage_ids_distinct"] is True
    assert binding["checkpoint_ids_distinct"] is True


@pytest.mark.parametrize(
    ("lineage_key", "lineage_value"),
    (("stage", "P0"), ("checkpoint_id", "wrong-checkpoint")),
)
def test_stage_or_checkpoint_lineage_mismatch_is_blocked(
    tmp_path, lineage_key, lineage_value
):
    path = _db(
        tmp_path,
        lineage_by_stage={"P1": {lineage_key: lineage_value}},
    )
    receipt = audit_three_stage_direction(path)

    assert receipt["status"] == "BLOCKED"
    assert any(item["code"] == "probability_envelope_invalid" for item in receipt["blockers"])
    assert any(
        "probability_" + ("stage" if lineage_key == "stage" else "checkpoint") + "_lineage_mismatch"
        in str(item)
        for item in receipt["blockers"]
    )


def test_regime_timezone_storage_mismatch_is_blocked(tmp_path):
    path = _db(
        tmp_path,
        storage_timezone_by_stage={"P1": "America/New_York"},
    )

    receipt = audit_three_stage_direction(path)
    assert receipt["status"] == "BLOCKED"
    assert any(item["code"] == "regime_artifact_invalid" for item in receipt["blockers"])
    assert any("regime_storage_scope_mismatch" in str(item) for item in receipt["blockers"])


def test_committed_envelope_read_cap_is_500(tmp_path):
    path = tmp_path / "over_limit.db"
    conn = sqlite3.connect(path)
    apply_production_ssot_schema(conn)
    for index in range(501):
        station = f"S{index:03d}"
        envelope = ProbabilityEnvelope.from_mapping(
            {
                "schema": "bayes_basket_probability_envelope_v1",
                "stage": "P0",
                "checkpoint_id": f"cp-{index}",
                "station_id": station,
                "event_date": EVENT_DATE,
                "settlement_timezone": TZ,
                "decision_at_utc": "2026-07-27T00:00:00Z",
                "fresh_until_utc": "2026-07-28T00:00:00Z",
                "bucket_ordinals": BUCKETS,
                "probabilities": DEFAULT_PROBS["P0"],
                "posterior_version": f"posterior-{index}",
                "posterior_sha256": _sha(f"posterior-{index}"),
                "bridge_sha256": _sha("bridge"),
                "lineage": {
                    "settlement_timezone": TZ,
                    "model_envelope_lower_bucket": "33C",
                    "model_envelope_upper_bucket": "35C",
                    "model_envelope_span_c": "2.0",
                },
            }
        )
        _insert_envelope(conn, envelope)
    conn.commit()
    conn.close()

    receipt = audit_three_stage_direction(path)
    assert receipt["status"] == "BLOCKED"
    assert receipt["counts"]["committed_envelopes"] == 501
    assert receipt["counts"]["envelopes_loaded"] == 0
    assert any(item["code"] == "basket_probability_envelope_read_limit_exceeded" for item in receipt["blockers"])
