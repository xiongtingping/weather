"""Charter 185: the activation walk reads in the order it walks, and stops.

``_activated_lead_bucket`` walks a scope's valid activations newest
generation first and returns at the first admissible one.  With every
activation still valid (RCSS DESCENT h12_24: 707 of 707 on 2026-09-21) the
charter-176 window seek kept them all, and each of 138 calls a cycle read
and sorted every one before the walk took one or two -- 3-7s a cycle.
"""

from __future__ import annotations

import json
import sqlite3

from bayes_basket_target import native_ensemble_p0 as p0
from bayes_basket_target.ssot import apply_production_ssot_schema, declared_index_names
from tests.test_native_ensemble_p0_training_lead_bucket import (
    DECISION_AT,
    SYSTEMS,
    _conn,
)

GENERATION = "calibration_native_activation_generation_idx"

OLD = (
    "SELECT r.ensemble_system_ids_json,a.run_cycle_by_system_json "
    "FROM calibration_native_ensemble_activation a "
    "JOIN calibration_cross_native_ensemble_artifact r "
    "ON r.cross_ensemble_artifact_id=a.artifact_id "
    "WHERE a.artifact_kind='CROSS_NATIVE_ENSEMBLE' "
    "AND a.station_id=? AND a.season_bin=? AND a.lead_bucket=? "
    "AND a.authority_scope='PRODUCTION_NO_LIVE_ONLY' "
    "AND a.valid_from_utc<=? AND a.valid_until_utc>? "
    "ORDER BY a.generation DESC,a.activation_id LIMIT 256"
)


def _walk_sql(conn) -> list[str]:
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    p0._activated_lead_bucket(
        conn, station_id="KLGA", season_bin="JJA", decision_at_utc=DECISION_AT,
        observed_system_ids=SYSTEMS, candidates=("h12_24",),
    )
    conn.set_trace_callback(None)
    # The walk itself: the activation rows joined to their artifacts.  The
    # id list the kept answer is checked against reads the same table first.
    return [
        s for s in statements
        if "calibration_native_ensemble_activation a" in s
        and "JOIN calibration_cross_native_ensemble_artifact r" in s
    ]


def test_the_index_is_declared_where_the_resident_reads_its_schema() -> None:
    assert declared_index_names()[GENERATION] == "calibration_native_ensemble_activation"


def test_the_walk_streams_off_the_generation_index(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    apply_production_ssot_schema(conn)
    sql = _walk_sql(conn)
    assert sql
    plan = " | ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql[0]))
    assert GENERATION in plan, plan
    assert "TEMP B-TREE" not in plan, plan


def test_the_rows_and_their_order_are_the_old_reads(tmp_path) -> None:
    conn = _conn()
    conn.execute(
        "CREATE INDEX calibration_native_activation_generation_idx ON"
        " calibration_native_ensemble_activation(station_id,artifact_kind,"
        "season_bin,lead_bucket,generation DESC,activation_id)"
    )
    conn.execute(
        "CREATE INDEX calibration_native_activation_window_idx ON"
        " calibration_native_ensemble_activation(station_id,artifact_kind,"
        "season_bin,lead_bucket,valid_until_utc)"
    )
    for n in range(60):
        artifact = f"artifact-{n}"
        systems = SYSTEMS if n % 3 else ["DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS"]
        conn.execute(
            "INSERT INTO calibration_cross_native_ensemble_artifact VALUES(?,?)",
            (artifact, json.dumps(systems)),
        )
        conn.execute(
            "INSERT INTO calibration_native_ensemble_activation VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"act-{n:03d}", "CROSS_NATIVE_ENSEMBLE", artifact, "KLGA", "JJA", "h12_24",
                "PRODUCTION_NO_LIVE_ONLY", n // 4,  # generations tie in fours
                "2026-08-09T00:00:00.000Z",
                # some already expired at the decision instant
                "2026-08-10T00:00:00.000Z" if n % 7 == 0 else "2026-09-08T00:00:00.000Z",
                json.dumps({s: "00Z" for s in systems}),
            ),
        )
    params = ("KLGA", "JJA", "h12_24", DECISION_AT, DECISION_AT)
    new = _walk_sql(conn)[0]
    assert list(conn.execute(new)) == conn.execute(OLD, params).fetchall()
