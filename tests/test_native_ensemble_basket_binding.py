from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.native_ensemble_basket_binding import (
    build_native_ensemble_basket_binding,
    select_probability_stage,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/materialize_native_ensemble_basket_binding.py"
BUCKETS = {"25°C": 0, "26°C": 1, "27°C": 2, "28°C": 3}
INTERVALS = {
    "25°C": [24.5, 25.5],
    "26°C": [25.5, 26.5],
    "27°C": [26.5, 27.5],
    "28°C": [27.5, 28.5],
}


def _role(role: str, bucket: str, families: list[str]) -> dict[str, object]:
    return {
        "role": role,
        "bucket": bucket,
        "lineage": {"role": role, "evidence": "native"},
        "supporting_families": families,
        "supporting_products": [
            {
                "ensemble_system_id": f"{family}_PRODUCT",
                "independence_family_id": family,
                "lineage": {
                    "source_class": "native_official",
                    "aggregator_or_republisher": False,
                },
            }
            for family in families
        ],
        "score": {"independent_family_count": len(families)},
        "selection_reason": "test",
    }


def _scenario(*, source_kind: str = "official_native") -> dict[str, object]:
    candidate_identity: dict[str, object] = {
        "schema": "native_ensemble_scenario_roles_station_v1",
        "method_version": "test",
        "station_id": "TEST",
        "event_date": "2026-08-01",
        "settlement_timezone": "America/Los_Angeles",
        "source_kind": source_kind,
        "source_packet_content_sha256": "a" * 64,
        "market_registry": {
            "bucket_ordinals": BUCKETS,
            "bucket_intervals": INTERVALS,
        },
        "role_selection_lineage": {
            "schema": "native_ensemble_scenario_roles_v1",
            "method_version": "test",
            "content_sha256": "b" * 64,
            "bucket_contract_sha256": "c" * 64,
            "source_family_ids": ["ECMWF", "NOAA_GEFS"],
            "source_product_ids": ["ECMWF_IFS", "NOAA_GEFS"],
            "source_product_lineage": [
                {
                    "ensemble_system_id": "ECMWF_IFS",
                    "independence_family_id": "ECMWF",
                    "source_class": "native_official",
                    "aggregator_or_republisher": False,
                },
                {
                    "ensemble_system_id": "NOAA_GEFS",
                    "independence_family_id": "NOAA_GEFS",
                    "source_class": "native_official",
                    "aggregator_or_republisher": False,
                },
            ],
        },
        "buckets": {"M": "27°C", "L": "26°C", "H": "28°C"},
        "roles": {
            "M": _role("M", "27°C", ["ECMWF", "NOAA_GEFS"]),
            "L": _role("L", "26°C", ["NOAA_GEFS"]),
            "H": _role("H", "28°C", ["ECMWF"]),
        },
        "observation_candidates": [],
        "blockers": [],
        "raw_member_probability_is_not_p0_position": True,
        "position_authority": "P0/P0-M/P1_AND_EXECUTION_POLICY",
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    candidate = {
        **candidate_identity,
        "content_sha256": content_sha256(candidate_identity),
    }
    artifact_identity: dict[str, object] = {
        "schema": "native_ensemble_scenario_roles_v1",
        "method_version": "test",
        "event_date": "2026-08-01",
        "station_scope": ["TEST"],
        "station_count": 1,
        "source_kinds": [source_kind],
        "input_inbox_schema": "official_native_ensemble_forward_inbox_v1",
        "input_inbox_content_sha256": "d" * 64,
        "market_registry_snapshot_sha256": "e" * 64,
        "candidates": [candidate],
        "writes_p0": False,
        "writes_p0m": False,
        "writes_p1": False,
        "writes_shared_live_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {
        **artifact_identity,
        "content_sha256": content_sha256(artifact_identity),
    }


def _envelope(stage: str, checkpoint: str, *, decision: str = "2026-07-31T00:00:00Z") -> dict[str, object]:
    lineage: dict[str, object] = {"authority": "materialized_stage", "stage": stage}
    if stage == "P1":
        lineage["prediction_root_stage"] = "P0-M"
    return {
        "schema": "bayes_basket_probability_envelope_v1",
        "stage": stage,
        "checkpoint_id": checkpoint,
        "station_id": "TEST",
        "event_date": "2026-08-01",
        "settlement_timezone": "America/Los_Angeles",
        "decision_at_utc": decision,
        "fresh_until_utc": "2026-08-02T00:00:00Z",
        "bucket_ordinals": BUCKETS,
        "probabilities": {"25°C": 0.1, "26°C": 0.2, "27°C": 0.5, "28°C": 0.2},
        "posterior_version": f"{stage}-test",
        "posterior_sha256": "f" * 64,
        "bridge_sha256": "1" * 64,
        "lineage": lineage,
    }


def test_p1_precedence_binds_roles_without_rewriting_probability():
    result = build_native_ensemble_basket_binding(
        scenario_roles_artifact=_scenario(),
        probability_stage_snapshots=[
            _envelope("P0", "p0"),
            _envelope("P0-M", "p0m"),
            _envelope("P1", "p1"),
        ],
    )
    assert result["state"] == "BUILT_NO_LIVE"
    station = result["candidates"][0]
    assert station["selected_probability_stage"] == "P1"
    assert station["probability_stage"]["probabilities"] == _envelope("P1", "p1")["probabilities"]
    assert [row["bucket"] for row in station["basket_candidates"]] == ["27°C", "26°C", "28°C"]
    assert all(row["raw_member_probability_used"] is False for row in station["basket_candidates"])
    assert result["writes_p0"] is False
    assert result["calls_clob"] is False


def test_selector_inbox_schema_is_accepted_and_stage_fallback_is_explicit():
    scenario = _scenario()
    scenario["schema"] = "native_ensemble_scenario_roles_inbox_v1"
    identity = {key: value for key, value in scenario.items() if key != "content_sha256"}
    scenario["content_sha256"] = content_sha256(identity)
    result = build_native_ensemble_basket_binding(
        scenario_roles_artifact=scenario,
        probability_stage_snapshots=_envelope("P0-M", "p0m"),
    )
    station = result["candidates"][0]
    assert result["state"] == "BUILT_NO_LIVE"
    assert station["selected_probability_stage"] == "P0-M"
    assert station["probability_stage"]["content_sha256"] == station[
        "probability_stage"
    ]["computed_content_sha256"]


def test_stage_selector_rejects_ambiguous_same_stage_checkpoint():
    with pytest.raises(ContractError, match="ambiguous"):
        select_probability_stage(
            [_envelope("P1", "a"), _envelope("P1", "b")],
            station_id="TEST",
            event_date="2026-08-01",
        )


def test_p0_root_p1_is_accepted_when_no_p0m_exists():
    legacy_p1 = _envelope("P1", "legacy-p1")
    legacy_p1["lineage"] = {
        "authority": "historical_stage",
        "stage": "P1",
        "prediction_root_stage": "P0",
    }

    result = build_native_ensemble_basket_binding(
        scenario_roles_artifact=_scenario(),
        probability_stage_snapshots=[
            _envelope("P0", "p0"),
            legacy_p1,
        ],
    )

    station = result["candidates"][0]
    assert result["state"] == "BUILT_NO_LIVE"
    assert station["selected_probability_stage"] == "P1"
    assert station["probability_stage"]["lineage"]["prediction_root_stage"] == "P0"


def test_unknown_p1_root_stage_remains_fail_closed_without_lower_stage_fallback():
    invalid_p1 = _envelope("P1", "invalid-p1")
    invalid_p1["lineage"] = {
        "authority": "historical_stage",
        "stage": "P1",
        "prediction_root_stage": "P2",
    }

    result = build_native_ensemble_basket_binding(
        scenario_roles_artifact=_scenario(),
        probability_stage_snapshots=[_envelope("P0", "p0"), invalid_p1],
    )

    station = result["candidates"][0]
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert station["selected_probability_stage"] is None
    assert station["blockers"] == ["probability_p1_root_stage_mismatch"]
    with pytest.raises(ContractError, match="probability_p1_root_stage_mismatch"):
        select_probability_stage(
            [_envelope("P0", "p0"), invalid_p1],
            station_id="TEST",
            event_date="2026-08-01",
        )


def test_malformed_stage_collection_is_fail_closed():
    with pytest.raises(ContractError, match="snapshot_invalid"):
        build_native_ensemble_basket_binding(
            scenario_roles_artifact=_scenario(),
            probability_stage_snapshots=[_envelope("P0", "p0"), "not-a-snapshot"],
        )


def test_open_meteo_roles_are_fail_closed_before_candidate_binding():
    result = build_native_ensemble_basket_binding(
        scenario_roles_artifact=_scenario(source_kind="open_meteo_aggregator"),
        probability_stage_snapshots=_envelope("P0", "p0"),
    )
    # The formal input contract is rejected before the stage can become a
    # basket authority; callers receive an explicit blocked reason.
    assert result["state"] == "BLOCKED_FAIL_CLOSED"


def test_proved_open_meteo_roles_bind_as_no_live_fallback():
    scenario = _scenario(source_kind="open_meteo_aggregator")
    candidate = scenario["candidates"][0]
    candidate["fallback_authority"] = {
        "authority_mode": "PRODUCTION_NO_LIVE_REPUBLISHER_FALLBACK",
        "source_role": "aggregator_republisher",
        "native_source_unavailable": True,
        "production_no_live_fallback_eligible": True,
        "historical_oos_activation_required": True,
        "native_arrival_index_eligible": False,
        "native_formal_p0_eligible": False,
        "formal_p0_eligible": False,
        "oos_event_temporal_scope": "observational_candidate_not_oos",
        "future_event_count": 0,
    }
    for product in candidate["role_selection_lineage"]["source_product_lineage"]:
        product["source_class"] = "aggregator_republisher"
        product["aggregator_or_republisher"] = True
    candidate_identity = {
        key: value for key, value in candidate.items() if key != "content_sha256"
    }
    candidate["content_sha256"] = content_sha256(candidate_identity)
    scenario_identity = {
        key: value for key, value in scenario.items() if key != "content_sha256"
    }
    scenario["content_sha256"] = content_sha256(scenario_identity)

    result = build_native_ensemble_basket_binding(
        scenario_roles_artifact=scenario,
        probability_stage_snapshots=_envelope("P0", "p0"),
    )

    assert result["state"] == "BUILT_NO_LIVE"
    station = result["candidates"][0]
    assert station["source_kind"] == "open_meteo_aggregator"
    assert station["fallback_authority"]["native_arrival_index_eligible"] is False
    assert station["selected_probability_stage"] == "P0"


def test_duplicate_family_support_is_rejected():
    scenario = _scenario()
    broken = copy.deepcopy(scenario)
    broken["candidates"][0]["roles"]["M"]["supporting_families"] = ["ECMWF", "NOAA_GEFS", "ECMWF"]
    broken_candidate = broken["candidates"][0]
    identity = {key: value for key, value in broken_candidate.items() if key != "content_sha256"}
    broken_candidate["content_sha256"] = content_sha256(identity)
    artifact_identity = {key: value for key, value in broken.items() if key != "content_sha256"}
    broken["content_sha256"] = content_sha256(artifact_identity)
    with pytest.raises(ContractError, match="family_vote_duplicate"):
        build_native_ensemble_basket_binding(
            scenario_roles_artifact=broken,
            probability_stage_snapshots=_envelope("P0", "p0"),
        )


def test_missing_station_stage_is_explicitly_blocked():
    result = build_native_ensemble_basket_binding(
        scenario_roles_artifact=_scenario(),
        probability_stage_snapshots=_envelope("P0", "p0"),
    )
    station = result["candidates"][0]
    assert result["state"] == "BUILT_NO_LIVE"
    assert station["selected_probability_stage"] == "P0"


def test_cli_writes_content_addressed_no_live_artifact(tmp_path):
    scenario_path = tmp_path / "scenario.json"
    stage_path = tmp_path / "p0.json"
    output_path = tmp_path / "binding.json"
    scenario_path.write_text(json.dumps(_scenario()), encoding="utf-8")
    stage_path.write_text(json.dumps(_envelope("P0", "p0")), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--scenario-roles",
            str(scenario_path),
            "--probability-stage",
            str(stage_path),
            "--output",
            str(output_path),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    assert artifact["content_sha256"] == content_sha256(
        {key: value for key, value in artifact.items() if key != "content_sha256"}
    )
    assert artifact["calls_order_gateway"] is False
    assert artifact["orders_submitted"] == 0
