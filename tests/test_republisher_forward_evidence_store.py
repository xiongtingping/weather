from __future__ import annotations

import hashlib
import json

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.republisher_forward_evidence_store import (
    store_manifest,
    store_probe_evidence,
    verify_address,
)


def _hashed(value: dict[str, object]) -> dict[str, object]:
    return {**value, "content_sha256": content_sha256(value)}


def _probe_result() -> dict[str, object]:
    products = []
    for model in ("AIFS", "ICON"):
        terminal_identity = {
            "upstream_model_id": model,
            "receipt_content_sha256": "1" * 64,
        }
        products.append(
            {
                "upstream_model_id": model,
                "object_receipts": [{"model": model, "status": 404}],
                "native_unavailability_receipt": terminal_identity,
            }
        )
    return _hashed(
        {
            "schema": "republisher_native_forward_probe_result_v1",
            "product_results": products,
        }
    )


def _capture(model: str) -> dict[str, object]:
    raw = (model + "-raw").encode()
    return {
        "raw_body": raw,
        "normalized_curve": {"model": model, "values": [1.0, 2.0]},
        "component_receipt": {
            "upstream_model_id": model,
            "raw_object_sha256": hashlib.sha256(raw).hexdigest(),
        },
    }


def test_manifest_owns_pre_post_raw_curve_and_receipt_objects(tmp_path) -> None:
    plan = _hashed({"schema": "republisher_native_forward_probe_v1"})
    result = _probe_result()
    stored = store_manifest(
        tmp_path,
        station_id="CYYZ",
        event_date="2026-08-04",
        settlement_timezone="America/Toronto",
        pre_probe_plan=plan,
        pre_probe_result=result,
        captures={"AIFS": _capture("AIFS"), "ICON": _capture("ICON")},
        post_probe_plan=plan,
        post_probe_result=result,
    )
    manifest_path = verify_address(
        tmp_path,
        content_address=stored["manifest_content_address"],
        expected_sha256=stored["manifest_content_sha256"],
    )
    manifest = json.loads(manifest_path.read_text())
    roles = {row["role"] for row in manifest["object_entries"]}
    assert {
        "native_pre_probe_plan",
        "native_pre_probe_result",
        "aifs_open_meteo_raw",
        "aifs_normalized_curve",
        "aifs_component_receipt",
        "icon_open_meteo_raw",
        "icon_normalized_curve",
        "icon_component_receipt",
        "native_post_probe_plan",
        "native_post_probe_result",
        "aifs_native_object_receipt_set",
        "icon_native_object_receipt_set",
    }.issubset(roles)
    assert manifest["object_entry_set_sha256"] == content_sha256(
        manifest["object_entries"]
    )

    replay = store_manifest(
        tmp_path,
        station_id="CYYZ",
        event_date="2026-08-04",
        settlement_timezone="America/Toronto",
        pre_probe_plan=plan,
        pre_probe_result=result,
        captures={"AIFS": _capture("AIFS"), "ICON": _capture("ICON")},
        post_probe_plan=plan,
        post_probe_result=result,
    )
    assert replay["manifest_content_sha256"] == stored["manifest_content_sha256"]


def test_content_address_tamper_fails_closed(tmp_path) -> None:
    plan = _hashed({"schema": "republisher_native_forward_probe_v1"})
    result = _probe_result()
    stored = store_manifest(
        tmp_path,
        station_id="CYYZ",
        event_date="2026-08-04",
        settlement_timezone="America/Toronto",
        pre_probe_plan=plan,
        pre_probe_result=result,
        captures={"AIFS": _capture("AIFS"), "ICON": _capture("ICON")},
        post_probe_plan=plan,
        post_probe_result=result,
    )
    path = verify_address(
        tmp_path,
        content_address=stored["manifest_content_address"],
        expected_sha256=stored["manifest_content_sha256"],
    )
    path.write_bytes(b"tampered")
    with pytest.raises(ContractError, match="object_missing"):
        verify_address(
            tmp_path,
            content_address=stored["manifest_content_address"],
            expected_sha256=stored["manifest_content_sha256"],
        )


def test_blocked_probe_retains_native_index_body(tmp_path) -> None:
    plan = _hashed({"schema": "republisher_native_forward_probe_v1"})
    raw = b'{"param":"2t"}\n'
    result = _probe_result()
    result["_retained_raw_objects"] = [
        {
            "upstream_model_id": "AIFS",
            "object_url": "https://example.invalid/aifs.grib2",
            "probe_url": "https://example.invalid/aifs.index",
            "valid_time_utc": "2026-08-04T04:00:00.000Z",
            "raw_object_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_body": raw,
        }
    ]
    stored = store_probe_evidence(
        tmp_path,
        phase="pre",
        plan=plan,
        result=result,
    )
    manifest_path = verify_address(
        tmp_path,
        content_address=stored["manifest_content_address"],
        expected_sha256=stored["manifest_content_sha256"],
    )
    manifest = json.loads(manifest_path.read_text())
    assert any(
        row["role"] == "native_pre_probe_raw_000"
        and row["content_sha256"] == hashlib.sha256(raw).hexdigest()
        for row in manifest["object_entries"]
    )
