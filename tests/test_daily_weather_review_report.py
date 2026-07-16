import json
import sqlite3
from pathlib import Path

from scripts import daily_weather_review_report as report


def _create_review_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            composer_version TEXT,
            fact_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY,
            run_id INTEGER,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            decided_state TEXT,
            will_order INTEGER,
            reason_codes_json TEXT,
            reasons_json TEXT,
            details_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE auto_orders (
            id INTEGER PRIMARY KEY,
            order_id TEXT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            price REAL,
            size REAL,
            strategy TEXT,
            created_at TEXT,
            order_status TEXT,
            order_checked_at TEXT
        )
        """
    )
    fact_payload = {
        "slug": "highest-temperature-in-chengdu-on-july-4-2026",
        "event_date": "2026-07-04",
        "run_date": "2026-07-04",
        "lineage_state": {
            "fact_snapshot_id": "fact_review_1",
            "posterior_run_id": "post_review_1",
            "source_event_ids": ["srcevt_review_1"],
        },
        "source_observations": [{"source_event_id": "srcevt_review_1"}],
        "distribution": [
            {"outcome": "30", "q_temperature_bucket_posterior": 0.7},
            {"outcome": "31", "q_temperature_bucket_posterior": 0.3},
        ],
        "posterior_target_bucket_selection": {
            "target_bucket": "30",
            "target_probability": 0.7,
        },
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "fact_snapshot_id": "fact_review_1",
            "posterior_run_id": "post_review_1",
            "target_bucket": "30",
            "target_probability": 0.7,
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "phase": "D0",
            "beta_weak": 0.4,
            "beta_strong": 0.8,
            "phase_reason": "unit_test_report",
            "hard_mask_reason": "none",
        },
        "settlement_actual": 30.0,
        "signal_to_order_timing": {"signal_id": "sig_review_1"},
    }
    conn.execute(
        """
        INSERT INTO event_fact_history
            (id, run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "2026-07-03",
            "highest-temperature-in-chengdu-on-july-4-2026",
            "2026-07-04",
            "2026-07-04T04:06:00+00:00",
            "test",
            json.dumps(fact_payload),
            "2026-07-04T04:06:01+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO auto_trade_decisions
            (id, run_date, slug, event_date, outcome, side, action, decided_state,
             will_order, reason_codes_json, reasons_json, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "2026-07-04",
            "highest-temperature-in-chengdu-on-july-4-2026",
            "2026-07-04",
            "30",
            "YES",
            "entry",
            "entry",
            1,
            "[]",
            "[]",
            "{}",
            "2026-07-04T04:06:02+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO auto_orders
            (id, order_id, slug, event_date, outcome, side, action, price,
             size, strategy, created_at, order_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            "ord_review_1",
            "highest-temperature-in-chengdu-on-july-4-2026",
            "2026-07-04",
            "30",
            "YES",
            "entry",
            0.5,
            1.0,
            "hard_source_direct",
            "2026-07-04T04:06:03+00:00",
            "submitted",
        ),
    )
    conn.commit()
    conn.close()
    return db_path


def _ui_snapshot_payload() -> dict:
    return {
        "ok": True,
        "snapshot_id": "uisnap_review_1",
        "run_date": "2026-07-04",
        "source_fact_ids": [1, 999],
        "lineage_state": {
            "schema": "ui_snapshot_lineage_state_v1",
            "placeholder": False,
            "fact_lineage_complete": True,
            "source_fact_ids": [1, 999],
            "fact_snapshot_ids": ["fact_review_1", "fact_other"],
            "source_event_ids": ["srcevt_review_1", "srcevt_other"],
            "items": [
                {
                    "slug": "highest-temperature-in-chengdu-on-july-4-2026",
                    "event_date": "2026-07-04",
                    "fact_id": 1,
                    "fact_snapshot_id": "fact_review_1",
                    "posterior_run_id": "post_review_1",
                    "source_event_ids": ["srcevt_review_1"],
                    "signal_id": "sig_review_1",
                },
                {
                    "slug": "highest-temperature-in-chengdu-on-july-3-2026",
                    "event_date": "2026-07-03",
                    "fact_id": 999,
                    "fact_snapshot_id": "fact_other",
                    "source_event_ids": ["srcevt_other"],
                }
            ],
        },
        "facts": {
            "items": [
                {"slug": "highest-temperature-in-chengdu-on-july-4-2026", "event_date": "2026-07-04", "fact_id": 1},
                {"slug": "highest-temperature-in-chengdu-on-july-3-2026", "event_date": "2026-07-03", "fact_id": 999},
            ]
        },
    }


def test_daily_weather_review_report_attaches_ui_lineage_audit_read_only(tmp_path: Path) -> None:
    db_path = _create_review_db(tmp_path)

    result = report.build_report(
        db_path,
        fact_id="1",
        include_ui_snapshot_audit=True,
        include_replay_audit=False,
        ui_snapshot_payload=_ui_snapshot_payload(),
    )

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["guards"]["writes_daily_weather_reviews"] is False
    assert result["guards"]["fetches_external_weather_sources"] is False
    assert result["review"]["first_bad_link"] == "none"
    assert result["review"]["posterior_run_id"] == "post_review_1"
    posterior_lineage = result["review"]["review_json"]["posterior_lineage"]
    assert posterior_lineage["status"] == "posterior_ssot_fact_present"
    assert posterior_lineage["posterior_target_source"] == "posterior_ssot_fact"
    assert posterior_lineage["fact_snapshot_id"] == "fact_review_1"
    assert posterior_lineage["posterior_run_id"] == "post_review_1"
    chain = result["review"]["review_json"]["chain_node_trace"]
    assert chain["schema"] == "daily_review_chain_node_trace_v1"
    nodes = {item["node_id"]: item for item in chain["nodes"]}
    assert nodes["fact_write"]["output_refs"]["fact_snapshot_id"] == "fact_review_1"
    assert nodes["fact_write"]["timestamps"]["fact_as_of"] == "2026-07-04T04:06:00+00:00"
    assert nodes["decision"]["timestamps"]["created_at"] == "2026-07-04T04:06:02+00:00"
    assert nodes["execution"]["timestamps"]["created_at"] == "2026-07-04T04:06:03+00:00"
    assert result["review"]["review_json"]["audit_attachment_summary"]["attachment_count"] == 1
    assert result["review"]["review_json"]["calibration_metrics"]["posterior_brier"] is not None
    assert result["review"]["review_json"]["error_attribution"]["primary_link"] == "none"
    assert result["review"]["daily_calibration_record"]["state"] == "no_apply"
    assert result["review"]["parameter_change_state"]["state"] == "no_candidates"
    assert result["review"]["rolling_calibration_summary"]["windows"]["30d"]["record_count"] == 1
    assert result["guards"]["writes_parameter_change_events"] is False
    assert result["daily_review_persistence_plan"]["row_counts"]["daily_weather_reviews"] == 1
    assert result["daily_review_persistence_plan"]["guards"]["writes_archive_db"] is False
    assert result["daily_review_persistence_schema_status"]["tables"]["daily_weather_reviews"]["exists"] is False
    assert result["audit_attachments"]["ui_snapshot_lineage"]["ok"] is True
    assert result["audit_attachments"]["ui_snapshot_lineage"]["counts"]["checked_facts"] == 1
    assert result["audit_attachments"]["ui_snapshot_lineage"]["counts"]["source_fact_ids"] == 1
    assert result["coverage"]["decision_count"] == 1
    assert result["coverage"]["order_count"] == 1


def test_daily_weather_review_report_attaches_operator_apply_gate_summary_read_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = _create_review_db(tmp_path)

    def fake_build_confirmation_packet(db: Path | str) -> dict:
        assert Path(db) == db_path
        return {
            "schema": "phase0_apply_confirmation_packet_v1",
            "operator_apply_gate_summary": {
                "schema": "phase0_operator_apply_gate_summary_v1",
                "read_only": True,
                "execution_mode": "phase0_operator_apply_gate_summary_no_apply",
                "ready_now": False,
                "ready_after_operator_preflight": False,
                "first_blocked_gate": "daily_review_persistence_apply",
                "actionable_blocked_gates": ["daily_review_persistence_apply"],
                "gates": [
                    {
                        "gate": "phase0_schema_apply",
                        "applicable": True,
                        "ready_now": False,
                        "ready_after_operator_preflight": True,
                        "blocked": False,
                        "blockers_now": [],
                        "reason": "schema apply requires operator preflight",
                        "source": "phase0_schema_apply_manifest",
                    },
                    {
                        "gate": "daily_review_persistence_apply",
                        "applicable": True,
                        "ready_now": False,
                        "ready_after_operator_preflight": False,
                        "blocked": True,
                        "blockers_now": ["daily_review_persistence_schema_missing"],
                        "reason": "daily review persistence is not apply-ready",
                        "source": "lineage_decision_apply_readiness.daily_review_persistence",
                    },
                ],
            },
        }

    monkeypatch.setattr(
        report.phase0_apply_confirmation_packet,
        "build_confirmation_packet",
        fake_build_confirmation_packet,
    )

    result = report.build_report(
        db_path,
        fact_id="1",
        include_ui_snapshot_audit=False,
        include_replay_audit=False,
        include_operator_gate_audit=True,
    )

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["places_orders"] is False
    gate_summary = result["audit_attachments"]["operator_apply_gate_summary"]
    assert gate_summary["schema"] == "phase0_operator_apply_gate_summary_v1"
    assert gate_summary["source_packet_schema"] == "phase0_apply_confirmation_packet_v1"
    assert gate_summary["ready_now"] is False
    assert gate_summary["first_blocked_gate"] == "daily_review_persistence_apply"
    assert gate_summary["actionable_blocked_gates"] == ["daily_review_persistence_apply"]
    gates = {item["gate"]: item for item in gate_summary["gates"]}
    assert gates["phase0_schema_apply"]["ready_after_operator_preflight"] is True
    assert gates["daily_review_persistence_apply"]["blocked"] is True
    assert gates["daily_review_persistence_apply"]["blockers_now"] == [
        "daily_review_persistence_schema_missing"
    ]


def test_daily_weather_review_report_run_date_falls_back_to_event_date(tmp_path: Path) -> None:
    db_path = _create_review_db(tmp_path)

    result = report.build_report(
        db_path,
        run_date="2026-07-04",
        include_ui_snapshot_audit=True,
        include_replay_audit=False,
        ui_snapshot_payload=_ui_snapshot_payload(),
    )

    assert result["ok"] is True
    assert result["fact"]["id"] == 1
    assert result["fact"]["run_date"] == "2026-07-03"
    assert result["review"]["event_date"] == "2026-07-04"
