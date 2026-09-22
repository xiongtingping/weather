"""Charter 179: event-day envelope reads use the event-day index, and the P1
consume-once guard parses each prior row's lineage once.

The cycle after charter 178 (2026-09-21 09:56Z) ranked first a read of the
newest P0 of one station-day (125 calls, 26.7s): the planner took
``station_stage_recency_idx`` -- no event_date in it -- and walked the
station's P0s newest-first across every event day to find this one (ZUUU
09-23: 3,099 rows, 511ms).  The 09:43Z cycle had the P1 guard reading every
prior P1 of the station-day with its ~7.5KB lineage on each insert (259
calls, 84.0s, inside the write lock).
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import materialization as mat
from bayes_basket_target.contracts import ContractError, ProbabilityEnvelope
from bayes_basket_target.materialization import insert_probability_envelope
from bayes_basket_target.ssot import apply_production_ssot_schema
from tests.test_bayes_basket_target_materialization import (
    _envelope,
    _p0m_root,
    _trajectory_packet,
)

ROOT = Path(__file__).resolve().parents[1]
RECENCY = "basket_probability_envelope_station_stage_recency_idx"


@pytest.fixture(autouse=True)
def _fresh_memo():
    mat.forget_p1_consume_keys()
    mat.forget_lineage_documents()
    yield
    mat.forget_p1_consume_keys()
    mat.forget_lineage_documents()


def _trace(conn):
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    return statements


# --- the event-day index ---------------------------------------------------

def _envelope_event_day_reads() -> list[tuple[str, int, str]]:
    found = []
    for path in sorted((ROOT / "bayes_basket_target").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            sql = " ".join(node.value.split())
            if (
                sql.upper().startswith("SELECT")
                and "FROM basket_probability_envelope" in sql
                and "event_date=?" in sql
                and "stage" in sql
            ):
                found.append((str(path.relative_to(ROOT)), node.lineno, sql))
    return found


def test_no_event_day_envelope_read_is_planned_on_the_recency_index(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    apply_production_ssot_schema(conn)
    reads = _envelope_event_day_reads()
    newest_p0 = [
        sql for _path, _line, sql in reads
        if "ORDER BY decision_at_utc DESC" in sql and "'P0'" in sql
    ]
    # The two reads this charter moved (probability_bridge's restatement
    # check, the resident P0 inbox's newest-committed check) must be found,
    # or the scan below proves nothing.
    assert len(newest_p0) >= 2, reads
    offenders = []
    for path, line, sql in reads:
        plan = " | ".join(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN " + sql, ("x",) * sql.count("?")
            )
        )
        if RECENCY in plan:
            offenders.append(f"{path}:{line}: {plan}")
    assert offenders == []


def test_the_unary_plus_changes_the_path_not_the_row(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    apply_production_ssot_schema(conn)
    row = (
        "INSERT INTO basket_probability_envelope VALUES("
        "?,'P0',?,'ZUUU',?,'Asia/Shanghai',?,?,'{}','{}','v',?,?,'{}',?,"
        "'COMMITTED','2026-09-21T00:00:00Z')"
    )
    n = 0
    for day in ("2026-09-21", "2026-09-22", "2026-09-23"):
        for hour in range(0, 24, 3):
            n += 1
            ckpt = f"p0-{n}"
            conn.execute(row, (
                ckpt, ckpt, day, f"2026-09-20T{hour:02d}:00:00Z",
                f"2026-09-20T{hour:02d}:30:00Z",
                "a" * 64, "b" * 64, f"{n:064d}",
            ))
    old = (
        "SELECT checkpoint_id FROM basket_probability_envelope "
        "WHERE station_id=? AND event_date=? AND stage='P0' "
        "AND state='COMMITTED' ORDER BY decision_at_utc DESC LIMIT 1"
    )
    new = old.replace("AND stage='P0'", "AND +stage='P0'")
    for day in ("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"):
        assert (
            conn.execute(old, ("ZUUU", day)).fetchone()
            == conn.execute(new, ("ZUUU", day)).fetchone()
        )


# --- the P1 consume-once keys ------------------------------------------------

def _raw_p1(conn, n: int, *, event_id: str, root: str = "root") -> None:
    lineage = json.dumps({
        "canonical_observation_event_id": event_id,
        "p0_root_checkpoint_id": "p0-root",
        "prediction_root_checkpoint_id": root,
        "prediction_root_content_sha256": "C" * 64,
        "padding": "x" * 7000,
    })
    ckpt = f"p1-{n}"
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES("
        "?,'P1',?,'VILK','2026-09-20','Asia/Kolkata',?,?,'{}','{}','v',?,?,?,?,"
        "'COMMITTED','2026-09-20T00:00:00Z')",
        (
            ckpt, ckpt, f"2026-09-20T{n // 60:02d}:{n % 60:02d}:00Z",
            f"2026-09-20T{n // 60:02d}:{n % 60:02d}:30Z",
            "a" * 64, "b" * 64, lineage, f"{n:064d}",
        ),
    )


def _unfiltered(conn):
    return [
        (mat._p1_consume_key(lineage_json), str(content))
        for lineage_json, content in conn.execute(
            "SELECT lineage_json,content_sha256 FROM basket_probability_envelope "
            "WHERE station_id=? AND event_date=? AND stage='P1'",
            ("VILK", "2026-09-20"),
        )
    ]


def _keys(conn):
    return mat._prior_p1_consume_keys(
        conn, station_id="VILK", event_date="2026-09-20"
    )


def test_keys_equal_the_unfiltered_read_and_only_new_rows_are_parsed(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    apply_production_ssot_schema(conn)
    for n in range(40):
        _raw_p1(conn, n, event_id=f"obs-{n % 7}")
    conn.commit()
    assert _keys(conn) == _unfiltered(conn)

    _raw_p1(conn, 40, event_id="obs-new")
    conn.commit()
    statements = _trace(conn)
    keys = _keys(conn)
    conn.set_trace_callback(None)
    assert keys == _unfiltered(conn)
    lineage_reads = [" ".join(s.split()) for s in statements if "lineage_json" in s]
    assert len(lineage_reads) == 1 and lineage_reads[0].endswith("rowid IN (41)")

    statements = _trace(conn)
    assert _keys(conn) == keys
    assert not [s for s in statements if "lineage_json" in s]


def test_a_rolled_back_row_does_not_stand_in_for_its_rowid_successor(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "ssot.db", isolation_level=None)
    apply_production_ssot_schema(conn)
    _raw_p1(conn, 0, event_id="obs-0")
    conn.execute("BEGIN")
    _raw_p1(conn, 1, event_id="obs-rolled-back")
    assert [key[0] for key, _ in _keys(conn)] == ["obs-0", "obs-rolled-back"]
    conn.execute("ROLLBACK")
    _raw_p1(conn, 2, event_id="obs-kept")  # takes the freed rowid
    assert [key[0] for key, _ in _keys(conn)] == ["obs-0", "obs-kept"]
    assert _keys(conn) == _unfiltered(conn)


def test_an_in_memory_store_is_not_remembered() -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    _raw_p1(conn, 0, event_id="obs-0")
    assert _keys(conn) == _unfiltered(conn)
    assert not mat._P1_CONSUME_KEYS


def test_consume_once_on_a_file_store_still_refuses_a_second_answer(tmp_path) -> None:
    p0 = _envelope()
    p0m = _p0m_root(p0)
    p1 = ProbabilityEnvelope.from_mapping(_trajectory_packet(p0m)["expected_envelope"])
    altered = ProbabilityEnvelope.from_mapping(
        _trajectory_packet(
            p0m, temperature_c=39.0, decision_at_utc="2026-07-23T05:35:00Z"
        )["expected_envelope"]
    )
    conn = sqlite3.connect(tmp_path / "ssot.db")
    conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(conn)
    insert_probability_envelope(conn, p0, created_at_utc="2026-07-23T04:01:00Z")
    insert_probability_envelope(conn, p0m, created_at_utc="2026-07-23T05:00:00Z")
    assert insert_probability_envelope(
        conn, p1, created_at_utc="2026-07-23T05:01:00Z"
    )["wrote"] is True
    conn.commit()
    with pytest.raises(ContractError, match="already_consumed"):
        insert_probability_envelope(conn, altered, created_at_utc="2026-07-23T05:06:00Z")
    conn.rollback()
    assert mat._P1_CONSUME_KEYS  # remembered on a file store
    statements = _trace(conn)
    with pytest.raises(ContractError, match="already_consumed"):
        insert_probability_envelope(conn, altered, created_at_utc="2026-07-23T05:07:00Z")
    conn.rollback()
    assert not [
        s for s in statements
        if re.search(r"SELECT checkpoint_id,content_sha256,lineage_json", s)
    ]


# --- the OOS holdout's conditioning reads ----------------------------------------

def test_the_holdout_reads_no_climatology_or_truth_record_for_the_deleted_skew(tmp_path) -> None:
    """Charter 43 amendment (2026-09-22): the holdout's same-day climatology
    window (charter 179's calendar-index read) and the station's whole truth
    record fed only the conditional tail skew's headroom, which is deleted.
    The shift signal the shape stack scores is unchanged and still read."""

    from datetime import date, timedelta

    from bayes_basket_target import native_ensemble_oos as OOS
    from tests.test_radiation_asof import _holdout_db, _predictions

    conn = _holdout_db(tmp_path, day_value=19.0, day_captured="2026-09-17T18:00:00Z")
    conn.execute(
        "CREATE TABLE calibration_station_same_day_climatology ("
        " station_id TEXT NOT NULL, local_date TEXT NOT NULL,"
        " daily_high_c REAL NOT NULL, source_id TEXT NOT NULL,"
        " reference_only INTEGER NOT NULL CHECK (reference_only = 1),"
        " PRIMARY KEY (station_id, local_date, source_id))"
    )
    start = date(2016, 1, 1)
    conn.executemany(
        "INSERT INTO calibration_station_same_day_climatology VALUES(?,?,?,?,1)",
        [
            ("ZUUU", (start + timedelta(days=i)).isoformat(),
             20.0 + ((i * 37) % 23) * 0.5, "era5")
            for i in range(3650)
        ],
    )
    conn.commit()
    predictions = _predictions({"NOAA_GEFS_05": 24.0, "ECMWF_IFS_ENS_OPEN_PF50": 26.0})
    statements = _trace(conn)
    signals = OOS._holdout_conditioning_signals(
        conn, station_id="ZUUU", holdout_days=["2026-09-19"],
        predictions_by_system=predictions, radiation_shift_sigma=0.15,
    )["2026-09-19"]
    conn.set_trace_callback(None)

    assert set(signals) == {"synoptic_cooling_shift_sigma", "radiation_as_of_state"}
    assert signals["synoptic_cooling_shift_sigma"] is not None
    assert not [s for s in statements if "same_day_climatology" in s]
    assert not [s for s in statements if "calibration_station_daily_high_truth" in s]


# --- the observed-high registry read ---------------------------------------------

def test_the_scope_lane_read_is_answered_from_the_index_alone(tmp_path) -> None:
    from bayes_basket_target import market_registry as mr

    conn = sqlite3.connect(tmp_path / "ssot.db")
    apply_production_ssot_schema(conn)
    mr.apply_target_market_registry_schema(conn)
    statements = _trace(conn)
    mr.registry_scope_timezone_lanes(conn, station_id="ZUUU", event_date="2026-09-21")
    conn.set_trace_callback(None)
    reads = [
        s for s in statements
        if "source_lane" in s and "FROM basket_target_market_registry" in s
    ]
    assert reads
    for sql in reads:
        plan = " | ".join(
            str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql)
        )
        assert "COVERING INDEX basket_target_market_registry_scope_lane_idx" in plan
