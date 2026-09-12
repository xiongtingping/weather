"""Charter 97's retirement trigger, read off the lane that has the sample.

The clause retires its own axis once the seats it opened clear the hurdle
and realise under one, and it pointed at the claimed-edge thermometer for
that reading.  The thermometer's thin bands (``e <= 0.05``, which is its
0-2% and 2-5% rows) hold 41 posted legs in the re-derived book -- under the
section's ten-settled-leg floor, and not closing at the book's current
rate.  The 2026-09-12 ruling moved the reading onto the stamped candidate
windows, where the same question has hundreds of station-days behind it.

What is pinned here is not the number but the three things that make the
number mean what it says: the unit is the station-day (cycles and stages
are repeats of one settled high, not independent draws), the verdict needs
an interval rather than a point estimate, and a sample under the day floor
says "样本不足" instead of guessing.  Those last two are the 2026-09-11
lambda-guard finding written into a reader.

The fixture tables carry the columns the reader touches, in this file's
prevailing style for audit readers; the shape under test is the pooling,
not the DDL.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

INTERVALS = json.dumps({
    "14°C or below": [None, 14.5],
    "15°C": [14.5, 15.5],
    "16°C": [15.5, 16.5],
    "17°C": [16.5, 17.5],
    "18°C or higher": [17.5, None],
})


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_policy_training_fact ("
        "  fact_id TEXT PRIMARY KEY, station_id TEXT NOT NULL,"
        "  event_date TEXT NOT NULL, stage TEXT NOT NULL,"
        "  decision_at_utc TEXT NOT NULL,"
        # The reader takes the lead off the station's own settlement day,
        # so the timezone is part of what it reads.
        "  settlement_timezone TEXT NOT NULL, feature_json TEXT NOT NULL);"
        "CREATE TABLE calibration_station_daily_high_truth ("
        "  station_id TEXT NOT NULL, local_date TEXT NOT NULL,"
        "  official_tmax_c REAL, qc_status TEXT NOT NULL);"
    )
    return conn


def _day(back: int) -> str:
    return (date.today() - timedelta(days=back)).isoformat()


def _seat(conn, station, day, *, source, buckets, claim, high,
          stage="P0", cycle=0, qc="ACCEPTED"):
    conn.execute(
        "INSERT OR IGNORE INTO calibration_station_daily_high_truth"
        " VALUES(?,?,?,?)", (station, day, high, qc)
    )
    conn.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?,?,?)",
        (
            f"{station}:{day}:{stage}:{cycle}", station, day, stage,
            f"{day}T{cycle:02d}:00:00Z", "UTC",
            json.dumps({
                "bucket_intervals_c_json": INTERVALS,
                "plan": {"basket_support_selection": {"seated": {
                    "effective_edge_lambda_source": source,
                    "buckets": list(buckets),
                    "win_probability": claim,
                    "effective_edge_lambda_source_note": "fixture",
                }}},
            }),
        ),
    )


def _axis(out, source):
    return next(a for a in out["axes"] if a["source"] == source)


def _fill(conn, *, station_days, hit_share, claim, source="agreement_band",
          first=1):
    """``station_days`` days, ``hit_share`` of them landing in the window."""

    hits = round(station_days * hit_share)
    for i in range(station_days):
        inside = i < hits
        _seat(
            conn, f"S{i:03d}", _day(first + i % 40), source=source,
            buckets=["16°C", "17°C"], claim=claim,
            high=16.0 if inside else 19.0, stage="P0", cycle=1,
        )


def test_the_unit_is_the_station_day_not_the_cycle():
    """Ten cycles of one day are one day; only the last one is read."""

    conn = _conn()
    for cycle in range(10):
        _seat(conn, "EDDM", _day(3), source="claim_edge_thin_band",
              buckets=["16°C", "17°C"], claim=0.8, high=16.0, cycle=cycle)
    out = C.collect_lambda_axis_realisation(conn)
    axis = _axis(out, "claim_edge_thin_band")
    assert out["station_days"] == 1
    assert axis["standing_days"] == 1
    # A one-day sample cannot be tightened by re-reading the same day.
    assert axis["realised_lo"] < 0.9 < axis["realised_hi"]
    assert axis["verdict"] == "样本不足"


def test_the_three_stages_of_one_day_settle_against_one_high():
    """Three stages of one day are one draw, not three."""

    conn = _conn()
    for stage in ("P0", "P0-M", "P1"):
        _seat(conn, "EDDM", _day(3), source="claim_edge_thin_band",
              buckets=["16°C"], claim=0.5, high=16.0, stage=stage, cycle=1)
    out = C.collect_lambda_axis_realisation(conn)
    assert out["station_days"] == 1
    assert _axis(out, "claim_edge_thin_band")["standing_days"] == 1


def test_a_day_that_changed_axis_belongs_to_the_one_that_closed_it():
    """The stale mid-day snapshot is not a seat that settled."""

    conn = _conn()
    # The thin band held the morning on a window that would have won; the
    # agreement band held the close on one that lost.  Reading each axis's
    # own last decision would score the thin band on the morning snapshot.
    _seat(conn, "EDDM", _day(3), source="claim_edge_thin_band",
          buckets=["16°C"], claim=0.5, high=16.0, cycle=1)
    _seat(conn, "EDDM", _day(3), source="agreement_band",
          buckets=["15°C"], claim=0.5, high=16.0, cycle=9)
    out = C.collect_lambda_axis_realisation(conn)
    assert out["mixed_axis_days"] == 1
    assert _axis(out, "agreement_band")["standing_days"] == 1
    assert _axis(out, "agreement_band")["hit_rate"] == 0.0
    assert _axis(out, "claim_edge_thin_band")["standing_days"] == 0
    assert _axis(out, "claim_edge_thin_band")["realised_x"] is None


def test_the_paired_column_only_counts_days_both_axes_served():
    """A one-axis day carries no comparison and must not enter one."""

    conn = _conn()
    _seat(conn, "EDDM", _day(3), source="claim_edge_thin_band",
          buckets=["16°C"], claim=0.5, high=16.0, cycle=1)
    _seat(conn, "EDDM", _day(3), source="agreement_band",
          buckets=["15°C"], claim=0.5, high=16.0, cycle=9)
    _seat(conn, "EGLL", _day(4), source="agreement_band",
          buckets=["16°C"], claim=0.5, high=16.0, cycle=1)
    out = C.collect_lambda_axis_realisation(conn)
    for source in ("agreement_band", "claim_edge_thin_band"):
        assert _axis(out, source)["paired_days"] == 1
    # Same day, same weather, and the two axes still differ: that is the
    # only comparison between axes this reader claims to support.
    assert _axis(out, "claim_edge_thin_band")["paired_hit_rate"] == 1.0
    assert _axis(out, "agreement_band")["paired_hit_rate"] == 0.0


def test_the_last_decision_of_the_day_is_the_seat():
    """A window the day moved off is not the seat that stood."""

    conn = _conn()
    _seat(conn, "EDDM", _day(3), source="claim_edge_thin_band",
          buckets=["15°C"], claim=0.5, high=16.0, cycle=1)
    _seat(conn, "EDDM", _day(3), source="claim_edge_thin_band",
          buckets=["16°C"], claim=0.5, high=16.0, cycle=9)
    axis = _axis(C.collect_lambda_axis_realisation(conn),
                 "claim_edge_thin_band")
    assert axis["standing_days"] == 1
    assert axis["hit_rate"] == 1.0


def test_an_open_ended_bucket_is_where_the_truth_can_land():
    """The mapping comes from the fact's own intervals, both ends open."""

    conn = _conn()
    _seat(conn, "EDDM", _day(3), source="agreement_band",
          buckets=["18°C or higher"], claim=0.5, high=30.0, cycle=1)
    _seat(conn, "EGLL", _day(4), source="agreement_band",
          buckets=["14°C or below"], claim=0.5, high=-3.0, cycle=1)
    axis = _axis(C.collect_lambda_axis_realisation(conn), "agreement_band")
    assert axis["standing_days"] == 2
    assert axis["hit_rate"] == 1.0


def test_under_the_day_floor_the_verdict_is_not_a_guess():
    """Twenty-nine days of a dead axis still says 样本不足, not 跌破."""

    conn = _conn()
    _fill(conn, station_days=29, hit_share=0.0, claim=0.9)
    axis = _axis(C.collect_lambda_axis_realisation(conn), "agreement_band")
    assert axis["standing_days"] == 29
    assert axis["realised_x"] == 0.0
    assert axis["verdict"] == "样本不足"


def test_the_trigger_is_the_interval_and_not_the_point_estimate():
    """A ratio under one that the sample cannot separate from one."""

    conn = _conn()
    _fill(conn, station_days=40, hit_share=0.85, claim=0.9)
    axis = _axis(C.collect_lambda_axis_realisation(conn), "agreement_band")
    assert axis["standing_days"] == 40
    assert axis["realised_x"] < 1.0
    assert axis["realised_hi"] > 1.0
    assert axis["verdict"] == "未分出"


def test_the_trigger_fires_when_the_interval_clears_the_line():
    conn = _conn()
    _fill(conn, station_days=200, hit_share=0.6, claim=0.9)
    axis = _axis(C.collect_lambda_axis_realisation(conn), "agreement_band")
    assert axis["realised_hi"] < 1.0
    assert axis["verdict"] == "跌破"


def test_an_axis_that_beats_its_claim_reads_达标():
    conn = _conn()
    _fill(conn, station_days=200, hit_share=0.95, claim=0.6)
    axis = _axis(C.collect_lambda_axis_realisation(conn), "agreement_band")
    assert axis["realised_lo"] > 1.0
    assert axis["verdict"] == "达标"


def test_a_quarantined_high_is_not_truth():
    conn = _conn()
    _seat(conn, "EDDM", _day(3), source="agreement_band", buckets=["16°C"],
          claim=0.5, high=16.0, cycle=1, qc="QUARANTINED")
    out = C.collect_lambda_axis_realisation(conn)
    assert out["available"] is True
    assert out["axes"] == []
