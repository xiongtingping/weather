from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from collections import Counter

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.native_daily_max_manifest import (
    build_decode_evidence_terminal_bundle,
    build_native_identity_index_from_decode_evidence,
    build_native_daily_max_manifest,
    expected_et_hourly_slots,
    verify_arrival_transport_bindings,
)
from bayes_basket_target.model_product_identity import (
    build_model_product_identity_receipt,
)


ROOT = Path(__file__).resolve().parents[1]
REAL_EVIDENCE = (
    ROOT / "output/audits/bayes_basket_target_native_model_arrival_decode_ecmwf_20260725_00z.json",
    ROOT / "output/audits/bayes_basket_target_native_model_arrival_decode_gfs_20260725_00z.json",
    ROOT / "output/audits/bayes_basket_target_native_model_arrival_decode_icon_20260725_00z_exact5.json",
)


def _identity(product_id: str = "ECMWF_AIFS_SINGLE_0P25") -> dict:
    matrix = json.loads(
        (ROOT / "output/audits/pr05_pr09_model_coverage_v5_20260804.json").read_text(
            encoding="utf-8"
        )
    )
    row = next(
        item for item in matrix["rows"]
        if item["product_id"] == product_id
        and item["station_id"] == "KATL" and item["lead_bucket"] == "d0"
    )
    receipt, missing = build_model_product_identity_receipt(row)
    assert missing == () and receipt is not None
    return receipt


def _points() -> list[dict]:
    result = []
    for index, slot in enumerate(expected_et_hourly_slots("2026-07-25")):
        result.append({
            "valid_time_utc": slot,
            "forecast_temperature_c": 10.0 + index,
            "published_at_utc": "2026-07-25T01:00:00Z",
            "received_at_utc": "2026-07-25T01:01:00Z",
            "decision_at_utc": "2026-07-25T01:02:00Z",
            "raw_object_sha256": f"{index + 1:064x}",
            "transport_receipt_sha256": f"{index + 101:064x}",
            "decoder_id": "test.hourly.2t.v1",
        })
    return result


from tests.ephemeral_audit_support import requires_audit_artifacts

# output/audits is untracked and pruned by age (commit 10f91279), so these
# decode-evidence and coverage receipts are present only until the pruner
# next runs.  Scoped per test: four of the eight here need them, and a
# module-level marker would take the other four with it.
_NEEDS_DECODE_EVIDENCE = requires_audit_artifacts(
    *REAL_EVIDENCE,
    ROOT / "output/audits/pr05_pr09_model_coverage_v5_20260804.json",
    ROOT / "output/audits/bayes_basket_target_current_et_20260726/native_decode_evidence.json",
    ROOT / "output/audits/bayes_basket_target_current_et_20260726/model_arrival_evidence.json",
)


@_NEEDS_DECODE_EVIDENCE
def test_complete_et_hourly_manifest_recomputes_max_and_sets() -> None:
    manifest = build_native_daily_max_manifest(
        identity_receipt=_identity(), event_date="2026-07-25",
        run_init_utc="2026-07-25T00:00:00Z", points=_points()
    )
    assert manifest["state"] == "MATERIALIZED_UNPUBLISHED"
    assert manifest["forecast_value_c"] == 33.0
    assert len(manifest["expected_hourly_slots"]) == 24
    assert len(manifest["raw_object_sha256s"]) == 24
    assert manifest["consumable"] is False


@_NEEDS_DECODE_EVIDENCE
def test_manifest_rejects_missing_slot_and_uses_latest_component_decision() -> None:
    with pytest.raises(ContractError, match="hourly_coverage_incomplete"):
        build_native_daily_max_manifest(
            identity_receipt=_identity(), event_date="2026-07-25",
            run_init_utc="2026-07-25T00:00:00Z", points=_points()[:-1]
        )
    points = _points()
    points[-1]["decision_at_utc"] = "2026-07-25T01:03:00Z"
    manifest = build_native_daily_max_manifest(
        identity_receipt=_identity(), event_date="2026-07-25",
        run_init_utc="2026-07-25T00:00:00Z", points=points
    )
    assert manifest["forecast_decision_at_utc"] == "2026-07-25T01:03:00.000Z"


@_NEEDS_DECODE_EVIDENCE
def test_manifest_rejects_republisher_and_clock_reversal() -> None:
    identity = _identity("ECMWF_AIFS_OPEN_METEO_REPUBLISHER")
    with pytest.raises(ContractError, match="native_source_required"):
        build_native_daily_max_manifest(
            identity_receipt=identity, event_date="2026-07-25",
            run_init_utc="2026-07-25T00:00:00Z", points=_points()
        )
    points = _points()
    points[0]["received_at_utc"] = "2026-07-25T01:03:00Z"
    with pytest.raises(ContractError, match="point_clock_order_invalid"):
        build_native_daily_max_manifest(
            identity_receipt=_identity(), event_date="2026-07-25",
            run_init_utc="2026-07-25T00:00:00Z", points=points
        )


@_NEEDS_DECODE_EVIDENCE
def test_real_three_model_evidence_yields_135_exact_terminals() -> None:
    evidences = [json.loads(path.read_text(encoding="utf-8")) for path in REAL_EVIDENCE]
    bundle = build_decode_evidence_terminal_bundle(
        evidences, as_of_utc="2026-07-25T08:30:00Z"
    )
    assert bundle["state"] == "BLOCKED"
    assert bundle["scope_count"] == 135
    assert bundle["manifest_count"] == 0
    assert bundle["terminal_count"] == 135
    assert bundle["daily_max_data_complete_scope_count"] == 0
    assert bundle["blocker_counts"] == {"LINEAGE_MISMATCH": 135}
    assert {item["model_id"] for item in bundle["terminals"]} == {
        "ECMWF", "GFS", "ICON",
    }
    assert Counter(item["model_id"] for item in bundle["terminals"]) == {
        "ECMWF": 45, "GFS": 45, "ICON": 45,
    }
    assert {item["observed_point_count"] for item in bundle["terminals"]} == {5}
    assert {len(item["missing_hourly_slots"]) for item in bundle["terminals"]} == {19}
    assert bundle["consumable"] is False


@_NEEDS_DECODE_EVIDENCE
def test_decode_evidence_tamper_is_rejected_even_if_payload_still_parses() -> None:
    evidence = json.loads(REAL_EVIDENCE[0].read_text(encoding="utf-8"))
    tampered = deepcopy(evidence)
    tampered["decoded_points"][0]["temperature_2m_c"] += 1.0
    with pytest.raises(ContractError, match="decode_evidence_receipt_mismatch"):
        build_decode_evidence_terminal_bundle(
            [tampered], as_of_utc="2026-07-25T08:30:00Z"
        )


@_NEEDS_DECODE_EVIDENCE
def test_current_et_arrival_join_closes_decision_but_not_identity_or_truth() -> None:
    decode = json.loads(
        (ROOT / "output/audits/bayes_basket_target_current_et_20260726/native_decode_evidence.json").read_text()
    )
    arrivals = json.loads(
        (ROOT / "output/audits/bayes_basket_target_current_et_20260726/model_arrival_evidence.json").read_text()
    )
    bundle = build_decode_evidence_terminal_bundle(
        [decode], arrival_evidences=[arrivals],
        transport_bindings=verify_arrival_transport_bindings(arrivals),
        as_of_utc="2026-07-26T11:25:33Z",
    )
    assert bundle["scope_count"] == 135
    assert bundle["daily_max_data_complete_scope_count"] == 45
    assert bundle["daily_max_data_complete_by_model"] == {
        "ECMWF": 0, "GFS": 0, "ICON": 45,
    }
    assert all(item["forecast_decision_at_utc"] is not None for item in bundle["terminals"])
    assert all(item["arrival_vector_sha256"] is not None for item in bundle["terminals"])
    assert all(item["transport_receipt_set_sha256"] is not None for item in bundle["terminals"])
    assert all(
        "FORECAST_DECISION_CLOCK_MISSING_FROM_DECODE_EVIDENCE"
        not in item["diagnostic_gaps"]
        for item in bundle["terminals"]
    )
    assert all(
        "TRANSPORT_RECEIPT_OBJECT_BINDING_NOT_EMBEDDED"
        not in item["diagnostic_gaps"]
        for item in bundle["terminals"]
    )


@_NEEDS_DECODE_EVIDENCE
def test_current_et_native_identity_materializes_45_icon_manifests() -> None:
    decode = json.loads(
        (ROOT / "output/audits/bayes_basket_target_current_et_20260726/native_decode_evidence.json").read_text()
    )
    arrivals = json.loads(
        (ROOT / "output/audits/bayes_basket_target_current_et_20260726/model_arrival_evidence.json").read_text()
    )
    identities = build_native_identity_index_from_decode_evidence(
        [decode], lead_bucket="d2", run_cycle="18Z"
    )
    assert len(identities) == 135
    bundle = build_decode_evidence_terminal_bundle(
        [decode], arrival_evidences=[arrivals],
        transport_bindings=verify_arrival_transport_bindings(arrivals),
        identity_receipts=identities, as_of_utc="2026-07-26T11:25:33Z",
    )
    assert bundle["scope_count"] == 135
    assert bundle["manifest_count"] == 45
    assert bundle["terminal_count"] == 90
    assert bundle["daily_max_data_complete_scope_count"] == 45
    assert bundle["daily_max_data_complete_by_model"] == {
        "ECMWF": 0, "GFS": 0, "ICON": 45,
    }
    assert bundle["blocker_counts"] == {"DATA_MISSING": 90}
    assert {item["model_id"] for item in bundle["manifests"]} == {"ICON"}
    assert all(item["lead_bucket"] == "d2" for item in bundle["manifests"])
    assert all(item["run_cycle"] == "18Z" for item in bundle["manifests"])
    assert all(item["consumable"] is False for item in bundle["manifests"])


def test_dst_days_have_exact_23_and_25_hour_slots() -> None:
    assert len(expected_et_hourly_slots("2026-03-08")) == 23
    assert len(expected_et_hourly_slots("2026-11-01")) == 25
