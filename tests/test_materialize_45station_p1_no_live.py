from __future__ import annotations

import json
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError
from scripts import materialize_45station_p1_no_live as cli


def test_future_day_is_explicit_no_eligible_and_never_waits(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.db"
    canonical = tmp_path / "canonical.db"
    target.touch()
    canonical.touch()

    row = {
        "station_id": "ZBAA",
        "event_date": "2026-08-01",
        "settlement_timezone": "Asia/Shanghai",
        "settlement_unit": "C",
        "outcome": "30°C",
        "source_lane": "amsc",
    }
    monkeypatch.setattr(cli, "load_active_registry_rows", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(
        cli,
        "load_started_registry_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("future day must not wait")),
    )
    result = cli.materialize_45station_p1_no_live(
        db_path=target,
        canonical_db_path=canonical,
        event_date="2026-08-01",
        as_of_utc="2026-07-31T04:00:00Z",
        station_ids=["ZBAA"],
    )

    assert result["state"] == cli.NO_ELIGIBLE_P1
    assert result["scopes"] == [
        {
            "station_id": "ZBAA",
            "event_date": "2026-08-01",
            "settlement_timezone": "Asia/Shanghai",
            "source_lane": "AMSC",
            "state": "NO_ELIGIBLE_P1",
            "reason": "future_event_day_no_eligible_p1",
            "observation_count": 0,
            "p1_update_count": 0,
            "p1_checkpoint_ids": [],
            "p0_root_checkpoint_id": "",
            "p0_rebase": False,
            "writes": [],
            "writes_target_ssot": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
            "live_order_authorized": False,
        }
    ]
    assert result["boundary"]["calls_order_gateway"] is False
    assert result["boundary"]["orders_submitted"] == 0


def test_canonical_station_without_active_market_is_no_eligible(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.db"
    canonical = tmp_path / "canonical.db"
    target.touch()
    canonical.touch()

    def missing_scope(*_args, **_kwargs):
        raise ContractError("target_market_registry_snapshot_empty")

    monkeypatch.setattr(cli, "load_active_registry_rows", missing_scope)
    result = cli.materialize_45station_p1_no_live(
        db_path=target,
        canonical_db_path=canonical,
        event_date="2026-08-01",
        as_of_utc="2026-07-31T04:00:00Z",
        station_ids=["ZBAA"],
    )

    assert result["state"] == cli.NO_ELIGIBLE_P1
    assert result["blocked_scope_count"] == 0
    assert result["scopes"][0]["reason"] == (
        "no_active_market_scope_no_eligible_p1"
    )
    assert result["boundary"]["writes_target_ssot"] is False
    assert result["boundary"]["orders_submitted"] == 0


def test_no_commit_does_not_call_target_materializer_or_gateway(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.db"
    canonical = tmp_path / "canonical.db"
    target.touch()
    canonical.touch()

    row = {
        "station_id": "KATL",
        "event_date": "2026-07-31",
        "settlement_timezone": "America/New_York",
        "settlement_unit": "F",
        "outcome": "75°F or below",
        "source_lane": "awc",
        "report_type": "METAR",
    }
    envelope = {
        "stage": "P0",
        "checkpoint_id": "p0-test",
        "station_id": "KATL",
        "event_date": "2026-07-31",
        "settlement_timezone": "America/New_York",
        "decision_at_utc": "2026-07-31T04:00:00Z",
        "fresh_until_utc": "2026-07-31T08:00:00Z",
        "bucket_ordinals": {"75°F or below": 0},
        "probabilities": {"75°F or below": 1.0},
        "posterior_version": "p0-test",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
        "lineage": {
            "source": "test",
            "settlement_timezone": "America/New_York",
            "season_bin": "JJA",
            "settlement_definition": "official_daily_tmax_c",
        },
    }
    packet = {"expected_envelope": envelope, "created_at_utc": "2026-07-31T04:10:00Z"}
    observation = {
        "station_id": "KATL",
        "event_date_local": "2026-07-31",
        "source_lane": "awc",
        "report_type": "METAR",
        "evidence_role": "FORMAL",
        "qc_status": "ACCEPTED",
        "correction_suspected": False,
        "canonical_committed_at_utc": "2026-07-31T04:05:00Z",
        "valid_at_utc": "2026-07-31T04:00:00Z",
        "observation_id": "obs-1",
    }
    monkeypatch.setattr(cli, "load_active_registry_rows", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(cli, "load_started_registry_rows", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(cli, "load_resident_formal_observations", lambda *_args, **_kwargs: [observation])
    monkeypatch.setattr(
        cli,
        "load_materialized_probability_envelopes",
        lambda *_args, **_kwargs: [cli.ProbabilityEnvelope.from_mapping(envelope)],
    )
    monkeypatch.setattr(
        cli,
        "select_p1_replay_base",
        lambda *_args, **_kwargs: (
            cli.ProbabilityEnvelope.from_mapping(envelope),
            cli.ProbabilityEnvelope.from_mapping(envelope),
            set(),
            False,
        ),
    )
    monkeypatch.setattr(cli, "_resolve_trajectory_artifact", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "compile_station_p1_packets", lambda **_kwargs: [packet])
    monkeypatch.setattr(
        cli,
        "probability_bridge_schema_status",
        lambda *_args, **_kwargs: {"ok": True, "missing_tables": []},
    )
    monkeypatch.setattr(
        cli,
        "build_xsb_projection",
        lambda *_args, **_kwargs: {"probability_by_bucket": []},
    )
    monkeypatch.setattr(
        cli,
        "materialize_probability_bridge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no --commit must not write")),
    )

    result = cli.materialize_45station_p1_no_live(
        db_path=target,
        canonical_db_path=canonical,
        event_date="2026-07-31",
        as_of_utc="2026-07-31T04:20:00Z",
        station_ids=["KATL"],
        commit=False,
    )

    assert result["state"] == cli.VALIDATED_NO_WRITE
    assert result["p1_update_count"] == 1
    assert result["boundary"]["writes_target_ssot"] is False
    assert result["boundary"]["calls_order_gateway"] is False
    assert result["boundary"]["orders_submitted"] == 0


def test_cli_prints_no_eligible_result_and_returns_success(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    expected = {
        "state": cli.NO_ELIGIBLE_P1,
        "boundary": {
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
        },
    }
    monkeypatch.setattr(cli, "materialize_45station_p1_no_live", lambda **_kwargs: expected)
    assert cli.main([
        "--db", str(tmp_path / "target.db"),
        "--canonical-db", str(tmp_path / "canonical.db"),
        "--event-date", "2026-07-31",
        "--as-of-utc", "2026-07-31T04:00:00Z",
        "--station-id", "KATL",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == expected


def test_amsc_station_rejects_awc_observation_instead_of_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.db"
    canonical = tmp_path / "canonical.db"
    target.touch()
    canonical.touch()
    row = {
        "station_id": "ZBAA",
        "event_date": "2026-07-31",
        "settlement_timezone": "Asia/Shanghai",
        "settlement_unit": "C",
        "outcome": "30°C",
        "source_lane": "amsc",
    }
    monkeypatch.setattr(cli, "load_active_registry_rows", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(cli, "load_started_registry_rows", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(
        cli,
        "load_resident_formal_observations",
        lambda *_args, **_kwargs: [{
            "station_id": "ZBAA",
            "event_date_local": "2026-07-31",
            "source_lane": "awc",
            "report_type": "METAR",
            "evidence_role": "FORMAL",
            "qc_status": "ACCEPTED",
            "correction_suspected": False,
            "canonical_committed_at_utc": "2026-07-31T04:05:00Z",
            "valid_at_utc": "2026-07-31T04:00:00Z",
        }],
    )
    result = cli.materialize_45station_p1_no_live(
        db_path=target,
        canonical_db_path=canonical,
        event_date="2026-07-31",
        as_of_utc="2026-07-31T04:20:00Z",
        station_ids=["ZBAA"],
    )
    assert result["state"] == cli.BLOCKED_FAIL_CLOSED
    assert "observation_source_lane_mismatch" in result["scopes"][0]["reason"]
    assert result["boundary"]["writes_target_ssot"] is False
    assert result["boundary"]["orders_submitted"] == 0


def test_preview_never_creates_missing_target_database(tmp_path: Path) -> None:
    target = tmp_path / "missing-target.db"
    canonical = tmp_path / "canonical.db"
    canonical.touch()
    with pytest.raises(ContractError, match="target_database_missing"):
        cli.materialize_45station_p1_no_live(
            db_path=target,
            canonical_db_path=canonical,
            event_date="2026-07-31",
            as_of_utc="2026-07-31T04:00:00Z",
            station_ids=["ZBAA"],
        )
    assert not target.exists()
