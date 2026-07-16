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
        lambda **_kwargs: {"ok": True, "state": "ready"},
    )
    writer_sha = readiness._sha(readiness.ROOT / "production_reported_x_market_transaction_writer.py")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        _receipt({
            "schema": "production_reported_x_market_no_order_drill_v1",
            "ok": True,
            "state": "accepted",
            "backup_unchanged": True,
            "forbidden_execution_tokens": [],
            "writer_sha256": writer_sha,
            "activation_bound": False,
        })
    )
    result = readiness.audit(
        db=db,
        probability_activation=tmp_path / "probability.json",
        bridge_activation=tmp_path / "bridge.json",
        pair_registry=tmp_path / "pairs.db",
        no_order_receipt=receipt,
        event_date="2026-07-15",
    )
    assert result["ok"] is True
    assert result["state"] == "safe_fail_closed_accumulating"
    assert result["production_market_projection_ready"] is False
    assert result["boundary"]["bridge_sample_or_oos_shortage_blocks_physical_x_live"] is False


def test_approved_bridge_and_ready_x_make_market_projection_ready(monkeypatch, tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _db(db)
    monkeypatch.setattr(
        readiness,
        "load_active_resolution_bridge_artifact",
        lambda **_kwargs: (
            {"state": "production_enabled", "activation_sha256": "b" * 64},
            {"artifact_sha256": "a" * 64},
        ),
    )
    monkeypatch.setattr(
        readiness,
        "build_resolution_bridge_pair_coverage_audit",
        lambda **_kwargs: _coverage(True),
    )
    monkeypatch.setattr(
        readiness,
        "audit_physical_x",
        lambda **_kwargs: {"ok": True, "state": "ready"},
    )
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        _receipt({
            "schema": "production_reported_x_market_no_order_drill_v1",
            "ok": True,
            "state": "accepted",
            "backup_unchanged": True,
            "forbidden_execution_tokens": [],
            "writer_sha256": readiness._sha(readiness.ROOT / "production_reported_x_market_transaction_writer.py"),
            "activation_bound": True,
            "bridge_activation_sha256": "b" * 64,
            "bridge_artifact_sha256": "a" * 64,
        })
    )
    result = readiness.audit(
        db=db,
        probability_activation=tmp_path / "probability.json",
        bridge_activation=tmp_path / "bridge.json",
        pair_registry=tmp_path / "pairs.db",
        no_order_receipt=receipt,
        event_date="2026-07-15",
    )
    assert result["ok"] is True
    assert result["state"] == "production_ready"
    assert result["production_market_projection_ready"] is True
