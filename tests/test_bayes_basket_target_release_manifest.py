from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import scripts.build_bayes_basket_target_release_manifest as release_module
from bayes_basket_target import FORWARD_SCORECARD_SCHEMA, build_forward_scorecard
from scripts.build_bayes_basket_target_release_manifest import build_manifest


ROOT = Path(__file__).resolve().parents[1]
ACTIVATION = ROOT / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"


def test_release_manifest_is_blocked_and_never_authorizes_orders(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db):
        pass
    ws = tmp_path / "quotes.json"
    ws.write_text("{}\n", encoding="utf-8")
    result = build_manifest(
        root=ROOT,
        db=db,
        activation=ACTIVATION,
        manifest=ws,
        generated_at_utc="2026-07-23T00:00:00Z",
        probe_services=False,
    )
    assert result["release_state"] == "BLOCKED_NOT_RELEASE"
    assert result["readiness_ok"] is False
    assert result["live_submit_authorized"] is False
    assert result["places_orders"] is False
    assert result["calls_order_gateway"] is False
    assert result["forward_scorecard"]["state"] == "BLOCKED"
    assert "target_forward_scorecard_incomplete" not in result["blockers"]
    assert "target_forward_scorecard_incomplete" not in (
        result["formal_promotion_blockers"]
    )
    assert result["formal_promotion_ready"] is False
    assert result["manifest_sha256"]


def test_release_manifest_hash_is_stable_for_same_inputs_and_time(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db):
        pass
    ws = tmp_path / "quotes.json"
    ws.write_text("{}\n", encoding="utf-8")
    kwargs = {
        "root": ROOT,
        "db": db,
        "activation": ACTIVATION,
        "manifest": ws,
        "generated_at_utc": "2026-07-23T00:00:00Z",
        "probe_services": False,
    }
    first = build_manifest(**kwargs)
    second = build_manifest(**kwargs)
    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_forward_scorecard_is_exported_from_package_public_interface() -> None:
    from bayes_basket_target.forward_scorecard import SCHEMA

    assert FORWARD_SCORECARD_SCHEMA == SCHEMA
    assert build_forward_scorecard.__name__ == "build_forward_scorecard"


def test_release_manifest_no_live_can_pass_before_forward_oos(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db):
        pass
    ws = tmp_path / "quotes.json"
    ws.write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(
        release_module,
        "audit",
        lambda **_: {
            "ok": True,
            "blockers": [],
            "forward_scorecard": {
                "schema": "bayes_basket_target_forward_scorecard_gate_v1",
                "state": "BLOCKED",
                "ready": False,
                "ready_for_cash_scorecard": False,
                "ready_for_station_scorecard": False,
                "receipt": None,
            },
        },
    )

    result = build_manifest(
        root=ROOT,
        db=db,
        activation=ACTIVATION,
        manifest=ws,
        generated_at_utc="2026-07-27T00:00:00Z",
        probe_services=False,
    )

    assert result["readiness_ok"] is True
    assert result["production_no_live_ready"] is True
    assert result["release_state"] == "READY_FOR_OPERATOR_CUTOVER"
    assert "target_forward_scorecard_incomplete" not in result["blockers"]
    assert result["formal_promotion_ready"] is True
    assert "target_forward_scorecard_incomplete" not in (
        result["formal_promotion_blockers"]
    )


def test_release_manifest_keeps_gateway_violation_as_no_live_blocker(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db):
        pass
    ws = tmp_path / "quotes.json"
    ws.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        release_module,
        "audit",
        lambda **_: {
            "ok": False,
            "blockers": ["forward_scorecard_gateway_boundary_invalid"],
            "formal_promotion_blockers": [],
            "forward_scorecard": {
                "schema": "bayes_basket_target_forward_scorecard_gate_v1",
                "state": "BLOCKED",
                "ready": False,
                "ready_for_cash_scorecard": False,
                "ready_for_station_scorecard": False,
                "calls_order_gateway": True,
                "orders_submitted": 0,
                "receipt": None,
            },
        },
    )

    result = build_manifest(
        root=ROOT,
        db=db,
        activation=ACTIVATION,
        manifest=ws,
        generated_at_utc="2026-07-27T00:00:00Z",
        probe_services=False,
    )

    assert result["production_no_live_ready"] is False
    assert result["release_state"] == "BLOCKED_NOT_RELEASE"
    assert "forward_scorecard_gateway_boundary_invalid" in result["blockers"]
