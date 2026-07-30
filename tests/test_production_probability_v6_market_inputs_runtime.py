import json
from datetime import date, timedelta
from pathlib import Path

from production_probability_v6_market_context_runtime import (
    activation_sha256 as context_activation_sha256,
    market_context_sha256,
)
from production_probability_v6_market_inputs_runtime import prewarm_v6_market_inputs
from resolution_bridge_activation_runtime import activation_sha256 as bridge_activation_sha256
from resolution_bridge_artifact_runtime import artifact_sha256
from resolution_bridge_calibration_runtime import fit_resolution_bridge_candidate
from tests.test_resolution_target_contract_runtime import _proposal


ROOT = Path(__file__).resolve().parents[1]


def _bridge(authority_sha: str) -> dict:
    start = date(2025, 1, 1)
    pairs = []
    for index in range(120):
        x = 20 + index % 15
        pairs.append(
            {
                "state": "available_for_bridge_calibration_only",
                "event_date": (start + timedelta(days=index)).isoformat(),
                "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
                "final_physical_high_c": x,
                "settlement_scalar_c": x + (-1, 0, 0, 0, 1)[index % 5],
                "observed_x_s_pair_sha256": f"{index:064x}",
                "physical_source": {
                    "source_id": "amsc_zuuu_scheduled_metar_daily_max_v2"
                },
                "settlement_source": {
                    "source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3"
                },
            }
        )
    candidate = fit_resolution_bridge_candidate(
        pairs, authority_contract_sha256=authority_sha
    )["candidate_artifact"]
    core = {
        **candidate,
        "state": "production_approved",
        "bridge_closed": True,
        "validated": True,
        "production_runtime_eligible": True,
        "shadow_only": False,
        "approval_id": "ADR-bridge-test",
        "approval_receipt_sha256": "c" * 64,
    }
    core.pop("artifact_sha256")
    return {**core, "artifact_sha256": artifact_sha256(core)}


def test_disabled_market_inputs_are_physical_only_and_nonblocking():
    result = prewarm_v6_market_inputs(event_date="2026-07-03")

    assert result["state"] == "physical_only"
    assert result["market_projection_available"] is False
    assert result["blocks_physical_x"] is False


def test_enabled_market_inputs_require_exact_context_bridge_and_policy_binding(tmp_path):
    target = _proposal()
    bridge = _bridge(target["authority_contract_sha256"])
    bridge_path = tmp_path / "bridge.json"
    bridge_path.write_text(json.dumps(bridge))
    bridge_activation_core = {
        "schema": "resolution_bridge_production_activation_v1",
        "state": "production_enabled",
        "artifact_path": "bridge.json",
        "artifact_sha256": bridge["artifact_sha256"],
        "approval_id": bridge["approval_id"],
        "approval_receipt_sha256": bridge["approval_receipt_sha256"],
        "places_orders": False,
        "authorizes_clob": False,
    }
    bridge_activation = {
        **bridge_activation_core,
        "activation_sha256": bridge_activation_sha256(bridge_activation_core),
    }
    bridge_activation_path = tmp_path / "bridge-activation.json"
    bridge_activation_path.write_text(json.dumps(bridge_activation))
    context_core = {
        "schema": "production_probability_v6_market_context_v1",
        "state": "production_approved",
        "event_date": "2026-07-03",
        "target_identity": "physical_weather_day_v1:ZUUU:2026-07-03",
        "slug": target["public_slug"],
        "market_target_contract": target,
        "target_contract_sha256": target["target_contract_sha256"],
        "bridge_artifact_sha256": bridge["artifact_sha256"],
        "places_orders": False,
        "authorizes_clob": False,
        "blocks_physical_x": False,
    }
    context = {
        **context_core,
        "market_context_sha256": market_context_sha256(context_core),
    }
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context))
    context_activation_core = {
        "schema": "production_probability_v6_market_context_activation_v1",
        "state": "production_enabled",
        "context_path": "context.json",
        "market_context_sha256": context["market_context_sha256"],
        "bridge_artifact_sha256": bridge["artifact_sha256"],
        "places_orders": False,
        "authorizes_clob": False,
        "blocks_physical_x": False,
    }
    context_activation = {
        **context_activation_core,
        "activation_sha256": context_activation_sha256(context_activation_core),
    }
    context_activation_path = tmp_path / "context-activation.json"
    context_activation_path.write_text(json.dumps(context_activation))

    result = prewarm_v6_market_inputs(
        event_date="2026-07-03",
        market_context_activation_path=context_activation_path,
        bridge_activation_path=bridge_activation_path,
        realization_policy_path=(
            ROOT / "config/trading/production_probability_v6_realization_policy.json"
        ),
        resolution_authority_path=(
            ROOT
            / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json"
        ),
        root=tmp_path,
    )

    assert result["state"] == "market_inputs_ready"
    assert result["market_projection_available"] is True
    assert result["bridge_artifact"]["artifact_sha256"] == bridge["artifact_sha256"]
    assert result["physical_resolution_contract"]["resolution_local_date"] == "2026-07-03"
    assert result["blocks_physical_x"] is False


def test_basket_probability_inputs_do_not_depend_on_retired_realization_policy(tmp_path):
    target = _proposal()
    bridge = _bridge(target["authority_contract_sha256"])
    bridge_path = tmp_path / "bridge.json"
    bridge_path.write_text(json.dumps(bridge))
    bridge_activation_core = {
        "schema": "resolution_bridge_production_activation_v1",
        "state": "production_enabled",
        "artifact_path": "bridge.json",
        "artifact_sha256": bridge["artifact_sha256"],
        "approval_id": bridge["approval_id"],
        "approval_receipt_sha256": bridge["approval_receipt_sha256"],
        "places_orders": False,
        "authorizes_clob": False,
    }
    bridge_activation = {
        **bridge_activation_core,
        "activation_sha256": bridge_activation_sha256(bridge_activation_core),
    }
    bridge_activation_path = tmp_path / "bridge-activation.json"
    bridge_activation_path.write_text(json.dumps(bridge_activation))
    context_core = {
        "schema": "production_probability_v6_market_context_v1",
        "state": "production_approved",
        "event_date": "2026-07-03",
        "target_identity": "physical_weather_day_v1:ZUUU:2026-07-03",
        "slug": target["public_slug"],
        "market_target_contract": target,
        "target_contract_sha256": target["target_contract_sha256"],
        "bridge_artifact_sha256": bridge["artifact_sha256"],
        "places_orders": False,
        "authorizes_clob": False,
        "blocks_physical_x": False,
    }
    context = {
        **context_core,
        "market_context_sha256": market_context_sha256(context_core),
    }
    (tmp_path / "context.json").write_text(json.dumps(context))
    context_activation_core = {
        "schema": "production_probability_v6_market_context_activation_v1",
        "state": "production_enabled",
        "context_path": "context.json",
        "market_context_sha256": context["market_context_sha256"],
        "bridge_artifact_sha256": bridge["artifact_sha256"],
        "places_orders": False,
        "authorizes_clob": False,
        "blocks_physical_x": False,
    }
    context_activation = {
        **context_activation_core,
        "activation_sha256": context_activation_sha256(context_activation_core),
    }
    context_activation_path = tmp_path / "context-activation.json"
    context_activation_path.write_text(json.dumps(context_activation))
    result = prewarm_v6_market_inputs(
        event_date="2026-07-03",
        market_context_activation_path=context_activation_path,
        bridge_activation_path=bridge_activation_path,
        realization_policy_path=tmp_path / "retired-policy-does-not-exist.json",
        resolution_authority_path=(
            ROOT
            / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json"
        ),
        root=tmp_path,
        strategy_projection_role="basket_probability_only",
    )
    assert result["state"] == "market_inputs_ready"
    assert result["strategy_projection_role"] == "basket_probability_only"
    assert result["realization_policy"] is None
