#!/usr/bin/env python3
"""Read-only dashboard for the model -> calibration -> serving chain.

Answers, in one page and in plain language: is each model still arriving,
is each calibration layer fitted and current, what got promoted and what is
still accumulating, where the data has holes, and which link of the chain
is the one holding everything else up.

Read-only by construction: opens the database read-only and writes nothing.

    python3 scripts/calibration_dashboard.py            # serve on :8787
    python3 scripts/calibration_dashboard.py --json     # print the payload
    python3 scripts/calibration_dashboard.py --html-out page.html
"""

from __future__ import annotations

import argparse
import json
from bisect import bisect_left
from collections import Counter, defaultdict
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from selection_market_dashboard import (  # noqa: E402
    STATION_LABELS,
    build_payload as build_selection_payload,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STATUS_JSON = ROOT / "data" / "weather_basket_yes_status.json"
TRUTH_SWEEP_STATUS = ROOT / "data" / "settlement_truth_sweep_status.json"

# The four native families that can hold a P0 seat, with the archive each
# one is fed from and whether a missed day can ever be recovered.
MEMBER_FAMILIES = (
    ("ECMWF_IFS_ENS_OPEN_PF50", "ECMWF", "ECMWF_OPEN_DATA_S3", True),
    ("NOAA_GEFS_05", "GEFS", "NOAA_GEFS_PDS_S3", True),
    ("ECCC_GEM_GLOBAL_ENS", "ECCC", "ECCC_DD_WEATHER", True),
    ("DWD_ICON_GLOBAL_EPS", "ICON", "DWD_OPENDATA_ICON_EPS", False),
)
STAGE_MEANING = {
    "P0": "开仓：模型融合出的第一版概率，交易据此建仓",
    "P0-M": "确认：当天早上的机场人工预报（TAF）对 P0 的确认或修正",
    "P1": "盯盘：当天实测气温不断进来后对概率的滚动更新",
}

# How far back the model-skill sections score.  Bounded rather than
# open-ended: the member table is append-only and grows by tens of thousands
# of rows a day, and this is the one read on the page that has to touch all
# of it -- an unbounded window makes the render slower every day it runs,
# forever.  Anchored on the newest settled truth rather than on the wall
# clock, so a stalled truth lane shows a thin window instead of an empty one.
SKILL_WINDOW_DAYS = 60
# The lead axis is the hour bucket, derived here from each run's own
# initialisation against each station's own local day start -- never the
# stored d0/d1/d2 label.  Those two disagree badly: measured over this same
# archive, d1 and d2 overlap across 18-31h and d2 alone spans 37 hours, so
# grouping skill by the calendar label mixes runs issued a day apart.
LEAD_LABELS = (
    ("h00_12", "0–12 小时"),
    ("h12_24", "12–24 小时"),
    ("h24_48", "24–48 小时"),
    ("h48_plus", "48 小时以上"),
)


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_hours(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        text = str(stamp).replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (_now() - moment).total_seconds() / 3600.0


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


# How many days of runs the cadence read looks back over.  Long enough that a
# family publishing twice a day still yields a handful of intervals, short
# enough that a schedule change from last month does not set the expectation
# for today.
RUN_CADENCE_WINDOW_DAYS = 6


def collect_run_cadence(
    conn: sqlite3.Connection,
) -> dict[str, list[dict[str, Any]]]:
    """Recent runs per family: when each was issued and when it landed.

    One scan of the member table serves all four families.  The table has no
    index on the run column, so a per-family query would cost a full scan
    each -- four scans of nearly a million rows to answer one question.
    """

    since = (
        _now() - timedelta(days=RUN_CADENCE_WINDOW_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    pairs = " OR ".join(
        "(ensemble_system_id=? AND transport_provider_id=?)"
        for _s, _l, _t, _b in MEMBER_FAMILIES
    )
    params: list[str] = []
    for system_id, _label, transport, _backfillable in MEMBER_FAMILIES:
        params.extend((system_id, transport))
    runs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT ensemble_system_id, native_run_init_utc,"
        " COUNT(DISTINCT station_id), MIN(archive_query_at_utc),"
        " MAX(archive_query_at_utc)"
        " FROM calibration_native_ensemble_historical_member_run"
        f" WHERE native_run_init_utc >= ? AND ({pairs})"
        " GROUP BY ensemble_system_id, native_run_init_utc"
        " ORDER BY ensemble_system_id, native_run_init_utc DESC",
        (since, *params),
    ):
        # First capture, not last: the first is when the run arrived, which
        # is what the publication delay is measured against.  The last only
        # says when the newest station of that run was topped up.
        runs[row[0]].append({
            "run_utc": row[1],
            "stations": row[2],
            "capture_utc": row[3],
            "last_capture_utc": row[4],
            "run_age_hours": _age_hours(row[1]),
            "capture_age_hours": _age_hours(row[3]),
        })
    return runs


def collect_members(
    conn: sqlite3.Connection,
    cadence: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Per family: is it still arriving, how deep is its training history.

    "Still arriving" is judged against the family's own observed rhythm, not
    a shared hour count.  The families publish on different schedules and
    land with different delays -- a fixed threshold either lets a six-hourly
    family sleep through a missed cycle or paints a twice-daily one amber
    every afternoon.
    """

    rows = []
    for system_id, label, transport, backfillable in MEMBER_FAMILIES:
        record = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT station_id),"
            " COUNT(DISTINCT target_local_date), MAX(native_run_init_utc),"
            " MAX(archive_query_at_utc)"
            " FROM calibration_native_ensemble_historical_member_run"
            " WHERE ensemble_system_id=? AND transport_provider_id=?",
            (system_id, transport),
        ).fetchone()
        seats = conn.execute(
            "SELECT COUNT(*) FROM"
            " calibration_cross_native_ensemble_selection_evidence"
            " WHERE selected_system_ids_json LIKE ?",
            (f"%{system_id}%",),
        ).fetchone()[0]
        run_age = _age_hours(record[3])
        recent = cadence.get(system_id, [])
        latest = recent[0] if recent else None
        # The gap between issue times is the family's publication rhythm; the
        # gap between issue and capture is how long its archive takes to
        # publish and us to pull.  The next run is only late once both have
        # elapsed.
        gaps = [
            a["run_age_hours"] - b["run_age_hours"]
            for a, b in zip(recent[1:], recent[:-1])
            if a["run_age_hours"] is not None and b["run_age_hours"] is not None
        ][:8]
        lags = [
            r["run_age_hours"] - r["capture_age_hours"]
            for r in recent[:8]
            if r["run_age_hours"] is not None
            and r["capture_age_hours"] is not None
        ]
        cycle_hours = _median([g for g in gaps if g > 0])
        lag_hours = _median([lag for lag in lags if lag >= 0])
        due_in_hours = None
        overdue_hours = None
        if run_age is not None and cycle_hours and lag_hours is not None:
            due_in_hours = cycle_hours + lag_hours - run_age
            overdue_hours = -due_in_hours
        if run_age is None:
            health = "red"
            note = "从未入库"
        elif overdue_hours is None:
            # Not enough history to know the rhythm -- fall back to the hour
            # count, which is all we can say without it.
            health = ("red" if run_age > 36
                      else "amber" if run_age > 18 else "green")
            note = (f"最新 run {run_age:.0f} 小时前"
                    + ("，慢于常态" if run_age > 18 else ""))
        # A run that is late by less than one whole cycle has not cost us a
        # cycle yet -- publication times wander by hours and a light that
        # comes on for that is on most of the day.  Amber is one missed
        # cycle, red is more than two.
        elif overdue_hours > 2.5 * cycle_hours:
            health = "red"
            note = (f"已漏掉 {overdue_hours / cycle_hours:.0f} 轮，"
                    f"最新 run 是 {run_age:.0f} 小时前的")
        elif overdue_hours > cycle_hours:
            health = "amber"
            note = (f"漏了一轮：按 {cycle_hours:.0f} 小时一轮，"
                    f"下一轮已晚 {overdue_hours:.1f} 小时")
        else:
            health = "green"
            note = (f"按 {cycle_hours:.0f} 小时一轮在更新"
                    if overdue_hours <= 0 else
                    f"按 {cycle_hours:.0f} 小时一轮在更新，"
                    f"下一轮晚了 {overdue_hours:.1f} 小时，仍在正常抖动内")
        rows.append({
            "label": label,
            "system_id": system_id,
            "member_rows": record[0],
            "stations": record[1],
            "training_days": record[2],
            "latest_run_utc": record[3],
            "latest_capture_utc": record[4],
            "run_age_hours": run_age,
            "latest_capture_age_hours": (
                latest["capture_age_hours"] if latest
                else _age_hours(record[4])
            ),
            "latest_run_stations": latest["stations"] if latest else None,
            "previous_run_utc": recent[1]["run_utc"] if len(recent) > 1 else None,
            "cycle_hours": cycle_hours,
            "publish_lag_hours": lag_hours,
            "due_in_hours": due_in_hours,
            "overdue_hours": overdue_hours,
            "runs_recent": recent[:6],
            "runs_window_days": RUN_CADENCE_WINDOW_DAYS,
            "seats_won": seats,
            "backfillable": backfillable,
            "health": health,
            "note": note,
        })
    return rows


def collect_correction_and_weights(conn: sqlite3.Connection) -> dict[str, Any]:
    """The two per-station tables P0 serves from."""

    correction = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT station_id), MAX(valid_from_utc)"
        " FROM calibration_native_ensemble_activation"
        " WHERE artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION'"
    ).fetchone()
    weights = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT station_id), MAX(valid_from_utc)"
        " FROM calibration_native_ensemble_activation"
        " WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE'"
    ).fetchone()
    lag = conn.execute(
        "SELECT COUNT(*) FROM ("
        " SELECT s.station_id, s.lead_bucket, s.run_cycle,"
        "  MAX(s.train_end_date) AS corr_end,"
        "  (SELECT MAX(e.train_end_date)"
        "   FROM calibration_cross_native_ensemble_selection_evidence e"
        "   WHERE e.station_id=s.station_id"
        "    AND e.lead_bucket=s.lead_bucket"
        "    AND e.run_cycle=s.run_cycle) AS weight_end"
        " FROM calibration_native_ensemble_system_artifact s"
        " WHERE s.lead_bucket IN ('h00_12','h12_24','h24_48')"
        " GROUP BY s.station_id, s.lead_bucket, s.run_cycle)"
        " WHERE weight_end IS NOT NULL AND julianday(corr_end)"
        " - julianday(weight_end) >= 3"
    ).fetchone()[0]
    return {
        "correction": {
            "activations": correction[0],
            "stations": correction[1],
            "latest_utc": correction[2],
            "age_hours": _age_hours(correction[2]),
        },
        "weights": {
            "activations": weights[0],
            "stations": weights[1],
            "latest_utc": weights[2],
            "age_hours": _age_hours(weights[2]),
        },
        "weight_lag_scopes": lag,
    }


def collect_band_stages(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per stage: served factors, evidence depth, what blocks each band."""

    from bayes_basket_target.band_reliability_store import (
        BAND_STAGES,
        MIN_QUALIFYING_DAYS,
    )

    if not _table_exists(conn, "calibration_band_reliability_table"):
        return []
    stages = []
    for stage in BAND_STAGES:
        row = conn.execute(
            "SELECT band_table_sha256, factors_json, evidence_json,"
            " fitted_at_utc, serving_day_count"
            " FROM calibration_band_reliability_table"
            " WHERE activated=1 AND stage=?",
            (stage,),
        ).fetchone()
        journal_rows = []
        if _table_exists(conn, "calibration_band_reliability_fit_journal"):
            journal_rows = conn.execute(
                "SELECT fitted_at_utc, outcome, serving_day_count,"
                " qualifying_cell_count, band_table_sha256,"
                " serving_station_day_count, evidence_json"
                " FROM calibration_band_reliability_fit_journal"
                " WHERE stage=? ORDER BY fitted_at_utc DESC LIMIT 8",
                (stage,),
            ).fetchall()
        if row is None:
            stages.append({
                "stage": stage,
                "meaning": STAGE_MEANING.get(stage, ""),
                "state": "未建表",
                "health": "amber",
                "bands": [],
                "journal": [],
            })
            continue
        factors = json.loads(row[1])
        evidence = json.loads(row[2])
        fitted_at = row[3]
        days = int(row[4])
        station_days = evidence.get("serving_station_day_count")
        # The active row is content-addressed by its factors and is only
        # rewritten when they change, so a stage that keeps re-fitting to
        # the same table -- an accumulating stage always does -- freezes
        # its fit time and day counts at whatever the last *change* saw.
        # The journal is the record of what each fit knew, so the evidence
        # side of this panel reads its newest entry for this exact table.
        if journal_rows and str(journal_rows[0][4]) == str(row[0]):
            latest = journal_rows[0]
            fitted_at = latest[0]
            days = int(latest[2])
            station_days = int(latest[5])
            try:
                evidence = json.loads(latest[6])
            except (ValueError, TypeError):
                pass
        reports = evidence.get("serving_report") or {}
        basis = evidence.get("basis") or {}
        bands = []
        for cell, factor in factors:
            key = str([float(cell[0]), float(cell[1])])
            detail = reports.get(key) or {}
            reason = str(basis.get(key) or "")
            bands.append({
                "lower": cell[0],
                "upper": cell[1],
                "factor": factor,
                "measured_ratio": detail.get("ratio"),
                "interval": detail.get("interval"),
                "qualified": bool(detail.get("qualified")),
                "basis": reason,
                "blocked_by": detail.get("blocked_by"),
            })
        accumulating = bool(evidence.get("accumulating"))
        active_cells = sum(1 for _c, f in factors if float(f) != 1.0)
        if accumulating:
            state = f"攒证据 {days}/{MIN_QUALIFYING_DAYS} 天"
            health = "amber"
        elif active_cells:
            state = f"{active_cells} 档生效"
            health = "green"
        else:
            state = "全档恒等（证据未够格）"
            health = "green"
        stages.append({
            "stage": stage,
            "meaning": STAGE_MEANING.get(stage, ""),
            "state": state,
            "health": health,
            "table_sha": str(row[0])[:12],
            "fitted_at_utc": fitted_at,
            "fit_age_hours": _age_hours(fitted_at),
            "serving_days": days,
            "min_days": MIN_QUALIFYING_DAYS,
            "station_days": station_days,
            "accumulating": accumulating,
            "bands": bands,
            "journal": [
                {
                    "at": j[0],
                    "outcome": j[1],
                    "days": j[2],
                    "cells": j[3],
                    "sha": str(j[4])[:12],
                }
                for j in journal_rows
            ],
        })
    return stages


def collect_chain(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Each link of the live chain, newest evidence and whether it moved."""

    links: list[dict[str, Any]] = []

    def add(
        name: str,
        plain: str,
        latest: str | None,
        volume: str,
        amber_h: float,
        red_h: float,
    ) -> None:
        age = _age_hours(latest)
        if age is None:
            health, note = "red", "无记录"
        elif age > red_h:
            health, note = "red", f"{age / 24:.1f} 天没有新记录"
        elif age > amber_h:
            health, note = "amber", f"{age:.0f} 小时没有新记录"
        else:
            health, note = "green", f"{age:.1f} 小时前有新记录"
        links.append({
            "name": name,
            "plain": plain,
            "latest_utc": latest,
            "age_hours": age,
            "volume": volume,
            "health": health,
            "note": note,
        })

    capture = conn.execute(
        "SELECT MAX(archive_query_at_utc), COUNT(*)"
        " FROM calibration_native_ensemble_historical_member_run"
    ).fetchone()
    add(
        "① 模型采集",
        "四家气象机构的集合预报下载入库",
        capture[0],
        f"{capture[1]:,} 条成员预报",
        18,
        36,
    )
    truth = conn.execute(
        "SELECT MAX(local_date), COUNT(*)"
        " FROM calibration_station_daily_high_truth"
    ).fetchone()
    truth_latest = (
        f"{truth[0]}T23:59:59Z" if truth[0] else None
    )
    add(
        "② 真值结算",
        "各站当天实际最高温入库，用来给预报打分",
        truth_latest,
        f"{truth[1]:,} 个站日",
        48,
        96,
    )
    art = conn.execute(
        "SELECT MAX(valid_from_utc), COUNT(*)"
        " FROM calibration_native_ensemble_activation"
    ).fetchone()
    add(
        "③ 校正与权重",
        "每站每模型的偏差订正，以及各模型的融合权重",
        art[0],
        f"{art[1]:,} 次激活",
        30,
        72,
    )
    band = None
    if _table_exists(conn, "calibration_band_reliability_table"):
        band = conn.execute(
            "SELECT MAX(fitted_at_utc), COUNT(*)"
            " FROM calibration_band_reliability_table"
        ).fetchone()
    add(
        "④ 概率分档校准",
        "对最终概率数字本身的可信度做修正",
        None if band is None else band[0],
        "未建表" if band is None else f"{band[1]} 个版本",
        30,
        72,
    )
    p0 = conn.execute(
        "SELECT MAX(decision_at_utc), COUNT(*)"
        " FROM basket_probability_bridge_history WHERE stage='P0'"
    ).fetchone()
    add(
        "⑤ P0 概率服务",
        "把校准后的概率投影到市场桶，供交易使用",
        p0[0],
        f"{p0[1]:,} 份快照",
        12,
        36,
    )
    p1 = conn.execute(
        "SELECT MAX(decision_at_utc), COUNT(*)"
        " FROM basket_probability_bridge_history WHERE stage='P1'"
    ).fetchone()
    add(
        "⑥ P1 盘中更新",
        "当天实测进来后滚动更新概率",
        p1[0],
        f"{p1[1]:,} 份快照",
        24,
        72,
    )
    return links


TAF_TRAINING_ROOT = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "taf_revision_training"
)


def collect_p0m_progress(conn: sqlite3.Connection) -> dict[str, Any]:
    """How far each station is from a P0-M artifact, and what is missing.

    P0-M has published nothing since 2026-07-30, and the reason is neither
    a broken stage nor a stale artifact: the training lane runs every cycle
    for all forty-five stations and correctly refuses to fit on evidence it
    does not have.  What it is short of lives in the receipts this reads.
    """

    rows: list[dict[str, Any]] = []
    if not TAF_TRAINING_ROOT.is_dir():
        return {"stations": [], "summary": {}}
    truth_days = {
        str(station): int(count)
        for station, count in conn.execute(
            "SELECT station_id, COUNT(DISTINCT local_date)"
            " FROM calibration_station_daily_high_truth"
            " WHERE qc_status='ACCEPTED' AND source_lane='AWC'"
            " GROUP BY station_id"
        )
    }
    span_days = conn.execute(
        "SELECT COUNT(DISTINCT local_date)"
        " FROM calibration_station_daily_high_truth WHERE qc_status='ACCEPTED'"
    ).fetchone()
    calendar_days = int((span_days or [0])[0] or 0)
    for folder in sorted(TAF_TRAINING_ROOT.iterdir()):
        station = folder.name
        receipt = folder / "receipt.json"
        rolling = folder / f"{station}_rolling_native_oos.json"
        if not receipt.is_file() or not rolling.is_file():
            continue
        try:
            rec = json.loads(receipt.read_text())
            roll = json.loads(rolling.read_text())
        except (ValueError, OSError):
            continue
        scope = rec.get("live_p0_scope") or {}
        want = (
            str(scope.get("season_bin") or ""),
            str(scope.get("lead_bucket") or ""),
        )
        best: dict[str, Any] | None = None
        refused = False
        for entry in roll.get("accumulating") or []:
            if (
                str(entry.get("season_bin") or ""),
                str(entry.get("lead_bucket") or ""),
            ) != want:
                continue
            if entry.get("state") == "OOS_REJECTED":
                refused = True
            plan = entry.get("plan") or {}
            missing = int(plan.get("missing_common_case_count") or 0)
            if best is None or missing < int(best["missing"]):
                best = {
                    "missing": missing,
                    "have": int(plan.get("common_case_count") or 0),
                    "need": int(plan.get("required_common_case_count") or 0),
                    "pair": [
                        str(value)
                        for value in (entry.get("selected_system_ids") or [])
                    ],
                    "refused": entry.get("state") == "OOS_REJECTED",
                }
        have_truth = truth_days.get(station, 0)
        rows.append({
            "station_id": station,
            "station_label": STATION_LABELS.get(station, ""),
            "lead_bucket": want[1],
            "state": str(rec.get("state") or ""),
            "missing": best["missing"] if best else None,
            "have": best["have"] if best else 0,
            "need": best["need"] if best else 0,
            "pair": best["pair"] if best else [],
            "oos_refused": bool(best and best["refused"]) or refused,
            "truth_days": have_truth,
            "truth_short": max(0, calendar_days - have_truth),
        })
    rows.sort(key=lambda item: (
        item["missing"] if item["missing"] is not None else 10_000
    ))
    ready = [r for r in rows if r["missing"] == 0]
    return {
        "stations": rows,
        "summary": {
            "station_count": len(rows),
            "calendar_days": calendar_days,
            "evidence_ready": len(ready),
            "oos_refused": len([r for r in rows if r["oos_refused"]]),
            "within_three_days": len([
                r for r in rows
                if r["missing"] is not None and 0 < r["missing"] <= 3
            ]),
            "truth_days_missing_total": sum(r["truth_short"] for r in rows),
        },
    }


def _market_bucket_index(unit: str, celsius: float) -> int | None:
    """Which rung of the station's own market ladder a temperature sits on.

    The ladder is not the same width everywhere: a Fahrenheit book steps two
    degrees F per rung (1.11C) and a Celsius book one degree C, so a fixed
    temperature tolerance would score the two books against different targets.
    Counting rungs is what makes "off by one bucket" mean the same thing at
    KLGA and at ZUUU.

    Deliberately unbounded at both ends.  A real book caps its ladder with an
    "or below"/"or higher" bucket, but clipping the distance there would
    report a five-rung miss as a one-rung miss purely because the truth
    landed past the last listed rung.
    """

    if unit == "F":
        # Rungs are 80-81F, 82-83F, ...: boundaries fall on the half degree
        # between an odd and the next even degree.
        return int((celsius * 9.0 / 5.0 + 32.0 + 0.5) // 2.0)
    if unit == "C":
        return int((celsius + 0.5) // 1.0)
    return None


def _correction_in_force(
    artifacts: dict[tuple[str, str, str, str], tuple[list[str], list[Any]]],
    system_id: str,
    station_id: str,
    lead_labels: set[str],
    run_cycle: str,
    event_date: str,
) -> tuple[dict[str, Any], float] | None:
    """The bias artifact that was already fitted when this day arrived.

    Strictly before the event date, so this scores the correction the way
    trading met it -- on a day it had never seen.  Using whatever artifact is
    active today would let a fit that trained *through* the day being scored
    mark its own homework, and the whole point of the corrected column is to
    say whether the correction layer earns its keep out of sample.

    Both lead vocabularies are tried because the archive spans the migration:
    a July run was stamped d0/d1/d2 and its artifacts are keyed that way,
    while an August run and its artifacts are keyed by hour bucket.  The run
    cycle still has to match exactly -- serving refuses a cross-cycle
    artifact, and a coverage number that quietly borrows one would overstate
    how much of the fleet is actually corrected.
    """

    best: tuple[dict[str, Any], float] | None = None
    best_end = ""
    for label in lead_labels:
        entry = artifacts.get((system_id, station_id, label, run_cycle))
        if entry is None:
            continue
        ends, rows = entry
        position = bisect_left(ends, event_date)
        if position and ends[position - 1] > best_end:
            best_end = ends[position - 1]
            best = rows[position - 1][1]
    return best


def collect_model_skill(conn: sqlite3.Connection) -> dict[str, Any]:
    """Per model and lead: member spread, and how often the centre lands.

    Three questions the page could not answer before.  How far apart do a
    model's own members sit -- its dispersion, which is the only uncertainty
    it states about itself.  How often does its raw centre land on the
    market's bucket, one rung out, two rungs out, and does that decay with
    lead time.  And does the per-station bias correction the fleet serves
    actually move those numbers.

    Raw and corrected are reported on the *same* station-days.  Only about a
    quarter of scored runs have an artifact fitted before their event date,
    and comparing a full-sample raw rate against a quarter-sample corrected
    one would credit the correction layer for a difference in which days it
    covers rather than in what it does to them.
    """

    from bayes_basket_target.native_archive_lead import (  # noqa: PLC0415
        lead_hour_bucket,
    )

    zone_names = {
        str(station): str(zone)
        for station, zone in conn.execute(
            "SELECT station_id, settlement_timezone"
            " FROM calibration_station_market_timezone_binding"
        )
    }
    units: dict[str, str] = {}
    for station, unit in conn.execute(
        "SELECT station_id, settlement_unit FROM basket_target_market_registry"
        " WHERE station_id IS NOT NULL AND settlement_unit IS NOT NULL"
        " GROUP BY station_id, settlement_unit"
    ):
        units.setdefault(str(station), str(unit))
    truth: dict[tuple[str, str], float] = {}
    for station, day, tmax in conn.execute(
        "SELECT station_id, local_date, official_tmax_c"
        " FROM calibration_station_daily_high_truth"
        " WHERE qc_status='ACCEPTED' ORDER BY revision_no"
    ):
        truth[(str(station), str(day))] = float(tmax)
    if not truth:
        return {"rows": [], "totals": [], "summary": {}}
    newest_truth = max(day for _station, day in truth)
    window_start = str(
        (
            datetime.fromisoformat(newest_truth)
            - timedelta(days=SKILL_WINDOW_DAYS)
        ).date()
    )

    system_ids = tuple(system for system, _l, _t, _b in MEMBER_FAMILIES)
    grouped: dict[tuple[str, str, str, str], list[Any]] = defaultdict(list)
    placeholders = ",".join("?" for _ in system_ids)
    for (
        system_id, station, lead, cycle, end, bias_json, spread_json
    ) in conn.execute(
        "SELECT ensemble_system_id, station_id, lead_bucket, run_cycle,"
        " train_end_date, bias_parameters_json, spread_parameters_json"
        " FROM calibration_native_ensemble_system_artifact"
        f" WHERE ensemble_system_id IN ({placeholders})",
        system_ids,
    ):
        try:
            bias = json.loads(bias_json)
            spread = float(json.loads(spread_json)["spread_factor"])
        except (ValueError, KeyError, TypeError):
            continue
        grouped[
            (str(system_id), str(station), str(lead), str(cycle))
        ].append((str(end), (bias, spread)))
    artifacts = {}
    for key, rows in grouped.items():
        rows.sort(key=lambda row: row[0])
        artifacts[key] = ([row[0] for row in rows], rows)

    # One row per model run per station-day: the members collapse to a centre
    # and a spread here rather than in Python, because the raw member table is
    # the largest thing on the page by an order of magnitude.
    conditions = " OR ".join(
        "(ensemble_system_id=? AND transport_provider_id=?)"
        for _s, _l, _t, _b in MEMBER_FAMILIES
    )
    parameters: list[Any] = [window_start]
    for system_id, _label, transport, _backfillable in MEMBER_FAMILIES:
        parameters.extend((system_id, transport))
    runs = conn.execute(
        "SELECT ensemble_system_id, station_id, target_local_date,"
        " native_run_init_utc, run_cycle, archive_lead_bucket,"
        " AVG(raw_tmax_c), AVG(raw_tmax_c*raw_tmax_c), COUNT(*)"
        " FROM calibration_native_ensemble_historical_member_run"
        " WHERE qc_status='ACCEPTED' AND native_run_init_utc IS NOT NULL"
        " AND target_local_date >= ?"
        f" AND ({conditions})"
        " GROUP BY 1,2,3,4,5,6",
        parameters,
    ).fetchall()

    def blank() -> dict[str, Any]:
        return {
            "runs": 0, "members": 0, "sigma": [], "scored": 0,
            "abs_error": 0.0, "signed_error": 0.0, "square_error": 0.0,
            "raw": [0, 0, 0], "paired": 0, "paired_raw": [0, 0, 0],
            "paired_cal": [0, 0, 0], "paired_square": 0.0,
            "paired_abs": 0.0, "factor": [], "uncorrected": 0,
        }

    cells: dict[tuple[str, str], dict[str, Any]] = defaultdict(blank)
    zones: dict[str, ZoneInfo] = {}
    day_starts: dict[tuple[str, str], datetime] = {}
    unscored: dict[str, int] = defaultdict(int)
    for (
        system_id, station, day, run_init, cycle, stored_lead,
        centre, mean_square, member_count,
    ) in runs:
        system_id, station, day = str(system_id), str(station), str(day)
        zone_name = zone_names.get(station)
        if zone_name is None:
            unscored["no_timezone"] += 1
            continue
        if station not in zones:
            try:
                zones[station] = ZoneInfo(zone_name)
            except (KeyError, ValueError):
                unscored["no_timezone"] += 1
                continue
        scope = (station, day)
        if scope not in day_starts:
            day_starts[scope] = datetime.fromisoformat(day).replace(
                tzinfo=zones[station]
            ).astimezone(timezone.utc)
        try:
            initialised = datetime.fromisoformat(
                str(run_init).replace("Z", "+00:00")
            )
        except ValueError:
            unscored["bad_run_init"] += 1
            continue
        if initialised.tzinfo is None:
            initialised = initialised.replace(tzinfo=timezone.utc)
        lead = lead_hour_bucket(
            (
                day_starts[scope] - initialised.astimezone(timezone.utc)
            ).total_seconds() / 3600.0
        )
        centre = float(centre)
        cell = cells[(system_id, lead)]
        cell["runs"] += 1
        cell["members"] += int(member_count)
        cell["sigma"].append(
            max(0.0, float(mean_square) - centre ** 2) ** 0.5
        )
        observed = truth.get(scope)
        if observed is None:
            unscored["no_truth_yet"] += 1
            continue
        unit = units.get(station, "")
        truth_bucket = _market_bucket_index(unit, observed)
        raw_bucket = _market_bucket_index(unit, centre)
        if truth_bucket is None or raw_bucket is None:
            unscored["no_market_ladder"] += 1
            continue
        cell["scored"] += 1
        cell["abs_error"] += abs(centre - observed)
        cell["signed_error"] += centre - observed
        cell["square_error"] += (centre - observed) ** 2
        raw_distance = abs(raw_bucket - truth_bucket)
        for offset in range(3):
            if raw_distance <= offset:
                cell["raw"][offset] += 1
        correction = _correction_in_force(
            artifacts, system_id, station,
            {lead, str(stored_lead)}, str(cycle), day,
        )
        if correction is None:
            cell["uncorrected"] += 1
            continue
        bias, factor = correction
        # One correction form; the board reports what P0 actually serves.
        corrected = centre + float(bias["bias_c"])
        corrected_bucket = _market_bucket_index(unit, corrected)
        if corrected_bucket is None:
            cell["uncorrected"] += 1
            continue
        cell["paired"] += 1
        cell["factor"].append(factor)
        cell["paired_abs"] += abs(corrected - observed)
        cell["paired_square"] += (corrected - observed) ** 2
        corrected_distance = abs(corrected_bucket - truth_bucket)
        for offset in range(3):
            if raw_distance <= offset:
                cell["paired_raw"][offset] += 1
            if corrected_distance <= offset:
                cell["paired_cal"][offset] += 1

    def emit(cell: dict[str, Any], label: str, system_id: str,
             lead: str, lead_label: str) -> dict[str, Any]:
        scored = cell["scored"]
        paired = cell["paired"]
        sigma = median(cell["sigma"]) if cell["sigma"] else None
        factor = median(cell["factor"]) if cell["factor"] else None
        width = None if sigma is None or factor is None else sigma * factor
        corrected_rmse = (
            (cell["paired_square"] / paired) ** 0.5 if paired else None
        )
        return {
            "label": label,
            "system_id": system_id,
            "lead_bucket": lead,
            "lead_label": lead_label,
            "runs": cell["runs"],
            "members_per_run": (
                cell["members"] / cell["runs"] if cell["runs"] else None
            ),
            "member_sigma_c": sigma,
            "spread_factor": factor,
            "served_width_c": width,
            "scored": scored,
            "raw_rates": [
                cell["raw"][i] / scored if scored else None
                for i in range(3)
            ],
            "mae_c": cell["abs_error"] / scored if scored else None,
            "signed_bias_c": (
                cell["signed_error"] / scored if scored else None
            ),
            "rmse_c": (
                (cell["square_error"] / scored) ** 0.5 if scored else None
            ),
            "paired": paired,
            "paired_raw_rates": [
                cell["paired_raw"][i] / paired if paired else None
                for i in range(3)
            ],
            "paired_calibrated_rates": [
                cell["paired_cal"][i] / paired if paired else None
                for i in range(3)
            ],
            "paired_mae_c": (
                cell["paired_abs"] / paired if paired else None
            ),
            "corrected_rmse_c": corrected_rmse,
            # Above 1 the served cloud is narrower than the errors it has to
            # cover -- the model claims more certainty than it earns.
            "width_ratio": (
                corrected_rmse / width
                if corrected_rmse and width else None
            ),
            "uncorrected": cell["uncorrected"],
        }

    rows: list[dict[str, Any]] = []
    totals: list[dict[str, Any]] = []
    for system_id, label, _transport, _backfillable in MEMBER_FAMILIES:
        combined = blank()
        for lead, lead_label in LEAD_LABELS:
            cell = cells.get((system_id, lead))
            if cell is None or not cell["runs"]:
                continue
            for field in (
                "runs", "members", "scored", "paired", "uncorrected",
            ):
                combined[field] += cell[field]
            for field in (
                "abs_error", "signed_error", "square_error",
                "paired_square", "paired_abs",
            ):
                combined[field] += cell[field]
            for field in ("raw", "paired_raw", "paired_cal"):
                for offset in range(3):
                    combined[field][offset] += cell[field][offset]
            combined["sigma"].extend(cell["sigma"])
            combined["factor"].extend(cell["factor"])
            rows.append(emit(cell, label, system_id, lead, lead_label))
        if combined["runs"]:
            totals.append(
                emit(combined, label, system_id, "all", "全部提前量")
            )
    return {
        "rows": rows,
        "totals": totals,
        "summary": {
            "window_days": SKILL_WINDOW_DAYS,
            "window_start": window_start,
            "window_end": newest_truth,
            "run_count": sum(r["runs"] for r in totals),
            "scored_count": sum(r["scored"] for r in totals),
            "paired_count": sum(r["paired"] for r in totals),
            "unscored": dict(unscored),
        },
    }


def collect_truth_materialization(conn: sqlite3.Connection) -> dict[str, Any]:
    """Whether every settled station-day has been turned into a truth row.

    The distinction this section exists to draw: a missing truth row almost
    never means a missing observation.  On 2026-08-17 the table held 13 of 45
    stations for 08-11 while the archive held all 45 that day -- the rows had
    simply never been asked for, and nothing retries a day once it passes.
    Everything downstream is gated on settled days, so a hole here does not
    cost one scope one day, it costs every cohort that would have shared it.
    """

    coverage = [
        {
            "event_date": str(day),
            "eligible": int(eligible),
            "materialized": int(materialized or 0),
            "missing": max(0, int(eligible) - int(materialized or 0)),
        }
        for day, eligible, materialized in conn.execute(
            "SELECT r.event_date, COUNT(DISTINCT r.station_id),"
            " (SELECT COUNT(DISTINCT t.station_id)"
            "  FROM calibration_station_daily_high_truth t"
            "  WHERE t.local_date=r.event_date AND t.qc_status='ACCEPTED')"
            " FROM basket_target_market_registry r"
            " WHERE r.source_eligible=1"
            "  AND r.event_date>=date('now','-21 days')"
            " GROUP BY r.event_date ORDER BY r.event_date DESC"
        )
    ]
    sweep: dict[str, Any] = {}
    if TRUTH_SWEEP_STATUS.is_file():
        try:
            sweep = json.loads(TRUTH_SWEEP_STATUS.read_text())
        except (ValueError, OSError):
            sweep = {}
    # A day still open somewhere is not a gap.  The fleet spans every
    # timezone, so an event date stays unsettleable for up to a day and a half
    # after it begins in UTC, and counting those rows as missing would make a
    # healthy lane look permanently a day behind.  Which days those are is not
    # computed from dates here: any rule this page invented would be wrong for
    # some station at some hour.  The sweep asks the writer itself, scope by
    # scope, and reports back how many it declined as still open on each day.
    open_by_day = sweep.get("open_by_day") or {}
    for row in coverage:
        row["settling"] = int(open_by_day.get(row["event_date"], 0)) > 0
    still_open = int(sweep.get("open_day_skipped") or 0)
    refused = int(sweep.get("refused") or 0)
    deferred = int(sweep.get("deferred_locked") or 0)
    outstanding = max(
        0,
        int(sweep.get("backlog_after") or 0)
        - still_open - refused - deferred,
    )
    age = _age_hours(sweep.get("swept_at_utc"))
    if not sweep:
        health, note = "red", "从未跑过物化清扫"
    elif age is None or age > 3:
        health, note = "red", "清扫已停，缺的日子没有任何东西会重试"
    elif outstanding:
        health, note = "amber", f"{outstanding} 个已结算站日还没有真值行"
    elif deferred:
        health, note = "green", f"{deferred} 个站日让给了常驻写入，下一轮再问"
    elif refused:
        health, note = "green", f"已结算的都已物化；{refused} 个站日被证据本身挡住"
    else:
        health, note = "green", "已结算的站日全部有真值行"
    return {
        "coverage": coverage,
        "sweep": sweep,
        "health": health,
        "note": note,
        "sweep_age_hours": age,
        "outstanding": outstanding,
        "still_open": still_open,
        "refused": refused,
        "deferred": deferred,
        "refusals": sweep.get("refusals") or [],
    }


def collect_oos_scores(conn: sqlite3.Connection) -> dict[str, Any]:
    """The gate's own scores, against the baseline that makes them readable.

    A CRPS of 0.80 means nothing alone -- it depends on the station, the
    season and how wide the buckets are.  What makes it a judgement is the
    same number for climatology on the same days: the gate admits a
    combination only when it beats that, and the ratio is the one figure that
    is comparable across scopes.
    """

    newest: dict[tuple[str, str, str], tuple[str, Any, Any, str, str]] = {}
    for (
        station, lead, cycle, evaluated, state, profile, metrics, baseline
    ) in conn.execute(
        "SELECT station_id, lead_bucket, run_cycle, evaluated_at_utc,"
        " oos_state, threshold_profile, oos_metrics_json,"
        " climatology_baseline_metrics_json"
        " FROM calibration_cross_native_ensemble_oos_evaluation"
        " WHERE evaluated_at_utc>=datetime('now','-7 days')"
        " ORDER BY evaluated_at_utc"
    ):
        newest[(str(station), str(lead), str(cycle))] = (
            str(evaluated), metrics, baseline, str(state), str(profile)
        )
    cells: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "scopes": 0, "passed": 0,
        "crps": [], "brier": [], "nll": [],
        "crps_base": [], "brier_base": [], "nll_base": [],
        "crps_skill": [], "brier_skill": [], "profiles": Counter(),
    })
    latest = ""
    for (_station, lead, _cycle), row in newest.items():
        evaluated, metrics_json, baseline_json, state, profile = row
        try:
            metrics = json.loads(metrics_json)
            baseline = json.loads(baseline_json)
        except (ValueError, TypeError):
            continue
        cell = cells[lead]
        cell["scopes"] += 1
        cell["profiles"][profile] += 1
        if state == "PRODUCTION_NO_LIVE_OOS_PASS":
            cell["passed"] += 1
        latest = max(latest, evaluated)
        for name in ("crps", "brier", "nll"):
            value = metrics.get(name)
            reference = baseline.get(name)
            if value is None or reference is None:
                continue
            cell[name].append(float(value))
            cell[f"{name}_base"].append(float(reference))
            if float(reference) > 0 and name in ("crps", "brier"):
                cell[f"{name}_skill"].append(
                    1.0 - float(value) / float(reference)
                )
    order = {name: index for index, (name, _l) in enumerate(LEAD_LABELS)}
    rows = []
    for lead in sorted(cells, key=lambda name: order.get(name, 9)):
        cell = cells[lead]
        rows.append({
            "lead_bucket": lead,
            "lead_label": dict(LEAD_LABELS).get(lead, lead),
            "scopes": cell["scopes"],
            "pass_rate": (
                cell["passed"] / cell["scopes"] if cell["scopes"] else None
            ),
            "profile": (
                cell["profiles"].most_common(1)[0][0]
                if cell["profiles"] else ""
            ),
            **{
                key: (median(cell[key]) if cell[key] else None)
                for key in (
                    "crps", "brier", "nll", "crps_base", "brier_base",
                    "nll_base", "crps_skill", "brier_skill",
                )
            },
        })
    return {
        "rows": rows,
        "scope_count": len(newest),
        "latest_evaluated_at_utc": latest or None,
        "latest_age_hours": _age_hours(latest or None),
    }


def collect_serving_weights(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every active P0 blend, spelled out: which models, at what weight.

    Section three counts the weight tables; this reads the weights
    themselves off the serving path -- newest activation pointer per scope,
    the artifact it binds, the selection evidence whose contest produced the
    weights -- so the page shows what P0 actually blends, not a census of
    the tables it is stored in.  The holdout end rides along because the
    fleet rebinds gradually after a re-selection: a scope still serving an
    old contest is visible here as an old window, not as a mystery.
    """

    labels = {system: label for system, label, _t, _b in MEMBER_FAMILIES}
    rows: list[dict[str, Any]] = []
    for (
        station, season, lead, cycle, weights_json,
        candidates_json, selected_json, holdout_end, train_end,
    ) in conn.execute(
        "SELECT p.station_id, p.season_bin, p.lead_bucket, p.run_cycle,"
        " se.selected_weights_json, se.candidate_system_ids_json,"
        " se.selected_system_ids_json, se.holdout_end_date,"
        " se.train_end_date"
        " FROM (SELECT station_id, season_bin, lead_bucket, run_cycle,"
        "   artifact_id, MAX(generation)"
        "  FROM calibration_native_ensemble_activation"
        "  WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE'"
        "  GROUP BY station_id, season_bin, lead_bucket, run_cycle) p"
        " JOIN calibration_cross_native_ensemble_artifact ar"
        "  ON ar.cross_ensemble_artifact_id = p.artifact_id"
        " JOIN calibration_cross_native_ensemble_selection_evidence se"
        "  ON se.content_sha256 = ar.selection_evidence_sha256"
    ):
        try:
            weights = json.loads(weights_json)
            candidates = json.loads(candidates_json)
            selected = json.loads(selected_json)
        except (ValueError, TypeError):
            continue
        rows.append({
            "station": str(station),
            "season": str(season),
            "lead_bucket": str(lead),
            "lead_label": dict(LEAD_LABELS).get(str(lead), str(lead)),
            "run_cycle": str(cycle),
            "weights": {
                labels.get(str(system), str(system)): float(value)
                for system, value in weights.items()
            },
            "candidates": [
                labels.get(str(system), str(system))
                for system in candidates
            ],
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "holdout_end": str(holdout_end),
            "train_end": str(train_end),
        })
    lead_order = {name: i for i, (name, _l) in enumerate(LEAD_LABELS)}
    rows.sort(key=lambda r: (
        r["station"], lead_order.get(r["lead_bucket"], 9), r["run_cycle"]
    ))
    newest_holdout = max((r["holdout_end"] for r in rows), default="")
    stale_cut = ""
    if newest_holdout:
        stale_cut = str(
            (datetime.fromisoformat(newest_holdout) - timedelta(days=2))
            .date()
        )
    summary = []
    for _system, label, _transport, _backfillable in MEMBER_FAMILIES:
        seated = [r for r in rows if label in r["weights"]]
        candidacies = sum(label in r["candidates"] for r in rows)
        by_lead = {}
        for lead, _lead_label in LEAD_LABELS:
            lead_weights = [
                r["weights"][label] for r in seated
                if r["lead_bucket"] == lead
            ]
            by_lead[lead] = {
                "seats": len(lead_weights),
                "median_weight": (
                    median(lead_weights) if lead_weights else None
                ),
            }
        summary.append({
            "label": label,
            "seats": len(seated),
            "candidacies": candidacies,
            "eliminated": candidacies - len(seated),
            "median_weight": (
                median(r["weights"][label] for r in seated)
                if seated else None
            ),
            "by_lead": by_lead,
        })
    return {
        "rows": rows,
        "summary": summary,
        "scope_count": len(rows),
        "newest_holdout_end": newest_holdout or None,
        "stale_cut": stale_cut or None,
        "stale_count": sum(
            1 for r in rows if stale_cut and r["holdout_end"] < stale_cut
        ),
    }


def collect_center_width(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The two scalars that move the served distribution, per stage.

    ``delta_c`` slides the centre, ``width_lambda`` stretches or squeezes it.
    They are fitted per stage and must stay per stage: P0 sees a model blend,
    P1 sees that blend after the day's own observations have narrowed it, and
    one pair of numbers cannot be right for both.
    """

    if not _table_exists(conn, "calibration_center_width_table"):
        return []
    served = {
        str(stage): rest
        for stage, *rest in conn.execute(
            "SELECT stage, delta_c, width_lambda, fitted_at_utc,"
            " serving_day_count, evidence_json"
            " FROM calibration_center_width_table WHERE activated=1"
        )
    }
    journal: dict[str, list[Any]] = {}
    if _table_exists(conn, "calibration_center_width_fit_journal"):
        for stage, *rest in conn.execute(
            "SELECT stage, fitted_at_utc, outcome, delta_c, width_lambda,"
            " serving_day_count, serving_cell_count, forward_nll_gain"
            " FROM calibration_center_width_fit_journal"
            " ORDER BY fitted_at_utc DESC"
        ):
            journal.setdefault(str(stage), list(rest))
    rows = []
    for stage in ("P0", "P0-M", "P1"):
        active = served.get(stage)
        last = journal.get(stage)
        evidence: dict[str, Any] = {}
        if active:
            try:
                evidence = json.loads(active[4])
            except (ValueError, TypeError):
                evidence = {}
        outcome = str(last[1]) if last else ""
        if active:
            state, health = "已生效", "green"
        elif outcome == "NOT_WIRED":
            state, health = "未接线", "amber"
        elif outcome == "ACCUMULATING":
            state, health = "攒证据中", "amber"
        elif outcome == "REFUSED_FORWARD":
            state, health = "试算后更差，不采用", "green"
        else:
            state, health = "无记录", "amber"
        rows.append({
            "stage": stage,
            "meaning": STAGE_MEANING.get(stage, ""),
            "state": state,
            "health": health,
            "delta_c": active[0] if active else None,
            "width_lambda": active[1] if active else None,
            # Served scalars come from the active row; the evidence behind
            # them comes from the newest journal entry, because the active
            # row is only rewritten when the scalars change and would
            # otherwise report a fit age of however long they have held.
            "fitted_at_utc": str(last[0]) if last else (
                active[2] if active else None
            ),
            "fit_age_hours": _age_hours(
                str(last[0]) if last else (active[2] if active else None)
            ),
            "serving_days": (
                int(last[4]) if last
                else (int(active[3]) if active else 0)
            ),
            "serving_cells": (
                int(last[5]) if last
                else evidence.get("serving_cell_count")
            ),
            "forward_nll_gain": (
                float(last[6]) if last else evidence.get("forward_nll_gain")
            ),
            "min_forward_nll_gain": evidence.get("min_forward_nll_gain"),
            "last_outcome": outcome,
        })
    return rows


# Thresholds for the six-layer scorecard.  Every one of them is anchored to
# something the system already means, not to a number from a textbook:
#
# * temperature errors are counted in RUNGS, never in degrees.  The ladder is
#   not one width: a Fahrenheit book steps two degrees F per rung (1.11C) and
#   a Celsius book one degree C, and 35 of the 46 stations are Celsius.  A
#   fixed degree tolerance would hold the two books to different standards,
#   and a rung is also the thing being traded -- picking the bucket is the
#   decision, so "off by one rung" is the error that costs money.  Half a
#   rung of bias is where the centre starts landing on the wrong rung
#   systematically; a full rung is where it usually does.
# * probability scores are judged against the same climatology baseline the
#   OOS gate uses, because a raw CRPS is not comparable across stations.
# * spread is judged against realised error: a calibrated ensemble states an
#   uncertainty that matches the error it actually makes (spread-skill ~ 1).
#   Too narrow is overconfidence, too wide is an ensemble saying nothing.
# * ECE is in probability units, where 5% is the usual "well calibrated" bar.
# * bootstrap width is relative, because it answers only "do we have enough
#   days to trust the number above", not "is the number good".
SCORECARD_THRESHOLDS = {
    "bias_rungs": (0.3, 0.8),        # |bias| in market rungs, either book
    "mae_rungs": (1.0, 1.5),         # one rung / one and a half rungs
    "crps_skill": (0.10, 0.0),       # share better than climatology, higher better
    "brier_skill": (0.10, 0.0),      # ditto
    "pit_deviation": (0.04, 0.08),   # max |empirical - uniform| over PIT bins
    "ece": (0.05, 0.10),             # expected calibration error
    "conditional_gap": (0.05, 0.10),  # worst well-populated reliability bin
    "residual_correlation": (0.5, 0.75),   # max off-diagonal
    "weight_distance": (0.10, 0.25),       # L1 gap, served vs minimum variance
    "spread_skill": (0.20, 0.45),    # |ratio - 1|, ensemble spread vs realised error
    "ci_relative": (0.15, 0.30),     # bootstrap half-width / value
}


def _grade(value: float | None, key: str, *, higher_better: bool = False) -> str:
    """Green / amber / red for one metric, or grey when it is not measurable."""

    if value is None:
        return "muted"
    good, warn = SCORECARD_THRESHOLDS[key]
    if higher_better:
        return "green" if value >= good else ("amber" if value >= warn else "red")
    return "green" if value <= good else ("amber" if value <= warn else "red")


def _worst(healths: list[str]) -> str:
    for level in ("red", "amber", "green"):
        if level in healths:
            return level
    return "muted"


def _bucket_order(centers: dict[str, float]) -> list[str]:
    return sorted(centers, key=lambda key: centers[key])


def _truth_bucket(centers: dict[str, float], order: list[str], truth: float) -> str:
    """Which market rung the settled high actually landed on.

    The end rungs are open ("20C or below"), so the nearest centre is the
    right answer there too -- clamping falls out of the same comparison.
    """

    return min(order, key=lambda key: abs(centers[key] - truth))


def _prediction_scores(
    probability: dict[str, float],
    centers: dict[str, float],
    truth: float,
) -> dict[str, float] | None:
    """Every per-day quantity the six layers are built from."""

    order = _bucket_order(centers)
    if not order:
        return None
    total = sum(probability.get(key, 0.0) for key in order)
    if total <= 0:
        return None
    probabilities = [probability.get(key, 0.0) / total for key in order]
    values = [centers[key] for key in order]
    actual_key = _truth_bucket(centers, order, truth)
    actual_index = order.index(actual_key)

    # Rung width read off the station's own ladder rather than from the unit
    # column: 1.0C on a Celsius book, 1.11C on a Fahrenheit one, and correct
    # by construction for any ladder either book grows later.
    steps = sorted(
        values[index + 1] - values[index] for index in range(len(values) - 1)
    )
    rung_c = steps[len(steps) // 2] if steps else 1.0
    if rung_c <= 0:
        return None

    mean = sum(p * v for p, v in zip(probabilities, values))
    variance = sum(p * (v - mean) ** 2 for p, v in zip(probabilities, values))
    error = mean - truth

    # CRPS over the bucket ladder, and the Brier score across all rungs.
    crps = 0.0
    cumulative = 0.0
    for index, p in enumerate(probabilities):
        cumulative += p
        crps += (cumulative - (1.0 if index >= actual_index else 0.0)) ** 2
    brier = sum(
        (p - (1.0 if index == actual_index else 0.0)) ** 2
        for index, p in enumerate(probabilities)
    )
    # Randomised PIT, the standard fix for a discrete forecast: without the
    # within-bucket term every PIT would pile up on the bucket edges and a
    # perfectly calibrated forecast would look badly non-uniform.
    below = sum(probabilities[:actual_index])
    pit = below + 0.5 * probabilities[actual_index]
    # The reliability diagram is built from EVERY rung, not just the settled
    # one: each (promised probability, did it happen) pair is one observation.
    # Scoring only the settled rung would take a biased sample of high
    # probabilities and report a base rate of 1 instead of 1/rungs.
    reliability_pairs = [
        (p, 1.0 if index == actual_index else 0.0)
        for index, p in enumerate(probabilities)
    ]
    return {
        # Rung-denominated, so a Celsius and a Fahrenheit book average
        # together honestly.  The degree figures ride along for display only.
        "error": error / rung_c,
        "abs_error": abs(error) / rung_c,
        "square_error": (error / rung_c) ** 2,
        "error_c": error,
        "abs_error_c": abs(error),
        "square_error_c": error * error,
        "rung_c": rung_c,
        "crps": crps,
        "brier": brier,
        "pit": pit,
        "truth_probability": probabilities[actual_index],
        "peak_probability": max(probabilities),
        "spread_rungs": variance ** 0.5 / rung_c,
        "spread_c": variance ** 0.5,
        "reliability_pairs": reliability_pairs,
    }


def _bootstrap_half_width(
    values: list[float], draws: int = 600, seed: int = 7
) -> float | None:
    """Half-width of a 95% interval on the mean, for the stability layer."""

    if len(values) < 8:
        return None
    rng = random.Random(seed)
    count = len(values)
    means = []
    for _ in range(draws):
        means.append(
            sum(values[rng.randrange(count)] for _ in range(count)) / count
        )
    means.sort()
    low = means[int(0.025 * draws)]
    high = means[int(0.975 * draws)]
    return (high - low) / 2.0


def _reliability_decomposition(
    pairs: list[tuple[float, float]], bins: int = 10
) -> dict[str, float | list[dict[str, Any]]]:
    """Murphy's decomposition, on the probability given to the settled rung.

    Reliability is the gap between what was promised and what happened,
    resolution is how far the forecasts move away from the base rate, and the
    two only mean something together: a forecast that always says the base
    rate is perfectly reliable and completely useless.
    """

    if not pairs:
        return {"ece": None, "reliability": None, "resolution": None,
                "uncertainty": None, "bins": []}
    buckets: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for promised, happened in pairs:
        index = min(bins - 1, max(0, int(promised * bins)))
        buckets[index].append((promised, happened))
    base_rate = sum(happened for _p, happened in pairs) / len(pairs)
    total = len(pairs)
    ece = reliability = resolution = 0.0
    rows = []
    for index in sorted(buckets):
        group = buckets[index]
        weight = len(group) / total
        mean_promised = sum(p for p, _o in group) / len(group)
        mean_happened = sum(o for _p, o in group) / len(group)
        ece += weight * abs(mean_promised - mean_happened)
        reliability += weight * (mean_promised - mean_happened) ** 2
        resolution += weight * (mean_happened - base_rate) ** 2
        rows.append({
            "bin": f"{index * 100 // bins}-{(index + 1) * 100 // bins}%",
            "n": len(group),
            "promised": mean_promised,
            "happened": mean_happened,
        })
    return {
        "ece": ece,
        "reliability": reliability,
        "resolution": resolution,
        "uncertainty": base_rate * (1.0 - base_rate),
        "bins": rows,
    }


def collect_calibration_scorecard(conn: sqlite3.Connection) -> dict[str, Any]:
    """Six layers of calibration quality, each reduced to one verdict.

    The scores already existed in pieces across the page; what was missing
    was a judgement.  Every layer here answers "is this good" with a colour
    and a sentence, and keeps the number beside it as the evidence rather
    than the headline.
    """

    if not _table_exists(
        conn, "calibration_cross_native_ensemble_oos_prediction"
    ):
        return {"available": False}
    truth: dict[tuple[str, str], float] = {}
    for station, day, tmax in conn.execute(
        "SELECT station_id, local_date, official_tmax_c"
        " FROM calibration_station_daily_high_truth"
        " WHERE qc_status='ACCEPTED' ORDER BY revision_no"
    ):
        truth[(str(station), str(day))] = float(tmax)
    if not truth:
        return {"available": False}

    per_lead: dict[str, list[dict[str, float]]] = defaultdict(list)
    climate: dict[str, list[dict[str, float]]] = defaultdict(list)
    stations: set[str] = set()
    latest = ""
    for (
        station, day, lead, probabilities_json, centers_json, decided
    ) in conn.execute(
        "SELECT station_id, event_date, lead_bucket, probabilities_json,"
        " bucket_centers_c_json, decision_at_utc"
        " FROM calibration_cross_native_ensemble_oos_prediction"
        " WHERE event_date>=date('now','-45 days')"
        " ORDER BY decision_at_utc"
    ):
        actual = truth.get((str(station), str(day)))
        if actual is None:
            continue
        # Calendar leads (d0/d1/d2) were retired for hour buckets; rows under
        # the old vocabulary stop in mid-August and would drag stale scopes
        # into a board that is meant to read as current.
        if not str(lead).startswith("h"):
            continue
        try:
            probability = json.loads(probabilities_json)
            centers = {
                str(key): float(value)
                for key, value in json.loads(centers_json).items()
            }
        except (ValueError, TypeError, AttributeError):
            continue
        scores = _prediction_scores(probability, centers, actual)
        if scores is None:
            continue
        per_lead[str(lead)].append(scores)
        stations.add(str(station))
        latest = max(latest, str(decided))
        # The climatology comparison the gate uses, rebuilt on the same day:
        # a flat forecast over the same ladder is the "no skill" reference.
        order = _bucket_order(centers)
        flat = {key: 1.0 / len(order) for key in order}
        flat_scores = _prediction_scores(flat, centers, actual)
        if flat_scores is not None:
            climate[str(lead)].append(flat_scores)

    everything = [row for rows in per_lead.values() for row in rows]
    if not everything:
        return {"available": False}
    everything_climate = [row for rows in climate.values() for row in rows]

    def _mean(rows: list[dict[str, float]], key: str) -> float | None:
        values = [row[key] for row in rows]
        return sum(values) / len(values) if values else None

    def _skill(rows, base, key) -> float | None:
        ours, theirs = _mean(rows, key), _mean(base, key)
        if ours is None or theirs is None or theirs <= 0:
            return None
        return 1.0 - ours / theirs

    # ---- layer 1: point forecast --------------------------------------
    bias = _mean(everything, "error")
    mae = _mean(everything, "abs_error")
    rmse = (_mean(everything, "square_error") or 0.0) ** 0.5
    bias_c = _mean(everything, "error_c")
    mae_c = _mean(everything, "abs_error_c")
    rmse_c = (_mean(everything, "square_error_c") or 0.0) ** 0.5
    point_health = _worst([
        _grade(abs(bias) if bias is not None else None, "bias_rungs"),
        _grade(mae, "mae_rungs"),
    ])

    # ---- layer 2: probability -----------------------------------------
    crps_skill = _skill(everything, everything_climate, "crps")
    brier_skill = _skill(everything, everything_climate, "brier")
    pit_values = [row["pit"] for row in everything]
    pit_bins = [0] * 10
    for value in pit_values:
        pit_bins[min(9, max(0, int(value * 10)))] += 1
    pit_deviation = max(
        abs(count / len(pit_values) - 0.1) for count in pit_bins
    )
    probability_health = _worst([
        _grade(crps_skill, "crps_skill", higher_better=True),
        _grade(brier_skill, "brier_skill", higher_better=True),
        _grade(pit_deviation, "pit_deviation"),
    ])

    # ---- layer 3: calibration -----------------------------------------
    decomposition = _reliability_decomposition([
        pair for row in everything for pair in row["reliability_pairs"]
    ])
    ece = decomposition["ece"]
    # ECE alone hides the shape.  Four fifths of all (rung, day) pairs sit in
    # the 0-10% bin, where the gap is tiny, so a weighted average stays small
    # even when every confident forecast is off by ten points.  The worst
    # well-populated bin is what says whether a number can be acted on.
    populated = [
        row for row in decomposition["bins"] if int(row["n"]) >= 100
    ]
    conditional_gap = max(
        (abs(row["promised"] - row["happened"]) for row in populated),
        default=None,
    )
    conditional_signed = None
    if populated:
        worst = max(
            populated, key=lambda row: abs(row["promised"] - row["happened"])
        )
        conditional_signed = worst["happened"] - worst["promised"]
    calibration_health = _worst([
        _grade(ece, "ece"),
        _grade(conditional_gap, "conditional_gap"),
    ])

    # ---- layer 5: sharpness (read against realised error) --------------
    mean_spread = _mean(everything, "spread_rungs")
    mean_spread_c = _mean(everything, "spread_c")
    spread_skill = (
        mean_spread / rmse if mean_spread is not None and rmse > 0 else None
    )
    sharpness_health = _grade(
        abs(spread_skill - 1.0) if spread_skill is not None else None,
        "spread_skill",
    )

    # ---- layer 6: stability --------------------------------------------
    mae_ci = _bootstrap_half_width([row["abs_error"] for row in everything])
    crps_ci = _bootstrap_half_width([row["crps"] for row in everything])
    ci_relative = None
    if mae_ci is not None and mae:
        ci_relative = mae_ci / mae
    stability_health = _grade(ci_relative, "ci_relative")

    rows = []
    order = {name: index for index, (name, _label) in enumerate(LEAD_LABELS)}
    for lead in sorted(per_lead, key=lambda name: order.get(name, 9)):
        group = per_lead[lead]
        base = climate[lead]
        group_bias = _mean(group, "error")
        group_mae = _mean(group, "abs_error")
        group_rmse = (_mean(group, "square_error") or 0.0) ** 0.5
        group_spread = _mean(group, "spread_rungs")
        group_ratio = (
            group_spread / group_rmse
            if group_spread is not None and group_rmse > 0 else None
        )
        rows.append({
            "lead_bucket": lead,
            "lead_label": dict(LEAD_LABELS).get(lead, lead),
            "days": len(group),
            "bias_rungs": group_bias,
            "mae_rungs": group_mae,
            "rmse_rungs": group_rmse,
            "bias_c": _mean(group, "error_c"),
            "mae_c": _mean(group, "abs_error_c"),
            "crps_skill": _skill(group, base, "crps"),
            "brier_skill": _skill(group, base, "brier"),
            "spread_skill": group_ratio,
            "bias_health": _grade(
                abs(group_bias) if group_bias is not None else None,
                "bias_rungs",
            ),
            "mae_health": _grade(group_mae, "mae_rungs"),
            "crps_health": _grade(
                _skill(group, base, "crps"), "crps_skill", higher_better=True
            ),
            "spread_health": _grade(
                abs(group_ratio - 1.0) if group_ratio is not None else None,
                "spread_skill",
            ),
        })

    fusion = _collect_fusion_layer(conn)
    layers = [
        {
            "key": "point",
            "name": "点预测",
            "question": "中心到底准不准",
            "health": point_health,
            "verdict": _point_verdict(bias, mae),
            "detail": (
                f"偏差 {bias:+.2f} 档（{bias_c:+.2f}°C） · "
                f"平均误差 {mae:.2f} 档（{mae_c:.2f}°C） · "
                f"均方根 {rmse:.2f} 档（{rmse_c:.2f}°C）"
            ) if bias is not None and mae is not None else "证据不足",
        },
        {
            "key": "probability",
            "name": "概率预测",
            "question": "概率说得准不准",
            "health": probability_health,
            "verdict": _probability_verdict(crps_skill, pit_deviation),
            "detail": (
                f"CRPS 比气候好 {crps_skill:.0%} · "
                f"Brier 好 {brier_skill:.0%} · "
                f"PIT 最大偏离 {pit_deviation:.1%}"
            ) if crps_skill is not None and brier_skill is not None
            else "证据不足",
        },
        {
            "key": "calibration",
            "name": "校准",
            "question": "说 70% 是不是真的 70%",
            "health": calibration_health,
            "verdict": _calibration_verdict(
                decomposition, conditional_gap, conditional_signed
            ),
            "detail": (
                f"整体 ECE {ece:.1%} · 最差分箱偏离 {conditional_gap:.1%} · "
                f"分辨力 {decomposition['resolution']:.4f}"
            ) if ece is not None and conditional_gap is not None
            else "证据不足",
        },
        {
            "key": "fusion",
            "name": "模型融合",
            "question": "几家模型的权重分对了吗",
            "health": fusion["health"],
            "verdict": fusion["verdict"],
            "detail": fusion["detail"],
        },
        {
            "key": "sharpness",
            "name": "分布锐度",
            "question": "分布宽窄配不配它的准度",
            "health": sharpness_health,
            "verdict": _sharpness_verdict(spread_skill),
            "detail": (
                f"平均宽度 {mean_spread:.2f} 档（{mean_spread_c:.2f}°C） · "
                f"实际误差 {rmse:.2f} 档 · 宽度/误差 {spread_skill:.2f}"
            ) if spread_skill is not None else "证据不足",
        },
        {
            "key": "stability",
            "name": "稳定性",
            "question": "上面这些数字靠不靠得住",
            "health": stability_health,
            "verdict": _stability_verdict(ci_relative, len(everything)),
            "detail": (
                # mae_ci bootstraps `abs_error`, which is rung-denominated so
                # a Celsius and a Fahrenheit book average together honestly.
                # Labelling it °C read one rung as one degree, which is only
                # true on the 34 Celsius stations.
                f"{len(everything):,} 个站日 · 平均误差 95% 区间 ±{mae_ci:.3f} 档"
                f" · CRPS ±{crps_ci:.3f}"
            ) if mae_ci is not None and crps_ci is not None else "证据不足",
        },
    ]
    return {
        "available": True,
        "overall": _worst([layer["health"] for layer in layers]),
        "layers": layers,
        "rows": rows,
        "pit_bins": [count / len(pit_values) for count in pit_bins],
        "reliability_bins": decomposition["bins"],
        "fusion_pairs": fusion["pairs"],
        "sample_days": len(everything),
        "station_count": len(stations),
        "latest_decision_at_utc": latest or None,
        "latest_age_hours": _age_hours(latest or None),
    }


def _point_verdict(bias: float | None, mae: float | None) -> str:
    """All figures in rungs, so Celsius and Fahrenheit books read alike."""

    if bias is None or mae is None:
        return "还没有足够的已结算日可以判断"
    side = "偏热" if bias > 0 else "偏冷"
    if abs(bias) <= 0.3 and mae <= 1.0:
        return f"中心站得住：系统性{side}不到 {abs(bias):.2f} 档，平均误差在一档内"
    if abs(bias) > 0.8:
        return f"中心系统性{side} {abs(bias):.2f} 档，接近整整一档，会持续押错桶"
    if mae > 1.5:
        return f"单日平均差 {mae:.2f} 档，超过一档半，选桶方向常常不对"
    return f"中心略{side} {abs(bias):.2f} 档，平均误差 {mae:.2f} 档，尚在容忍范围"


def _probability_verdict(skill: float | None, pit: float | None) -> str:
    if skill is None or pit is None:
        return "还没有足够的已结算日可以判断"
    if skill < 0:
        return f"概率比直接用气候还差 {abs(skill):.0%}，这一层目前是负价值"
    head = f"比气候基准好 {skill:.0%}"
    if pit > 0.08:
        return f"{head}，但概率分布形状明显歪（PIT 偏离 {pit:.1%}），尾部不可信"
    if skill < 0.10:
        return f"{head}，优势很薄，接近没有信息"
    return f"{head}，且分布形状匀称，概率可以直接用"


def _calibration_verdict(
    decomposition: dict[str, Any],
    conditional_gap: float | None,
    conditional_signed: float | None,
) -> str:
    """Judged on the worst usable bin, not on the flattering average."""

    ece = decomposition["ece"]
    if ece is None or conditional_gap is None:
        return "还没有足够的已结算日可以判断"
    resolution = decomposition["resolution"] or 0.0
    reliability = decomposition["reliability"] or 0.0
    direction = (
        "说少了" if (conditional_signed or 0.0) > 0 else "说多了"
    )
    points = abs(conditional_signed or 0.0)
    if conditional_gap > 0.10:
        return (
            f"能下注的那一段不可信：概率高的时候{direction} "
            f"{points:.0%} 个百分点，整体 ECE {ece:.1%} 是被低概率段拉平的"
        )
    if conditional_gap > 0.05:
        return (
            f"低概率段准，但概率一上来就{direction} {points:.0%} 个百分点"
            f"——真要下注的那一段有系统性偏差"
        )
    if resolution <= reliability:
        return f"偏差小（{ece:.1%}），但分辨力不足——概率总在基准率附近晃"
    return f"说 70% 就真是 70%（各段偏离都在 {conditional_gap:.1%} 内），可以直接用"


def _sharpness_verdict(ratio: float | None) -> str:
    if ratio is None:
        return "还没有足够的已结算日可以判断"
    if abs(ratio - 1.0) <= 0.20:
        return f"宽度配得上准度（宽度/误差 {ratio:.2f}），既不吹牛也不含糊"
    if ratio < 0.55:
        return f"分布太窄（只有实际误差的 {ratio:.2f} 倍）——过度自信，尾部会被打脸"
    if ratio > 1.45:
        return f"分布太宽（实际误差的 {ratio:.2f} 倍）——过于保守，等于没给信息"
    if ratio < 1.0:
        return f"分布偏窄（{ratio:.2f} 倍），可以用但别压满"
    return f"分布偏宽（{ratio:.2f} 倍），信息量打了折扣"


def _stability_verdict(relative: float | None, days: int) -> str:
    if relative is None:
        return f"只有 {days} 个站日，还不足以给出置信区间"
    if relative <= 0.15:
        return f"{days:,} 个站日，上面的结论稳，重采样不会翻号"
    if relative <= 0.30:
        return f"{days:,} 个站日，结论大方向可信，但小差距（几个百分点）别当真"
    return f"只有 {days:,} 个站日，区间比数值本身还宽——上面每个判断都是暂定的"


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting, for an 8x8 at most."""

    size = len(vector)
    augmented = [list(matrix[row]) + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(
            range(column, size), key=lambda row: abs(augmented[row][column])
        )
        if abs(augmented[pivot][column]) < 1e-12:
            return None
        augmented[column], augmented[pivot] = (
            augmented[pivot], augmented[column]
        )
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column] / augmented[column][column]
            for term in range(column, size + 1):
                augmented[row][term] -= factor * augmented[column][term]
    return [augmented[row][size] / augmented[row][row] for row in range(size)]


def _minimum_variance_from_correlation(
    systems: list[str],
    correlation: dict[str, dict[str, float]],
    sigmas: list[float],
    shrinkage: float,
) -> dict[str, float] | None:
    """Minimum-variance shares from a stored correlation and per-system RMSE.

    Negative shares are refused rather than clipped, because a blend is a
    mixture of distributions and a negative share has no meaning in one.  The
    constrained optimum is found by enumerating which systems sit at zero,
    which is cheap at this cohort size.
    """

    size = len(systems)
    if size < 2 or size > 8:
        return None
    covariance = [[0.0] * size for _ in range(size)]
    for row, a in enumerate(systems):
        for column, b in enumerate(systems):
            try:
                value = float(correlation[a][b])
            except (KeyError, TypeError, ValueError):
                return None
            if row != column:
                value *= 1.0 - shrinkage
            covariance[row][column] = value * sigmas[row] * sigmas[column]

    best: tuple[float, dict[str, float]] | None = None
    for mask in range(1, 1 << size):
        active = [index for index in range(size) if mask & (1 << index)]
        if len(active) < 2:
            continue
        block = [[covariance[r][c] for c in active] for r in active]
        solution = _solve(block, [1.0] * len(active))
        if solution is None or any(value <= 0 for value in solution):
            continue
        total = sum(solution)
        if total <= 0:
            continue
        shares = [value / total for value in solution]
        variance = sum(
            shares[i] * shares[j] * block[i][j]
            for i in range(len(active))
            for j in range(len(active))
        )
        if best is None or variance < best[0]:
            best = (
                variance,
                {
                    systems[index]: shares[position]
                    for position, index in enumerate(active)
                },
            )
    if best is None:
        return None
    return {system: best[1].get(system, 0.0) for system in systems}


def _collect_fusion_layer(conn: sqlite3.Connection) -> dict[str, Any]:
    """How much the served weights differ from the covariance-aware optimum.

    Inverse-RMSE pays each system for being good alone, so two systems making
    the same mistake get paid twice for one piece of information.  The
    minimum-variance solution prices that overlap.  The gap between the two
    is what the measured correlations are worth -- and a high correlation
    with a small gap means the fleet is redundant but the weights already
    know it.
    """

    # The production solver takes per-day residuals, which this page does not
    # hold; what the SSOT stores is the finished correlation matrix.  The
    # covariance is recoverable from it -- S = D R D, with D the per-system
    # training RMSE -- so the optimum is solved here from the stored pieces
    # rather than re-deriving residuals.  Same shrinkage as production, and
    # the same refusal to clip negative shares.
    from bayes_basket_target.native_ensemble_oos import (  # noqa: PLC0415
        MINIMUM_VARIANCE_SHRINKAGE,
    )

    deviation: dict[tuple[str, str, str], float] = {}
    for station, lead, cycle, system, spread_json in conn.execute(
        "SELECT station_id, lead_bucket, run_cycle, ensemble_system_id,"
        " spread_parameters_json"
        " FROM calibration_native_ensemble_system_artifact"
    ):
        try:
            value = float(json.loads(spread_json)["train_rmse_c"])
        except (ValueError, KeyError, TypeError):
            continue
        if value > 0:
            deviation[(str(station), str(lead), str(system))] = value

    pairs: list[dict[str, Any]] = []
    correlations: list[float] = []
    distances: list[float] = []
    for station, lead, correlation_json, weights_json in conn.execute(
        "SELECT station_id, lead_bucket,"
        " cross_system_residual_correlation_json, selected_weights_json"
        " FROM calibration_cross_native_ensemble_selection_evidence"
        " WHERE selected_system_count>1 AND lead_bucket LIKE 'h%'"
        " ORDER BY rowid DESC LIMIT 400"
    ):
        try:
            correlation = json.loads(correlation_json)
            served = json.loads(weights_json)
        except (ValueError, TypeError):
            continue
        if not isinstance(correlation, dict) or len(correlation) < 2:
            continue
        systems = sorted(served)
        off_diagonal = [
            float(correlation[a][b])
            for a in systems for b in systems
            if a != b and a in correlation and b in correlation.get(a, {})
        ]
        if not off_diagonal:
            continue
        peak = max(off_diagonal)
        correlations.append(peak)
        sigmas = [
            deviation.get((str(station), str(lead), system))
            for system in systems
        ]
        optimum = None
        if all(sigma is not None for sigma in sigmas):
            optimum = _minimum_variance_from_correlation(
                systems, correlation, sigmas, MINIMUM_VARIANCE_SHRINKAGE
            )
        distance = None
        if optimum:
            distance = 0.5 * sum(
                abs(float(served.get(system, 0.0))
                    - float(optimum.get(system, 0.0)))
                for system in systems
            )
            distances.append(distance)
        pairs.append({
            "station_id": str(station),
            "lead_bucket": str(lead),
            "max_correlation": peak,
            "distance": distance,
            "served": served,
            "optimum": optimum,
        })

    if not correlations:
        return {
            "health": "muted",
            "verdict": "还没有两家以上模型同时在座的组合",
            "detail": "证据不足",
            "pairs": [],
        }
    mean_correlation = sum(correlations) / len(correlations)
    mean_distance = (
        sum(distances) / len(distances) if distances else None
    )
    health = _worst([
        _grade(mean_correlation, "residual_correlation"),
        _grade(mean_distance, "weight_distance"),
    ])
    if mean_distance is None:
        verdict = (
            f"模型之间残差相关 {mean_correlation:.2f}，"
            "但最优权重解不出来（多为单族占满）"
        )
    elif mean_correlation >= 0.75 and mean_distance > 0.25:
        verdict = (
            f"几家模型在犯同一个错（相关 {mean_correlation:.2f}），"
            f"而权重没有反映这件事（离最优 {mean_distance:.0%}）——重复计票"
        )
    elif mean_correlation >= 0.75:
        verdict = (
            f"模型高度重复（相关 {mean_correlation:.2f}），"
            f"但权重已经知道（离最优 {mean_distance:.0%}），暂时不亏"
        )
    elif mean_distance > 0.25:
        verdict = (
            f"模型互补性尚可（相关 {mean_correlation:.2f}），"
            f"但权重偏离协方差最优 {mean_distance:.0%}，有改进空间"
        )
    else:
        verdict = (
            f"几家模型互相独立（相关 {mean_correlation:.2f}），"
            f"权重也接近最优（差 {mean_distance:.0%}）"
        )
    return {
        "health": health,
        "verdict": verdict,
        "detail": (
            f"{len(correlations)} 个组合 · 平均最大相关 {mean_correlation:.2f}"
            + (f" · 离最小方差最优 {mean_distance:.0%}"
               if mean_distance is not None else "")
        ),
        "pairs": sorted(
            pairs,
            key=lambda row: (row["distance"] or 0.0),
            reverse=True,
        )[:12],
    }


def collect_gaps(
    conn: sqlite3.Connection,
    members: list[dict[str, Any]],
    stages: list[dict[str, Any]],
    tables: dict[str, Any],
) -> list[dict[str, Any]]:
    """Named holes, each with what it costs and whether it self-heals."""

    gaps = []
    for member in members:
        if member["health"] == "green":
            continue
        gaps.append({
            "severity": "high" if member["health"] == "red" else "medium",
            "what": f"{member['label']} 模型数据{member['note']}",
            "cost": (
                "该模型无法参与融合，少一路独立意见"
                + ("；此源无历史档案，丢一天永久丢一天"
                   if not member["backfillable"] else "")
            ),
            "self_heals": member["backfillable"],
        })
    # A family that arrives daily can still be near-useless to the fit if its
    # archive only goes back a few days -- the weight ramp prices it down and
    # the gate can refuse the combinations it appears in.
    deepest = max((m["training_days"] or 0) for m in members)
    for member in members:
        days = member["training_days"] or 0
        if deepest <= 0 or days >= deepest * 0.5:
            continue
        gaps.append({
            "severity": "medium",
            "what": (
                f"{member['label']} 的训练历史只有 {days} 天，"
                f"最深的同行有 {deepest} 天"
            ),
            "cost": (
                "样本浅的模型权重会被自动调低，参与融合的分量有限；"
                + ("可从公开档案回补" if member["backfillable"]
                   else "此源无历史档案，只能靠今后每天积累")
            ),
            "self_heals": True,
        })
    for stage in stages:
        if not stage.get("accumulating"):
            continue
        gaps.append({
            "severity": "low",
            "what": (
                f"{stage['stage']} 阶段校准仍在攒证据"
                f"（{stage['serving_days']}/{stage['min_days']} 天）"
            ),
            "cost": "该阶段暂时不做分档修正，按原样服务，不影响正确性",
            "self_heals": True,
        })
    if tables.get("weight_lag_scopes"):
        gaps.append({
            "severity": "medium",
            "what": (
                f"{tables['weight_lag_scopes']} 个站×提前量的融合权重"
                "落后于偏差订正 3 天以上"
            ),
            "cost": "这些格子的权重仍是旧证据算的，融合比例可能不是当前最优",
            "self_heals": True,
        })
    stations = conn.execute(
        "SELECT COUNT(*) FROM calibration_station_registry"
    ).fetchone()[0]
    taf = conn.execute(
        "SELECT COUNT(DISTINCT station_id)"
        " FROM calibration_native_ensemble_historical_member_run"
        " WHERE ensemble_system_id='OFFICIAL_AIRPORT_TAF_TX'"
    ).fetchone()[0]
    if taf < stations:
        gaps.append({
            "severity": "low",
            "what": f"{stations} 个站里只有 {taf} 个站的人工预报带温度组",
            "cost": "其余站的 P0-M 确认环节没有人工预报可用，是数据源本身的限制",
            "self_heals": False,
        })
    return gaps


def _daily_table(selection: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per event day: what was seated, and how it settled.

    Credible fills only.  A sub-one-cent basket is a minimum-tick offer with
    nothing bid against it, and letting those into a daily P&L makes every
    day they appear on unreadable.
    """

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selection.get("rows") or []:
        if row.get("dust_offer"):
            continue
        by_day[str(row["event_date"])].append(row)
    table = []
    for day in sorted(by_day, reverse=True):
        rows = by_day[day]
        # One row per chain: the settlement is joined onto every stage row
        # of its station-day, so summing settled stage rows counts one
        # chain's P&L two or three times.  Only the tail row is the chain.
        settled = [
            r for r in rows if r.get("settled") and r.get("chain_tail")
        ]
        hits = sum(int(r["hit"]) for r in settled)
        staked = sum(float(r["chain_stake_usdc"]) for r in settled)
        pnl = sum(float(r["pnl_usdc"] or 0.0) for r in settled)
        odds = [r["odds"] for r in settled if r.get("odds") is not None]
        claimed = (
            sum(float(r["win_probability"]) for r in settled) / len(settled)
            if settled else None
        )
        errors = [
            float(r["center_error_c"]) for r in settled
            if r.get("center_error_c") is not None
        ]
        table.append({
            "event_date": day,
            "station_count": len({r["station_id"] for r in rows}),
            "position_count": len(rows),
            "settled_count": len(settled),
            "sides": {
                side: sum(1 for r in rows if r["side"] == side)
                for side in ("above", "aligned", "below")
            },
            "mean_window_size": (
                sum(int(r["window_size"]) for r in rows) / len(rows)
                if rows else None
            ),
            # What the day posted against what it bought, in rungs.  The
            # window mean above counts filled rungs only, so a day that
            # posted three-rung ladders and was sold one of each reads as a
            # day of one-rung baskets -- which is a statement about the book,
            # not about what was chosen.
            "posted_leg_total": sum(
                int(r["entry"]["posted_leg_count"]) for r in rows
            ),
            "filled_leg_total": sum(
                int(r["entry"]["filled_leg_count"]) for r in rows
            ),
            "posted_leg_mean": (
                sum(int(r["entry"]["posted_leg_count"]) for r in rows)
                / len(rows) if rows else None
            ),
            "filled_leg_mean": (
                sum(int(r["entry"]["filled_leg_count"]) for r in rows)
                / len(rows) if rows else None
            ),
            "hit_rate": hits / len(settled) if settled else None,
            "claimed_win_probability_mean": claimed,
            "calibration_ratio": (
                (hits / len(settled)) / claimed
                if settled and claimed else None
            ),
            "odds_mean": sum(odds) / len(odds) if odds else None,
            "staked_usdc": staked,
            "pnl_usdc": pnl,
            "center_bias_c": (
                sum(errors) / len(errors) if errors else None
            ),
        })
    return table


def collect_window_divergence(conn: sqlite3.Connection) -> dict[str, Any]:
    """Station-days where the posterior and the held window part ways.

    The signal is the out-of-window posterior mass at the *last recompute
    where the held buckets were still alive* (mass on them above 5%) -- the
    last moment a top-up could still have acted.  A single daily maximum was
    tried first and rewarded the early morning's naturally broad posterior;
    the end-state is what the disagreement actually settled into.

    This is a disagreement detector, not a verdict.  Either side can be the
    wrong one, and both archetypes are real: RKPK 2026-08-21 held 29-32
    while the posterior (rightly) piled onto 33-34 and the day settled 34;
    KSEA 2026-08-17 held 78-79 while the model (wrongly) kept 76% at or
    above 80 through local midnight -- the hard bound only ever cuts from
    below, so a model stuck high is a wrongness no observation can remove.
    The settled column is what says who was right, station-day by
    station-day.
    """

    chains: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT station_id, event_date, decision_at_utc,"
        " window_buckets_json, leg_shares_json"
        " FROM paper_trade_position ORDER BY decision_at_utc"
    ):
        key = (str(row[0]), str(row[1]))
        entry = chains.setdefault(
            key, {"held": set(), "opened": str(row[2])}
        )
        legs = json.loads(row[4] or "{}")
        held = [
            bucket for bucket, shares in legs.items() if (shares or 0) > 0
        ] or json.loads(row[3])
        entry["held"].update(held)
    settled = {
        (str(row[0]), str(row[1])): {
            "hit": int(row[2]),
            "pnl": float(row[3]),
            "truth_bucket": str(row[4]),
        }
        for row in conn.execute(
            "SELECT station_id, event_date, hit, pnl_usdc, settled_bucket"
            " FROM paper_trade_settlement"
        )
    }
    rows = []
    for (station, day), entry in chains.items():
        outcome = settled.get((station, day))
        if outcome is None:
            continue
        late = None
        for row in conn.execute(
            "SELECT decision_at_utc, envelope_json"
            " FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            " AND decision_at_utc>=? ORDER BY decision_at_utc",
            (station, day, entry["opened"]),
        ):
            envelope = json.loads(row[1])
            probabilities = envelope["probabilities"]
            ordinals = envelope["bucket_ordinals"]
            inside = sum(
                value for bucket, value in probabilities.items()
                if bucket in entry["held"]
            )
            if inside <= 0.05:
                continue
            held_ordinals = [
                ordinals[bucket] for bucket in entry["held"]
                if bucket in ordinals
            ]
            if not held_ordinals:
                continue
            low, high = min(held_ordinals), max(held_ordinals)
            late = {
                "at": str(row[0]),
                "out": 1.0 - inside,
                "out_above": sum(
                    value for bucket, value in probabilities.items()
                    if ordinals.get(bucket, low) > high
                ),
                "out_below": sum(
                    value for bucket, value in probabilities.items()
                    if ordinals.get(bucket, high) < low
                ),
            }
        if late is None:
            continue
        rows.append({
            "station_id": station,
            "event_date": day,
            "held_buckets": sorted(entry["held"]),
            "late_out": late["out"],
            "out_above": late["out_above"],
            "out_below": late["out_below"],
            "measured_at_utc": late["at"],
            **outcome,
        })
    thresholds = []
    for threshold in (0.3, 0.4, 0.5):
        hot = [row for row in rows if row["late_out"] > threshold]
        cold = [row for row in rows if row["late_out"] <= threshold]
        if not hot or not cold:
            continue
        thresholds.append({
            "threshold": threshold,
            "hot_n": len(hot),
            "hot_hit_rate": sum(r["hit"] for r in hot) / len(hot),
            "hot_pnl": sum(r["pnl"] for r in hot),
            "cold_n": len(cold),
            "cold_hit_rate": sum(r["hit"] for r in cold) / len(cold),
            "cold_pnl": sum(r["pnl"] for r in cold),
        })
    flagged = sorted(
        (row for row in rows if row["late_out"] > 0.4),
        key=lambda row: (row["event_date"], row["late_out"]),
        reverse=True,
    )
    return {
        "evaluated_station_days": len(rows),
        "thresholds": thresholds,
        "flagged": flagged[:80],
    }


def build_payload(db: Path) -> dict[str, Any]:
    conn = _connect(db)
    members = collect_members(conn, collect_run_cadence(conn))
    tables = collect_correction_and_weights(conn)
    stages = collect_band_stages(conn)
    chain = collect_chain(conn)
    gaps = collect_gaps(conn, members, stages, tables)
    p0m = collect_p0m_progress(conn)
    skill = collect_model_skill(conn)
    truth = collect_truth_materialization(conn)
    scores = collect_oos_scores(conn)
    serving_weights = collect_serving_weights(conn)
    center_width = collect_center_width(conn)
    scorecard = collect_calibration_scorecard(conn)
    worker = {}
    if STATUS_JSON.is_file():
        try:
            status = json.loads(STATUS_JSON.read_text())
            worker = {
                "heartbeat_at": status.get("heartbeat_at"),
                "heartbeat_age_hours": _age_hours(
                    status.get("heartbeat_at")
                ),
                "pid": status.get("pid"),
                "live_submit_authorized": status.get(
                    "live_submit_authorized"
                ),
            }
        except (ValueError, OSError):
            worker = {}
    healths = (
        [m["health"] for m in members]
        + [c["health"] for c in chain]
        + [s["health"] for s in stages]
        + [truth["health"]]
    )
    overall = (
        "red" if "red" in healths
        else ("amber" if "amber" in healths else "green")
    )
    selection = build_selection_payload(db)
    return {
        "schema": "calibration_dashboard_v2",
        "generated_at_utc": _now().isoformat().replace("+00:00", "Z"),
        "overall_health": overall,
        "selection": selection,
        "daily": _daily_table(selection),
        "members": members,
        "tables": tables,
        "stages": stages,
        "chain": chain,
        "gaps": gaps,
        "p0m": p0m,
        "skill": skill,
        "truth": truth,
        "scores": scores,
        "serving_weights": serving_weights,
        "center_width": center_width,
        "scorecard": scorecard,
        "window_divergence": collect_window_divergence(conn),
        "worker": worker,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>校准与链路看板</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--soft:rgba(38,43,54,.6);
--text:#e6e9ef;--muted:#9aa4b8;--green:#3fb950;--amber:#d29922;--red:#f85149;
--accent:#58a6ff;--up:#7ee787;--down:#e3b341;--violet:#d2a8ff;
--pill-green-bg:rgba(63,185,80,.14);--pill-green-fg:#7ee787;
--pill-amber-bg:rgba(210,153,34,.16);--pill-amber-fg:#e3b341;
--pill-red-bg:rgba(248,81,73,.15);--pill-red-fg:#ff7b72;
--pill-violet-bg:rgba(188,140,255,.16);
--why-bg:rgba(88,166,255,.07);--why-fg:#c9d4e5;--bar-bg:#232833;
--shadow:none}
/* The light palette is not the dark one inverted: on a white ground the
   same hues read washed out and the greens stop being distinguishable from
   the ambers, so each accent is darkened to its own readable weight. */
[data-theme="light"]{--bg:#f6f7f9;--card:#ffffff;--line:#e1e4ea;
--soft:rgba(140,149,165,.28);--text:#1c2029;--muted:#5b6472;--green:#1a7f37;
--amber:#9a6700;--red:#cf222e;--accent:#0969da;--up:#1a7f37;--down:#9a6700;
--violet:#8250df;
--pill-green-bg:rgba(26,127,55,.11);--pill-green-fg:#116329;
--pill-amber-bg:rgba(154,103,0,.13);--pill-amber-fg:#7d4e00;
--pill-red-bg:rgba(207,34,46,.10);--pill-red-fg:#a40e26;
--pill-violet-bg:rgba(130,80,223,.12);
--why-bg:rgba(9,105,218,.06);--why-fg:#2b3648;--bar-bg:#e6e9ef;
--shadow:0 1px 2px rgba(27,31,36,.06)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 72px}
h1{font-size:23px;margin:0 0 4px}
h2{font-size:16px;margin:22px 0 4px;display:flex;align-items:center;gap:8px}
.sub{color:var(--muted);font-size:13px;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);
border-radius:9px;padding:10px 12px;margin-bottom:8px;box-shadow:var(--shadow)}
.grid{display:grid;gap:8px}
.g4{grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none}
.green{background:var(--green)}.amber{background:var(--amber)}
.red{background:var(--red)}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;
border:1px solid var(--line);color:var(--muted)}
.pill.green{background:var(--pill-green-bg);color:var(--pill-green-fg);
border-color:transparent}
.pill.amber{background:var(--pill-amber-bg);color:var(--pill-amber-fg);
border-color:transparent}
.pill.red{background:var(--pill-red-bg);color:var(--pill-red-fg);
border-color:transparent}
/* The station-day's outcome, on the cell that names the station: hit,
   miss, still open, never opened.  Four states that a reader otherwise has
   to reconstruct by reading three stage cells and a P&L column. */
td.st{border-left:3px solid transparent}
td.st-hit{background:var(--pill-green-bg);border-left-color:var(--green)}
td.st-miss{background:var(--pill-red-bg);border-left-color:var(--red)}
td.st-open{background:var(--pill-amber-bg);border-left-color:var(--amber)}
td.st-flat{background:var(--why-bg);border-left-color:var(--accent)}
/* The fold that hides the prose.  A pill rather than a triangle, because it
   sits under a heading where a bare disclosure arrow reads as an artifact. */
details.note>summary{color:var(--muted);font-size:12px;cursor:pointer;
list-style:none;display:inline-block;border:1px solid var(--line);
border-radius:99px;padding:1px 10px;margin:2px 0 6px;user-select:none}
details.note>summary::-webkit-details-marker{display:none}
details.note>summary:hover{color:var(--text)}
details.note[open]>summary{margin-bottom:8px}
/* The strip at the top.  Eight numbers, each one a question the rest of
   the page answers at length, coloured so a bad one is found without
   reading any of them. */
/* Four across, two rows.  Eight in a row does not fit the content width
   and lands as seven plus a widow. */
.kpi{display:grid;gap:7px;grid-template-columns:repeat(auto-fit,minmax(250px,1fr))}
.kpi .k{border:1px solid var(--line);border-radius:9px;padding:8px 10px;
background:var(--card);border-left-width:3px;border-left-style:solid}
.kpi .k.green{border-left-color:var(--green)}
.kpi .k.amber{border-left-color:var(--amber)}
.kpi .k.red{border-left-color:var(--red)}
.kpi .k.muted{border-left-color:var(--line)}
.kpi .k .lab{color:var(--muted);font-size:11.5px}
.kpi .k .val{font-size:20px;font-weight:650;letter-spacing:-.3px;
line-height:1.25;margin-top:1px}
.kpi .k .sub2{color:var(--muted);font-size:11.5px;margin-top:1px}
.kpi .k.red .val{color:var(--red)}
.kpi .k.amber .val{color:var(--amber)}
.big{font-size:19px;font-weight:650;letter-spacing:-.3px}
.muted{color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--muted);font-weight:500;font-size:12.5px;
padding:4px 8px;border-bottom:1px solid var(--line);user-select:none}
th:hover{color:var(--text)}
th[data-sorted="1"]{color:var(--accent)}
th[data-sort-dir="asc"]::after{content:" ↑"}
th[data-sort-dir="desc"]::after{content:" ↓"}
td{padding:5px 8px;border-bottom:1px solid var(--soft);
vertical-align:top}
tr:last-child td{border-bottom:none}
.scroll{max-height:520px;overflow:auto}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.bar{height:6px;border-radius:99px;background:var(--bar-bg);overflow:hidden;
margin-top:6px}
.bar>i{display:block;height:100%;background:var(--accent)}
.chain{display:flex;flex-direction:column;gap:0}
.link{display:flex;gap:10px;align-items:flex-start;padding:6px 0;
border-bottom:1px solid var(--soft)}
.link:last-child{border-bottom:none}
.link .body{flex:1;min-width:0}
.link .name{font-weight:600}
.why{background:var(--why-bg);border-left:2px solid var(--accent);
padding:9px 13px;border-radius:0 7px 7px 0;color:var(--why-fg);font-size:13.4px;
margin:9px 0 0}
.gap{display:flex;gap:9px;padding:6px 0;border-bottom:1px solid
var(--soft);align-items:flex-start}
.gap:last-child{border-bottom:none}
.factor{font-weight:650}
.up{color:var(--up)}.down{color:var(--down)}.flat{color:var(--muted)}
footer{margin-top:34px;color:var(--muted);font-size:12.5px;text-align:center}
/* The group switcher.  Sticky, because it is the only way back to the
   other two thirds of the page once a long section has been scrolled
   into. */
.tabs{position:sticky;top:0;z-index:8;display:flex;gap:6px;flex-wrap:wrap;
align-items:center;margin:14px 0 0;padding:9px 0 8px;background:var(--bg);
border-bottom:1px solid var(--line)}
.tabs button{cursor:pointer;background:var(--card);color:var(--muted);
border:1px solid var(--line);border-radius:99px;padding:5px 13px;
font:13px/1.35 inherit;display:flex;align-items:center;gap:6px}
.tabs button:hover{color:var(--text);border-color:var(--accent)}
.tabs button[data-active="1"]{color:var(--text);border-color:var(--accent);
background:var(--why-bg)}
.tabs .muted{font-size:11.5px}
.grouptitle{display:flex;gap:10px;flex-wrap:wrap;align-items:baseline;
margin:18px 0 0;font-weight:650}
.grouptitle .muted{font-weight:400}
#theme{position:fixed;top:14px;right:16px;z-index:9;cursor:pointer;
background:var(--card);color:var(--muted);border:1px solid var(--line);
border-radius:99px;padding:6px 13px;font:13px/1 inherit;
box-shadow:var(--shadow)}
#theme:hover{color:var(--text);border-color:var(--accent)}
@media(max-width:820px){#theme{position:static;margin:14px 20px 0}}
</style>
<script>
// Before first paint, so a light-mode reader does not get a dark flash.
// The stored choice wins over the system one: someone who has clicked the
// button has said something the OS setting cannot override.
(function(){
  try{
    var saved = localStorage.getItem("board-theme");
    var dark = window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme =
      saved || (dark ? "dark" : "light");
  }catch(e){ document.documentElement.dataset.theme = "dark"; }
})();
</script>
</head><body>
<button id="theme" type="button"></button>
<div class="wrap" id="app"></div>
<script>
const D = __PAYLOAD__;
const h = (s) => String(s == null ? "" : s);
const hours = (v) => v == null ? "—" :
  (v < 1 ? Math.round(v * 60) + " 分钟前" :
   v < 48 ? v.toFixed(1) + " 小时前" : (v / 24).toFixed(1) + " 天前");
const OVERALL = {green:["运转正常","所有环节都在按预期更新"],
  amber:["有需要留意的地方","不影响出数，但有环节慢了或在攒数据"],
  red:["有环节断了","下面标红的环节需要处理"]};

function head(){
  const [t, s] = OVERALL[D.overall_health] || OVERALL.amber;
  const w = D.worker || {};
  return `<h1>校准与链路看板</h1>
  <p class="sub">这一页回答四件事：模型数据还在不在进来、校准有没有按时更新、
  哪些修正已经生效、哪里还缺数据。</p>
  <div class="card"><div style="display:flex;gap:14px;align-items:center;
    flex-wrap:wrap">
    <span class="dot ${D.overall_health}" style="width:13px;height:13px"></span>
    <span class="big">${t}</span>
    <span class="muted">${s}</span>
    <span style="flex:1"></span>
    <span class="muted">常驻进程心跳 ${hours(w.heartbeat_age_hours)}
      ${w.pid ? "· pid " + w.pid : ""}</span>
  </div></div>`;
}

// The top strip: one number per question the page spends a section
// answering.  Ordered the way a reader triages -- is the data coming in, is
// it being turned into positions, are the positions any good -- and
// coloured so a bad one is found without reading the others.
//
// Every number here is read from the same payload the sections below use.
// Restating a figure would let the strip and the section it summarises
// disagree, which is worse than not having the strip.
function overview(){
  const S=D.selection||{};
  const pct0=(v)=>v==null?"—":(v*100).toFixed(0)+"%";
  const band=(v,good,ok)=>v==null?"muted"
    :(v>=good?"green":v>=ok?"amber":"red");
  const cards=[];
  const push=(cls,lab,val,sub2,title)=>cards.push(
    `<div class="k ${cls}" title="${h(title||"")}">
      <div class="lab">${lab}</div><div class="val">${val}</div>
      <div class="sub2">${sub2}</div></div>`);

  // 1. Are the four model families still arriving.
  const fresh=(D.members||[]).filter(m=>m.health==="green").length;
  const total=(D.members||[]).length;
  // Which family is furthest from its last run.  The count alone says how
  // many are fine; the reader's next question is always how stale the
  // stalest one is, and that is one number.
  const laggard=(D.members||[]).slice().sort((a,b)=>
    (b.run_age_hours==null?1e9:b.run_age_hours)
    -(a.run_age_hours==null?1e9:a.run_age_hours))[0];
  push(fresh>=total?"green":fresh>=total-1?"amber":"red",
    "模型在进来", `${fresh}/${total}`,
    laggard?`最旧一家 ${h(laggard.label)} · ${
      hours(laggard.run_age_hours)}的 run`:"没有成员族",
    "第一节：四家集合预报的到货情况");

  // 2. Is anything blocking the chain right now.
  const blocking=(D.gaps||[]).filter(g=>g.severity==="high").length;
  const waiting=(D.gaps||[]).filter(g=>g.severity!=="high").length;
  // Only a blocking gap is a problem.  Colouring the strip amber because
  // some scope is still accumulating samples makes the strip amber almost
  // always, and a light that is always on is not a light.
  push(blocking?"red":"green",
    "要处理的缺口", String(blocking),
    waiting?`另有 ${waiting} 条在正常等待`:"没有待处理项",
    "第十八节：数据缺口与健康检查");

  // 3. Did the truth for settled days actually land.
  const outstanding=(D.truth||{}).outstanding;
  push(outstanding==null?"muted":outstanding===0?"green"
      :outstanding<=5?"amber":"red",
    "真值待物化", outstanding==null?"—":String(outstanding),
    "结算真值还没落库的站日", "第十九节：真值物化健康度");

  // 4. How much of the fleet actually opened a position on the newest day.
  const day=S.latest_day||(S.roster_event_dates||[])[0];
  const roster=(S.station_roster||[]).filter(e=>e.event_date===day);
  const opened=roster.filter(e=>e.stages&&e.stages.P0).length;
  const rate=roster.length?opened/roster.length:null;
  push(band(rate,0.5,0.25), "建仓率",
    `${opened}/${roster.length||"—"}`,
    `${h(day||"")} · ${pct0(rate)} 的站建了仓`,
    "第十二节：没建仓的站，格子里写着原因");

  // 5. Of what was posted, how much the book actually sold us.  The
  // target is 90% on every stage; the card shows the worst one, because
  // the strip is for finding the number that needs attention.
  const p0=(S.resting_entry||{}).stages||[];
  const worstFill=p0.reduce((worst,s)=>{
    const v=s.closed_share_fill_rate!=null?s.closed_share_fill_rate
      :s.share_fill_rate!=null?s.share_fill_rate:s.rung_completion_rate;
    return v!=null&&(worst.v==null||v<worst.v)?{v,stage:s.stage}:worst;
  },{v:null,stage:null});
  push(band(worstFill.v,0.8,0.7), "挂单成交率", pct0(worstFill.v),
    worstFill.stage?`最低的阶段 ${h(worstFill.stage)} · 底线每段 ≥80%`:"—",
    "第十五节：EV 优先，成交率是守底线的诊断指标");

  // 6. Does the model's stated probability survive contact with the truth.
  const cs=S.coverage_stats||{};
  const stagesAll=S.stages||{};
  const cal=((stagesAll.P0||{}).credible||{}).calibration_ratio;
  push(cal==null?"muted":(cal>=0.85&&cal<=1.15)?"green"
      :(cal>=0.6&&cal<=1.4)?"amber":"red",
    "说得准不准", cal==null?"—":cal.toFixed(2)+"×",
    "命中率 ÷ 声称胜率，1.00× 是说得准",
    "第六节与第十节：概率分档校准");

  // 6b. Does the basket we CHOSE cover the truth, at the widths the
  // charter actually seats -- and is that moving.  Grouped on the posted
  // ladder, not the filled one: the market sells us part of a three-rung
  // choice and the filled width would file that day under one rung,
  // emptying the three-rung row of exactly its hardest cases.
  //
  // The trend is the later half of settlements against the earlier, split
  // where the counts are even rather than at the middle date.  A direction
  // is only printed when it clears two combined standard errors; under
  // that it is a coin landing differently and saying "worse" would invent
  // a signal the sample cannot carry.
  ((S.posted_basket_hit||{}).sizes||[]).forEach(row=>{
    const hitv=row.hit_rate;
    const arrow=row.delta==null?""
      :!row.delta_is_readable?"→"
      :row.delta>0?"↑":"↓";
    const move=row.delta==null?`${row.settled_count} 笔 · 趋势样本不足`
      :!row.delta_is_readable
        ?`${row.settled_count} 笔 · 与噪音不可分（Δ${
          (row.delta*100>=0?"+":"")+(row.delta*100).toFixed(0)}，需 ±${
          (2*row.delta_stderr*100).toFixed(0)}）`
      :`${row.settled_count} 笔 · 后半 ${
        (row.delta*100>=0?"+":"")+(row.delta*100).toFixed(0)} 点`;
    push(hitv==null?"muted":band(hitv,0.5,0.33),
      `${row.posted_size} 桶命中 ${arrow}`,
      pct0(hitv), move,
      `按挂出的篮子算。买到的篮子只中 ${pct0(row.filled_hit_rate)}` +
      `，差额是市场没把会赢的那条腿卖给我们。声称 ${
      pct0(row.claimed_mean)}，实现/声称 ${
      row.realised_over_claimed==null?"—"
        :row.realised_over_claimed.toFixed(2)}×`);
  });

  // 7. What the one book is worth now.
  const capital=S.initial_capital_usdc||1000;
  const equity=capital+(cs.realized_usdc||0);
  push(equity>=capital?"green":equity>=capital*0.85?"amber":"red",
    "纸面权益", equity.toFixed(0),
    `本金 ${capital} · ${h(S.stats_start_event_date||"")} 起 ${
      cs.settled_count||0} 条链`,
    "一本账：P0 建仓，P0-M 与 P1 加仓，一个站日结算一次");

  // 8. Per USDC put at risk, what came back.
  const roi=cs.staked_usdc>0?(cs.realized_usdc||0)/cs.staked_usdc:null;
  push(roi==null?"muted":roi>=0?"green":roi>=-0.15?"amber":"red",
    "回报率", roi==null?"—":(roi*100).toFixed(1)+"%",
    `${h(S.stats_start_event_date||"")} 起 · 投入 ${
      (cs.staked_usdc||0).toFixed(0)} · 实得 ${
      (cs.realized_usdc||0).toFixed(0)}`,
    "第十节：覆盖 · 价格 · EV");

  return `<div class="kpi" style="margin:10px 0 4px">${cards.join("")}</div>`;
}


// A run stamp is the one timestamp on this page read as an identity rather
// than as a moment: 06Z says which cycle of the day the model ran, and two
// families are comparable only cycle against cycle.
const runLabel = (s) => {
  const m = String(s == null ? "" : s).match(/^\d{4}-(\d{2})-(\d{2})T(\d{2})/);
  return m ? `${m[1]}-${m[2]} ${m[3]}Z` : "—";
};
const cycleLabel = (s) => {
  const m = String(s == null ? "" : s).match(/T(\d{2})/);
  return m ? `${m[1]}Z` : "—";
};

function members(){
  // The question asked of this section is always the same one: is each model
  // still running, and when did its last run land.  So the newest run is the
  // headline and the archive depth is the footnote -- the depth moves by one
  // a day and is read once a week, the run moves every few hours and is read
  // every time the page is opened.
  const rows = D.members.map(m => {
    // Same threshold the health dot uses: late by a whole cycle is late
    // enough to say out loud, and publication times wander by hours below
    // that.
    const late = m.overdue_hours != null && m.overdue_hours > m.cycle_hours;
    const span = (v) => hours(v).replace("前", "");
    const due = m.due_in_hours == null ? ""
      : late ? `<span class="down">下一轮已晚 ${span(m.overdue_hours)}</span>`
      : m.due_in_hours >= 0 ? `下一轮约 ${span(m.due_in_hours)}后到`
      : "下一轮随时到";
    // Four runs, two per line: enough to see a dropped cycle, short enough
    // that every card spends the same number of lines on it and the last
    // line of all four cards sits on one baseline.
    const seq = (m.runs_recent || []).slice(0, 4).map(r =>
      `<span style="white-space:nowrap">${cycleLabel(r.run_utc)}·${
        r.stations}站</span>`).join("&nbsp; ");
    return `<div class="card" title="${h(m.note)}${m.cycle_hours ?
      ` · 观察到 ${m.cycle_hours.toFixed(0)} 小时一轮，发布后约 ${
        m.publish_lag_hours.toFixed(0)} 小时落库` : ""}">
    <div style="display:flex;align-items:center;gap:7px">
      <span class="dot ${m.health}"></span><b>${h(m.label)}</b>
      <span style="flex:1"></span>
      ${m.backfillable ? "" :
        `<span class="pill amber" title="此来源不留历史档案，当天没抓到就永久缺失">不可回填</span>`}
      <span class="pill ${m.health}">${m.seats_won} 次入选</span>
    </div>
    <div style="display:flex;align-items:baseline;gap:7px;margin-top:6px">
      <span class="big">${runLabel(m.latest_run_utc)}</span>
      <span class="muted">${hours(m.run_age_hours)}发布</span>
    </div>
    <div class="muted" style="margin-top:2px">${
      hours(m.latest_capture_age_hours)}落库${
      m.latest_run_stations == null ? "" :
        ` · 这轮 ${m.latest_run_stations} 站`}</div>
    <div class="muted" style="margin-top:2px">${due || "&nbsp;"}</div>
    <div class="mono muted" style="margin-top:5px;min-height:2.6em"
      title="最近 ${m.runs_window_days} 天入库的 run：轮次与该轮覆盖的站数">${
      seq || "近日无 run 入库"}</div>
    <div class="muted" style="margin-top:4px">存档 ${m.training_days} 天 ·
      ${m.stations} 站 · ${(m.member_rows/1000).toFixed(0)}k 条成员预报</div>
  </div>`;
  }).join("");
  return `<h2>一、四家模型的数据在不在进来</h2>
  <p class="sub">大字是这家机构最新一次 run 的轮次（UTC），后面是它多久前发布、
  多久前落到我们库里、这一轮覆盖了几个站，以及按它自己观察到的节奏下一轮该什么时候到。
  灰色那行是最近几轮的轮次与站数，中间断一档就是漏了一轮。
  "入选次数"是这个模型被选进最终融合的次数——没入选不代表坏，
  只代表那天它不是最优组合的一员。悬停卡片看这家的发布节奏与到货延迟。</p>
  <div class="grid g4">${rows}</div>`;
}

function chain(){
  // A table, not a stack of two-line blocks.  The links are read down the
  // column -- "which one stopped" -- and stacked blocks put four times the
  // vertical space between the answers.
  const rows = D.chain.map(c => `<tr>
    <td><span class="dot ${c.health}"></span> <b>${h(c.name)}</b></td>
    <td class="muted">${h(c.plain)}</td>
    <td class="mono">${h(c.volume)}</td>
    <td class="muted">${h(c.note)}</td>
  </tr>`).join("");
  return `<h2>十七、整条链路每一环是否在动</h2>
  <p class="sub">数据从上到下流：模型下载 → 真值结算 → 算校正 → 算概率修正 →
  出概率 → 盘中更新。任何一环停了，它下面的环节迟早会跟着停。</p>
  <div class="card"><div class="scroll"><table><thead><tr>
    <th>环节</th><th>在做什么</th><th>量</th><th>状态</th>
  </tr></thead><tbody>${rows}</tbody></table></div></div>`;
}

function tables(){
  const t = D.tables;
  return `<h2>二、每站的偏差订正与融合权重</h2>
  <p class="sub">这两张表是"每个模型在每个站偏多少度"和"融合时各模型占多大比重"，
  每天自动重算、自动换代。</p>
  <div class="grid g3">
    <div class="card"><div class="muted">偏差订正表</div>
      <div class="big">${t.correction.stations} 站</div>
      <div class="muted">${t.correction.activations.toLocaleString()} 次激活 ·
        最近更新 ${hours(t.correction.age_hours)}</div></div>
    <div class="card"><div class="muted">融合权重表</div>
      <div class="big">${t.weights.stations} 站</div>
      <div class="muted">${t.weights.activations.toLocaleString()} 次激活 ·
        最近更新 ${hours(t.weights.age_hours)}</div></div>
    <div class="card"><div class="muted">权重落后订正的格子</div>
      <div class="big ${t.weight_lag_scopes ? "down" : ""}">
        ${t.weight_lag_scopes}</div>
      <div class="muted">落后 3 天以上的站×提前量组合；
        权重要过检验才换代，持续不过就会停在旧版本</div></div>
  </div>`;
}

function stages(){
  if(!D.stages.length) return "";
  const cards = D.stages.map(s => {
    const pct = s.accumulating && s.min_days ?
      Math.min(100, 100 * s.serving_days / s.min_days) : 100;
    const bands = (s.bands || []).map(b => {
      const f = b.factor;
      const cls = f > 1.001 ? "up" : (f < 0.999 ? "down" : "flat");
      const arrow = f > 1.001 ? "调高" : (f < 0.999 ? "调低" : "不动");
      const measured = b.measured_ratio == null ? "—" :
        b.measured_ratio.toFixed(2) + "×";
      // A live factor explains how it got in; only an identity factor
      // explains what is holding it back.
      const basis = b.basis || "";
      const why = Math.abs(f - 1) > 1e-9
        ? (basis === "gate_transfer"
            ? "本阶段样本还不够，但另一套证据独立测出同方向，已生效"
            : "证据充分，已生效")
        : (b.blocked_by === "interval_spans_one" ? "统计上还分不清是真偏还是巧合" :
           b.blocked_by === "sign_flips_between_halves" ? "前后半段偏的方向相反，不稳定" :
           basis.includes("accumulating") ? "样本还不够，先不动" :
           basis.includes("forward") ? "试算后反而变差，不采用" :
           basis.includes("no_sample") ? "这一档还没有样本" : "暂不修正");
      const upper = Math.min(100, b.upper * 100);
      return `<tr>
        <td class="mono">${(b.lower*100).toFixed(0)}–${upper.toFixed(0)}%</td>
        <td>${measured}</td>
        <td class="factor ${cls}">${f.toFixed(3)} <span class="muted">${arrow}</span></td>
        <td class="muted">${why}</td></tr>`;
    }).join("");
    const journal = (s.journal || []).slice(0, 5).map(j =>
      `<tr><td class="mono">${h(j.at).slice(0,16).replace("T"," ")}</td>
       <td>${j.outcome === "ACTIVATED" ? "换新版本" :
            j.outcome === "UNCHANGED" ? "与上次相同" : "攒证据中"}</td>
       <td>${j.days} 天</td><td>${j.cells} 档生效</td>
       <td class="mono muted">${h(j.sha)}</td></tr>`).join("");
    return `<div class="card">
      <div style="display:flex;align-items:center;gap:8px">
        <span class="dot ${s.health}"></span><b>${h(s.stage)}</b>
        <span class="muted">${h(s.meaning)}</span>
        <span style="flex:1"></span>
        <span class="pill ${s.health}">${h(s.state)}</span>
      </div>
      <div class="muted" style="margin-top:8px">
        证据：${s.serving_days} 天 / ${s.station_days || 0} 个站日 ·
        当前版本 <span class="mono">${h(s.table_sha)}</span> ·
        上次拟合 ${hours(s.fit_age_hours)}</div>
      ${s.accumulating ? `<div class="bar"><i style="width:${pct}%"></i></div>
        <div class="muted" style="margin-top:5px">攒够 ${s.min_days} 天就会
        自动开始尝试修正，不需要改代码</div>` : ""}
      <table style="margin-top:12px"><thead><tr>
        <th>概率档</th><th>实际兑现</th><th>修正系数</th><th>为什么</th>
      </tr></thead><tbody>${bands}</tbody></table>
      ${journal ? `<details style="margin-top:8px">
      <summary class="muted" style="cursor:pointer">最近几次自动拟合</summary>
      <table><thead><tr><th>时间</th><th>结果</th><th>样本</th>
      <th>生效档数</th><th>版本</th></tr></thead>
      <tbody>${journal}</tbody></table></details>` : ""}
    </div>`;
  }).join("");
  return `<h2>六、概率分档校准：三个阶段各自的账</h2>
  <p class="sub">系统会记录"凡是说过 X% 的地方，后来实际发生了多少次"。
  如果长期说 6% 只发生 4%，就把这一档的数字乘一个小于 1 的系数压回去。</p>
  <div class="why"><b>怎么看这张表：</b>「实际兑现」是实测兑现率除以声称概率——
  1.00× 表示说得准，0.69× 表示说大话（只兑现了 69%），1.23× 表示说小话。
  「修正系数」是实际乘上去的那个数，只有证据足够扎实的档才会不等于 1.000，
  而且取的是保守值——宁可少改，不可改过头。</div>
  <div class="why" style="margin-top:8px"><b>这一节是一条闭环，分三步：</b><br>
  ① <b>记账</b>——三张卡各自记录本阶段"凡是说过 X% 的地方，后来兑现了多少"；<br>
  ② <b>拧旋钮</b>——证据过门的档拟出修正系数，乘进下一次服务的概率。
  这是全链<b>唯一改变数字</b>的一步；<br>
  ③ <b>体检</b>——下面「说了多少 vs 真发生了多少」量的是系数生效<b>之后</b>、
  三阶段合并的剩余偏差。它只诊断、不回写：故意用不同的分箱（十分位，
  而非上面的修正档位），这样体检没法机械地照抄旋钮，才配当监督。
  若某档残差持续变大，说明对应旋钮方向或粒度不对——回到 ②。</div>
  <div class="grid g3" style="margin-top:8px">${cards}</div>
  ${reliabilityAudit()}`;
}

// Step ③ of section 4: the post-correction residual, pooled and re-binned.
// Lives beside the knob it audits; reads the scorecard payload so the two
// sections cannot drift apart on data.
function reliabilityAudit(){
  const S = D.scorecard || {};
  const rows = (S.reliability_bins||[])
    .filter(b => b.n >= 100)
    .map(b => {
      const gap = b.happened - b.promised;
      const tone = Math.abs(gap) <= 0.05 ? "green"
                 : (Math.abs(gap) <= 0.10 ? "amber" : "red");
      return `<tr>
        <td class="mono">${h(b.bin)}</td>
        <td class="mono muted">${b.n.toLocaleString()}</td>
        <td class="mono">${(b.promised*100).toFixed(1)}%</td>
        <td class="mono">${(b.happened*100).toFixed(1)}%</td>
        <td class="mono ${gap>0?"up":"down"}">
          <span class="dot ${tone}"></span>
          ${gap>0?"+":""}${(gap*100).toFixed(1)} 点</td>
      </tr>`;
    }).join("");
  if(!rows) return "";
  return `<div class="card" style="margin-top:12px">
  <div class="muted"><b>③ 体检：说了多少 vs 真发生了多少</b>（修正系数生效后的残差 ·
  近 45 天全部已结算站日 · 只诊断，无下游消费）</div>
  <table style="margin-top:8px"><thead><tr>
    <th>承诺区间</th><th>样本</th><th>平均承诺</th><th>实际发生</th><th>差</th>
  </tr></thead><tbody>${rows}</tbody></table>
  <div class="muted" style="margin-top:6px">
    「差」为正＝实际比承诺更常发生，即修完还偏保守；为负＝修完仍吹头。
    只显示样本 ≥100 的区间。</div></div>`;
}

function basketHit(){
  const S=D.selection||{};const bs=S.basket_hit_by_size||[];
  if(!bs.length) return "";
  const p=v=>v==null?"—":(v*100).toFixed(1)+"%";
  const rows=bs.map(b=>{
    const ratio=b.claimed_win_probability_mean>0
      ? b.hit_rate/b.claimed_win_probability_mean : null;
    const roi=b.staked_usdc>0 ? b.pnl_usdc/b.staked_usdc : null;
    return `<tr><td><b>${b.window_size} 桶</b></td>
      <td>${b.settled_count}</td>
      <td>${b.hit_count}</td>
      <td class="${b.hit_rate>=0.5?"up":""}">${p(b.hit_rate)}</td>
      <td>${p(b.claimed_win_probability_mean)}</td>
      <td class="${ratio==null?"":(ratio>=0.9&&ratio<=1.15?"up":"down")}">${
        ratio==null?"—":ratio.toFixed(2)+"×"}</td>
      <td class="mono">${b.all_in_cost_mean.toFixed(3)}</td>
      <td class="mono ${roi==null?"":(roi>=0?"up":"down")}">${
        roi==null?"—":((roi>=0?"+":"")+(roi*100).toFixed(1)+"%")}</td></tr>`;
  }).join("");
  return `<h2>十四、篮子命中率（按桶数）</h2>
  <p class="sub">结算真值落在篮子里就算命中。只算可成交的（整篮 ≥1 分）。</p>
  <div class="why"><b>这张表回答一件事：</b>多买一个桶买到的命中率，
  值不值它多花的钱。命中率一定随桶数上升——问题是成本也上升，
  最右边的每元回报才是净结果。</div>
  <table><thead><tr><th>篮子宽度</th><th>已结算</th><th>命中</th><th>命中率</th>
    <th>声称</th><th>实现/声称</th><th>均成本</th><th>每元回报</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function refusalStats(){
  const S=D.selection||{};
  const stages=(S.refusal_stats||{}).stages||[];
  if(!stages.length) return "";
  // Top three per stage, the rest folded (user ruling, 2026-08-21).  The
  // reasons arrive sorted by count and the tail is a long list of one- and
  // two-count rows: nine rows per stage is most of this section's height
  // and only the first few ever change what anyone does.  Folded rather
  // than dropped, because the funnel only balances with all of them -- and
  // the fold's own summary carries the remainder's total so the arithmetic
  // still closes with the fold shut.
  const line=r=>`<tr>
      <td>${h(r.label)}</td>
      <td class="mono">${r.count}</td>
      <td class="mono">${(r.share*100).toFixed(1)}%</td>
      <td class="muted">${h(r.note)}${r.example?
        ` <span class="mono">（例：${h(r.example)}）</span>`:""}</td>
    </tr>`;
  const head3=`<thead><tr>
        <th>原因</th><th>站日数</th><th>占扫到的</th><th>这是什么意思</th>
      </tr></thead>`;
  const cards=stages.map(st=>{
    const all=st.reasons||[];
    const top=all.slice(0,3);
    const rest=all.slice(3);
    const restCount=rest.reduce((a,r)=>a+(r.count||0),0);
    const restShare=rest.reduce((a,r)=>a+(r.share||0),0);
    const restBlock=rest.length?`<details style="margin-top:8px">
      <summary class="muted" style="cursor:pointer">其余 ${rest.length} 项
        合计 ${restCount} 个站日（${(restShare*100).toFixed(1)}%）</summary>
      <table style="margin-top:8px">${head3}
        <tbody>${rest.map(line).join("")}</tbody></table></details>`:"";
    return `<div class="card" style="margin-top:12px">
      <div class="muted">${h(st.stage)} · 扫到 ${st.scope_count} 个站日，
        建仓 ${st.seated_count}${st.scope_restricted_to?
          ` <span class="mono">（只计 ${h(st.scope_restricted_to)}）</span>`:""}</div>
      <div class="big ${st.seat_rate>=0.3?"up":"down"}">${
        (st.seat_rate*100).toFixed(1)}%</div>
      <div class="muted">其余 ${st.refused_count} 个没建仓，
        占大头的三条如下${rest.length?"，长尾折在表下":""}</div>
      <table style="margin-top:10px">${head3}
        <tbody>${top.map(line).join("")}</tbody></table>
      ${restBlock}
    </div>`;
  }).join("");
  return `<h2>十六、没建仓是因为什么（逐阶段）</h2>
  <p class="sub">分母是这一阶段扫到的全部站日，建仓数 + 各项原因 = 分母，
  所以这是一条漏斗而不是两份互不相干的清单。
  <b>P0-M 只统计 TAF 带温度组的那 17 个站</b>——这一阶段是拿 TAF 做确认，
  没有 TAF 的站根本走不到，把它们算进分母等于拿它没法做的活给它打分。</p>
  <div class="why"><b>为什么要分开列：</b>以前页面只能靠"没有这一行"来推断，
  于是所有情况都被说成"目标函数看过后没坐下"——这只对其中一条成立，
  对其余每一条都是错的，而它们各有各的负责人和各自的修法。
  <b>「目标函数看过，不下注」「想加的已经在仓里」是策略在工作，不是故障；
  「整天没凑出过一篮子」「整篮报价高过满额赔付」是市场那边太窄或太贵，
  「决策时没有盘口」「盘口深度不够」则是采集和深度的问题；
  「P0 没建仓，本阶段无仓可加」不是本阶段的事，真正的原因在 P0 那张表。</b><br>
  三个阶段不合并：它们面对的是三个不同的书，某个原因在一个阶段占主导，
  在另一个阶段可能根本不出现。</div>
  <div class="why"><b>每一行记的是那个站日走得最深的一次，不是最后一次。</b>
  一个站日一天会被反复问价，而收盘时的书已经收敛成"一个桶 0.999、其余
  0.001"，从那里什么篮子都凑不出来。按最后一次记，就会把上午已经凑出篮子、
  被目标函数判过的站日，写成"整天没凑出过一篮子"。<br>
  漏斗从浅到深是：没有盘口 → 盘口不完整 → 整天没凑出篮子 →
  凑出来了但价不合算 → 下单了但没成交。走到越深，说明这一天离成交越近。</div>
  ${cards}`;
}


function windowDivergence(){
  const W=D.window_divergence||{};
  const rows=W.flagged||[];
  const ths=W.thresholds||[];
  if(!rows.length&&!ths.length)
    return `<h2>十六A、分布与持仓分家</h2>
    <div class="card"><span class="dot green"></span> 没有可评估的站日。</div>`;
  const pct=v=>v==null?"—":(v*100).toFixed(0)+"%";
  const thRows=ths.map(t=>`<tr>
    <td class="mono">&gt;${pct(t.threshold)}</td>
    <td>${t.hot_n}</td><td>${pct(t.hot_hit_rate)}</td>
    <td class="${t.hot_pnl>=0?"up":"down"}">${t.hot_pnl>=0?"+":""}${t.hot_pnl.toFixed(1)}</td>
    <td>${t.cold_n}</td><td>${pct(t.cold_hit_rate)}</td>
    <td class="${t.cold_pnl>=0?"up":"down"}">${t.cold_pnl>=0?"+":""}${t.cold_pnl.toFixed(1)}</td></tr>`).join("");
  const short=b=>String(b).replace(/°[CF]| or below| or higher/g,"");
  const line=r=>{
    // Which way the mass leaked tells the reader which archetype this is
    // likely to be before the settled column confirms it.
    const dir=r.out_above>=r.out_below
      ?`上方 ${pct(r.out_above)}`
      :`下方 ${pct(r.out_below)}`;
    return `<tr>
    <td class="mono">${h(r.station_id)}</td><td class="mono">${h(r.event_date)}</td>
    <td class="mono">${r.held_buckets.map(short).join("+")}</td>
    <td><b>${pct(r.late_out)}</b> <span class="muted">${dir}</span></td>
    <td class="mono muted">${h((r.measured_at_utc||"").slice(5,16))}</td>
    <td class="mono">${short(r.truth_bucket)}</td>
    <td>${r.hit?'<span class="up">中</span>':'<span class="down">未中</span>'}</td>
    <td class="${r.pnl>=0?"up":"down"}">${r.pnl>=0?"+":""}${r.pnl.toFixed(2)}</td></tr>`;
  };
  return `<h2>十六A、分布与持仓分家</h2>
  <p class="sub">每个已结算的持仓站日，取<b>持仓桶还活着的最后一次概率重算</b>
  （持仓桶质量仍高于 5%，即加仓还来得及的最后时刻），量它有多少后验质量落在持仓窗之外。
  共评估 ${W.evaluated_station_days||0} 个站日，下表列出末态窗外质量超过 40% 的。</p>
  <div class="why"><b>这是分歧探测器，不是判决书——两边谁错它都会亮。</b>
  两种原型都真实存在：RKPK 2026-08-21 持 29-32，后验（正确地）压向 33-34，实测 34，
  该加仓没加，是<b>选桶层</b>的均值锚拖不过右尾；KSEA 2026-08-17 持 78-79，
  模型到当地午夜还把 76% 压在 80 以上，实测 78-79——硬下界只能从下方砍桶，
  砍不掉模型头顶的幻觉，这次是<b>模型</b>错、持仓对（夜间无软证据层收敛，
  物理路径 v5 的 NIGHT scope 铺开后这类应减少）。
  「中/未中」与盈亏就是逐日的裁决记录。</div>
  <div class="card"><div class="muted">按阈值切分：分家越狠，命中越差</div>
  <table style="margin-top:8px"><thead><tr><th>末态窗外</th>
    <th>站日</th><th>命中</th><th>盈亏 u</th>
    <th>其余</th><th>命中</th><th>盈亏 u</th></tr></thead>
  <tbody>${thRows}</tbody></table></div>
  <div class="scroll" style="margin-top:10px"><table>
    <thead><tr><th>站</th><th>事件日</th><th>持仓窗</th><th>末态窗外·方向</th>
    <th>时刻(UTC)</th><th>真值</th><th>结果</th><th>盈亏 u</th></tr></thead>
    <tbody>${rows.map(line).join("")}</tbody></table></div>`;
}


function makerProbe(){
  const S=D.selection||{};const m=S.maker_probe||{};
  if(!m.available) return "";
  const cells=m.cells||[];
  if(!cells.length) return `<h2>十五、挂单影子探针</h2>
    <div class="card"><span class="dot amber"></span>
      已写入 ${m.open_count||0} 个未到期意图，还没有到期可判定的。</div>`;
  const p=v=>v==null?"—":(v*100).toFixed(1)+"%";
  const BAND={open_gt24h:"建仓 >24h",open_12_24h:"12–24h",
    pre_event_0_12h:"0–12h",event_day:"事件日"};
  const rows=cells.map(c=>`<tr>
    <td>${BAND[c.lead_band]||c.lead_band}</td>
    <td class="mono">${h(c.price_rule)}</td>
    <td>${c.horizon_minutes}分</td>
    <td>${c.decided_count}</td>
    <td class="${c.fill_rate>=0.5?"up":""}">${p(c.fill_rate)}</td>
    <td class="${c.unfilled_rate>=0.5?"down":"muted"}">${p(c.unfilled_rate)}</td>
    <td class="mono">${c.limit_price_mean.toFixed(3)}</td>
    <td class="mono">${c.reference_ask_mean.toFixed(3)}</td>
    <td class="mono up">+${c.spread_saved_mean.toFixed(3)}</td></tr>`).join("");
  const st=m.settled||{};
  const settledLine=st.count
    ? `<div class="card" style="margin-top:12px"><div class="muted">已结算的成交</div>
       <div class="big">${st.count} 笔 · 命中 ${p(st.hit_rate)}</div>
       <div class="muted">均价 ${st.limit_price_mean==null?"—":st.limit_price_mean.toFixed(3)}
       · 每份盈亏 ${st.pnl_per_share_mean==null?"—":
         ((st.pnl_per_share_mean>=0?"+":"")+st.pnl_per_share_mean.toFixed(4))}</div></div>`
    : `<div class="card" style="margin-top:12px"><span class="dot amber"></span>
       成交已记录，但所属站日还没有结算真值，所以还没有命中率。</div>`;
  const re=S.resting_entry||{};
  const pct=(v)=>v==null?"—":(v*100).toFixed(1)+"%";
  const reCards=(re.stages||[]).map(r=>{
    const depth=(r.by_posted_rungs||[]).map(d=>
      `${d.posted_rungs}档×${d.leg_count}腿 平均成交${d.mean_filled_rungs.toFixed(2)}`
    ).join(" · ");
    return `<div class="card">
      <div class="muted">${h(r.stage)} · 挂单实际买到多少</div>
      <div class="big ${(r.rung_completion_rate||0)>=0.8?"up":"down"}">${
        pct(r.rung_completion_rate)}</div>
      <div class="muted">挂出 ${r.leg_count} 条腿的阶梯，成交这个比例的档
        （挂 100 买到 ${r.rung_completion_rate==null?"—":
          Math.round(r.rung_completion_rate*100)}）</div>
      <table style="margin-top:10px"><tbody>
        <tr><td><b>挂过单的站日</b>（下面两项之和）</td>
            <td class="mono"><b>${r.seated_count+r.nothing_filled_count}</b></td></tr>
        <tr><td>├ 建仓篮子（至少一腿成交）</td>
            <td class="mono">${r.seated_count}</td></tr>
        <tr><td>│&nbsp;&nbsp;└ 其中缺腿建仓（有腿没成交，照样建）</td>
            <td class="mono">${r.partial_basket_count}</td></tr>
        <tr><td>└ 全篮落空（一腿都没成交，没建仓）</td>
            <td class="mono">${r.nothing_filled_count}</td></tr>
        <tr><td>空桶率（该腿一档都没买到）</td>
            <td class="mono">${pct(r.empty_leg_rate)}</td></tr>
        <tr><td>整条阶梯全档成交</td>
            <td class="mono">${pct(r.full_ladder_rate)}</td></tr>
        <tr><td>实际成交均价</td><td class="mono">${
          r.mean_filled_price==null?"—":r.mean_filled_price.toFixed(4)}</td></tr>
      </tbody></table>
      ${depth?`<div class="muted" style="margin-top:8px">${depth}</div>`:""}
    </div>`;
  }).join("");
  // The three-stage assessment under the ROI-first objective (user
  // ruling, 2026-08-20): EV is the goal, fill rate is a diagnostic with
  // an 80% floor -- it exists to catch the failure mode where resting
  // orders quietly stop buying anything, not to be maximised.  One
  // verdict line, then one card per stage.
  const TARGET=0.8;
  const reStages=re.stages||[];
  // Judged on resolved days: an unresolved day's orders are still on the
  // book, not missed, and counting them fails every morning.
  const judged=(r)=>r.closed_share_fill_rate!=null
    ?r.closed_share_fill_rate:r.share_fill_rate;
  // The 80% floor is P0's alone.  P0 opens the position, so its posted
  // basket is the whole target and "how much of it did we get" is a fair
  // question.  P0-M and P1 add to what P0 opened, and a large share of
  // what they post is a rung an earlier stage already posted and the
  // market never came down to -- measured 2026-08-22, 94% of P0-M's
  // whole-basket misses were on days P0 had also left a leg unbought.
  // Holding a top-up stage to the opener's floor scores it on the
  // opener's leftovers, which is why P0-M read "挂单在空转" while its
  // core leg was filling 93.9% and it was still saving 30.9% against
  // crossing.  The two rates below say what it is actually doing.
  const failing=reStages.filter(r=>
    r.stage==="P0"&&(judged(r)==null||judged(r)<TARGET)).map(r=>r.stage);
  const evbad=reStages.filter(r=>
    r.maker_saving_vs_taker!=null&&r.maker_saving_vs_taker<=0)
    .map(r=>r.stage);
  const verdict=!reStages.length?"":
    (failing.length===0&&evbad.length===0
      ?`<span class="pill green">底线守住：P0 成交率 ≥80%，三阶段挂单仍优于吃单</span>`
      :[failing.length?`<span class="pill red">P0 成交率破 80% 底线</span>`:"",
        evbad.length?`<span class="pill red">价差收益转负：${
          evbad.map(h).join("、")}</span>`:""].join(" "));
  const assessCards=reStages.map(r=>{
    const sf=judged(r);
    const sav=r.maker_saving_vs_taker;
    const core=r.core_fill_rate, hedge=r.hedge_fill_rate;
    // Three gates, not one: the core leg answers to a hard floor (its job
    // is the exposure), the portfolio to the old 80% line, and the hedge
    // to the value red line first -- its rate is a reading, not a target.
    const coreOk=core==null||core>=0.9;
    const opener=r.stage==="P0";
    // A top-up stage answers to the core floor only; its pooled rate is
    // reported but not judged, because its denominator is inherited.
    const ok=coreOk&&(!opener||(sf!=null&&sf>=TARGET));
    const carried=r.carried_fill_rate, fresh=r.fresh_fill_rate;
    return `<div class="card">
      <div class="muted">${h(r.stage)} · ${opener
        ?"建仓完整度（核心 ≥90% · 组合 ≥80%，按已收盘）"
        :"加仓完整度（核心 ≥90%；组合率只读，分母是继承来的）"}</div>
      <div class="big ${sf==null?"":(ok?"up":"down")}">${pct(sf)}</div>
      <div class="muted">${sf==null?"还没有挂单记录"
        :!coreOk?"核心腿破线 · 底仓没建起来，先查执行"
        :ok&&opener?"两道门之上 · 目标仓位基本建成"
        :ok?"核心腿之上 · 在 P0 的仓位上加仓"
        :"组合破线 · 挂单在空转，先查执行不查模型"}</div>
      <table style="margin-top:10px"><tbody>
        <tr><td>核心腿成交率（中心桶，门 ≥90%）</td>
            <td class="mono ${core==null?"":(core>=0.9?"up":"down")}">${pct(core)}</td></tr>
        <tr><td>对冲腿成交率（受价值红线约束，只读不设门）</td>
            <td class="mono">${pct(hedge)}</td></tr>
        ${opener?"":`<tr><td>继承腿成交率（P0 挂过没买到的，再挂一次）</td>
            <td class="mono">${pct(carried)}</td></tr>
        <tr><td>新增腿成交率（链上从未持有的桶）</td>
            <td class="mono">${pct(fresh)}</td></tr>`}
        <tr><td>含进行中事件日（单还在书上）</td>
            <td class="mono">${pct(r.share_fill_rate)}</td></tr>
        <tr><td>省价 vs 吃单（正 = 还在赚价差）</td>
            <td class="mono ${sav==null?"":(sav>0?"up":"down")}">${
              sav==null?"—":((sav>=0?"+":"")+(sav*100).toFixed(1)+"%")}</td></tr>
        <tr><td>金额成交率（实付 ÷ 全成交应付）</td>
            <td class="mono">${pct(r.stake_fill_rate)}</td></tr>
        <tr><td>全篮落空（一档都没成交，没建仓，另计）</td>
            <td class="mono">${r.nothing_filled_count}</td></tr>
      </tbody></table>
    </div>`;
  }).join("");
    const cons=(S.consolidation_split||{});
  const consRows=(cons.strata||[]).map(r=>`<tr>
      <td class="mono">${r.signal_count} 个</td>
      <td class="mono">${r.settled}</td>
      <td class="mono">${r.hit_rate==null?"—":(100*r.hit_rate).toFixed(0)+"%"}</td>
      <td class="mono">${r.staked.toFixed(0)}</td>
      <td class="mono ${r.roi>=0?"up":"down"}">${r.roi==null?"—":((r.roi>=0?"+":"")+(100*r.roi).toFixed(1)+"%")}</td>
      <td class="mono ${r.roi_ex_best>=0?"up":"down"}">${r.roi_ex_best==null?"—":((r.roi_ex_best>=0?"+":"")+(100*r.roi_ex_best).toFixed(1)+"%")}</td>
    </tr>`).join("");
  const consBlock=cons.available?`<div class="card" style="margin-top:12px">
    <div class="muted">确定性溢价 · 前向验证中（只读，不参与任何决策）</div>
    <div class="why" style="margin-top:8px">入场那一刻数三个"市场已完成定价"的信号：
    中心桶连续 3 次未变、top2 质量 ≥60%、理想窗全数有 ≥2 分报价。
    样本内测得：<b>0 信号组独占全部正收益（+77%，去最大赢 +42%），
    任一信号亮起后 −35%~−44%</b>——确定性是市场收费的东西。
    此表按结算前向积累；等 0 信号组在前向样本里也过"去最大赢"检验，
    才谈要不要变成门。</div>
    <table style="margin-top:8px"><thead><tr>
      <th>信号数</th><th>结算</th><th>命中</th><th>投入</th><th>ROI</th><th>去最大赢</th>
    </tr></thead><tbody>${consRows}</tbody></table></div>`:"";
  const assessBlock=assessCards?`
    <h2 style="margin-top:0">十五、挂单执行 · 三阶段总体评估 ${verdict}</h2>
    <p class="sub"><b>目标是在不突破模型价值的前提下，用剩余时间最大化目标仓位的
    实际建成度（2026-08-21 重设计）。</b>三层闸：<b>闸1 价值红线</b>——对冲腿不超过
    λ·p(t)，核心腿不超过它自己的 p(t)，永不突破；<b>闸2 组合EV</b>——整篮声称 EV
    转负即全撤，只管"还买不买"；<b>闸3 执行压力</b>——只管"剩下的单挂在哪"：
    前 1/3 窗程挂带底要价差，中段起加价随<b>本腿未成交缺口</b>上浮（买满的腿无压力），
    后 1/3 窗程仍未成交的腿直接吃单——但只在价值红线之内，市场要价高过模型估值的腿
    宁可不买。有效期到该站达峰时刻分位为止。成交结果不改变价值，只改变剩余订单的挂法。
    省价为正说明挂单还在赚价差；进行中的事件日单子还挂在书上，读数会暂时偏低。</p>
    <div class="grid g3">${assessCards}</div>${consBlock}`:"";
  const reBlock=reCards?`<div class="grid g3">${reCards}</div>
    <div class="why" style="margin-top:12px"><b>三个阶段分开看，不合并：</b>
    P0 在赛前十几小时挂，盘口几乎不动；P1 在盘中挂，是当天最活跃的书；
    P0-M 只挂一次，赶上什么行情算什么。合并成一个成交率是三个不同市场的平均数，
    哪个都不描述。<br>
    <b>逐步建仓，不是全腿成交才建仓：</b>每条腿的每一档都是独立的一张单，
    各自成交。一条腿只成交四档就是四档，一条腿一档没成交就是缺这条腿——
    篮子照样建，中奖概率按<b>实际买到的桶</b>重算，缺的桶不算数。
    只有一条腿都没成交才放弃。<br>
    <b>「建仓篮子」和「全篮落空」是并列的两堆，不是包含关系：</b>
    落空的站日一张单都没成交，压根没建仓，所以它不在建仓数里面——
    两者相加才是这一阶段挂过单的站日总数。P0-M 上曾出现「建仓 19、
    落空 20」并排，看着像 19 里有 20 个，其实是 39 个站日里 19 个建成、
    20 个落空。<br>
    <b>成交率的分母只含建了仓的那些：</b>落空的站日没有成交价可算，
    计入会把「一张没成交」和「买到八成」压进同一个平均数。要看落空有多少，
    读上面那一行。<br>
    <b>挂单价格是区间不是单点：</b>每条腿在<b>买价上一跳 ~ 卖价下一跳</b>
    之间均分挂 5 档。上界是硬的——挂到卖价就是吃单，等于把价差付出去。
    价差只有一跳时区间退化成买价一档。<br>
    <b>没成交的档不会一直原地等：</b>每 60 分钟按当时盘口重挂一次，
    且区间下沿在挂单窗前半程内逐步向上沿收拢——前几个小时要价差，
    后几个小时要成交。收拢后区间放不下的档挂在同一价位（同价多档 =
    同一价位一张更大的单），上沿永远是当时卖价的下一跳，所以每一笔成交
    仍然比当刻吃单便宜至少一跳。</div>`:"";
  // The pooled cards above say whether resting works; they cannot say
  // whether it is getting better or worse, and that is the question after
  // every change to how long an order rests or how often it is re-posted.
  //
  // Only from 2026-08-15 (user ruling, 2026-08-21).  The rows before that
  // date were filled under a different resting rule -- other rung ladders,
  // other re-post cadence, another book -- so putting them in the same
  // column invites reading a rule change as a trend.  The older days are
  // dropped from the table rather than folded: this table exists to be
  // compared row against row, and a row that is not comparable is not
  // evidence at any depth.
  const TREND_FROM="2026-08-15";
  const trendAll=S.resting_entry_trend||[];
  const trend=trendAll.filter(d=>String(d.event_date)>=TREND_FROM);
  const trendDropped=trendAll.length-trend.length;
  const STAGES=["P0","P0-M","P1"];
  const cell=(s)=>{
    if(!s) return `<td class="muted">—</td>`;
    const rc=s.rung_completion_rate;
    return `<td class="mono ${rc==null?"":(rc>=0.8?"up":rc<0.5?"down":"")}">
      ${pct(rc)}
      <span class="muted">腿 ${pct(s.leg_fill_rate)} ·
      ${s.baskets}篮/${s.legs}腿 · 均价 ${
        s.mean_filled_price==null?"—":s.mean_filled_price.toFixed(3)}</span></td>`;
  };
  // A day that has not resolved is still filling.  Without saying so the
  // last two rows read as a collapse in fill rate when they are simply the
  // rows whose orders are still on the book.
  const today=(D.generated_at_utc||"").slice(0,10);
  const trendRows=trend.map(d=>`<tr>
    <td class="mono">${h(d.event_date)}${d.event_date>=today
      ? ` <span class="pill amber" title="事件日未结束，挂单还在成交，这一行会继续往上走">进行中</span>`
      : ""}</td>
    ${STAGES.map(k=>cell(d.stages[k])).join("")}
  </tr>`).join("");
  // Totals across the window, computed from the same rows rather than
  // restated: a footer that disagrees with its own column is worse than no
  // footer.
  const totals=STAGES.map(k=>{
    const acc=trend.reduce((a,d)=>{const s=d.stages[k];if(!s)return a;
      a.baskets+=s.baskets;a.legs+=s.legs;
      if(s.rung_completion_rate!=null)a.rc+=s.rung_completion_rate*s.legs;
      if(s.leg_fill_rate!=null)a.lf+=s.leg_fill_rate*s.legs;
      a.days++;return a;},{baskets:0,legs:0,rc:0,lf:0,days:0});
    return acc.legs?`<td class="mono"><b>${pct(acc.rc/acc.legs)}</b>
      <span class="muted">腿 ${pct(acc.lf/acc.legs)} ·
      ${acc.baskets}篮/${acc.legs}腿 · ${acc.days}天</span></td>`
      :`<td class="muted">—</td>`;
  }).join("");
  const trendBlock=trendRows?`
    <h2 style="margin-top:26px">按事件日看三阶段的成交趋势</h2>
    <p class="sub">每格上行是<b>档位成交率</b>（挂出去的阶梯买到几档），
    下行是整腿成交率、篮子数/腿数与实际成交均价。挂单时长或重挂周期一改，
    先看这里有没有动。标「进行中」的事件日还没结束，单子还挂在书上，
    那一行只会继续往上走——不要拿它和已经走完的日子直接比。<br>
    <b>只列 ${TREND_FROM} 起的事件日</b>，合计行也只合计这几行：
    更早的日子挂在另一套挂法下${trendDropped?`（${trendDropped} 天）`:""}，
    与现在的成交率不是同一件事的两次测量。</p>
    <div class="card"><div class="scroll"><table><thead><tr>
      <th>事件日</th>${STAGES.map(k=>`<th>${k}</th>`).join("")}
    </tr></thead><tbody>${trendRows}
      <tr><td><b>合计</b></td>${totals}</tr>
    </tbody></table></div></div>`:"";
  return `${assessBlock}<h2>挂单影子探针与逐阶段明细</h2>${reBlock}${trendBlock}
  <p class="sub">在真实时间轴上记录"此刻会挂什么单"，<b>不下真单</b>，
  再用后到的实时盘口判定它会不会成交。${m.open_count||0} 个意图未到期。</p>
  <div class="why"><b>为什么只有成交率没有成交量比例：</b>
  探针记录的是价格和到期，不带下单数量——它不下真单，就没有"挂 100 成交 80"
  这回事。这里的成交率是<b>订单级</b>的：到期时市场有没有穿过这个价。
  「空桶率」就是没穿过、这条腿买不到的比例。<br>
  <b>成交判据偏乐观：</b>只要盘口触及限价就算成交，没有排队位置。
  每条意图都存了 <span class="mono">queue_ahead_shares</span>，
  将来可以据此修正，而不是现在假设掉。</div>
  <div class="scroll"><table><thead><tr>
    <th>提前期</th><th>挂单价</th><th>有效期</th><th>已判定</th>
    <th>成交率</th><th>空桶率</th><th>挂单均价</th><th>同刻卖价</th><th>省下价差</th>
  </tr></thead><tbody>${rows}</tbody></table></div>
  ${settledLine}`;
}


function gaps(){
  if(!D.gaps.length) return `<h2>十八、数据缺口</h2>
    <div class="card"><span class="dot green"></span> 当前没有发现缺口。</div>`;
  const sev = {high:["red","要处理"],medium:["amber","留意"],low:["amber","正常等待"]};
  const rows = D.gaps.map(g => {
    const [cls, label] = sev[g.severity] || sev.low;
    return `<tr>
      <td><span class="pill ${cls}">${label}</span></td>
      <td><b>${h(g.what)}</b></td>
      <td class="muted">${h(g.cost)}</td>
      <td class="muted">${g.self_heals ? "会自行恢复" : "需要人工介入"}</td>
    </tr>`;
  }).join("");
  return `<h2>十八、数据缺口与健康检查</h2>
  <p class="sub">每一条都写清楚"缺了什么、会造成什么、会不会自己好"。
  标"正常等待"的不是故障，是样本还在积累。</p>
  <div class="card"><div class="scroll"><table><thead><tr>
    <th>级别</th><th>缺什么</th><th>影响</th><th>会不会自己好</th>
  </tr></thead><tbody>${rows}</tbody></table></div></div>`;
}

// The one table that lives above the tabs, with the health light and the
// strip (user ruling, 2026-08-21).  It is the page's core: one row per
// event day, every stage's account of it on the same line, and it answers
// "what happened yesterday" without choosing a group first.  Unnumbered for
// that reason -- the numbered sections are what the three groups hold, and
// a number here would imply it sits in one of them.
function daily(){
  if(!D.daily || !D.daily.length) return "";
  const SIDE={above:"偏上",aligned:"一致",below:"偏下"};
  const rows=D.daily.map(r=>{
    const s=r.sides||{};
    const cal=r.calibration_ratio;
    const calCls=cal==null?"":(cal>=0.9&&cal<=1.15?"up":"down");
    return `<tr>
      <td class="mono">${h(r.event_date)}</td>
      <td>${r.station_count}</td>
      <td>${r.position_count}</td>
      <td class="muted">${s.above||0}/${s.aligned||0}/${s.below||0}</td>
      <td class="mono">${r.posted_leg_total}
        <span class="muted">均 ${r.posted_leg_mean==null?"—"
          :r.posted_leg_mean.toFixed(1)}</span></td>
      <td class="mono">${r.filled_leg_total}
        <span class="muted">均 ${r.filled_leg_mean==null?"—"
          :r.filled_leg_mean.toFixed(1)}</span>
        ${r.posted_leg_total?`<span class="muted"> · ${
          (100*r.filled_leg_total/r.posted_leg_total).toFixed(0)}%</span>`:""}</td>
      <td>${r.settled_count}</td>
      <td>${r.hit_rate==null?"—":(r.hit_rate*100).toFixed(0)+"%"}</td>
      <td>${r.claimed_win_probability_mean==null?"—":(r.claimed_win_probability_mean*100).toFixed(0)+"%"}</td>
      <td class="factor ${calCls}">${cal==null?"—":cal.toFixed(2)+"×"}</td>
      <td>${r.odds_mean==null?"—":r.odds_mean.toFixed(1)+"x"}</td>
      <td class="mono">${r.staked_usdc.toFixed(2)}</td>
      <td class="mono ${r.pnl_usdc>=0?"up":"down"}">${(r.pnl_usdc>=0?"+":"")+r.pnl_usdc.toFixed(2)}</td>
      <td>${r.center_bias_c==null?"—":(r.center_bias_c>=0?"+":"")+r.center_bias_c.toFixed(2)}</td>
    </tr>`;
  }).join("");
  return `<h2>每日统计</h2>
  <p class="sub">按事件日汇总，只算可成交的（整篮成本 ≥1 分）——不足一分的多是无人出价那侧的
  挂单，名义上有价、实际买不到，混进来会让当天的盈亏读不出意思。</p>
  <div class="why"><b>列的意思：</b>「位置」是相对市场偏上/一致/偏下的笔数；
  「预计建仓桶」是当天所有座位挂出去的腿数合计（括号里是每座位平均几腿），
  「实际建仓桶」是其中真正买到的腿数与占比——两者的差是市场没卖，不是选桶变窄；
  「实现/声称」是命中率÷声称胜率，1.00× 表示概率说得准；
  「赔率」是 (1−成本)/成本；「中心偏差」是模型中心减实测，正数表示我们偏热。</div>
  <div class="card"><table><thead><tr>
    <th>事件日</th><th>站数</th><th>仓位</th><th>位置<br><span class="muted">上/平/下</span></th>
    <th>预计建仓桶<br><span class="muted">挂单腿数</span></th>
    <th>实际建仓桶<br><span class="muted">成交腿数</span></th>
    <th>已结算</th><th>命中率</th><th>声称</th><th>实现/声称</th>
    <th>赔率</th><th>投入</th><th>盈亏</th><th>中心偏差</th>
  </tr></thead><tbody>${rows}</tbody></table></div>`;
}


function coverage(){
  const S=D.selection||{};const c=S.coverage_stats||{};
  if(!c.position_count) return "";
  const p=v=>v==null?"—":(v*100).toFixed(1)+"%";
  const n=(v,d=3)=>v==null?"—":Number(v).toFixed(d);
  const vig=c.book_ask_total_mean==null?null:(c.book_ask_total_mean-1);
  const rr=c.realization_ratio;
  const sd=S.sides||{};const tot=S.position_count||1;
  const seg=(k,col)=>`<i style="display:block;height:9px;width:${100*(sd[k]||0)/tot}%;background:${col}"></i>`;
  return `<h2>十、覆盖 · 价格 · EV</h2>
  <p class="sub">只算可成交的（整篮 ≥1 分，排除虚价）。三组数回答同一个问题的三面：
  座位罩住了多少概率、为此付了多少、这笔交换值不值。</p>
  <div class="card"><div class="muted">相对市场的位置（模型质量中心减市场质量中心，单位是桶）</div>
    <div style="display:flex;height:9px;border-radius:99px;overflow:hidden;margin-top:7px">
      ${seg("above","#bc8cff")}${seg("aligned","#3fb950")}${seg("below","#d29922")}</div>
    <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
      <span class="pill" style="background:var(--pill-violet-bg);color:var(--violet)">偏上 ${sd.above||0}</span>
      <span class="pill green">一致 ${sd.aligned||0}</span>
      <span class="pill amber">偏下 ${sd.below||0}</span>
      <span style="flex:1"></span>
      <span class="muted">${S.position_count} 仓位 · ${S.day_count} 天</span></div></div>
  <div class="grid g4">
    <div class="card"><div class="muted">选桶概率和</div>
      <div class="big">${p(c.model_coverage_mean)}</div>
      <div class="muted">校准后概率在座位内的合计 · 平均 ${n(c.window_size_mean,1)} 桶</div></div>
    <div class="card"><div class="muted">选桶卖价和</div>
      <div class="big">${n(c.seat_ask_total_mean,3)}</div>
      <div class="muted">同样这几个桶的全含费卖价合计，即整篮成本</div></div>
    <div class="card"><div class="muted">每 1U 的期望回报</div>
      <div class="big ${(c.ev_per_usdc_mean||0)>0?"up":"down"}">${(c.ev_per_usdc_mean||0)>0?"+":""}${n(c.ev_per_usdc_mean,2)}U</div>
      <div class="muted">概率和÷卖价和 − 1，把不同价位的座位放到同一把尺子上</div></div>
    <div class="card"><div class="muted">已结算：期望 vs 实得</div>
      <div class="big">${n(c.expected_usdc,2)} → <span class="${(c.realized_usdc||0)>=0?"up":"down"}">${n(c.realized_usdc,2)}</span></div>
      <div class="muted">${c.settled_count} 笔 · 投入 ${n(c.staked_usdc,2)}U · 兑现比 ${c.realization_ratio==null?"—":n(c.realization_ratio,2)+"×"}</div></div>
  </div>
  <div class="why"><b>三个数都只关于这个篮子：</b>概率和是我们认为它值多少，
  卖价和是买下它要付多少，EV/U 是两者的比值减一——把 0.07 的座位和 0.7 的座位
  放到同一把尺子上比较。兑现比（实得÷期望）是最终裁判：长期低于 1，
  说明期望里有校准高估的水分。</div>`;
}


// A station's whole-window account, one row per station.  These averages
// used to ride along on every row of the daily roster, where a station with
// no book that day still showed an ask total and an EV and read as that
// day's price.  Here the window is stated once in the header and a row is a
// station, so the number cannot be mistaken for a date's.
function stationSummary(){
  const S=D.selection||{};const ss=S.station_stats||[];
  if(!ss.length) return "";
  const p2=v=>v==null?"—":(v*100).toFixed(1)+"%";
  const n2=(v,d=3)=>v==null?"—":Number(v).toFixed(d);
  // Stations that never took a credible seat have nothing to average, so
  // they are absent from this table by construction.  Say so, or their
  // absence reads as an omission rather than as the fact it is.
  const seated=new Set(ss.map(s=>s.station_id));
  const silent=[...new Set((S.station_roster||[]).map(e=>e.station_id))]
    .filter(id=>!seated.has(id)).sort();
  const rows=ss.map((s,i)=>{
    const sd=s.sides||{};const cal=s.calibration_ratio;
    const rr=s.realization_ratio;const ev=s.ev_per_usdc_mean;
    // A number per station (user ruling, 2026-08-21).  Assigned in the
    // table's natural order -- station id, ascending -- and printed in the
    // row rather than derived from the row's position, so it stays with the
    // station when a column is sorted instead of renumbering under it.
    return `<tr>
      <td class="mono muted">${i+1}</td>
      <td><span class="mono">${h(s.station_id)}</span>
        <span class="muted">${h(s.station_label)}</span></td>
      <td>${s.position_count}</td>
      <td>${s.settled_count}</td>
      <td class="mono">${s.posted_leg_total}
        <span class="muted">均 ${s.posted_leg_mean==null?"—"
          :s.posted_leg_mean.toFixed(1)}</span></td>
      <td class="mono">${s.filled_leg_total}
        <span class="muted">均 ${s.filled_leg_mean==null?"—"
          :s.filled_leg_mean.toFixed(1)}</span>
        ${s.posted_leg_total?`<span class="muted"> · ${
          (100*s.filled_leg_total/s.posted_leg_total).toFixed(0)}%</span>`:""}</td>
      <td class="muted">${sd.above||0}/${sd.aligned||0}/${sd.below||0}</td>
      <td>${p2(s.model_coverage_mean)}</td>
      <td class="mono">${n2(s.seat_ask_total_mean)}</td>
      <td class="mono">${s.odds_mean==null?"—":n2(s.odds_mean,1)+"x"}</td>
      <td class="${ev==null?"":(ev>0?"up":"down")}">${ev==null?"—":((ev>0?"+":"")+n2(ev,2))}</td>
      <td>${p2(s.hit_rate)}</td>
      <td>${p2(s.claimed_win_probability_mean)}</td>
      <td class="${cal==null?"":(cal>=0.9&&cal<=1.15?"up":"down")}">${cal==null?"—":n2(cal,2)+"×"}</td>
      <td class="mono">${n2(s.staked_usdc,2)}</td>
      <td class="mono ${(s.realized_usdc||0)>=0?"up":"down"}">${((s.realized_usdc||0)>=0?"+":"")+n2(s.realized_usdc,2)}</td>
      <td class="${rr==null?"":(rr>=1?"up":"down")}">${rr==null?"—":n2(rr,2)+"×"}</td></tr>`;
  }).join("");
  const note=silent.length?`<div class="muted" style="margin-top:8px">
    另有 ${silent.length} 个站在这段窗口里一笔可成交仓位都没有，因此没有可平均的数，
    只出现在下一节的逐日明细里：<span class="mono">${
      silent.map((id,i)=>`${i+1}.${h(id)}`).join("　")}</span></div>`:"";
  return `<h2>十一、逐站汇总（跨日）</h2>
  <p class="sub">一站一行，把该站 ${h(S.stats_start_event_date)} 起的全部可成交仓位
  （整篮 ≥1 分，排除虚价）平均到一起。<b>这些数不属于任何一天</b>——它们回答的是
  「这个站长期表现如何」，逐日的账在下一节。</p>
  <div class="why"><b>列的意思：</b>「#」是站的编号，按站号字母序编，
  排序时跟着这一站走，所以可以直接拿它点名某一行；
  「仓位」是参与平均的笔数，样本少于三笔时右侧的
  命中率与实现/声称基本读不出信号；
  「预计建仓桶」是该站这段窗口里挂出去的腿数合计（括号里是每座位平均几腿），
  「实际建仓桶」是真正买到的腿数与占比——占比低说明这个站的盘口不肯按我们的价成交，
  与选桶宽窄无关；「位置」是相对市场偏上/一致/偏下的笔数；
  「概率和/卖价和/赔率/EV」同第十节，只是范围缩到这一个站；
  「实现/声称」是命中率÷声称胜率；「兑现比」是实得÷期望。</div>
  <div class="card"><div class="scroll"><table><thead><tr>
    <th>#</th><th>站点</th><th>仓位</th><th>已结算</th>
    <th>预计建仓桶<br><span class="muted">挂单腿数</span></th>
    <th>实际建仓桶<br><span class="muted">成交腿数</span></th>
    <th>位置<br><span class="muted">上/平/下</span></th>
    <th>概率和</th><th>卖价和</th><th>赔率</th><th>EV/U</th>
    <th>命中率</th><th>声称</th><th>实现/声称</th>
    <th>投入</th><th>实得</th><th>兑现比</th>
  </tr></thead><tbody>${rows}</tbody></table></div>${note}</div>`;
}


function roster(){
  const S=D.selection||{};const rs=S.station_roster||[];
  if(!rs.length) return "";
  // The ledger now records why it did not seat, so these are its own words
  // rather than the page's guess.  What "未入座" used to cover
  // indiscriminately splits by which gate of the enumeration funnel took the
  // last window still standing: the structural floors never reach a price at
  // all (44% of those days could not reach three contiguous quoted buckets
  // through the centre), a missing ask cannot be priced, a sub-floor leg is
  // our own refusal to treat a tail offer as a price, and only a cleared
  // window whose edge misses friction is the market pricing above the model
  // (the other 56%, best-basket edge median -0.25).  Those two percentages
  // were the argument for splitting the code and are now each attached to
  // the code they describe.  A refused Kelly is a basket that was priced and
  // judged not worth its price, a basket over its own payout is a loss by
  // arithmetic the model never got asked about, and no depth is a book too
  // thin to fill at any size.
  //
  // Short labels here, long ones in section 13; both come from the same
  // reason codes, so a code added to the ledger needs an entry in both.
  // The rule set the ledger currently compiles under.  Everything stamped
  // with it is directly comparable; anything else is a leftover from before
  // a rule change and is flagged per basket.
  const CURRENT_RULE=(D.selection||{}).current_selection_rule_label||"";
  const REASON={no_probability:["无概率","这一天没有为该站算出概率"],
    no_market_book:["无盘口","有概率但没抓到可定价的市场"],
    no_book_at_decision:["盘口未落库","每次决策时刻都没有半小时内的盘口快照"],
    incomplete_book:["盘口缺桶","有盘口但缺了模型认得的桶，整站不可定价"],
    selection_failed:["选桶报错","枚举窗口时抛错，该轮跳过"],
    no_legal_window:["无合法窗口","结构闸拿走了整批枚举：断桶、不含中心桶、或长度不够。这些窗口没走到看报价那一步，跟贵不贵无关。约四成的未入座是这一类"],
    no_priced_window:["窗口定不出价","窗口过了全部结构闸，但里面有腿根本没有卖价，成本算不出来。缺的是盘口"],
    negative_edge_window:["边际不敌摩擦","窗口过闸也报得出价，胜率减全包成本仍盖不住逐腿最小跳动——这一条才是市场比模型贵。约六成的未入座是这一类，最好的一篮边际中位数 −0.25"],
    leg_below_floor:["单腿报价过低","窗口里有腿的报价低于一分——那是无人出价那侧的挂牌，不是价格。这是我们自己设的闸，不是市场没给价"],
    no_positive_kelly:["赔率不划算","篮子凑出来也定了价，但赢面低于要价，Kelly 给出零仓位"],
    cost_exceeds_payout:["比满赔还贵","整篮成本超过它赢了能赔的 1.00，多高的概率都是亏——模型没被问到，是书的问题"],
    resting_unfilled:["挂单没成交","价格没等到；规模是够的，市场没走到我们这里来。每 60 分钟重挂一次，但过了本站达峰时刻就收摊，不是挂到结算"],
    belief_withdrew_order:["信念转负撤单","不是市场没来，是我们自己撤的：重挂时用最新概率重算整篮期望，转负即全撤。该查模型，不是执行"],
    no_top_up_needed:["无需加仓","这一阶段想要的梯级已经在仓里了，按兵不动"],
    chain_never_opened:["P0 未开仓","只有 P0 开仓，后面两阶段是加仓；P0 没坐下这里就没仓可加，真正的原因看 P0"],
    no_depth:["盘口太薄","价格划算但挂单量填不满最小整篮"],
    no_seat_taken:["未入座","有概率也有盘口，目标函数看过后选择不坐"]};
  const short=x=>String(x).replace("°C","").replace("°F","");
  const LAB={above:"偏上",below:"偏下",aligned:"一致",unknown:"—"};
  // An empty stage cell used to be a dash and nothing else, so a station with
  // no P0 basket looked identical whether the market was never quoted or the
  // objective priced it and declined.  The refusal goes in the cell it
  // belongs to.
  const why=(r)=>{
    if(!r) return "";
    const lab=REASON[r.reason]||[r.reason,""];
    return `<div class="muted" title="${h(lab[1])}">${h(lab[0])}</div>`+
      (r.detail?`<div class="muted mono" style="font-size:11px">${h(r.detail)}</div>`:"");
  };
  const pctS=v=>v==null?"—":(v*100).toFixed(0)+"%";
  const usd2=v=>v==null?"—":v.toFixed(2);
  // The order as posted, drawn through the window it filled.  A bucket that
  // bought nothing is struck through rather than dropped: the charter's
  // three-rung floor is a rule about what was posted, so a cell that shows
  // only the fill cannot be read against it -- which is how one filled rung
  // came to look like a one-rung basket that should never have been chosen.
  // Charter 50a gives P1 a floor of two rungs, not three: the ladder
  // converges as the day resolves and the tail rung is still carrying the
  // insurance.  A flat three-rung test marks every legitimate P1 seat as a
  // violation.
  const FLOOR={"P0":3,"P0-M":3,"P1":2};
  const entryCell=(e,stage)=>{
    if(!e||!e.posted_leg_count) return "";
    const legs=e.legs.map(l=>l.empty
      ? `<span class="muted" style="text-decoration:line-through">${short(l.bucket)}</span>`
      : `<span>${short(l.bucket)}</span>`).join("+");
    const floorBad=e.posted_leg_count<(FLOOR[stage]||3);
    const full=e.filled_leg_count===e.posted_leg_count;
    const legLine=`<div class="mono" style="font-size:11px">${legs}
      <span class="muted">${e.posted_leg_count}腿→${e.filled_leg_count}腿</span>
      ${floorBad?`<span class="pill red" title="宪章 50/50a：${stage} 的挂单下界是 ${FLOOR[stage]||3} 桶">挂单不足${FLOOR[stage]||3}桶</span>`:""}
      ${e.gapped?`<span class="pill amber" title="选桶层强制连续，出洞必是成交造成的">缺腿</span>`:""}</div>`;
    // Share completion and stake completion disagree whenever the legs that
    // missed were not the ones carrying the money, so both are shown; the
    // rung count is what the market actually came down to.
    const money=`<div class="muted" style="font-size:11px">
      ${usd2(e.intended_stake_usdc)}→${usd2(e.filled_stake_usdc)}
      <span title="金额完成">(${pctS(e.stake_completion)})</span> ·
      <span title="份数完成 / 档位完成">份${pctS(e.share_completion)} ·
      档${pctS(e.rung_completion)}</span>${full?
        ` <span class="pill green">整篮</span>`:""}</div>`;
    const rows=e.legs.map(l=>`<tr${l.empty?' class="muted"':''}>
      <td>${h(l.bucket)}</td><td>${l.intended_shares.toFixed(1)}</td>
      <td>${l.empty?"<b>0</b>":l.filled_shares.toFixed(1)}</td>
      <td>${l.posted_rungs==null?"—":l.filled_rungs+"/"+l.posted_rungs}</td>
      <td>${l.empty||l.fill_price==null?"—":l.fill_price.toFixed(4)}</td></tr>`).join("");
    return legLine+money+`<details style="font-size:11px">
      <summary class="muted">逐腿</summary>
      <table class="mono" style="font-size:11px"><thead><tr>
        <th>腿</th><th>目标份</th><th>成交份</th><th>档</th><th>均价</th>
      </tr></thead><tbody>${rows}</tbody></table></details>`;
  };
  const cell=(s,extra,refusal,stage)=>{
    if(!s) return `<td class="muted">—${why(refusal)}${extra||""}</td>`;
    const w=s.window.map(short).join("+");
    // The chain settles once, and the stage that last topped the position
    // up is the one that carries the result.  Marking every stage of a
    // chain with the same hit reads as three bets that all won.
    const res=s.hit==null?""
      :(s.chain_tail
        ?(s.hit?` <span class="up">中</span>`:` <span class="down">未中</span>`)
        :` <span class="muted" title="本阶段是加仓，结算记在本链最后一次加仓上">并入本链</span>`);
    // "Dust offer" is trading jargon and reads as nothing in Chinese; the
    // thing itself is a price that exists on the book but cannot be filled.
    const dust=s.dust_offer?` <span class="pill red">虚价</span>`:"";
    const rung=s.centre_bucket?
      `<div class="muted">横梁 <span class="mono" style="color:var(--violet)">${short(s.centre_bucket)}</span></div>`:"";
    // Which rule chose this basket.  A day compiled under a retired rule set
    // sits beside one compiled under the current rule and is not comparable
    // to it; without this the page invites exactly that comparison.  Current
    // rule is stated once in the section header, so a basket only carries the
    // tag when it differs from it -- otherwise every cell repeats the same
    // string and the ones that matter stop standing out.
    // The full rule string is a fingerprint; a cell only needs to warn that
    // this basket is not comparable to the others, and the name belongs in
    // the tooltip where it does not crowd the numbers out.
    const tag=(s.selection_rule_label && s.selection_rule_label!==CURRENT_RULE)
      ? `<div><span class="pill amber" title="按已退役规则选出，与当前规则不可直接比较：${h(s.selection_rule_label)}">旧规则</span></div>`
      : "";
    return `<td><div class="mono">${w}</div>${rung}${tag}
      <div class="muted">${(s.win_probability*100).toFixed(0)}%
      @ ${s.all_in_cost.toFixed(3)} · ${s.odds==null?"—":s.odds.toFixed(1)+"x"}${res}${dust}</div>
      ${entryCell(s.entry,stage)}${extra||""}</td>`;
  };
  // Every temperature on this row is in the market's own unit except the
  // settled truth, which is Celsius everywhere.  For the eleven Fahrenheit
  // stations that put "96-97" beside "36.0" in the same row, which is a
  // 60-degree disagreement that is really no disagreement at all.  So the
  // truth is shown in the unit the row's buckets are named in, and the unit
  // is stated on the station rather than repeated in every cell.
  const asUnit=(celsius,unit)=>unit==="F"?celsius*9/5+32:celsius;
  // What the station-day did, as one of four states.  Reconstructing it
  // meant reading three stage cells and a P&L column.
  const STATE={
    hit:["st-hit","命中"],miss:["st-miss","未命中"],
    open:["st-open","未结算"],flat:["st-flat","未建仓"],
  };
  const stateOf=(e)=>{
    if(!e.seated_stage_count) return "flat";
    const settled=["P0","P0-M","P1"].map(k=>e.stages[k])
      .find(v=>v&&v.chain_tail&&v.hit!=null);
    if(!settled) return "open";
    return settled.hit?"hit":"miss";
  };
  const row=(e)=>{
    const any=e.stages.P0||e.stages["P0-M"]||e.stages.P1;
    const side=any?`<span class="pill ${any.side==="below"?"amber":any.side==="aligned"?"green":""}"
      ${any.side==="above"?'style="background:var(--pill-violet-bg);color:var(--violet)"':""}>${LAB[any.side]}</span>`:"";
    const note=e.seated_stage_count?"":
      `<span class="muted">${(REASON[e.absent_reason]||["—",""])[0]}</span>`;
    const taf=e.taf_tx_c==null?`<div class="muted">TAF 无温度组</div>`:
      `<div class="muted">TAF 最高 <span class="mono" style="color:var(--up)">${
        asUnit(e.taf_tx_c,e.settlement_unit||"C").toFixed(1)
      }°${h(e.settlement_unit||"C")}</span></div>`;
    // Recomputations and rung moves are different counts; conflating them
    // reads as a rung that thrashed when it never budged.
    const hist=e.p1_history||[];
    const rungs=hist.map(x=>x.centre_bucket).filter(Boolean);
    const path=rungs.filter((x,i)=>i===0||x!==rungs[i-1]);
    // The last recompute's point estimate is P1's own predicted maximum for
    // the day, shown in the market's unit.  The tag says which conditioner
    // produced it: a physical-path prediction, the retired curve method, or
    // -- at zero strength -- the bare hard bound, which is not a prediction
    // and must not read as one.
    const MODE_LABEL={
      PHYSICAL_ABSOLUTE_TMAX_SOFT_LR_PLUS_HARD_EXCLUSION_V5:"物理路径",
      CONDITIONAL_HOURLY_FUTURE_PATH_SOFT_LR_PLUS_HARD_EXCLUSION_V4:"曲线法",
      CONDITIONAL_FUTURE_PATH_SOFT_LR_PLUS_HARD_EXCLUSION_V3:"路径法(旧)",
      FULL_PREFIX_ROOT_RECOMPUTE_SOFT_LR_PLUS_HARD_EXCLUSION_V2:"重算法(旧)",
    };
    const last=hist.length?hist[hist.length-1]:null;
    const lastC=last==null?null:Number(last.centre_c);
    const predUnit=e.settlement_unit||"C";
    const strength=last&&last.trajectory_strength!=null
      ?Number(last.trajectory_strength):null;
    const modeTag=last==null?"":
      (strength!=null&&strength<=0
        ?`<span class="muted">仅硬下界</span>`
        :`<span class="muted">${MODE_LABEL[last.likelihood_mode]||"—"}${
            strength==null?"":" · 强度"+strength.toFixed(2)}</span>`);
    const pred=(last==null||lastC==null||!isFinite(lastC))?"":
      `<div class="muted">预测最高 <span class="mono" style="color:var(--up)">${
        asUnit(lastC,predUnit).toFixed(1)}°${h(predUnit)}</span> ${modeTag}</div>`;
    const drift=(!hist.length?"":
      `<div class="muted">${hist.length} 次重算 ·
       ${path.length<2
         ? `横梁未动 <span class="mono">${path.length?short(path[0]):"—"}</span>`
         : `横梁 <span class="mono">${path.map(short).join(" → ")}</span>`}</div>`)+pred;
    // One number for the station-day, because it held one position.  The
    // stake beside it is everything the chain spent, opening and top-ups.
    const pnl=e.realized_usdc==null?'<span class="muted">—</span>':
      `<span class="${e.realized_usdc>=0?"up":"down"}">${e.realized_usdc>=0?"+":""}${e.realized_usdc.toFixed(2)}</span>`
      +(e.chain_stake_usdc==null?"":
        `<div class="muted" style="font-size:11px">投入 ${e.chain_stake_usdc.toFixed(2)}</div>`);
    // Only what happened at this station on this day.  The averages that
    // used to sit here -- coverage, ask total, odds, EV, hit rate,
    // calibration -- are computed over the station's whole window, so the
    // same six numbers repeated on every date and read as the day's own
    // account: ZUUU showed an ask total of 0.514 on 2026-08-17, a day whose
    // book never reached the table.  They now live in section eight.
    const state=stateOf(e);
    const unit=e.settlement_unit||"C";
    return `<tr>
      <td class="st ${STATE[state][0]}" title="${STATE[state][1]}">
        <div class="mono">${h(e.station_id)}</div>
        <div class="muted">${h(e.station_label)}</div>
        <div class="muted" style="font-size:11px">${STATE[state][1]} ·
          盘口 °${h(unit)}</div></td>
      ${cell(e.stages.P0,"",(e.refusals||{}).P0,"P0")}
      ${cell(e.stages["P0-M"],taf,(e.refusals||{})["P0-M"],"P0-M")}
      ${cell(e.stages.P1,drift,(e.refusals||{}).P1,"P1")}
      <td>${side}${note}</td>
      <td class="mono">${e.settled_tmax_c==null?"—"
        :asUnit(e.settled_tmax_c,unit).toFixed(1)+"°"+h(unit)}</td>
      <td class="mono">${pnl}</td></tr>`;
  };
  const head=`<tr><th>站点</th><th>P0 建仓</th><th>P0-M 确认</th><th>P1 盯盘</th>
    <th>位置</th><th>实测</th><th>实得</th></tr>`;
  const dates=S.roster_event_dates||[];
  const block=(day,open)=>{
    const rows=rs.filter(e=>e.event_date===day);
    const seated=rows.filter(e=>e.seated_stage_count>0).length;
    // P0 counted on its own: it is the opening stage, so "how many stations
    // got a basket today" is a question about P0 and the all-stage count
    // answers it only by accident.
    const p0=rows.filter(e=>e.stages.P0).length;
    // The rule name is a fingerprint, not prose, and printing it in full on
    // every day pushed the numbers off the line.  What a reader needs from
    // it is the exception: a day compiled under more than one rule set mixes
    // two strategies and its totals are not one account.  Say that, and put
    // the names in the tooltip.
    const dayRules=[...new Set(rows.flatMap(e=>
      ["P0","P0-M","P1"].map(k=>e.stages[k]&&e.stages[k].selection_rule_label)
        .filter(Boolean)))].sort();
    const stale=dayRules.filter(r=>r!==CURRENT_RULE);
    const ruleNote=(dayRules.length>1||stale.length)
      ? ` <span class="pill amber" title="${h(dayRules.join(" / "))}">${
          dayRules.length>1?"混用两套选桶规则":"按已退役规则选出"}</span>`
      : "";
    // The day's entry account, summed over every seat it took.  A seat is a
    // posted order first and a position second, and the gap between the two
    // is the day's own number -- not a per-station curiosity.
    const seats=rows.flatMap(e=>["P0","P0-M","P1"]
      .map(k=>e.stages[k]).filter(v=>v&&v.entry&&v.entry.posted_leg_count));
    const short3=rows.flatMap(e=>["P0","P0-M","P1"].map(k=>[k,e.stages[k]]))
      .filter(([k,v])=>v&&v.entry&&v.entry.posted_leg_count
        &&v.entry.posted_leg_count<(FLOOR[k]||3)).length;
    const partial=seats.filter(v=>
      v.entry.filled_leg_count<v.entry.posted_leg_count).length;
    const want=seats.reduce((s,v)=>s+(v.entry.intended_stake_usdc||0),0);
    const got=seats.reduce((s,v)=>s+v.stake_usdc,0);
    const fill=seats.length?` · 部分成交 ${partial}/${seats.length} ·
      计划 ${usd2(want)}→实投 ${usd2(got)}（${pctS(want>0?got/want:null)}）${
        short3?`<span class="pill red">挂单低于本阶段下界 ${short3}</span>`:""}`:"";
    return `<details class="card" ${open?"open":""}>
      <summary><b class="mono">${h(day)}</b>
        <span class="muted">${rows.length} 站 · P0 建仓 ${p0} · 任一阶段有座位 ${seated}${ruleNote}${fill}</span></summary>
      <div class="scroll" style="margin-top:10px"><table>
        <thead>${head}</thead><tbody>${rows.map(row).join("")}</tbody></table></div>
    </details>`;
  };
  const legend=Object.entries(REASON).map(([k,v])=>
    `<div class="muted">· <b>${v[0]}</b>：${v[1]}</div>`).join("");
  // Newest day open, everything older folded away beneath it.
  return `<h2>十二、逐站逐日明细：选了什么、挂了什么、买到了什么</h2>
  <p class="sub">每个事件日一组，注册表里的每个站都在，包括没有座位、甚至当天没算出概率的
  ——没坐比坐了更值得看清原因。
  每格上行是<b>成交后</b>的桶，下方是横梁与胜率 @ 成本 · 赔率；再下一行是这一笔的
  <b>挂单账</b>：挂出去的每一桶都在，一档没买到的划掉，后面跟腿数、金额与三个完成率，
  展开「逐腿」是每条腿的目标份数 / 成交份数 / 成交档位 / 均价。
  空格里写的是账本自己给出的拒绝理由，不再统一算作「未入座」。
  P1 格里的「预测最高」是当天<b>最后一次重算</b>的点预测（后验加权中心，随盘口单位显示），
  旁边注明它出自哪个条件化器：<b>物理路径</b>是只看观测的 v5 预测器（带生效强度），
  「仅硬下界」表示该次重算没有任何软证据参与——那不是预测，只是排除。
  每行只讲这一天：跨日均值在上一节。最新一天默认展开，更早的日期折叠在下方。</p>
  <div class="why"><b>站点格的底色＝这个站日最后怎么了：</b>
  <span class="pill green">命中</span>真值落在最终持仓里 ·
  <span class="pill red">未命中</span>落在持仓外 ·
  <span class="pill amber">未结算</span>建了仓但当天真值还没到 ·
  <span class="pill" style="background:var(--why-bg);color:var(--accent)">未建仓</span>
  P0 没入座，原因写在格子里。
  <b>温度单位随盘口</b>：11 个美国站的桶名是华氏，其余是摄氏，站点格里注明了是哪一种；
  「实测」与 TAF 都换算成该站盘口的单位显示，所以一行之内不会出现摄氏和华氏并排。</div>
  <div class="why"><b>三个阶段是一个仓位、一本账。</b>P0 建仓，P0-M 与 P1 在同一个仓位上
  <b>加仓</b>（只加不减：可以加宽梯子、也可以把已有的一级买厚，但不卖回），
  一个站日只结算一次，命中按<b>最终持仓</b>判。所以「实得」是整条链的一个数，
  「投入」是开仓加历次加仓的总和；后面两个阶段的格子里写「并入本链」，
  不是没结算，是结算记在本链最后一次加仓那一格。
  同一个 2% 单站上限也管整条链——三个阶段各拿 2% 就是同一件事上押了 6%。</div>
  <div class="why"><b>桶数下界管的是挂单那一栏。</b>宪章 50/50a 要求席位≥3 桶，
  那是选桶层的规则——划掉的桶照样算数，所以「挂 3 腿成 1 腿」不违宪，标黄；
  只有<b>挂单</b>本身低于 3 桶才标红。建仓是渐进的：每条腿在买卖价之间挂 5 档，
  6 小时内盘口穿到哪档成交哪档，一档没穿到的腿就此不存在，因此持仓可以比选中的篮子窄、
  甚至中间缺一腿（选桶层强制连续，出洞必是成交造成的）。
  <b>三个完成率不是一回事：</b>「档」是市场把价压下来多少档，「份」是这买到了计划仓位的
  多少份，「金额」又与份数不同——便宜的腿没成交时，金额完成率会显著高于份数完成率。</div>
  <div class="why">${legend}</div>
  ${dates.map((day,index)=>block(day,index===0)).join("")}`;
}


function anomalies(){
  const S=D.selection||{};const an=S.anomalies||[];
  if(!an.length) return `<h2>十三、异常盘口</h2>
    <div class="card"><span class="dot green"></span> 未发现异常。</div>`;
  const rows=an.map(a=>`<tr>
    <td class="mono">${h(a.station_id)}</td><td class="mono">${h(a.event_date)}</td>
    <td>${h(a.stage)}</td>
    <td class="mono">${a.window.join(" + ")}</td>
    <td class="mono">${a.all_in_cost.toFixed(3)}</td>
    <td>${(a.win_probability*100).toFixed(1)}%</td>
    <td><span class="pill red">${a.kind==="basket_under_one_cent"?"虚价（整篮不足1分）":"声称超市场10倍"}</span></td>
  </tr>`).join("");
  return `<h2>十三、异常盘口</h2>
  <p class="sub">这些不是机会，是信号：整篮报价不足一分（虚价）、或声称概率超市场定价十倍，
  几乎总是无人出价那侧的挂单，真下单填不满。</p>
  <div class="card scroll"><table><thead><tr>
    <th>站点</th><th>事件日</th><th>阶段</th><th>窗口</th><th>成本</th><th>声称</th><th>类型</th>
  </tr></thead><tbody>${rows}</tbody></table></div>`;
}


const pct1 = (v) => v == null ? "—" : (v * 100).toFixed(1) + "%";
const deg2 = (v) => v == null ? "—" : v.toFixed(2) + " °C";

function dispersion(){
  const S = D.skill || {}; const rows = S.rows || [];
  if(!rows.length) return "";
  const body = rows.map(r => `<tr>
    <td><b>${h(r.label)}</b></td>
    <td>${h(r.lead_label)}</td>
    <td class="mono">${r.runs.toLocaleString()}</td>
    <td class="mono">${r.members_per_run == null ? "—" :
      r.members_per_run.toFixed(0)}</td>
    <td class="mono">${deg2(r.member_sigma_c)}</td>
    <td class="mono">${r.paired.toLocaleString()}</td>
    <td class="mono">${r.spread_factor == null ? "—" :
      r.spread_factor.toFixed(2) + "×"}</td>
    <td class="mono">${deg2(r.served_width_c)}</td>
    <td class="mono">${deg2(r.corrected_rmse_c)}</td>
    <td class="mono ${r.width_ratio == null ? "" :
      (r.width_ratio >= 0.85 && r.width_ratio <= 1.15 ? "up" : "down")}">${
      r.width_ratio == null ? "—" : r.width_ratio.toFixed(2) + "×"}</td>
  </tr>`).join("");
  const bare = [...new Set(rows.filter(r => !r.paired).map(r => r.label))];
  return `<h2>四、四大模型的离散度</h2>
  <p class="sub">离散度是一个集合预报里几十个成员之间的分歧，也是模型对自己唯一的
  不确定性表态：成员挤在一起就是"我很有把握"，散开就是"这天不好说"。
  统计窗口 ${h(S.summary.window_start)} 至 ${h(S.summary.window_end)}
  （${S.summary.window_days} 天）。</p>
  <div class="why"><b>怎么看：</b>「成员离散度」是模型原始的成员标准差；系统不直接
  用它，而是乘一个「服务放大」系数再拿去出概率——因为集合预报普遍过于自信，
  成员分歧一向小于实际会犯的错。<b>最右边的「宽度校准比」才是结论：</b>
  它是校正后的实际误差除以服务宽度，1.00× 表示放大得刚好，
  大于 1 表示放大之后仍然偏窄（还是过于自信），小于 1 表示放过了头。</div>
  <table style="margin-top:12px"><thead><tr>
    <th>模型</th><th>提前量</th><th>run 数</th><th>成员/run</th>
    <th>成员离散度 σ</th><th>可比 run</th><th>服务放大</th><th>服务宽度</th>
    <th>校正后 RMSE</th><th>宽度校准比</th>
  </tr></thead><tbody>${body}</tbody></table>
  <div class="muted" style="margin-top:6px">左边三列按全部 run 统计；
  「服务放大」之后的四列只能在有校正表可用的那部分 run 上测，
  所以另立「可比 run」一列说明它们的分母。${bare.length ?
    `<b>${bare.join("、")}</b> 在本窗口内一个可比 run 都没有：
     它现存的校正表是按旧的 d0/d1/d2 口径、且挂在聚合 cycle 上拟合的，
     和它今天每一次 native 采集所带的 run cycle 对不上，因此右侧四列为空。`
    : ""}</div>`;
}

function hitRate(){
  const S = D.skill || {};
  const rows = S.rows || []; const totals = S.totals || [];
  if(!rows.length) return "";
  const cards = totals.map(t => {
    const gain = t.paired ?
      t.paired_calibrated_rates[1] - t.paired_raw_rates[1] : null;
    return `<div class="card">
      <div style="display:flex;align-items:center;gap:8px">
        <b>${h(t.label)}</b><span style="flex:1"></span>
        <span class="muted">${t.scored.toLocaleString()} 个已结算 run</span>
      </div>
      <div class="big" style="margin-top:6px">${pct1(t.raw_rates[1])}</div>
      <div class="muted">原始预测落在真值 ±1 桶内</div>
      <div style="margin-top:9px;font-size:13.5px">
        精准命中 ${pct1(t.raw_rates[0])} · ±2 桶 ${pct1(t.raw_rates[2])}</div>
      ${t.paired ? `<div class="why" style="margin-top:10px">
        同样 ${t.paired.toLocaleString()} 个 run 上，校正后 ±1 桶
        <b class="up">${pct1(t.paired_calibrated_rates[1])}</b>
        （校正前 ${pct1(t.paired_raw_rates[1])}，
        ${gain >= 0 ? "+" : ""}${(gain * 100).toFixed(1)} 个百分点）</div>`
      : `<div class="why" style="margin-top:10px">
        这个模型在本窗口内没有自己的校正表，无法给出校正后数字</div>`}
    </div>`;
  }).join("");
  const raw = rows.map(r => `<tr>
    <td><b>${h(r.label)}</b></td>
    <td>${h(r.lead_label)}</td>
    <td class="mono">${r.scored.toLocaleString()}</td>
    <td class="${r.raw_rates[0] >= 0.25 ? "up" : ""}">${
      pct1(r.raw_rates[0])}</td>
    <td>${pct1(r.raw_rates[1])}</td>
    <td>${pct1(r.raw_rates[2])}</td>
    <td class="mono ${r.signed_bias_c == null ? "" :
      (Math.abs(r.signed_bias_c) <= 0.5 ? "up" : "down")}">${
      r.signed_bias_c == null ? "—" :
      (r.signed_bias_c >= 0 ? "+" : "") + r.signed_bias_c.toFixed(2)}</td>
    <td class="mono">${deg2(r.mae_c)}</td>
  </tr>`).join("");
  const arrow = (before, after) => {
    if(before == null || after == null) return "—";
    const better = after > before;
    return `<span class="muted">${pct1(before)}</span> →
      <b class="${better ? "up" : (after < before ? "down" : "")}">${
      pct1(after)}</b>`;
  };
  const cal = rows.map(r => r.paired ? `<tr>
    <td><b>${h(r.label)}</b></td>
    <td>${h(r.lead_label)}</td>
    <td class="mono">${r.paired.toLocaleString()}</td>
    <td>${arrow(r.paired_raw_rates[0], r.paired_calibrated_rates[0])}</td>
    <td>${arrow(r.paired_raw_rates[1], r.paired_calibrated_rates[1])}</td>
    <td>${arrow(r.paired_raw_rates[2], r.paired_calibrated_rates[2])}</td>
    <td class="mono">${deg2(r.paired_mae_c)}</td>
  </tr>` : `<tr>
    <td><b>${h(r.label)}</b></td>
    <td>${h(r.lead_label)}</td>
    <td class="mono">0</td>
    <td colspan="4" class="muted">这一格的 ${r.uncorrected.toLocaleString()}
      个 run 都没有事件日之前就已拟合好的校正表，不参与对比</td>
  </tr>`).join("");
  return `<h2>五、四大模型的命中率：原始 vs 校正后</h2>
  <p class="sub">把一个模型的成员均值当成它的答案，看它落在市场的哪个桶里，
  再和当天结算真值所在的桶比。桶按各站自己的市场刻度数——美国站 2°F 一档，
  其余 1°C 一档——所以"差一个桶"在各站是同一件事。</p>
  <div class="grid g4" style="margin-top:12px">${cards}</div>
  <div class="why" style="margin-top:12px"><b>为什么原始命中率这么低：</b>
  全球集合预报没有见过某一个跑道旁的温度计。它给的是格点上的温度，
  站点的地形、下垫面、仪器位置带来的系统性偏差要靠每站的偏差订正表补回来，
  这正是下面第二张表在量的东西。</div>
  <table style="margin-top:12px"><thead><tr>
    <th>模型</th><th>提前量</th><th>已结算 run</th><th>精准命中</th>
    <th>±1 桶内</th><th>±2 桶内</th><th>平均偏差</th><th>平均绝对误差</th>
  </tr></thead><tbody>${raw}</tbody></table>
  <div class="muted" style="margin-top:6px">「平均偏差」是预报减真值：
  负数表示这个模型在这一提前量上系统性报冷。</div>
  <h2 style="margin-top:28px">校正后（同样本对比）</h2>
  <div class="why"><b>两点必须说明：</b>一是用的校正表必须是<b>事件日之前</b>就已经
  拟合好的那一版，所以它没见过被打分的这一天；二是左右两列跑在<b>同一批 run</b> 上，
  只有约三成 run 在当时已经有可用的校正表，拿全样本的原始命中率去比这三成的
  校正后命中率，比的是覆盖面而不是校正本身。</div>
  <table style="margin-top:12px"><thead><tr>
    <th>模型</th><th>提前量</th><th>可比 run</th><th>精准命中 原始→校正</th>
    <th>±1 桶内 原始→校正</th><th>±2 桶内 原始→校正</th>
    <th>校正后平均绝对误差</th>
  </tr></thead><tbody>${cal}</tbody></table>`;
}


function materialization(){
  const T = D.truth || {}; const cov = T.coverage || [];
  if(!cov.length) return "";
  const s = T.sweep || {};
  // Only the days with a hole (user ruling, 2026-08-21).  Three weeks of
  // "45/45 齐了" is twenty rows of nothing happening, and the one row that
  // matters is found by scanning past them.  The complete days are counted
  // in a line under the table rather than dropped silently: a table that
  // shows only exceptions has to say how many non-exceptions it hid, or an
  // empty table reads as a broken query instead of a clean fleet.
  const gapDays = cov.filter(c => c.missing > 0);
  const cleanDays = cov.length - gapDays.length;
  const rows = gapDays.map(c => `<tr>
    <td class="mono">${h(c.event_date)}</td>
    <td class="mono">${c.eligible}</td>
    <td class="mono">${c.materialized}</td>
    <td class="mono ${c.settling ? "" : "down"}">${c.missing}</td>
    <td class="muted">${c.settling ? "当天还在结算中" : "缺口"}</td>
  </tr>`).join("");
  const why = (T.refusals || []).map(r => `<tr>
    <td>${h(r.label || r.reason)}</td>
    <td class="mono">${r.count}</td>
    <td><span class="pill ${r.blocker_class === "AVAILABILITY" ?
      "amber" : "red"}">${r.blocker_class === "AVAILABILITY" ?
      "证据不全" : "证据矛盾"}</span></td>
    <td class="mono muted">${h(r.example)}</td>
  </tr>`).join("");
  return `<h2>十九、真值物化健康度</h2>
  <p class="sub">观测进来只是第一步：还要把一整天的报文收敛成"这个站这天的最高温"
  一行，才有东西给预报打分。这一节看的是这一步有没有落下。</p>
  <div class="why"><b>为什么单独看它：</b>缺一行真值几乎从来不是缺观测——
  2026-08-17 查到 08-11 只有 13/45 站有真值行，而档案里那天 45 个站全都在，
  每站上千条报文。<b>只是没人问过它们，而错过的日子没有任何东西会重试。</b>
  下游每一层拟合都按已结算天数算，所以一个洞不是耽误一个格子一天，
  是耽误所有本该共用那一天的组合。</div>
  <div class="grid g4" style="margin-top:12px">
    <div class="card"><div class="muted">上次清扫</div>
      <div class="big">${hours(T.sweep_age_hours)}</div>
      <div class="muted">每 30 分钟自动跑一次 ·
        本次写入 ${(s.committed || 0).toLocaleString()} 个站日${
        T.deferred ? " · " + T.deferred + " 个让给了常驻写入" : ""}</div></div>
    <div class="card"><div class="muted">已结算但还没物化</div>
      <div class="big ${T.outstanding ? "down" : "up"}">${T.outstanding}</div>
      <div class="muted">这一格不为零才是真的落后</div></div>
    <div class="card"><div class="muted">当天还在结算中</div>
      <div class="big">${T.still_open}</div>
      <div class="muted">各站按自己的本地日收盘，不是缺口</div></div>
    <div class="card"><div class="muted">被证据本身挡住</div>
      <div class="big ${T.refused ? "down" : "up"}">${T.refused}</div>
      <div class="muted">下表列出分别卡在什么上</div></div>
  </div>
  ${why ? `<div class="card" style="margin-top:12px">
    <div class="muted">这些站日清扫也写不了，原因如下</div>
    <table style="margin-top:8px"><thead><tr>
      <th>卡在什么上</th><th>站日数</th><th>类型</th><th>例</th>
    </tr></thead><tbody>${why}</tbody></table>
    <div class="muted" style="margin-top:8px">「证据不全」会随补采自愈；
      「证据矛盾」要人看一眼，不会自己好。</div>
  </div>` : ""}
  ${rows ? `<table style="margin-top:12px"><thead><tr>
    <th>事件日</th><th>在册站数</th><th>已物化</th><th>缺口</th><th>状态</th>
  </tr></thead><tbody>${rows}</tbody></table>` : ""}
  <div class="muted" style="margin-top:8px">${rows
    ? `只列有缺口的事件日；近 ${cov.length} 天里另有 ${cleanDays} 天在册站全部物化，不列。`
    : `近 ${cov.length} 天每一天的在册站都有真值行，没有可列的缺口。`}</div>`;
}

function oosScores(){
  const S = D.scores || {}; const rows = S.rows || [];
  if(!rows.length) return "";
  const n3 = (v) => v == null ? "—" : v.toFixed(3);
  const body = rows.map(r => `<tr>
    <td>${h(r.lead_label)}</td>
    <td class="mono">${r.scopes}</td>
    <td class="${r.pass_rate == null ? "" :
      (r.pass_rate >= 0.75 ? "up" : "down")}">${pct1(r.pass_rate)}</td>
    <td class="mono">${n3(r.crps)}</td>
    <td class="mono muted">${n3(r.crps_base)}</td>
    <td class="mono ${r.crps_skill == null ? "" :
      (r.crps_skill > 0 ? "up" : "down")}">${pct1(r.crps_skill)}</td>
    <td class="mono">${n3(r.brier)}</td>
    <td class="mono muted">${n3(r.brier_base)}</td>
    <td class="mono ${r.brier_skill == null ? "" :
      (r.brier_skill > 0 ? "up" : "down")}">${pct1(r.brier_skill)}</td>
    <td class="mono">${n3(r.nll)}</td>
    <td class="mono muted">${n3(r.nll_base)}</td>
  </tr>`).join("");
  return `<h2>七、OOS 打分：CRPS / Brier / NLL</h2>
  <p class="sub">这是门自己用的分数，全部在样本外的 holdout 上算。
  近 7 天共 ${S.scope_count.toLocaleString()} 个站×提前量×cycle 被评过，
  最近一次 ${hours(S.latest_age_hours)}。</p>
  <div class="why"><b>三个分数各管一件事：</b>CRPS 管整条分布离真值多远（越小越好，
  单位是度）；Brier 管每个桶的概率说得准不准；NLL 是门做取舍时真正比的那个数。
  <b>光看绝对值没有意义</b>——它随站点、季节、桶宽变。所以每个分数旁边都摆着
  同样日子上的气候基准，右边的「胜出」＝比气候好了百分之多少，
  这才是可以跨站比较的一个数。</div>
  <table style="margin-top:12px"><thead><tr>
    <th>提前量</th><th>评过的格子</th><th>通过率</th>
    <th>CRPS</th><th>气候</th><th>胜出</th>
    <th>Brier</th><th>气候</th><th>胜出</th>
    <th>NLL</th><th>气候</th>
  </tr></thead><tbody>${body}</tbody></table>
  <div class="muted" style="margin-top:6px">窗口口径
    ${h((rows[0] || {}).profile || "")}：5 个训练日 + 3 天共享 holdout。
    没通过的格子不是坏事，是门在挡——它们不会被拿去服务。</div>`;
}

function centerWidth(){
  const rows = D.center_width || [];
  if(!rows.length) return "";
  const cards = rows.map(r => `<div class="card">
    <div style="display:flex;align-items:center;gap:8px">
      <span class="dot ${r.health}"></span><b>${h(r.stage)}</b>
      <span style="flex:1"></span>
      <span class="pill ${r.health}">${h(r.state)}</span>
    </div>
    <div class="muted" style="margin-top:6px">${h(r.meaning)}</div>
    <div style="display:flex;gap:22px;margin-top:12px">
      <div><div class="muted">中心 δ</div>
        <div class="big ${r.delta_c == null ? "" :
          (Math.abs(r.delta_c) < 1e-9 ? "flat" :
           (r.delta_c > 0 ? "up" : "down"))}">${
          r.delta_c == null ? "—" :
          (r.delta_c > 0 ? "+" : "") + r.delta_c.toFixed(2)}</div>
        <div class="muted">°C，整条分布平移</div></div>
      <div><div class="muted">宽度 λ</div>
        <div class="big ${r.width_lambda == null ? "" :
          (Math.abs(r.width_lambda - 1) < 1e-9 ? "flat" :
           (r.width_lambda < 1 ? "up" : "down"))}">${
          r.width_lambda == null ? "—" : r.width_lambda.toFixed(3)}</div>
        <div class="muted">×，小于 1 是收窄</div></div>
    </div>
    <div class="muted" style="margin-top:10px">
      证据 ${r.serving_days} 天${r.serving_cells ?
        " · " + r.serving_cells.toLocaleString() + " 个格子" : ""} ·
      上次拟合 ${hours(r.fit_age_hours)}</div>
    ${r.forward_nll_gain == null ? "" : `<div class="muted">
      前瞻 NLL 收益 ${r.forward_nll_gain.toFixed(4)}${
      r.min_forward_nll_gain == null ? "" :
      "，门槛 " + r.min_forward_nll_gain}</div>`}
  </div>`).join("");
  return `<h2>八、中心/宽度两标量</h2>
  <p class="sub">最后一层修正只有两个数：把整条分布左右平移多少（中心 δ），
  再横向拉宽或收窄多少倍（宽度 λ）。δ=0、λ=1 就是原样不动。</p>
  <div class="why"><b>为什么必须按阶段分开拟合：</b>P0 面对的是一堆模型融出来的
  分布，P1 面对的是当天实测已经把它收窄之后的分布——同一对数字不可能对两者都成立。
  一个阶段还没接线或还在攒证据，是按原样服务，不是出错。
  <b>只有前瞻试算确实变好（NLL 收益超过门槛）才会换上去</b>，试算变差就退回原样。</div>
  <div class="grid g3" style="margin-top:12px">${cards}</div>`;
}


// Click a header to sort by that column.  Numbers, percentages, prices and
// odds all read as numbers; anything else falls back to text, and an em dash
// always sinks so empty cells never crowd the top of either direction.
function makeSortable(table){
  // Direct children only.  A stage cell now carries its own per-leg table,
  // so a descendant selector reaches into it: the outer table's header
  // collected the inner table's `th`, and sorting collected the inner
  // table's rows and re-appended them into the outer `tbody`.  The page
  // showed bucket names under the station column and rung counts under the
  // stage columns -- the inner table's rows, wearing the outer table's
  // header.
  const heads=[...table.querySelectorAll(":scope > thead > tr > th")];
  heads.forEach((th,index)=>{
    th.style.cursor="pointer";
    th.title="点击排序";
    th.addEventListener("click",()=>{
      const body=table.querySelector(":scope > tbody");
      if(!body) return;
      const rows=[...body.querySelectorAll(":scope > tr")];
      const asc=th.dataset.sortDir!=="asc";
      heads.forEach(o=>{o.dataset.sortDir="";o.dataset.sorted="";});
      th.dataset.sortDir=asc?"asc":"desc";
      th.dataset.sorted="1";
      const value=(row)=>{
        const cell=row.cells[index];
        if(!cell) return {n:null,s:""};
        // Sort on what the cell shows, not on what it hides.  ``textContent``
        // is recursive, so a folded-away per-leg table contributes its share
        // counts and rung prices to the sort key -- and the first number in
        // the cell is then whichever one the DOM happens to reach first.
        const shown=cell.cloneNode(true);
        shown.querySelectorAll("details,table").forEach(n=>n.remove());
        const text=shown.textContent.trim();
        if(!text||text==="—") return {n:null,s:""};
        const m=text.replace(/[,\s]/g,"").match(/-?\d+(\.\d+)?/);
        return {n:m?parseFloat(m[0]):null,s:text};
      };
      rows.sort((a,b)=>{
        const x=value(a),y=value(b);
        if(x.n===null&&y.n===null) return x.s.localeCompare(y.s)*(asc?1:-1);
        if(x.n===null) return 1;   // blanks sink either way
        if(y.n===null) return -1;
        return (x.n-y.n)*(asc?1:-1);
      });
      rows.forEach(r=>body.appendChild(r));
    });
  });
}

// P0-M is the one stage with no output at all, and the page used to say
// nothing about why.  It is not broken: the training lane runs every cycle
// for every station and refuses to fit on evidence it does not have.  Show
// the distance, per station, so the wait is a number instead of a mystery.

// One section throwing used to blank the whole page: the assembly is a
// single expression, so a ReferenceError anywhere in it means nothing is
// ever assigned to #app and the reader gets a white screen with no clue.
// A section that cannot render says so, in place, and the other twelve
// still render.
function safe(fn, name){
  try { return fn(); }
  catch (err) {
    return `<h2>${name}</h2><div class="card"><span class="dot red"></span>
      这一节渲染失败：<span class="mono">${String(err)}</span></div>`;
  }
}

// Six layers, six lights.  The colour and the sentence are the answer; the
// numbers underneath are only there for someone who wants to check it.
const VERDICT_WORD = {green:"良好", amber:"注意", red:"有问题", muted:"证据不足"};

function scorecard(){
  const S = D.scorecard || {};
  if(!S.available) return `<h2>九、模型校准总检</h2>
    <p class="sub">还没有已结算的预测可以评分。</p>`;
  const cards = (S.layers||[]).map(L => `<div class="card">
    <div style="display:flex;align-items:center;gap:8px">
      <span class="dot ${L.health}"></span><b>${h(L.name)}</b>
      <span style="flex:1"></span>
      <span class="pill ${L.health}">${VERDICT_WORD[L.health]||""}</span>
    </div>
    <div class="muted" style="margin-top:2px">${h(L.question)}</div>
    <div style="margin-top:10px;font-size:14.5px;line-height:1.5">
      ${h(L.verdict)}</div>
    <div class="muted" style="margin-top:8px">${h(L.detail)}</div>
  </div>`).join("");

  const leadRows = (S.rows||[]).map(r => `<tr>
    <td>${h(r.lead_label)}</td>
    <td class="mono muted">${r.days.toLocaleString()}</td>
    <td class="mono"><span class="dot ${r.bias_health}"></span>
      ${r.bias_rungs == null ? "—" :
        (r.bias_rungs>0?"+":"")+r.bias_rungs.toFixed(2)} 档</td>
    <td class="mono"><span class="dot ${r.mae_health}"></span>
      ${r.mae_rungs == null ? "—" : r.mae_rungs.toFixed(2)} 档</td>
    <td class="mono"><span class="dot ${r.crps_health}"></span>
      ${pct1(r.crps_skill)}</td>
    <td class="mono muted">${pct1(r.brier_skill)}</td>
    <td class="mono"><span class="dot ${r.spread_health}"></span>
      ${r.spread_skill == null ? "—" : r.spread_skill.toFixed(2)}×</td>
  </tr>`).join("");

  const pit = (S.pit_bins||[]);
  const pitMax = Math.max(0.1, ...pit);
  const pitBars = pit.map((v,i) => `<div style="flex:1;text-align:center">
    <div style="height:52px;display:flex;align-items:flex-end">
      <div style="width:100%;height:${(v/pitMax*100).toFixed(0)}%;
        background:var(--${Math.abs(v-0.1)<=0.04?"green":
          (Math.abs(v-0.1)<=0.08?"amber":"red")});border-radius:3px 3px 0 0">
      </div></div>
    <div class="muted" style="font-size:10px">${(i*10)}</div>
  </div>`).join("");

  const fusion = (S.fusion_pairs||[]).filter(p => p.distance != null)
    .slice(0,8).map(p => {
      const names = Object.keys(p.served||{}).sort();
      const tone = p.distance <= 0.10 ? "green"
                 : (p.distance <= 0.25 ? "amber" : "red");
      return `<tr>
        <td class="mono">${h(p.station_id)}</td>
        <td class="mono muted">${h(p.lead_bucket)}</td>
        <td class="mono">${p.max_correlation.toFixed(2)}</td>
        <td class="mono"><span class="dot ${tone}"></span>
          ${(p.distance*100).toFixed(0)}%</td>
        <td class="muted" style="font-size:12px">${names.map(n =>
          `${h(n.split("_")[0])} ${(p.served[n]*100).toFixed(0)}→${
           ((p.optimum||{})[n]*100||0).toFixed(0)}%`).join(" · ")}</td>
      </tr>`;
    }).join("");

  return `<h2>九、模型校准总检：六层各自及格没有</h2>
  <p class="sub">近 45 天 ${S.sample_days.toLocaleString()} 个已结算站日、
  ${S.station_count} 个站，全部在样本外。总体
  <span class="pill ${S.overall}">${VERDICT_WORD[S.overall]||""}</span>，
  最近一次预测 ${hours(S.latest_age_hours)}。</p>
  <div class="why"><b>怎么看：</b>先看六个灯，绿＝这一层能用，黄＝能用但别压满，
  红＝这一层的数字现在不能直接拿来下注。每个灯下面那句话就是结论，再下面的数字
  只是给你复核用的。<br>
  <b>为什么用「档」不用「度」：</b>盘口有摄氏和华氏两种，华氏每档 2°F（1.11°C）、
  摄氏每档 1°C，用度会把两种盘口按不同标准打分。而且真正要押的就是档位，
  所以「差几档」才是会亏钱的那个误差。<br>
  <b>红黄绿的线划在哪：</b>偏差 0.3/0.8 档、平均误差 1.0/1.5 档（半档起就会系统性押错）；
  概率分要赢气候基准 10% 以上；ECE 与最差分箱偏离 5%/10%；
  宽度/误差偏离 1.0 超过 0.20/0.45；自举区间超过数值本身 15%/30% 就说明天数还不够。</div>
  <div class="grid g3" style="margin-top:12px">${cards}</div>

  <h2 style="margin-top:26px">按提前量拆开</h2>
  <p class="sub">越往后越难，这张表看的是哪一档提前量开始撑不住。</p>
  <table><thead><tr>
    <th>提前量</th><th>站日</th><th>偏差</th><th>平均误差</th>
    <th>CRPS 胜气候</th><th>Brier 胜气候</th><th>宽度/误差</th>
  </tr></thead><tbody>${leadRows}</tbody></table>

  <p class="sub" style="margin-top:26px">「说了多少 vs 真发生了多少」残差表
  已并入第六节，与产生它的修正系数放在同一条闭环里；本节的校准灯仍由同一份
  数据打分。</p>

  <h2 style="margin-top:26px">PIT：分布形状正不正</h2>
  <p class="sub">如果分布形状是对的，真值落在分布各个位置的次数应该一样多，
  下面十根柱子应该一样高（每根 10%）。中间鼓＝分布太宽，两头翘＝分布太窄。</p>
  <div style="display:flex;gap:4px;align-items:flex-end;margin-top:10px">
    ${pitBars}</div>
  <div class="muted" style="margin-top:4px">横轴：真值在预测分布中的位置（百分位）</div>

  ${fusion ? `<h2 style="margin-top:26px">融合权重 vs 协方差最优</h2>
  <p class="sub"><b>现役权重就是最小方差（收缩协方差，2026-08-18 上线，
  影子先行后晋升 +0.0197 bits，282/282 个 scope 在役）。</b>这张表因此是
  一致性自检而非改进提示：右边是用存档的残差相关性当场重解的最优，
  距离应当接近零；不为零的地方是族权重上限咬住了，或该 scope 落回了
  逆 RMSE 兜底（共享训练窗太短撑不起协方差时）。距离若系统性变大，
  说明上线的解和存档的相关性对不上了，先查数据再查解法。</p>
  <table><thead><tr>
    <th>站</th><th>提前量</th><th>最大相关</th><th>离最优</th>
    <th>现在 → 最优</th>
  </tr></thead><tbody>${fusion}</tbody></table>
  <div class="muted" style="margin-top:6px">
    只列偏离最大的几个。相关高但偏离小＝模型重复但权重已经知道，不亏。</div>` : ""}`;
}

function servingWeights(){
  const W = D.serving_weights || {}; const rows = W.rows || [];
  if(!rows.length) return "";
  const summary = W.summary || [];
  const pct = w => w == null ? "—" : (w * 100).toFixed(0) + "%";
  const cards = summary.map(s => `<div class="card">
    <div class="muted">${h(s.label)}</div>
    <div class="big">${s.seats} <span class="muted"
      style="font-size:14px">/ ${W.scope_count} 个格子在场</span></div>
    <div class="muted">上场时权重中位数 ${pct(s.median_weight)} ·
      参赛 ${s.candidacies} 次，落选 ${s.eliminated} 次</div></div>`).join("");
  const leadLabels = {};
  rows.forEach(r => { leadLabels[r.lead_bucket] = r.lead_label; });
  const leadOrder = Object.keys((summary[0] || {}).by_lead || {})
    .filter(bucket => bucket in leadLabels);
  const matrixHead = leadOrder.map(bucket =>
    `<th>${h(leadLabels[bucket])}</th>`).join("");
  const matrixBody = summary.map(s => `<tr><td><b>${h(s.label)}</b></td>${
    leadOrder.map(bucket => {
      const cell = (s.by_lead || {})[bucket];
      if(!cell || !cell.seats) return `<td class="muted">—</td>`;
      return `<td class="mono">${pct(cell.median_weight)}
        <span class="muted">×${cell.seats}</span></td>`;
    }).join("")}</tr>`).join("");
  const modelCols = summary.map(s => s.label);
  const detailBody = rows.map(r => {
    const stale = W.stale_cut && r.holdout_end < W.stale_cut;
    const cells = modelCols.map(label => {
      if(label in r.weights)
        return `<td class="mono"><b>${pct(r.weights[label])}</b></td>`;
      if(r.candidates.includes(label))
        return `<td class="muted">落选</td>`;
      return `<td class="muted">—</td>`;
    }).join("");
    return `<tr><td><b>${h(r.station)}</b></td>
      <td>${h(r.lead_label)}</td><td class="mono">${h(r.run_cycle)}</td>
      ${cells}
      <td class="mono">${r.candidate_count}→${r.selected_count}</td>
      <td class="mono ${stale ? "down" : ""}">${h(r.holdout_end)}</td></tr>`;
  }).join("");
  return `<h2>三、四大模型的权重：谁在场、占多少</h2>
  <p class="sub">这里读的是服务路径本身——每个「站×提前量×cycle」格子当前激活的
  组合指针，连到它绑定的选拔证据，把里面的权重原样摊开。不是权重表的行数统计，
  就是 P0 此刻在用的那份配比。</p>
  <div class="why"><b>怎么看：</b>每个格子的席位都是在完全相同的检验窗上赛出来的：
  参赛的模型一起在最近几天的共同 holdout 上打分，CRPS 最低的子集当选，权重随之而来。
  「落选」＝参赛了但没打赢（权重 0），「—」＝这个格子没凑齐它的参赛条件。
  权重每天随新证据重赛，所以同一格子的配比会换代；
  「窗至」是该格子当前配比所依据的检验窗最后一天，
  日期旧说明这个格子还在服旧一轮的赛果——换代受快照新鲜度限速，逐格推进，属正常。</div>
  <div class="grid g4" style="margin-top:12px">${cards}</div>
  <table style="margin-top:12px"><thead><tr>
    <th>模型</th>${matrixHead}
  </tr></thead><tbody>${matrixBody}</tbody></table>
  <div class="muted" style="margin-top:6px">格子里是「上场时权重中位数 × 席位数」，
  按提前量分列。${W.stale_count ? `目前 <b>${W.stale_count}</b> 个格子仍在服
    ${h(W.stale_cut)} 之前的赛果（最新窗至 ${h(W.newest_holdout_end)}），
    重绑进行中。` : `全部格子的检验窗都在 ${h(W.newest_holdout_end)} 附近，无积压。`}</div>
  <details style="margin-top:10px">
    <summary class="muted" style="cursor:pointer">
      逐格明细：${W.scope_count} 个格子的完整权重（可排序）</summary>
    <table style="margin-top:8px"><thead><tr>
      <th>站</th><th>提前量</th><th>cycle</th>
      ${modelCols.map(label => `<th>${h(label)}</th>`).join("")}
      <th>候选→当选</th><th>窗至</th>
    </tr></thead><tbody>${detailBody}</tbody></table>
  </details>`;
}

// Sections of one kind stand together, and only one kind is on screen at a
// time (user ruling, 2026-08-21).  Twenty sections in a single column is a
// page nobody scrolls to the end of, and the order they were written in
// interleaved the three questions being asked of them: the model's skill,
// the market's account of it, and whether the data feeding both arrived.
// Reading any one of those meant scrolling past the other two.
//
// The section numbers follow this grouping rather than the old order.  A
// number that no longer matches its neighbours is a worse handle than a
// renumbered one, because it only reads as an accident.
const GROUPS = [
  {id:"model", label:"模型与校准", note:"一～九节：数据到货、订正与权重、离散度与命中率、各层校准打分",
   sections:[[members,"成员"],[tables,"偏差订正与权重"],
     [servingWeights,"四大模型权重"],[dispersion,"模型离散度"],
     [hitRate,"模型命中率"],[stages,"概率分档校准"],[oosScores,"OOS 打分"],
     [centerWidth,"中心/宽度"],[scorecard,"模型校准总检"]]},
  {id:"market", label:"市场与执行", note:"十～十六节：覆盖与 EV、逐站与逐日的账、挂单执行、没建仓的原因、分布与持仓分家",
   sections:[[coverage,"覆盖"],[stationSummary,"站汇总"],[roster,"站名册"],
     [anomalies,"异常"],[basketHit,"篮子命中率"],
     [makerProbe,"挂单影子探针"],[refusalStats,"未建仓原因"],
     [windowDivergence,"分布与持仓分家"]]},
  {id:"data", label:"数据与链路", note:"十七～十九节：链路每一环、数据缺口、真值物化",
   sections:[[chain,"链路"],[gaps,"数据缺口"],[materialization,"真值物化"]]},
];

const tabs = `<nav class="tabs" id="tabs">${
  GROUPS.map(g => `<button type="button" data-tab="${g.id}">${g.label}
    <span class="muted">${g.sections.length}</span></button>`).join("")
}<button type="button" data-tab="all">全部</button></nav>`;

const groupHtml = GROUPS.map(g => `<section class="group" data-group="${g.id}">
  <div class="grouptitle">${g.label}<span class="muted">${g.note}</span></div>
  ${g.sections.map(([fn, name]) => safe(fn, name)).join("")}</section>`).join("");

document.getElementById("app").innerHTML =
  [[head, "页头"], [overview, "总览"], [daily, "每日统计"]]
    .map(([fn, name]) => safe(fn, name)).join("") +
  tabs + groupHtml +
  `<footer>数据生成于 ${h(D.generated_at_utc)} · 只读页面，不写任何数据</footer>`;

// Which group is showing survives a reload, because the page is reloaded
// far more often than the question being asked of it changes.  "全部" is
// the old single-column page, one click away, for anyone reading it top to
// bottom or searching it with the browser's own find.
(function(){
  const nav = document.getElementById("tabs");
  if(!nav) return;
  let current = GROUPS[0].id;
  try{
    const saved = localStorage.getItem("board-tab");
    if(saved) current = saved;
  }catch(e){}
  const known = new Set(GROUPS.map(g=>g.id).concat(["all"]));
  if(!known.has(current)) current = GROUPS[0].id;
  const paint = () => {
    document.querySelectorAll(".group").forEach(sec => {
      sec.hidden = current !== "all" && sec.dataset.group !== current;
    });
    nav.querySelectorAll("button").forEach(b => {
      b.dataset.active = b.dataset.tab === current ? "1" : "";
    });
  };
  nav.addEventListener("click", (ev) => {
    const button = ev.target.closest("button[data-tab]");
    if(!button) return;
    current = button.dataset.tab;
    try{ localStorage.setItem("board-tab", current); }catch(e){}
    paint();
  });
  paint();
})();

document.querySelectorAll("table").forEach(makeSortable);

// Prose folds away by default (user ruling, 2026-08-18).  Forty-six
// explanation blocks is most of the page's height, and every one of them is
// something a reader needs once and then never again -- while the numbers
// they explain are what the page is opened for.  Done here rather than in
// each section because the rule is "all of it", and forty-six hand-written
// wrappers is forty-six chances to miss one.
//
// Consecutive blocks share a fold: three separate "说明" toggles in a row
// is the same clutter in a smaller font.
document.querySelectorAll(".why,.sub").forEach(node=>{
  // Only fold prose.  A block holding a table or a card is not an
  // explanation that ran long -- it is a mis-closed tag that swallowed the
  // section, and folding it hides the numbers instead of the words.  Section
  // six closed a `div.why` with `</p>`, so its whole daily table lived
  // inside the explanation and disappeared behind the toggle.  The tag is
  // fixed; this makes the next one cost a stray blue box rather than a
  // section.
  if(node.querySelector("table,.card")) return;
  const prev=node.previousElementSibling;
  if(prev&&prev.classList.contains("note")){prev.appendChild(node);return;}
  const box=document.createElement("details");
  box.className="note";
  box.innerHTML="<summary>说明</summary>";
  node.parentNode.insertBefore(box,node);
  box.appendChild(node);
});

// The button lives in the static body rather than in a rendered section, so
// it still works on a page where every section failed to draw -- which is
// exactly when a reader is most likely to be squinting at it.
(function(){
  const button = document.getElementById("theme");
  const paint = () => {
    const dark = document.documentElement.dataset.theme !== "light";
    button.textContent = dark ? "☀ 亮色" : "☾ 暗色";
    button.title = dark ? "切换到亮色" : "切换到暗色";
  };
  button.addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme !== "light";
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try{ localStorage.setItem("board-theme", dark ? "light" : "dark"); }
    catch(e){}
    paint();
  });
  paint();
})();
</script></body></html>"""


def render_html(payload: dict[str, Any]) -> str:
    return HTML_TEMPLATE.replace(
        "__PAYLOAD__", json.dumps(payload, ensure_ascii=False)
    )


def serve(db: Path, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api"):
                body = json.dumps(
                    build_payload(db), ensure_ascii=False
                ).encode()
                content_type = "application/json; charset=utf-8"
            else:
                body = render_html(build_payload(db)).encode()
                content_type = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"校准看板: http://127.0.0.1:{port}/  (Ctrl-C 停止)")
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--html-out", type=Path)
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    if args.json:
        print(json.dumps(
            build_payload(database), ensure_ascii=False, sort_keys=True
        ))
        return 0
    if args.html_out:
        args.html_out.write_text(
            render_html(build_payload(database)), encoding="utf-8"
        )
        print(str(args.html_out))
        return 0
    serve(database, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
