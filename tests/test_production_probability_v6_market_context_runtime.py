import json

from production_probability_v6_market_context_runtime import (
    activation_sha256,
    load_active_v6_market_context,
    market_context_sha256,
    resolve_active_v6_market_context,
    validate_v6_market_context,
)
from tests.test_resolution_target_contract_runtime import _proposal


def test_disabled_market_context_is_explicit_and_never_blocks_physical_x(tmp_path):
    path = tmp_path / "activation.json"
    path.write_text(
        json.dumps(
            {
                "schema": "production_probability_v6_market_context_activation_v1",
                "state": "disabled",
                "context_path": "",
                "market_context_sha256": "",
                "bridge_artifact_sha256": "",
                "places_orders": False,
                "authorizes_clob": False,
                "blocks_physical_x": False,
            }
        )
    )

    result = load_active_v6_market_context(
        activation_path=path,
        root=tmp_path,
        event_date="2025-04-30",
    )

    assert result["state"] == "disabled"
    assert result["market_projection_available"] is False
    assert result["blocks_physical_x"] is False


def test_enabled_context_binds_exact_day_target_rule_and_bridge(tmp_path):
    target = _proposal()
    core = {
        "schema": "production_probability_v6_market_context_v1",
        "state": "production_approved",
        "event_date": "2026-07-03",
        "target_identity": "physical_weather_day_v1:ZUUU:2026-07-03",
        "slug": target["public_slug"],
        "market_target_contract": target,
        "target_contract_sha256": target["target_contract_sha256"],
        "bridge_artifact_sha256": "a" * 64,
        "places_orders": False,
        "authorizes_clob": False,
        "blocks_physical_x": False,
    }
    context = {**core, "market_context_sha256": market_context_sha256(core)}
    validate_v6_market_context(context)
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context))
    activation_core = {
        "schema": "production_probability_v6_market_context_activation_v1",
        "state": "production_enabled",
        "context_path": "context.json",
        "market_context_sha256": context["market_context_sha256"],
        "bridge_artifact_sha256": context["bridge_artifact_sha256"],
        "places_orders": False,
        "authorizes_clob": False,
        "blocks_physical_x": False,
    }
    activation = {
        **activation_core,
        "activation_sha256": activation_sha256(activation_core),
    }
    activation_path = tmp_path / "activation.json"
    activation_path.write_text(json.dumps(activation))

    loaded = load_active_v6_market_context(
        activation_path=activation_path,
        root=tmp_path,
        event_date="2026-07-03",
    )

    assert loaded["state"] == "available"
    assert loaded["market_context"]["slug"] == target["public_slug"]
    assert loaded["places_orders"] is False


def test_invalid_enabled_context_is_market_na_and_never_blocks_physical_x(tmp_path):
    path = tmp_path / "activation.json"
    path.write_text("{not-json")

    result = resolve_active_v6_market_context(
        activation_path=path,
        root=tmp_path,
        event_date="2026-07-03",
    )

    assert result["ok"] is False
    assert result["state"] == "unavailable"
    assert result["market_projection_available"] is False
    assert result["blocks_physical_x"] is False


def _write_enabled_pointer(path, pointer):
    core = {
        "schema": "production_probability_v6_market_context_activation_v1",
        "state": "production_enabled",
        "context_path": pointer,
        "market_context_sha256": "a" * 64,
        "bridge_artifact_sha256": "b" * 64,
        "places_orders": False,
        "authorizes_clob": False,
        "blocks_physical_x": False,
    }
    path.write_text(json.dumps({**core, "activation_sha256": activation_sha256(core)}))


def test_external_or_parent_market_pointer_is_na_and_never_blocks_physical_x(tmp_path):
    for pointer in ("/Volumes/archive/context.json", "../context.json"):
        path = tmp_path / f"activation-{len(pointer)}.json"
        _write_enabled_pointer(path, pointer)
        result = resolve_active_v6_market_context(
            activation_path=path, root=tmp_path, event_date="2026-07-03"
        )
        assert result["market_projection_available"] is False
        assert result["blocks_physical_x"] is False
        assert "runtime_pointer_must_be_repo_relative" in result["block_reasons"]


def test_symlink_market_pointer_is_na_without_following_external_target(tmp_path):
    outside = tmp_path.parent / "external-context-do-not-read.json"
    outside.write_text("not-a-context")
    (tmp_path / "context.json").symlink_to(outside)
    path = tmp_path / "activation.json"
    _write_enabled_pointer(path, "context.json")
    result = resolve_active_v6_market_context(
        activation_path=path, root=tmp_path, event_date="2026-07-03"
    )
    assert result["market_projection_available"] is False
    assert result["blocks_physical_x"] is False
    assert "runtime_pointer_symlink_forbidden" in result["block_reasons"]
