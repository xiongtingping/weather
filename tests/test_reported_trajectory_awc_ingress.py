from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from reported_trajectory_awc_ingress import ReportedTrajectoryAwcIngress
from reported_trajectory_market_registry import load_config
from weather_basket_yes_schema import apply_weather_basket_yes_schema


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    apply_weather_basket_yes_schema(conn)
    registry = {
        "slug": "highest-temperature-in-beijing-on-july-18-2026",
        "event_date": "2026-07-18",
        "city_slug": "beijing",
        "station_id": "ZBAA",
        "settlement_timezone": "Asia/Shanghai",
        "settlement_unit": "C",
        "source_lane": "awc",
        "source_eligible": True,
        "outcome": "31°C",
        "lower": 31,
        "upper": 31,
        "kind": "single",
        "yes_token": "yes31",
        "no_token": "no31",
    }
    refreshed_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn.execute(
        """INSERT INTO reported_trajectory_market_registry(
             slug,event_date,outcome,city_slug,station_id,settlement_timezone,settlement_unit,
             source_lane,source_eligible,source_block_reason,resolution_url,rules_sha256,
             yes_token,no_token,market_active,market_accepting_orders,registry_json,refreshed_at
           ) VALUES(?,?,?,?,?,?,?,?,1,'','url','sha',?,?,1,1,?,?)""",
        (
            registry["slug"], registry["event_date"], registry["outcome"], registry["city_slug"],
            registry["station_id"], registry["settlement_timezone"], registry["settlement_unit"],
            registry["source_lane"], registry["yes_token"], registry["no_token"],
            json.dumps(registry), refreshed_at,
        ),
    )
    conn.commit()
    return conn


def test_legacy_hard_no_history_stays_readable_but_is_not_initialized() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE reported_trajectory_no_intents("
            "order_intent_id TEXT PRIMARY KEY, archived_note TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO reported_trajectory_no_intents VALUES('legacy-no-1','archive')"
        )

        status = apply_weather_basket_yes_schema(conn)

        assert conn.execute(
            "SELECT archived_note FROM reported_trajectory_no_intents"
        ).fetchone()[0] == "archive"
        assert status["hard_no_writes_enabled"] is False
        assert status["retired_hard_no_tables"] == [
            "reported_trajectory_no_intents"
        ]
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='reported_trajectory_exit_orders'"
        ).fetchone() is None
    finally:
        conn.close()


def _poll(
    raw_text: str,
    *,
    source_report_time: str = "2026-07-18T02:00:00Z",
) -> dict[str, object]:
    return {
        "source_lane": "awc",
        "station_ids": ["ZBAA"],
        "status": 200,
        "response_started_at": "2026-07-18T02:00:00Z",
        "station_errors": [],
        "taf_requested": False,
        "reports": [{
            "station_id": "ZBAA",
            "raw_text": raw_text,
            "source_report_time": source_report_time,
        }],
    }


def test_awc_ingress_writes_truth_and_running_max_without_any_execution_surface() -> None:
    conn = _conn()
    try:
        ingress = ReportedTrajectoryAwcIngress(conn, config=load_config())
        result = ingress.process_source_poll(
            _poll("METAR ZBAA 180200Z 18004MPS CAVOK 31/20 Q1000"),
            request_started_at="2026-07-18T02:00:00Z",
            response_completed_at="2026-07-18T02:00:00.100Z",
        )

        assert result["new_observation_count"] == 1
        assert result["station_day_fact_updates"] == [{
            "event_date": "2026-07-18",
            "state": "running_max_advanced",
            "reported_max_c": 31,
            "bound_generation": 1,
        }]
        assert result["calls_order_gateway"] is False
        assert result["places_orders"] is False
        assert result["orders_submitted"] == 0
        assert conn.execute("SELECT COUNT(*) FROM reported_trajectory_observations").fetchone()[0] == 1
        assert conn.execute(
            "SELECT reported_max_c FROM reported_trajectory_station_day_bounds"
        ).fetchone()[0] == 31
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='reported_trajectory_no_intents'"
        ).fetchone() is None
    finally:
        conn.close()


def test_awc_correction_halts_station_day_without_creating_a_decision_or_order() -> None:
    conn = _conn()
    try:
        ingress = ReportedTrajectoryAwcIngress(conn, config=load_config())
        result = ingress.process_source_poll(
            _poll("METAR COR ZBAA 180200Z 18004MPS CAVOK 31/20 Q1000"),
            request_started_at="2026-07-18T02:00:00Z",
            response_completed_at="2026-07-18T02:00:00.100Z",
        )

        assert result["station_day_fact_updates"] == [{
            "event_date": "2026-07-18",
            "state": "halted_for_correction_review",
            "reason": "metar_correction_requires_manual_review",
        }]
        assert conn.execute(
            "SELECT state FROM reported_trajectory_station_halts"
        ).fetchone()[0] == "active"
        assert conn.execute("SELECT COUNT(*) FROM reported_trajectory_station_day_bounds").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM auto_trade_decisions").fetchone()[0] == 0
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='reported_trajectory_no_intents'"
        ).fetchone() is None
    finally:
        conn.close()


def test_awc_nonhourly_correction_is_retained_only_to_halt_station_day() -> None:
    conn = _conn()
    try:
        ingress = ReportedTrajectoryAwcIngress(conn, config=load_config())
        result = ingress.process_source_poll(
            _poll(
                "METAR COR ZBAA 180252Z 18004MPS CAVOK 31/20 Q1000",
                source_report_time="2026-07-18T02:52:00Z",
            ),
            request_started_at="2026-07-18T02:53:00Z",
            response_completed_at="2026-07-18T02:53:00.100Z",
        )

        assert result["new_observation_count"] == 1
        assert result["station_day_fact_updates"] == [{
            "event_date": "2026-07-18",
            "state": "halted_for_correction_review",
            "reason": "metar_correction_requires_manual_review",
        }]
        row = conn.execute(
            "SELECT valid_at,correction_suspected "
            "FROM reported_trajectory_observations"
        ).fetchone()
        assert row == ("2026-07-18T02:52:00.000Z", 1)
        assert conn.execute(
            "SELECT COUNT(*) FROM reported_trajectory_station_day_bounds"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM reported_trajectory_station_halts"
        ).fetchone()[0] == "active"
    finally:
        conn.close()


def test_awc_offhour_routine_metar_is_formal_and_advances_the_running_max() -> None:
    """Routine METARs are formal at the provider's own official minute.

    EDDM/EFHK/EGLC/LIMC/LTAC publish at :20/:50, EHAM at :25/:55 and the
    ASOS-fed United States stations at :51-:58.  Under the previous
    literal-`:00` contract all 17 of them produced no daily-high truth
    at all.
    """

    conn = _conn()
    try:
        ingress = ReportedTrajectoryAwcIngress(conn, config=load_config())
        result = ingress.process_source_poll(
            _poll(
                "METAR ZBAA 180252Z 18004MPS CAVOK 31/20 Q1000",
                source_report_time="2026-07-18T02:52:00Z",
            ),
            request_started_at="2026-07-18T02:53:00Z",
            response_completed_at="2026-07-18T02:53:00.100Z",
        )

        assert result["new_observation_count"] == 1
        assert result["nonformal_or_malformed_report_count"] == 0
        assert result["station_day_fact_updates"] == [{
            "event_date": "2026-07-18",
            "state": "running_max_advanced",
            "reported_max_c": 31,
            "bound_generation": 1,
        }]
        assert conn.execute(
            "SELECT COUNT(*) FROM reported_trajectory_station_halts"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_awc_running_max_is_monotonic_across_every_routine_minute() -> None:
    """All routine reports are formal, so the bound is a plain maximum.

    A cooler later report -- whether :00 after :52, or :50 after :20 --
    leaves the running maximum untouched; both rows are retained as
    evidence.
    """

    conn = _conn()
    try:
        ingress = ReportedTrajectoryAwcIngress(conn, config=load_config())
        ingress.process_source_poll(
            _poll(
                "METAR ZBAA 180220Z 18004MPS CAVOK 31/20 Q1000",
                source_report_time="2026-07-18T02:20:00Z",
            ),
            request_started_at="2026-07-18T02:21:00Z",
            response_completed_at="2026-07-18T02:21:00.100Z",
        )
        result = ingress.process_source_poll(
            _poll(
                "METAR ZBAA 180250Z 18004MPS CAVOK 29/20 Q1000",
                source_report_time="2026-07-18T02:50:00Z",
            ),
            request_started_at="2026-07-18T02:51:00Z",
            response_completed_at="2026-07-18T02:51:00.100Z",
        )

        assert result["new_observation_count"] == 1
        assert result["station_day_fact_updates"] == [{
            "event_date": "2026-07-18",
            "state": "running_max_unchanged",
            "reported_max_c": 31,
        }]
        assert conn.execute(
            "SELECT COUNT(*) FROM reported_trajectory_observations"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT reported_max_c,max_valid_at "
            "FROM reported_trajectory_station_day_bounds"
        ).fetchone() == (31, "2026-07-18T02:20:00.000Z")
    finally:
        conn.close()


def test_awc_nonhourly_speci_remains_formal() -> None:
    conn = _conn()
    try:
        ingress = ReportedTrajectoryAwcIngress(conn, config=load_config())
        result = ingress.process_source_poll(
            _poll(
                "SPECI ZBAA 180252Z 18004MPS CAVOK 31/20 Q1000",
                source_report_time="2026-07-18T02:52:00Z",
            ),
            request_started_at="2026-07-18T02:53:00Z",
            response_completed_at="2026-07-18T02:53:00.100Z",
        )

        assert result["new_observation_count"] == 1
        assert result["station_day_fact_updates"] == [{
            "event_date": "2026-07-18",
            "state": "running_max_advanced",
            "reported_max_c": 31,
            "bound_generation": 1,
        }]
        assert conn.execute(
            "SELECT report_type,correction_suspected "
            "FROM reported_trajectory_observations"
        ).fetchone() == ("SPECI", 0)
    finally:
        conn.close()


def test_duplicate_awc_poll_is_zero_write_after_the_canonical_observation_is_known() -> None:
    conn = _conn()
    try:
        ingress = ReportedTrajectoryAwcIngress(conn, config=load_config())
        first = ingress.process_source_poll(
            _poll("METAR ZBAA 180200Z 18004MPS CAVOK 31/20 Q1000"),
            request_started_at="2026-07-18T02:00:00Z",
            response_completed_at="2026-07-18T02:00:00.100Z",
        )
        second = ingress.process_source_poll(
            _poll("METAR ZBAA 180200Z 18004MPS CAVOK 31/20 Q1000"),
            request_started_at="2026-07-18T02:00:01Z",
            response_completed_at="2026-07-18T02:00:01.100Z",
        )

        assert first["new_observation_count"] == 1
        assert second["new_observation_count"] == 0
        assert second["duplicate_poll_zero_write"] is True
        assert conn.execute("SELECT COUNT(*) FROM reported_trajectory_source_polls").fetchone()[0] == 1
    finally:
        conn.close()


def test_awc_ingress_has_no_gateway_or_order_client_import() -> None:
    source = Path(__file__).parents[1].joinpath("reported_trajectory_awc_ingress.py").read_text(encoding="utf-8")
    assert "ClobOrderServiceClient" not in source
    assert "place_order" not in source
    assert "auto_trade_decisions" not in source


def test_origin_registry_sha_is_resolved_for_a_known_station() -> None:
    # An AWC report is raw METAR and carries no registry identity, so this is
    # the only place the observation can be bound to the registry that
    # admitted its station.  Written empty from 2026-08-01 on, it terminated
    # every station in the PR12 contract builder and stopped truth revision
    # facts -- and with them every order.
    from reported_trajectory_awc_ingress import _origin_registry_sha256

    resolved = _origin_registry_sha256("RKPK")

    assert len(resolved) == 64
    assert resolved == _origin_registry_sha256("KDAL")


def test_origin_registry_sha_degrades_instead_of_stopping_capture() -> None:
    from reported_trajectory_awc_ingress import _origin_registry_sha256

    assert _origin_registry_sha256("") == ""
    assert _origin_registry_sha256("   ") == ""
