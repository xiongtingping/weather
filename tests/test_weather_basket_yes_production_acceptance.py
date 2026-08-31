from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reported_trajectory_country_sources import load_official_source_config
from scripts.weather_basket_yes_production_acceptance import (
    CORE,
    LEGACY,
    canonical_sha,
    awc_global_station_contract,
    forward_awc_taf_capture_isolated,
    held_yes_quote_evidence_records,
    held_yes_inventory_fresh,
    held_yes_reconcile_receipt_valid,
    primary_ws_pure_yes_token_snapshots,
)
from scripts import reconcile_reported_trajectory_held_yes_clob as held_yes_reconcile
from bayes_basket_target.fleet import expected_station_count


ROOT = Path(__file__).parents[1]


def test_pure_yes_service_identity_replaces_retired_no_owner() -> None:
    """The acceptance contract must reject the old launchd service label."""

    assert "com.weather.weather_basket_yes" in CORE
    assert "com.weather.reported_trajectory_no" in LEGACY
    assert set(CORE).isdisjoint(LEGACY)


def _canonical_registry_lanes() -> dict[str, set[str]]:
    lane_map = json.loads(
        (
            ROOT
            / "config/trading/bayes_basket_target_station_lane_map_v1.json"
        ).read_text(encoding="utf-8")
    )["stations"]
    return {
        str(station).upper(): {str(lane).lower()}
        for station, lane in lane_map.items()
    }


def test_global_awc_station_contract_covers_the_whole_fleet() -> None:
    official = load_official_source_config()
    lanes = _canonical_registry_lanes()

    failures = awc_global_station_contract(
        eligible_stations=set(lanes),
        registry_lanes_by_station=lanes,
        source_routes=official["station_routes"],
        source_providers=official["providers"],
    )

    assert failures == []
    assert len(lanes) == expected_station_count()
    assert all(lane == {"awc"} for lane in lanes.values())


def test_global_awc_station_contract_rejects_amsc_reintroduction() -> None:
    official = load_official_source_config()
    lanes = _canonical_registry_lanes()
    lanes["ZBAA"] = {"amsc"}

    failures = awc_global_station_contract(
        eligible_stations=set(lanes),
        registry_lanes_by_station=lanes,
        source_routes=official["station_routes"],
        source_providers=official["providers"],
    )

    assert failures == ["ZBAA:expected=awc:actual=amsc"]


def test_forward_awc_taf_capture_is_separate_from_temperature_only_truth() -> None:
    status = {
        "taf_requested": True,
        "weak_evidence_taf_capture_isolated": True,
        "forecast_prefetch": {
            "forward_taf_capture": {
                "state": "not_due",
                "calls_order_gateway": False,
                "places_orders": False,
                "last_attempt": {
                    "state": "captured",
                    "capture_count": 4,
                    "calls_order_gateway": False,
                    "places_orders": False,
                },
            },
        },
    }

    assert forward_awc_taf_capture_isolated(status) is True

    status["forecast_prefetch"]["forward_taf_capture"]["state"] = (
        "captured_with_scope_failures"
    )
    assert forward_awc_taf_capture_isolated(status) is True
    status["forecast_prefetch"]["forward_taf_capture"]["last_attempt"][
        "calls_order_gateway"
    ] = True
    assert forward_awc_taf_capture_isolated(status) is False


def test_forward_awc_taf_capture_accepts_inflight_bounded_worker_with_valid_previous_attempt() -> None:
    """An isolated TAF retry must not fail readiness during its short poll window."""

    status = {
        "taf_requested": True,
        "weak_evidence_taf_capture_isolated": True,
        "forecast_prefetch": {
            "forward_taf_capture": {
                "state": "pending_same_bounded_worker",
                "calls_order_gateway": False,
                "places_orders": False,
                "last_attempt": {
                    "state": "captured_with_scope_failures",
                    "capture_count": 3,
                    "failure_count": 1,
                    "calls_order_gateway": False,
                    "places_orders": False,
                },
            },
        },
    }

    assert forward_awc_taf_capture_isolated(status) is True


def _primary_yes_snapshot(*, token_id: str = "yes-1", side: str = "YES") -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "clob_ws_token_quote_snapshot_v1",
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
        "shard_active": True,
        "shard_index": 0,
        "shard_generation": 7,
        "token_id": token_id,
        "quote": {"token_id": token_id, "side": side},
    }
    payload["snapshot_sha256"] = canonical_sha(payload)
    return payload


def test_primary_ws_requires_current_bounded_yes_only_token_shards(tmp_path: Path) -> None:
    snapshot = _primary_yes_snapshot()
    (tmp_path / "yes-1.json").write_text(json.dumps(snapshot), encoding="utf-8")
    manifest = {
        "materialized_token_count": 1,
        "subscribed_token_count": 1,
        "active_shards": [0],
        "active_shard_generations": {"0": 7},
    }

    assert primary_ws_pure_yes_token_snapshots(
        manifest=manifest, shard_dir=tmp_path
    ) is True

    snapshot = _primary_yes_snapshot(side="NO")
    (tmp_path / "yes-1.json").write_text(json.dumps(snapshot), encoding="utf-8")
    assert primary_ws_pure_yes_token_snapshots(
        manifest=manifest, shard_dir=tmp_path
    ) is False


def _clob_bound_held_yes_evidence() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    active_token_ids = ["token-a", "token-b"]
    observed_at = "2026-07-31T17:37:49.356Z"
    receipt: dict[str, object] = {
        "schema": "reported_trajectory_held_yes_clob_reconcile_v1",
        "source": "official_local_clob_execution_guard_snapshot",
        "complete": True,
        "evidence_only": True,
        "authorizes_order": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "observed_at": observed_at,
        "nonzero_token_ids": active_token_ids,
        "zero_token_ids": ["token-zero"],
        "evidence_only_token_ids": [],
        "unverified_balance_token_ids": [],
        "unavailable_token_ids": [],
        "token_count": 3,
        "scope_token_ids": ["token-a", "token-b", "token-zero"],
    }
    receipt["scope_sha256"] = hashlib.sha256(
        "\x00".join(receipt["scope_token_ids"]).encode("utf-8")
    ).hexdigest()
    receipt["receipt_sha256"] = canonical_sha(receipt)
    inventory: dict[str, object] = {
        "complete": True,
        "token_ids": ["token-zero", *active_token_ids],
        "active_token_ids": active_token_ids,
        "observed_at": observed_at,
        "active_token_ids_source": "official_local_clob_execution_guard_snapshot",
        "active_token_ids_observed_at": observed_at,
        "active_token_ids_receipt_sha256": receipt["receipt_sha256"],
    }
    health: dict[str, object] = {
        "active_held_yes_balance_ready": True,
        "conditional_balance_ready_token_ids": active_token_ids,
    }
    return inventory, receipt, health


def test_clob_bound_receipt_keeps_held_yes_inventory_fresh_with_continuous_prefetch() -> None:
    inventory, receipt, health = _clob_bound_held_yes_evidence()

    assert held_yes_reconcile_receipt_valid(inventory=inventory, receipt=receipt) is True
    # The reconcile observation may be older than 15 seconds: freshness is
    # carried by the live local read-only balance prefetch for the same tokens.
    assert held_yes_inventory_fresh(
        inventory=inventory, receipt=receipt, health=health
    ) is True


@pytest.mark.parametrize("mutation", ("schema", "source", "hash", "nonzero_token_ids"))
def test_clob_bound_receipt_rejects_identity_or_projection_mismatch(mutation: str) -> None:
    inventory, receipt, _health = _clob_bound_held_yes_evidence()
    if mutation == "schema":
        receipt["schema"] = "wrong"
        receipt["receipt_sha256"] = canonical_sha(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    elif mutation == "source":
        receipt["source"] = "untrusted"
        receipt["receipt_sha256"] = canonical_sha(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    elif mutation == "hash":
        receipt["receipt_sha256"] = "0" * 64
    else:
        receipt["nonzero_token_ids"] = ["token-a"]
        receipt["receipt_sha256"] = canonical_sha(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )

    assert held_yes_reconcile_receipt_valid(inventory=inventory, receipt=receipt) is False


def test_clob_bound_inventory_requires_live_prefetch_for_each_active_token() -> None:
    inventory, receipt, health = _clob_bound_held_yes_evidence()
    health["conditional_balance_ready_token_ids"] = ["token-a"]

    assert held_yes_inventory_fresh(
        inventory=inventory, receipt=receipt, health=health
    ) is False


@pytest.mark.parametrize("field, value", (
    ("scope_token_ids", ["token-a", "token-b"]),
    ("evidence_only_token_ids", ["token-zero"]),
    ("unavailable_token_ids", ["token-zero"]),
))
def test_clob_bound_receipt_rejects_partial_or_unreadable_inventory_scope(
    field: str, value: object,
) -> None:
    inventory, receipt, _health = _clob_bound_held_yes_evidence()
    receipt[field] = value
    receipt["receipt_sha256"] = canonical_sha(
        {key: item for key, item in receipt.items() if key != "receipt_sha256"}
    )

    assert held_yes_reconcile_receipt_valid(inventory=inventory, receipt=receipt) is False


def test_full_known_scope_is_required_before_held_yes_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "held_yes.json"
    output_path = tmp_path / "receipt.json"
    inventory_path.write_text(json.dumps({
        "schema": "reported_trajectory_held_yes_inventory_v1",
        "complete": True,
        "token_ids": ["token-a", "token-b"],
        "active_token_ids": ["token-a"],
    }), encoding="utf-8")

    def _snapshot(_url: str, token_id: str, _timeout: float) -> dict[str, object]:
        return {
            "ok": True,
            "observed_at": "2026-08-01T12:00:00.000Z",
            "conditional_balance_allowance": {
                "balance": "1" if token_id == "token-a" else "0",
            },
        }

    monkeypatch.setattr(held_yes_reconcile, "_post_snapshot", _snapshot)
    receipt = held_yes_reconcile.run(
        inventory_path=inventory_path,
        output_path=output_path,
        gateway_url="http://127.0.0.1:8765/execution-guard-snapshot",
        timeout=1.0,
        retries=1,
    )

    assert receipt["complete"] is True
    assert receipt["scope_token_ids"] == ["token-a", "token-b"]
    assert receipt["nonzero_token_ids"] == ["token-a"]
    held_yes_reconcile.update_active_projection(inventory_path, receipt)
    assert json.loads(inventory_path.read_text(encoding="utf-8"))["active_token_ids"] == ["token-a"]


def test_verified_balance_can_reconcile_when_sell_book_is_separately_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory_path = tmp_path / "held_yes.json"
    output_path = tmp_path / "receipt.json"
    inventory_path.write_text(json.dumps({
        "schema": "reported_trajectory_held_yes_inventory_v1",
        "complete": True,
        "token_ids": ["token-a"],
        "active_token_ids": ["token-a"],
    }), encoding="utf-8")
    monkeypatch.setattr(held_yes_reconcile, "_post_snapshot", lambda *_args: {
        "ok": False,
        "observed_at": "2026-08-01T12:00:00.000Z",
        "conditional_balance_allowance": {"balance": "2"},
        "evidence_errors": ["clob_ws_order_book_depth_missing"],
    })

    receipt = held_yes_reconcile.run(
        inventory_path=inventory_path,
        output_path=output_path,
        gateway_url="http://127.0.0.1:8765/execution-guard-snapshot",
        timeout=1.0,
        retries=1,
    )

    assert receipt["complete"] is True
    assert receipt["rows"][0]["execution_guard_ok"] is False
    assert receipt["nonzero_token_ids"] == ["token-a"]


@pytest.mark.parametrize("raw_balance", (None, "NaN", "-1"))
def test_unverified_balance_cannot_shrink_held_yes_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_balance: object,
) -> None:
    inventory_path = tmp_path / "held_yes.json"
    output_path = tmp_path / "receipt.json"
    inventory_path.write_text(json.dumps({
        "schema": "reported_trajectory_held_yes_inventory_v1",
        "complete": True,
        "token_ids": ["token-a", "token-b"],
        "active_token_ids": ["token-a", "token-b"],
    }), encoding="utf-8")

    def _snapshot(_url: str, token_id: str, _timeout: float) -> dict[str, object]:
        balance = "0" if token_id == "token-a" else raw_balance
        return {
            "ok": True,
            "observed_at": "2026-08-01T12:00:00.000Z",
            "conditional_balance_allowance": {"balance": balance},
        }

    monkeypatch.setattr(held_yes_reconcile, "_post_snapshot", _snapshot)
    receipt = held_yes_reconcile.run(
        inventory_path=inventory_path,
        output_path=output_path,
        gateway_url="http://127.0.0.1:8765/execution-guard-snapshot",
        timeout=1.0,
        retries=1,
    )

    assert receipt["complete"] is False
    assert receipt["unverified_balance_token_ids"] == ["token-b"]
    with pytest.raises(RuntimeError, match="held_yes_clob_reconcile_incomplete"):
        held_yes_reconcile.update_active_projection(inventory_path, receipt)


def test_held_yes_auxiliary_quote_requires_scope_hash_and_fresh_complete_book(
    tmp_path: Path,
) -> None:
    token = "123456"
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    observed_at = "2026-08-01T12:00:00.000Z"
    scope = [token]
    scope_sha = hashlib.sha256("\x00".join(scope).encode("utf-8")).hexdigest()
    auxiliary = {
        "schema": "clob_ws_held_yes_quote_manifest_v1",
        "state": "ready",
        "connection_observed_at": observed_at,
        "scope_token_ids": scope,
        "scope_sha256": scope_sha,
        "complete_book_token_count": 1,
        "complete_book_token_ids": scope,
        "missing_complete_book_token_ids": [],
        "quote_storage": "sha256_token_shards_v1",
        "quote_shard_dir": str(tmp_path / "auxiliary"),
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
        "last_error": "",
    }
    auxiliary["snapshot_sha256"] = canonical_sha(auxiliary)
    quote = {
        "token_id": token,
        "source": "clob_ws",
        "source_detail": "book",
        "bids": [],
        "asks": [],
        "book_observed_at": observed_at,
    }
    shard = {
        "schema": "clob_ws_held_yes_token_quote_snapshot_v1",
        "token_id": token,
        "scope_sha256": scope_sha,
        "scope_token_ids": scope,
        "quote": quote,
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True,
        "authorizes_order": False,
    }
    shard["snapshot_sha256"] = canonical_sha(shard)
    auxiliary_dir = tmp_path / "auxiliary"
    auxiliary_dir.mkdir()
    (auxiliary_dir / f"{hashlib.sha256(token.encode()).hexdigest()}.json").write_text(
        json.dumps(shard), encoding="utf-8"
    )

    records = held_yes_quote_evidence_records(
        active_token_ids={token},
        primary_manifest={},
        auxiliary_manifest=auxiliary,
        primary_shard_dir=tmp_path / "primary",
        auxiliary_shard_dir=auxiliary_dir,
        now=now,
    )
    assert records[0]["valid"] is True
    assert records[0]["accepted_source"] == "auxiliary"
    assert records[0]["blocking_reasons"] == []

    shard["scope_sha256"] = "0" * 64
    shard["snapshot_sha256"] = canonical_sha(
        {key: value for key, value in shard.items() if key != "snapshot_sha256"}
    )
    (auxiliary_dir / f"{hashlib.sha256(token.encode()).hexdigest()}.json").write_text(
        json.dumps(shard), encoding="utf-8"
    )
    rejected = held_yes_quote_evidence_records(
        active_token_ids={token},
        primary_manifest={},
        auxiliary_manifest=auxiliary,
        primary_shard_dir=tmp_path / "primary",
        auxiliary_shard_dir=auxiliary_dir,
        now=now,
    )
    assert rejected[0]["valid"] is False
    assert "primary_token_shard_missing" in rejected[0]["blocking_reasons"]
    assert "auxiliary_token_scope_invalid" in rejected[0]["blocking_reasons"]
