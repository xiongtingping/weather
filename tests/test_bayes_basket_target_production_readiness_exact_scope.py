from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import bayes_basket_target.readiness_scope as readiness_scope
import scripts.bayes_basket_target_production_readiness as readiness_module
from bayes_basket_target.contracts import ContractError


ROOT = Path(__file__).resolve().parents[1]
ACTIVATION = ROOT / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
AS_OF = "2026-07-27T00:00:01Z"
POLICY_SCOPE = {
    "season_bin": "summer",
    "maturity_bin": "d1",
    "liquidity_bin": "liquid",
    "strategy_version": "target-v1",
}
RECEIPT_SHA = "d" * 64


def _create_minimal_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "target.db")
    conn.executescript(
        """
        CREATE TABLE basket_probability_envelope(
            stage TEXT NOT NULL,
            event_date TEXT NOT NULL,
            settlement_timezone TEXT NOT NULL
        );
        CREATE TABLE basket_cash_phase_policy_artifact(
            policy_version TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            station_id TEXT NOT NULL,
            station_timezone TEXT NOT NULL,
            valid_from_utc TEXT NOT NULL,
            valid_until_utc TEXT NOT NULL,
            promoted INTEGER NOT NULL
        );
        CREATE TABLE basket_cash_phase_authority_manifest(
            policy_version TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            station_id TEXT NOT NULL,
            station_timezone TEXT NOT NULL,
            valid_from_utc TEXT NOT NULL,
            valid_until_utc TEXT NOT NULL,
            authority_scope TEXT NOT NULL
        );
        INSERT INTO basket_probability_envelope VALUES
            ('P1', '2026-07-27', 'UTC');
        INSERT INTO basket_cash_phase_policy_artifact VALUES
            ('candidate-v1', 'a', 'RKPK', 'UTC',
             '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', 0),
            ('formal-v1', 'b', 'RKPK', 'UTC',
             '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z', 1);
        INSERT INTO basket_cash_phase_authority_manifest VALUES
            ('candidate-v1', 'a', 'RKPK', 'UTC',
             '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z',
             'PRODUCTION_NO_LIVE_CANDIDATE'),
            ('formal-v1', 'b', 'RKPK', 'UTC',
             '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z',
             'FORMAL_PROMOTED');
        """
    )
    conn.commit()
    return conn


def _install_aggregate_ready_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only the aggregate side true; exact readiness remains under test."""

    monkeypatch.setattr(
        readiness_module,
        "_table_names",
        lambda _conn: set(readiness_module.TARGET_INPUT_TABLES),
    )
    monkeypatch.setattr(readiness_module, "_count", lambda *_args: 1)
    monkeypatch.setattr(readiness_module, "_latest_flag", lambda *_args: 1)
    monkeypatch.setattr(readiness_module, "_count_where", lambda *_args, **_kwargs: 1)


def _registry(station_id: str, event_date: str) -> dict[str, object]:
    sha = "a" * 64
    return {
        "snapshot_sha256": sha,
        "slug": f"{station_id.lower()}-{event_date}",
        "event_date": event_date,
        "outcome": "38°C",
        "station_id": station_id,
        "settlement_timezone": "UTC",
        "yes_token": f"yes-{station_id}-{event_date}",
        "no_token": f"no-{station_id}-{event_date}",
        "rules_sha256": sha,
        "market_active": 1,
        "market_accepting_orders": 1,
    }


def _compiled(station_id: str, event_date: str) -> dict[str, object]:
    return {
        "status": "ready_no_live",
        "reason_codes": [],
        "probability_envelope": {
            "stage": "P1",
            "checkpoint_id": f"p1-{station_id}-{event_date}",
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": "UTC",
            "content_sha256": "1" * 64,
            "posterior_sha256": "2" * 64,
            "bridge_sha256": "3" * 64,
            "lineage": {"ensemble_artifact_id": "ensemble-1"},
        },
        "production_market_snapshot": {
            "snapshot_id": f"ws-{station_id}-{event_date}",
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": "UTC",
            "official_ws_quote_sha256": "4" * 64,
            "quote_freshness_policy_sha256": "5" * 64,
            "inventory_sha256": "6" * 64,
            "order_state_sha256": "7" * 64,
            "quote_observed_at": "2026-07-27T00:00:00Z",
            "quote_expires_at": "2026-07-27T00:00:05Z",
        },
        "plan": {"lineage": {"policy_resolution_sha256": "8" * 64}},
        "resolved_station_policy": {
            "activation_id": "activation-1",
            "policy_sha256": "8" * 64,
        },
        "production_inputs": {
            "probability_bridge_history_id": (
                f"xsb-{station_id}-{event_date}"
            ),
            "probability_bridge_projection_sha256": "d" * 64,
            "cash_phase_policy_sha256": "9" * 64,
            "cash_phase_authority_manifest": {
                "manifest_sha256": "c" * 64,
            },
            "strict_delta_artifact_sha256": "b" * 64,
        },
    }


def _use_scoped_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        readiness_module,
        "resolve_target_readiness_scopes",
        readiness_scope.resolve_target_readiness_scopes,
    )


def test_aggregate_counts_true_exact_scope_false_blocks_target_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_aggregate_ready_stubs(monkeypatch)
    monkeypatch.setattr(
        readiness_module,
        "resolve_target_readiness_scopes",
        lambda *_args, **_kwargs: {
            "schema": readiness_scope.SCHEMA,
            "ready": False,
            "scope_count": 2,
            "exact_scope_count": 1,
            "blocked_scope_count": 1,
            "items": [{
                "status": "MISSING",
                "reason_codes": [
                    "ContractError:materialized_probability_envelope_missing",
                ],
            }],
            "content_sha256": RECEIPT_SHA,
        },
    )
    conn = _create_minimal_db(tmp_path)
    try:
        inputs = readiness_module._target_inputs(
            conn,
            as_of_utc=AS_OF,
            policy_authority_scope="FORMAL_PROMOTED",
            target_scope=POLICY_SCOPE,
            station_ids=["AAAA", "BBBB"],
        )
    finally:
        conn.close()

    assert inputs["aggregate_counts_ready"] is True
    assert inputs["exact_scope_readiness"]["ready"] is False
    assert inputs["exact_scope_readiness"]["items"][0]["reason_codes"] == [
        "ContractError:materialized_probability_envelope_missing",
    ]
    assert inputs["ready"] is False


def test_exact_scope_true_is_required_and_receipt_digest_is_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_aggregate_ready_stubs(monkeypatch)
    _use_scoped_resolver(monkeypatch)
    monkeypatch.setattr(
        readiness_scope,
        "load_started_registry_rows",
        lambda *_args, **_kwargs: [_registry("AAAA", "2026-07-27")],
    )
    monkeypatch.setattr(
        readiness_scope,
        "compile_production_cycle_from_ssot",
        lambda _conn, *, station_id, event_date, **_kwargs: _compiled(
            station_id, event_date
        ),
    )
    conn = _create_minimal_db(tmp_path)
    try:
        inputs = readiness_module._target_inputs(
            conn,
            as_of_utc=AS_OF,
            policy_authority_scope="FORMAL_PROMOTED",
            target_scope=POLICY_SCOPE,
            station_ids=["AAAA"],
        )
    finally:
        conn.close()

    assert inputs["aggregate_counts_ready"] is True
    assert inputs["exact_scope_readiness"]["ready"] is True
    assert inputs["exact_scope_readiness"]["exact_scope_count"] == 1
    assert len(inputs["exact_scope_readiness"]["content_sha256"]) == 64


def test_missing_p1_in_one_started_scope_blocks_even_when_other_scope_is_exact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_aggregate_ready_stubs(monkeypatch)
    _use_scoped_resolver(monkeypatch)
    monkeypatch.setattr(
        readiness_scope,
        "load_started_registry_rows",
        lambda *_args, **_kwargs: [
            _registry("AAAA", "2026-07-27"),
            _registry("BBBB", "2026-07-27"),
        ],
    )

    def compile_scope(_conn, *, station_id, event_date, **_kwargs):
        if station_id == "BBBB":
            raise ContractError("materialized_probability_envelope_missing")
        return _compiled(station_id, event_date)

    monkeypatch.setattr(readiness_scope, "compile_production_cycle_from_ssot", compile_scope)
    conn = _create_minimal_db(tmp_path)
    try:
        inputs = readiness_module._target_inputs(
            conn,
            as_of_utc=AS_OF,
            policy_authority_scope="FORMAL_PROMOTED",
            target_scope=POLICY_SCOPE,
            station_ids=["AAAA", "BBBB"],
        )
    finally:
        conn.close()

    exact = inputs["exact_scope_readiness"]
    assert inputs["aggregate_counts_ready"] is True
    assert exact["scope_count"] == 2
    assert exact["exact_scope_count"] == 1
    assert exact["ready"] is False
    assert [item["status"] for item in exact["items"]] == [
        "EXACTLY_ONE",
        "MISSING",
    ]
    assert exact["items"][1]["reason_codes"] == [
        "ContractError:materialized_probability_envelope_missing",
    ]
    assert inputs["ready"] is False


def test_irrelevant_station_and_date_cannot_satisfy_target_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_aggregate_ready_stubs(monkeypatch)
    _use_scoped_resolver(monkeypatch)
    rows = [
        _registry("TARGET", "2026-07-27"),
        _registry("IRRELEVANT", "2026-07-28"),
    ]
    monkeypatch.setattr(
        readiness_scope,
        "load_started_registry_rows",
        lambda *_args, **_kwargs: rows,
    )
    compiled_calls: list[tuple[str, str]] = []

    def compile_scope(_conn, *, station_id, event_date, **_kwargs):
        compiled_calls.append((station_id, event_date))
        raise ContractError("materialized_probability_envelope_missing")

    monkeypatch.setattr(readiness_scope, "compile_production_cycle_from_ssot", compile_scope)
    conn = _create_minimal_db(tmp_path)
    try:
        inputs = readiness_module._target_inputs(
            conn,
            as_of_utc=AS_OF,
            policy_authority_scope="FORMAL_PROMOTED",
            target_scope=POLICY_SCOPE,
            station_ids=["target"],
        )
    finally:
        conn.close()

    exact = inputs["exact_scope_readiness"]
    assert exact["scope_count"] == 1
    assert exact["items"][0]["station_id"] == "TARGET"
    assert compiled_calls == [("TARGET", "2026-07-27")]
    assert exact["items"][0]["reason_codes"] == [
        "ContractError:materialized_probability_envelope_missing",
    ]
    assert exact["ready"] is False
    assert inputs["ready"] is False


def test_candidate_formal_authority_mismatch_remains_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_aggregate_ready_stubs(monkeypatch)
    _use_scoped_resolver(monkeypatch)
    monkeypatch.setattr(
        readiness_scope,
        "load_started_registry_rows",
        lambda *_args, **_kwargs: [_registry("AAAA", "2026-07-27")],
    )

    def compile_scope(_conn, **kwargs):
        assert kwargs["policy_authority_scope"] == "FORMAL_PROMOTED"
        raise ContractError("policy_authority_scope_candidate_formal_mismatch")

    monkeypatch.setattr(readiness_scope, "compile_production_cycle_from_ssot", compile_scope)
    conn = _create_minimal_db(tmp_path)
    try:
        inputs = readiness_module._target_inputs(
            conn,
            as_of_utc=AS_OF,
            policy_authority_scope="FORMAL_PROMOTED",
            target_scope=POLICY_SCOPE,
            station_ids=["AAAA"],
        )
    finally:
        conn.close()

    exact = inputs["exact_scope_readiness"]
    assert exact["items"][0]["status"] == "AUTHORITY_MISMATCH"
    assert exact["items"][0]["reason_codes"] == [
        "ContractError:policy_authority_scope_candidate_formal_mismatch",
    ]
    assert exact["ready"] is False
    assert inputs["ready"] is False


def test_audit_forwards_activation_scope_and_allowlist_to_resolver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_aggregate_ready_stubs(monkeypatch)
    captured: dict[str, object] = {}

    def resolver(_conn, **kwargs):
        captured.update(kwargs)
        return {
            "schema": readiness_scope.SCHEMA,
            "ready": False,
            "scope_count": 1,
            "exact_scope_count": 0,
            "blocked_scope_count": 1,
            "items": [{
                "status": "MISSING",
                "reason_codes": ["readiness_scope_missing"],
            }],
            "content_sha256": RECEIPT_SHA,
        }

    monkeypatch.setattr(readiness_module, "resolve_target_readiness_scopes", resolver)
    monkeypatch.setattr(
        readiness_module,
        "inspect_migration",
        lambda _conn: {
            "legacy_decision_table_collision": False,
            "production_ssot_schema": {"ok": True},
            "target_ledger_schema": {"ok": True},
        },
    )
    monkeypatch.setattr(
        readiness_module,
        "_capital_pool",
        lambda _conn: {"ready": True, "state": "ready"},
    )
    monkeypatch.setattr(
        readiness_module,
        "_db_activation_alignment",
        lambda _conn, _activation: {"aligned": True, "state": "checked"},
    )
    monkeypatch.setattr(
        readiness_module,
        "_official_ws_scope_coverage",
        lambda *_args, **_kwargs: {
            "state": "loaded",
            "complete_scope_count": 0,
            "incomplete_scope_count": 1,
        },
    )
    monkeypatch.setattr(
        readiness_module,
        "audit_future_day_residue",
        lambda *_args, **_kwargs: {"checks": {"future_residue_isolated": True}},
    )
    monkeypatch.setattr(
        readiness_module,
        "inspect_target_resource_budget",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(readiness_module, "assert_target_database_owner", lambda _conn: None)
    monkeypatch.setattr(readiness_module, "_unfinished_target_inputs", lambda *a, **k: [])
    monkeypatch.setattr(readiness_module, "_main_chain_status", lambda *a, **k: {"ready": False})
    monkeypatch.setattr(
        readiness_module,
        "_calibration_evidence",
        lambda _path: {
            "state": "VALID_BLOCKED_EVIDENCE",
            "checks": {},
            "et_history_evidence": {},
        },
    )
    monkeypatch.setattr(
        readiness_module,
        "_native_decode_evidence",
        lambda _path: {"state": "VALID_BLOCKED_NATIVE_DECODE_EVIDENCE"},
    )

    db = tmp_path / "target.db"
    conn = _create_minimal_db(tmp_path)
    conn.close()
    manifest = tmp_path / "target-manifest.json"
    manifest.write_text(json.dumps({"state": "connected"}) + "\n", encoding="utf-8")

    result = readiness_module.audit(
        db=db,
        activation_path=ACTIVATION,
        manifest_path=manifest,
        calibration_evidence_path=tmp_path / "calibration.json",
        native_decode_evidence_path=tmp_path / "native.json",
        probe_services=False,
        station_ids=["rkpk"],
    )

    with ACTIVATION.open(encoding="utf-8") as handle:
        activation = json.load(handle)
    assert captured["target_scope"] == activation["target_scope"]
    assert captured["station_ids"] == ["rkpk"]
    assert captured["authority_scope"] == activation["policy_authority_scope"]
    assert result["target_inputs"]["ready"] is False
    assert result["target_inputs"]["exact_scope_readiness"]["content_sha256"] == RECEIPT_SHA
    assert result["target_inputs"]["exact_scope_readiness"]["items"][0]["reason_codes"] == [
        "readiness_scope_missing",
    ]
