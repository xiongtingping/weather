from pathlib import Path
import sqlite3
import hashlib
import json

from scripts import production_reported_x_market_readiness as readiness


def _db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        for table in readiness.REQUIRED_TABLES:
            conn.execute(f"CREATE TABLE {table}(id INTEGER)")


def _coverage(ok: bool = False):
    return {
        "ok": ok,
        "state": "ready_for_calibration_review" if ok else "accumulating",
        "block_reasons": [] if ok else ["bridge_pair_event_day_coverage_below_minimum"],
    }


def _receipt(value: dict) -> str:
    payload = {
        **value,
        "runtime_sha256": {
            name: readiness._sha(path)
            for name, path in readiness.V6_RUNTIME_FILES.items()
        },
        "generator_sha256": readiness._sha(
            readiness.ROOT / "scripts/production_reported_x_market_no_order_drill.py"
        ),
    }
    payload["receipt_sha256"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return json.dumps(payload)


def test_bridge_accumulation_is_safe_and_does_not_block_physical_x(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _db(db)
    monkeypatch.setattr(
        readiness,
        "load_active_resolution_bridge_artifact",
        lambda **_kwargs: ({"state": "disabled"}, None),
    )
    monkeypatch.setattr(
        readiness,
        "build_resolution_bridge_pair_coverage_audit",
        lambda **_kwargs: _coverage(False),
    )
    monkeypatch.setattr(
        readiness,
        "audit_physical_x",
        lambda *_args, **_kwargs: {"ok": True, "state": "ready"},
    )
    monkeypatch.setattr(
        readiness,
        "resolve_active_v6_market_context",
        lambda **_kwargs: {
            "state": "disabled", "market_projection_available": False,
            "na_reason": "market_projection_context_disabled",
        },
    )
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        _receipt({
            "schema": "production_probability_v6_market_no_order_drill_v2",
            "ok": True,
            "state": "accepted",
            "archive_unchanged": True,
            "writes_archive_db": False,
            "external_disk_required": False,
            "forbidden_execution_tokens": [],
            "activation_bound": False,
        })
    )
    result = readiness.audit(
        db=db,
        probability_activation=tmp_path / "probability.json",
        bridge_activation=tmp_path / "bridge.json",
        market_context_activation=tmp_path / "context.json",
        pair_registry=tmp_path / "pairs.db",
        no_order_receipt=receipt,
        event_date="2026-07-15",
    )
    assert result["ok"] is True
    assert result["state"] == "safe_fail_closed_accumulating"
    assert result["production_market_projection_ready"] is False
    assert result["boundary"]["bridge_sample_or_oos_shortage_blocks_physical_x_live"] is False


def test_approved_operational_baseline_is_ready_even_when_calibration_coverage_accumulates(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _db(db)
    monkeypatch.setattr(
        readiness,
        "load_active_resolution_bridge_artifact",
        lambda **_kwargs: (
            {"state": "production_enabled", "activation_sha256": "b" * 64},
            {
                "artifact_sha256": "a" * 64,
                "kernel_kind": readiness.OPERATIONAL_BASELINE_KERNEL_KIND,
            },
        ),
    )
    monkeypatch.setattr(
        readiness,
        "build_resolution_bridge_pair_coverage_audit",
        lambda **_kwargs: _coverage(False),
    )
    monkeypatch.setattr(
        readiness,
        "audit_physical_x",
        lambda *_args, **_kwargs: {"ok": True, "state": "ready"},
    )
    monkeypatch.setattr(
        readiness,
        "resolve_active_v6_market_context",
        lambda **_kwargs: {
            "state": "available", "market_projection_available": True,
            "market_context": {"market_context_sha256": "d" * 64},
            "activation": {"activation_sha256": "e" * 64},
        },
    )
    monkeypatch.setattr(
        readiness,
        "_latest_reported_x_distribution",
        lambda _db: {"38": 1.0},
    )
    monkeypatch.setattr(
        readiness,
        "build_physical_only_resolution_contract",
        lambda *_args, **_kwargs: {"resolution_contract_id": "physical"},
    )
    monkeypatch.setattr(
        readiness,
        "build_posterior_market_projection",
        lambda **_kwargs: {
            "ok": True,
            "state": "market_projection_available",
            "market_bucket_probabilities": {"37C": 0.1, "38C": 0.8, "39C": 0.1},
            "reported_x_unchanged": True,
        },
    )
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        _receipt({
            "schema": "production_probability_v6_market_no_order_drill_v2",
            "ok": True,
            "state": "accepted",
            "archive_unchanged": True,
            "writes_archive_db": False,
            "external_disk_required": False,
            "forbidden_execution_tokens": [],
            "activation_bound": True,
            "bridge_activation_sha256": "b" * 64,
            "bridge_artifact_sha256": "a" * 64,
            "market_context_activation_sha256": "e" * 64,
            "market_context_sha256": "d" * 64,
        })
    )
    result = readiness.audit(
        db=db,
        probability_activation=tmp_path / "probability.json",
        bridge_activation=tmp_path / "bridge.json",
        market_context_activation=tmp_path / "context.json",
        pair_registry=tmp_path / "pairs.db",
        no_order_receipt=receipt,
        event_date="2026-07-15",
    )
    assert result["ok"] is True
    assert result["state"] == "production_ready"
    assert result["production_market_projection_ready"] is True
    assert result["pair_coverage_role"] == "calibrated_successor_readiness_only"
    assert result["boundary"]["operational_baseline_pair_coverage_is_live_gate"] is False
