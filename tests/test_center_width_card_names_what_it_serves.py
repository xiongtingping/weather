"""What the centre/width card must say out loud.

Two reports on 2026-09-12, both of the same shape: a fact the page had and
printed as an absence.

P1's card read "中心 δ —  宽度 λ —" and was read as no data.  P1 has no
activated row, and no activated row is not missing data: the runtime serves
the identity pair, which is a served decision.  It is also, right now, the
right one -- P1's served width measures 9% too narrow, and its only fitted
row (0.825, 2026-08-16) would narrow it further -- so "—" hid a working
state behind what looked like a hole.

The other is the wait.  Every stage carries a day count and a threshold in
one muted line and the reader had to subtract them.  For a stage serving a
stale scalar that difference is the card's most actionable number: P0 serves
a width fitted three days before the climate blend came out from under it,
and the only thing between that and a clean refit is the day count.

The watchdog test belongs with them.  It returned early once a fingerprint
was acknowledged as inert and never named the served version, so the line
directly above the countdown printed "边界取服务版本 —" for the boundary the
countdown counts to.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target import serving_arithmetic as sa  # noqa: E402
from bayes_basket_target.center_width_store import (  # noqa: E402
    MIN_QUALIFYING_DAYS,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE calibration_center_width_table ("
        "  stage TEXT, delta_c REAL, width_lambda REAL, evidence_json TEXT,"
        "  fitted_at_utc TEXT, serving_day_count INTEGER, activated INTEGER,"
        "  center_width_sha256 TEXT);"
        "CREATE TABLE calibration_center_width_fit_journal ("
        "  stage TEXT, fitted_at_utc TEXT, outcome TEXT, delta_c REAL,"
        "  width_lambda REAL, serving_day_count INTEGER,"
        "  serving_cell_count INTEGER, forward_nll_gain REAL,"
        "  evidence_json TEXT);"
    )
    return conn


def _journal(conn, stage, *, days, outcome="ACCUMULATING"):
    conn.execute(
        "INSERT INTO calibration_center_width_fit_journal"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (stage, "2026-09-12T00:00:00Z", outcome, 0.0, 1.0, days, days * 90,
         0.0, "{}"),
    )


def _row(rows, stage):
    return next(r for r in rows if r["stage"] == stage)


def test_no_activated_row_is_the_identity_pair_not_a_hole():
    conn = _conn()
    conn.execute(
        "INSERT INTO calibration_center_width_table"
        " VALUES('P0',0.0,0.75,'{}','2026-09-03T00:00:00Z',8,1,'sha')"
    )
    for stage in ("P0", "P0-M", "P1"):
        _journal(conn, stage, days=2)
    rows = C.collect_center_width(conn)
    p1 = _row(rows, "P1")
    assert p1["served_identity"] is True
    assert p1["delta_c"] == 0.0
    assert p1["width_lambda"] == 1.0
    p0 = _row(rows, "P0")
    assert p0["served_identity"] is False
    assert p0["width_lambda"] == 0.75


def test_the_wait_is_a_number_the_card_carries():
    conn = _conn()
    _journal(conn, "P0", days=2)
    _journal(conn, "P0-M", days=MIN_QUALIFYING_DAYS)
    _journal(conn, "P1", days=MIN_QUALIFYING_DAYS + 3)
    rows = C.collect_center_width(conn)
    assert _row(rows, "P0")["days_remaining"] == MIN_QUALIFYING_DAYS - 2
    assert _row(rows, "P0")["earliest_refit_date"] == (
        date.today() + timedelta(days=MIN_QUALIFYING_DAYS - 2)
    ).isoformat()
    # At or past the threshold there is nothing to count down to, and no
    # date may be invented for it.
    for stage in ("P0-M", "P1"):
        assert _row(rows, stage)["days_remaining"] == 0
        assert _row(rows, stage)["earliest_refit_date"] is None


def test_an_acknowledged_fingerprint_still_names_its_boundary(monkeypatch):
    monkeypatch.setattr(sa, "fingerprint", lambda stage: "a" * 64)
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_probability_bridge_history ("
        " stage TEXT, state TEXT, decision_at_utc TEXT, checkpoint_id TEXT);"
        "CREATE TABLE basket_probability_envelope ("
        " envelope_id TEXT, posterior_version TEXT);"
    )
    conn.execute(
        "INSERT INTO basket_probability_bridge_history"
        " VALUES('P0','COMMITTED','2026-09-11T12:00:00Z','env0')"
    )
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES('env0','served_v4')"
    )
    sa.ensure_schema(conn)
    conn.execute(
        "INSERT INTO calibration_serving_arithmetic VALUES(?,?,?)",
        ("P0", "a" * 64, "2026-09-10T09:00:00Z"),
    )
    conn.execute(
        "INSERT INTO calibration_serving_arithmetic_ack"
        " VALUES('P0',?, '2026-09-11T01:30:00Z','receipt only')",
        ("a" * 64,),
    )
    conn.commit()
    reading = sa.watchdog(conn, stage="P0")
    assert reading["acknowledged"] is not None
    assert reading["discrepancy"] is False
    # The lamp is out and the boundary is still named.
    assert reading["served_version"] == "served_v4"
