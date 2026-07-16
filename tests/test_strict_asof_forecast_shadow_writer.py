from __future__ import annotations

import json
import sqlite3

import pytest

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from forecast_revision_canonical_runtime import canonicalize_forecast_revisions
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError
from strict_asof_forecast_capture_runtime import classify_strict_asof_forecast_capture
from strict_asof_forecast_source_observation_runtime import (
    build_tier_a_forecast_source_observation_proposal,
)
from strict_asof_forecast_shadow_writer import (
    initialize_isolated_forecast_capture_staging_schema,
    persist_tier_a_forecast_source_observation_proposal,
)


def _proposal() -> dict:
    envelope = build_external_forecast_raw_envelope(
        source_id="ecmwf",
        response_body_utf8='{"raw":"provider"}',
        requested_at="2026-07-11T00:00:00.000Z",
        response_started_at="2026-07-11T00:00:00.010Z",
        response_completed_at="2026-07-11T00:00:00.200Z",
        provider_available_at="2026-07-11T00:00:00.200Z",
        http_status=200,
        parser_version="capture-v2",
        source_event_id="forward_ecmwf_2026-07-12",
        capture_time_provenance="observed_transport",
        response_headers_observed=True,
    )
    forecast = {
        "source_id": "ecmwf",
        "target_event_date_local": "2026-07-12",
        "forecast_peak_c": 33.0,
        "forecast_snapshot_id": envelope["response_sha256"],
        "source_event_id": "forward_ecmwf_2026-07-12",
        "raw_provider_response_present": True,
        "hourly_curve": [{"valid_time_local": "2026-07-12T15:00:00+08:00", "temperature_c": 33.0}],
    }
    capture = classify_strict_asof_forecast_capture(raw_envelope=envelope, normalized_forecast=forecast)
    return build_tier_a_forecast_source_observation_proposal(
        capture=capture,
        normalized_forecast=forecast,
        station_id="ZUUU",
        raw_ref="research://forecast/ecmwf/20260711T000000Z.json.gz",
    )


def test_isolated_staging_persists_only_tier_a_append_only_source_observation_and_revision_can_read_it() -> None:
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_forecast_capture_staging_schema(conn)
    proposal = _proposal()

    first = persist_tier_a_forecast_source_observation_proposal(conn, proposal)
    second = persist_tier_a_forecast_source_observation_proposal(conn, proposal)

    assert first["ok"] is True and first["idempotent"] is False
    assert second["ok"] is True and second["idempotent"] is True
    row = conn.execute("SELECT * FROM source_observations").fetchone()
    columns = [item[1] for item in conn.execute("PRAGMA table_info(source_observations)").fetchall()]
    observation = dict(zip(columns, row))
    observation["payload"] = json.loads(observation.pop("payload_json"))
    revision = canonicalize_forecast_revisions(
        [observation],
        target_identity="highest-temperature-in-chengdu-on-july-12-2026",
        event_date="2026-07-12",
        decision_as_of="2026-07-11T00:00:01Z",
    )
    assert revision["ok"] is True
    assert revision["source_revisions"][0]["source_id"] == "ecmwf"
    assert revision["source_revisions"][0]["available_at"] == "2026-07-11T00:00:00.200Z"


def test_isolated_staging_refuses_normal_db_and_replay_drift() -> None:
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ShadowTransactionIntegrityError, match="strict_asof_forecast_shadow_manifest_missing"):
        initialize_isolated_forecast_capture_staging_schema(conn)

    isolated = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(isolated)
    initialize_isolated_forecast_capture_staging_schema(isolated)
    proposal = _proposal()
    persist_tier_a_forecast_source_observation_proposal(isolated, proposal)
    proposal["source_observation"]["payload"]["forecast_snapshot_id"] = "tampered"

    with pytest.raises(ShadowTransactionIntegrityError, match="strict_asof_forecast_source_event_replay_drift"):
        persist_tier_a_forecast_source_observation_proposal(isolated, proposal)
