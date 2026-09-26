"""The judge looks where the shape could have stepped, not only where code moved.

A fingerprint covers two kinds of code.  A change on the serving path moves
what is served when it lands.  A change inside a fitting lane moves nothing
then: it moves a number when that lane next runs and activates what it fitted,
which is a day later on the daily sweep.

Until 2026-09-10 the lanes were not fingerprint roots at all, so the question
never arose.  Now that they are -- a change to what a lane will store is a
change to what the stage serves, one activation later -- a judge that only
measures across the code moment would call every lane change inert, and the
lamp would go out on the strength of a step nobody had taken.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "acknowledge_serving_arithmetic_lane",
    ROOT / "scripts" / "acknowledge_serving_arithmetic.py",
)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

SINCE = "2026-09-10T09:00:00Z"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_p0_preselection_plan ("
        " station_id TEXT, event_date TEXT, created_at TEXT, plan_json TEXT)"
    )
    for table, journal in audit.LANE_TABLES.values():
        conn.execute(
            f"CREATE TABLE {table} (stage TEXT, activated INTEGER,"
            " activated_at_utc TEXT)"
        )
        conn.execute(
            f"CREATE TABLE {journal} (stage TEXT, fitted_at_utc TEXT,"
            " outcome TEXT)"
        )
    return conn


def _shape(conn, *, day: int, hour_from: int, hour_to: int, sigma: float,
           centre: float, stage: str = "P0") -> None:
    payload = {
        "compiled_plan": {
            "stage": stage,
            "plan": {"skew_reading": {
                "sigma_buckets": sigma, "mean_ordinal": centre,
            }},
        }
    }
    for hour in range(hour_from, hour_to):
        for minute in range(0, 60, 5):
            conn.execute(
                "INSERT INTO basket_p0_preselection_plan VALUES(?,?,?,?)",
                ("X", "2026-09-10",
                 f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00Z",
                 json.dumps(payload)),
            )


def _lane(conn, lane: str, *, fitted: str | None, activated: str | None) -> None:
    table, journal = audit.LANE_TABLES[lane]
    if fitted:
        conn.execute(
            f"INSERT INTO {journal} VALUES(?,?,?)",
            ("P0", fitted, "ACTIVATED" if activated else "UNCHANGED"),
        )
    if activated:
        conn.execute(
            f"INSERT INTO {table} VALUES(?,?,?)", ("P0", 1, activated)
        )


def _all_lanes_settled(conn, *, fitted: str = "2026-09-10T10:00:00Z") -> None:
    for lane in audit.LANE_TABLES:
        _lane(conn, lane, fitted=fitted, activated=None)


def test_a_lane_that_has_not_refitted_yields_no_verdict() -> None:
    """The state that used to read as inert, and is the whole point."""

    conn = _conn()
    _shape(conn, day=9, hour_from=21, hour_to=24, sigma=1.0, centre=10.0)
    _shape(conn, day=10, hour_from=9, hour_to=12, sigma=1.0, centre=10.0)
    _lane(conn, "station width", fitted=None, activated=None)
    verdict = audit._shape_verdict(conn, stage="P0", since=SINCE)
    assert verdict["state"] == "pending"
    assert verdict["unfitted"] == sorted(audit.LANE_TABLES)
    # Charter 232: a retired lane is not one a verdict waits for.
    assert "band reliability" not in audit.LANE_TABLES


def test_a_lane_that_refused_its_candidate_took_no_step() -> None:
    """Fitted and refused is settled, not pending: it changed nothing."""

    conn = _conn()
    _shape(conn, day=9, hour_from=21, hour_to=24, sigma=1.0, centre=10.0)
    _shape(conn, day=10, hour_from=9, hour_to=12, sigma=1.0, centre=10.0)
    _all_lanes_settled(conn)
    verdict = audit._shape_verdict(conn, stage="P0", since=SINCE)
    assert verdict["state"] == "inert"
    assert verdict["label"] == "代码落地"


def test_the_step_is_judged_where_the_lane_activated() -> None:
    """Flat across the code moment, a step at the activation a day later."""

    conn = _conn()
    # Both sides of the code moment are the same shape: inert if that is the
    # only place anyone looks.
    _shape(conn, day=9, hour_from=21, hour_to=24, sigma=1.0, centre=10.0)
    _shape(conn, day=10, hour_from=9, hour_to=12, sigma=1.0, centre=10.0)
    # The lane activates the next morning and the width halves.
    _shape(conn, day=11, hour_from=0, hour_to=8, sigma=1.0, centre=10.0)
    _shape(conn, day=11, hour_from=9, hour_to=17, sigma=0.5, centre=10.0)
    _all_lanes_settled(conn)
    _lane(conn, "station width", fitted="2026-09-11T08:55:00Z",
          activated="2026-09-11T09:00:00Z")
    at_code = audit._shape_shift(conn, stage="P0", since=SINCE)
    assert at_code is not None and at_code["moved"] is False

    verdict = audit._shape_verdict(conn, stage="P0", since=SINCE)
    assert verdict["state"] == "moved"
    assert verdict["label"] == "station width"
    assert verdict["at"] == "2026-09-11T09:00:00Z"


def test_one_fingerprint_bundling_inert_and_real_owes_one_bump() -> None:
    conn = _conn()
    _shape(conn, day=9, hour_from=21, hour_to=24, sigma=1.0, centre=10.0)
    _shape(conn, day=10, hour_from=9, hour_to=12, sigma=1.0, centre=10.0)
    _shape(conn, day=11, hour_from=0, hour_to=8, sigma=1.0, centre=10.0)
    _shape(conn, day=11, hour_from=9, hour_to=17, sigma=1.0, centre=10.6)
    _all_lanes_settled(conn)
    _lane(conn, "centre/width scalars", fitted="2026-09-11T08:55:00Z",
          activated="2026-09-11T09:00:00Z")
    verdict = audit._shape_verdict(conn, stage="P0", since=SINCE)
    assert verdict["state"] == "moved"
    assert verdict["shift"]["centre_shift"] > audit.SHAPE_SHIFT_CENTRE_BUCKETS


def test_a_store_without_the_lane_tables_is_not_a_lane_that_owes_a_step() -> None:
    """Fixtures and replays carry a narrower schema than production."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_p0_preselection_plan ("
        " station_id TEXT, event_date TEXT, created_at TEXT, plan_json TEXT)"
    )
    _shape(conn, day=9, hour_from=21, hour_to=24, sigma=1.0, centre=10.0)
    _shape(conn, day=10, hour_from=9, hour_to=12, sigma=1.0, centre=10.0)
    steps = audit._lane_steps(conn, stage="P0", since=SINCE)
    assert steps == {"activations": [], "unfitted": []}
    assert audit._shape_verdict(conn, stage="P0", since=SINCE)["state"] == "inert"


def test_a_pending_lane_is_not_acknowledged(tmp_path, capsys) -> None:
    """The lamp must not go out on a step nobody has taken.

    End to end through ``--audit``, because the auto-acknowledgement is what
    the state exists to withhold.
    """

    db = tmp_path / "ssot.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE basket_p0_preselection_plan ("
        " station_id TEXT, event_date TEXT, created_at TEXT, plan_json TEXT)"
    )
    for table, journal in audit.LANE_TABLES.values():
        conn.execute(
            f"CREATE TABLE {table} (stage TEXT, activated INTEGER,"
            " activated_at_utc TEXT)"
        )
        conn.execute(
            f"CREATE TABLE {journal} (stage TEXT, fitted_at_utc TEXT,"
            " outcome TEXT)"
        )
    _shape(conn, day=9, hour_from=21, hour_to=24, sigma=1.0, centre=10.0)
    _shape(conn, day=10, hour_from=9, hour_to=12, sigma=1.0, centre=10.0)
    audit.serving_arithmetic.ensure_schema(conn)
    conn.commit()
    conn.close()

    original = audit.serving_arithmetic.watchdog

    def reading(_conn, *, stage):
        return {
            "stage": stage, "fingerprint": "a" * 64,
            "fingerprint_since_utc": SINCE,
            "served_version": "v_test", "discrepancy": stage == "P0",
            "acknowledged": None,
        }

    audit.serving_arithmetic.watchdog = reading
    try:
        assert audit._audit(db) == 0
    finally:
        audit.serving_arithmetic.watchdog = original
    out = capsys.readouterr().out
    assert "还没重拟过" in out
    assert "自动记为惰性" not in out
    check = sqlite3.connect(str(db))
    try:
        rows = check.execute(
            "SELECT COUNT(*) FROM calibration_serving_arithmetic_ack"
        ).fetchone()[0]
    finally:
        check.close()
    assert rows == 0


def test_an_acknowledgement_can_be_withdrawn() -> None:
    """A judgement made on a partial reading has to be retractable.

    The first row this table ever held was exactly that: judged inert across
    the moment the code landed, when what had changed were two fitting lanes
    whose numbers do not reach serving until those lanes next activate.
    """

    from bayes_basket_target import serving_arithmetic as sa

    conn = sqlite3.connect(":memory:")
    sa.ensure_schema(conn)
    sa.acknowledge(
        conn, stage="P0", reason="judged too early",
        fingerprint_sha256="b" * 64,
    )
    assert sa.acknowledged(conn, stage="P0", fingerprint_sha256="b" * 64)
    receipt = sa.withdraw(conn, stage="P0", fingerprint_sha256="b" * 64)
    assert receipt["withdrawn"] == 1
    assert sa.acknowledged(conn, stage="P0", fingerprint_sha256="b" * 64) is None
    # Withdrawing what is not there is a no-op that says so, not an error.
    assert sa.withdraw(
        conn, stage="P0", fingerprint_sha256="b" * 64
    )["withdrawn"] == 0


def test_withdrawing_still_demands_a_reason() -> None:
    import subprocess

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "acknowledge_serving_arithmetic.py"),
         "--stage", "P0", "--withdraw"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "--reason is required to withdraw" in result.stderr
