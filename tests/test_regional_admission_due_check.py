"""A question that becomes answerable in a week is one nobody remembers to ask.

Two of the four regional lanes have never been measured -- ICON-EU and CMA
GRAPES keep no history and accumulate forward only, so on 2026-09-07 they
carried seven and eight settled station-days against the forty-six the MSM was
admitted on.  That is an empty docket, not a verdict, and the difference has to
survive into whatever reads the receipt later.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import regional_admission_due_check as D  # noqa: E402


def test_an_empty_docket_is_not_a_refusal() -> None:
    """The bug this test exists for, caught on the first live run.

    The gate says "nothing to measure" two different ways -- per system when
    other systems had rows, and once for the whole run when none did -- and
    reading only the first recorded CMA GRAPES as REFUSED on a day it simply
    had not accumulated yet.  A false verdict is worse here than no verdict,
    because the receipt's only purpose is to be believed later.
    """

    per_system = "  CMA_GRAPES_GLOBAL_POINT: 无可比前缀 {}"
    whole_run = "没有可比前缀：该区间内没有站日同时具备车道 run、结算真值与 TOO_EARLY 回执。"
    assert D.classify_verdict(per_system, 0) == "NO_COMPARABLE_PREFIX"
    assert D.classify_verdict(whole_run, 1) == "NO_COMPARABLE_PREFIX"
    # Even if the wording changes, a non-zero exit is never a verdict.
    assert D.classify_verdict("something else entirely", 1) == "NO_COMPARABLE_PREFIX"


def test_the_two_real_verdicts_are_the_gates_own_words() -> None:
    admitted = "    Δ对数损失 -0.6732 nat · Δ命中 +26.8 点 → 有增益，可接"
    refused = "    Δ对数损失 +0.0526 nat · Δ命中 +6.2 点 → 无增益，不可接"
    assert D.classify_verdict(admitted, 0) == "ADMISSIBLE"
    assert D.classify_verdict(refused, 0) == "REFUSED"


def test_it_asks_about_the_unmeasured_lanes_and_not_the_refused_ones() -> None:
    """A refusal is re-opened by a ruling, not by a cron job."""

    assert set(D.DEFAULT_SYSTEMS) == {
        "CMA_GRAPES_GLOBAL_POINT", "DWD_ICON_EU_POINT"
    }
    assert "NCEP_HRRR_POINT" not in D.DEFAULT_SYSTEMS
    assert "NCEP_NBM_POINT" not in D.DEFAULT_SYSTEMS


def test_material_counts_only_station_days_that_can_be_scored() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE calibration_regional_point_run("
        " ensemble_system_id TEXT, station_id TEXT, target_local_date TEXT);"
        "CREATE TABLE calibration_station_daily_high_truth("
        " station_id TEXT, local_date TEXT, qc_status TEXT, source_lane TEXT);"
    )
    conn.executemany(
        "INSERT INTO calibration_regional_point_run VALUES(?,?,?)",
        [("X", "ZUUU", "2026-09-06"), ("X", "ZBAA", "2026-09-06"),
         ("X", "ZBAA", "2026-09-07"), ("Y", "ZUUU", "2026-09-06")],
    )
    conn.executemany(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?)",
        [("ZUUU", "2026-09-06", "ACCEPTED", "AWC"),
         ("ZBAA", "2026-09-06", "ACCEPTED", "AWC"),
         # 09-07 has not settled: a lane row without a truth is not material.
         ("ZBAA", "2026-09-07", "PENDING", "AWC")],
    )
    have = D.material(conn, "X")
    assert have["station_days_with_truth"] == 2
    assert have["stations"] == 2
    assert (have["from"], have["through"]) == ("2026-09-06", "2026-09-06")
    assert D.material(conn, "ABSENT")["station_days_with_truth"] == 0


def test_the_threshold_is_the_bootstrap_s_and_says_so() -> None:
    assert D.DEFAULT_MIN_STATION_DAYS == 30
    source = (ROOT / "scripts" / "regional_admission_due_check.py").read_text(
        encoding="utf-8")
    assert "46 station-days" in source, "the threshold cites what it is measured against"


def test_a_not_due_run_still_leaves_a_row(tmp_path) -> None:
    """The days it said nothing are how a lane that never accumulates is noticed."""

    receipt = tmp_path / "due.jsonl"
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE calibration_regional_point_run("
        " ensemble_system_id TEXT, station_id TEXT, target_local_date TEXT);"
        "CREATE TABLE calibration_station_daily_high_truth("
        " station_id TEXT, local_date TEXT, qc_status TEXT, source_lane TEXT);"
    )
    conn.commit()
    conn.close()
    assert D.main.__module__
    sys.argv = [
        "due", "--db", str(db), "--receipt", str(receipt),
        "--system", "CMA_GRAPES_GLOBAL_POINT", "--now-utc", "2026-09-13T03:30:00Z",
    ]
    assert D.main() == 0
    rows = [json.loads(line) for line in receipt.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["state"] == "NOT_DUE"
    assert rows[0]["station_days_with_truth"] == 0
    assert rows[0]["checked_at_utc"] == "2026-09-13T03:30:00Z"
