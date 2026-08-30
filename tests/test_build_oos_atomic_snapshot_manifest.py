from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import content_sha256
from scripts import build_oos_atomic_snapshot_manifest as snapshot


STATION_IDS = tuple(f"T{index:03d}" for index in range(expected_station_count()))


def _write(path: Path, identity: dict) -> dict:
    value = {**identity, "content_sha256": content_sha256(identity)}
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return value


def _args(tmp_path: Path) -> argparse.Namespace:
    p0 = tmp_path / "p0.json"
    p0_value = _write(p0, {
        "schema": "bayes_basket_target_p0_inbox_v1",
        "generated_at_utc": "2026-08-02T00:00:00Z",
        "packets": [
            {"packet": {"station_id": station_id}}
            for station_id in STATION_IDS
        ],
    })
    taf = tmp_path / "taf.json"
    _write(taf, {
        "schema": "bayes_basket_target_taf_p0m_exact_join_45station_audit_v1",
        "input_artifacts": {"p0_inbox": {
            "content_sha256": p0_value["content_sha256"],
            "generated_at_utc": p0_value["generated_at_utc"],
        }},
        "as_of_utc": "2026-08-02T00:00:00Z",
        "entries": [{"station_id": station_id} for station_id in STATION_IDS],
    })
    linkage = _write(tmp_path / "linkage.json", {
        "schema": "native_ensemble_activation_oos_linkage_audit_v1",
        "as_of_utc": "2026-08-02T00:00:00Z",
        "active_exact_oos_consumable_activation_station_ids": list(STATION_IDS),
    })
    pointer_identity = {
        "schema": "native_ensemble_artifact_pointer_v1",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "selection_evidence_sha256": "a" * 64,
        "cross_ensemble_artifact_id": "native-cross-artifact-test",
        "cross_artifact_content_sha256": "c" * 64,
        "activation_id": "native-activation-test",
        "activation_identity_sha256": "d" * 64,
        "generation": 1,
    }
    pointer = {
        **pointer_identity,
        "pointer_sha256": content_sha256(pointer_identity),
    }
    repair = _write(tmp_path / "repair.json", {
        "schema": "native_ensemble_activation_repair_plan_v1",
        "as_of_utc": "2026-08-02T00:00:00Z",
        "canonical_station_ids": list(STATION_IDS),
        "artifact_pointer_count": len(STATION_IDS),
        "candidate_plans": [{
            "action": "NOOP_ALREADY_CONSUMABLE",
            "candidate_state": "PRODUCTION_NO_LIVE_OOS_PASS",
            "selection_evidence_sha256": "a" * 64,
            "scope": {
                "station_id": station_id,
                "season_bin": "JJA",
                "lead_bucket": "d2",
                "run_cycle": "18Z",
                "run_cycle_by_system_json": "{\"X\":\"18Z\"}",
                "run_cycle_by_system_sha256": content_sha256({"X": "18Z"}),
            },
            "artifact_pointer": pointer,
            "expected_artifact": {
                "cross_ensemble_artifact_id": "native-cross-artifact-test",
                "cross_artifact_content_sha256": "c" * 64,
                "activation_id": "native-activation-test",
                "activation_identity_sha256": "d" * 64,
                "generation": 1,
            },
        } for station_id in STATION_IDS],
    })
    return argparse.Namespace(
        p0_inbox=p0,
        taf_audit=taf,
        native_linkage=tmp_path / "linkage.json",
        native_repair=tmp_path / "repair.json",
        output=tmp_path / "manifest.json",
        as_of_utc="2026-08-02T00:00:00Z",
        max_retries=2,
    )


def test_snapshot_binds_p0_pointer_and_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    args = _args(tmp_path)
    monkeypatch.setattr(
        snapshot,
        "coordinate_registry_manifest",
        lambda: {"station_ids": list(STATION_IDS)},
    )
    result = snapshot.build(args)
    assert result["schema"] == snapshot.SCHEMA
    assert result["production_no_live"] is True
    assert result["writes_production_database"] is False
    assert result["input_artifacts"]["p0_inbox"]["packet_count"] == expected_station_count()
    assert result["input_artifacts"]["taf_audit"]["p0_inbox_content_sha256"] == result["input_artifacts"]["p0_inbox"]["declared_content_sha256"]
    assert result["input_artifacts"]["taf_audit"]["evidence_clock_utc"] == "2026-08-02T00:00:00.000Z"
    assert result["input_artifacts"]["taf_audit"]["evidence_clock_field"] == "as_of_utc"
    assert result["scope_fingerprint"]
    assert result["native_p0_binding"]["binding_count"] == 45
    assert result["native_p0_binding"]["source_linkage_content_sha256"] == result["input_artifacts"]["native_linkage"]["declared_content_sha256"]
    assert result["native_p0_binding"]["source_repair_content_sha256"] == result["input_artifacts"]["native_repair"]["declared_content_sha256"]


def test_snapshot_accepts_taf_generated_evidence_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path)
    taf = json.loads(args.taf_audit.read_text(encoding="utf-8"))
    taf.pop("as_of_utc")
    taf["generated_at_utc"] = "2026-08-02T00:00:00Z"
    identity = {key: value for key, value in taf.items() if key != "content_sha256"}
    taf["content_sha256"] = content_sha256(identity)
    args.taf_audit.write_text(json.dumps(taf, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        snapshot,
        "coordinate_registry_manifest",
        lambda: {"station_ids": list(STATION_IDS)},
    )

    result = snapshot.build(args)

    assert result["input_artifacts"]["taf_audit"]["evidence_clock_field"] == "generated_at_utc"
    assert result["input_artifacts"]["taf_audit"]["evidence_clock_utc"] == "2026-08-02T00:00:00.000Z"


def test_snapshot_rejects_taf_without_evidence_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path)
    taf = json.loads(args.taf_audit.read_text(encoding="utf-8"))
    taf.pop("as_of_utc")
    taf.pop("generated_at_utc", None)
    identity = {key: value for key, value in taf.items() if key != "content_sha256"}
    taf["content_sha256"] = content_sha256(identity)
    args.taf_audit.write_text(json.dumps(taf, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        snapshot,
        "coordinate_registry_manifest",
        lambda: {"station_ids": list(STATION_IDS)},
    )

    with pytest.raises(snapshot.SnapshotError, match="taf_audit_generated_at_utc_missing"):
        snapshot.build(args)


def test_snapshot_rejects_future_taf_evidence_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path)
    taf = json.loads(args.taf_audit.read_text(encoding="utf-8"))
    taf["as_of_utc"] = "2026-08-02T00:00:00.001Z"
    identity = {key: value for key, value in taf.items() if key != "content_sha256"}
    taf["content_sha256"] = content_sha256(identity)
    args.taf_audit.write_text(json.dumps(taf, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        snapshot,
        "coordinate_registry_manifest",
        lambda: {"station_ids": list(STATION_IDS)},
    )

    with pytest.raises(
        snapshot.SnapshotError,
        match="taf_audit_as_of_utc_after_snapshot_as_of",
    ):
        snapshot.build(args)


def test_snapshot_rejects_taf_evidence_clock_without_utc_z(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path)
    taf = json.loads(args.taf_audit.read_text(encoding="utf-8"))
    taf["as_of_utc"] = "2026-08-02T00:00:00+00:00"
    identity = {key: value for key, value in taf.items() if key != "content_sha256"}
    taf["content_sha256"] = content_sha256(identity)
    args.taf_audit.write_text(json.dumps(taf, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        snapshot,
        "coordinate_registry_manifest",
        lambda: {"station_ids": list(STATION_IDS)},
    )

    with pytest.raises(snapshot.SnapshotError, match="taf_audit_as_of_utc_must_be_utc_z"):
        snapshot.build(args)


def test_snapshot_rejects_unbound_taf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _args(tmp_path)
    taf = args.taf_audit
    value = json.loads(taf.read_text(encoding="utf-8"))
    value["input_artifacts"] = {}
    identity = {k: v for k, v in value.items() if k != "content_sha256"}
    value["content_sha256"] = content_sha256(identity)
    taf.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(snapshot, "coordinate_registry_manifest", lambda: {"station_ids": [f"T{i:03d}" for i in range(expected_station_count())]})
    with pytest.raises(snapshot.SnapshotError, match="taf_p0_input_pointer_missing"):
        snapshot.build(args)


def test_snapshot_rejects_p0_drift_after_taf_was_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path)
    p0 = json.loads(args.p0_inbox.read_text(encoding="utf-8"))
    p0["packets"][0]["packet"]["test_marker"] = "content-drift"
    identity = {key: value for key, value in p0.items() if key != "content_sha256"}
    p0["content_sha256"] = content_sha256(identity)
    args.p0_inbox.write_text(json.dumps(p0, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        snapshot,
        "coordinate_registry_manifest",
        lambda: {"station_ids": [f"T{i:03d}" for i in range(expected_station_count())]},
    )
    with pytest.raises(snapshot.SnapshotError, match="snapshot_taf_p0_input_pointer_mismatch"):
        snapshot.build(args)


@pytest.mark.parametrize("source", (
    "p0_inbox",
    "taf_audit",
    "native_linkage",
    "native_repair",
))
def test_snapshot_rejects_duplicate_direct_evidence_station(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    args = _args(tmp_path)
    path = getattr(args, source)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if source == "p0_inbox":
        payload["packets"].append({"packet": {"station_id": STATION_IDS[0]}})
        expected = "snapshot_station_duplicate:p0_inbox"
    elif source == "taf_audit":
        payload["entries"].append({"station_id": STATION_IDS[0]})
        expected = "snapshot_station_duplicate:entries"
    elif source == "native_linkage":
        payload["active_exact_oos_consumable_activation_station_ids"].append(
            STATION_IDS[0]
        )
        expected = "snapshot_station_duplicate:active_exact_oos_consumable_activation_station_ids"
    else:
        payload["canonical_station_ids"].append(STATION_IDS[0])
        expected = "snapshot_station_duplicate:canonical_station_ids"
    identity = {key: value for key, value in payload.items() if key != "content_sha256"}
    payload["content_sha256"] = content_sha256(identity)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        snapshot,
        "coordinate_registry_manifest",
        lambda: {"station_ids": list(STATION_IDS)},
    )

    with pytest.raises(snapshot.SnapshotError, match=expected):
        snapshot.build(args)


@pytest.mark.parametrize("source", (
    "p0_inbox",
    "taf_audit",
    "native_linkage",
    "native_repair",
))
def test_snapshot_rejects_invalid_direct_input_content_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    args = _args(tmp_path)
    path = getattr(args, source)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["content_sha256"] = "f" * 64
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        snapshot,
        "coordinate_registry_manifest",
        lambda: {"station_ids": list(STATION_IDS)},
    )

    with pytest.raises(
        snapshot.SnapshotError,
        match=f"snapshot_{source}_content_address_invalid",
    ):
        snapshot.build(args)


@pytest.mark.parametrize("source", (
    "p0_inbox",
    "taf_audit",
    "native_linkage",
    "native_repair",
))
def test_snapshot_materializes_partial_direct_evidence_scope_as_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    args = _args(tmp_path)
    path = getattr(args, source)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if source == "p0_inbox":
        payload["packets"] = payload["packets"][:-1]
    elif source == "taf_audit":
        payload["entries"] = payload["entries"][:-1]
    elif source == "native_linkage":
        payload["active_exact_oos_consumable_activation_station_ids"] = (
            payload["active_exact_oos_consumable_activation_station_ids"][:-1]
        )
    else:
        payload["canonical_station_ids"] = payload["canonical_station_ids"][:-1]
    identity = {key: value for key, value in payload.items() if key != "content_sha256"}
    payload["content_sha256"] = content_sha256(identity)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    if source == "p0_inbox":
        taf = json.loads(args.taf_audit.read_text(encoding="utf-8"))
        taf["input_artifacts"]["p0_inbox"]["content_sha256"] = payload[
            "content_sha256"
        ]
        taf_identity = {
            key: value for key, value in taf.items() if key != "content_sha256"
        }
        taf["content_sha256"] = content_sha256(taf_identity)
        args.taf_audit.write_text(
            json.dumps(taf, sort_keys=True) + "\n", encoding="utf-8"
        )
    elif source == "native_linkage":
        repair = json.loads(args.native_repair.read_text(encoding="utf-8"))
        repair["candidate_plans"] = repair["candidate_plans"][:-1]
        repair["artifact_pointer_count"] = len(repair["candidate_plans"])
        repair_identity = {
            key: value for key, value in repair.items() if key != "content_sha256"
        }
        repair["content_sha256"] = content_sha256(repair_identity)
        args.native_repair.write_text(
            json.dumps(repair, sort_keys=True) + "\n", encoding="utf-8"
        )
    monkeypatch.setattr(
        snapshot,
        "coordinate_registry_manifest",
        lambda: {"station_ids": list(STATION_IDS)},
    )

    result = snapshot.build(args)

    assert result["state"] == "BLOCKED"
    assert result["blockers"] == [f"SNAPSHOT_SCOPE_MISMATCH:{source}"]
    assert result["production_no_live"] is True
    assert result["writes_target_database"] is False
    assert result["writes_production_database"] is False
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0


@pytest.mark.parametrize(
    ("as_of_utc", "match"),
    [
        ("", "native_linkage_as_of_utc_missing"),
        ("2026-08-02T00:00:00.001Z", "native_linkage_as_of_utc_after_snapshot_as_of"),
    ],
)
def test_snapshot_rejects_empty_or_future_native_frozen_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_of_utc: str,
    match: str,
) -> None:
    args = _args(tmp_path)
    for path in (args.native_linkage, args.native_repair):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["as_of_utc"] = as_of_utc
        identity = {key: value for key, value in payload.items() if key != "content_sha256"}
        payload["content_sha256"] = content_sha256(identity)
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        snapshot,
        "coordinate_registry_manifest",
        lambda: {"station_ids": [f"T{i:03d}" for i in range(expected_station_count())]},
    )
    with pytest.raises(snapshot.SnapshotError, match=match):
        snapshot.build(args)


def test_snapshot_rejects_native_pointer_selection_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path)
    repair = args.native_repair
    value = json.loads(repair.read_text(encoding="utf-8"))
    value["candidate_plans"][0]["artifact_pointer"][
        "selection_evidence_sha256"
    ] = "b" * 64
    pointer = value["candidate_plans"][0]["artifact_pointer"]
    pointer_identity = {
        key: item for key, item in pointer.items()
        if key != "pointer_sha256"
    }
    pointer["pointer_sha256"] = content_sha256(pointer_identity)
    identity = {key: item for key, item in value.items() if key != "content_sha256"}
    value["content_sha256"] = content_sha256(identity)
    repair.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        snapshot,
        "coordinate_registry_manifest",
        lambda: {"station_ids": [f"T{i:03d}" for i in range(expected_station_count())]},
    )
    with pytest.raises(snapshot.SnapshotError, match="native_p0_binding_identity_mismatch"):
        snapshot.build(args)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cross_artifact_content_sha256", "e" * 64),
        ("activation_identity_sha256", "f" * 64),
        ("generation", 2),
    ],
)
def test_snapshot_rejects_content_addressed_pointer_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    args = _args(tmp_path)
    repair = args.native_repair
    payload = json.loads(repair.read_text(encoding="utf-8"))
    pointer = payload["candidate_plans"][0]["artifact_pointer"]
    pointer[field] = value
    pointer_identity = {
        key: item for key, item in pointer.items()
        if key != "pointer_sha256"
    }
    pointer["pointer_sha256"] = content_sha256(pointer_identity)
    identity = {
        key: item for key, item in payload.items()
        if key != "content_sha256"
    }
    payload["content_sha256"] = content_sha256(identity)
    repair.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        snapshot,
        "coordinate_registry_manifest",
        lambda: {"station_ids": [f"T{i:03d}" for i in range(expected_station_count())]},
    )
    with pytest.raises(
        snapshot.SnapshotError,
        match="native_p0_binding_identity_mismatch",
    ):
        snapshot.build(args)


def test_snapshot_rejects_native_linkage_scope_outside_canonical_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path)
    linkage = args.native_linkage
    value = json.loads(linkage.read_text(encoding="utf-8"))
    value["active_exact_oos_consumable_activation_station_ids"] = [
        *STATION_IDS, "STALE",
    ]
    identity = {key: item for key, item in value.items() if key != "content_sha256"}
    value["content_sha256"] = content_sha256(identity)
    linkage.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        snapshot,
        "coordinate_registry_manifest",
        lambda: {"station_ids": [f"T{i:03d}" for i in range(expected_station_count())]},
    )
    with pytest.raises(
        snapshot.SnapshotError,
        match="snapshot_station_scope_outside_canonical:native_linkage",
    ):
        snapshot.build(args)
