"""Charter 237 补记: OOS predictions superseded by a newer activation generation leave.

User 2026-10-19 「把 OOS 预测里被更新代次取代、且不是当前在役组合的行迁到外置盘冷层」.
Per key and event day only the newest generation's row stays (the training-fact
reader skips the rest); a day only an older generation predicts keeps its row;
the artifact a scope serves now keeps every row whatever its generation.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import ssot_cold_offload as off  # noqa: E402

TABLE = "calibration_cross_native_ensemble_oos_prediction"
AS_OF = datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc)


def _store(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    conn.execute(
        f"CREATE TABLE {TABLE}(prediction_id TEXT PRIMARY KEY, cross_ensemble_artifact_id TEXT, station_id TEXT,"
        " event_date TEXT, settlement_timezone TEXT, season_bin TEXT, lead_bucket TEXT, run_cycle TEXT,"
        " settlement_definition TEXT, decision_at_utc TEXT, ensemble_system_ids_json TEXT, probabilities_json TEXT)"
    )
    conn.execute(f"CREATE INDEX p_scope ON {TABLE}(station_id, event_date)")
    conn.execute(f"CREATE INDEX p_day ON {TABLE}(event_date)")
    conn.execute(
        "CREATE TABLE calibration_native_ensemble_activation(artifact_kind TEXT, artifact_id TEXT, station_id TEXT,"
        " ensemble_system_id_or_star TEXT, season_bin TEXT, lead_bucket TEXT, run_cycle TEXT,"
        " available_system_set_hash_or_star TEXT, generation INTEGER, valid_from_utc TEXT, valid_until_utc TEXT)"
    )
    return conn


def _activate(conn, artifact, generation, *, until="2026-11-18T00:00:00Z"):
    conn.execute(
        "INSERT INTO calibration_native_ensemble_activation VALUES"
        " ('CROSS_NATIVE_ENSEMBLE',?,'ZUUU','*','autumn','h24_48','00Z','*',?,'2026-09-01T00:00:00Z',?)",
        (artifact, generation, until),
    )


def _predict(conn, pid, artifact, day):
    conn.execute(
        f"INSERT INTO {TABLE} VALUES (?,?,'ZUUU',?,'Asia/Shanghai','autumn','h24_48','00Z','metar',?,'[\"A\"]','{{}}')",
        (pid, artifact, day, f"{day}T00:00:00Z"),
    )


def _leaving(conn) -> set[str]:
    plan = off.plan_table(conn, off.POLICY_BY_TABLE[TABLE], as_of=AS_OF, holds={}, row_days_cache=None)
    ids = set()
    for rowid in plan.candidates:
        ids.add(conn.execute(f"SELECT prediction_id FROM {TABLE} WHERE rowid=?", (rowid,)).fetchone()[0])
    return ids


def test_only_rows_a_newer_generation_repredicts_leave(tmp_path):
    conn = _store(tmp_path)
    _activate(conn, "g1", 1)
    _activate(conn, "g2", 2)
    _activate(conn, "g3", 3)
    # 10-01: all three generations; 10-02: g1 and g2; 10-03: only g1.
    for pid, art, day in (("a1", "g1", "2026-10-01"), ("a2", "g2", "2026-10-01"), ("a3", "g3", "2026-10-01"),
                          ("b1", "g1", "2026-10-02"), ("b2", "g2", "2026-10-02"), ("c1", "g1", "2026-10-03")):
        _predict(conn, pid, art, day)
    conn.commit()
    # g3 serves now; a1, a2 and b1 are superseded; the newest row of each day stays.
    assert _leaving(conn) == {"a1", "a2", "b1"}


def test_the_serving_artifact_keeps_every_row(tmp_path):
    conn = _store(tmp_path)
    _activate(conn, "g1", 1)
    # g2 is newer but its activation has ended: the scope serves g1 now.
    _activate(conn, "g2", 2, until="2026-10-10T00:00:00Z")
    _predict(conn, "a1", "g1", "2026-10-01")
    _predict(conn, "a2", "g2", "2026-10-01")
    conn.commit()
    assert _leaving(conn) == set()


def test_the_rule_reads_the_training_fact_readers_key_and_generation():
    rule = off.POLICY_BY_TABLE[TABLE].move_superseded
    assert rule.order[0] == "generation" and "generation" in rule.computed
    from bayes_basket_target import native_ensemble_training_facts as tf
    import inspect
    source = inspect.getsource(tf)
    assert "ORDER BY p.event_date,a.generation DESC" in source
    for column in rule.key:
        assert f"p.{column}=?" in source or column == "ensemble_system_ids_json" and "p.ensemble_system_ids_json=?" in source
