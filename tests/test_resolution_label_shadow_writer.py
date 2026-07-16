from __future__ import annotations

import hashlib
import sqlite3

import pytest

from resolution_bridge_runtime import build_final_resolution_label
from resolution_label_shadow_writer import (
    LABEL_TABLE,
    initialize_isolated_resolution_label_staging_schema,
    persist_final_resolution_label_proposal_shadow,
    persist_final_resolution_label_shadow,
)
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError


def _lineage(*, source_id: str, source_observation_id: str, observed_at: str) -> dict:
    return {
        "source_id": source_id,
        "source_observation_ids": [source_observation_id],
        "raw_sha256": hashlib.sha256(source_observation_id.encode("utf-8")).hexdigest(),
        "raw_object_ref": f"research://raw/{source_observation_id}.json",
        "observed_at": observed_at,
        "finality": "final",
    }


def _label(*, contract_revision: str = "") -> dict:
    return build_final_resolution_label(
        target_identity="highest-temperature-in-chengdu-on-july-3-2026",
        event_date="2026-07-03",
        physical_high_label={
            "final_physical_high_c": 30.0,
            **_lineage(
                source_id="physical_truth_zuuu",
                source_observation_id="physical-20260703",
                observed_at="2026-07-03T18:00:00Z",
            ),
        },
        settlement_label={
            "settlement_scalar_c": 31.0,
            **_lineage(
                source_id="official_resolution_source",
                source_observation_id="settlement-20260703",
                observed_at="2026-07-03T19:00:00Z",
            ),
        },
        resolution_contract={
            "resolution_contract_id": "resolution_zuuu_daily_high_v1",
            "resolution_source_id": "official_resolution_source",
            "resolution_local_date": "2026-07-03",
            "settlement_unit": "C",
            "physical_to_settlement_kernel": {"30": [{"settlement_value_c": 31.0, "probability": 1.0}]},
            "bucket_contract": {"31°C": {"lower_inclusive": 30.5, "upper_exclusive": 31.5}},
            "bridge_closed": True,
            "validated": True,
            **({"resolution_revision": contract_revision} if contract_revision else {}),
        },
        label_available_at="2026-07-03T19:00:00Z",
    )


def test_final_label_registry_is_isolated_append_only_and_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_resolution_label_staging_schema(conn)
    label = _label()

    first = persist_final_resolution_label_shadow(conn, label)
    second = persist_final_resolution_label_shadow(conn, label)

    assert first["ok"] is True and first["idempotent"] is False
    assert second["ok"] is True and second["idempotent"] is True
    row = conn.execute(f"SELECT final_physical_high_c,settlement_scalar_c,market_settlement_bucket FROM {LABEL_TABLE}").fetchone()
    assert row == (30.0, 31.0, "31°C")


def test_final_label_registry_rejects_ordinary_db_and_label_or_contract_identity_drift() -> None:
    normal = sqlite3.connect(":memory:")
    with pytest.raises(ShadowTransactionIntegrityError, match="resolution_label_shadow_manifest_missing"):
        initialize_isolated_resolution_label_staging_schema(normal)

    isolated = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(isolated)
    initialize_isolated_resolution_label_staging_schema(isolated)
    label = _label()
    persist_final_resolution_label_shadow(isolated, label)
    tampered = dict(label)
    tampered["label_available_at"] = "2026-07-03T20:00:00Z"
    with pytest.raises(ValueError, match="resolution_final_label_not_valid:.*sha256_mismatch"):
        persist_final_resolution_label_shadow(isolated, tampered)

    contract_drift = _label(contract_revision="revision-without-new-contract-id")
    with pytest.raises(ShadowTransactionIntegrityError, match="resolution_final_label_contract_identity_drift"):
        persist_final_resolution_label_shadow(isolated, contract_drift)


def test_ready_final_label_proposal_binds_identity_before_persisting() -> None:
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_resolution_label_staging_schema(conn)
    label = _label()
    proposal = {
        "ok": True,
        "state": "proposal_ready",
        "target_identity": label["target_identity"],
        "event_date": label["event_date"],
        "resolution_final_label": label,
    }

    persisted = persist_final_resolution_label_proposal_shadow(conn, proposal)

    assert persisted["ok"] is True
    proposal["event_date"] = "2026-07-04"
    with pytest.raises(ValueError, match="resolution_final_label_proposal_event_date_mismatch"):
        persist_final_resolution_label_proposal_shadow(conn, proposal)
