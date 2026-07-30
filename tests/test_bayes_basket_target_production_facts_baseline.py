from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from scripts import bayes_basket_target_production_facts_baseline as baseline


def _write_fixture(root: Path) -> None:
    for relative in set(baseline.PROTECTED_FILES) | set(baseline.RUNTIME_CLOSURE_FILES):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".json":
            path.write_text("{}\n", encoding="utf-8")
        elif relative == Path("scripts/cutover_basket_plus_hard_no_production.py"):
            path.write_text("basket_activation_not_live_enabled\n", encoding="utf-8")
        else:
            path.write_text("fixture\n", encoding="utf-8")
    (root / baseline.TARGET_ACTIVATION).write_text(json.dumps({
        "schema": "target",
        "state": "production_no_live",
        "execution_mode": "production_no_live",
        "target_only": True,
        "live_submit_authorized": False,
        "places_orders": False,
        "calls_order_gateway": False,
        "writes_production_db": False,
        "activation_contract_sha256": "a" * 64,
    }), encoding="utf-8")
    (root / baseline.PROTECTED_FILES[0]).write_text(json.dumps({
        "schema": "resident",
        "state": "enabled",
        "strategy_id": "resident",
        "live_submit_authorized": True,
        "execution_mode": "production_live",
        "places_orders": True,
        "calls_order_gateway": True,
        "activation_contract_sha256": "b" * 64,
    }), encoding="utf-8")
    (root / baseline.PROTECTED_FILES[1]).write_text(json.dumps({
        "schema": "status",
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "yes_orders_allowed": False,
    }), encoding="utf-8")
    (root / baseline.PROTECTED_FILES[2]).write_text(json.dumps({
        "schema": "ws",
        "state": "connected",
        "evidence_only": True,
        "authorizes_order": False,
    }), encoding="utf-8")
    (root / baseline.PROTECTED_FILES[3]).write_text(json.dumps({
        "schema": "floor",
        "state": "prepared",
        "execution_boundary": {
            "calculation_only": True,
            "submits_orders": False,
            "does_not_read_weather_or_probability": True,
        },
    }), encoding="utf-8")
    db = root / baseline.TARGET_DB
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.commit()


def test_baseline_is_deterministic_and_does_not_mutate_protected_files(tmp_path, monkeypatch) -> None:
    _write_fixture(tmp_path)
    monkeypatch.setattr(baseline, "PROCESS_MARKERS", {
        "market_ws": "scripts/market_ws_listener.py",
        "clob_order_service": "scripts/clob_order_service.mjs",
        "reported_trajectory_no": "scripts/reported_trajectory_no_daemon.py",
    })
    ps = subprocess.CompletedProcess(
        ["ps"],
        0,
        stdout="101 /tmp/scripts/market_ws_listener.py\n102 /tmp/scripts/clob_order_service.mjs\n103 /tmp/scripts/reported_trajectory_no_daemon.py\n",
        stderr="",
    )
    monkeypatch.setattr(baseline.subprocess, "run", lambda *args, **kwargs: ps)

    first = baseline.audit(project_root=tmp_path)
    before = {path: (tmp_path / path).read_bytes() for path in baseline.PROTECTED_FILES}
    second = baseline.audit(project_root=tmp_path)
    after = {path: (tmp_path / path).read_bytes() for path in baseline.PROTECTED_FILES}

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["facts_sha256"] == second["facts_sha256"]
    assert before == after
    assert first["boundary"]["writes_shared_live_database"] is False
    assert first["boundary"]["writes_target_database"] is False


def test_baseline_ignores_expected_ws_payload_refresh_but_keeps_contract_guard(tmp_path, monkeypatch) -> None:
    _write_fixture(tmp_path)
    monkeypatch.setattr(baseline, "PROCESS_MARKERS", {
        "market_ws": "scripts/market_ws_listener.py",
        "clob_order_service": "scripts/clob_order_service.mjs",
        "reported_trajectory_no": "scripts/reported_trajectory_no_daemon.py",
    })
    ps = subprocess.CompletedProcess(
        ["ps"],
        0,
        stdout="101 /tmp/scripts/market_ws_listener.py\n102 /tmp/scripts/clob_order_service.mjs\n103 /tmp/scripts/reported_trajectory_no_daemon.py\n",
        stderr="",
    )
    monkeypatch.setattr(baseline.subprocess, "run", lambda *args, **kwargs: ps)

    first = baseline.audit(project_root=tmp_path)
    ws_path = tmp_path / baseline.PROTECTED_FILES[2]
    ws_path.write_text(json.dumps({
        "schema": "ws",
        "state": "connected",
        "evidence_only": True,
        "authorizes_order": False,
        "quotes": [{"token_id": "new-evidence", "ask": 0.42}],
    }), encoding="utf-8")
    second = baseline.audit(project_root=tmp_path)

    assert first["facts_sha256"] == second["facts_sha256"]
    assert first["protected_files_before"][2]["sha256"] != second["protected_files_before"][2]["sha256"]
    assert first["protected_boundary"]["market_ws"]["checks"] == second["protected_boundary"]["market_ws"]["checks"]
    assert second["ok"] is True


def test_baseline_ignores_protected_runtime_heartbeat_refresh_but_keeps_no_live_guard(tmp_path, monkeypatch) -> None:
    _write_fixture(tmp_path)
    monkeypatch.setattr(baseline, "PROCESS_MARKERS", {
        "market_ws": "scripts/market_ws_listener.py",
        "clob_order_service": "scripts/clob_order_service.mjs",
        "reported_trajectory_no": "scripts/reported_trajectory_no_daemon.py",
    })
    ps = subprocess.CompletedProcess(
        ["ps"],
        0,
        stdout="101 /tmp/scripts/market_ws_listener.py\n102 /tmp/scripts/clob_order_service.mjs\n103 /tmp/scripts/reported_trajectory_no_daemon.py\n",
        stderr="",
    )
    monkeypatch.setattr(baseline.subprocess, "run", lambda *args, **kwargs: ps)

    first = baseline.audit(project_root=tmp_path)
    status_path = tmp_path / baseline.PROTECTED_FILES[1]
    status_path.write_text(json.dumps({
        "schema": "status",
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "yes_orders_allowed": False,
        "heartbeat_at": "2026-07-24T01:00:01Z",
        "pid": 999,
    }), encoding="utf-8")
    second = baseline.audit(project_root=tmp_path)

    assert first["facts_sha256"] == second["facts_sha256"]
    assert first["protected_files_before"][1]["sha256"] != second["protected_files_before"][1]["sha256"]
    assert second["protected_boundary"]["reported_trajectory_no"]["checks"]["production_no_live"] is True
    assert second["ok"] is True
