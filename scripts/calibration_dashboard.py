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
import ast
import collections
import hashlib
import inspect
import json
import math
import os
import re
import statistics
from bisect import bisect_left
from collections import Counter, defaultdict
import random
import sqlite3
import time
import sys
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from collections.abc import Mapping
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from paper_trading_ledger import INITIAL_CAPITAL_USDC  # noqa: E402
from selection_market_dashboard import (  # noqa: E402
    STATION_LABELS,
    STATS_START_EVENT_DATE,
    build_payload as build_selection_payload,
)
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STATUS_JSON = ROOT / "data" / "weather_basket_yes_status.json"
TRUTH_SWEEP_STATUS = ROOT / "data" / "settlement_truth_sweep_status.json"


def _bridge_envelope_payload(conn, checkpoint_id, sha, stored) -> dict:
    """History rows since 2026-08-26 carry no inline envelope body."""

    from bayes_basket_target.probability_bridge import (  # noqa: PLC0415
        history_envelope_payload,
    )
    return history_envelope_payload(
        conn,
        checkpoint_id=str(checkpoint_id),
        envelope_content_sha256=str(sha),
        stored=str(stored) if stored else None,
    )

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


#: Lane-level failures worth waking someone for, keyed by the ingress the
#: resident publishes them under.  Per-candidate refusals are deliberately
#: NOT here: "no solvable support window" is the objective declining a
#: station-day, which is ordinary business and already has its own section.
#: What belongs here is a lane that could not run at all.
_PRODUCTION_LANES = (
    ("resident_probability_chain", "概率链"),
    ("resident_p0_ingress", "P0"),
    ("resident_p0m_ingress", "P0-M"),
    ("resident_p1_ingress", "P1"),
    ("market_ingress", "行情入口"),
    ("native_ensemble_ingress", "模型入口"),
    ("regime_ingress", "政权入口"),
)


def _production_health(status: Mapping[str, Any]) -> dict[str, Any]:
    """Whether the pipeline is producing, not merely whether it is alive.

    The heartbeat only says the process still runs its loop.  On
    2026-08-26 that was true for eleven hours while every ingress reported
    ``database is locked`` and the cycle produced nothing -- one station's
    TAF with no temperature group had taken P0-M to BLOCKED_FAIL_CLOSED,
    and an ungated third writer was holding the SQLite lock.  Both were
    plainly visible in the resident's own status file; nothing read them.
    """

    production = status.get("production")
    inflight = status.get("production_inflight")
    if not isinstance(production, Mapping):
        # A cycle in flight replaces the completed receipt with an
        # ``production_inflight`` marker, so "no receipt" is the ordinary
        # state for most of a cycle, not a fault -- but the marker carries
        # its own overdue flag, which is the stall signal this card exists
        # for.  Reading only the completed receipt made the card report
        # "未发布" on every render taken mid-cycle.
        if isinstance(inflight, Mapping):
            elapsed = inflight.get("elapsed_sec")
            overdue = bool(inflight.get("overdue"))
            seconds = (
                float(elapsed) if isinstance(elapsed, (int, float)) else None
            )
            note = (
                f"本轮进行中 {seconds:.0f} 秒"
                if seconds is not None else "本轮进行中"
            )
            if overdue:
                after = inflight.get("overdue_after_sec")
                note += (
                    f"，已超时（上限 {float(after):.0f} 秒）"
                    if isinstance(after, (int, float)) else "，已超时"
                )
            return {
                "production_health": (
                    "red" if seconds is not None and seconds > 900.0
                    else ("amber" if overdue else "green")
                ),
                "production_note": note,
                "production_state": "in_flight",
                "production_cycle_seconds": (
                    round(seconds, 1) if seconds is not None else None
                ),
                "production_blocked_lanes": [],
            }
        return {
            "production_health": "amber",
            "production_note": "常驻未发布 production receipt",
        }
    # The receipt's own verdict, before any lane is inspected.  A worker
    # that cannot open the SSOT never reaches a lane, so it publishes a
    # receipt with no lane maps at all: `state=blocked_fail_closed`,
    # `ok=false`, a reason, and `cycle_total` of a fraction of a
    # millisecond because it bailed immediately.  Every test below then
    # passes -- no blocked lanes, no stale `worker_checked_at` (the key is
    # absent on this shape), no slow cycle -- and the card printed
    # "周期正常产出" over a pipeline that had produced nothing for
    # nineteen hours.  Measured 2026-08-31: last decision 08-30T17:18Z,
    # every cycle since fail-closed on
    # `target_database_wal_checkpoint_busy`.  This function's docstring is
    # about catching exactly this and it was reading past it.
    state_word = str(production.get("state") or "").upper()
    fail_closed = (
        production.get("ok") is False
        or "BLOCK" in state_word
        or "FAIL" in state_word
    )
    if fail_closed:
        reason = str(production.get("reason") or "").strip()
        recovery = production.get("connection_recovery")
        if not reason and isinstance(recovery, Mapping):
            reason = str(recovery.get("reason") or "")
        note = "生产回执 fail-closed：" + (
            str(production.get("state") or "未命名状态")
        )
        if reason:
            # Head and tail, not the first eighty characters.  These reasons
            # are colon-chains whose informative end is last -- truncating
            # from the front kept "target_ssot_read_only_open_failed:
            # ResourceBoundError" and dropped the part that names what to
            # fix (``target_database_wal_checkpoint_busy``).
            parts = [seg for seg in reason.split(":") if seg]
            note += "·" + (
                f"{parts[0][:40]}…{parts[-1][:60]}"
                if len(parts) > 1 else reason[:80]
            )
        return {
            "production_health": "red",
            "production_note": note,
            "production_state": str(production.get("state") or ""),
            "production_checked_at": production.get("worker_checked_at"),
            "production_cycle_seconds": None,
            "production_blocked_lanes": [note],
        }
    stale_hours = _age_hours(production.get("worker_checked_at"))
    blocked: list[str] = []
    locked = 0
    for key, label in _PRODUCTION_LANES:
        lane = production.get(key)
        if not isinstance(lane, Mapping):
            continue
        state = str(lane.get("state") or "").upper()
        if "BLOCK" not in state and "FAIL" not in state:
            continue
        reason = str(lane.get("reason") or "")
        detail = reason.split(":")[-1][:40] if reason else ""
        if not detail:
            # A scoped halt (P1 correction/revision, one station-day)
            # carries no lane-level reason -- the detail lives only in the
            # ``blockers`` list, and the status file is overwritten every
            # cycle.  The 2026-08-29 16:03 P1 flap was undiagnosable an
            # hour later because this note read just "P1".
            scoped = [
                b for b in lane.get("blockers") or []
                if isinstance(b, Mapping)
            ]
            if scoped:
                first = scoped[0]
                reason = str(first.get("reason") or "")
                detail = (
                    f"{first.get('station_id')}|{first.get('event_date')}"
                    f"·{reason.split(':')[-1][:40]}"
                )
                if len(scoped) > 1:
                    detail += f" 等{len(scoped)}个站日"
        if not detail:
            # The probability chain is the one lane that blocks on behalf of
            # something else.  It carries no reason and no blockers of its
            # own: it names the stage that failed in ``blocked_stages`` and
            # keeps that stage's blockers one level down.  Reading only the
            # top level printed a bare "概率链" over a fully diagnosed P0-M
            # halt -- three station-days named, in the same document.
            stages = [
                str(item) for item in lane.get("blocked_stages") or []
                if str(item or "").strip()
            ]
            for stage in stages:
                nested = lane.get(stage.replace("-", "").lower())
                scoped = [
                    b for b in (
                        nested.get("blockers")
                        if isinstance(nested, Mapping) else None
                    ) or []
                    if isinstance(b, Mapping)
                ]
                if not scoped:
                    continue
                first = scoped[0]
                nested_reason = str(first.get("reason") or "")
                detail = (
                    f"{stage}·{first.get('station_id')}"
                    f"|{first.get('event_date')}"
                    f"·{nested_reason.split(':')[-1][:40]}"
                )
                if len(scoped) > 1:
                    detail += f" 等{len(scoped)}个站日"
                break
            if not detail and stages:
                detail = "、".join(stages)[:40]
        blocked.append(f"{label}{'·' + detail if detail else ''}")
        if "locked" in reason:
            locked += 1
    notes: list[str] = []
    health = "green"
    # Partial commitment is the fleet's normal shape: some scopes always sit
    # in a soft per-station refusal.  Name the split instead of painting the
    # whole cycle red -- committed count first, then the top refusal reasons,
    # so the reader sees "the cycle works, these scopes are waiting" rather
    # than an outage banner over nine committed live decisions.
    blocked_scope_count = int(production.get("blocked_scope_count") or 0)
    committed_scope_count = int(production.get("committed_scope_count") or 0)
    if blocked_scope_count and committed_scope_count:
        reasons = production.get("blocked_scope_reasons") or {}
        top = "、".join(
            f"{BLOCKED_SCOPE_REASON_LABELS.get(name, name)}×{count}"
            for name, count in sorted(
                reasons.items(), key=lambda kv: -kv[1]
            )[:3]
        )
        health = "amber"
        notes.append(
            f"{committed_scope_count} 个 scope 落账，"
            f"{blocked_scope_count} 个逐站阻断"
            + (f"（{top}）" if top else "")
        )
    if stale_hours is not None and stale_hours > 1.0:
        health = "red"
        notes.append(f"周期已 {stale_hours:.1f} 小时没有完成")
    if locked:
        health = "red"
        notes.append(f"{locked} 条车道被写锁挡住（单写者门失效？）")
    elif blocked:
        # A blocked lane with no lock error is a genuine fault too, but the
        # kind that usually names one station -- amber, not red.  Escalate
        # only: a stale cycle already went red above and a single blocked
        # lane must not talk it back down.
        if len(blocked) >= 3:
            health = "red"
        elif health == "green":
            health = "amber"
        notes.append("阻断车道：" + "、".join(blocked[:4]))
    # Blocking, beside latency.  The card below names where the seconds
    # inside a cycle went; until 2026-09-09 nothing on this page said a word
    # about the cycles that could not start.  The two are different faults
    # with the same symptom: a round of 533s against a 300s target is worth
    # one line, and the 35-to-53 minute gaps between those rounds -- one
    # capture sweep holding the single writer for its whole 1800s subprocess
    # timeout -- were worth rather more, and got a staleness note that named
    # nobody.  Waiting is judged against the same declared numbers as
    # running: a lane made to wait longer than the whole cycle is meant to
    # take is late by definition.
    overdue_sec = float(
        (
            production.get("target_cycle_runtime")
            if isinstance(production.get("target_cycle_runtime"), Mapping)
            else {}
        ).get("overdue_seconds") or 720.0
    )
    target_for_wait = float(
        (
            production.get("target_cycle_runtime")
            if isinstance(production.get("target_cycle_runtime"), Mapping)
            else {}
        ).get("target_seconds") or 300.0
    )
    waiting = status.get("production_waiting")
    waited = status.get("production_wait")
    wait_sec = None
    wait_by: list[str] = []
    wait_word = ""
    if isinstance(waiting, Mapping) and isinstance(
        waiting.get("waiting_sec"), (int, float)
    ):
        wait_sec = float(waiting["waiting_sec"])
        wait_by = [str(x) for x in (waiting.get("blocked_by") or [])]
        wait_word = "正在等写入轮次"
    elif isinstance(waited, Mapping) and isinstance(
        waited.get("waited_sec"), (int, float)
    ):
        wait_sec = float(waited["waited_sec"])
        wait_by = [str(x) for x in (waited.get("blocked_by") or [])]
        wait_word = "上轮开始前等了"
    if wait_sec is not None and wait_sec > target_for_wait:
        if wait_sec > overdue_sec:
            health = "red"
        elif health == "green":
            health = "amber"
        notes.append(
            f"{wait_word} {wait_sec:.0f} 秒"
            + (f"（被 {'、'.join(wait_by[:3])} 占住）" if wait_by else "")
        )
    cycle_ms = (production.get("stage_timings_ms") or {}).get("cycle_total")
    # From the receipt, so the board judges against what the running daemon
    # targets rather than a number typed here.  The fallbacks are the values
    # declared 2026-09-01 and only apply to a receipt written before they
    # were published.
    cycle_runtime = production.get("target_cycle_runtime")
    cycle_runtime = (
        cycle_runtime if isinstance(cycle_runtime, Mapping) else {}
    )
    target_sec = float(cycle_runtime.get("target_seconds") or 300.0)
    warn_sec = float(cycle_runtime.get("warn_seconds") or 450.0)
    if isinstance(cycle_ms, (int, float)) and cycle_ms > warn_sec * 1000:
        # Judged against the declared target, not against a number typed here.
        # This line used to read "> 600s（常态约 200-350）" -- a threshold and
        # a normal range that agreed with nothing else in the system, so a
        # cycle drifting from 350s to 850s said nothing until it passed 600.
        # That drift happened on 2026-09-01 and took 21 of 28 scopes with it:
        # the compile loop outran a sixty-second quote lease.
        health = "amber" if health == "green" else health
        # Name where the seconds went, on the same line that says there were
        # too many of them.  An overrun used to report only its total, so
        # every diagnosis started by asking the status file a question it had
        # already answered -- and the answer was usually read instead off the
        # point-in-time statement probe, which names whichever query happened
        # to be running, not whichever stage owns the cycle.  Three weeks of
        # "fixed again" came out of that gap.  ``unattributed`` is in the
        # ranking on purpose: a stage added after the last mark shows up here
        # as the biggest consumer rather than as silence.
        stages = production.get("stage_timings_ms") or {}
        top_stages = "、".join(
            f"{name} {float(value) / 1000:.0f}s"
            for name, value in sorted(
                (
                    (str(k), float(v))
                    for k, v in stages.items()
                    if k != "cycle_total" and isinstance(v, (int, float))
                ),
                key=lambda item: -item[1],
            )[:3]
            if value >= 1000.0
        )
        notes.append(
            f"单轮 {cycle_ms / 1000:.0f} 秒"
            f"（目标 {target_sec:.0f}，告警 {warn_sec:.0f}）"
            + (f"：{top_stages}" if top_stages else "")
        )
    return {
        "production_health": health,
        "production_note": "；".join(notes) or "周期正常产出",
        "production_state": str(production.get("state") or ""),
        "production_checked_at": production.get("worker_checked_at"),
        "production_cycle_seconds": (
            round(cycle_ms / 1000.0, 1)
            if isinstance(cycle_ms, (int, float)) else None
        ),
        "production_blocked_lanes": blocked,
        "production_wait_seconds": wait_sec,
        "production_wait_blocked_by": wait_by,
    }


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
    # Charter 105: a table carries the axis its cells are keyed on, and a
    # reader that cannot answer which one must not render it -- the two
    # partitions take the same shape ([lo, hi) pairs) and both "work".
    # The column is absent on a database written before 2026-09-07, where
    # every table was fitted on the claim.
    has_axis = any(
        str(col[1]) == "axis"
        for col in conn.execute(
            "PRAGMA table_info(calibration_band_reliability_table)"
        )
    )
    axis_column = "COALESCE(axis, 'claim')" if has_axis else "'claim'"
    stages = []
    for stage in BAND_STAGES:
        row = conn.execute(
            "SELECT band_table_sha256, factors_json, evidence_json,"
            " fitted_at_utc, serving_day_count, " + axis_column +
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
                "axis": "claim",
                "bands": [],
                "journal": [],
            })
            continue
        factors = json.loads(row[1])
        evidence = json.loads(row[2])
        fitted_at = row[3]
        days = int(row[4])
        axis = str(row[5] or "claim")
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
            "axis": axis,
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


TRUTH_LANDING_SINCE = "2026-08-15"
# Five-percent bands capped at 50%, with the first cell split at the leg
# floor (user rulings, 2026-08-25): 0-3% is the penny zone the 0.03 floor
# refuses to buy, and folding it into 0-5% hid exactly the boundary the
# floor is ruled on -- so this table alone carries the extra cut, on both
# margins and both cross-tab axes.  Section six's band table stays on the
# pure 5% grid (charter 60); the two rulers agree from 5% upward.
TRUTH_LANDING_PRICE_BANDS = ((0.00, 0.03), (0.03, 0.05)) + tuple(
    (round(step * 0.05, 2), round(step * 0.05 + 0.05, 2))
    for step in range(1, 10)
) + ((0.50, 1.01),)
TRUTH_LANDING_COARSE = TRUTH_LANDING_PRICE_BANDS


# The bias layer's half-life change, and the instant it began being fitted.
# Both are facts about a past deployment, so they are stated rather than
# derived -- the same two constants ``h5_ledger_readiness_check`` states.
H5_FROM_UTC = "2026-08-27T06:48:00Z"
# How far back to read bias history.  Only needs to reach far enough that
# every scope still has its last pre-H5 artifact to serve as the control.
H5_BASELINE_FROM_UTC = "2026-08-01"
# An ensemble mean over fewer members than this is not a forecast worth
# scoring a correction against.
MIN_MEMBERS_FOR_BIAS_CELL = 3


def collect_h5_bias_accuracy(conn: sqlite3.Connection) -> dict[str, Any]:
    """Did the shorter half-life put the corrected forecast closer to truth?

    The ledger cannot answer this yet -- one scoreable day carries 31
    chains, and the book needs far more than that to separate a parameter
    from a lottery -- but the calibration side can: every settled
    station-day gives a few hundred (system x lead x cycle) cells, and the
    counterfactual is arithmetic rather than a replay.  Each cell's raw
    ensemble mean is corrected twice, once by the bias the scope actually
    served under H5 and once by the last bias it had under H15, and the
    two absolute errors are compared against the same truth.

    Three rules this measurement lives by, each one a trap already fallen
    into once:

    * **As-of selection.**  Both biases are taken as of the event day's
      start, never the newest fitted.  Using the newest H5 artifact lets
      it see days it is being scored on.
    * **Cluster by station-day.**  The cells within one station-day share
      a station and a day's weather, so they are not independent samples.
      Counting them as such reported z = -5.21 where the honest clustered
      test says -1.38 -- the difference between "settled" and "not yet".
    * **Both sides needed.**  A scope that has no pre-H5 artifact, or has
      not yet been refit under H5, contributes nothing rather than a
      half-comparison.

    Accumulates on its own: every newly settled day adds its station-days
    to the sample, so the section sharpens without anyone re-running it.
    """

    h5_from = H5_FROM_UTC
    series: dict[tuple[str, str, str, str], list[tuple[str, float]]] = (
        defaultdict(list)
    )
    for station, system, lead, cycle, valid_from, params in conn.execute(
        "SELECT station_id, ensemble_system_id, lead_bucket, run_cycle,"
        " valid_from_utc, bias_parameters_json"
        " FROM calibration_native_ensemble_system_artifact"
        " WHERE valid_from_utc>=?"
        " ORDER BY valid_from_utc",
        (H5_BASELINE_FROM_UTC,),
    ):
        try:
            bias = json.loads(params).get("bias_c")
        except (TypeError, ValueError, AttributeError):
            continue
        if bias is None:
            continue
        series[(station, system, lead, cycle)].append(
            (str(valid_from), float(bias))
        )

    def _as_of(
        key: tuple[str, str, str, str],
        cutoff: str,
        *,
        before: str | None = None,
    ) -> float | None:
        chosen: float | None = None
        for valid_from, bias in series.get(key, ()):
            if valid_from > cutoff:
                break
            if before is not None and valid_from >= before:
                break
            chosen = bias
        return chosen

    def _first_h5_at(key: tuple[str, str, str, str], cutoff: str) -> bool:
        """Whether the scope had actually turned over to H5 by ``cutoff``."""

        latest = None
        for valid_from, _bias in series.get(key, ()):
            if valid_from > cutoff:
                break
            latest = valid_from
        return latest is not None and latest >= h5_from

    truth = {
        (str(row[0]), str(row[1])): float(row[2])
        for row in conn.execute(
            "SELECT station_id, local_date, official_tmax_c"
            " FROM calibration_station_daily_high_truth"
            " WHERE qc_status='ACCEPTED' AND local_date>=?"
            " AND official_tmax_c IS NOT NULL",
            (h5_from[:10],),
        )
    }
    if not truth:
        return {"from_utc": h5_from, "station_days": 0, "days": []}
    members: dict[tuple[str, str, str, str, str], list[float]] = (
        defaultdict(list)
    )
    for station, system, day, lead, cycle, raw in conn.execute(
        "SELECT station_id, ensemble_system_id, target_local_date,"
        " archive_lead_bucket, run_cycle, raw_tmax_c"
        " FROM calibration_native_ensemble_historical_member_run"
        " WHERE target_local_date>=? AND qc_status='ACCEPTED'"
        " AND raw_tmax_c IS NOT NULL",
        (h5_from[:10],),
    ):
        members[
            (str(station), str(system), str(day), str(lead), str(cycle))
        ].append(float(raw))

    per_station_day: dict[tuple[str, str], list[float]] = defaultdict(list)
    per_system: dict[str, list[float]] = defaultdict(list)
    cells = 0
    for (station, system, day, lead, cycle), values in members.items():
        settled = truth.get((station, day))
        if settled is None or len(values) < MIN_MEMBERS_FOR_BIAS_CELL:
            continue
        key = (station, system, lead, cycle)
        cutoff = f"{day}T00:00:00Z"
        if not _first_h5_at(key, cutoff):
            continue
        bias_h5 = _as_of(key, cutoff)
        bias_h15 = _as_of(key, cutoff, before=h5_from)
        if bias_h5 is None or bias_h15 is None:
            continue
        forecast = statistics.mean(values)
        delta = (
            abs(forecast - bias_h5 - settled)
            - abs(forecast - bias_h15 - settled)
        )
        per_station_day[(station, day)].append(delta)
        per_system[str(system)].append(delta)
        cells += 1
    if not per_station_day:
        return {"from_utc": h5_from, "station_days": 0, "days": []}

    station_day_means = {
        key: statistics.mean(values)
        for key, values in per_station_day.items()
    }
    better = sum(1 for value in station_day_means.values() if value < 0)
    worse = sum(1 for value in station_day_means.values() if value > 0)
    decided = better + worse
    # Clustered sign test: one vote per station-day, not per cell.
    z = (
        (better - decided / 2) / math.sqrt(decided / 4)
        if decided else 0.0
    )
    by_day: dict[str, list[float]] = defaultdict(list)
    for (_station, day), value in station_day_means.items():
        by_day[day].append(value)
    return {
        "from_utc": h5_from,
        "cells": cells,
        "station_days": len(station_day_means),
        "mean_delta_c": round(
            statistics.mean(station_day_means.values()), 4
        ),
        "median_delta_c": round(
            statistics.median(station_day_means.values()), 4
        ),
        "h5_better": better,
        "h5_worse": worse,
        "sign_z": round(z, 2),
        # The line the section draws its verdict on.  Two-sided 5% on a
        # clustered sign test; anything inside it is "not yet decided",
        # however large the per-cell sample looks.
        "significant": abs(z) >= 1.96,
        "days": [
            {
                "event_date": day,
                "stations": len(values),
                "mean_delta_c": round(statistics.mean(values), 4),
                "h5_better": sum(1 for value in values if value < 0),
            }
            for day, values in sorted(by_day.items())
        ],
        "systems": [
            {
                "system": system,
                "cells": len(values),
                "mean_delta_c": round(statistics.mean(values), 4),
                "h5_better": sum(1 for value in values if value < 0),
                "h5_worse": sum(1 for value in values if value > 0),
            }
            for system, values in sorted(per_system.items())
        ],
    }


def collect_h5_readiness(conn: sqlite3.Connection) -> dict[str, Any]:
    """Whether the bias half-life change can yet be scored on the book.

    The question the section answers is "has the fleet finished turning
    over onto H5", and it is asked of the DECISIONS rather than of the
    artifact pool.  Each P0 envelope records the artifact fingerprint that
    served each of its systems, so the answer is exact.  Two pool proxies
    were tried first and both lied by a wide margin -- artifacts carry a
    thirty-day validity window against a daily refit, so a scope holds
    about twenty-one superseded generations that no decision will ever
    read, and counting them put 2026-08-29 at 18% when the decisions say
    100%.  Provisional members are excluded, not counted against: they
    have no artifact of their own by design.
    """

    known = {
        str(row[0]) for row in conn.execute(
            "SELECT content_sha256"
            " FROM calibration_native_ensemble_system_artifact"
        )
    }
    fitted_under_h5 = {
        str(row[0]) for row in conn.execute(
            "SELECT content_sha256"
            " FROM calibration_native_ensemble_system_artifact"
            " WHERE valid_from_utc>=?",
            (H5_FROM_UTC,),
        )
    }
    days: list[dict[str, Any]] = []
    for (day,) in conn.execute(
        "SELECT DISTINCT event_date FROM basket_probability_envelope"
        " WHERE stage='P0' AND state='COMMITTED' AND event_date>=?"
        " ORDER BY event_date",
        (H5_FROM_UTC[:10],),
    ):
        decisions = 0
        under_h5 = 0
        provisional_only = 0
        for (lineage_json,) in conn.execute(
            "SELECT lineage_json FROM basket_probability_envelope"
            " WHERE stage='P0' AND state='COMMITTED' AND event_date=?",
            (str(day),),
        ):
            try:
                lineage = json.loads(lineage_json)
            except (TypeError, ValueError):
                continue
            raw = lineage.get("system_artifact_shas_json")
            if not raw:
                continue
            try:
                shas = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                continue
            if not isinstance(shas, dict) or not shas:
                continue
            calibrated = [v for v in shas.values() if v in known]
            if not calibrated:
                provisional_only += 1
                continue
            decisions += 1
            if all(v in fitted_under_h5 for v in calibrated):
                under_h5 += 1
        if not decisions:
            continue
        settled = conn.execute(
            "SELECT COUNT(*) FROM paper_trade_settlement WHERE event_date=?",
            (str(day),),
        ).fetchone()[0]
        days.append({
            "event_date": str(day),
            "decisions": decisions,
            "served_under_h5": under_h5,
            "share": round(under_h5 / decisions, 3),
            "provisional_only": provisional_only,
            "settled_chains": int(settled or 0),
        })
    scored = [
        item for item in days
        if item["share"] >= 0.9 and item["settled_chains"] > 0
    ]
    return {
        "from_utc": H5_FROM_UTC,
        "days": days,
        "scoreable_days": [item["event_date"] for item in scored],
    }


def collect_truth_landing(conn: sqlite3.Connection) -> dict[str, Any]:
    """Where truth settles, by market price and by model claim.

    Truth here is the market's own adjudication -- the bucket whose last
    recorded bid converged to 0.80 or better -- because three hand-built
    truth vocabularies in one evening each disagreed with the market's
    closing prices, and the market is the party that actually pays.
    Validated 60 of 62 against the settlement pipeline's settled_bucket the
    day it was built (the two dissents are ZGSZ truth-revision days).
    Station-days whose capture was truncated before convergence are
    excluded and counted, never guessed.

    Price is the entry the seat actually faced: the median best ask over
    the four hours after the station-day's first P0 decision, at least two
    quotes.  The claim is that same first P0 envelope's served probability.
    A day whose local calendar has not finished is excluded -- an unfinished
    day's "truth" is whatever the afternoon has not yet overwritten, which
    is how the first version of this table put 45 winners under a cent.
    """

    if not _table_exists(conn, "basket_official_ws_snapshot"):
        return {}
    now = datetime.now(timezone.utc)
    # 48 rows, not 148,000.  This map is station -> timezone and nothing
    # else, and it was being built by DISTINCT over every envelope ever
    # written: the predicate is on a NOT NULL column so it never excludes a
    # row, no index carries both columns in a shape the planner will pick,
    # and the chosen index does not cover settlement_timezone -- so it read
    # 147,971 rows off a 38 GB store to return 45.  Measured 2026-09-09:
    # 127.6 seconds, inside a render whose whole budget is 30 minutes.
    # ``calibration_station_market_timezone_binding`` is the authority for
    # exactly this question -- station_id is UNIQUE there and a CHECK holds
    # market_registry_timezone_count to 1 -- and on the day this changed its
    # 48 rows covered all 45 stations the envelopes name, with no timezone
    # disagreeing.  The fallback is deliberately NOT the old scan: a station
    # missing from the binding gets no zone, day_closed answers False, and
    # the day is excluded -- the same answer the old query gave for a
    # station whose envelopes carried no timezone.
    zones: dict[str, str] = {}
    if _table_exists(conn, "calibration_station_market_timezone_binding"):
        zones = {
            str(station): str(zone)
            for station, zone in conn.execute(
                "SELECT station_id, settlement_timezone"
                " FROM calibration_station_market_timezone_binding"
            )
        }

    def day_closed(station: str, event_date: str) -> bool:
        zone = zones.get(station)
        if not zone:
            return False
        try:
            end = datetime.fromisoformat(
                event_date + "T00:00:00"
            ).replace(tzinfo=ZoneInfo(zone)) + timedelta(days=1)
        except Exception:
            return False
        return now >= end.astimezone(timezone.utc)

    first_decision: dict[tuple[str, str], str] = {}
    claims: dict[tuple[str, str], dict[str, float]] = {}
    # One row per station-day, chosen in SQL, instead of 147,971 sorted in a
    # temp B-tree and all but 1,215 thrown away in Python.
    # ``MIN(decision_at_utc)`` with bare columns beside it is SQLite's
    # documented min/max behaviour: the bare columns come from the row the
    # minimum was found on, which is precisely the first decision.
    #
    # Worth 69.1s -> 47.5s measured 2026-09-09, and the modest size is the
    # point: the scan was never the cost.  Broken down, the covering index
    # scan is 2.2s and the 1,215 overflow-page reads of probabilities_json
    # are 45.3s -- so the 122:1 row ratio buys 31%, not an order of
    # magnitude, and an index here would have targeted the 2.2s.  What is
    # left is the irreducible cost of reading 1,215 claims.
    for station, event, probabilities_json, decided in conn.execute(
        "SELECT station_id, event_date, probabilities_json,"
        " MIN(decision_at_utc)"
        " FROM basket_probability_envelope"
        " WHERE event_date >= ? AND stage='P0'"
        " GROUP BY station_id, event_date",
        (TRUTH_LANDING_SINCE,),
    ):
        key = (str(station), str(event))
        # The GROUP BY already made the key unique, so the only test left is
        # whether the local day has finished.
        if not day_closed(*key):
            continue
        first_decision[key] = str(decided)
        try:
            claims[key] = {
                str(bucket): float(value)
                for bucket, value in json.loads(probabilities_json).items()
            }
        except (ValueError, TypeError):
            first_decision.pop(key, None)

    entry_asks: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    last_bid: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for station, event, observed, buckets_json in conn.execute(
        "SELECT station_id, event_date, observed_at, buckets_json"
        " FROM basket_official_ws_snapshot WHERE event_date >= ?"
        " ORDER BY observed_at",
        (TRUTH_LANDING_SINCE,),
    ):
        key = (str(station), str(event))
        decided = first_decision.get(key)
        if decided is None:
            continue
        try:
            rows = json.loads(buckets_json)
        except (ValueError, TypeError):
            continue
        if not isinstance(rows, list):
            continue
        in_entry = False
        if str(observed) >= decided:
            try:
                opened = datetime.fromisoformat(
                    decided.replace("Z", "+00:00")
                )
                seen = datetime.fromisoformat(
                    str(observed).replace("Z", "+00:00")
                )
                in_entry = (seen - opened).total_seconds() <= 4 * 3600
            except ValueError:
                in_entry = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            bucket = str(row.get("bucket"))
            levels = row.get("ask_levels") or []
            ask = levels[0].get("price") if levels else None
            bid = row.get("bid_price") or 0
            bid_levels = row.get("bid_levels") or []
            if not bid and bid_levels:
                bid = bid_levels[0].get("price") or 0
            if in_entry and ask and float(ask) > 0:
                entry_asks[(key[0], key[1], bucket)].append(float(ask))
            if bid and float(bid) > 0:
                last_bid[key][bucket] = float(bid)

    winners: dict[tuple[str, str], str] = {}
    for key, bids in last_bid.items():
        bucket, top = max(bids.items(), key=lambda item: item[1])
        if top >= 0.80:
            winners[key] = bucket

    def banded(
        bands: tuple[tuple[float, float], ...],
        rows: list[tuple[int, float]],
    ) -> list[dict[str, Any]]:
        cells: dict[tuple[float, float], list[float]] = {
            band: [0.0, 0.0, 0.0] for band in bands
        }
        for hit, value in rows:
            for low, high in bands:
                if low <= value < high:
                    cell = cells[(low, high)]
                    cell[0] += hit
                    cell[1] += 1
                    cell[2] += value
                    break
        total_truth = sum(cell[0] for cell in cells.values()) or 1.0
        return [
            {
                "band": [low, high],
                "truth_count": int(cell[0]),
                "bucket_count": int(cell[1]),
                "truth_share": cell[0] / total_truth,
                "hit_rate": (cell[0] / cell[1]) if cell[1] else None,
                "value_mean": (cell[2] / cell[1]) if cell[1] else None,
            }
            for (low, high), cell in cells.items()
        ]

    price_rows: list[tuple[int, float]] = []
    for (station, event, bucket), asks in entry_asks.items():
        key = (station, event)
        if key not in winners or len(asks) < 2:
            continue
        price_rows.append(
            (1 if winners[key] == bucket else 0, median(asks))
        )
    claim_rows: list[tuple[int, float]] = []
    for key, claimed in claims.items():
        if key not in winners:
            continue
        for bucket, value in claimed.items():
            claim_rows.append((1 if winners[key] == bucket else 0, value))

    def coarse(value: float) -> str:
        for low, high in TRUTH_LANDING_COARSE:
            if low <= value < high:
                return f"{low:g}-{high:g}"
        return ""

    cross: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for (station, event, bucket), asks in entry_asks.items():
        key = (station, event)
        if key not in winners or len(asks) < 2:
            continue
        claim = claims.get(key, {}).get(bucket)
        if claim is None:
            continue
        cell = cross[(coarse(claim), coarse(median(asks)))]
        cell[0] += 1 if winners[key] == bucket else 0
        cell[1] += 1

    return {
        "since": TRUTH_LANDING_SINCE,
        "closed_station_days": len(first_decision),
        "adjudicated_station_days": len(winners),
        "capture_truncated_days": len(first_decision) - len(winners),
        "by_price": banded(TRUTH_LANDING_PRICE_BANDS, price_rows),
        "by_claim": banded(TRUTH_LANDING_PRICE_BANDS, claim_rows),
        "coarse_labels": [f"{low:g}-{high:g}" for low, high in TRUTH_LANDING_COARSE],
        "cross": [
            {"claim_band": claim_band, "price_band": price_band,
             "truth_count": cell[0], "bucket_count": cell[1]}
            for (claim_band, price_band), cell in cross.items()
        ],
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
    # The cycle materializes the live edge itself now (2026-08-30), so the
    # sweep's clock alone no longer decides whether anything would retry a
    # missing day.  Freshness is whichever engine asked most recently, and
    # the alarm is graded on actual harm: red means settled station-days
    # with no truth row and nothing fresh retrying; a stale engine with a
    # complete book is a scheduling observation, not a data emergency --
    # that conflation cried wolf on every deploy day.
    worker_truth: dict[str, Any] = {}
    resident_started_at: str | None = None
    try:
        status_document = json.loads(STATUS_JSON.read_text())
        worker_truth = (
            status_document.get("production", {})
            .get("truth_materialization") or {}
        )
        resident_started_at = status_document.get("started_at") or None
    except (OSError, ValueError, AttributeError):
        worker_truth = {}
    cycle_age = _age_hours(worker_truth.get("checked_at_utc"))
    # The status document is rebuilt from nothing at every boot and the
    # production block lands in it only when the first cycle completes, so
    # for the ten-odd minutes after a deploy the cycle engine has no
    # receipt to show.  That is not an engine that stopped: it is one that
    # has not yet come round once.  Measured 2026-09-02 20:30: the board
    # rendered five minutes after a restart, read no receipt, and graded a
    # complete book amber on the sweep's clock alone -- the same false
    # alarm this section was rewritten on 2026-08-30 to stop crying.  A
    # just-started resident is fresh as of its start; if it never completes
    # a cycle, that clock ages into a real alarm on its own.
    boot_age = (
        _age_hours(resident_started_at) if cycle_age is None else None
    )
    fresh_age = min(
        (
            value for value in (age, cycle_age, boot_age)
            if value is not None
        ),
        default=None,
    )
    if cycle_age is not None:
        cycle_engine = f"周期增量 {cycle_age:.1f}h 前"
    elif boot_age is not None:
        cycle_engine = (
            f"周期增量：常驻 {boot_age * 60:.0f} 分钟前重启，首轮未到"
        )
    else:
        cycle_engine = "周期增量还没有回执"
    engines = "；".join((
        f"清扫 {age:.1f}h 前" if age is not None else "清扫从未跑过",
        cycle_engine,
    ))
    if outstanding and (fresh_age is None or fresh_age > 3):
        health, note = "red", (
            f"{outstanding} 个已结算站日缺真值，且两台重试引擎都停了"
            f"（{engines}）"
        )
    elif outstanding:
        health, note = "amber", (
            f"{outstanding} 个已结算站日还没有真值行（{engines}）"
        )
    elif fresh_age is None or fresh_age > 3:
        health, note = "amber", f"覆盖完整，但重试引擎陈旧（{engines}）"
    elif deferred:
        health, note = "green", f"{deferred} 个站日让给了常驻写入，下一轮再问"
    elif refused:
        # Naming the permanent share keeps this line from reading as a backlog
        # that is about to clear.  Nine of these predate their own AWC capture
        # and sit outside the endpoint's thirty-day retention: they are the
        # answer, not a queue.
        expired = sum(
            int(item.get("expired_count") or 0)
            for item in (sweep.get("refusals") or [])
            if isinstance(item, dict)
        )
        health, note = "green", (
            f"已结算的都已物化；{refused} 个站日被证据本身挡住"
            + (f"，其中 {expired} 个源头已过期、补不回来" if expired else "")
        )
    else:
        health, note = "green", "已结算的站日全部有真值行"
    return {
        "coverage": coverage,
        "sweep": sweep,
        "health": health,
        "note": note,
        "sweep_age_hours": age,
        "cycle_age_hours": cycle_age,
        "resident_boot_age_hours": boot_age,
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
        # Charter 62: only rows in the station-local vocabulary are the
        # serving path now.  Without this filter the legacy calendar rows
        # (which nothing queries but which stay valid for up to 30 days)
        # render as a second column per scope and pollute the seat counts.
        "  AND season_bin IN"
        "   ('PEAK','DESCENT','TROUGH','ASCENT','ALLYEAR')"
        "  AND valid_until_utc > strftime('%Y-%m-%dT%H:%M:%SZ','now')"
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


def collect_band_residual_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    """Post-correction residuals per stage, on the band table's own ruler.

    Same lane, same clock, same window as the knob table above it: the
    band store's own ``serving_lane_by_day`` -- each station-day's FIRST
    decision (the 2026-08-24 fit-clock ruling), full history, per stage --
    with exactly one difference, stated by the flag: the claims are kept
    as served, factors included, where the fit inverts them back to
    pre-band.  So the two tables are the same population read on either
    side of the correction: the knob row says what the raw claim realised
    and what factor that earned; this row says what remains after the
    factor.  An earlier version of this audit read the training lane's
    OOS predictions (different population, no stage, lead vocabulary) and
    then a 3-hourly anchored sample (different clock and window); each
    mismatch surfaced as a fake contradiction on the page.
    """

    from bayes_basket_target.band_reliability_store import (
        BAND_TABLE_CELLS,
        serving_lane_by_station_day,
    )
    from bayes_basket_target.center_width_store import _truth

    truth = _truth(conn)
    labels = [
        "\u226550%" if low >= 0.5 else f"{low * 100:g}-{high * 100:g}%"
        for low, high in BAND_TABLE_CELLS
    ]
    stages_out = []
    # Per station, pooled across the three stages: claimed centre+-1 mass
    # against whether truth landed there, the promise-conservation ruler.
    # This is the court for "does band need station structure": the band
    # tables are deliberately fleet-pooled, so a station whose ratio sits
    # significantly off 1 after them is upstream evidence, not band's.
    station_cells: dict[str, list[float]] = {}
    for stage in ("P0", "P0-M", "P1"):
        try:
            by_station_day = serving_lane_by_station_day(
                conn, truth, stage=stage, pre_band=False,
            )
        except Exception:  # noqa: BLE001 -- an audit must not blank the page
            continue
        cells = {index: [0, 0.0, 0] for index in range(len(BAND_TABLE_CELLS))}
        days = set()
        for (station, event), day_rows in by_station_day.items():
            days.add(event)
            for entries in day_rows:
                for claim, hit, _lower in entries:
                    for index, (low, high) in enumerate(BAND_TABLE_CELLS):
                        if low <= claim < high:
                            slot = cells[index]
                            slot[0] += 1
                            slot[1] += float(claim)
                            slot[2] += int(hit)
                            break
                if entries:
                    centre = max(
                        range(len(entries)), key=lambda i: entries[i][0]
                    )
                    window = entries[max(0, centre - 1):centre + 2]
                    slot = station_cells.setdefault(station, [0, 0.0, 0])
                    slot[0] += 1
                    slot[1] += sum(claim for claim, _hit, _low in window)
                    slot[2] += 1 if any(
                        hit for _claim, hit, _low in window
                    ) else 0
        rows = []
        for index, label in enumerate(labels):
            n, promised, hits = cells[index]
            if not n:
                continue
            rows.append({
                "bin": label,
                "n": n,
                "promised": promised / n,
                "happened": hits / n,
            })
        stages_out.append({
            "stage": stage,
            "since": None,
            "day_count": len(days),
            "rows": rows,
        })
    stations = []
    for station, (n, promised, hits) in sorted(station_cells.items()):
        if not n or promised <= 0:
            continue
        claimed = promised / n
        realized = hits / n
        # Binomial 2-sigma on the realized rate, read in ratio units; a
        # stratum whose |ratio-1| clears it is marked, not gated.
        sigma = (
            (realized * (1.0 - realized) / n) ** 0.5 / claimed
            if 0.0 < realized < 1.0 else 1.0 / (n * claimed)
        )
        ratio = realized / claimed
        stations.append({
            "station": station,
            "station_label": STATION_LABELS.get(station, ""),
            "n": n,
            "claimed": claimed,
            "realized": realized,
            "ratio": ratio,
            "significant": abs(ratio - 1.0) > 2.0 * sigma,
        })
    stations.sort(key=lambda row: row["ratio"])
    return {"labels": labels, "stages": stages_out, "stations": stations}


def collect_band_shadow(conn: sqlite3.Connection) -> dict[str, Any]:
    """The shadow band layer's standing: which dimension earns its keep.

    The served table (section six) is pooled -- one factor per stage per
    claim band, the same for forty-five stations and every bucket in a
    ladder.  The shadow fits that same evidence with two extra dimensions
    and serves none of it; this panel is how the accumulation is read.

    The number that matters is per *level*, never the total.  A fit that
    promotes five fleet-wide position cells and lets two hundred fine
    cells inherit them reads as "209 qualified" on a total, which is the
    one reading that would be false.
    """

    if not _table_exists(conn, "calibration_band_shadow_journal"):
        return {}
    from bayes_basket_target.band_shadow_store import (
        BACKOFF_LEVELS,
        MIN_CELL_CLAIMED_MASS,
        MIN_CELL_DAYS,
        MIN_CELL_ROWS,
        FDR_Q,
    )

    LEVEL_LABEL = {
        "station_position": "站 × 桶位置",
        "station": "站",
        "position": "桶位置",
    }
    GROUP_LABEL = {
        "cool_far": "偏冷远端",
        "cool_near": "偏冷邻桶",
        "centre": "峰值桶",
        "warm_near": "偏暖邻桶",
        "warm_far": "偏暖远端",
    }
    BLOCK_LABEL = {
        "gate_a_insufficient_mass": "样本不够（一门）",
        "gate_b_halves_not_stable": "前后半段不稳（二门）",
        "gate_b_fdr_not_significant": "多重校正后不显著（二门）",
        "gate_c_forward_window_not_improved": "试算反而变差（三门）",
    }
    stages: list[dict[str, Any]] = []
    for stage in ("P0", "P0-M", "P1"):
        rows = conn.execute(
            "SELECT fitted_at_utc, shadow_sha256, serving_day_count,"
            " serving_station_day_count, cell_count, promoted_cell_count,"
            " forward_nll_gain, evidence_json"
            " FROM calibration_band_shadow_journal"
            " WHERE stage=? ORDER BY fitted_at_utc DESC LIMIT 8",
            (stage,),
        ).fetchall()
        if not rows:
            continue
        latest = rows[0]
        try:
            evidence = json.loads(latest[7])
        except (TypeError, ValueError):
            evidence = {}
        levels = evidence.get("levels") or {}
        level_rows = []
        for level in BACKOFF_LEVELS[:-1]:
            report = levels.get(level)
            if not report:
                continue
            blocked = report.get("blocked") or {}
            level_rows.append({
                "level": level,
                "label": LEVEL_LABEL.get(level, level),
                "tested": report.get("tested", 0),
                "eligible": report.get("eligible", 0),
                "promoted": report.get("promoted", 0),
                "blocked": [
                    {"reason": BLOCK_LABEL.get(key, key), "n": value}
                    for key, value in sorted(
                        blocked.items(), key=lambda kv: -kv[1]
                    )
                ],
            })
        promoted = []
        for key, factor in sorted((evidence.get("promoted") or {}).items()):
            try:
                parsed = ast.literal_eval(key)
            except (ValueError, SyntaxError):
                continue
            station, group, band = parsed
            promoted.append({
                "station": None if station == "*" else station,
                "group": group,
                "group_label": GROUP_LABEL.get(group, group),
                "band": [float(band[0]), float(band[1])],
                "factor": float(factor),
            })
        promoted.sort(key=lambda row: (row["band"][0], row["group"]))
        stages.append({
            "stage": stage,
            "meaning": STAGE_MEANING.get(stage, ""),
            "fitted_at": latest[0],
            "fit_age_hours": _age_hours(latest[0]),
            "sha": str(latest[1])[:12],
            "serving_days": int(latest[2]),
            "station_days": int(latest[3]),
            "cell_count": int(latest[4]),
            "inherited_cell_count": int(latest[5]),
            "forward_nll_gain": float(latest[6]),
            "levels": level_rows,
            "promoted": promoted,
            "journal": [
                {
                    "at": row[0],
                    "sha": str(row[1])[:12],
                    "days": int(row[2]),
                    "promoted": int(row[5]),
                    "gain": float(row[6]),
                }
                for row in rows
            ],
        })
    if not stages:
        return {}
    return {
        "stages": stages,
        "gates": {
            "min_claimed_mass": MIN_CELL_CLAIMED_MASS,
            "min_days": MIN_CELL_DAYS,
            "min_rows": MIN_CELL_ROWS,
            "fdr_q": FDR_Q,
        },
        "distance": collect_band_shadow_distance(conn),
    }


def collect_band_shadow_distance(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """How far the starved levels are from the mass floor, in days.

    A level that promotes nothing reads the same on day one and on the day
    before it qualifies.  The fattest cell's mass against the floor, at
    the rate it has actually been accumulating, is the difference between
    "this is going nowhere" and "this needs three more weeks" -- and the
    ETA is the only actionable number on a panel of zeroes.
    """

    if not _table_exists(conn, "calibration_band_shadow_cell"):
        return []
    from bayes_basket_target.band_shadow_store import MIN_CELL_CLAIMED_MASS

    out: list[dict[str, Any]] = []
    for stage in ("P0", "P0-M", "P1"):
        sha_row = conn.execute(
            "SELECT shadow_sha256, serving_day_count"
            " FROM calibration_band_shadow_journal"
            " WHERE stage=? ORDER BY fitted_at_utc DESC LIMIT 1",
            (stage,),
        ).fetchone()
        if sha_row is None:
            continue
        sha, days = str(sha_row[0]), int(sha_row[1] or 0)
        if days <= 0:
            continue
        # The stored cells are the finest level; the coarser levels are
        # their sums, so both distances come from one scan.
        rows = conn.execute(
            "SELECT station_id, position_group, band_lower, claimed_mass"
            " FROM calibration_band_shadow_cell"
            " WHERE shadow_sha256=? AND stage=?",
            (sha, stage),
        ).fetchall()
        if not rows:
            continue
        fine: dict[tuple[str, str, float], float] = {}
        by_station: dict[tuple[str, float], float] = {}
        for station, group, lower, mass in rows:
            fine[(station, group, float(lower))] = float(mass)
            key = (station, float(lower))
            by_station[key] = by_station.get(key, 0.0) + float(mass)
        for level, masses in (
            ("station", by_station.values()),
            ("station_position", fine.values()),
        ):
            values = sorted(masses, reverse=True)
            if not values:
                continue
            best = values[0]
            reached = sum(1 for m in values if m >= MIN_CELL_CLAIMED_MASS)
            rate = best / days
            out.append({
                "stage": stage,
                "level": level,
                "cells": len(values),
                "best_mass": best,
                "floor": MIN_CELL_CLAIMED_MASS,
                "reached": reached,
                "days_now": days,
                "days_needed": (
                    None if rate <= 0
                    else max(0.0, MIN_CELL_CLAIMED_MASS / rate)
                ),
            })
    return out


def _live_execution_mode() -> str:
    """The mode the production lane is actually compiled for."""

    try:
        from bayes_basket_target.production import (  # noqa: PLC0415
            PRODUCTION_EXECUTION_MODE,
        )
        return str(PRODUCTION_EXECUTION_MODE)
    except Exception:
        return "unknown"


def _read_open_orders() -> dict[str, Any]:
    """What the exchange says is resting, right now.

    The gateway is the only thing allowed to ask, and it is a local
    read-only call; a failure is reported rather than raised, because a
    board that cannot reach the gateway should say so, not vanish.
    """

    import datetime as _dt  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    port = os.environ.get("PM_CLOB_ORDER_SERVICE_PORT", "8765")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/lookup-open-orders",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}"}
    orders = raw.get("open_orders") or []
    return {
        "available": bool(raw.get("ok")),
        "count": int(raw.get("total_open_orders") or 0),
        # The board is a static render; this row is a reading taken once, at
        # render time.  Without the stamp a stale page asserts a live zero.
        "asked_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
        "orders": [
            {
                "id": str(o.get("id") or ""),
                "side": str(o.get("side") or ""),
                "price": o.get("price"),
                "original_size": o.get("original_size"),
                "size_matched": o.get("size_matched"),
                "outcome": str(o.get("outcome") or ""),
                "created_at": o.get("created_at"),
            }
            for o in orders[:20]
        ],
    }


def _read_chain_probe() -> dict[str, Any]:
    """The recorded live-order round trip, if one has been run."""

    path = ROOT / "data" / "chain_probe_receipt.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


# Charter 75 reads the plan's direction straight off the skew sign, so a
# plan minted before the shape was recorded still says which way the day
# leaned -- with one caveat the label carries: evidence that was not fresh
# reads NEUTRAL whatever the distribution looked like.
_SKEW_BY_DIRECTION = {
    "UP": "RIGHT_SKEWED", "DOWN": "LEFT_SKEWED", "NEUTRAL": "SYMMETRIC",
}


def _window_seconds(created_at: Any, deadline_at: Any) -> float | None:
    """Seconds a basket had to reach the exchange, or None if unreadable.

    Reported rather than assumed: the deadline is a fixed offset today, but
    a panel that prints the constant instead of the row's own two timestamps
    would keep printing it after the rule changed.  Unparseable reads as
    unknown -- a missing window is not a generous one.
    """

    try:
        start = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(deadline_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return round((end - start).total_seconds(), 3)


def _live_basket_plan_state(
    conn: sqlite3.Connection, rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """What each live basket's own plan recorded about the day, by decision.

    Two readings, both read back rather than derived.  ``center_bucket`` is
    the rung the optimiser seated the ladder on (charter 75: the mean
    landing bucket, moved by the skew rule, which is why it is not simply
    the middle of the window).  ``skew_reading`` is the shape that placed it
    -- symmetric, right-skewed, left-skewed -- with the mean-minus-mode gap
    in bucket steps and the threshold it was judged against.

    Deriving either here would mean re-implementing the per-station grid and
    skew rules the plan already applied, and the ledger's seat row is not a
    stand-in: measured across the whole live order book on 2026-09-06, 15 of
    53 baskets carry a ledger seat decided at another moment, and 6 of those
    name a centre that is not even one of the basket's legs.

    ``skew_reading`` exists only on plans compiled from 2026-09-06.  Older
    plans fall back to the plan's own ``direction``, which is the same skew
    sign under charter 75 with one difference worth naming: a day whose
    evidence was not fresh reads NEUTRAL whatever its shape.  So the two are
    labelled apart -- the fallback says it is the lean the plan acted on,
    not the shape it read.
    """

    ids = sorted({
        str(row["decision_id"]) for row in rows if row.get("decision_id")
    })
    if not ids or not _table_exists(conn, "basket_p0_preselection_plan"):
        return {}
    # decision -> the plan it was compiled from, and the scope to look in.
    wanted: dict[tuple[str, str], dict[str, list[str]]] = {}
    try:
        query = conn.execute(
            "SELECT decision_id, station_id, event_date, details_json"
            " FROM auto_trade_decisions WHERE decision_id IN (%s)"
            % ",".join("?" * len(ids)),
            ids,
        )
    except sqlite3.Error:
        return {}
    for decision_id, station, day, details in query:
        try:
            plan_sha = str(
                (json.loads(details or "{}") or {}).get(
                    "production_plan_sha256"
                ) or ""
            )
        except (TypeError, ValueError):
            plan_sha = ""
        if not plan_sha:
            continue
        scope = wanted.setdefault((str(station), str(day)), {})
        scope.setdefault(plan_sha, []).append(str(decision_id))
    state: dict[str, dict[str, Any]] = {}
    for (station, day), by_plan in wanted.items():
        try:
            plans = conn.execute(
                "SELECT"
                " json_extract(plan_json,"
                "'$.compiled_plan.production_plan_sha256'),"
                " json_extract(plan_json,'$.compiled_plan.plan.center_bucket'),"
                " json_extract(plan_json,'$.compiled_plan.plan.direction'),"
                " json_extract(plan_json,"
                "'$.compiled_plan.plan.skew_reading.state'),"
                " json_extract(plan_json,"
                "'$.compiled_plan.plan.skew_reading.gap_buckets'),"
                " json_extract(plan_json,"
                "'$.compiled_plan.plan.skew_reading.threshold_buckets'),"
                " json_extract(plan_json,"
                "'$.compiled_plan.plan.skew_reading.sigma_buckets'),"
                " json_extract(plan_json,'$.compiled_plan.plan"
                ".basket_support_selection.symmetric_seat_width_in_band'),"
                " json_extract(plan_json,'$.compiled_plan.plan"
                ".basket_support_selection.symmetric_seat_forced'),"
                " json_extract(plan_json,'$.compiled_plan.plan"
                ".basket_support_selection.symmetric_seat_width_band')"
                " FROM basket_p0_preselection_plan"
                " WHERE station_id=? AND event_date=?",
                (station, day),
            ).fetchall()
        except sqlite3.Error:
            continue
        for (plan_sha, center, direction, skew, gap, threshold, sigma,
             in_band, forced, band_json) in plans:
            targets = by_plan.get(str(plan_sha or ""), ())
            if not targets:
                continue
            recorded = str(skew or "")
            reading = {
                "center": str(center or ""),
                # SYMMETRIC / RIGHT_SKEWED / LEFT_SKEWED, or the direction
                # the plan acted on when the shape was not written down.
                "skew": recorded or _SKEW_BY_DIRECTION.get(
                    str(direction or "").upper(), ""
                ),
                "skew_source": "reading" if recorded else (
                    "direction" if direction else ""
                ),
                "gap_buckets": gap,
                "threshold_buckets": threshold,
                # Charter 122: the served width the shape was read at, in
                # the same bucket steps.  Written from 2026-09-08; older
                # plans carry no width and the chip says nothing about it.
                "sigma_buckets": sigma,
                # Charter 122: whether the day's width let the symmetric
                # seat rule fire, and whether it actually did.  A reader
                # seeing only "SYMMETRIC" cannot tell a seat the rule fixed
                # from one the enumeration chose, and since 2026-09-09 most
                # symmetric days are the second kind.
                "seat_width_in_band": in_band,
                "seat_forced": forced,
                "seat_width_band": band_json,
            }
            for decision_id in targets:
                state[decision_id] = reading
    return state


def _collect_live_orders(conn: sqlite3.Connection) -> dict[str, Any]:
    """The order book: how many, worth how much, and what came back.

    Two books, never mixed.  ``live_submit_authorized=1`` is the only
    thing that makes a row live -- the column exists on rows minted after
    the go-live migration widened the CHECK, and the 1406 rows that
    predate it are all 0.  The top-level counts are the live book, so the
    card's headline cannot quietly count paper intents; ``book`` is the
    whole intent table with the split named on every row, because an
    intent lane that is minting every cycle and never leaving the gateway
    is the state this system is actually in and it was invisible here.

    ROI and PnL come off the reconciliation table -- what the exchange
    said it filled -- and never off the paper ledger.  The settled bucket
    label is read from the settlement rows because that label is derived
    from truth by the production settler; re-deriving it here would mean
    re-implementing the per-station grid rule (°C and 2°F stations do not
    share one), which is how audits invent their own findings.  Money is
    entirely the live fill columns: cost is what was paid, payout is one
    unit per share on the bucket truth landed in.
    """

    out: dict[str, Any] = {
        "intent_count": 0, "submitted_count": 0, "accepted_count": 0,
        "notional_usdc": 0.0, "matched_notional_usdc": 0.0, "rows": [],
        "book": {}, "exchange": {}, "pnl": {}, "plan_state": {},
    }
    if not _table_exists(conn, "basket_target_order_intents"):
        return out
    try:
        rows = conn.execute(
            "SELECT i.order_intent_id, i.decision_id, i.bucket, i.side,"
            " i.limit_price, i.requested_size, i.requested_notional_usdc,"
            " i.state, i.created_at, d.station_id, d.event_date,"
            " i.target_execution_hard_deadline_at"
            " FROM basket_target_order_intents i"
            " LEFT JOIN auto_trade_decisions d ON d.decision_id=i.decision_id"
            " WHERE i.live_submit_authorized=1"
            " ORDER BY i.created_at DESC LIMIT 200"
        ).fetchall()
    except sqlite3.Error:
        return out
    out["intent_count"] = len(rows)
    # Why a leg ended the way it did, from its latest event: the venue's
    # refusal text, the gateway's, or the submission gate's (held, barred,
    # disarmed).  A state alone -- failed, cancelled -- was the whole story
    # the board told on 2026-09-05, and it was not enough to act on.
    latest_event: dict[str, tuple[str, str]] = {}
    if _table_exists(conn, "basket_target_order_events"):
        try:
            for intent_id, event_type, event_json in conn.execute(
                "SELECT e.order_intent_id, e.event_type, e.event_json"
                " FROM basket_target_order_events e"
                " JOIN basket_target_order_intents i"
                "   ON i.order_intent_id=e.order_intent_id"
                " WHERE i.live_submit_authorized=1"
                " ORDER BY e.observed_at ASC, e.event_id ASC"
            ):
                try:
                    reason = str(
                        (json.loads(str(event_json or "{}")) or {}).get("reason")
                        or ""
                    )
                except (TypeError, ValueError):
                    reason = ""
                latest_event[str(intent_id)] = (str(event_type), reason)
        except sqlite3.Error:
            latest_event = {}
    # Which station-days have settled, and on which bucket -- the same
    # settlement rows the PnL reads, so a date the board collapses is a
    # date whose money is already counted.
    settled_bucket: dict[tuple[str, str], str] = {}
    if _table_exists(conn, "paper_trade_settlement"):
        try:
            for station, day, bucket in conn.execute(
                "SELECT station_id, event_date, MAX(settled_bucket)"
                " FROM paper_trade_settlement WHERE settled_bucket IS NOT NULL"
                " GROUP BY station_id, event_date"
            ):
                settled_bucket[(str(station), str(day))] = str(bucket)
        except sqlite3.Error:
            settled_bucket = {}
    for row in rows:
        out["notional_usdc"] += float(row[6] or 0.0)
        station = str(row[9] or "")
        day = str(row[10] or "")
        event_type, reason = latest_event.get(str(row[0]), ("", ""))
        out["rows"].append({
            "order_intent_id": str(row[0]), "decision_id": str(row[1]),
            "bucket": str(row[2]), "side": str(row[3]),
            "limit_price": row[4], "requested_size": row[5],
            "requested_notional_usdc": row[6], "state": str(row[7]),
            "created_at": str(row[8]),
            # A leg is not a basket.  The decision is what the solver sized
            # as one joint action, so the panel has to be able to put the
            # legs of one basket back together -- read as five unrelated
            # single-bucket orders, this table says the system buys buckets
            # one at a time, which is the opposite of what it does.
            "station_id": station,
            "event_date": day,
            "execution_deadline_at": str(row[11] or ""),
            "execution_window_seconds": _window_seconds(row[8], row[11]),
            "event_type": event_type,
            "reason": reason,
            "settled_bucket": settled_bucket.get((station, day), ""),
        })
    # What each basket's own plan recorded -- the centre rung and the day's
    # shape.  Keyed by decision, because the decision is the basket: a
    # station-day carries several and they share neither.
    out["plan_state"] = _live_basket_plan_state(conn, out["rows"])
    if _table_exists(conn, "basket_target_order_events"):
        try:
            for kind, count, matched in conn.execute(
                "SELECT e.event_type, COUNT(*),"
                " COALESCE(SUM(e.matched_notional_usdc),0)"
                " FROM basket_target_order_events e"
                " JOIN basket_target_order_intents i"
                "   ON i.order_intent_id=e.order_intent_id"
                " WHERE i.live_submit_authorized=1 GROUP BY e.event_type"
            ):
                kind_l = str(kind).lower()
                if "accept" in kind_l or "submit" in kind_l:
                    out["submitted_count"] += int(count)
                if "accept" in kind_l:
                    out["accepted_count"] += int(count)
                out["matched_notional_usdc"] += float(matched or 0.0)
        except sqlite3.Error:
            pass
    out["notional_usdc"] = round(out["notional_usdc"], 4)
    out["matched_notional_usdc"] = round(out["matched_notional_usdc"], 4)
    out["book"] = _collect_order_book(conn)
    out["exchange"] = _collect_order_exchange(conn)
    out["pnl"] = _collect_live_pnl(conn)
    return out


def _collect_order_book(conn: sqlite3.Connection) -> dict[str, Any]:
    """The live intent book, cut the three ways a reader asks for.

    Live only (user ruling, 2026-08-31): every query here is filtered to
    ``live_submit_authorized=1``.  The paper intents are a different book
    and counting them beside live ones in the same table is how the two
    get added up by eye.  Their count is carried separately, named as
    excluded, for the one sentence that explains an empty panel -- a
    reader who sees zero live intents still needs to know whether the
    lane is minting anything at all.
    """

    book: dict[str, Any] = {}
    joined = (
        " FROM basket_target_order_intents i"
        " LEFT JOIN auto_trade_decisions d ON d.decision_id=i.decision_id"
        " WHERE i.live_submit_authorized=1"
    )
    try:
        book["paper_excluded"] = int(conn.execute(
            "SELECT COUNT(*) FROM basket_target_order_intents"
            " WHERE live_submit_authorized=0"
        ).fetchone()[0])
    except sqlite3.Error:
        book["paper_excluded"] = 0
    try:
        row = conn.execute(
            "SELECT COUNT(*),"
            " COALESCE(SUM(i.requested_notional_usdc),0),"
            " AVG(i.requested_notional_usdc), AVG(i.limit_price),"
            " COUNT(DISTINCT d.station_id), COUNT(DISTINCT i.decision_id),"
            " COUNT(DISTINCT i.cycle_id),"
            " MIN(d.event_date), MAX(d.event_date), MAX(i.created_at)"
            + joined
        ).fetchone()
    except sqlite3.Error:
        return book
    if not row or not row[0]:
        book["total"] = 0
        return book
    book.update({
        "total": int(row[0]),
        "notional_usdc": round(float(row[1] or 0.0), 2),
        "mean_notional_usdc": round(float(row[2] or 0.0), 3),
        "mean_limit_price": round(float(row[3] or 0.0), 4),
        "stations": int(row[4] or 0), "decisions": int(row[5] or 0),
        "cycles": int(row[6] or 0),
        "first_event_date": str(row[7] or ""),
        "last_event_date": str(row[8] or ""),
        "last_minted_at": str(row[9] or ""),
    })
    try:
        book["by_state"] = [
            {"state": str(state), "count": int(count),
             "notional_usdc": round(float(notional or 0.0), 2)}
            for state, count, notional in conn.execute(
                "SELECT i.state, COUNT(*),"
                " COALESCE(SUM(i.requested_notional_usdc),0)"
                " FROM basket_target_order_intents i"
                " WHERE i.live_submit_authorized=1"
                " GROUP BY i.state ORDER BY COUNT(*) DESC"
            )
        ]
    except sqlite3.Error:
        book["by_state"] = []
    try:
        book["by_day"] = [
            {"event_date": str(day), "count": int(count),
             "stations": int(stations or 0),
             "notional_usdc": round(float(notional or 0.0), 2)}
            for day, count, stations, notional in conn.execute(
                "SELECT d.event_date, COUNT(*),"
                " COUNT(DISTINCT d.station_id),"
                " COALESCE(SUM(i.requested_notional_usdc),0)"
                + joined +
                " AND d.event_date IS NOT NULL"
                " GROUP BY d.event_date ORDER BY d.event_date DESC LIMIT 14"
            )
        ]
    except sqlite3.Error:
        book["by_day"] = []
    try:
        book["by_station"] = [
            {"station_id": str(station), "count": int(count),
             "days": int(days or 0),
             "mean_limit_price": round(float(price or 0.0), 4),
             "notional_usdc": round(float(notional or 0.0), 2)}
            for station, count, days, price, notional in conn.execute(
                "SELECT d.station_id, COUNT(*),"
                " COUNT(DISTINCT d.event_date), AVG(i.limit_price),"
                " COALESCE(SUM(i.requested_notional_usdc),0)"
                + joined +
                " AND d.station_id IS NOT NULL"
                " GROUP BY d.station_id"
                " ORDER BY SUM(i.requested_notional_usdc) DESC LIMIT 12"
            )
        ]
    except sqlite3.Error:
        book["by_station"] = []
    # Keyed by station and day so the three ledger sections -- daily stats,
    # the cross-day per-station roll-up and the per-station-day detail --
    # can each show what the live lane did on their own row.  Those three
    # are built entirely off ``paper_trade_*``, and a reader who only looks
    # at them has no way to tell a station that traded live from one that
    # only traded on paper.  Carried as a separate map, never folded into
    # the paper aggregates: the two books do not add up, and a live column
    # sitting beside paper columns is the only way to show both without
    # inviting the eye to sum them.
    try:
        book["by_station_day"] = {
            f"{station}|{day}": {
                "count": int(count),
                "legs_reached_exchange": int(reached or 0),
                "notional_usdc": round(float(notional or 0.0), 4),
                "baskets": int(baskets or 0),
                "states": str(states or ""),
            }
            for station, day, count, baskets, notional, reached, states in
            conn.execute(
                "SELECT d.station_id, d.event_date, COUNT(*),"
                " COUNT(DISTINCT i.decision_id),"
                " COALESCE(SUM(i.requested_notional_usdc),0),"
                " COALESCE(SUM(CASE WHEN EXISTS("
                "   SELECT 1 FROM basket_target_order_events e"
                "   WHERE e.order_intent_id=i.order_intent_id"
                " ) THEN 1 ELSE 0 END),0),"
                " GROUP_CONCAT(DISTINCT i.state)"
                + joined +
                " AND d.station_id IS NOT NULL AND d.event_date IS NOT NULL"
                " GROUP BY d.station_id, d.event_date"
            )
        }
    except sqlite3.Error:
        book["by_station_day"] = {}
    return book


def _collect_order_exchange(conn: sqlite3.Connection) -> dict[str, Any]:
    """What came back from the gateway, on the live book only."""

    live_join = (
        " JOIN basket_target_order_intents i"
        "   ON i.order_intent_id=t.order_intent_id"
        " WHERE i.live_submit_authorized=1"
    )
    ex: dict[str, Any] = {
        "events": 0, "event_types": [], "reconciliations": 0,
        "transitions": 0, "filled_size": 0.0, "filled_notional_usdc": 0.0,
        "by_state": [], "blocking": [],
    }
    if _table_exists(conn, "basket_target_order_events"):
        try:
            ex["event_types"] = [
                {"event_type": str(kind), "count": int(count)}
                for kind, count in conn.execute(
                    "SELECT t.event_type, COUNT(*)"
                    " FROM basket_target_order_events t" + live_join +
                    " GROUP BY t.event_type ORDER BY COUNT(*) DESC"
                )
            ]
        except sqlite3.Error:
            ex["event_types"] = []
        ex["events"] = sum(item["count"] for item in ex["event_types"])
    if _table_exists(conn, "basket_target_order_reconciliations"):
        try:
            ex["by_state"] = [
                {"state": str(state), "count": int(count),
                 "filled_size": round(float(size or 0.0), 4),
                 "filled_notional_usdc": round(float(notional or 0.0), 4)}
                for state, count, size, notional in conn.execute(
                    "SELECT t.state, COUNT(*),"
                    " COALESCE(SUM(t.filled_size),0),"
                    " COALESCE(SUM(t.filled_notional_usdc),0)"
                    " FROM basket_target_order_reconciliations t" + live_join +
                    " GROUP BY t.state ORDER BY COUNT(*) DESC"
                )
            ]
            ex["blocking"] = [
                {"reason": str(reason), "count": int(count)}
                for reason, count in conn.execute(
                    # Only reconciliations that are still open.  The
                    # blocking_reason column keeps its last value after the
                    # question is settled, so counting every non-null one
                    # reported a resolved verdict as an outstanding blocker:
                    # order_never_reached_exchange is the answer, not the
                    # obstacle, and the board would have shown it as
                    # "对账阻断 x1" indefinitely over an order proven never
                    # to have been placed.
                    "SELECT t.blocking_reason, COUNT(*)"
                    " FROM basket_target_order_reconciliations t" + live_join +
                    "   AND t.blocking_reason IS NOT NULL"
                    "   AND t.state NOT IN ("
                    "     'resolved_filled','resolved_cancelled',"
                    "     'resolved_partial','resolved_not_placed')"
                    " GROUP BY t.blocking_reason ORDER BY COUNT(*) DESC LIMIT 8"
                )
            ]
        except sqlite3.Error:
            ex["by_state"] = []
        ex["reconciliations"] = sum(item["count"] for item in ex["by_state"])
        ex["filled_size"] = round(
            sum(item["filled_size"] for item in ex["by_state"]), 4)
        ex["filled_notional_usdc"] = round(
            sum(item["filled_notional_usdc"] for item in ex["by_state"]), 4)
    if _table_exists(conn, "basket_target_execution_transition_versions"):
        try:
            ex["transitions"] = int(conn.execute(
                "SELECT COUNT(*)"
                " FROM basket_target_execution_transition_versions t"
                + live_join
            ).fetchone()[0])
        except sqlite3.Error:
            pass
    return ex


def _collect_live_pnl(conn: sqlite3.Connection) -> dict[str, Any]:
    """ROI and PnL of the live book, off live fills and settled truth.

    One row per filled live intent: cost is the notional the exchange
    reported filling, payout is one USDC per share when truth landed in
    that intent's bucket and zero otherwise.  A filled leg whose
    station-day has no settled bucket yet is counted as pending, never
    scored as a loss -- an unsettled day that reads as a zero payout is
    how a live book would look worse than it is every afternoon.
    """

    pnl: dict[str, Any] = {
        "available": False, "reason": "还没有实盘成交",
        "filled_legs": 0, "won_legs": 0, "pending_legs": 0,
        "cost_usdc": 0.0, "payout_usdc": 0.0, "pnl_usdc": 0.0,
        "roi": None, "station_days": 0, "by_station": [],
    }
    needed = ("basket_target_order_reconciliations",
              "basket_target_order_intents", "auto_trade_decisions")
    if not all(_table_exists(conn, name) for name in needed):
        pnl["reason"] = "执行账本表还没建"
        return pnl
    try:
        fills = conn.execute(
            "SELECT d.station_id, d.event_date, i.bucket,"
            " r.filled_size, r.filled_notional_usdc"
            " FROM basket_target_order_reconciliations r"
            " JOIN basket_target_order_intents i"
            "   ON i.order_intent_id=r.order_intent_id"
            " JOIN auto_trade_decisions d ON d.decision_id=i.decision_id"
            " WHERE i.live_submit_authorized=1 AND r.filled_size>0"
        ).fetchall()
    except sqlite3.Error:
        pnl["reason"] = "执行账本读不出来"
        return pnl
    if not fills:
        pnl["reason"] = "还没有一笔实盘成交"
        return pnl
    settled: dict[tuple[str, str], str] = {}
    if _table_exists(conn, "paper_trade_settlement"):
        for station, day, bucket in conn.execute(
            "SELECT station_id, event_date, MAX(settled_bucket)"
            " FROM paper_trade_settlement WHERE settled_bucket IS NOT NULL"
            " GROUP BY station_id, event_date"
        ):
            settled[(str(station), str(day))] = str(bucket)
    per_station: dict[str, dict[str, float]] = {}
    days: set[tuple[str, str]] = set()
    for station, day, bucket, size, notional in fills:
        key = (str(station), str(day))
        truth = settled.get(key)
        cost = float(notional or 0.0)
        acc = per_station.setdefault(str(station), {
            "legs": 0.0, "won": 0.0, "pending": 0.0,
            "cost": 0.0, "payout": 0.0,
        })
        acc["legs"] += 1
        if truth is None:
            acc["pending"] += 1
            pnl["pending_legs"] += 1
            continue
        days.add(key)
        payout = float(size or 0.0) if str(bucket) == truth else 0.0
        acc["cost"] += cost
        acc["payout"] += payout
        acc["won"] += 1 if payout > 0 else 0
        pnl["filled_legs"] += 1
        pnl["won_legs"] += 1 if payout > 0 else 0
        pnl["cost_usdc"] += cost
        pnl["payout_usdc"] += payout
    pnl["station_days"] = len(days)
    pnl["pnl_usdc"] = round(pnl["payout_usdc"] - pnl["cost_usdc"], 4)
    pnl["cost_usdc"] = round(pnl["cost_usdc"], 4)
    pnl["payout_usdc"] = round(pnl["payout_usdc"], 4)
    pnl["roi"] = (
        round(pnl["pnl_usdc"] / pnl["cost_usdc"], 6)
        if pnl["cost_usdc"] > 0 else None
    )
    pnl["by_station"] = sorted((
        {"station_id": station, "legs": int(acc["legs"]),
         "won": int(acc["won"]), "pending": int(acc["pending"]),
         "cost_usdc": round(acc["cost"], 4),
         "pnl_usdc": round(acc["payout"] - acc["cost"], 4),
         "roi": (round((acc["payout"] - acc["cost"]) / acc["cost"], 6)
                 if acc["cost"] > 0 else None)}
        for station, acc in per_station.items()
    ), key=lambda item: item["cost_usdc"], reverse=True)
    pnl["available"] = pnl["filled_legs"] > 0
    if not pnl["available"]:
        pnl["reason"] = "成交腿都还没结算"
    return pnl


def _live_scope() -> dict[str, Any]:
    """The live membership, from the one place that owns it.

    Imported lazily: this module is also loaded by tooling that has no
    business starting anything, and the control server module is where the
    rule lives rather than a service that has to be running.
    """

    import sys as _sys  # noqa: PLC0415
    _scripts = str(ROOT / "scripts")
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
    from live_control_server import live_stations  # noqa: PLC0415

    return live_stations()


def collect_live_readiness(
    conn: sqlite3.Connection,
    band_stations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Everything the go-live page needs: gates per station, knobs, contracts.

    Five gates, all measured, none aesthetic: the band table's own
    realized/claimed ratio (claim honesty), the served centre's bias
    against settled truth, the ledger's chain count and ROI for the
    station, and truth freshness.  The three control files are read fresh
    at render so the page shows what production would read this cycle;
    the write path is scripts/live_control_server.py, never this page.
    """

    ratio_by_station = {
        str(row["station"]): row for row in (band_stations or [])
    }
    bias: dict[str, tuple[int, float]] = {}
    try:
        for sid, n, value in conn.execute(
            "SELECT p.station_id, COUNT(*),"
            " AVG(p.center_c - t.official_tmax_c)"
            " FROM paper_trade_position p"
            " JOIN calibration_station_daily_high_truth t"
            "   ON t.station_id=p.station_id AND t.local_date=p.event_date"
            " WHERE p.center_c IS NOT NULL GROUP BY p.station_id"
        ):
            bias[str(sid)] = (int(n), float(value))
        book: dict[str, dict[str, Any]] = {}
        settled = conn.execute(
            "SELECT station_id, event_date, hit, stake_usdc, pnl_usdc"
            " FROM paper_trade_settlement"
        ).fetchall()
        # The trailing window the release criterion reads.  Five settled days
        # is short enough to answer "has it stopped bleeding" and long enough
        # that one lucky basket cannot carry it on its own.
        recent_floor = sorted({str(row[1]) for row in settled})[-5:]
        recent_floor = recent_floor[0] if recent_floor else ""
        for sid, day, hit, stake, pnl in settled:
            slot = book.setdefault(str(sid), {
                "stake": 0.0, "pnl": 0.0, "days": set(), "settlements": 0,
                "recent_stake": 0.0, "recent_pnl": 0.0, "recent_n": 0,
            })
            slot["settlements"] += 1
            slot["stake"] += float(stake or 0.0)
            slot["pnl"] += float(pnl or 0.0)
            slot["days"].add(str(day))
            if str(day) >= recent_floor:
                slot["recent_stake"] += float(stake or 0.0)
                slot["recent_pnl"] += float(pnl or 0.0)
                slot["recent_n"] = slot.get("recent_n", 0) + 1
        truth_latest = {
            str(sid): str(day) for sid, day in conn.execute(
                "SELECT station_id, MAX(local_date)"
                " FROM calibration_station_daily_high_truth"
                " GROUP BY station_id"
            )
        }
    except sqlite3.OperationalError:
        return {"available": False}

    import datetime as _dt
    fresh_floor = (
        _dt.date.today() - _dt.timedelta(days=2)
    ).isoformat()
    # Two per-station difficulty readings (2026-09-01 investigation).
    # ``climate_jump``: mean |Tmax(d) - Tmax(d-1)| from the truth table --
    # the station's weather temperament, slow-moving and exogenous to the
    # book.  It passed every hygiene check the investigation ran (feature
    # from pre-08-27 truth only, neutral in the early window p=0.56,
    # predictive in the late one p=0.023, stable across cut points), which
    # none of the ledger-derived screens did.  ``shape_gap``: the model's
    # own mean-to-mode split, pool scope over the last 14 days -- the
    # downstream shadow of the same difficulty (r=+0.32), kept beside it as
    # corroboration.  Both are instruments for the operator's manual bar
    # rulings, not gates: the hand-picked list is the only station screen
    # that has ever beaten random on this book.
    climate_jump: dict[str, float] = {}
    try:
        truth_rows = collections.defaultdict(dict)
        for sid, day, tmax in conn.execute(
            "SELECT station_id, local_date, official_tmax_c"
            " FROM calibration_station_daily_high_truth"
        ):
            truth_rows[str(sid)][str(day)] = float(tmax)
        for sid, days in truth_rows.items():
            ordered = sorted(days)
            jumps = [
                abs(days[ordered[i]] - days[ordered[i - 1]])
                for i in range(1, len(ordered))
            ]
            if len(jumps) >= 8:
                climate_jump[sid] = sum(jumps) / len(jumps)
    except sqlite3.OperationalError:
        pass
    shape_gap: dict[str, float] = {}
    try:
        gap_floor = (
            _dt.date.today() - _dt.timedelta(days=14)
        ).isoformat()
        latest: dict[tuple[str, str], tuple[str, int]] = {}
        for sid, day, created, rowid in conn.execute(
            "SELECT station_id, event_date, created_at, rowid"
            " FROM basket_probability_envelope"
            " WHERE stage='P0' AND event_date >= ?",
            (gap_floor,),
        ):
            key = (str(sid), str(day))
            if key not in latest or created > latest[key][0]:
                latest[key] = (str(created), int(rowid))
        import re as _re
        gaps: dict[str, list[float]] = collections.defaultdict(list)
        rowids = [v[1] for v in latest.values()]
        rid_station = {v[1]: k[0] for k, v in latest.items()}
        for i in range(0, len(rowids), 500):
            part = rowids[i:i + 500]
            for rowid, pj in conn.execute(
                "SELECT rowid, probabilities_json"
                " FROM basket_probability_envelope WHERE rowid IN (%s)"
                % ",".join(map(str, part))
            ):
                try:
                    raw = {
                        b: float(v) for b, v in json.loads(pj or "{}").items()
                        if float(v) > 0
                    }
                except (TypeError, ValueError):
                    continue
                if len(raw) < 3:
                    continue
                ords = {}
                for b in raw:
                    m = _re.match(r"^\s*(-?\d+)", str(b))
                    if not m:
                        ords = None
                        break
                    ords[b] = int(m.group(1))
                if not ords:
                    continue
                total = sum(raw.values())
                mean_o = sum(ords[b] * v / total for b, v in raw.items())
                mode_o = ords[max(raw, key=lambda b: raw[b])]
                gaps[rid_station[rowid]].append(abs(mean_o - mode_o))
        for sid, values in gaps.items():
            if len(values) >= 3:
                shape_gap[sid] = sum(values) / len(values)
    except sqlite3.OperationalError:
        pass
    stations = []
    for sid in sorted(set(ratio_by_station) | set(book) | set(bias)):
        band_row = ratio_by_station.get(sid)
        ratio = float(band_row["ratio"]) if band_row else None
        chain_count = len(book[sid]["days"]) if sid in book else 0
        stake = book[sid]["stake"] if sid in book else 0.0
        roi = (book[sid]["pnl"] / stake) if stake else None
        bias_c = bias[sid][1] if sid in bias else None
        gates = {
            "claim_honest": (
                ratio is not None and 0.88 <= ratio <= 1.18
            ),
            "centre_bias": bias_c is not None and abs(bias_c) <= 0.8,
            "chains": chain_count >= 5,
            "roi": roi is not None and roi > -0.08,
            "truth_fresh": truth_latest.get(sid, "") >= fresh_floor,
        }
        recent_roi = None
        if sid in book and book[sid]["recent_stake"]:
            recent_roi = book[sid]["recent_pnl"] / book[sid]["recent_stake"]
        stations.append({
            "climate_jump_c": round(climate_jump[sid], 2)
            if sid in climate_jump else None,
            "shape_gap_buckets": round(shape_gap[sid], 2)
            if sid in shape_gap else None,
            "recent_roi": recent_roi,
            "station": sid,
            "station_label": STATION_LABELS.get(sid, ""),
            "score": sum(gates.values()),
            "gates": gates,
            "ratio": ratio,
            "bias_c": bias_c,
            "chains": chain_count,
            "roi": roi,
            "truth_latest": truth_latest.get(sid),
        })
    stations.sort(key=lambda row: (-row["score"], -(row["roi"] or -9.0)))

    def _config(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    policy = _config(ROOT / "config/trading/portfolio_capital_policy_v1.json")
    # The runtime contract the mode selects, which is the rule production
    # itself follows (weather_basket_yes_daemon.py, and the budget read in
    # production_worker.py).  This panel read the no-live file
    # unconditionally, so after the go-live migration minted the live twin
    # it reported live_submit_authorized=false about a system whose
    # contract said true -- the same mistake production_worker.py already
    # carries a comment about, and it went unnoticed there for the same
    # reason: the two files carry the same budget.  Naming the file that
    # was read is the part that makes it impossible to repeat.
    live_mode = _live_execution_mode() == "production_live"
    runtime_path = ROOT / "config/bayes_basket_target" / (
        "live_runtime_v1.json" if live_mode else "no_live_runtime_v1.json"
    )
    runtime = _config(runtime_path)
    intent = _config(ROOT / "config/trading/live_intent_v1.json")
    exclusion_doc = _config(
        ROOT / "config/trading/live_station_exclusions_v1.json"
    )
    exclusions_set = barred_station_set()
    exclusions = sorted(exclusions_set)
    auto_barred = sorted({
        str(row.get("station") or "").strip().upper()
        for row in (exclusion_doc.get("auto_excluded_stations") or [])
        if isinstance(row, dict) and row.get("station")
    })
    # Candidates, recomputed every render so the list tracks the data -- and
    # candidates only.  Fourteen automatic rules were walk-forward tested on
    # 2026-08-30 and the best was p=0.346 against excluding stations at
    # random, so nothing here may move a station by itself; the criteria earn
    # their keep by putting one in front of a person with its numbers.
    # What the bar is worth, in the only terms that settle the argument: the
    # money on each side of it.  Barred stations keep being seated and settled
    # on paper precisely so this comparison exists -- stop booking them and
    # the next release decision has nothing to read.  Both sides are the same
    # window and the same rule fingerprint, so they are comparable; neither is
    # a forecast of the other.
    def _side(members: set[str]) -> dict[str, Any]:
        rows = [r for r in stations if r["station"] in members]
        stake = sum(book[r["station"]]["stake"] for r in rows if r["station"] in book)
        pnl = sum(book[r["station"]]["pnl"] for r in rows if r["station"] in book)
        return {
            "stations": len(rows),
            "settled_days": sum(
                len(book[r["station"]]["days"]) for r in rows if r["station"] in book
            ),
            "staked_usdc": stake,
            "pnl_usdc": pnl,
            "roi": (pnl / stake) if stake else None,
        }

    # The batch that would actually trade, derived the way the control server
    # derives it: in all_except_barred mode the stored list is empty and
    # reading it would report a live open of nothing.
    # Ask the control plane rather than re-deriving it.  This page had its own
    # copy of the membership rule for one afternoon and the two drifted the
    # moment a waiver appeared: the server had LIMC live, the board drew it as
    # probation, and a reader comparing them had no way to tell which one was
    # the system.  One rule, one owner, read here.
    scope = _live_scope()
    scope_mode = scope["scope"]
    minimum = int(scope.get("minimum_settlements") or 0)
    allowlist_set = set(scope["stations"])
    probation_set = set(scope["probation"])
    waived_set = set(scope.get("probation_waivers") or [])
    held_set = set(scope.get("probation_holds") or [])
    # Which of the two bar lists a station is on.  The switch behaves
    # differently on them -- releasing an automatic bar has to record an
    # operator ruling too, or the next sweep undoes the click -- so the row
    # has to be able to say which one it is looking at.
    operator_barred = sorted({
        str(row.get("station") or "").strip().upper()
        for row in (exclusion_doc.get("excluded_stations") or [])
        if isinstance(row, dict) and row.get("station")
    })
    every = {r["station"] for r in stations}
    split = {
        "barred": _side(exclusions_set),
        "live": _side(allowlist_set),
        "probation": _side(probation_set),
        "not_barred": _side(every - exclusions_set),
        "fleet": _side(every),
    }
    for row in stations:
        row["excluded"] = row["station"] in exclusions
        barred = row["excluded"]
        roi_now, recent = row["roi"], row["recent_roi"]
        row["add_candidate"] = bool(
            not barred and roi_now is not None
            and roi_now <= -0.25 and row["chains"] >= 5
        )
        # Releasing is not the mirror of barring, and the criterion must not
        # be either.  Barring a station costs a skipped market; releasing one
        # puts money back on it, so release asks for more.  EPWA 2026-08-30 is
        # the case that forced this: it cleared an ROI-only rule on a 2.2-point
        # technicality (-22.8% against a -25% line) with a five-day window
        # reading +60.1% -- and underneath, every one of its six forward seats
        # lost 100%, the three wins were all P1 near-certainties bought after
        # the day was observed at claims of 0.93-0.99, and the model runs
        # 1.62C cold with a claim ratio of 0.46.  A recovery has to show in the
        # station's structure, not only in the last few settlements.
        row["release_candidate"] = bool(
            barred and recent is not None and recent > 0.0
            and roi_now is not None and roi_now > -0.25
            and gates["claim_honest"] and gates["centre_bias"]
            and (book[sid]["recent_n"] if sid in book else 0) >= 3
        )
    return {
        "add_candidates": [
            r["station"] for r in stations if r["add_candidate"]
        ],
        "release_candidates": [
            r["station"] for r in stations if r["release_candidate"]
        ],
        "available": True,
        "stations": stations,
        "kelly": policy.get("portfolio_risk_fraction"),
        "budget_usdc": runtime.get("target_remaining_budget_usdc"),
        "live_enabled": intent.get("live_enabled"),
        # What the system is, not what it was when this panel was written.
        # The banner used to state "三重合同仍锁 no_live" as a fixed string,
        # and the arming prompt promised "它不下单" -- both true only until
        # the go-live migration ran, after which the panel was reassuring the
        # operator about a system that had stopped being that system.
        "execution_mode": _live_execution_mode(),
        "submission_paused": (ROOT / "data" / "live_paused.flag").is_file(),
        # The live book, distinguished at the row level.  An intent minted
        # with live_submit_authorized=1 is a live order by identity -- the
        # widened CHECK is what the go-live migration existed to change --
        # while every pre-migration row carries 0.  Counted fresh at render
        # so the panel and the ledger cannot say different things.
        "live_orders": _collect_live_orders(conn),
        # The round-trip that proved the chain, kept as the record it is.
        # Before it, every claim about whether this machine could reach the
        # exchange was inference: the region gate read the website's geoblock
        # and the exchange itself had not been asked since 2026-08-07.
        "chain_probe": _read_chain_probe(),
        # Asked of the exchange, not of our own records.  An order this
        # machine placed and then lost track of -- which happened on
        # 2026-08-31, for eighteen minutes -- is invisible to every table we
        # own and visible here.
        "open_orders": _read_open_orders(),
        "station_scope": scope_mode,
        "minimum_settlements_to_qualify": minimum,
        "station_probation_waivers": sorted(waived_set),
        # The third switch: a qualified station the operator is holding back.
        # Reported apart from probation itself because the two look identical
        # on the board and are not the same fact -- one is arithmetic, the
        # other is somebody's decision this morning.
        "station_probation_holds": sorted(held_set),
        "station_allowlist": sorted(allowlist_set),
        "station_probation": sorted(probation_set),
        # The bar, not just the pass.  A station absent from the allowlist
        # might be merely unselected; one on this list is refused, and the
        # station table below marks the difference so the two do not read
        # the same.
        "station_exclusions": exclusions,
        "auto_barred": auto_barred,
        "operator_barred": operator_barred,
        "bar_split": split,
        "contracts": {
            "execution_mode": runtime.get("execution_mode"),
            "live_submit_authorized": runtime.get("live_submit_authorized"),
            "calls_order_gateway": runtime.get("calls_order_gateway"),
            "places_orders": runtime.get("places_orders"),
            "policy_scope": policy.get("authority_scope"),
            "runtime_config": runtime_path.name,
            "minted_at_utc": runtime.get("minted_at_utc"),
        },
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
            **_evidence_clock(conn, stage),
        })
    return rows


def _evidence_clock(conn: sqlite3.Connection, stage: str) -> dict[str, Any]:
    """Why this stage is still accumulating: where its floor sits and who set it.

    The fit's evidence floor is the latest of three bounds, and the AST
    fingerprint is the one that moves without anyone deciding anything: it
    hashes the serving functions, so a commit that only records a receipt
    or opens an inert replay door resets the floor exactly as a genuine
    arithmetic change does.  Measured 2026-08-27, three P0 generations
    landed in four hours and P1 has never once held a fingerprint for the
    eight days the layer needs.  Without this on the page the layer just
    reads "攒证据中" forever with nothing saying what keeps restarting it.
    """

    out: dict[str, Any] = {
        "min_qualifying_days": None,
        "ast_fingerprint": None,
        "ast_first_seen_utc": None,
        "ast_generations": None,
        "ast_days_held": None,
        "ast_longest_quiet_days": None,
        "watchdog_discrepancy": None,
        "served_version": None,
    }
    try:
        from bayes_basket_target import serving_arithmetic
        from bayes_basket_target.center_width_store import MIN_QUALIFYING_DAYS
    except ImportError:
        return out
    out["min_qualifying_days"] = MIN_QUALIFYING_DAYS
    try:
        alarm = serving_arithmetic.watchdog(conn, stage=stage)
        out["watchdog_discrepancy"] = bool(alarm.get("discrepancy"))
        out["served_version"] = alarm.get("served_version")
    except sqlite3.OperationalError:
        pass
    try:
        current = serving_arithmetic.fingerprint(stage)
        stamps = [
            (str(sha), str(seen))
            for sha, seen in conn.execute(
                "SELECT fingerprint_sha256, first_seen_utc"
                " FROM calibration_serving_arithmetic"
                " WHERE stage=? ORDER BY first_seen_utc",
                (stage,),
            )
        ]
    except sqlite3.OperationalError:
        return out
    if not stamps:
        return out
    out["ast_generations"] = len(stamps)
    seen_by_sha = {sha: seen for sha, seen in stamps}
    first_seen = seen_by_sha.get(str(current or ""))
    if first_seen:
        out["ast_fingerprint"] = str(current)[:10]
        out["ast_first_seen_utc"] = first_seen
        held = _age_hours(first_seen)
        out["ast_days_held"] = None if held is None else held / 24.0
    moments = [seen for _sha, seen in stamps]
    gaps = [
        (_age_hours(a) or 0.0) - (_age_hours(b) or 0.0)
        for a, b in zip(moments, moments[1:])
    ]
    newest_age = _age_hours(moments[-1])
    if newest_age is not None:
        gaps.append(newest_age)
    if gaps:
        out["ast_longest_quiet_days"] = max(gaps) / 24.0
    return out


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


# Where the truth lands relative to the distribution the seat is built on,
# split by how wide that distribution called itself (2026-09-05, user asked
# for the sigma-tier landing table on the page).  The centre/width section
# above says what the layer *does*; this says whether what it serves is
# where the day actually went, which is the question the seat rule and the
# positive-EV gate both read through the mean+-1 window.
CENTER_LANDING_TIERS = (
    (0.0, 1.44, "narrow", "尖 <1.44"),
    (1.44, 2.0, "mid", "中 1.44-2.0"),
    (2.0, 2.5, "wide", "宽 2.0-2.5"),
    (2.5, 3.0, "flat", "平 2.5-3.0"),
    (3.0, 99.0, "flattest", "最平 ≥3.0"),
)
CENTER_LANDING_FLAT_RUNGS = 2.5
CENTER_LANDING_STAGE_MEANING = {
    "P0": "开仓：模型融合出的第一版概率",
    "P0-M": "确认：早上的 TAF 对 P0 的修正",
    "P1": "盯盘：当天实测进来后的滚动更新",
}


def _center_landing_bootstrap(
    by_day: dict[str, list[float]], draws: int = 600, seed: int = 11
) -> tuple[float | None, float | None]:
    """A 95% interval for the mean, resampling whole days.

    The day is the unit because one weather day is shared across stations:
    resampling cells would call a fleet-wide warm day forty-five
    independent observations and shrink the interval to nothing.
    """

    days = list(by_day)
    if not days:
        return None, None
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        pick = [rng.choice(days) for _ in days]
        values = [value for day in pick for value in by_day[day]]
        if values:
            means.append(sum(values) / len(values))
    if not means:
        return None, None
    means.sort()
    return (
        means[int(0.025 * (len(means) - 1))],
        means[int(0.975 * (len(means) - 1))],
    )


def collect_center_landing(conn: sqlite3.Connection) -> dict[str, Any]:
    """Truth minus the served mean, in rungs, by how wide the day claimed to be.

    Read off the centre/width layer's own serving cells (one decision per
    station-day-stage-hourblock, anchored at the served-arithmetic
    boundary) and the official truth, so the population is the one that
    layer fits on and nothing here re-derives a rule.  Distances are in
    rungs of each station's own ladder (charter 53(b)), which is what makes
    a Celsius and a Fahrenheit market comparable in one column.
    """

    try:
        from bayes_basket_target.center_width_calibration import (
            sigma_floor_lift_v1,
        )
        from bayes_basket_target.center_width_store import (
            _evidence_floor,
            _truth,
            serving_cells_by_day,
        )
    except ImportError:
        return {}
    if not _table_exists(conn, "basket_probability_bridge_history"):
        return {}
    truth = _truth(conn)
    stages_out: list[dict[str, Any]] = []
    for stage in ("P0", "P0-M", "P1"):
        try:
            floor = _evidence_floor(conn, stage=stage, caller_bound=None)
            by_day = serving_cells_by_day(
                conn, truth, stage=stage, min_decision_at_utc=floor,
            )
        except Exception:  # noqa: BLE001 -- a panel must not blank the page
            continue
        landings: list[tuple[float, float, float, str, str]] = []
        for day in sorted(by_day):
            for cell in by_day[day]:
                probabilities = cell["probabilities"]
                keys = cell["ordered_keys"]
                centers = cell["key_centers_c"]
                actual = truth.get((cell["station_id"], cell["event_date"]))
                if actual is None:
                    continue
                try:
                    _lift, sigma_c, rung_c = sigma_floor_lift_v1(
                        probabilities,
                        ordered_keys=keys,
                        key_centers_c=centers,
                        floor_rungs=0.0,
                    )
                except (ValueError, KeyError):
                    continue
                if rung_c <= 0.0:
                    continue
                total = sum(probabilities.values())
                if total <= 0.0:
                    continue
                mean_c = sum(
                    centers[index] * probabilities[key]
                    for index, key in enumerate(keys)
                ) / total
                peak = max(
                    range(len(keys)),
                    key=lambda index: (probabilities[keys[index]], -index),
                )
                # The seat window itself (charter 49/79): the centre bucket
                # and one rung either side.  Not "within one rung of the
                # mean point" -- that is a different, narrower set, and
                # reporting it under the seat's name is how a reader
                # concludes the seat covers 40% of flat days when it
                # covers 55%.
                centre = min(
                    range(len(keys)),
                    key=lambda index: (abs(centers[index] - mean_c), index),
                )

                def _window(anchor: int) -> tuple[float, int]:
                    names = keys[max(0, anchor - 1):anchor + 2]
                    return (
                        sum(probabilities[name] for name in names) / total,
                        1 if cell["settled_bucket"] in names else 0,
                    )

                mean_claim, mean_hit = _window(centre)
                mode_claim, mode_hit = _window(peak)
                landings.append((
                    sigma_c / rung_c,
                    (float(actual) - mean_c) / rung_c,
                    mean_claim,
                    mean_hit,
                    mode_claim,
                    mode_hit,
                    peak - centre,
                    cell["station_id"],
                    cell["event_date"],
                ))
        if not landings:
            continue
        rows = []
        for lower, upper, key, label in CENTER_LANDING_TIERS:
            members = [row for row in landings if lower <= row[0] < upper]
            if not members:
                continue
            errors = [row[1] for row in members]
            count = len(errors)
            mean_error = sum(errors) / count
            spread = (
                math.sqrt(
                    sum((value - mean_error) ** 2 for value in errors)
                    / (count - 1)
                )
                if count > 1 else None
            )
            by_event: dict[str, list[float]] = {}
            for row in members:
                by_event.setdefault(row[8], []).append(row[1])
            low, high = _center_landing_bootstrap(by_event)
            seat_claim = sum(row[2] for row in members) / count
            seat_cover = sum(row[3] for row in members) / count
            mode_claim = sum(row[4] for row in members) / count
            mode_cover = sum(row[5] for row in members) / count
            rows.append({
                "tier": key,
                "label": label,
                "cells": count,
                "bias_rungs": mean_error,
                "ci_low": low,
                "ci_high": high,
                "median_rungs": sorted(errors)[count // 2],
                "sd_rungs": spread,
                "warm_share": sum(
                    1 for value in errors if value > 0.0
                ) / count,
                "seat_claim": seat_claim,
                "seat_cover": seat_cover,
                "seat_ratio": (
                    seat_cover / seat_claim if seat_claim > 0.0 else None
                ),
                "mode_cover": mode_cover,
                "mode_ratio": (
                    mode_cover / mode_claim if mode_claim > 0.0 else None
                ),
                "same_window_share": sum(
                    1 for row in members if row[6] == 0
                ) / count,
                "significant": bool(
                    low is not None and high is not None
                    and (low > 0.0 or high < 0.0)
                ),
            })
        flat = [
            row for row in landings
            if row[0] >= CENTER_LANDING_FLAT_RUNGS
        ]
        by_station: dict[str, list[float]] = {}
        for row in flat:
            by_station.setdefault(row[7], []).append(row[1])
        flat_stations = sorted(
            (
                {
                    "station": station,
                    "cells": len(values),
                    "bias_rungs": sum(values) / len(values),
                    "warm_share": sum(
                        1 for value in values if value > 0.0
                    ) / len(values),
                    "days": len({
                        row[8] for row in flat if row[7] == station
                    }),
                }
                for station, values in by_station.items()
                if len(values) >= 8
            ),
            key=lambda entry: -abs(entry["bias_rungs"]),
        )[:10]
        stages_out.append({
            "stage": stage,
            "meaning": CENTER_LANDING_STAGE_MEANING.get(stage, ""),
            "cells": len(landings),
            "days": len(by_day),
            "since": (floor or "")[:10],
            "rows": rows,
            "flat_stations": flat_stations,
            "flat_cells": len(flat),
        })
    if not stages_out:
        return {}
    return {
        "schema": "center_landing_v1",
        "flat_threshold_rungs": CENTER_LANDING_FLAT_RUNGS,
        "stages": stages_out,
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
    # Five-percent steps capped at 50% (user ruling, 2026-08-25): the band
    # cards in section six now render on the same grid, so the knob table
    # and this audit finally line up row for row.  Everything promised at
    # half or more pools into one open top bin -- claims that high are rare
    # enough that finer rows would be noise wearing labels.
    buckets: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for promised, happened in pairs:
        index = min(10, max(0, int(promised / 0.05)))
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
            "bin": (
                "≥50%" if index >= 10
                else f"{index * 5}-{index * 5 + 5}%"
            ),
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


LIVE_STATION_EXCLUSIONS_PATH = (
    ROOT / "config/trading/live_station_exclusions_v1.json"
)


def barred_station_set() -> set[str]:
    """Stations the live lane may not trade -- hand-ruled and automatic.

    One reader on purpose.  The live-readiness panel and the daily table's
    station breakdown both need this list, and two copies of the same file
    read is how two panels come to disagree about who is barred.
    """

    try:
        doc = json.loads(
            LIVE_STATION_EXCLUSIONS_PATH.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return set()
    return {
        str(row.get("station") or "").strip().upper()
        for key in ("excluded_stations", "auto_excluded_stations")
        for row in (doc.get(key) or [])
        if isinstance(row, dict) and row.get("station")
    }


def _station_breakdown(
    roster: list[dict[str, Any]], barred: set[str]
) -> dict[str, dict[str, Any]]:
    """Per event day: of the whole fleet, who opened and why the rest did not.

    The daily table's station count says how many stations carried a
    position; on 2026-08-30 that was 29 and the fleet was 48, and the page
    left the other nineteen unaccounted for -- a reader could not tell a day
    the model had nothing to say about from a day the market priced us out.
    The ledger already records its own reason per station-day, so this
    groups those rather than guessing at them (user ruling, 2026-09-02).

    The live bar is counted separately and never folded into the reasons.
    It is a trading ruling about the live lane, not a reason a paper seat
    was not taken -- the book seats a barred station exactly as it seats any
    other -- so adding it to the same total would double-count the ones that
    opened.
    """

    by_day: dict[str, dict[str, Any]] = {}
    grouped: dict[str, Counter] = defaultdict(Counter)
    fleet: Counter = Counter()
    opened: Counter = Counter()
    barred_seen: dict[str, set[str]] = defaultdict(set)
    for entry in roster:
        day = str(entry.get("event_date") or "")
        if not day:
            continue
        station = str(entry.get("station_id") or "")
        fleet[day] += 1
        if station.upper() in barred:
            barred_seen[day].add(station)
        stages = entry.get("stages") or {}
        if any(stages.get(name) for name in ("P0", "P0-M", "P1")):
            opened[day] += 1
            continue
        grouped[day][str(entry.get("absent_reason") or "unknown")] += 1
    for day, total in fleet.items():
        counts = grouped.get(day) or Counter()
        by_day[day] = {
            "fleet": total,
            "opened": opened.get(day, 0),
            "not_opened": total - opened.get(day, 0),
            "reasons": [
                {"reason": reason, "count": count}
                for reason, count in sorted(
                    counts.items(), key=lambda item: (-item[1], item[0])
                )
            ],
            "live_barred": len(barred_seen.get(day) or ()),
        }
    return by_day


# What a scope refusal means, for the one surface a person reads it on.
#
# The board printed these codes verbatim, and one of them is actively
# misleading: ``complete_basket_target_not_activated`` reads as "a feature
# is switched off" and means the opposite -- the target-inventory argmax
# chose the no-new-action baseline, which is an economic HOLD by a working
# engine.  On 2026-09-08 it was 11 of one batch's 15 refusals, and it cost
# an hour of misdiagnosis: the code was read as a disabled completion path
# while the engine was in fact declining to buy, on purpose, every time.
#
# Renamed nowhere.  The string is the charter's own spelling for clause 66
# (乙), clause 96 cites it as evidence, a doctrine test binds it to the
# kernel source, and 13,915 decision rows carry it -- so the identifier is
# constitutional and this is a reading fix, not a rename.  The raw codes
# stay untouched in ``blocked_scope_reasons`` for anything that greps.
#
# ``.get(name, name)`` on purpose: an unmapped code shows raw rather than
# being hidden or guessed at.
BLOCKED_SCOPE_REASON_LABELS = {
    "complete_basket_target_not_activated": "经济性持有（不新建仓最优）",
    "complete_basket_candidate_missing": "缺候选腿",
    "complete_basket_delta_not_positive": "增量不为正",
    "delta_v2_support_empty": "无可选支撑窗口",
    "exchange_minimum_lot_unaffordable": "交易所最小手买不起",
    "production_quote_expired": "报价过期",
    "p1_entry_window_not_open": "P1 入场窗未开",
    "preliminary_no_live_p1_pending": "P1 待定（初步）",
    "sizing_clamped_below_authorized_clip": "定量被砍到授权额以下",
    "bundle_rescore_refused": "重评拒绝",
    "support_floor_after_lift": "抬手后支持集地板拒",
    "no_decision_moment": "非决策时刻",
}


def _daily_table(
    selection: dict[str, Any],
    cover: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One row per event day: what was seated, and how it settled.

    Credible fills only.  A sub-one-cent basket is a minimum-tick offer with
    nothing bid against it, and letting those into a daily P&L makes every
    day they appear on unreadable.

    ``cover`` is the P0 intended-window cover rate keyed by event date,
    folded in here because the panel that used to carry it was a second
    place to look for the same day's account (user ruling, 2026-08-28).
    Its population is not this table's: it counts every P0 station-day the
    ledger recorded a centre for, seated or not, filled or not, so its
    station count can exceed the day's position count.
    """

    stages = ("P0", "P0-M", "P1")
    breakdown = _station_breakdown(
        list(selection.get("station_roster") or []), barred_station_set()
    )
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selection.get("rows") or []:
        if row.get("dust_offer"):
            continue
        by_day[str(row["event_date"])].append(row)
    table = []
    for day in sorted(set(by_day) | set(cover or {}), reverse=True):
        rows = by_day.get(day, [])
        # One row per chain: the settlement is joined onto every stage row
        # of its station-day, so summing settled stage rows counts one
        # chain's P&L two or three times.  Only the tail row is the chain.
        settled = [
            r for r in rows if r.get("settled") and r.get("chain_tail")
        ]
        hits = sum(int(r["hit"]) for r in settled)
        pnl = sum(float(r["pnl_usdc"] or 0.0) for r in settled)
        # What the day actually put on the table.  Summing over settled
        # chains made an unsettled day read as 0.00 deployed while it was
        # carrying real positions (user ruling, 2026-08-29).  One reading
        # per station -- the chain stake, which every stage of the chain
        # carries -- so a topped-up day is not counted twice.
        chain_stake_by_station: dict[str, float] = {}
        for row in rows:
            key = str(row["station_id"])
            value = float(row.get("chain_stake_usdc") or 0.0)
            if value > chain_stake_by_station.get(key, 0.0):
                chain_stake_by_station[key] = value
        staked = sum(chain_stake_by_station.values())
        # P&L only ever belongs to settled chains, so the ratio a reader
        # forms from these two columns is only meaningful once the day has
        # settled; the settled-only stake is kept for that.
        settled_staked = sum(float(r["chain_stake_usdc"]) for r in settled)
        odds = [r["odds"] for r in settled if r.get("odds") is not None]
        claimed = (
            sum(float(r["win_probability"]) for r in settled) / len(settled)
            if settled else None
        )
        # A day that has not settled still knows what it claimed: the seat
        # stated a win probability the moment it was taken.  Averaging only
        # over settled chains left the two newest rows -- the days a reader
        # most wants to look at -- printing a dash for a number that has
        # existed since the position opened (user ruling, 2026-08-29).
        # One reading per station: the chain's latest stage, which is the
        # claim the position currently carries.
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row["station_id"])
            prior = latest.get(key)
            if prior is None or str(row["decision_at_utc"]) > str(
                prior["decision_at_utc"]
            ):
                latest[key] = row
        claimed_open = (
            sum(float(r["win_probability"]) for r in latest.values())
            / len(latest)
            if latest else None
        )
        # Coverage is not the chance of coming out ahead, and on an unequal
        # basket the two are far apart (user ruling, 2026-09-09).  A seat
        # holding ``n_k`` shares of bucket k against an all-in premium C
        # profits by ``n_k - C`` when the day settles there, so a rung with
        # ``n_k <= C`` LOSES on the day it wins -- and ``win_probability``
        # counts that day as a hit.  Measured 2026-09-09 over 235 seated
        # baskets, 26 carried such a rung: MMMX 2026-09-06 covered 0.789 of
        # the claim and had a 0.107 chance of ending ahead.
        #
        # Unheld buckets contribute nothing by construction: they pay zero,
        # so they are never profitable, which is why this is read off the
        # legs rather than off the window.
        def _net_profit_probability(row: Mapping[str, Any]) -> float | None:
            try:
                legs = json.loads(str(row.get("leg_shares_json") or "{}"))
                claims = json.loads(str(row.get("probabilities_json") or "{}"))
                cost = float(row.get("chain_stake_usdc") or 0.0)
            except (TypeError, ValueError):
                return None
            if not isinstance(legs, dict) or not isinstance(claims, dict):
                return None
            if cost <= 0.0:
                return None
            return sum(
                float(claims.get(bucket, 0.0) or 0.0)
                for bucket, shares in legs.items()
                if float(shares or 0.0) > cost
            )

        net_profit_open = [
            value for value in (
                _net_profit_probability(row) for row in latest.values()
            ) if value is not None
        ]
        net_profit_settled = [
            value for value in (
                _net_profit_probability(row) for row in settled
            ) if value is not None
        ]
        table.append({
            "event_date": day,
            # The open claim is only shown where the settled one cannot be:
            # they are averaged over different populations and must never be
            # read as the same column.
            "claimed_open_mean": claimed_open,
            # Beside the claim, never instead of it: the two answer
            # different questions and a reader who sees only the first
            # reads a covered day as a winning one.
            "net_profit_probability_mean": (
                sum(net_profit_settled) / len(net_profit_settled)
                if net_profit_settled else None
            ),
            "net_profit_probability_open_mean": (
                sum(net_profit_open) / len(net_profit_open)
                if net_profit_open else None
            ),
            "station_count": len({r["station_id"] for r in rows}),
            # The rest of the fleet, and the ledger's own word for why each
            # one is not in the count above.
            "station_breakdown": breakdown.get(day),
            "position_count": len(rows),
            "settled_count": len(settled),
            # Where the seat sat against the market, judged by the bucket
            # the seat was actually built around: charter 54's mean landing
            # bucket, not the unsnapped centre of mass (user ruling,
            # 2026-08-28).  The other sections still read ``side``; this one
            # is the mean-bucket reading and says so in its header.
            "sides": {
                side: sum(1 for r in rows if r["ladder_side"] == side)
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
            # The same bought rungs split by which stage bought them.  One
            # position is opened by P0 and topped up by P0-M and P1, so a
            # single filled total cannot say whether a day was built at
            # entry or added to afterwards.
            "filled_leg_by_stage": {
                stage: sum(
                    int(r["entry"]["filled_leg_count"])
                    for r in rows if str(r["stage"]) == stage
                )
                for stage in stages
            },
            "posted_leg_by_stage": {
                stage: sum(
                    int(r["entry"]["posted_leg_count"])
                    for r in rows if str(r["stage"]) == stage
                )
                for stage in stages
            },
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
            "settled_staked_usdc": settled_staked,
            "pnl_usdc": pnl,
            **((cover or {}).get(day) or {
                "cover_rate": None,
                "truth_inside_rate": None,
                "cover_station_count": 0,
                "cover_settled_count": 0,
                "unfilled_intended": 0,
            }),
        })
    return table


def _intended_cover_by_day(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    """Did the truth land inside the P0 basket we meant to buy?

    One reading per station-day the P0 stage posted an intended basket for,
    settled or not, part-filled or not (user ruling, 2026-08-25), rolled up
    by event date for the daily table (user ruling, 2026-08-28: one place to
    read a day's account, not two).  The window is the INTENDED legs, not
    the filled ones -- what the book sold us is execution, and cover grades
    the choice.  ``ladder_center_bucket`` is read back verbatim as the
    seat's own record of its centre, never recomputed here, and a row whose
    centre cannot be located in the ladder is dropped rather than guessed.

    Station-days whose resting orders never filled a single leg carry no
    position row, and the refusal table keeps only prose for them, so their
    centres cannot be read back; each day counts them as absent so the gap
    is on the record rather than silent.
    """

    if not _table_exists(conn, "paper_trade_position"):
        return {}
    # The same generation floor the position rows carry.  These two
    # populations now share a table, and a day that reached one of them but
    # not the other would read as a day the other one lost.
    unfilled: dict[str, int] = {}
    for day, count in conn.execute(
        "SELECT event_date, COUNT(*) FROM paper_trade_refusal"
        " WHERE stage = 'P0'"
        " AND reason IN ('resting_unfilled', 'belief_withdrew_order')"
        " AND event_date >= ?"
        " GROUP BY event_date",
        (STATS_START_EVENT_DATE,),
    ):
        unfilled[str(day)] = int(count)
    query = conn.execute(
        "SELECT p.event_date, p.station_id, p.ladder_center_bucket,"
        " p.probabilities_json, p.intended_legs_json,"
        " s.settled_bucket, s.settled_tmax_c"
        " FROM paper_trade_position p"
        " LEFT JOIN paper_trade_settlement s"
        " ON s.station_id = p.station_id AND s.event_date = p.event_date"
        " WHERE p.stage = 'P0' AND p.event_date >= ?"
        # One reading per station-day, and the ``seen`` set below keeps the
        # first row it meets.  P0 may seat twice on one day since the
        # one-seat constraint was rebuilt away (2026-09-02), so without an
        # order that "first" is whatever the query planner hands back and
        # the cover rate can move without the ledger moving.  The opening
        # decision is the one this reading has always taken; name it.
        " ORDER BY p.event_date, p.station_id, p.decision_at_utc",
        (STATS_START_EVENT_DATE,),
    )
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in query:
        key = (str(record[0]), str(record[1]))
        if key in seen:
            continue
        seen.add(key)
        try:
            probabilities = json.loads(record[3] or "{}") or {}
            intended = json.loads(record[4] or "{}") or {}
        except (TypeError, ValueError):
            continue
        # Key order is ladder order: the ledger writes probabilities_json
        # from the ladder walk, and the selection payload's own lean and
        # skew columns already lean on that.
        ordinals = {
            str(bucket): index
            for index, bucket in enumerate(probabilities.keys())
        }
        center = str(record[2] or "")
        center_ordinal = ordinals.get(center)
        if center_ordinal is None:
            continue
        window = sorted(
            (str(bucket) for bucket in intended if str(bucket) in ordinals),
            key=lambda bucket: ordinals[bucket],
        )
        settled_bucket = str(record[5] or "")
        truth_ordinal = (
            ordinals.get(settled_bucket) if settled_bucket else None
        )
        rows.append({
            "event_date": key[0],
            "intended_cover": (
                None if truth_ordinal is None
                else (1 if settled_bucket in set(window) else 0)
            ),
            # What the model CLAIMED the intended window covers: the mass it
            # put on those buckets, summed.  Known the moment the basket is
            # chosen (user ruling, 2026-08-29) -- the realised cover above
            # needs the truth, this does not, and a day with no settlement
            # was printing a dash for both.
            "claimed_cover": sum(
                float(probabilities.get(bucket) or 0.0) for bucket in window
            ),
        })
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in rows:
        by_day[entry["event_date"]].append(entry)
    out: dict[str, dict[str, Any]] = {}
    for day in sorted(set(by_day) | set(unfilled), reverse=True):
        members = by_day.get(day, ())
        covers = [
            e["intended_cover"] for e in members
            if e["intended_cover"] is not None
        ]
        claims = [
            e["claimed_cover"] for e in members
            if e.get("claimed_cover") is not None
        ]
        out[day] = {
            # 意向窗盖住率 IS the probability the chosen buckets carry --
            # the model's own mass summed over the intended window (user
            # ruling, 2026-08-30).  It is not a hit rate, claimed or
            # realised: those are different questions and they already have
            # their own columns.  Known the moment the basket is chosen, so
            # it never waits for settlement.
            "cover_rate": sum(claims) / len(claims) if claims else None,
            # Whether the truth actually landed inside that window, kept
            # under its own name so it can never be read as the line above.
            "truth_inside_rate": (
                sum(covers) / len(covers) if covers else None
            ),
            "cover_station_count": len(members),
            "cover_settled_count": len(covers),
            "unfilled_intended": unfilled.get(day, 0),
        }
    return out


def collect_risk_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    """The book as a portfolio: Sharpe against its target, and its cousins.

    One return per event day -- the day's settled P&L over the equity the
    book opened that day with -- which is the series the equity curve is
    actually built from, so the chart and the ratios cannot disagree.
    Annualised at 365, not 252: this book trades every calendar day it has
    a market for, weekends included.

    The honest part is the error bar.  A Sharpe estimated on a handful of
    days is almost pure noise, and the classical standard error says so:
    ``sqrt((1 + S^2/2) / n)`` per period, which annualises by the same
    root-365 the ratio does.  On this book that error bar is several times
    the estimate, so the section leads with the interval and with how many
    days it would take to tell the number from zero -- otherwise a reader
    sees "1.54" beside a target of 2 and thinks the gap is the finding.
    """

    # Claimed EV comes along for the ride: a basket costing C pays 1 when
    # the truth lands inside, so a unit of stake buys 1/C of payout and
    # expects p/C - 1 back.  That is the same ``ev_per_usdc`` the coverage
    # section reports, summed in money instead of averaged per seat, so
    # the board has exactly one definition of EV and not a second one
    # wearing the same name.
    rows = conn.execute(
        "SELECT event_date, SUM(stake_usdc), SUM(pnl_usdc),"
        " SUM(CASE WHEN basket_all_in_cost > 0"
        "   THEN stake_usdc * (claimed_win_probability"
        "        / basket_all_in_cost - 1.0) ELSE 0 END),"
        # Odds and cost, weighted by the money that bought them: a 30-USDC
        # seat at 1.4x and a 1-USDC seat at 9x do not average to 5.2x for
        # any purpose this book has.
        " SUM(CASE WHEN basket_all_in_cost > 0"
        "   THEN stake_usdc * (1.0 - basket_all_in_cost)"
        "        / basket_all_in_cost ELSE 0 END),"
        " SUM(stake_usdc * basket_all_in_cost),"
        " SUM(stake_usdc * hit), COUNT(*), SUM(hit)"
        " FROM paper_trade_settlement GROUP BY 1 ORDER BY 1"
    ).fetchall()
    if len(rows) < 3:
        return {"available": False, "days": len(rows)}
    equity = INITIAL_CAPITAL_USDC
    series: list[dict[str, Any]] = []
    returns: list[float] = []
    claimed_ev_total = 0.0
    claimed_equity = INITIAL_CAPITAL_USDC
    odds_weighted = 0.0
    cost_weighted = 0.0
    hit_weighted = 0.0
    chain_count = 0
    hit_count = 0
    for day, staked, pnl, claimed_ev, odds_w, cost_w, hit_w, n, hits in rows:
        odds_weighted += float(odds_w or 0.0)
        cost_weighted += float(cost_w or 0.0)
        hit_weighted += float(hit_w or 0.0)
        chain_count += int(n or 0)
        hit_count += int(hits or 0)
        opening = equity
        value = float(pnl or 0.0)
        expected = float(claimed_ev or 0.0)
        returns.append(value / opening if opening > 0 else 0.0)
        equity = opening + value
        claimed_ev_total += expected
        claimed_equity += expected
        series.append({
            "event_date": str(day),
            "staked_usdc": round(float(staked or 0.0), 2),
            "pnl_usdc": round(value, 2),
            "claimed_ev_usdc": round(expected, 2),
            "opening_equity_usdc": round(opening, 2),
            "equity_usdc": round(equity, 2),
            "claimed_equity_usdc": round(claimed_equity, 2),
            "return": returns[-1],
        })
    count = len(returns)
    mean = sum(returns) / count
    variance = sum((r - mean) ** 2 for r in returns) / count
    sigma = math.sqrt(variance)
    # Downside deviation against a zero target: the losing days only, and
    # divided by the whole count -- the winning days are not evidence that
    # the downside is small, they are evidence that there is upside.
    downside = math.sqrt(
        sum(min(0.0, r) ** 2 for r in returns) / count
    )
    annual = 365.0
    root = math.sqrt(annual)
    sharpe = (mean / sigma * root) if sigma > 0 else None
    sortino = (mean / downside * root) if downside > 0 else None
    per_period = (mean / sigma) if sigma > 0 else 0.0
    sharpe_se = math.sqrt((1.0 + 0.5 * per_period ** 2) / count) * root
    # How long until the estimate could clear zero at 95%.  Solving
    # 1.96 * se(n) < |S| for n, with se scaling as 1/sqrt(n).
    days_needed = None
    if sharpe not in (None, 0.0):
        days_needed = int(math.ceil(
            (1.96 ** 2) * (1.0 + 0.5 * per_period ** 2) * annual
            / (sharpe ** 2)
        ))
    # The running peak starts at the capital the book opened with, not at
    # its first closing equity.  Seeded from the series instead, a book
    # that lost on day one had that loss excluded from its own drawdown:
    # this one opened at 1000, closed day one at 947.8, and reported -8.2%
    # where the trough is -9.3% below the money that was actually put in.
    # Value at Risk and its conditional cousin, from the empirical tail --
    # no distribution assumed, because fifteen points cannot support one.
    #
    # The honest part is the tail count.  Expected shortfall at 95% asks
    # for the mean of the worst 5% of days; on fifteen days that is the
    # worst 0.75 of a day, which rounds to exactly one observation.  Both
    # the 95% and 90% levels therefore report the same number and it is
    # not an estimate of a tail -- it is the worst day this book has had.
    # A tail needs its own sample: five observations in the tail wants
    # 100 days at 95%, 50 at 90%, 25 at 80%.  So each level is published
    # WITH the count standing behind it, and the section refuses to let a
    # one-observation number wear the same weight as the others.
    ordered = sorted(returns)

    def tail_at(level: float) -> dict[str, Any]:
        wanted = math.floor((1.0 - level) * count)
        size = max(1, wanted)
        tail = ordered[:size]
        return {
            "level": level,
            "tail_days": size,
            "tail_days_wanted": wanted,
            "var": tail[-1],
            "cvar": sum(tail) / size,
            "days_for_five_in_tail": int(math.ceil(5.0 / (1.0 - level))),
        }

    tails = [tail_at(level) for level in (0.95, 0.90, 0.80)]
    daily_ratios = [
        row["pnl_usdc"] / row["claimed_ev_usdc"]
        for row in series if row["claimed_ev_usdc"] > 0
    ]
    window = series[-7:]
    window_claim = sum(row["claimed_ev_usdc"] for row in window)
    trailing_7d = (
        sum(row["pnl_usdc"] for row in window) / window_claim
        if window_claim > 0 else None
    )
    losing = [r for r in returns if r < 0]
    peak = float(INITIAL_CAPITAL_USDC)
    max_drawdown = 0.0
    trough_day = ""
    peak_day = ""
    running_peak_day = "开仓前"
    for row in series:
        if row["equity_usdc"] > peak:
            peak = row["equity_usdc"]
            running_peak_day = row["event_date"]
        drop = (row["equity_usdc"] - peak) / peak if peak > 0 else 0.0
        if drop < max_drawdown:
            max_drawdown = drop
            trough_day = row["event_date"]
            peak_day = running_peak_day
    # Least squares on cumulative P&L: the straight line a reader draws by
    # eye, made explicit so the eye is not doing the fitting.
    xs = list(range(1, count + 1))
    ys = [row["equity_usdc"] - INITIAL_CAPITAL_USDC for row in series]
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    denominator = sum((x - mean_x) ** 2 for x in xs)
    slope = (
        sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
        if denominator else 0.0
    )
    intercept = mean_y - slope * mean_x
    residual = sum(
        (y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys)
    )
    total_ss = sum((y - mean_y) ** 2 for y in ys)
    total_return = (equity / INITIAL_CAPITAL_USDC) - 1.0
    return {
        "available": True,
        "days": count,
        "first_day": series[0]["event_date"],
        "last_day": series[-1]["event_date"],
        "initial_equity_usdc": INITIAL_CAPITAL_USDC,
        "final_equity_usdc": round(equity, 2),
        "total_return": total_return,
        "annualised_return_geometric": (
            (equity / INITIAL_CAPITAL_USDC) ** (annual / count) - 1.0
            if equity > 0 else None
        ),
        "annualised_return_arithmetic": mean * annual,
        "annualised_volatility": sigma * root,
        "daily_mean_return": mean,
        "daily_volatility": sigma,
        "downside_deviation": downside,
        "sharpe": sharpe,
        "sharpe_target": 2.0,
        "sharpe_standard_error": sharpe_se,
        "sharpe_ci": (
            None if sharpe is None
            else [sharpe - 1.96 * sharpe_se, sharpe + 1.96 * sharpe_se]
        ),
        "days_to_separate_from_zero": days_needed,
        "sortino": sortino,
        # What the book said it would make, against what it made.  Not a
        # forecast score -- the claim is what sized every bet, so a ratio
        # far from 1.0 says the sizing input is wrong, not just the copy.
        "claimed_ev_usdc": claimed_ev_total,
        "total_staked_usdc": sum(row["staked_usdc"] for row in series),
        # (1 - cost) / cost: what a winning USDC pays back on top of itself.
        "odds_mean": (
            odds_weighted / sum(row["staked_usdc"] for row in series)
            if sum(row["staked_usdc"] for row in series) > 0 else None
        ),
        "cost_mean": (
            cost_weighted / sum(row["staked_usdc"] for row in series)
            if sum(row["staked_usdc"] for row in series) > 0 else None
        ),
        # The win rate those odds need just to break even, and what the
        # book actually hit -- the pair that says whether the price was
        # payable at all.
        "realised_win_rate_weighted": (
            hit_weighted / sum(row["staked_usdc"] for row in series)
            if sum(row["staked_usdc"] for row in series) > 0 else None
        ),
        "realised_win_rate": (hit_count / chain_count) if chain_count else None,
        "chain_count": chain_count,
        "breakeven_win_rate": (
            cost_weighted / sum(row["staked_usdc"] for row in series)
            if sum(row["staked_usdc"] for row in series) > 0 else None
        ),
        "claimed_ev_per_usdc": (
            claimed_ev_total / sum(row["staked_usdc"] for row in series)
            if sum(row["staked_usdc"] for row in series) > 0 else None
        ),
        "realised_pnl_usdc": equity - INITIAL_CAPITAL_USDC,
        "ev_realisation_ratio": (
            (equity - INITIAL_CAPITAL_USDC) / claimed_ev_total
            if claimed_ev_total > 0 else None
        ),
        "days_beating_claim": sum(
            1 for row in series
            if row["pnl_usdc"] >= row["claimed_ev_usdc"]
        ),
        # One ratio cannot answer this.  Money-weighted, a handful of big
        # losing days set the whole number: 0.055x aggregate against 0.343x
        # on the median day, a factor of six apart on the same fifteen days
        # (user challenge, 2026-08-30).  Both are published, plus the
        # trailing window, because they answer different questions -- what
        # happened to the money, what a typical day does, and where it has
        # been lately.  None of the three escapes the sample-size problem
        # the Sharpe tile states.
        "ev_realisation_daily_median": (
            statistics.median(daily_ratios) if daily_ratios else None
        ),
        "ev_realisation_daily_mean": (
            statistics.fmean(daily_ratios) if daily_ratios else None
        ),
        "ev_realisation_trailing_7d": trailing_7d,
        "ev_realisation_daily": daily_ratios,
        "tails": tails,
        "losing_days": len(losing),
        "mean_losing_day": (sum(losing) / len(losing)) if losing else None,
        "worst_day": ordered[0] if ordered else None,
        "worst_day_date": (
            next(
                (row["event_date"] for row in series
                 if abs(row["return"] - ordered[0]) < 1e-12),
                "",
            ) if ordered else ""
        ),
        "max_drawdown": max_drawdown,
        "max_drawdown_peak_day": peak_day,
        "max_drawdown_trough_day": trough_day,
        "trend_slope_usdc_per_day": slope,
        "trend_intercept_usdc": intercept,
        "trend_r_squared": (
            None if total_ss <= 0 else max(0.0, 1.0 - residual / total_ss)
        ),
        "series": series,
    }


def collect_convergence_claim(conn: sqlite3.Connection) -> dict[str, Any]:
    """Arm B (2026-09-06): the convergence claim lift, in shadow.

    Two halves.  The receipts are production's own: every basket decision
    row carries the enumeration's ``convergence_claim`` receipt -- how many
    windows the price gate judged were converging, how many the lift would
    have admitted -- read straight off ``auto_trade_decisions``.  The
    evidence is the measurement the arm rests on, recomputed on this book
    with the kernel's own state function and the same evidence loader
    production hands in: at sampled P0-M/P1 checkpoints of settled
    station-days, the max-mass three-rung window (a stand-in for the seat,
    not the seat rule) is classified converging or not and scored against
    truth.  Nothing here re-derives the rule; the rule is called.
    """

    from bayes_basket_target.basket_kernel import delta_v2 as K
    from bayes_basket_target.convergence_evidence import (
        best_asks,
        load_convergence_evidence,
        window_sum,
    )

    out: dict[str, Any] = {
        "mode": K.CONVERGENCE_CLAIM_MODE,
        "factor": K.CONVERGENCE_CLAIM_FACTOR,
        "horizon_hours": K.CONVERGENCE_CLAIM_HORIZON_HOURS,
        "stages": sorted(K.CONVERGENCE_CLAIM_STAGES),
        "min_agreement": K.CONVERGENCE_CLAIM_MIN_AGREEMENT,
        "min_claim_rise": K.CONVERGENCE_CLAIM_MIN_CLAIM_RISE,
        "min_price_fall": K.CONVERGENCE_CLAIM_MIN_PRICE_FALL,
        "receipts": [],
        "examples": [],
        "evidence": [],
        "evidence_days": 14,
    }

    # --- receipts: production's decisions over the last seven days ---
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    tally: dict[str, dict[str, int]] = {}
    examples: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT station_id, event_date, created_at, details_json"
            " FROM auto_trade_decisions"
            " WHERE strategy_lane='basket' AND created_at>=?"
            " ORDER BY created_at DESC LIMIT 6000",
            (since,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for station_id, event_date, created_at, details_json in rows:
        try:
            details = json.loads(details_json or "{}")
        except (TypeError, ValueError):
            continue
        receipt = details.get("convergence_claim")
        if not isinstance(receipt, Mapping) or not receipt:
            continue
        signature = details.get("decision_signature") or {}
        stage = str(signature.get("stage") or "?")
        slot = tally.setdefault(stage, {
            "decisions": 0, "with_evidence": 0, "candidates": 0,
            "would_admit": 0, "admitted_by_lift": 0,
        })
        slot["decisions"] += 1
        slot["with_evidence"] += int(bool(receipt.get("evidence_present")))
        slot["candidates"] += int(receipt.get("candidate_count") or 0)
        slot["would_admit"] += int(receipt.get("would_admit_count") or 0)
        slot["admitted_by_lift"] += int(receipt.get("admitted_by_lift_count") or 0)
        if len(examples) < 12:
            for row in receipt.get("candidates") or []:
                if len(examples) >= 12:
                    break
                examples.append({
                    "station_id": str(station_id), "event_date": str(event_date),
                    "stage": stage, "at": str(created_at)[:16],
                    "buckets": list(row.get("buckets") or []),
                    "claim": row.get("claim"), "price": row.get("all_in_price"),
                    "agreement": row.get("agreement"),
                    "claim_rise": row.get("claim_rise"),
                    "price_fall": row.get("price_fall"),
                    "lifted_edge": row.get("lifted_edge"),
                    "flips_gate": bool(row.get("flips_gate")),
                })
    out["receipts"] = [{"stage": stage, **slot} for stage, slot in sorted(tally.items())]
    out["examples"] = examples

    # --- evidence: settled station-days, sampled P0-M/P1 checkpoints ---
    try:
        truth: dict[tuple[str, str], float] = {}
        floor_day = (datetime.now(timezone.utc) - timedelta(days=out["evidence_days"])).date().isoformat()
        for st, ld, t in conn.execute(
            "SELECT station_id, local_date, official_tmax_c"
            " FROM calibration_station_daily_high_truth"
            " WHERE official_tmax_c IS NOT NULL AND local_date>=?"
            " ORDER BY revision_no",
            (floor_day,),
        ):
            truth[(str(st), str(ld))] = float(t)
    except sqlite3.Error:
        truth = {}
    cells: dict[str, dict[str, Any]] = {}

    def _cell(name: str) -> dict[str, Any]:
        return cells.setdefault(name, {"n": 0, "hits": 0, "claim": 0.0, "unit": 0.0, "stations": set()})

    for (st, ed), t_true in sorted(truth.items()):
        try:
            cps = conn.execute(
                "SELECT history_id, stage, decision_at_utc"
                " FROM basket_probability_bridge_history"
                " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                "   AND stage IN ('P0-M','P1') ORDER BY decision_at_utc",
                (st, ed),
            ).fetchall()
        except sqlite3.Error:
            break
        if not cps:
            continue
        last_slot = None
        for history_id, stage, decided in cps:
            slot = str(decided)[:13]  # one sample per station-day-hour
            hour = int(str(decided)[11:13] or 0)
            if slot == last_slot or hour % 4:
                continue
            last_slot = slot
            try:
                buckets = sorted(conn.execute(
                    "SELECT ordinal, bucket, lower_c, upper_c, probability"
                    " FROM basket_probability_bridge_bucket_probability WHERE history_id=?",
                    (history_id,),
                ).fetchall())
                book = conn.execute(
                    "SELECT buckets_json FROM basket_official_ws_snapshot"
                    " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                    "   AND observed_at>=? ORDER BY observed_at ASC LIMIT 1",
                    (st, ed, str(decided)),
                ).fetchone()
            except sqlite3.Error:
                break
            if len(buckets) < 3 or book is None:
                continue
            best, best_i = -1.0, 0
            for i in range(len(buckets) - 2):
                mass = sum(float(b[4]) for b in buckets[i:i + 3])
                if mass > best:
                    best, best_i = mass, i
            run = buckets[best_i:best_i + 3]
            names = [str(b[1]) for b in run]
            asks = best_asks(book[0])
            price_now = window_sum(asks, names)
            if price_now is None or best <= 0:
                continue
            evidence = load_convergence_evidence(
                conn, station_id=st, event_date=ed, as_of_utc=str(decided),
            )
            state = K.convergence_state_v1(
                stage=str(stage), claim_now=best,
                claim_prev=window_sum((evidence or {}).get("prior_probabilities"), names),
                price_now=price_now,
                price_prev=window_sum((evidence or {}).get("prior_asks"), names),
                agreement_now=price_now / best,
            )
            lo, hi = run[0][2], run[-1][3]
            hit = int((lo is None or t_true >= lo) and (hi is None or t_true < hi))
            if state["converging"]:
                name = "双向收敛（会被抬价）"
            elif state["reason"] == "claim_not_rising" and (state.get("price_fall") or 0) >= K.CONVERGENCE_CLAIM_MIN_PRICE_FALL:
                name = "模型没动、市场跌（不抬）"
            elif state["reason"] == "no_prior":
                name = "无先验"
            elif state["reason"] == "not_dear":
                name = "不贵（a<1.1）"
            else:
                name = "贵但未收敛"
            cell = _cell(f"{stage} / {name}")
            cell["n"] += 1
            cell["hits"] += hit
            cell["claim"] += best
            cell["unit"] += (1.0 / price_now - 1.0) if hit else -1.0
            cell["stations"].add(st)
    out["evidence"] = [
        {
            "cell": name, "n": c["n"], "stations": len(c["stations"]),
            "hit_pct": round(100.0 * c["hits"] / c["n"], 1) if c["n"] else None,
            "claimed_pct": round(100.0 * c["claim"] / c["n"], 1) if c["n"] else None,
            "realised_over_claimed": round(c["hits"] / c["claim"], 2) if c["claim"] else None,
            "unit_return_pct": round(100.0 * c["unit"] / c["n"], 1) if c["n"] else None,
        }
        for name, c in sorted(cells.items())
    ]
    return out


def collect_effective_edge_lambda(conn: sqlite3.Connection) -> dict[str, Any]:
    """What fraction of a claim arrives, per stage and per agreement band.

    Charter 68 has the objective read ``lambda(a) * p``, where a = price /
    claim is how far the book agrees with a basket.  P0 and P0-M have carried
    a fitted table since 2026-09-01; P1 got one of its own on 2026-09-02, and
    the point of showing them side by side is that they are opposite shapes.
    P0's is an inverted U -- where the market disagrees most, five sixths of
    the claim never arrives -- and P1's is monotone at or above one, because
    P1's edge is the observation rather than the shape and the observation is
    right exactly where the market doubts it.

    Three columns per band and they answer different questions.  ``served`` is
    the constant the compile chain is running right now.  ``raw`` is what this
    book says that constant should be, recomputed here on the same rule the
    store's rolling fit uses, so a gap between the two IS the promotion
    backlog.  The early/late split is the only thing that decides whether a
    gap may be closed: a band whose two halves disagree is a band that has not
    earned a constant yet, and the 0.70-0.85 P1 band -- 0.54 early, 0.92 late
    -- is exactly that case.
    """

    from bayes_basket_target import effective_edge_store as _store
    from bayes_basket_target.basket_kernel.delta_v2 import (
        EFFECTIVE_EDGE_LAMBDA,
        EFFECTIVE_EDGE_P0M_LAMBDA,
        EFFECTIVE_EDGE_P0M_STAGES,
        EFFECTIVE_EDGE_P1_LAMBDA,
        EFFECTIVE_EDGE_P1_STAGES,
        EFFECTIVE_EDGE_STAGES,
    )
    from bayes_basket_target.delta_v2_runtime import EFFECTIVE_EDGE_MODE
    from bayes_basket_target.basket_kernel.delta_v2 import (
        CLAIM_EDGE_THERMOMETER_MODE,
        CLAIM_EDGE_THIN_BAND_LAMBDA,
        CLAIM_EDGE_THIN_BAND_MAX,
        EFFECTIVE_EDGE_BAND_BOUNDS,
    )

    def band_label(bound: float, lower: float) -> str:
        # Charter 96 moved the top boundary twice; a hardcoded "≥0.85" here
        # went on printing the retired partition after the split.  Derive it.
        return f"≥{lower:g}" if bound == float("inf") else f"<{bound:g}"

    def admits(lower: float, upper: float, lam: float) -> list[float] | None:
        """What this band can actually buy.

        Charter 96: the gate reduces to ``lambda(a) > a``, so a band admits
        exactly ``[its lower bound, its own lambda)`` -- its served value is
        its own cut point, and a band whose lambda falls under its floor buys
        nothing at all no matter how right the model is.  Showing the served
        number without this interval is what let two bands sit blind on the
        page for days looking like ordinary rows.
        """

        if lam <= lower:
            return None
        return [round(lower, 4), round(min(lam, upper), 4)]

    def measure(stages: tuple[str, ...], served, since: str | None,
                until: str | None) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in stages)
        clause = ""
        params: list[Any] = list(stages)
        if since:
            clause += " AND c.event_date >= ?"
            params.append(since)
        if until:
            clause += " AND c.event_date < ?"
            params.append(until)
        try:
            rows = conn.execute(
                # Every candidate window, which is the population the
                # served table is fitted on -- a panel that reads a
                # different sample than the fit is a panel that cannot be
                # used to check the fit.  ``win_probability`` here is
                # already the lifted claim; nothing lifts it again.
                #
                # P&L and stake come from the seats and only from the
                # seats: money follows the basket that was held, and a
                # window nobody bought has none.  The two halves of this
                # row answer two different questions on purpose, and the
                # money half is NULL for most rows.
                #
                # Joined per station-day-and-stage, not per station-day.  A
                # chain seats at P0 and is topped up at P0-M and P1, and a
                # station-day join handed the same settlement to each of
                # those seated candidates: 53 of them, carrying the chain's
                # whole P&L and stake once per stage.  That put 548 USDC of
                # stake and -373 of P&L on this panel against a ledger
                # holding 414 and -257, and moved the band returns with it
                # (measured 2026-09-08).  The settlement names the stage
                # that closed the chain; that seat, and only it, is where
                # the chain's money belongs -- the same one-book rule the
                # rest of the page reads.
                "SELECT c.win_probability, c.all_in_price, c.hit,"
                " s.pnl_usdc, s.stake_usdc"
                " FROM paper_trade_candidate_window c"
                " LEFT JOIN paper_trade_settlement s"
                "   ON s.station_id = c.station_id"
                "  AND s.event_date = c.event_date"
                "  AND s.stage = c.stage"
                "  AND c.seated = 1"
                f" WHERE c.stage IN ({placeholders}){clause}"
                "   AND c.hit IS NOT NULL",
                params,
            ).fetchall()
        except sqlite3.Error:
            return []
        tally = [
            {"seats": 0, "claim": 0.0, "hits": 0, "pnl": 0.0, "stake": 0.0}
            for _ in served
        ]
        for claim, cost, hit, pnl, stake in rows:
            if claim is None or cost is None or hit is None:
                continue
            claimed = float(claim or 0.0)
            price = float(cost or 0.0)
            if claimed <= 0.0 or price <= 0.0:
                continue
            index = _store._band_index(price / claimed, served)
            slot = tally[index]
            slot["seats"] += 1
            slot["claim"] += claimed
            slot["hits"] += int(hit or 0)
            slot["pnl"] += float(pnl or 0.0)
            slot["stake"] += float(stake or 0.0)
        out = []
        lower_bound = 0.0
        for (bound, lam), slot in zip(served, tally):
            raw = (slot["hits"] / slot["claim"]) if slot["claim"] > 0 else None
            out.append({
                "band": band_label(bound, lower_bound),
                "lower": round(lower_bound, 4),
                "upper": None if bound == float("inf") else round(bound, 4),
                "admits": admits(lower_bound, float(bound), float(lam)),
                "served": round(float(lam), 4),
                "seats": slot["seats"],
                "claim_mass": round(slot["claim"], 2),
                "hits": slot["hits"],
                "raw_lambda": None if raw is None else round(raw, 4),
                "return": (
                    None if slot["stake"] <= 0
                    else round(slot["pnl"] / slot["stake"], 4)
                ),
                "admissible": (
                    slot["seats"] >= _store.MIN_SEATS_PER_BAND
                    and slot["claim"] >= _store.MIN_CLAIM_MASS
                ),
            })
            if bound != float("inf"):
                lower_bound = float(bound)
        return out

    def activated(scope: str) -> dict[str, Any] | None:
        try:
            row = conn.execute(
                "SELECT lambda_table_sha256, fitted_at_utc, seat_count,"
                " evidence_json FROM calibration_effective_edge_lambda"
                " WHERE activated=1 AND stage_scope=?"
                " ORDER BY fitted_at_utc DESC LIMIT 1",
                (scope,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            evidence = json.loads(str(row[3]))
        except ValueError:
            evidence = []
        # Two shapes live in this column.  The store wrapped the band list
        # in {"bands": [...], "activation_guard": {...}} when the guard was
        # added; rows written before that are still the bare list, and P0's
        # activated row is one of them.  Iterating the wrapper yielded its
        # keys -- strings -- and the render died on
        # ``'str' object has no attribute 'get'`` after 516 seconds of work,
        # every render, producing nothing.  Read whichever shape is there.
        if isinstance(evidence, Mapping):
            evidence = evidence.get("bands") or []
        if not isinstance(evidence, list):
            evidence = []
        bands = [item for item in evidence if isinstance(item, Mapping)]
        return {
            "sha256": str(row[0])[:12],
            "fitted_at_utc": str(row[1]),
            "seat_count": int(row[2] or 0),
            "blind_bands": [
                item.get("band_upper_bound") for item in bands
                if item.get("accumulating") and not item.get("seats")
            ],
        }

    days = [
        str(row[0]) for row in conn.execute(
            "SELECT DISTINCT event_date FROM paper_trade_settlement"
            " ORDER BY event_date"
        )
    ]
    midpoint = days[len(days) // 2] if days else None

    def serving(scope: str, bootstrap) -> tuple[tuple[float, float], ...]:
        """The table production is actually serving for this scope.

        Charter 68(c) (2026-09-05) made the rolling fit promote itself, so the
        kernel constant became the bootstrap and nothing more -- but this
        section went on reading the constant and calling the column "the
        constant the compile chain is running right now".  It was wrong for a
        day: on 2026-09-06 the page printed P0's top band as 0.82 while the
        store served 1.0112, and the split at 0.80 that opened a band did not
        appear at all.  Read what the dispatch reads, and fall back to the
        constant on exactly its own condition -- no activation yet.
        """

        active = _store.load_active_lambda(conn, stage_scope=scope)
        return tuple(active) if active else tuple(bootstrap)

    scopes = []
    for scope, stages, served, ceiling in (
        ("P0", tuple(sorted(EFFECTIVE_EDGE_STAGES)),
         serving("P0", EFFECTIVE_EDGE_LAMBDA), _store.LAMBDA_CEILING),
        ("P0-M", tuple(sorted(EFFECTIVE_EDGE_P0M_STAGES)),
         serving("P0-M", EFFECTIVE_EDGE_P0M_LAMBDA), _store.LAMBDA_CEILING),
        ("P1", tuple(sorted(EFFECTIVE_EDGE_P1_STAGES)),
         serving("P1", EFFECTIVE_EDGE_P1_LAMBDA), _store.P1_LAMBDA_CEILING),
    ):
        whole = measure(stages, served, None, None)
        early = measure(stages, served, None, midpoint)
        late = measure(stages, served, midpoint, None)
        for index, band in enumerate(whole):
            band["early_lambda"] = (
                early[index]["raw_lambda"] if index < len(early) else None
            )
            band["late_lambda"] = (
                late[index]["raw_lambda"] if index < len(late) else None
            )
            band["early_seats"] = (
                early[index]["seats"] if index < len(early) else 0
            )
            # A band may be promoted when this book says something different
            # from the constant, the evidence clears the store's own hurdle,
            # and both halves of the window agree.  Anything less is a number
            # waiting for its second half.
            capped = None if band["raw_lambda"] is None else min(
                float(ceiling), band["raw_lambda"]
            )
            band["fitted_capped"] = (
                None if capped is None else round(capped, 4)
            )
            stable = (
                band["early_lambda"] is not None
                and band["late_lambda"] is not None
                and band["early_seats"] >= _store.MIN_SEATS_PER_BAND
                and abs(band["early_lambda"] - band["late_lambda"]) <= 0.25
            )
            band["candidate"] = bool(
                capped is not None
                and band["admissible"]
                and abs(capped - band["served"]) > 0.02
            )
            band["stable"] = bool(stable)
        scopes.append({
            "scope": scope,
            "stages": list(stages),
            "ceiling": float(ceiling),
            "bands": whole,
            "store": activated(scope),
            # Shadow axis (2026-09-06): the same bands split by which way the
            # market moved the window in the hours before the seat.  Read
            # from the shadow journal the daily refit writes; served by nothing.
            "dq_shadow": _store.load_dq_shadow(conn, stage_scope=scope),
        })
    return {
        "mode": EFFECTIVE_EDGE_MODE,
        # Charter 96/97: the partition is a rule in the kernel, and the
        # claim-edge axis is a second one.  Both belong on the page, since a
        # reader cannot otherwise tell why a band with a healthy lambda still
        # buys nothing, or why a thin-edge window was bought at all.
        "band_bounds": [
            None if b == float("inf") else float(b)
            for b in EFFECTIVE_EDGE_BAND_BOUNDS
        ],
        "claim_edge_thermometer": CLAIM_EDGE_THERMOMETER_MODE,
        "claim_edge_thin_band_max": CLAIM_EDGE_THIN_BAND_MAX,
        "claim_edge_thin_band_lambda": CLAIM_EDGE_THIN_BAND_LAMBDA,
        "dq_shadow_mode": _store.DQ_SHADOW_MODE,
        "dq_shadow_horizon_hours": _store.DQ_SHADOW_HORIZON_HOURS,
        "dq_shadow_threshold": _store.DQ_SHADOW_THRESHOLD,
        "minimum_seats_per_band": _store.MIN_SEATS_PER_BAND,
        "split_at_event_date": midpoint,
        "scopes": scopes,
    }


def collect_lambda_axis_usage(conn: sqlite3.Connection) -> dict[str, Any]:
    """Which axis bought each seat, so charter 97 can be retired on evidence.

    Charter 97 serves the claim-edge thermometer as lambda's second axis and
    sets its own retirement condition: once the seats that axis opened clear
    charter 68's hurdle, retire it if their realisation falls under one.
    Nothing could evaluate that without knowing which axis admitted a window,
    which is why the gate now stamps ``effective_edge_lambda_source`` on every
    priced row.  This counts them.

    Deliberately cheap and deliberately honest about being young: the field
    was added 2026-09-06 and rows written before that carry none, so the
    reading starts empty and fills as cycles run.  An empty panel here means
    "not measured yet", never "the axis bought nothing".
    """

    since = (
        date.today() - timedelta(days=3)
    ).isoformat()
    seated: dict[str, int] = {}
    considered: dict[str, int] = {}
    scanned = 0
    try:
        rows = conn.execute(
            "SELECT details_json FROM auto_trade_decisions"
            " WHERE created_at >= ?"
            "   AND details_json LIKE '%effective_edge_lambda_source%'"
            " ORDER BY created_at DESC LIMIT 400",
            (since,),
        ).fetchall()
    except sqlite3.Error:
        return {"available": False, "since": since}

    def walk(node: Any) -> "Iterator[dict[str, Any]]":
        if isinstance(node, dict):
            if "basket_support_selection" in node:
                yield node["basket_support_selection"]
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    for (payload,) in rows:
        try:
            detail = json.loads(payload or "{}")
        except ValueError:
            continue
        scanned += 1
        for selection in walk(detail):
            seat = selection.get("seated")
            if isinstance(seat, dict) and seat.get("effective_edge_lambda_source"):
                key = str(seat["effective_edge_lambda_source"])
                seated[key] = seated.get(key, 0) + 1
            for row in selection.get("considered") or []:
                if isinstance(row, dict) and row.get(
                    "effective_edge_lambda_source"
                ):
                    key = str(row["effective_edge_lambda_source"])
                    considered[key] = considered.get(key, 0) + 1
    return {
        "available": True,
        "since": since,
        "scanned_decisions": scanned,
        "seated_by_source": seated,
        "considered_by_source": considered,
    }


def collect_edge_band_health(conn: sqlite3.Connection) -> dict[str, Any]:
    """The claims layer's thermometer: edge bands versus what they realise.

    Edge = claimed probability minus the all-in ask at posting, leg by leg.
    The 2026-08-26 execution battle measured the shape once: realisation
    over claim collapses monotonically with edge (0.94x at <0 down to
    0.06x at >=30%), fills rise with edge (the market sells exactly the
    overclaims), and the unfilled side realises 1.36x its claims.  This is
    deliberately a monitor, not a gate -- the gate version died its
    excision test (A1) -- so what this section is FOR is watching the
    charter 62 calibration repair land: the >=10% bands should shrink and
    realisation ratios should climb toward 1.0 as the centre bias closes.
    Split at each leg's own posting day, most recent seven days against
    everything before.
    """

    number = re.compile(r"-?\d+(?:\.\d+)?")

    def bucket_value(label: str) -> float | None:
        found = number.findall(label or "")
        return float(found[0]) if found else None

    bands = ("<0", "0-2%", "2-5%", "5-10%", "10-20%", "20-30%", ">=30%")

    def band_of(edge: float) -> str:
        if edge < 0:
            return "<0"
        for top, name in (
            (0.02, "0-2%"), (0.05, "2-5%"), (0.10, "5-10%"),
            (0.20, "10-20%"), (0.30, "20-30%"),
        ):
            if edge < top:
                return name
        return ">=30%"

    settled = {
        (str(row[0]), str(row[1])): str(row[2] or "")
        for row in conn.execute(
            "SELECT station_id, event_date, settled_bucket"
            " FROM paper_trade_settlement"
        )
    }
    latest_day = ""
    legs: list[dict[str, Any]] = []
    # Every leg that was POSTED (``intended_legs_json``), with ``filled``
    # read off ``leg_shares_json`` -- what came back.  Two earlier versions
    # of this loop were each right for the execution family in force and
    # wrong for the next one.  It first read ``resting_fill_json``, which
    # charter 65's taker law left empty on every row, so the section printed
    # "无腿级数据" while the book traded daily (2026-08-29).  It was then
    # moved to ``leg_shares_json`` with ``filled`` hard-coded true, which is
    # exactly right for crossing -- a FOK leg fills or is refused -- and
    # became a lie the day charter 94 made the order a resting ladder: an
    # unfilled leg simply vanished from the sample, so every band reported a
    # 100% fill rate and a realisation computed only over the legs the
    # market agreed to sell.  The filled/unfilled split is the sharpest
    # reading this panel has and it is the one the resting lane produces, so
    # the loop now takes the posted leg as the unit and lets the fill be an
    # observation about it rather than a precondition for seeing it.
    for row in conn.execute(
        "SELECT station_id, event_date, probabilities_json, prices_json,"
        " leg_shares_json, stage, intended_legs_json"
        " FROM paper_trade_position"
    ):
        try:
            probs = json.loads(row[2] or "{}")
            prices = json.loads(row[3] or "{}")
            held = json.loads(row[4] or "{}")
            posted = json.loads(row[6] or "{}")
        except ValueError:
            continue
        key = (str(row[0]), str(row[1]))
        truth = settled.get(key)
        latest_day = max(latest_day, str(row[1]))
        # A row written before the ledger recorded its posted legs can still
        # be read for the legs it holds; it just cannot say what it asked
        # for, so it contributes no unfilled side.
        for bucket in (posted or held):
            claim = probs.get(bucket)
            ask = prices.get(bucket)
            if claim is None or ask is None:
                continue
            try:
                bought = float(held.get(bucket, 0.0) or 0.0) > 0.0
            except (TypeError, ValueError):
                continue
            legs.append({
                "day": str(row[1]),
                "stage": str(row[5]),
                "band": band_of(float(claim) - float(ask)),
                "claim": float(claim),
                "filled": bought,
                "win": (
                    None if truth is None
                    else (1 if str(bucket) == truth else 0)
                ),
            })
    if not legs:
        return {"bands": [], "recent_floor": "", "totals": {}, "stages": []}
    recent_floor = (
        date.fromisoformat(latest_day) - timedelta(days=6)
    ).isoformat()

    def cell(rows: list[dict[str, Any]]) -> dict[str, Any]:
        settled_rows = [leg for leg in rows if leg["win"] is not None]
        filled_rows = [leg for leg in settled_rows if leg["filled"]]
        unfilled_rows = [leg for leg in settled_rows if not leg["filled"]]

        def ratio(group: list[dict[str, Any]]) -> float | None:
            if len(group) < 10:
                return None
            claimed = sum(leg["claim"] for leg in group) / len(group)
            won = sum(leg["win"] for leg in group) / len(group)
            return round(won / claimed, 2) if claimed > 0 else None

        return {
            "n": len(rows),
            "fill_rate": (
                round(
                    sum(1 for leg in rows if leg["filled"]) / len(rows), 3
                ) if rows else None
            ),
            "claimed_mean": (
                round(sum(leg["claim"] for leg in rows) / len(rows), 3)
                if rows else None
            ),
            "realize_filled_x": ratio(filled_rows),
            "realize_unfilled_x": ratio(unfilled_rows),
            "n_settled": len(settled_rows),
        }

    # Split by stage, because a claim is not the same claim at every stage.
    #
    # P1 re-solves as the day's observations arrive, so a P1 leg claiming a
    # 30% edge is claiming it with hours of METAR behind it while a P0 leg
    # claiming the same has only the model.  Pooling them reported >=30% as
    # realising 0.25x, which was 0.07x for P0 and 0.35x for P1 blended --
    # a number true of neither lane (measured 2026-09-02, user ruling).
    # It matters most here of all: this section exists to watch the charter
    # 62 calibration repair land, and if the stages repair at different
    # rates the pooled column is exactly what hides it.
    #
    # P0-M is kept as a column rather than dropped: it carries three to
    # twelve legs a band, so ``cell``'s own ten-leg floor blanks nearly
    # every ratio, which is the honest rendering of a lane too thin to read.
    stage_names = [
        name for name in ("P0", "P0-M", "P1")
        if any(leg["stage"] == name for leg in legs)
    ]
    band_rows = []
    for name in bands:
        in_band = [leg for leg in legs if leg["band"] == name]
        band_rows.append({
            "band": name,
            "all": cell(in_band),
            "recent": cell(
                [leg for leg in in_band if leg["day"] >= recent_floor]
            ),
            "by_stage": {
                stage: cell(
                    [leg for leg in in_band if leg["stage"] == stage]
                )
                for stage in stage_names
            },
        })
    total_recent = sum(1 for leg in legs if leg["day"] >= recent_floor)
    for entry in band_rows:
        entry["share_all"] = round(entry["all"]["n"] / len(legs), 3)
        entry["share_recent"] = (
            round(entry["recent"]["n"] / total_recent, 3)
            if total_recent else None
        )
    return {
        "bands": band_rows,
        "recent_floor": recent_floor,
        "stages": stage_names,
        "stage_totals": {
            stage: sum(1 for leg in legs if leg["stage"] == stage)
            for stage in stage_names
        },
        "totals": {"legs": len(legs), "recent_legs": total_recent},
    }


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
        # Stage precedence, not clock order: P0 recomputes all day but its
        # chain carries no observation, so at night its rows are forecasts
        # written after the answer was measured.  Once a P1 row exists for
        # the station-day, later P0/P0-M rows are not the end-state belief.
        p1_seen = False
        # Read p(B) from the bridge's own projection rather than rebuilding
        # each envelope body (2026-08-29).  The projection table is what
        # this module's docstring calls "a read-optimised projection" and
        # it is written from the envelope inside the same savepoint, so it
        # is the same numbers by construction -- checked value by value
        # over 600 rows sampled across the whole history, every
        # probability and ordinal identical, no row missing its
        # projection.  Rebuilding a body costs a JSON parse of the full
        # lineage plus a content hash, per decision, and this walk reads
        # about a hundred thousand of them.
        for row in conn.execute(
            "SELECT h.decision_at_utc, h.stage, h.history_id"
            " FROM basket_probability_bridge_history h"
            " WHERE h.station_id=? AND h.event_date=? AND h.state='COMMITTED'"
            " AND h.decision_at_utc>=? ORDER BY h.decision_at_utc",
            (station, day, entry["opened"]),
        ):
            if str(row[1]) == "P1":
                p1_seen = True
            elif p1_seen:
                continue
            projection = conn.execute(
                "SELECT bucket, ordinal, probability"
                " FROM basket_probability_bridge_bucket_probability"
                " WHERE history_id=?",
                (str(row[2]),),
            ).fetchall()
            if not projection:
                continue
            probabilities = {
                str(bucket): float(value)
                for bucket, _ordinal, value in projection
            }
            ordinals = {
                str(bucket): int(ordinal)
                for bucket, ordinal, _value in projection
            }
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
    # The connection lives exactly one payload and is closed on every path.
    # It used to be leaked, one per page refresh, and a leaked read-only
    # connection is not harmless on a WAL database: any cursor left
    # unexhausted keeps a read transaction open, checkpointing can never
    # advance past the oldest reader snapshot, and the WAL grew to 417MB --
    # at which point every writer's transaction slowed enough that ordinary
    # lock contention compounded into 45-minute P1 outages (2026-08-24).
    conn = _connect(db)
    started = time.perf_counter()
    try:
        payload = _build_payload(conn, db)
    finally:
        conn.close()
    # The render's own cost, on the page.  Reported because the page is a
    # file on a thirty-minute timer, not a live query: a reader who
    # refreshes right after a change sees the PREVIOUS build until the next
    # one lands, and without these two numbers there is no way to tell that
    # from a broken section.  2026-09-09 is the case -- P1's band table
    # moved at 03:23Z, the page on screen was built at 03:07Z, and the
    # build that would have shown the move was twenty-four minutes in.
    elapsed = time.perf_counter() - started
    slowest = sorted(
        (
            {
                "section": name,
                "build_sec": float(entry["build_sec"]),
                "cached": bool(entry.get("cached")),
            }
            for name, entry in _SECTION_CACHE_LOG.items()
            if entry.get("build_sec") is not None
        ),
        key=lambda row: -row["build_sec"],
    )
    payload["render"] = {
        "generated_at_utc": payload.get("generated_at_utc"),
        "build_sec": round(elapsed, 1),
        # What was actually computed this round.  A cached section that was
        # reused contributes nothing here, which is the point: the sum is
        # what THIS build paid, not what a cold build would.
        "computed_sec": round(sum(r["build_sec"] for r in slowest), 1),
        "computed_sections": len(slowest),
        "reused_sections": sum(
            1 for e in _SECTION_CACHE_LOG.values() if e.get("reused")
        ),
        "slowest": slowest[:12],
    }
    return payload


REGIONAL_SWEEP_JSON = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "regional_hires_historical_oos.json"
)
# The four regional point systems and how each one's archive behaves.
# ``backfillable`` decides how a stale lane reads: the NOAA buckets and
# the RISH mirror keep history, so a stale lane there is a delay; the DWD
# directory keeps only the newest run of each cycle, so a stale lane
# there is runs permanently lost.
REGIONAL_LANES = {
    "JMA_MSM_POINT": {
        "label": "JMA MSM 5km", "region": "东亚",
        "transport": "RISH 京都镜像·字节区间", "backfillable": True},
    "NCEP_HRRR_POINT": {
        "label": "HRRR 3km", "region": "美国",
        "transport": "NOAA S3·idx 区间", "backfillable": True},
    "NCEP_NBM_POINT": {
        "label": "NBM 混合", "region": "美国",
        "transport": "NOAA S3·idx 区间", "backfillable": True},
    "DWD_ICON_EU_POINT": {
        "label": "ICON-EU 7km", "region": "欧洲",
        "transport": "DWD opendata·全域场", "backfillable": False},
}
_SWEEP_MODEL_TO_SYSTEM = {
    "jma_msm": "JMA_MSM_POINT",
    "ncep_hrrr_conus": "NCEP_HRRR_POINT",
    "ncep_nbm_conus": "NCEP_NBM_POINT",
    "dwd_icon_eu": "DWD_ICON_EU_POINT",
}


def _capture_rosters() -> dict[str, set[str]]:
    """Which stations each lane is RULED IN at -- read from the lanes.

    Not inferred from which rows exist.  A station whose lane started an
    hour ago, or whose turn the per-pass cap has not reached, has no rows
    and is not a reversal; reading the roster off the corpus would print
    "measured reversed" over stations that were admitted.  The rosters
    live in the archive modules for exactly this second reader.
    """

    from bayes_basket_target import noaa_conus_archive
    from bayes_basket_target import dwd_icon_eu_point_archive
    from bayes_basket_target import jma_msm_archive  # noqa: F401

    rosters = {
        "NCEP_HRRR_POINT": {
            s[0] for s in noaa_conus_archive.CAPTURE_STATIONS["HRRR"]
        },
        "NCEP_NBM_POINT": {
            s[0] for s in noaa_conus_archive.CAPTURE_STATIONS["NBM"]
        },
        "DWD_ICON_EU_POINT": {
            s[0] for s in dwd_icon_eu_point_archive.CAPTURE_STATIONS
        },
        "JMA_MSM_POINT": {
            s[0] for s in getattr(
                jma_msm_archive, "CAPTURE_STATIONS", ()
            )
        },
    }
    return rosters


def collect_regional_lanes(conn: sqlite3.Connection) -> dict[str, Any]:
    """The regional capture lanes: corpus growth, race state, verdicts.

    Three questions in one section, because they are one story read at
    three moments: what the 46-pair sweep measured (the reason a lane
    exists), what each lane has captured so far (the corpus the gates
    will re-ask), and where each system stands in the OOS race (what a
    win will buy -- envelope evidence, never a membership seat).
    """

    lanes = []
    rosters = _capture_rosters()
    captured_stations: dict[str, set[str]] = {}
    for system_id, meta in REGIONAL_LANES.items():
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT station_id),"
            " COUNT(DISTINCT station_id || '|' || target_local_date),"
            " MAX(archive_query_at_utc), MAX(native_run_init_utc)"
            " FROM calibration_native_ensemble_historical_member_run"
            " WHERE ensemble_system_id=?",
            (system_id,),
        ).fetchone()
        rows, stations, station_days, last_query, last_run = row
        captured_stations[system_id] = {
            str(r[0]) for r in conn.execute(
                "SELECT DISTINCT station_id"
                " FROM calibration_native_ensemble_historical_member_run"
                " WHERE ensemble_system_id=?",
                (system_id,),
            )
        }
        age = _age_hours(last_query)
        # A lane is judged by whether the hourly sweep is still landing
        # rows, on the clock of the product it captures (six-hourly runs,
        # a few hours of publication lag).  For the DWD lane a red is not
        # a delay but permanent loss, which the note says out loud.
        if rows == 0:
            health = "amber"
            note = "道已建，等第一批行落库"
        elif age is not None and age <= 26:
            health = "green"
            note = ""
        elif age is not None and age <= 50:
            health = "amber"
            note = "超过一天没有新行"
        else:
            health = "red"
            note = (
                "断采——DWD 不留历史，错过的 run 永久丢失"
                if not meta["backfillable"]
                else "断采——归档还在，可回填"
            )
        lanes.append({
            "system_id": system_id, **meta,
            "row_count": rows, "station_count": stations,
            # The roster's size beside the count that has rows: a lane
            # at 1/10 is either young or starved, and either way that is
            # the number to look at, not the row total.
            "roster_count": len(rosters.get(system_id, set())),
            "station_days": station_days,
            "last_capture_age_hours": age,
            "last_run_init": last_run,
            "health": health, "note": note,
        })

    # Race state, from the worker's own OOS ingress readiness -- the same
    # rows the admission gates read, not a reimplementation of them.
    readiness = []
    if STATUS_JSON.is_file():
        try:
            status = json.loads(STATUS_JSON.read_text())
            for entry in (
                status.get("production", {})
                .get("native_ensemble_oos_ingress", {})
                .get("readiness") or []
            ):
                if entry.get("ensemble_system_id") in REGIONAL_LANES:
                    readiness.append(entry)
        except (ValueError, OSError):
            pass

    # The sweep's verdicts, pinned: which pairs earned a lane and which
    # measured reversed.  Only the four systems that HAVE lanes -- the
    # rest of the 46 pairs stay in the evidence file.
    verdicts = []
    if REGIONAL_SWEEP_JSON.is_file():
        try:
            sweep = json.loads(REGIONAL_SWEEP_JSON.read_text())
            for candidate in sweep.get("candidates") or []:
                system_id = _SWEEP_MODEL_TO_SYSTEM.get(
                    str(candidate.get("model"))
                )
                if system_id is None:
                    continue
                station = str(candidate.get("station_id"))
                morning = candidate.get("morning_argmax") or {}
                verdicts.append({
                    "system_id": system_id,
                    "system_label": REGIONAL_LANES[system_id]["label"],
                    "station_id": station,
                    "passes_all_three": bool(
                        candidate.get("passes_all_three")
                    ),
                    "gates": candidate.get("gates") or {},
                    "holdout_days": candidate.get("holdout_days"),
                    "candidate_exact": (morning.get("candidate") or {})
                    .get("exact"),
                    "incumbent_exact": (morning.get("incumbent") or {})
                    .get("exact"),
                    "candidate_within_1": (morning.get("candidate") or {})
                    .get("within_1"),
                    # Two different questions, kept apart on purpose.
                    # ``in_roster`` is the ruling (is this pair captured
                    # at all); ``has_rows`` is progress (has its turn
                    # come yet).  Collapsing them prints "measured
                    # reversed" over stations that were admitted an hour
                    # ago and simply have no rows yet.
                    "in_roster": station in rosters.get(system_id, set()),
                    "has_rows": station in captured_stations[system_id],
                })
        except (ValueError, OSError):
            pass
    verdicts.sort(
        key=lambda v: (
            not v["in_roster"], not v["has_rows"],
            -(v["candidate_exact"] or 0.0),
        )
    )
    return {"lanes": lanes, "readiness": readiness, "verdicts": verdicts}


# ---------------------------------------------------------------------------
# Section cache
#
# A render rebuilds every section from scratch, and the hosted sweep starts
# one every thirty minutes; measured 2026-09-02 a full build took about
# fourteen minutes, most of it recomputing answers that had not moved since
# the previous build.  Sections whose inputs change on the order of hours
# keep their last value on disk and are recomputed only when it ages out.
#
# What may be cached is decided by two safety rules, not by cost:
#
#   * Nothing that feeds ``alerts`` / ``overall_health``.  The banner is the
#     one thing on this page a reader trusts to mean "now" -- production sat
#     BLOCKED_FAIL_CLOSED for eleven hours behind a healthy heartbeat on
#     2026-08-26 -- so members, chain, band stages and truth materialization
#     are rebuilt every render however long they take.
#   * Nothing about the live book.  Live readiness, open orders and the
#     whole selection payload are what the page is opened for mid-session.
#
# And every cached section carries the moment it was computed, which the
# footer prints, so a cache that quietly stops refreshing reads as stale
# rather than as current.
SECTION_CACHE_DIR = ROOT / "data" / "dashboard_section_cache"
# Bumped by hand when a helper a cached collector calls changes without the
# collector's own body changing -- the fingerprint below sees the collector
# and nothing it calls.  A bump discards every cached section.
SECTION_CACHE_EPOCH = 1
# Lifetimes, deliberately unequal.  Renders come round every thirty minutes;
# equal lifetimes would expire the whole set on one render and put the
# fourteen minutes back in a single go.
SECTION_CACHE_TTL_SEC = {
    "truth_landing": 150 * 60,
    "window_divergence": 165 * 60,
    "scorecard": 180 * 60,
    "h5_readiness": 195 * 60,
    "skill": 210 * 60,
    "h5_bias_accuracy": 225 * 60,
    "band_residual_audit": 240 * 60,
    "center_landing": 255 * 60,
    "convergence_claim": 270 * 60,
}
# Set by ``--no-section-cache`` so a reader who needs every number computed
# against this instant can have it, at the price of the long build.
SECTION_CACHE_ENABLED = True
_SECTION_CACHE_LOG: dict[str, dict[str, Any]] = {}


def _section_fingerprint(collector: Any) -> str:
    try:
        source = inspect.getsource(collector)
    except (OSError, TypeError):
        source = repr(collector)
    return hashlib.sha256(
        f"{SECTION_CACHE_EPOCH}\n{source}".encode("utf-8")
    ).hexdigest()


def _cached_section(name: str, collector: Any, *args: Any, **kwargs: Any) -> Any:
    """The collector's value, reused from disk while it is still young.

    A miss round-trips the fresh value through JSON before returning it, so
    a hit and a miss hand the rest of the build byte-identical structures --
    otherwise a tuple would reach the page as a tuple on the render that
    computed it and as a list on the five that reused it.
    """

    ttl = SECTION_CACHE_TTL_SEC.get(name)
    if not ttl or not SECTION_CACHE_ENABLED:
        # Still timed.  Only the nine sections with a TTL passed through
        # here, so the nine that were already the known-slow ones were the
        # only ones anybody could see the cost of -- and the render was 24
        # minutes on 2026-09-09 with no record of where the other minutes
        # went.  A section without a lifetime is not a section without a
        # price.
        started = time.perf_counter()
        value = collector(*args, **kwargs)
        _SECTION_CACHE_LOG[name] = {
            "build_sec": round(time.perf_counter() - started, 1),
            "reused": False,
            "cached": False,
        }
        return value
    fingerprint = _section_fingerprint(collector)
    path = SECTION_CACHE_DIR / f"{name}.json"
    now = _now()
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        stamp = str(blob["computed_at_utc"])
        age = (
            now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        ).total_seconds()
        if blob.get("fingerprint") == fingerprint and 0.0 <= age < ttl:
            _SECTION_CACHE_LOG[name] = {
                "computed_at_utc": stamp,
                "age_sec": round(age),
                "ttl_sec": ttl,
                "reused": True,
                "cached": True,
            }
            return blob["value"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    started = time.perf_counter()
    value = collector(*args, **kwargs)
    elapsed = time.perf_counter() - started
    stamp = now.isoformat().replace("+00:00", "Z")
    _SECTION_CACHE_LOG[name] = {
        "computed_at_utc": stamp,
        "age_sec": 0,
        "ttl_sec": ttl,
        "reused": False,
        "cached": True,
        "build_sec": round(elapsed, 1),
    }
    try:
        value = json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        # Not representable, so it cannot be cached either.  Serve it and
        # say so rather than pretending the section is cached.
        _SECTION_CACHE_LOG[name]["reused"] = False
        _SECTION_CACHE_LOG[name]["uncacheable"] = True
        return value
    try:
        SECTION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for stale in SECTION_CACHE_DIR.glob(f"{path.name}.*.tmp"):
            # A render killed mid-write leaves one of these; nothing reads
            # them, so they would accumulate silently forever.
            try:
                if stale.stat().st_mtime < time.time() - 6 * 3600:
                    stale.unlink()
            except OSError:
                pass
        # Unique per process: the hosted sweep and a hand-run render can
        # overlap (measured: two builds in flight at 04:10 on 2026-09-02),
        # and a shared temp name lets one replace the file the other is
        # still writing.  Distinct names make last-writer-wins the only
        # outcome, and both writers leave a complete file behind.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "schema": "dashboard_section_cache_v1",
                    "section": name,
                    "fingerprint": fingerprint,
                    "computed_at_utc": stamp,
                    "build_sec": round(elapsed, 1),
                    "value": value,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except (OSError, TypeError, ValueError):
        pass
    return value


def _build_payload(conn: sqlite3.Connection, db: Path) -> dict[str, Any]:
    _SECTION_CACHE_LOG.clear()
    cadence = _cached_section("run_cadence", collect_run_cadence, conn)
    members = _cached_section("members", collect_members, conn, cadence)
    tables = _cached_section(
        "correction_and_weights", collect_correction_and_weights, conn
    )
    stages = _cached_section("band_stages", collect_band_stages, conn)
    band_shadow = _cached_section(
        "band_shadow", collect_band_shadow, conn
    )
    chain = _cached_section("chain", collect_chain, conn)
    gaps = _cached_section(
        "gaps", collect_gaps, conn, members, stages, tables
    )
    p0m = _cached_section("p0m", collect_p0m_progress, conn)
    skill = _cached_section("skill", collect_model_skill, conn)
    truth = _cached_section(
        "truth_materialization", collect_truth_materialization, conn
    )
    scores = _cached_section("oos_scores", collect_oos_scores, conn)
    serving_weights = _cached_section(
        "serving_weights", collect_serving_weights, conn
    )
    center_width = _cached_section(
        "center_width", collect_center_width, conn
    )
    center_landing = _cached_section(
        "center_landing", collect_center_landing, conn
    )
    scorecard = _cached_section(
        "scorecard", collect_calibration_scorecard, conn
    )
    truth_landing = _cached_section(
        "truth_landing", collect_truth_landing, conn
    )
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
                **_production_health(status),
            }
        except (ValueError, OSError):
            worker = {}
    # The banner used to say "something below is red" and leave the reader
    # to scroll sixteen sections for the one red word.  Collect the
    # non-green rows from exactly the four sources the light is computed
    # from, so the banner can name them -- and derive the light from this
    # list, so the two can never disagree.
    alerts = (
        [
            {"health": m["health"], "label": f"模型 {m['label']}",
             "note": m.get("note") or ""}
            for m in members if m["health"] != "green"
        ]
        + [
            {"health": c["health"], "label": f"链路·{c['name']}",
             "note": c.get("note") or ""}
            for c in chain if c["health"] != "green"
        ]
        + [
            {"health": s["health"], "label": f"band 校准 {s['stage']}",
             "note": s.get("state") or ""}
            for s in stages if s["health"] != "green"
        ]
        + (
            [{"health": truth["health"], "label": "真值物化",
              "note": truth.get("note") or ""}]
            if truth["health"] != "green" else []
        )
        # The resident's own health.  It was absent from this list until
        # 2026-08-26, when production sat BLOCKED_FAIL_CLOSED for eleven
        # hours behind a healthy heartbeat: the page showed a live pid and
        # a fresh timestamp while nothing was being produced, and the
        # outage surfaced only because someone audited refusals by hand.
        # A heartbeat says the process is alive; it does not say the
        # pipeline is working.
        + (
            [{"health": worker["production_health"],
              "label": "生产周期",
              "note": worker.get("production_note") or ""}]
            if worker.get("production_health") not in (None, "green") else []
        )
    )
    overall = (
        "red" if any(a["health"] == "red" for a in alerts)
        else ("amber" if alerts else "green")
    )
    selection = _cached_section("selection", build_selection_payload, db)
    daily = _cached_section(
        "daily", _daily_table, selection,
        _cached_section("intended_cover", _intended_cover_by_day, conn),
    )
    # Aggregate first, then trim.  The rows ship as a hand-audit sample --
    # no section reads them -- and they arrive newest first, so trimming
    # before the daily table is built deletes whole event days off the
    # bottom of it.
    selection["rows"] = (selection.get("rows") or [])[:400]
    return {
        "schema": "calibration_dashboard_v2",
        "generated_at_utc": _now().isoformat().replace("+00:00", "Z"),
        "overall_health": overall,
        "alerts": alerts,
        "selection": selection,
        "daily": daily,
        "members": members,
        "tables": tables,
        "truth_landing": truth_landing,
        "band_residual_audit": (band_audit := _cached_section(
            "band_residual_audit", collect_band_residual_audit, conn,
        )),
        "live_readiness": _cached_section(
            "live_readiness", collect_live_readiness,
            conn, band_audit.get("stations") or [],
        ),
        "stages": stages,
        "band_shadow": band_shadow,
        "chain": chain,
        "gaps": gaps,
        "p0m": p0m,
        "skill": skill,
        "truth": truth,
        "scores": scores,
        "serving_weights": serving_weights,
        "center_width": center_width,
        "center_landing": center_landing,
        "scorecard": scorecard,
        "h5_readiness": _cached_section(
            "h5_readiness", collect_h5_readiness, conn
        ),
        "h5_bias_accuracy": _cached_section(
            "h5_bias_accuracy", collect_h5_bias_accuracy, conn
        ),
        "window_divergence": _cached_section(
            "window_divergence", collect_window_divergence, conn
        ),
        "edge_band_health": _cached_section(
            "edge_band_health", collect_edge_band_health, conn
        ),
        "effective_edge_lambda": _cached_section(
            "effective_edge_lambda", collect_effective_edge_lambda, conn
        ),
        "lambda_axis_usage": _cached_section(
            "lambda_axis_usage", collect_lambda_axis_usage, conn
        ),
        "convergence_claim": _cached_section(
            "convergence_claim", collect_convergence_claim, conn
        ),
        "risk_metrics": _cached_section(
            "risk_metrics", collect_risk_metrics, conn
        ),
        "regional": _cached_section(
            "regional", collect_regional_lanes, conn
        ),
        "worker": worker,
        "section_cache": dict(_SECTION_CACHE_LOG),
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>气温篮子全链看板</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--soft:rgba(38,43,54,.6);
--text:#e6e9ef;--muted:#9aa4b8;--green:#3fb950;--amber:#d29922;--red:#f85149;
--accent:#58a6ff;--up:#7ee787;--down:#e3b341;--violet:#d2a8ff;
--pill-green-bg:rgba(63,185,80,.14);--pill-green-fg:#7ee787;
--pill-amber-bg:rgba(210,153,34,.16);--pill-amber-fg:#e3b341;
--pill-red-bg:rgba(248,81,73,.15);--pill-red-fg:#ff7b72;
--pill-violet-bg:rgba(188,140,255,.16);
--why-bg:rgba(88,166,255,.07);--why-fg:#c9d4e5;--bar-bg:#232833;
--shadow:none;
/* Scrollbars, form controls and the ground the UA paints follow this, not
   the tokens above.  Omitting it left the board dark with light scrollbars
   wherever a host stylesheet had declared `color-scheme: light` -- which is
   what the Artifact shell does. */
color-scheme:dark}
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
--shadow:0 1px 2px rgba(27,31,36,.06);color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 72px}
h1{font-size:23px;margin:0 0 4px}
h2{font-size:16px;margin:22px 0 4px;display:flex;align-items:center;gap:8px}
.sub{color:var(--muted);font-size:13px;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);
border-radius:9px;padding:10px 12px;margin-bottom:8px;box-shadow:var(--shadow)}
/* The station cell's link to its own book.  Undecorated until hover so a
   table of forty-five stations does not read as forty-five links, and
   coloured from the accent token so it stays legible on both grounds
   rather than falling back to the browser's blue on a dark card. */
a.mkt{color:var(--accent);text-decoration:none}
a.mkt:hover,a.mkt:focus-visible{text-decoration:underline}
a.mkt:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.grid{display:grid;gap:8px}
.g4{grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
/* .g2 was written at two call sites and never defined here, so both fell
   back to bare `.grid` -- one column, every cell stacked, and the right
   two thirds of the card blank (user report, 2026-08-31).  Same auto-fit
   family as its siblings: the tracks that have no cell collapse and the
   ones that do share the whole row, so a three- or four-cell card fills
   the width instead of leaving a margin nothing lives in. */
.g2{grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
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
/* The live flag reads as a switch because it is one: a control with two
   states and no third.  The old button said "切换" next to a word saying
   OFF, which asks the reader to work out which of the two the button is
   about to do.  A switch shows the state and the action as the same
   object. */
.lc-sw{position:relative;width:34px;height:18px;padding:0;border-radius:99px;
border:1px solid var(--line);background:var(--line);cursor:pointer;
transition:background .13s ease;flex:none}
.lc-sw[aria-checked="true"]{background:var(--red);border-color:transparent}
.lc-sw::after{content:"";position:absolute;top:1px;left:1px;width:14px;
height:14px;border-radius:99px;background:#fff;
box-shadow:0 1px 2px rgba(0,0,0,.35);transition:transform .13s ease}
.lc-sw[aria-checked="true"]::after{transform:translateX(16px)}
.lc-sw:disabled{opacity:.4;cursor:not-allowed}
.lc-sw:focus-visible{outline:2px solid var(--text);outline-offset:2px}
/* The station gate, three switches per row.  A 34px slider each would be
   110px of chrome on a 45-row table, so these carry their state in the fill
   instead of a knob -- same contract as .lc-sw (the state and the action are
   one object), drawn small enough to live in a cell.  Off is an outline, on
   is the colour of what it means: red bars, green admits, amber waits. */
.lc-gate{font:inherit;font-size:11px;line-height:1;padding:3px 6px;margin-right:3px;
border-radius:99px;border:1px solid var(--line);background:transparent;
color:var(--muted);cursor:pointer;white-space:nowrap}
.lc-gate:hover:not(:disabled){border-color:var(--text);color:var(--text)}
.lc-gate[aria-checked="true"][data-gate="bar"]{background:var(--pill-red-bg);
border-color:var(--red);color:var(--red)}
.lc-gate[aria-checked="true"][data-gate="waive"]{background:var(--pill-green-bg);
border-color:var(--green);color:var(--green)}
.lc-gate[aria-checked="true"][data-gate="hold"]{background:var(--pill-amber-bg);
border-color:var(--amber);color:var(--amber)}
/* A bar the machine wrote, not the operator.  Dashed, because clicking it
   off is a ruling that also has to be recorded as one -- the switch does
   that, and the border is the warning that this row has an owner. */
.lc-gate[data-owner="auto"][aria-checked="true"]{border-style:dashed}
.lc-gate:disabled{opacity:.4;cursor:not-allowed}
.lc-gate:focus-visible{outline:2px solid var(--text);outline-offset:2px}
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
.zone-trap{background:rgba(224,82,82,.16)}
.zone-market{background:rgba(224,164,64,.14)}
.zone-agree{background:rgba(96,150,224,.10)}
[data-theme="light"] .zone-trap{background:rgba(200,48,48,.10)}
[data-theme="light"] .zone-market{background:rgba(190,130,20,.10)}
[data-theme="light"] .zone-agree{background:rgba(50,110,190,.08)}
footer{margin-top:34px;color:var(--muted);font-size:12.5px;text-align:center}
/* The group switcher.  Sticky, because it is the only way back to the
   other two thirds of the page once a long section has been scrolled
   into. */
.topnav{position:sticky;top:0;z-index:8;margin:0 0 16px;padding:9px 0 0;
background:var(--bg);border-bottom:1px solid var(--line)}
/* Five areas, not one flat row (user ruling, 2026-08-29).  Each group
   carries its own hue -- set per button as --tone -- so the bar reads as
   regions rather than as eleven identical pills, and the group titles
   below repeat the same hue so a reader never has to match text to know
   where a tab landed.  "全部" is pushed to its own end of the bar because
   it is not one of the areas; it is the way out of them. */
.tabs{display:flex;gap:7px;flex-wrap:wrap;align-items:stretch;
margin:0;padding:0 0 9px}
.tabs button{cursor:pointer;background:var(--card);color:var(--muted);
border:1px solid var(--line);border-radius:9px;padding:5px 12px 5px 10px;
font-family:inherit;font-size:13px;line-height:1.35;
display:flex;align-items:center;gap:7px;
border-left:3px solid var(--tone,var(--line))}
.tabs button:hover{color:var(--text);border-color:var(--line);
border-left-color:var(--tone,var(--accent));background:var(--why-bg)}
.tabs button[data-active="1"]{color:var(--text);
border-color:var(--tone,var(--accent));background:var(--why-bg);
box-shadow:inset 0 0 0 1px var(--tone,var(--accent))}
.tabs button .cnt{font-size:11px;color:var(--muted);
background:var(--bar-bg);border-radius:99px;padding:0 6px;min-width:18px;
text-align:center}
.tabs .spacer{flex:1;min-width:10px}
.tabs button.all{border-left-color:var(--line);color:var(--muted)}
.tabs .muted{font-size:11.5px}
/* The group heading wears its area's colour on the same edge as its tab. */
.grouptitle{border-left:3px solid var(--tone,var(--line));padding-left:10px}
/* Per-day tabs inside a card.  The same pill as the group switcher, one
   size down and not sticky: this one picks which day a card shows, it is
   not a way around the page. */
.daytabs{display:flex;gap:6px;flex-wrap:wrap;align-items:center;
margin:0 0 10px}
.daytabs button{cursor:pointer;background:var(--bg);color:var(--muted);
border:1px solid var(--line);border-radius:99px;padding:4px 10px;
font-family:inherit;font-size:12.5px;line-height:1.35;
display:flex;align-items:center;gap:5px}
.daytabs button:hover{color:var(--text);border-color:var(--accent)}
.daytabs button[data-active="1"]{color:var(--text);border-color:var(--accent);
background:var(--why-bg)}
.daytabs .muted{font-size:11px}
/* 全部 stacks every day in one card, so the days need a seam.  Only
   there: with one pane showing, a rule keyed on the hidden pane before it
   draws a line under nothing. */
.card[data-dayall="1"] [data-daypane]+[data-daypane]{margin-top:14px;
padding-top:10px;border-top:1px solid var(--line)}
.grouptitle{display:flex;gap:10px;flex-wrap:wrap;align-items:baseline;
margin:18px 0 0;font-weight:650}
.grouptitle .muted{font-weight:400}
#theme{position:fixed;top:14px;right:16px;z-index:9;cursor:pointer;
background:var(--card);color:var(--muted);border:1px solid var(--line);
border-radius:99px;padding:6px 13px;font-family:inherit;font-size:13px;
line-height:1;box-shadow:var(--shadow)}
#theme:hover{color:var(--text);border-color:var(--accent)}
/* Second row of the sticky bar: one chip per section on screen, so a
   twenty-section page is one click deep instead of one scroll long.  It
   wraps rather than scrolling sideways -- a jump bar whose far half is
   off-screen is the problem it was added to solve, and the labels are cut
   short enough that two rows is the worst case. */
.jump{display:flex;gap:5px;flex-wrap:wrap;align-items:center;
padding:0 0 8px}
.jump button{cursor:pointer;white-space:nowrap;flex:none;background:none;
color:var(--muted);border:1px solid transparent;border-radius:99px;
padding:3px 9px;font-family:inherit;font-size:12.5px;line-height:1.35}
.jump button:hover{color:var(--text);border-color:var(--line);
background:var(--card)}
.jump .sep{flex:none;width:1px;height:14px;background:var(--line);margin:0 4px}
/* Only once there is something to come back from. */
#totop{position:fixed;right:16px;bottom:18px;z-index:9;cursor:pointer;
background:var(--card);color:var(--muted);border:1px solid var(--line);
border-radius:99px;padding:7px 14px;font-family:inherit;font-size:13px;
line-height:1;box-shadow:var(--shadow);display:none}
#totop[data-show="1"]{display:block}
#totop:hover{color:var(--text);border-color:var(--accent)}
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
<button id="totop" type="button" title="返回顶部">↑ 顶部</button>
<div class="wrap" id="app"></div>
<script>
const D = __PAYLOAD__;
const h = (s) => String(s == null ? "" : s);
const hours = (v) => v == null ? "—" :
  (v < 1 ? Math.round(v * 60) + " 分钟前" :
   v < 48 ? v.toFixed(1) + " 小时前" : (v / 24).toFixed(1) + " 天前");
// Shares are whole lots: the exchange's minimum is five and the grid steps
// by one.  But sizing divides a notional by a price, so the stored REAL
// keeps the division's float residue -- 0.15×7.000000000000001 and
// 0.47×4.999999999999999 were both on the board, reading as precision the
// number does not have.  Print the integer when the value is one, to
// within that residue; a size that is genuinely fractional still shows its
// fraction rather than being silently rounded to a lot it is not.
const shares = (v) => {
  const n = Number(v);
  if (!isFinite(n)) return h(v);
  return Math.abs(n - Math.round(n)) < 1e-6
    ? String(Math.round(n)) : String(Number(n.toFixed(4)));
};
const OVERALL = {green:["运转正常","所有环节都在按预期更新"],
  amber:["有需要留意的地方","不影响出数，但有环节慢了或在攒数据"],
  red:["有环节断了","下面标红的环节需要处理"]};

function head(){
  const [t, s] = OVERALL[D.overall_health] || OVERALL.amber;
  const w = D.worker || {};
  // Name the broken links on the banner itself.  The payload knows which
  // rows turned the light red; making the reader scroll for them is what
  // "black box" complaints are made of.  Red banner lists red rows, amber
  // banner lists amber rows -- the level that set the light.
  const named = (D.alerts || [])
    .filter(a => a.health === D.overall_health);
  const chips = named.slice(0, 6).map(a =>
    `<span class="pill ${a.health}" style="font-size:13px">${h(a.label)}
      <span style="opacity:.78">｜${h(a.note)}</span></span>`).join("");
  const more = named.length > 6
    ? `<span class="muted">…等 ${named.length} 处，详见下文标${
        D.overall_health === "red" ? "红" : "黄"}的环节</span>` : "";
  return `<h1>气温篮子全链看板</h1>
  <p class="sub">从模型到下单的一整条链。<b>首页是看板加逐站账</b>：健康条、
  组合风险指标与图表、每日统计、逐站逐日明细与三张跟进表；
  往下按五个<b>区域</b>分组，每区一个颜色，标签栏与区标题同色。
  板块不再编号——它们会随分组移动，编号只会指错地方。</p>
  <div class="card"><div style="display:flex;gap:14px;align-items:center;
    flex-wrap:wrap">
    <span class="dot ${D.overall_health}" style="width:13px;height:13px"></span>
    <span class="big">${t}</span>
    <span class="muted">${s}</span>
    <span style="flex:1"></span>
    <span class="muted">常驻进程心跳 ${hours(w.heartbeat_age_hours)}
      ${w.pid ? "· pid " + w.pid : ""}</span>
    ${liveSwitch()}
  </div>
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;
    margin-top:9px;padding-top:9px;border-top:1px solid var(--line)">
    <span class="dot ${h(w.production_health||"muted")}"></span>
    <b>生产周期</b>
    <span class="muted">${h(w.production_note||"—")}</span>
    <span style="flex:1"></span>
    <span class="muted">上轮 ${w.production_cycle_seconds==null?"—":
      w.production_cycle_seconds+" 秒"} ·
      ${hours(D.worker&&D.worker.production_checked_at?
        (Date.now()-Date.parse(D.worker.production_checked_at))/3600000:null)}</span>
  </div>
  ${renderStamp()}${chips ? `<div style="display:flex;gap:8px;flex-wrap:wrap;
    align-items:center;margin-top:9px">${chips}${more}</div>` : ""}</div>`;
}

// When this page was built, and how old that makes it.
//
// The page is a FILE on a thirty-minute timer, not a live query, and it
// takes minutes to build -- so a reader who changes something and refreshes
// gets the previous build back.  On 2026-09-09 that read as a bug: P1's
// band table moved at 03:23Z, the page on screen was built at 03:07Z, and
// the build that would have shown it was twenty-four minutes into its own
// run.  Nothing on the page said so; the only timestamp was in the footer,
// in UTC, seven thousand lines down.
//
// So: local time, an age in minutes, and the build's own cost -- amber once
// the age passes a full cycle, because past that the reader is looking at
// something a render should already have replaced.
function renderStamp(){
  const R = D.render || {};
  const stamp = R.generated_at_utc || D.generated_at_utc;
  if(!stamp) return "";
  const at = Date.parse(stamp);
  if(!isFinite(at)) return "";
  const ageMin = Math.max(0, Math.round((Date.now() - at) / 60000));
  const local = new Date(at).toLocaleString(undefined, {
    month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const stale = ageMin >= 30;
  const build = R.build_sec == null ? null
    : (R.build_sec >= 90 ? (R.build_sec/60).toFixed(1) + " 分钟"
                         : Math.round(R.build_sec) + " 秒");
  const reused = R.reused_sections
    ? ` · 沿用 ${R.reused_sections} 区块` : "";
  return `<div style="display:flex;gap:10px;align-items:center;
    flex-wrap:wrap;margin-top:9px;padding-top:9px;
    border-top:1px solid var(--line)">
    <span class="dot ${stale ? "amber" : "green"}"></span>
    <b>本页渲染</b>
    <span class="${stale ? "pill amber" : "muted"}">${h(local)}
      · ${ageMin} 分钟前</span>
    <span class="muted">${stale
      ? "已超过一轮重建周期，页面上的数字可能落后于库里的"
      : "页面每 30 分钟重建一次，刷新浏览器不会重算"}</span>
    <span style="flex:1"></span>
    <span class="muted">${build ? "构建耗时 " + h(build) : ""}${
      R.computed_sections ? ` · 现算 ${R.computed_sections} 区块` : ""
    }${reused}</span>
  </div>`;
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
  // A ledger that is not there must not be read as a ledger that found
  // nothing.  A rebuild wipes the three tables and commits before its
  // forty-minute re-walk, and the render runs on its own timer, so a render
  // inside that window used to publish every count and every hit rate at
  // zero with nothing saying why (2026-09-08 10:20).  One banner, ahead of
  // the strip, and the strip is suppressed rather than shown as zeros.
  if(S.ledger_absent){
    return `<div class="card red" style="margin:10px 0 4px">
      <b>纸面账本此刻是空的——下面的仓位/命中率/回报率一律不要读。</b>
      <div class="muted" style="margin-top:6px">
        账本有历史可走却一条仓位都没有，这是「账本不在」而不是「一天都没入座」。
        最常见的原因是 <span class="mono">paper_trading_ledger.py --rebuild</span>
        正在跑：它先清空三张表并提交，再花约 40 分钟重走，这段时间任何读者
        看到的都是零。等重建结束后本页会自行恢复，无需处理。</div></div>`;
  }
  const pct0=(v)=>v==null?"—":(v*100).toFixed(0)+"%";
  const band=(v,good,ok)=>v==null?"muted"
    :(v>=good?"green":v>=ok?"amber":"red");
  const cards=[];
  const push=(cls,lab,val,sub2,title)=>cards.push(
    `<div class="k ${cls}" title="${h(title||"")}">
      <div class="lab">${lab}</div><div class="val">${val}</div>
      <div class="sub2">${sub2}</div></div>`);

  // 0. The resident itself, first: everything below this strip is read out
  // of a database the resident fills, so a reader who does not know the
  // pipeline stalled will misread every number after it as current.  A
  // heartbeat and a pid say the process is alive; the production light says
  // whether it is producing.  Those are different questions, and on
  // 2026-08-26 they had different answers for eleven hours.
  const W=D.worker||{};
  const beat=W.heartbeat_age_hours;
  push(beat==null?"muted":beat<0.25?"green":beat<1?"amber":"red",
    "常驻心跳", beat==null?"—":hours(beat),
    W.pid?`pid ${W.pid}`:"进程未发布 pid",
    "常驻进程最后一次写状态的时刻。它只说进程活着，不说流水线在出货——看右边那张");
  push(W.production_health||"muted",
    "生产周期", W.production_cycle_seconds==null?"—"
      :W.production_cycle_seconds.toFixed(0)+" 秒",
    h(W.production_note||"—"),
    `常驻自己的生产回执：${h(W.production_state||"未发布")}。`+
    `绿=正常产出，黄=单条车道阻断，红=多车道断/被写锁挡住/超一小时没完成一轮。`+
    `逐候选的日常拒绝不计入——那是目标函数在正常挑站日，见「没建仓是因为什么」`);
  push(W.live_submit_authorized?"amber":"green",
    "下单授权", W.live_submit_authorized?"已授权":"未授权（纸面）",
    W.live_submit_authorized?"真实下单已开启":"全链纸面运行，网关不发单",
    "是否允许向交易网关发真实订单。纸面态是设计常态：绿灯代表没有真钱在跑");
  // The other end of the same chain: without settled truth nothing below
  // this strip can be scored, so a stalled sweep silently freezes every
  // hit rate and calibration number on the page at its last good day.
  const T=D.truth||{};
  push(T.health||"muted",
    "真值物化", T.outstanding==null?"—"
      :(T.outstanding===0?"无欠账":T.outstanding+" 站日欠账"),
    T.sweep_age_hours==null?h(T.note||"—")
      :`上次清扫 ${hours(T.sweep_age_hours)}`+
       (T.refused?` · ${T.refused} 站日被证据挡住`:""),
    h(T.note||"")+"。真值是所有命中率与校准数字的分母，停了这页所有成绩都会冻在最后一个好日子上");

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
    "「模型数据到货」：四家集合预报的到货情况");

  // 2. Is anything blocking the chain right now.
  const blocking=(D.gaps||[]).filter(g=>g.severity==="high").length;
  const waiting=(D.gaps||[]).filter(g=>g.severity!=="high").length;
  // Only a blocking gap is a problem.  Colouring the strip amber because
  // some scope is still accumulating samples makes the strip amber almost
  // always, and a light that is always on is not a light.
  push(blocking?"red":"green",
    "要处理的缺口", String(blocking),
    waiting?`另有 ${waiting} 条在正常等待`:"没有待处理项",
    "「数据缺口」：数据缺口与健康检查");

  // 3. Did the truth for settled days actually land.
  const outstanding=(D.truth||{}).outstanding;
  push(outstanding==null?"muted":outstanding===0?"green"
      :outstanding<=5?"amber":"red",
    "真值待物化", outstanding==null?"—":String(outstanding),
    "结算真值还没落库的站日", "「真值物化健康度」：真值物化健康度");

  // 4. How much of the fleet actually opened a position on the newest day.
  const day=S.latest_day||(S.roster_event_dates||[])[0];
  const roster=(S.station_roster||[]).filter(e=>e.event_date===day);
  const opened=roster.filter(e=>e.stages&&e.stages.P0).length;
  const rate=roster.length?opened/roster.length:null;
  push(band(rate,0.5,0.25), "建仓率",
    `${opened}/${roster.length||"—"}`,
    `${h(day||"")} · ${pct0(rate)} 的站建了仓`,
    "「逐站逐日明细」（每日统计下方）：没建仓的站，格子里写着原因");

  // 6. Does the model's stated probability survive contact with the truth.
  const cs=S.coverage_stats||{};
  const stagesAll=S.stages||{};
  const cal=((stagesAll.P0||{}).credible||{}).calibration_ratio;
  push(cal==null?"muted":(cal>=0.85&&cal<=1.15)?"green"
      :(cal>=0.6&&cal<=1.4)?"amber":"red",
    "说得准不准", cal==null?"—":cal.toFixed(2)+"×",
    "命中率 ÷ 声称胜率，1.00× 是说得准",
    "「band 表」与「覆盖 · 价格 · EV」：概率分档校准");

  // 6b. Does the basket the objective CHOSE cover the truth, at the widths
  // the charter actually seats -- and is that moving.  Grouped on the
  // chain-level union of intended legs (hedge rungs included), not the
  // posted or filled set: the market sells us part of a three-rung choice
  // and the narrower groupings file that day under one rung, emptying the
  // three-rung row of exactly its hardest cases.
  //
  // The trend is the later half of settlements against the earlier, split
  // where the counts are even rather than at the middle date.  A direction
  // is only printed when it clears two combined standard errors; under
  // that it is a coin landing differently and saying "worse" would invent
  // a signal the sample cannot carry.
  ((S.chosen_basket_hit||{}).sizes||[]).forEach(row=>{
    const hitv=row.hit_rate;
    const arrow=row.delta==null?""
      :!row.delta_is_readable?"→"
      :row.delta>0?"↑":"↓";
    const move=row.delta==null?`${row.settled_count} 链 · 趋势样本不足`
      :!row.delta_is_readable
        ?`${row.settled_count} 链 · 与噪音不可分（Δ${
          (row.delta*100>=0?"+":"")+(row.delta*100).toFixed(0)}，需 ±${
          (2*row.delta_stderr*100).toFixed(0)}）`
      :`${row.settled_count} 链 · 后半 ${
        (row.delta*100>=0?"+":"")+(row.delta*100).toFixed(0)} 点`;
    push(hitv==null?"muted":band(hitv,0.5,0.33),
      `${row.chosen_size} 桶选中命中 ${arrow}`,
      pct0(hitv), move,
      `目标函数选中的腿集合（含护栏腿，跨阶段并集）盖住结算真值的比率。` +
      `买到的篮子只中 ${pct0(row.filled_hit_rate)}` +
      `，差额是市场没把会赢的那条腿卖给我们。声称 ${
      pct0(row.claimed_mean)}，实现/声称 ${
      row.realised_over_claimed==null?"—"
        :row.realised_over_claimed.toFixed(2)}×。见「篮子命中率」的脱靶分布`);
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
    "「覆盖 · 价格 · EV」：覆盖 · 价格 · EV");

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
  return `<h2>四家模型的数据在不在进来</h2>
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
  return `<h2>整条链路每一环是否在动</h2>
  <p class="sub">数据从上到下流：模型下载 → 真值结算 → 算校正 → 算概率修正 →
  出概率 → 盘中更新。任何一环停了，它下面的环节迟早会跟着停。</p>
  <div class="card"><div class="scroll"><table><thead><tr>
    <th>环节</th><th>在做什么</th><th>量</th><th>状态</th>
  </tr></thead><tbody>${rows}</tbody></table></div></div>`;
}

function tables(){
  const t = D.tables;
  return `<h2>每站的偏差订正与融合权重</h2>
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
    // The rows are the TABLE's own axis, never the page's preference
    // (charter 105).  A stage fitted on distance from the peak has five
    // cells that are not probabilities at all, and reading them through
    // the claim grid put the [0,1) cell -- which every claim midpoint
    // falls into -- on all eleven rows: one factor, printed eleven times,
    // beside a header that said "4 档生效".  That was the page lying
    // about a table production was applying correctly.
    const axis = s.axis || "claim";
    const distance = axis === "centre_distance";
    const cell = (b, rowLabel, cellNote) => {
      const f = b.factor;
      const cls = f > 1.001 ? "up" : (f < 0.999 ? "down" : "flat");
      const arrow = f > 1.001 ? "调高" : (f < 0.999 ? "调低" : "不动");
      const measured = b.measured_ratio == null ? "—" :
        b.measured_ratio.toFixed(2) + "×";
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
      return `<tr>
        <td class="mono">${rowLabel}</td>
        <td>${measured}${cellNote}</td>
        <td class="factor ${cls}">${f.toFixed(3)} <span class="muted">${arrow}</span></td>
        <td class="muted">${why}</td></tr>`;
    };
    // Distance axis: one row per fitted cell, no display grid to map onto
    // -- the cells ARE the rungs.  The open top cell is every bucket four
    // or more steps out from the peak.
    const distanceRows = () => (s.bands || []).map(b => {
      const lo = Math.round(b.lower);
      const rowLabel = lo === 0 ? "峰值桶"
        : (b.upper >= 90 ? `离峰 ≥${lo} 格` : `离峰 ${lo} 格`);
      return cell(b, rowLabel, "");
    }).join("");
    // Five-percent display grid capped at 50% (user ruling, 2026-08-25),
    // the same rows the audit table below uses, so knob and audit line up.
    // A display bin belongs to the fitted cell holding its midpoint; the
    // fitted cells are wider than 5%, so consecutive rows sharing a cell
    // repeat its factor and say which cell owns them.
    const claimRows = () => {
      const GRID = [];
      for (let i = 0; i < 10; i++) GRID.push([i*5, i*5+5]);
      GRID.push([50, 100]);
      return GRID.map(([lo, hi]) => {
        const mid = (lo + hi) / 2 / 100;
        const b = (s.bands || []).find(x => mid >= x.lower && mid < x.upper);
        if (!b) return "";
        const cellLabel = `${(b.lower*100).toFixed(0)}–${
          Math.min(100, b.upper*100).toFixed(0)}%`;
        const rowLabel = hi >= 100 ? "≥50%" : `${lo}–${hi}%`;
        const cellNote = cellLabel === `${lo}–${hi}%` ||
          (hi >= 100 && b.lower >= 0.5)
          ? "" : ` <span class="muted">档 ${cellLabel}</span>`;
        return cell(b, rowLabel, cellNote);
      }).join("");
    };
    const bands = distance ? distanceRows() : claimRows();
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
        <span class="pill">${distance ? "按离峰距离分档" : "按声称概率分档"}</span>
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
        <th>${distance ? "桶离峰距离" : "概率档"}</th>
        <th>实际兑现</th><th>修正系数</th><th>为什么</th>
      </tr></thead><tbody>${bands}</tbody></table>
      ${journal ? `<details style="margin-top:8px">
      <summary class="muted" style="cursor:pointer">最近几次自动拟合</summary>
      <table><thead><tr><th>时间</th><th>结果</th><th>样本</th>
      <th>生效档数</th><th>版本</th></tr></thead>
      <tbody>${journal}</tbody></table></details>` : ""}
    </div>`;
  }).join("");
  const axisNote = D.stages.map(s => `${s.stage}＝${
    (s.axis || "claim") === "centre_distance" ? "离峰距离" : "声称概率"}`)
    .join("、");
  const anyDistance = D.stages.some(s => s.axis === "centre_distance");
  return `<h2>band 表：分档校准（三阶段各自的账）</h2>
  <p class="sub">系统会记录"凡是说过 X% 的地方，后来实际发生了多少次"。
  如果长期说 6% 只发生 4%，就把这一档的数字乘一个小于 1 的系数压回去。</p>
  <div class="why"><b>三张卡不一定按同一个东西分档</b>（第 105 条）：
  当前 ${h(axisNote)}。<b>声称概率</b>是按"这个桶报了几成"分；
  <b>离峰距离</b>是按"这个桶离当天分布的最高点差几格"分——
  这张表治的病是过度分散（峰上压得太薄、四格开外压得太厚），那是一句关于
  <b>位置</b>的话，而位置在乘完系数、重新归一之后不会变，声称概率会变。
  每张卡的行就是它自己那根轴，不再互相翻译。</div>
  <div class="why" style="margin-top:8px"><b>怎么看这张表：</b>「实际兑现」是实测兑现率除以声称概率——
  1.00× 表示说得准，0.69× 表示说大话（只兑现了 69%），1.23× 表示说小话。
  「修正系数」是实际乘上去的那个数，只有证据足够扎实的档才会不等于 1.000，
  而且取的是保守值——宁可少改，不可改过头。</div>
  <div class="why" style="margin-top:8px"><b>这一节是一条闭环，分三步：</b><br>
  ① <b>记账</b>——三张卡各自记录本阶段"凡是说过 X% 的地方，后来兑现了多少"；<br>
  ② <b>拧旋钮</b>——证据过门的档拟出修正系数，乘进下一次服务的概率。
  这是全链<b>唯一改变数字</b>的一步；<br>
  ③ <b>体检</b>——下面「说了多少 vs 真发生了多少」量的是系数生效<b>之后</b>的
  剩余偏差，<b>分 stage、与上表同一取样车道、同一证据起点</b>
  （2026-08-25 起；此前它读的是训练端 OOS 预测，人群不同曾与上表互相矛盾）。
  它只诊断、不回写。${anyDistance ? `体检永远按<b>声称概率</b>的 5% 尺分档
  ——问的是"报出去的数准不准"；上表按离峰距离分档的那一阶段，
  和这张表<b>不能逐行对照</b>，只能看总体残差有没有变小。` :
  "它与上表同一把 5% 尺，可以逐行对照。"}
  若某档残差持续变大，说明对应旋钮方向或粒度不对——回到 ②。</div>
  <div class="grid g3" style="margin-top:8px">${cards}</div>
  ${reliabilityAudit()}`;
}

// The shadow candidate for the knob section six turns.  It is rendered
// beside that section on purpose: the question it answers -- is one
// factor per band per stage too coarse -- is unreadable without the table
// it would replace sitting next to it.
// Charter-level knob, its shadow, and its audit are one reading, not three
// sections (user ruling, 2026-08-29): the shadow's whole question is "should
// the table above be split finer", and answering it two headings away made
// readers compare a pooled row with a split row by scrolling.
function bandTable(){
  // The edge thermometer lives here rather than under the market tab
  // (user ruling, 2026-09-01): the band table audits claimed probability,
  // the thermometer audits claimed edge, and they are one reading.
  return stages() + bandShadow() + edgeBandHealth()
    + effectiveEdgeLambda()
    + convergenceClaim();
}


function bandShadow(){
  const S = D.band_shadow || {};
  if(!(S.stages||[]).length) return "";
  const g = S.gates || {};
  const cards = S.stages.map(s => {
    // Three columns, not five: these cards sit three to a row, and a
    // five-column table there breaks its own header mid-word.  The funnel
    // reads better as one cell anyway -- 1091 → 0 → 0 is the story, and
    // splitting it across columns makes the reader reassemble it.
    const levels = (s.levels||[]).map(L => {
      const cls = L.promoted > 0 ? "up" : "flat";
      const blocked = (L.blocked||[]).map(b =>
        `${h(b.reason)} ${b.n}`).join("；");
      return `<tr>
        <td style="white-space:nowrap">${h(L.label)}</td>
        <td class="mono" style="white-space:nowrap">${L.tested} →
          ${L.eligible} → <span class="factor ${cls}">${L.promoted}</span></td>
        <td class="muted">${blocked || "—"}</td></tr>`;
    }).join("");
    const promoted = (s.promoted||[]).map(p => {
      const cls = p.factor > 1.001 ? "up" : (p.factor < 0.999 ? "down" : "flat");
      const arrow = p.factor > 1.001 ? "调高" : (p.factor < 0.999 ? "调低" : "不动");
      return `<tr>
        <td>${h(p.group_label)}${p.station ? " · " + h(p.station) : ""}</td>
        <td class="mono">${(p.band[0]*100).toFixed(0)}–${
          Math.min(100, p.band[1]*100).toFixed(0)}%</td>
        <td class="factor ${cls}">${p.factor.toFixed(3)}
          <span class="muted">${arrow}</span></td></tr>`;
    }).join("");
    const dist = (S.distance||[]).filter(d => d.stage === s.stage);
    const distRows = dist.map(d => {
      const label = d.level === "station" ? "站" : "站 × 桶位置";
      // Days REMAINING, not days total.  days_needed counts from the
      // start of accumulation, and reporting it raw reads as "37 more
      // days" when 28 of those 37 are already banked.
      const left = d.days_needed == null ? null
        : Math.max(0, Math.ceil(d.days_needed - d.days_now));
      const eta = d.reached > 0 ? "已达标"
        : (left == null ? "—" : `还需约 ${left} 天`);
      const pct = Math.min(100, 100 * d.best_mass / d.floor);
      return `<tr>
        <td style="white-space:nowrap">${label}<br>
          <span class="muted">${d.cells} 格</span></td>
        <td><div class="bar" style="margin:0"><i style="width:${pct}%"></i></div>
          <span class="muted">最肥格 ${d.best_mass.toFixed(2)} /
          门槛 ${d.floor.toFixed(1)}</span></td>
        <td>${eta}</td></tr>`;
    }).join("");
    const journal = (s.journal||[]).slice(0,5).map(j =>
      `<tr><td class="mono">${h(j.at).slice(0,16).replace("T"," ")}</td>
       <td>${j.days} 天</td><td>${j.promoted} 格继承非池因子</td>
       <td class="mono">${j.gain >= 0 ? "+" : ""}${j.gain.toFixed(2)}</td>
       <td class="mono muted">${h(j.sha)}</td></tr>`).join("");
    return `<div class="card">
      <div style="display:flex;align-items:center;gap:8px">
        <b>${h(s.stage)}</b><span class="muted">${h(s.meaning)}</span>
        <span style="flex:1"></span>
        <span class="pill">影子 · 不服务</span>
      </div>
      <div class="muted" style="margin-top:8px">
        证据：${s.serving_days} 天 / ${s.station_days} 个站日 ·
        版本 <span class="mono">${h(s.sha)}</span> ·
        上次拟合 ${hours(s.fit_age_hours)}</div>
      <table style="margin-top:12px"><thead><tr>
        <th>维度</th><th style="white-space:nowrap">测→过门→够格</th>
        <th>卡在哪一门</th></tr></thead><tbody>${levels}</tbody></table>
      ${promoted ? `<div class="muted" style="margin-top:10px">够格的格子
        （前向窗 ${s.forward_nll_gain >= 0 ? "+" : ""}${
        s.forward_nll_gain.toFixed(2)} NLL）：</div>
      <table><thead><tr><th>位置</th><th>概率档</th><th>系数</th>
      </tr></thead><tbody>${promoted}</tbody></table>` :
      `<div class="muted" style="margin-top:10px">本次没有任何格子够格，
        全部回退池权重。</div>`}
      ${distRows ? `<div class="muted" style="margin-top:10px">饿死的维度
        还差多远：</div>
      <table><thead><tr><th>维度</th><th>最肥格的证据量</th>
      <th>按当前速率</th></tr></thead><tbody>${distRows}</tbody></table>` : ""}
      ${journal ? `<details style="margin-top:8px">
      <summary class="muted" style="cursor:pointer">最近几次影子拟合</summary>
      <table><thead><tr><th>时间</th><th>样本</th><th>继承格数</th>
      <th>前向窗</th><th>版本</th></tr></thead>
      <tbody>${journal}</tbody></table></details>` : ""}
    </div>`;
  }).join("");
  return `<h3 style="margin-top:30px">影子 band 表：拆细了值不值得</h3>
  <p class="sub">上面那张表是<b>全舰队共用</b>的——四十五个站、一天里所有温度桶，
  只要报的数字落在同一档，就吃同一个折扣。这一节在问：拆开来会不会更准？</p>
  <div class="why"><b>拆成两维试：</b>一维是<b>站</b>（成都和纽约分开记账），
  一维是<b>桶位置</b>（离当天最高点最近的那个桶，和远在偏冷／偏暖一侧的桶，分开记账）。
  拆完之后每一格都要自己重新过三道门；<b>过不了的格子，退回去用全舰队那个折扣</b>
  ——不是退回"不打折"，那样会把舰队层面已经测准的修正一起扔掉。</div>
  <div class="why" style="margin-top:8px"><b>这一节不改任何数字。</b>
  它是影子层：只记录"如果按这个更细的表来，会怎样"，服务端仍然走上一节那张池化表。
  真要启用，得改代码并过账本重放，不是这里点一下就生效。</div>
  <div class="why" style="margin-top:8px"><b>三道门为薄格重设过：</b>
  ①<b>样本门</b>——这一格至少要攒到 ${g.min_claimed_mass} 个"期望命中"、
  跨 ${g.min_days} 天、${g.min_rows} 行；攒不到就根本判不出该调高还是调低。
  ②<b>显著门</b>——按天重抽样，并做<b>多重比较校正</b>（FDR ${g.fdr_q}）：
  同时检验几千格时，不校正会凭空冒出上百个假阳性。
  ③<b>试算门</b>——把这个系数放进最近四成的日子里重算一遍，真的更准才采用，
  而且比的是"退回池权重"那个基准，不是"完全不修正"。</div>
  <div class="why" style="margin-top:8px"><b>怎么读「测→过门→够格」：</b>
  这是一条漏斗——先看这一维拆出<b>多少格</b>，再看<b>多少格攒够了样本</b>，
  最后看<b>多少格真的自己测出了系数</b>。最后那个数只算<b>这一维自己测出来</b>的；
  卡片下方「最近几次影子拟合」里的「继承格数」是另一回事——一个全舰队的桶位置
  系数会被两百多个细格继承，那<b>不代表</b>两百多个站各自测准了。</div>
  <div class="grid g3" style="margin-top:8px">${cards}</div>`;
}

// Step ③ of section 4: the post-correction residual, pooled and re-binned.
// Lives beside the knob it audits; reads the scorecard payload so the two
// sections cannot drift apart on data.
function reliabilityAudit(){
  // Same lane, same stages, same evidence anchors, same 5% cells as the
  // knob table above -- the audit that lived here before read the training
  // lane's OOS holdout and disagreed with the knobs on the page for
  // population reasons, not calibration ones.
  const A = D.band_residual_audit || {};
  const stageBlocks = (A.stages||[]).map(s => {
    const rows = (s.rows||[])
      .filter(b => b.n >= 30)
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
    return `<div style="margin-top:10px">
      <div class="muted"><b>${h(s.stage)}</b> · ${s.day_count} 天 ·
        每站日首个决策 · 与上表同窗</div>
      <table style="margin-top:6px"><thead><tr>
        <th>声称档</th><th>样本</th><th>平均声称</th><th>实际发生</th><th>差</th>
      </tr></thead><tbody>${rows}</tbody></table></div>`;
  }).join("");
  if(!stageBlocks) return "";
  const stationRows=(A.stations||[]).map(r=>`<tr>
      <td class="mono">${h(r.station)}${r.station_label
        ?` <span class="muted">${h(r.station_label)}</span>`:""}</td>
      <td class="mono muted">${r.n}</td>
      <td class="mono">${(r.claimed*100).toFixed(0)}%</td>
      <td class="mono">${(r.realized*100).toFixed(0)}%</td>
      <td class="mono ${r.ratio>=1?"up":"down"}">${r.ratio.toFixed(2)}${
        r.significant?' <b title="按 2σ 显著偏离 1">✱</b>':""}</td>
    </tr>`).join("");
  const stationBlock=!stationRows?"":`<div style="margin-top:14px">
    <div class="muted"><b>每站 实现/声称（中心±1，三 stage 合并）</b> ·
    band 是全舰队池化的，这张表是「band 要不要分站」的法庭：
    带 ✱ 的站在池化修正之后仍按 2σ 显著偏离 1——
    比值持续 &lt;1 是该站上游（偏差/权重层）没修干净的证据，
    先修上游；上游修好后仍显著，才轮到给 band 加站维度（向池化收缩）。</div>
    <div class="scroll" style="max-height:340px;margin-top:6px">
    <table><thead><tr>
      <th>站</th><th>站日</th><th>声称(中心±1)</th><th>实现</th><th>实现/声称</th>
    </tr></thead><tbody>${stationRows}</tbody></table></div></div>`;
  return `<div class="card" style="margin-top:12px">
  <div class="muted"><b>③ 体检：说了多少 vs 真发生了多少</b>（band 店同一车道 ·
  每站日首个决策 · 同窗同 stage · 本表恒按<b>声称概率</b>的 5% 尺分档，
  与上表逐行对照仅在该 stage 也按声称概率分档时成立 · 唯一差别：上表读系数前的
  原始声称，本表读系数后的实际服务值 · 只诊断，无下游消费）</div>
  ${stageBlocks}
  <div class="muted" style="margin-top:6px">
    「差」为正＝实际比声称更常发生（修完还保守）；为负＝修完仍吹头。
    与上表逐行对照（仅限同按声称概率分档的 stage）：某档上表刚给了压缩系数、
    本表仍显著为负，说明系数还没压够；本表转正说明压过了头。
    按离峰距离分档的 stage 看整列残差是否整体收窄，不做逐行对照。
    只显示样本 ≥30 的档。</div>
  ${stationBlock}</div>`;
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
  // The objective's own choice, before the market and the clamps thin it:
  // the chain-level union of intended legs.  The table above groups on the
  // width that was actually bought, which files a thinned three-rung
  // choice under one rung -- these rows keep the hardest cases where the
  // choice put them.  The same numbers head the top strip; restating them
  // here puts the bought-width table they correct right beside them.
  const chosen=((S.chosen_basket_hit||{}).sizes||[]);
  const chosenRows=chosen.map(row=>{
    const ratio=row.realised_over_claimed;
    return `<tr><td><b>${row.chosen_size} 桶</b></td>
      <td>${row.settled_count}</td>
      <td class="${row.hit_rate>=0.5?"up":""}">${p(row.hit_rate)}</td>
      <td>${p(row.filled_hit_rate)}</td>
      <td>${p(row.claimed_mean)}</td>
      <td class="${ratio==null?"":(ratio>=0.9&&ratio<=1.15?"up":"down")}">${
        ratio==null?"—":ratio.toFixed(2)+"×"}</td></tr>`;
  }).join("");
  const chosenBlock=chosen.length?`
  <h3 style="margin-top:18px">目标函数选桶的真值命中率（intended，跨阶段并集）</h3>
  <div class="why"><b>选中≠买到：</b>「选中命中」是真值落进目标函数要的腿集合，
  「买到命中」是落进真正成交的腿。两列的差不是模型错，
  是市场没把会赢的那条腿按我们的价卖给我们。</div>
  <table><thead><tr><th>选桶宽度</th><th>已结算链</th><th>选中命中</th>
    <th>买到命中</th><th>声称</th><th>实现/声称</th></tr></thead>
    <tbody>${chosenRows}</tbody></table>`:"";

  // Where the truth lands when the choice misses, in the station's own
  // bucket steps.  Distance is from the chosen set's EDGE -- the nearest
  // chosen bucket -- never from the centre bucket.  Sign is the
  // temperature axis: above means the day ran hotter than everything
  // chosen, below colder.
  const off=S.chosen_miss_offsets||null;
  let offBlock="";
  if(off&&off.misses){
    const most=Math.max(1,...(off.histogram||[]).map(b=>b.count));
    const bin=k=>((off.histogram||[]).find(b=>b.offset===k)||{}).count||0;
    const offRows=(off.histogram||[]).map(b=>`<tr>
      <td class="mono">${h(b.offset)}</td>
      <td class="mono">${b.count}</td>
      <td class="mono">${(100*b.count/off.misses).toFixed(0)}%</td>
      <td><span class="mono">${"█".repeat(
        Math.max(1,Math.round(12*b.count/most)))}</span></td></tr>`).join("");
    const sided=off.above+off.below;
    const hotOne=bin("+1"), coldOne=bin("-1");
    offBlock=`
  <h3 style="margin-top:18px">选桶脱靶分布（真值出选桶边界几格）</h3>
  <p class="sub">${off.settled} 条已结算链：选中 ${off.hits}，脱靶 ${off.misses}${
    off.unparsed?`（另 ${off.unparsed} 条标签无法解析）`:""}${
    off.inside_gap?`（含 ${off.inside_gap} 条落在并集空隙里，无方向）`:""}。
    距离从选桶自己的边界起算，与中心桶无关：
    −1 = 真值停在最冷选桶再往下一格，+1 = 最热选桶再往上一格。</p>
  <div class="why"><b>贴边还是远脱：</b>出界 1 桶的有
  ${off.adjacent} 条（占有向脱靶的 ${sided?(
    100*off.adjacent/sided).toFixed(0):"—"}%）——真值就停在选桶隔壁，
  那一侧多放一格就盖住；出界 ≥2 桶的 ${off.far} 条（${sided?(
    100*off.far/sided).toFixed(0):"—"}%）——整个方向看歪了，加一格也救不回。
  方向上：出热边（真值在上）${off.above} 条 vs 出冷边 ${off.below} 条${
    off.above>off.below?"，选桶整体偏冷（低估了白天的抬升）"
    :off.below>off.above?"，选桶整体偏热":"，两侧均衡"}。
  算术上：热侧全体加一格可多盖 ${hotOne} 条（${off.hits}→${
    off.hits+hotOne}/${off.settled}），两侧各加一格到 ${
    off.hits+hotOne+coldOne}/${off.settled}——代价是每篮多一到两条腿的本金，
  且宪章上限四桶，这里只报算术不作推荐。</div>
  <table><thead><tr><th>出界（桶）</th><th>条数</th><th>占脱靶</th><th></th>
    </tr></thead><tbody>${offRows}</tbody></table>`;
  }

  // Section 14 was the maker lane's own account -- resting fill rates, the
  // shadow probe, the stage-by-stage saving-versus-taker table.  Charter 65
  // made taker the execution law, so it described a lane that stopped
  // running; it was removed on 2026-08-29 rather than left printing a dash.
  // The number is NOT reused: every other section keeps the number a reader
  // already knows it by, and a gap is cheaper than renumbering nine of them.
  return `<h2>篮子命中率（按桶数）</h2>
  <p class="sub">结算真值落在篮子里就算命中。只算可成交的（整篮 ≥1 分）。</p>
  <div class="why"><b>这张表回答一件事：</b>多买一个桶买到的命中率，
  值不值它多花的钱。命中率一定随桶数上升——问题是成本也上升，
  最右边的每元回报才是净结果。</div>
  <table><thead><tr><th>篮子宽度</th><th>已结算</th><th>命中</th><th>命中率</th>
    <th>声称</th><th>实现/声称</th><th>均成本</th><th>每元回报</th></tr></thead>
    <tbody>${rows}</tbody></table>
  ${chosenBlock}
  ${offBlock}
  ${fleetRegime()}`;
}

// Sits under the miss distribution because it is that distribution's
// diagnosed cause: the +1 misses were the served centre running cold while
// the fitted layers, which need days of held-out evidence before they may
// move, had not moved.  This is what reacts the next morning instead.
function fleetRegime(){
  const R=(D.selection||{}).fleet_regime;
  if(!R||!R.available) return "";
  const LIGHT={GREEN:["#3fb950","正常"],YELLOW:["#d29922","偏移"],
    RED:["#f85149","显著偏移"]};
  const rows=Object.entries(R.stages||{}).map(([stage,s])=>{
    const [colour,word]=LIGHT[s.alert]||LIGHT.GREEN;
    const sig=s.signal_c==null?"—":`${s.signal_c>=0?"+":""}${s.signal_c.toFixed(2)}`;
    // A dark stage shows what it would have served, in brackets, so the
    // number it is withholding stays visible rather than reading as zero.
    const shift=s.served
      ?`${s.regime_delta_c>=0?"+":""}${s.regime_delta_c.toFixed(2)}`
      :(s.withheld_delta_c==null?"—"
        :`(${s.withheld_delta_c>=0?"+":""}${s.withheld_delta_c.toFixed(2)})`);
    const spark=(s.recent||[]).map(d=>{
      const v=d.residual_c_mean;
      const c=Math.abs(v)<0.3?"#8b949e":(v>0?"#f85149":"#58a6ff");
      return `<span style="color:${c}" title="${h(d.event_date)} ${
        v>=0?"+":""}${v.toFixed(2)}°C，${d.station_count} 站">${
        v>=0?"▲":"▼"}</span>`;
    }).join(" ");
    return `<tr>
      <td><b>${h(stage)}</b>${s.served?"":
        '<span class="muted"> 暗</span>'}</td>
      <td><span class="pill" style="background:${colour}22;color:${
        colour}">${word}</span></td>
      <td class="mono">${sig}</td>
      <td class="mono">${s.run_length}</td>
      <td class="mono">${h(s.tier)}</td>
      <td class="mono">${shift}</td>
      <td class="mono">${s.station_count||"—"}</td>
      <td>${spark}</td>
      <td class="muted">${h(s.reason||s.state||"")}</td>
    </tr>`;
  }).join("");
  const [banner,word]=LIGHT[R.alert]||LIGHT.GREEN;
  return `<h3 style="margin-top:18px">舰队隔日政权偏移
    <span class="pill" style="background:${banner}22;color:${banner}">${
      word}</span></h3>
  <p class="sub">昨天全舰队结算下来，服务出去的分布中心差了多少度；
  今天按这个差额把中心挪回来。</p>
  <div class="why"><b>这一层目前只测不修，三个阶段全暗。</b>
  它读昨天全舰队真实差了多少，乘写死的系数并封顶——不拟合任何参数，所以
  本来敢在一天证据上动，而旁边那个拟合的中心/宽度层要攒八天才敢。
  <b>但账本重放否决了它：</b>08-17~26 共同结算的 207 条链，选桶覆盖
  118→117（得 3 失 4，p=1.000，纯噪声），ROI −19.4%→−26.7%（差 7.3 点，
  剔除最好 1/2/3 条后仍差 6.7/6.3/6.7 点）。<b>病灶不在选桶：</b>207 条里
  200 条选的桶一模一样，光这 200 条就从 −323 掉到 −454 USDC。变的是
  <i>声称概率</i>——Kelly 按它定注、EV 闸按它准入。舰队均值中心变准了，
  可每站自己的真值桶概率反而略降（中位 −0.0011）。
  <b>教训：账本不是按舰队均值定价的，一个不含站的数字改不好每站的声称。</b>
  日序列和预警继续跑（两者都不花钱且有用），修正要等一个能改善每站声称的
  版本。</div>
  <table><thead><tr><th>阶段</th><th>灯</th><th>昨日差额°C</th><th>连续</th>
    <th>档位</th><th>今日修正°C</th><th>站数</th><th>近七日</th><th>状态</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

// Sits after the per-station summary because it is that table's follow-up
// question: the summary ranks stations by what they lost, this asks which
// of them lost for a reason.  Ranking by P&L alone cannot tell a broken
// station from an unlucky one on five settled chains; failing several
// dimensions that do not share a mechanism can.
// The five-dimension failure read, folded into the gate table rather than
// standing beside it (user ruling, 2026-09-09).  Both are per-station
// verdicts, and an operator flipping a switch needs the case for it on the
// same row -- two tables one under the other made them line up by eye.
//
// It stays a separate measurement, not a merged one: the gate's ratio and
// the matrix's calibration_ratio are different windows over different
// populations (CYYZ reads 1.04 and 0.66 on the same day), and so are the
// gate's bias against the matrix's centre residual, and the gate's chains
// against the settled count.  Collapsing either pair into one column would
// be inventing a number neither layer computed.
function stationFailureIndex(){
  const M=(D.selection||{}).station_failure_matrix;
  if(!M||!M.available) return null;
  const by={};
  (M.stations||[]).forEach(s=>{by[s.station_id]=s;});
  return {by:by, thresholds:M.thresholds||{},
    split:M.split_event_date,
    worst:(M.stations||[]).filter(s=>s.flag_count>=4).map(s=>s.station_id),
    clean:(M.stations||[]).filter(s=>s.flag_count===0).length};
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
  return `<h2>没建仓是因为什么（逐阶段）</h2>
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


function truthLanding(){
  const T=D.truth_landing||{};
  if(!(T.by_price||[]).length) return "";
  const p=v=>v==null?"—":(v*100).toFixed(1)+"%";
  const bandRows=(rows,valueLabel)=>rows.filter(r=>r.bucket_count>0).map(r=>{
    const ratio=(valueLabel&&r.value_mean>0&&r.hit_rate!=null)
      ? r.hit_rate/r.value_mean : null;
    return `<tr><td class="mono">${r.band[0].toFixed(2)}–${r.band[1]>=1?"1.00":r.band[1].toFixed(2)}</td>
      <td class="mono">${r.truth_count}</td>
      <td class="mono">${r.bucket_count}</td>
      <td>${p(r.truth_share)}</td>
      <td>${p(r.hit_rate)}</td>
      <td>${p(r.value_mean)}</td>
      <td class="${ratio==null?"":(ratio>=0.85&&ratio<=1.2?"up":"down")}">${
        ratio==null?"—":ratio.toFixed(2)+"×"}</td></tr>`;
  }).join("");
  const head=label=>`<thead><tr><th>档</th><th>真值</th><th>桶数</th>
    <th>占真值</th><th>中标率</th><th>${label}</th><th>实现/声称</th></tr></thead>`;
  const labels=T.coarse_labels||[];
  const cell={};
  (T.cross||[]).forEach(c=>{cell[c.claim_band+"|"+c.price_band]=c;});
  const crossRows=labels.map((row,ri)=>{
    const tds=labels.map((col,ci)=>{
      // Zones by who is the optimist, not by the cell's own number: below
      // the diagonal the model claims more than the market charges -- the
      // exact cells an edge ranking wants to buy -- above it the market
      // keeps alive what the model has dismissed, and on it the two agree.
      const zone=ri>ci?"zone-trap":(ri<ci?"zone-market":"zone-agree");
      const c=cell[row+"|"+col];
      if(!c||!c.bucket_count) return `<td class="muted ${zone}">—</td>`;
      const rate=c.truth_count/c.bucket_count;
      const cls=rate>=0.25?"up":(rate<=0.02&&c.bucket_count>=20?"down":"");
      return `<td class="mono ${cls} ${zone}">${c.truth_count}/${c.bucket_count}
        <span class="muted">(${(rate*100).toFixed(1)}%)</span></td>`;
    }).join("");
    return `<tr><td class="mono"><b>${row}</b></td>${tds}</tr>`;
  }).join("");
  const legend=`<p class="sub" style="margin-top:8px">
    <span class="zone-trap" style="padding:2px 8px;border-radius:4px">红区</span>
    模型比市场乐观（edge 门想买的格子——历史上几乎全空）&nbsp;
    <span class="zone-market" style="padding:2px 8px;border-radius:4px">黄区</span>
    市场比模型乐观（真值跟着市场走——别因模型嫌贵而放弃）&nbsp;
    <span class="zone-agree" style="padding:2px 8px;border-radius:4px">蓝区</span>
    两者一致（校准良好的对角线）</p>`;
  return `<h2>真值落位（价格 × 概率）</h2>
  <p class="sub">${T.since} 起 · 已闭合站日 ${T.closed_station_days}
    · 市场裁定出赢家 ${T.adjudicated_station_days}
    · 采集截断剔除 ${T.capture_truncated_days}。
    真值＝市场收敛裁定（末端买价 ≥0.80 的桶）；
    价格＝首个 P0 决策后 4 小时的中位卖一；概率＝同一时刻的服务后验。</p>
  <div class="why"><b>这三张表回答一件事：</b>真值到底住在哪个价位、哪个声称档，
  以及两把尺子对不齐时该信谁。交叉表按行读：同一模型档内，中标率跟着市场价爬；
  按列读：同一价格档内，模型的梯度几乎消失——分歧格里对的是市场。</div>
  <h3 style="margin-top:14px">按市场价格（入场后 4h 中位卖一）</h3>
  <table>${head("均价")}<tbody>${bandRows(T.by_price,true)}</tbody></table>
  <h3 style="margin-top:14px">按模型概率（P0 建仓时刻声称）</h3>
  <table>${head("声称均值")}<tbody>${bandRows(T.by_claim,true)}</tbody></table>
  <h3 style="margin-top:14px">交叉：行＝模型概率档，列＝市场价格档（中标/桶数）</h3>
  <table><thead><tr><th>模型＼市场</th>${
    labels.map(l=>`<th class="mono">${l}</th>`).join("")
  }</tr></thead><tbody>${crossRows}</tbody></table>${legend}`;
}

// The lambda tables, one per stage scope.  Placed beside the edge
// thermometer because they are the same question asked at the other end:
// the thermometer measures how much of a claimed EDGE arrives, this
// measures how much of a claimed PROBABILITY arrives, and the objective
// sizes on the second.
function effectiveEdgeLambda(){
  const E=D.effective_edge_lambda||{};
  const scopes=E.scopes||[];
  if(!scopes.length)
    return `<h2 style="margin-top:30px">有效边际 λ 表（按阶段）</h2>
    <div class="card"><span class="dot green"></span> 无结算数据。</div>`;
  const num=(v,d)=>v==null?"—":v.toFixed(d==null?2:d);
  const pct=v=>v==null?"—":(v*100).toFixed(0)+"%";
  const bl=s=>String(s).replace(/</g,"&lt;");

  // ------------------------------------------------------------------
  // Charter 96, drawn instead of described.  The rule is one inequality --
  // the gate admits a window when lambda(a) > a -- and lambda is a
  // staircase, so a band buys exactly the sliver where its own step stands
  // above the diagonal.  Written out that took a paragraph and still left
  // the reader unable to say which bands were blind; drawn, a blind band is
  // one with no green under its step, and the promotion backlog is the
  // distance from the solid line up to the dotted one.  The three panels
  // share their scales on purpose: the claim being made is that P0 is an
  // inverted U and P1 is monotone, and that only reads on common axes.
  const XMAX=1.25, YMAX=1.30;
  const curve=sc=>{
    const W=340,H=204,PL=28,PR=8,PT=20,PB=44;
    const IW=W-PL-PR, IH=H-PT-PB, BASE=PT+IH;
    const X=a=>PL+IW*Math.max(0,Math.min(a,XMAX))/XMAX;
    const Y=l=>PT+IH*(1-Math.max(0,Math.min(l,YMAX))/YMAX);
    const bands=(sc.bands||[]).filter(b=>b.lower<XMAX);
    const hiSeats=Math.max(1,...bands.map(b=>b.seats||0));
    let seatBars="",grid="",stair="",rawline="",green="",ribbon="",hover="";
    let prevY=null;
    bands.forEach(b=>{
      const lo=b.lower, up=(b.upper==null?XMAX:Math.min(b.upper,XMAX));
      const xa=X(lo), xb=X(up), w=Math.max(1,xb-xa), y=Y(b.served);
      // Seat mass behind everything, so "this band is blind" can be read
      // together with "and it is where the book actually sat".
      const hgt=(b.seats||0)/hiSeats*(IH*0.36);
      if(hgt>0.5) seatBars+=`<rect x="${(xa+1).toFixed(1)}" y="${(BASE-hgt).toFixed(1)}"
        width="${Math.max(1,w-2).toFixed(1)}" height="${hgt.toFixed(1)}"
        fill="var(--bar-bg)" fill-opacity=".75"></rect>`;
      if(lo>0) grid+=`<line x1="${xa.toFixed(1)}" y1="${PT}" x2="${xa.toFixed(1)}"
        y2="${BASE}" stroke="var(--line)" stroke-width="1"></line>`;
      if(prevY!=null) stair+=`<line x1="${xa.toFixed(1)}" y1="${prevY.toFixed(1)}"
        x2="${xa.toFixed(1)}" y2="${y.toFixed(1)}" stroke="var(--accent)"
        stroke-width="1.4" stroke-opacity=".5"></line>`;
      stair+=`<line x1="${xa.toFixed(1)}" y1="${y.toFixed(1)}" x2="${xb.toFixed(1)}"
        y2="${y.toFixed(1)}" stroke="var(--accent)" stroke-width="2.4"></line>`;
      prevY=y;
      if(b.raw_lambda!=null){
        const yr=Y(b.raw_lambda);
        rawline+=`<line x1="${xa.toFixed(1)}" y1="${yr.toFixed(1)}" x2="${xb.toFixed(1)}"
          y2="${yr.toFixed(1)}" stroke="var(--violet)" stroke-width="1.5"
          stroke-dasharray="3 3"></line>`;
      }
      ribbon+=`<rect x="${xa.toFixed(1)}" y="${(BASE+8).toFixed(1)}"
        width="${w.toFixed(1)}" height="5" rx="2" fill="var(--line)"></rect>`;
      if(b.admits){
        const e=Math.min(b.admits[1],XMAX), xe=X(e);
        // The sliver: bounded below by the gate line, above by the step,
        // left by the band floor, right by the band's own lambda.
        green+=`<path d="M${xa.toFixed(1)},${Y(lo).toFixed(1)}
          L${xa.toFixed(1)},${y.toFixed(1)} L${xe.toFixed(1)},${y.toFixed(1)}
          L${xe.toFixed(1)},${Y(e).toFixed(1)} Z" fill="var(--green)"
          fill-opacity=".30" stroke="var(--green)" stroke-width="1.2"></path>`;
        ribbon+=`<rect x="${xa.toFixed(1)}" y="${(BASE+8).toFixed(1)}"
          width="${Math.max(2,xe-xa).toFixed(1)}" height="5" rx="2"
          fill="var(--green)"></rect>`;
      }
      hover+=`<rect x="${xa.toFixed(1)}" y="${PT}" width="${w.toFixed(1)}"
        height="${IH}" fill="transparent"><title>${sc.scope} ${bl(b.band)} 档 · ${b.seats} 座
服务 λ ${num(b.served)}　本账本 raw ${num(b.raw_lambda)}
${b.admits?"可买 ["+b.admits[0].toFixed(3)+", "+b.admits[1].toFixed(3)+")"
  :"空窗：λ "+num(b.served)+" ≤ 档下界 "+b.lower.toFixed(2)}</title></rect>`;
    });
    const yticks=[0.5,1.0].map(v=>`
      <line x1="${PL}" y1="${Y(v).toFixed(1)}" x2="${(W-PR).toFixed(1)}"
        y2="${Y(v).toFixed(1)}" stroke="var(--line)" stroke-width="1"
        stroke-dasharray="2 4"></line>
      <text x="${(PL-4).toFixed(1)}" y="${(Y(v)+3.5).toFixed(1)}" text-anchor="end"
        fill="var(--muted)" font-size="9.5">${v.toFixed(1)}</text>`).join("")
      +`<text x="2" y="${(PT-8).toFixed(1)}" fill="var(--muted)" font-size="9.5">λ</text>`;
    const xticks=[0.5,0.7,0.85,1.0].map(v=>`
      <text x="${X(v).toFixed(1)}" y="${(BASE+27).toFixed(1)}" text-anchor="middle"
        fill="var(--muted)" font-size="9.5">${v}</text>`).join("")
      +`<text x="${X(1.175).toFixed(1)}" y="${(BASE+27).toFixed(1)}"
        text-anchor="middle" fill="var(--muted)" font-size="9.5">≥1.1</text>
       <text x="${(W-PR).toFixed(1)}" y="${(BASE+39).toFixed(1)}" text-anchor="end"
        fill="var(--muted)" font-size="9.5">a = 篮子价 / 声称胜率 →</text>`;
    const diag=`<line x1="${X(0).toFixed(1)}" y1="${Y(0).toFixed(1)}"
      x2="${X(XMAX).toFixed(1)}" y2="${Y(XMAX).toFixed(1)}" stroke="var(--muted)"
      stroke-width="1.3" stroke-dasharray="5 4"></line>
      <text x="${(W-PR).toFixed(1)}" y="${(Y(XMAX)-5).toFixed(1)}"
        text-anchor="end" fill="var(--muted)" font-size="9.5">闸线 λ=a</text>`;
    const admitTxt=(sc.bands||[]).filter(b=>b.admits)
      .map(b=>`[${b.admits[0].toFixed(2)}, ${b.admits[1].toFixed(2)})`)
      .join(" ∪ ")||"整段空窗";
    const seatSum=(sc.bands||[]).reduce((a,b)=>a+(b.seats||0),0);
    const blindSeats=(sc.bands||[]).filter(b=>!b.admits)
      .reduce((a,b)=>a+(b.seats||0),0);
    return `<div class="card" style="margin-bottom:0">
      <div style="display:flex;align-items:baseline;gap:7px;flex-wrap:wrap">
        <b>${sc.scope}</b>
        <span class="muted" style="font-size:11.5px">阶段 ${sc.stages.join("/")}
          · 封顶 ${sc.ceiling.toFixed(2)} · ${seatSum} 座</span></div>
      <svg viewBox="0 0 ${W} ${H}" width="100%"
        style="height:auto;display:block;margin-top:2px" role="img"
        aria-label="${sc.scope} 的 λ 阶梯与闸线 λ=a，绿色是可买窗口">
        ${seatBars}${grid}${yticks}${green}${diag}${rawline}${stair}
        <line x1="${PL}" y1="${BASE}" x2="${(W-PR).toFixed(1)}" y2="${BASE}"
          stroke="var(--line)" stroke-width="1"></line>
        ${ribbon}${xticks}${hover}
      </svg>
      <div style="font-size:11.5px;margin-top:1px">可买
        <span class="mono up">${admitTxt}</span></div>
      <div class="muted" style="font-size:11.5px">空窗档坐过
        <b>${blindSeats}</b> 座（占 ${seatSum?pct(blindSeats/seatSum):"—"}）——
        这些席位往后一个也接不到。</div>
    </div>`;
  };
  const sw=(css,txt)=>`<span style="display:inline-flex;align-items:center;gap:5px">
    <i style="display:inline-block;width:17px;${css}"></i>${txt}</span>`;
  const legend=`<div class="muted" style="font-size:11.5px;display:flex;gap:6px 16px;
    flex-wrap:wrap;margin-top:7px">
    ${sw("height:0;border-top:2.4px solid var(--accent)","当前服务 λ（阶梯）")}
    ${sw("height:0;border-top:1.5px dashed var(--violet)","本账本 raw λ")}
    ${sw("height:0;border-top:1.3px dashed var(--muted)","闸线 λ=a")}
    ${sw("height:9px;border-radius:2px;background:var(--pill-green-bg);border:1px solid var(--green)","可买窗口＝阶梯高过闸线的那一段")}
    ${sw("height:9px;border-radius:2px;background:var(--bar-bg);border:1px solid var(--line)","席位分布（这档历史上坐过多少）")}
  </div>`;

  // The worked example is picked off the book rather than written down, so
  // it cannot go stale the way a hardcoded band label does: the blind band
  // with the most seats is the one a reader is most likely to be looking
  // for, and the widest raw-over-floor gap is the promotion backlog's own
  // best case.
  const all=[];
  scopes.forEach(sc=>(sc.bands||[]).forEach(b=>all.push({scope:sc.scope,b:b})));
  const blind=all.filter(x=>!x.b.admits&&x.b.seats>0)
    .sort((p,q)=>q.b.seats-p.b.seats)[0];
  const backlog=all.filter(x=>!x.b.admits&&x.b.raw_lambda!=null
      &&x.b.raw_lambda>x.b.lower)
    .sort((p,q)=>(q.b.raw_lambda-q.b.served)-(p.b.raw_lambda-p.b.served))[0];
  const plain=(()=>{
    if(!blind) return "";
    const b=blind.b;
    let s=`<b>白话版</b>：λ 读作「这一档的声称，历史上真到账几成」。
      买得下手的条件只有一句 —— <b>到账的比例，要高过要价的比例</b>，
      也就是这一档的 λ 得高过它自己的<b>起点</b>。看图：蓝阶梯掉到斜虚线以下的档，
      绿色就没了，模型再准也一张票发不出。眼下最典型的是
      <b>${blind.scope} 的 ${bl(b.band)} 档</b>：服务 λ <span class="mono">${num(b.served)}</span>
      看着不低，可这档从 <span class="mono">${b.lower.toFixed(2)}</span> 起步，
      ${num(b.served)} &lt; ${b.lower.toFixed(2)}，整档空窗 ——
      它历史上坐过 ${b.seats} 个席位。`;
    if(backlog){
      const q=backlog.b;
      s+=(backlog.scope===blind.scope&&q.band===b.band)
        ? `而同一档的 raw（紫点线）已经是 <span class="mono">${num(q.raw_lambda)}</span>，
           高过 ${q.lower.toFixed(2)} 了 —— <b>差的不是模型准不准，是这张表还没晋级</b>。`
        : `另一处：<b>${backlog.scope} 的 ${bl(q.band)} 档</b>服务
           <span class="mono">${num(q.served)}</span> 空窗，raw 却已到
           <span class="mono">${num(q.raw_lambda)}</span>（＞下界 ${q.lower.toFixed(2)}）——
           紫点线抬过了斜虚线、蓝实线没跟上的地方，就是待晋级的量。`;
    }
    return s;
  })();

  const bounds=(E.band_bounds||[]).map(b=>b==null?"∞":b).join(" / ");
  const chips=`<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:9px">
    <span class="pill ${E.mode==="on"?"green":"amber"}">开关 ${E.mode||"?"}</span>
    <span class="pill">入选门槛 ${E.minimum_seats_per_band} 座</span>
    <span class="pill">前后半以 ${E.split_at_event_date||"—"} 为界</span>
    <span class="pill">分档边界 <span class="mono">${bounds}</span></span>
  </div>`;

  // The second axis is a partition of one number, so it draws as one bar.
  // Widths are the real widths on e in [-0.05, 0.20]: the band that serves
  // 1.00 is a sliver, and a legend that made it look like a third of the
  // line would be arguing the opposite of what the clause says.
  // Only the interval goes inside the segment; the meaning goes underneath.
  // Labelling in place looked tidier at 1180px and clipped its own words at
  // 620 -- the thin band is a twentieth of the line, and no phrase fits in
  // a twentieth of anything.
  const seg=(flex,bg,fg,txt,tip)=>`<div title="${tip}" style="flex:${flex};
    background:${bg};color:${fg};display:flex;align-items:center;
    justify-content:center;padding:0 3px;white-space:nowrap;overflow:hidden">${txt}</div>`;
  const key=(bg,bd,txt)=>`<span style="display:inline-flex;align-items:center;gap:5px">
    <i style="width:9px;height:9px;border-radius:2px;background:${bg};
      border:1px solid ${bd};display:inline-block"></i>${txt}</span>`;
  const thermo=E.claim_edge_thermometer==="on"?`<div style="margin-top:12px">
    <div style="font-size:12.5px"><b>λ 有第二根轴</b>（宪章 97，2026-09-06 起服务）：按<b>绝对</b>声称边际
      <span class="mono">e = 抬后声称 − 全含价</span> 分段，<b>只准抬不准压</b> ——
      一致度轴已更信任的档不被它压低。</div>
    <div style="display:flex;height:26px;margin-top:5px;border:1px solid var(--line);
      border-radius:7px;overflow:hidden;font-size:11.5px;font-weight:600">
      ${seg(20,"var(--pill-red-bg)","var(--pill-red-fg)","e ≤ 0","声称还不如价格贵，闸不放行")}
      ${seg(20,"var(--pill-green-bg)","var(--pill-green-fg)",
        "0–"+E.claim_edge_thin_band_max,
        "体温计薄档轴：该档实测兑现 1.036，封顶 "+E.claim_edge_thin_band_lambda.toFixed(2))}
      ${seg(60,"var(--bar-bg)","var(--muted)","&gt; "+E.claim_edge_thin_band_max,
        "厚边际窗口仍由一致度轴定 λ")}
    </div>
    <div class="muted" style="font-size:11.5px;margin-top:4px;display:flex;
      gap:5px 15px;flex-wrap:wrap">
      ${key("var(--pill-red-bg)","var(--red)","不放行（声称还不如价格贵）")}
      ${key("var(--pill-green-bg)","var(--green)","λ 直接服务 <b>"+E.claim_edge_thin_band_lambda.toFixed(2)+"</b>（体温计薄档轴）")}
      ${key("var(--bar-bg)","var(--line)","回一致度轴（上面的阶梯）")}
    </div>
    <div class="muted" style="font-size:11.5px;margin-top:3px">
      封顶 ${E.claim_edge_thin_band_lambda.toFixed(2)} —— 信它，但不比它信自己更多。
      行上记 <span class="mono">effective_edge_lambda_source</span> 说明是哪根轴买的这一单。
      注意 <span class="mono">1−a</span> 是<b>相对</b>边际、是上面那根轴的补，
      <b>不是</b>这里的 e，两者不可混引。</div></div>`:"";

  // Charter 97's own instrument.  The clause retires itself when the seats
  // this axis opened clear the hurdle and realise under one; that can only
  // be judged if the seats are attributable, so the gate stamps the source
  // and this counts it.  Empty means not yet measured, never "bought none".
  const axisUsage=()=>{
    const U=D.lambda_axis_usage||{};
    if(!U.available) return "";
    const seated=U.seated_by_source||{};
    const considered=U.considered_by_source||{};
    const total=Object.values(seated).reduce((a,b)=>a+b,0);
    const label={agreement_band:"一致度轴（上面的阶梯）",
      claim_edge_thin_band:"体温计薄档轴"};
    const tone={agreement_band:"var(--accent)",
      claim_edge_thin_band:"var(--violet)"};
    if(!total) return `<p class="muted" style="margin-top:11px;font-size:12.5px">
      <b>λ 来源分布</b>（宪章 97 回收条件的仪表）：${U.since} 起扫到
      ${U.scanned_decisions} 份带来源字段的计划，<b>尚无入座记录</b>。
      来源字段 2026-09-06 才加，此前的计划不带；空＝还没量到，<b>不是</b>该轴没买到东西。</p>`;
    const items=Object.entries(seated).sort((a,b)=>b[1]-a[1]);
    const bar=items.map(([k,v])=>`<div title="${label[k]||k} 入座 ${v} 次"
      style="flex:${v};background:${tone[k]||"var(--muted)"};display:flex;
      align-items:center;justify-content:center;color:var(--bg);font-size:11.5px;
      font-weight:700">${(v/total*100).toFixed(0)}%</div>`).join("");
    const rows=items.map(([k,v])=>`<span style="display:inline-flex;align-items:center;gap:5px">
      <i style="width:9px;height:9px;border-radius:2px;display:inline-block;
        background:${tone[k]||"var(--muted)"}"></i>${label[k]||k}
      <b>${v}</b> 次入座${considered[k]?`（考虑 ${considered[k]} 次 · ${
        (v/considered[k]*100).toFixed(0)}% 落座）`:""}</span>`).join("");
    return `<div style="margin-top:12px">
      <div style="font-size:12.5px"><b>λ 来源分布</b>（宪章 97 回收条件的仪表，${
        U.since} 起 ${U.scanned_decisions} 份计划，${total} 次入座）</div>
      <div style="display:flex;height:22px;margin-top:5px;border-radius:6px;
        overflow:hidden;border:1px solid var(--line)">${bar}</div>
      <div class="muted" style="font-size:11.5px;margin-top:4px;display:flex;
        gap:6px 16px;flex-wrap:wrap">${rows}</div>
      <div class="muted" style="font-size:11.5px;margin-top:3px">
        体温计轴的席位攒够 ${E.minimum_seats_per_band} 座后若兑现跌破 1.0，
        按宪章 97 应收回该轴。</div></div>`;
  };

  const block=sc=>{
    const isP1=sc.scope==="P1";
    const rows=(sc.bands||[]).map(b=>{
      // A served value the book disagrees with is the promotion backlog;
      // whether it may be closed is the early/late column's answer, not
      // this one's.
      const gap=b.fitted_capped==null?null:b.fitted_capped-b.served;
      const mark=!b.candidate?["—",""]
        :b.stable?["可晋级","up"]
        :["证据不稳",""];
      const win=b.admits
        ? `<span class="mono">[${b.admits[0].toFixed(3)}, ${b.admits[1].toFixed(3)})</span>`
        : `<span class="down">空窗</span>`;
      return `<tr>
        <td class="mono">${b.band}</td>
        <td class="mono">${num(b.served)}</td>
        <td>${win}</td>
        <td class="mono ${gap==null?"":(gap<0?"down":"up")}">${num(b.raw_lambda)}</td>
        <td class="mono">${num(b.fitted_capped)}</td>
        <td>${b.seats}${b.admissible?"":" ⚠"}</td>
        <td class="mono">${num(b.early_lambda)} / ${num(b.late_lambda)}</td>
        <td class="mono">${pct(b.return)}</td>
        <td class="${mark[1]}">${mark[0]}</td></tr>`;
    }).join("");
    const st=sc.store;
    const note=isP1
      ? `P1 的形状是 P0 的镜像：市场越不同意，兑现越超过声称、回报越高，因为 P1 的边际是<b>观测</b>不是形状。
         服务值封顶 1.00 —— 豁免的意思是「信它」，不是「比它自己还信它」。`
      : `P0 / P0-M 是倒 U：市场最不同意的那一带，五分之四的声称从未到账。
         <b>能买的不是"λ 高的档"，是"λ 高过自己下界的档"</b> —— 上图绿色的那几段。`;
    return `<h3 style="margin-top:16px">${sc.scope} 档（阶段 ${sc.stages.join(" / ")}，封顶 ${sc.ceiling.toFixed(2)}）</h3>
      <div class="card" style="margin-bottom:6px">${note}</div>
      <table><thead><tr>
        <th>一致度带 a=价/声称</th><th>当前服务 λ</th>
        <th title="闸的判据化简后是 λ(a)>a，所以这一档只放行 [档下界, 该档自己的 λ)">可买区间</th>
        <th>本账本 raw λ</th>
        <th>封顶后</th><th>座位</th><th>前半 / 后半</th><th>该带回报</th><th>状态</th>
      </tr></thead><tbody>${rows}</tbody></table>
      <div class="muted mono" style="margin-top:4px">${
        st?`store 已激活 ${st.sha256} · ${st.fitted_at_utc} · ${st.seat_count} 座`
          :"store 尚无该档的激活行（明日 refit 起逐日记录）"
      }</div>
      ${dqShadow(sc)}`;
  };
  // The shadow axis: same band, split by the market's own move on the
  // window over the hours before the seat.  A cell shows its fitted λ only
  // when it clears the store's admission rule; otherwise the seat count
  // alone, so a thin cell reads as thin rather than as a number.
  const dqShadow=sc=>{
    const sh=sc.dq_shadow;
    if(!sh) return `<div class="muted" style="margin-top:6px">Δq 影子：尚无拟合行（下一轮 refit 起记录）。</div>`;
    const tiers=[["down","跌 ≤−"+E.dq_shadow_threshold],["steady","平"],["up","涨 ≥+"+E.dq_shadow_threshold],["no_prior","无先验"]];
    const rows=(sh.evidence||[]).map(b=>{
      const cells=tiers.map(([t])=>{
        const c=(b.tiers||{})[t]||{};
        if(!c.seats) return `<td class="mono muted">—</td>`;
        const lam=c.fitted_lambda;
        const cls=lam==null?"":(lam<b.served_lambda-0.1?"down":(lam>b.served_lambda+0.1?"up":""));
        return `<td class="mono ${cls}">${lam==null?`<span class="muted">(${c.seats}座 raw ${num(c.raw_lambda)})</span>`:`${num(lam)} <span class="muted">(${c.seats}座)</span>`}</td>`;
      }).join("");
      const bound=b.band_upper_bound==null?"≥0.85":"<"+b.band_upper_bound;
      return `<tr><td class="mono">${bound}</td><td class="mono">${num(b.served_lambda)}</td>${cells}</tr>`;
    }).join("");
    return `<div class="muted" style="margin-top:10px">Δq 影子轴（视野 ${E.dq_shadow_horizon_hours}h，同一带按席位前市场对该窗口的移动分格；只记账不服务；拟合 ${sh.fitted_at_utc} · ${sh.seat_count} 座）</div>
      <table><thead><tr><th>一致度带</th><th>服务 λ</th>${tiers.map(([,l])=>`<th>${l}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table>`;
  };

  return `<h2 style="margin-top:30px">有效边际 λ 表（按阶段）</h2>
    <div class="card">
      <div class="muted" style="font-size:12.5px;margin-bottom:9px">
        定注读的是 <span class="mono">λ(a)·p</span> 而不是 p（宪章 68），闸只放行
        <b><span class="mono">λ(a) &gt; a</span></b> 的窗口（宪章 96）—— λ 是<b>阶梯</b>，
        所以每一档只买得到 <span class="mono">[档下界, 该档自己的 λ)</span>，
        <b>下图绿色的就是它</b>；λ 掉到本档下界以下的档整段空窗。</div>
      <div class="grid g3">${scopes.map(curve).join("")}</div>
      ${legend}
      <div class="why" style="margin-top:10px">${plain}</div>
      ${chips}
      ${thermo}
      ${axisUsage()}
      <details class="note" style="margin-top:10px">
        <summary>展开条文原文 · 三列分别在答什么 · 2026-09-06 为什么拆档</summary>
        <div style="font-size:13.4px">
          <p style="margin:0 0 8px">a = 篮子价格 / 声称胜率，λ = 历史上这声称真正到账的比例。
          <b>服务列＝库里此刻激活的那张表</b>（宪章 68(c) 起机器自晋级，
          内核常量只在某 scope 从未激活过时兜底）；raw 列是本账本说它该是多少，
          两者的差就是待晋级的量，能不能晋级由前半/后半那一列决定 ——
          两半不一致的带还没挣到一个常量。</p>
          <p style="margin:0">宪章 96（2026-09-06）：闸判的是折后声称减价格为正，化简后就是
          <span class="mono">λ(a) &gt; a</span>。按这条在 <span class="mono">0.80</span> 与
          <span class="mono">0.95</span> 两处拆档，救回被平均掉的半档，
          并把顶档那把"用自己平均值切自己"的刀换成常量边界。</p>
        </div>
      </details>
    </div>
    ${scopes.map(block).join("")}`;
}


// Arm B (2026-09-06): the convergence claim lift, in shadow.  Left half is
// what production's enumeration journaled; right half is the measurement
// the arm rests on, recomputed on this book through the kernel's own state
// function.  Nothing is served by it until a person promotes the arm.
function convergenceClaim(){
  const C=D.convergence_claim||{};
  const num=(v,d)=>v==null?"—":Number(v).toFixed(d==null?2:d);
  const pct=v=>v==null?"—":Number(v).toFixed(0)+"%";
  const receipts=(C.receipts||[]).map(r=>`<tr>
      <td class="mono">${r.stage}</td><td>${r.decisions}</td><td>${r.with_evidence}</td>
      <td>${r.candidates}</td><td class="${r.would_admit?"up":""}">${r.would_admit}</td><td>${r.admitted_by_lift}</td></tr>`).join("");
  const examples=(C.examples||[]).map(e=>`<tr>
      <td class="mono">${e.station_id} ${e.event_date} ${e.stage}</td><td class="mono">${e.at}</td>
      <td class="mono">${(e.buckets||[]).join(" · ")}</td><td class="mono">${num(e.claim)}</td>
      <td class="mono">${num(e.price)}</td><td class="mono">${num(e.agreement)}</td>
      <td class="mono">${e.claim_rise==null?"—":(e.claim_rise>=0?"+":"")+num(e.claim_rise)} / ${e.price_fall==null?"—":"−"+num(e.price_fall)}</td>
      <td class="mono ${e.lifted_edge>0?"up":""}">${num(e.lifted_edge,3)}</td><td>${e.flips_gate?"会放行":"仍拒"}</td></tr>`).join("");
  const evidence=(C.evidence||[]).map(e=>`<tr>
      <td>${e.cell}</td><td>${e.n}</td><td>${e.stations}</td><td class="mono">${pct(e.hit_pct)}</td>
      <td class="mono">${pct(e.claimed_pct)}</td>
      <td class="mono ${e.realised_over_claimed>1.15?"up":(e.realised_over_claimed<0.9?"down":"")}">${num(e.realised_over_claimed)}</td>
      <td class="mono ${e.unit_return_pct>0?"up":"down"}">${e.unit_return_pct==null?"—":(e.unit_return_pct>=0?"+":"")+num(e.unit_return_pct,0)+"%"}</td></tr>`).join("");
  return `<h2 style="margin-top:30px">双向收敛系数（B 臂，影子）</h2>
    <div class="card">
      模型把窗口调高 ≥${num(C.min_claim_rise)}、市场把同一窗口的卖一合计压低 ≥${num(C.min_price_fall)}（视野 ${C.horizon_hours}h），
      而市场仍比模型贵（a=价/声称 ≥${num(C.min_agreement)}）——这样的窗口实测兑现 1.3–1.4 倍声称，却被正 EV 闸挡住。
      本臂在闸门前把这种窗口的声称乘 <span class="mono">${num(C.factor)}</span>。当前模式 <span class="mono">${C.mode}</span>：
      影子只记账不放行；阶段限 ${(C.stages||[]).join("/")}，P0 开仓无先验不参与。
      与宪章 68 的 P1 封顶（信它不比它更信它）正面冲突，转正须点名裁定。
    </div>
    <h3 style="margin-top:12px">生产回执（近 7 天决策行）</h3>
    <table><thead><tr><th>阶段</th><th>决策</th><th>有先验</th><th>收敛候选窗</th><th>抬价会放行</th><th>已因抬价放行</th></tr></thead>
    <tbody>${receipts||'<tr><td colspan="6" class="muted">尚无带回执的决策行（常驻重启后开始记录）。</td></tr>'}</tbody></table>
    ${examples?`<h3 style="margin-top:12px">最近候选窗</h3>
    <table><thead><tr><th>站日/阶段</th><th>时刻</th><th>窗口</th><th>声称</th><th>价</th><th>a</th><th>Δ声称 / Δ价</th><th>抬后 edge</th><th>闸门</th></tr></thead><tbody>${examples}</tbody></table>`:""}
    <h3 style="margin-top:12px">证据格（近 ${C.evidence_days} 天已结算站日，P0-M/P1 每 4 小时抽样，最大质量三桶窗，研究口径）</h3>
    <table><thead><tr><th>阶段 / 格</th><th>n</th><th>站</th><th>命中</th><th>声称</th><th>实现÷声称</th><th>按卖一全买</th></tr></thead>
    <tbody>${evidence||'<tr><td colspan="7" class="muted">无样本。</td></tr>'}</tbody></table>`;
}

// Rendered inside the band-table module, not as its own section (user
// ruling, 2026-09-01): both tables answer "when the model claims X, what
// actually happens", one keyed by claimed probability, one by claimed
// edge, and reading them two tabs apart hid that they are the same audit.
function edgeBandHealth(){
  const E=D.edge_band_health||{};
  const rows=E.bands||[];
  if(!rows.length)
    return `<h2 style="margin-top:30px">声称边际体温计（edge 带 × 兑现）</h2>
    <div class="card"><span class="dot green"></span> 无腿级数据。</div>`;
  const pct=v=>v==null?"—":(v*100).toFixed(0)+"%";
  const mult=v=>v==null?"—":"×"+v.toFixed(2);
  const STAGES=E.stages||[];
  const tr=r=>{
    const a=r.all,rc=r.recent;
    // The sick bands are the big-edge ones; paint the band label by the
    // filled-side realisation so the eye finds the fever line.
    const x=a.realize_filled_x;
    const state=x==null?["—",""]
      :x>=0.9?["✅ 健康",""]
      :x>=0.5?["😐 偏虚",""]
      :x>=0.2?["🤒 发烧","down"]
      :["🔥 重烧","down"];
    // Per stage, beside the pooled reading rather than instead of it: the
    // pooled number is what the reader has been looking at, and the split
    // only means something next to what it corrects.
    const byStage=STAGES.map(st=>{
      const c=(r.by_stage||{})[st];
      if(!c||!c.n) return `<td class="muted">—</td>`;
      const v=c.realize_filled_x;
      const cls=v==null?"muted":(v>=0.9?"":(v>=0.2?"":"down"));
      return `<td class="${cls}" title="${st}：${c.n} 腿，声称均值 ${
        pct(c.claimed_mean)}${c.realize_filled_x==null
          ?"，样本不足 10 条，不出兑现比":""}"><b>${mult(v)}</b><span
        class="muted" style="font-size:11px"> ${c.n}</span></td>`;
    }).join("");
    return `<tr>
      <td class="mono ${state[1]}">${r.band}</td>
      <td class="${state[1]}">${state[0]}</td>
      <td>${a.n}</td><td>${pct(r.share_all)}</td><td>${pct(a.fill_rate)}</td>
      <td>${pct(a.claimed_mean)}</td>
      <td class="${state[1]}">${mult(x)}</td>
      ${byStage}
      <td>${mult(a.realize_unfilled_x)}</td>
      <td>${pct(r.share_recent)}</td>
      <td>${mult(rc.realize_filled_x)}</td></tr>`;
  };
  // The fever curve drawn as an arch, with the geometry carrying the
  // numbers rather than decorating them (user ruling, 2026-09-01 -- the
  // first version drew seven equal wedges, so the silhouette was ornament
  // and only the colour and the printed digits said anything).
  //
  // Radius is the reading: each band's wedge grows outward from the
  // springing line by its realisation multiple, so the arch's own outline
  // IS the fever curve, and the dashed ×1.00 ring is the honesty line --
  // a wedge that reaches past it delivered what it claimed.  Angular slots
  // stay equal so the left-to-right trend reads on an even axis; leg share
  // gets its own channel as the thickness of the footing under each wedge
  // (how much of the book rests on that band).  The white contour is the
  // recent window on the same radial scale, so charter 62's treatment is
  // visible as the line standing outside the fill it sits on.
  const RAMP=[[0,176,42,55],[0.2,224,49,49],[0.35,232,89,12],
    [0.55,240,160,32],[0.8,148,181,39],[1.0,47,158,68],[1.5,26,127,55]];
  const tone=x=>{
    if(x==null) return "var(--soft)";
    const v=Math.max(0,Math.min(1.5,x));
    let a=RAMP[0],b=RAMP[RAMP.length-1];
    for(let i=0;i<RAMP.length-1;i++)
      if(v>=RAMP[i][0]&&v<=RAMP[i+1][0]){a=RAMP[i];b=RAMP[i+1];break;}
    const f=b[0]===a[0]?0:(v-a[0])/(b[0]-a[0]);
    const c=k=>Math.round(a[k]+(b[k]-a[k])*f);
    return `rgb(${c(1)},${c(2)},${c(3)})`;
  };
  const CX=450,CY=366,R0=124,RMAX=302,XMAX=1.6;
  const rad=x=>R0+Math.max(0,Math.min(XMAX,x))/XMAX*(RMAX-R0);
  const PT=(r,t)=>{const a=(180-t)*Math.PI/180;
    return [(CX+r*Math.cos(a)).toFixed(1),(CY-r*Math.sin(a)).toFixed(1)];};
  const pt=(r,t)=>PT(r,t).join(" ");
  const wedge=(t0,t1,ri,ro)=>`M ${pt(ro,t0)} A ${ro} ${ro} 0 0 1 ${pt(ro,t1)}
    L ${pt(ri,t1)} A ${ri} ${ri} 0 0 0 ${pt(ri,t0)} Z`;
  const ring=(r,t0,t1)=>`M ${pt(r,t0)} A ${r} ${r} 0 0 1 ${pt(r,t1)}`;
  // Weighted by claimed mass, which is the only weight under which the
  // ratio stays the same quantity the per-band column shows.
  const overall=key=>{
    let mass=0,hits=0;
    rows.forEach(r=>{const s=r[key]||{};
      const n=s.n_settled||s.n||0,c=s.claimed_mean,x=s.realize_filled_x;
      if(!n||c==null||x==null) return;
      mass+=n*c;hits+=n*c*x;});
    return mass>0?hits/mass:null;
  };
  const ovAll=overall("all"),ovRec=overall("recent");
  const feverShare=rows.reduce((a,r)=>a+((r.all.realize_filled_x!=null
    &&r.all.realize_filled_x<0.5)?(r.share_all||0):0),0);
  const step=180/rows.length;
  const maxShare=Math.max(...rows.map(r=>r.share_all||0))||1;
  const parts=rows.map((r,i)=>{
    const t0=i*step+1.4,t1=(i+1)*step-1.4,tm=(t0+t1)/2;
    const xa=r.all.realize_filled_x,xr=r.recent.realize_filled_x;
    // A band that realises nothing still gets a sliver, so its slot reads
    // as "measured and zero" rather than as an empty slot with no data.
    const ro=xa==null?R0+3:Math.max(rad(xa),R0+2);
    const foot=R0-7-(r.share_all||0)/maxShare*46;
    const [lx,ly]=PT(320,tm);
    const anchor=tm<70?"end":(tm>110?"start":"middle");
    // The footing carries leg share; it is the same colour as the wedge at
    // low opacity so the eye reads it as one pier rather than two shapes.
    const pier=`<path d="${wedge(t0,t1,foot,R0-7)}" fill="${tone(xa)}"
      opacity=".34"><title>${h(r.band)}：占全部腿 ${pct(r.share_all)}（${
      r.all.n} 腿）</title></path>`;
    const body=`<path d="${wedge(t0,t1,R0,ro)}" fill="${tone(xa)}"
      ><title>${h(r.band)}：全期兑现 ${mult(xa)} · ${r.all.n} 腿 · 占 ${
      pct(r.share_all)}</title></path>`;
    const inset=Math.min(1,(t1-t0)*0.08);
    const recent=xr==null?"":`<path d="${ring(rad(xr),t0+inset,t1-inset)}" fill="none"
      stroke="var(--text)" stroke-width="2.6" stroke-linecap="round"
      opacity=".9"><title>${h(r.band)}：近7天兑现 ${mult(xr)}（${
      r.recent.n} 腿）</title></path>
      <circle cx="${PT(rad(xr),tm)[0]}" cy="${PT(rad(xr),tm)[1]}" r="3"
      fill="var(--text)"/>`;
    return pier+body+recent+`<text x="${lx}" y="${ly}"
      text-anchor="${anchor}" font-size="12.5" fill="var(--text)">${h(r.band)}
      <tspan font-weight="700">${mult(xa)}</tspan>
      <tspan x="${lx}" dy="15" font-size="11.5" fill="var(--muted)">${
      pct(r.share_all)} 的腿</tspan></text>`;
  }).join("");
  // The empty track keeps the whole inverted U on the page even where the
  // reading is short: without it the silhouette is only as tall as the
  // data, and a band that realises almost nothing loses the column that
  // ties it to its own label.
  const tracks=rows.map((r,i)=>{
    const t0=i*step+1.4,t1=(i+1)*step-1.4;
    return `<path d="${wedge(t0,t1,R0,RMAX)}" fill="var(--line)"
      opacity=".38"/>`;
  }).join("");
  const guide=x=>`<path d="${ring(rad(x),0,180)}" fill="none"
    stroke="var(--muted)" stroke-width="1" stroke-dasharray="2 6"
    opacity=".55"/>`;
  const dir=(ovAll!=null&&ovRec!=null)
    ?(ovRec>ovAll+0.01?["↑ 在退烧","var(--up)"]
      :(ovRec<ovAll-0.01?["↓ 还在烧","var(--down)"]:["＝ 持平","var(--muted)"]))
    :["","var(--muted)"];
  const arc=`<div class="scroll"><svg viewBox="0 0 900 442" width="100%"
    style="min-width:560px;max-width:900px;height:auto;display:block;
    margin:4px auto 0"
    role="img" aria-label="声称边际与兑现的拱形图：自认便宜越大，兑现越崩">
    ${tracks}${guide(0.5)}${guide(1.5)}
    ${parts}
    <path d="${ring(rad(1),0,180)}" fill="none" stroke="var(--text)"
      stroke-width="1.4" stroke-dasharray="6 4" opacity=".75"/>
    <text x="${(CX-rad(1)).toFixed(1)}" y="${CY+15}" text-anchor="middle"
      font-size="11.5" fill="var(--text)">×1.00 说到做到</text>
    <line x1="${(CX-RMAX).toFixed(1)}" y1="${CY}" x2="${(CX+RMAX).toFixed(1)}"
      y2="${CY}" stroke="var(--line)"/>
    <text x="${CX}" y="${CY-58}" text-anchor="middle" font-size="15"
      font-weight="700" fill="var(--text)">自认便宜越大</text>
    <text x="${CX}" y="${CY-38}" text-anchor="middle" font-size="15"
      font-weight="700" fill="var(--text)">兑现越崩</text>
    <text x="${CX}" y="${CY-15}" text-anchor="middle" font-size="11.5"
      fill="var(--muted)">整体 ${mult(ovAll)}${ovRec==null?""
      :` · 近7天 <tspan fill="${dir[1]}">${mult(ovRec)} ${dir[0]}</tspan>`}</text>
    <text x="${CX}" y="${CY+38}" text-anchor="middle" font-size="11.5"
      fill="var(--muted)">→ 自认占的便宜（声称 − 卖价）从小到大 →</text>
    <text x="${(CX-RMAX).toFixed(1)}" y="${CY+60}" font-size="12"
      fill="var(--green)">← 没占多少便宜 · 反而最诚实</text>
    <text x="${(CX+RMAX).toFixed(1)}" y="${CY+60}" text-anchor="end"
      font-size="12" fill="var(--red)">自认占了大便宜 · 兑现最崩 →</text>
  </svg></div>
  <div class="muted" style="margin-top:2px;text-align:center;font-size:11.5px">
    <b>怎么读这张拱</b>：扇形<b>伸得越远＝兑现越好</b>（长度就是「×」这个数）；
    越过<b>虚线圈＝说到做到</b>，缩在圈内＝声称打了折。
    白线是<b>近7天</b>，站在填色外面说明这一档在退烧。
    拱脚<b>越厚＝这一档压的腿越多</b>。颜色只是同一个数的冷暖复述：
    <span style="color:#2f9e44">绿＝健康</span> ·
    <span style="color:#f0a020">黄＝偏虚</span> ·
    <span style="color:#e8590c">橙＝发烧</span> ·
    <span style="color:#e03131">红＝重烧</span>。
    ${feverShare>0?`当前 <b>${pct(feverShare)}</b> 的腿压在发烧及以上（×&lt;0.5）的档里。`:""}</div>`;
  return `<h2 style="margin-top:30px">声称边际体温计（edge 带 × 兑现）</h2>
  <div class="card">
  ${arc}
  <p class="note" style="margin-top:12px"><b>这张表回答一个问题：模型觉得"捡到的便宜"是真是假。</b>
  每条腿有两个数——模型信它多少（声称）、市场卖它多少（卖价），差值＝模型自认占的便宜，
  按大小分七档。"兑现"列＝买到后真值实际落桶的比例是声称的几成：×1.0 说得准，
  ×0.45 自估打了四五折，×0.07 声称基本是错的。规律从上往下一眼可见：
  <b>自认便宜越大，市场越乐意卖，兑现越崩——"太好的价格"本身是警报</b>。
  <b>2026-09-06 起这张表不再只是体温计</b>（宪章 97）：最薄那一档
  <span class="mono">e ≤ 0.05</span>（实测兑现 ×1.036）已<b>转正服务</b>，
  作有效边际 λ 的<b>第二根轴</b> —— 落在该档的窗口 λ 直接服务 1.00（封顶，不用实测的 1.036），
  且<b>只准抬不准压</b>。它不是第二道闸：闸仍只有"折后声称 &gt; 价格"一个判据，
  变的是薄档窗口的 λ 由这根轴给出。<b>其余各档仍只是仪表</b>，
  早先"按 edge≥0.30 拒窗"的硬闸版本已被账本否决（剔 2 条即翻号），不要再提。
  <b>三条代价照原样记在这里，不软化</b>：①闸的 λ 不再是一致度的一元函数；
  ②回测增量只 2 席、优势基本由 08-27 SBGR 单张票决定，对 9/8 在盘口移动前增量为零；
  ③回测人口是删失的 —— 那 20 席全是被一致度轴放进来才结算的，
  上线后会放行历史上从没开过的窗口，影响面大于 +2 席、方向未知。
  回收条件：该轴自己的席位攒够 12 席后若兑现跌破 1.0 即收回（尚未自动化，需人工复核）。
  "近7天"两列仍是宪章62校准修复的疗效仪表，起效的样子＝发烧行（黄）占比缩小、兑现回升。样本&lt;10 显 —。
  <b>按阶段分开看</b>（用户裁定 2026-09-02）：同一个"声称占 30% 便宜"，
  P0 只有模型，P1 已经看过当天几小时实测气温——不是同一种声称。
  合并后 ≥30% 档兑现 ×0.25，拆开是 P0 ×0.07 与 P1 ×0.35，
  差五倍，那个 ×0.25 哪个阶段都不成立。P0-M 每档只有几条腿，
  多数格子按十条门槛留空，这就是"这条车道太薄读不出来"的诚实写法。
  近7天窗起点 ${E.recent_floor||"—"}，腿数 ${E.totals&&E.totals.recent_legs||0}/${E.totals&&E.totals.legs||0}
  （${(E.stages||[]).map(st=>`${st} ${(E.stage_totals||{})[st]||0}`).join(" · ")}）。</p>
  <table><thead><tr><th>自认便宜多少</th><th>状态</th><th>腿数</th><th>占比</th>
  <th>市场肯卖率</th><th>模型声称</th><th>买到后兑现<br><span class="muted">全部阶段</span></th>
  ${(E.stages||[]).map(st=>`<th title="${st} 阶段单独看；括号里是该档该阶段的腿数">${
    st}<br><span class="muted">兑现·腿数</span></th>`).join("")}
  <th>没买到的兑现</th>
  <th>近7天占比</th><th>近7天兑现</th></tr></thead>
  <tbody>${rows.map(tr).join("")}</tbody></table></div>`;
}

function windowDivergence(){
  const W=D.window_divergence||{};
  const rows=W.flagged||[];
  const ths=W.thresholds||[];
  if(!ths.length)
    return `<h2>分布与持仓分家</h2>
    <div class="card"><span class="dot green"></span> 没有可评估的站日。</div>`;
  const pct=v=>v==null?"—":(v*100).toFixed(0)+"%";
  const thRows=ths.map(t=>`<tr>
    <td class="mono">&gt;${pct(t.threshold)}</td>
    <td>${t.hot_n}</td><td>${pct(t.hot_hit_rate)}</td>
    <td class="${t.hot_pnl>=0?"up":"down"}">${t.hot_pnl>=0?"+":""}${t.hot_pnl.toFixed(1)}</td>
    <td>${t.cold_n}</td><td>${pct(t.cold_hit_rate)}</td>
    <td class="${t.cold_pnl>=0?"up":"down"}">${t.cold_pnl>=0?"+":""}${t.cold_pnl.toFixed(1)}</td></tr>`).join("");
  return `<h2>分布与持仓分家</h2>
  <p class="sub">每个已结算的持仓站日，取<b>持仓桶还活着的最后一次概率重算</b>
  （持仓桶质量仍高于 5%，即加仓还来得及的最后时刻），量它有多少后验质量落在持仓窗之外。
  共评估 ${W.evaluated_station_days||0} 个站日，其中末态窗外质量超过 40% 的
  ${rows.length} 个——逐站日明细不在此列出，只按阈值切分看总账。</p>
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
  <tbody>${thRows}</tbody></table></div>`;
}



function consolidation(){
  const S=D.selection||{};
  const cons=(S.consolidation_lights||{});
  if(!cons.available) return "";
  const consRule=cons.rule||{};
  const LAMP={stable:"稳",sharp:"尖"};
  const LAMPDEF={stable:"中心稳：中心桶连续 3 次未变",
    sharp:"分布尖：头两桶质量 ≥60%"};
  const lamp=(on)=>on?`<span class="down">●</span>`:`<span class="muted">○</span>`;
  const fmtRoi=(v)=>v==null?"—":((v>=0?"+":"")+(100*v).toFixed(1)+"%");
  const roiTd=(v)=>`<td class="mono ${v==null?"":(v>=0?"up":"down")}">${fmtRoi(v)}</td>`;
  const statTds=(r)=>`
      <td class="mono">${r.chains}</td>
      <td class="mono">${r.chains?((100*r.hit_chains/r.chains).toFixed(0)+"%"):"—"}</td>
      <td class="mono">${r.staked.toFixed(0)}</td>
      <td class="mono ${r.pnl>=0?"up":"down"}">${r.pnl>=0?"+":""}${r.pnl.toFixed(1)}</td>
      ${roiTd(r.roi)}${roiTd(r.roi_ex_best)}${roiTd(r.roi_ex_best2)}`;
  const comboRows=(cons.combos||[]).filter(r=>r.chains>0)
    .sort((a,b)=>a.count-b.count||b.chains-a.chains).map(r=>`<tr>
      <td class="mono">${lamp(r.stable)}${lamp(r.sharp)}
        <span class="muted">${["stable","sharp"]
          .filter(k=>r[k]).map(k=>LAMP[k]).join("")||"全灭"}</span></td>
      <td class="mono">${r.tilted
        ?`<span class="pill amber">×${consRule.factor}</span>`:"满仓"}</td>
      ${statTds(r)}
    </tr>`).join("");
  const countRows=(cons.counts||[]).map(r=>`<tr>
      <td class="mono">${r.count} 盏${r.count>=(consRule.min_signals||2)
        ?` <span class="pill amber">×${consRule.factor}</span>`:""}</td>
      <td></td>${statTds(r)}
    </tr>`).join("");
  const singleRows=(cons.singles||[]).map(r=>`<tr>
      <td>${h(LAMPDEF[r.signal]||r.signal)}</td>
      <td class="mono ${r.lit?"down":"up"}">${r.lit?"亮":"灭"}</td>
      ${statTds(r)}
    </tr>`).join("");
  const consHead=`<tr><th></th><th>注码</th><th>链</th><th>命中链</th>
      <th>投入</th><th>pnl</th><th>ROI</th><th>去最大赢</th><th>去前两赢</th></tr>`;
  return `<h2>固化减仓：两盏灯的账</h2>
  <p class="sub">驱动固化减仓的两盏模型灯，P0 开链定档、全链继承。
  原与挂单执行同节；挂单显示退役后（2026-08-29）自立门户，规则本身没动。</p>
  <div class="card">
    <div class="why" style="margin-top:8px"><b>入场那一刻记两盏模型灯，
    量的是"当天的答案定型了没有"：</b>
    ${Object.values(LAMPDEF).join("；")}。
    灯亮=市场的定价功课做完了，此时还剩的价差多半是别人知道我们不知道的事。
    <b>现行规则（2026-08-28 裁定）：任一盏亮，
    整链注码 ×${consRule.factor??0.1}</b>——只在开仓时少买，
    永不拒入场、永不卖出、永不撤单（改成门会自反，折扣门 −54% 判例）。<br>
    <b>第三盏灯「报满价」已退役（2026-08-28）：</b>它几乎天天亮，
    说的是"有没有盘口"而非"市场多确定"，不再计数也不再显示；
    原始记录仍逐链落盘。层薄的格子以"去前两赢"为准，
    符号靠最大一两条链撑住的读数是彩票不是发现。灯取 <b>P0 开链那一刻</b>的读数，
    链级合计（P0+P0-M+P1 同链同档）。</div>
    <table style="margin-top:8px"><thead>${consHead}</thead>
    <tbody>${comboRows}</tbody></table>
    <div class="muted" style="margin-top:10px">按亮灯数</div>
    <table style="margin-top:4px"><thead>${consHead}</thead>
    <tbody>${countRows}</tbody></table>
    <div class="muted" style="margin-top:10px">单灯切分</div>
    <table style="margin-top:4px"><thead><tr><th>灯</th><th>状态</th>
      <th>链</th><th>命中链</th><th>投入</th><th>pnl</th>
      <th>ROI</th><th>去最大赢</th><th>去前两赢</th></tr></thead>
    <tbody>${singleRows}</tbody></table>
  </div>`;
}


function riskMetrics(){
  const R = D.risk_metrics || {};
  if(!R.available) return `<h3 style="margin-top:22px;font-size:15px">组合风险指标</h3>
    <div class="card"><span class="dot muted"></span>
      结算日不足 3 天，比率算不出来。</div>`;
  // Signed for anything that can go either way; unsigned for dispersion,
  // which cannot be negative and reads wrong with a plus in front of it.
  const pct = (v,d=1)=>v==null?"—":((v>=0?"+":"")+(100*v).toFixed(d)+"%");
  const mag = (v,d=1)=>v==null?"—":(100*v).toFixed(d)+"%";
  const num = (v,d=2)=>v==null?"—":v.toFixed(d);
  const S = R.sharpe, TGT = R.sharpe_target, CI = R.sharpe_ci||[];
  // The target is a goal, not a verdict: with an error bar this wide the
  // only honest colour is "cannot tell", so the tile is muted until the
  // interval clears zero.
  const decided = CI.length===2 && (CI[0]>0 || CI[1]<0);
  const cls = !decided ? "muted" : (S>=TGT ? "up" : "down");
  const tile = (label, value, note, klass) => `<div class="card">
    <div class="muted">${label}</div>
    <div class="big ${klass||""}">${value}</div>
    <div class="muted" style="font-size:11.5px">${note}</div></div>`;

  // Expected shortfall, with the sample standing behind each level shown
  // beside it.  At fifteen days the 95% tail is one observation, so the
  // number is the worst day wearing a percentile's clothes; the table
  // says so per row rather than in a footnote nobody reads.
  const tails = R.tails || [];
  const eqNow = R.final_equity_usdc;
  const thin = t => t.tail_days < 3;
  const cvarRows = tails.map(t => `<tr class="${thin(t)?"muted":""}">
    <td class="mono">${(t.level*100).toFixed(0)}%</td>
    <td class="mono ${thin(t)?"":"down"}">${mag(-t.var)}</td>
    <td class="mono ${thin(t)?"":"down"}"><b>${mag(-t.cvar)}</b></td>
    <td class="mono">${(t.cvar*eqNow).toFixed(1)}</td>
    <td>${t.tail_days} 天${thin(t)
      ? ` <span class="pill amber">样本不足</span>` : ""}</td>
    <td class="muted">${t.days_for_five_in_tail} 天</td></tr>`).join("");
  const cvar = !tails.length ? "" : `<div class="card" style="margin-top:12px">
    <div class="muted">CVaR / 期望损失（Expected Shortfall）· 经验尾部，不假设分布</div>
    <div class="why" style="margin-top:8px"><b>先看最右两列。</b>
    CVaR 是"最差那 (1−置信) 比例的日子，平均亏多少"。${R.days} 天的样本里，
    95% 的尾部只剩 <b>${tails[0].tail_days} 个观测</b>——那不是尾部估计，
    那就是<b>最差的一天</b>穿了个百分位的外衣，95% 和 90% 两行报同一个数正是这个原因。
    要让尾部自己有五个观测，95% 需要约 ${tails[0].days_for_five_in_tail} 天、
    80% 需要约 ${tails[2].days_for_five_in_tail} 天。标灰的行是样本不足的行。</div>
    <div class="scroll"><table style="margin-top:6px"><thead><tr>
      <th>置信</th><th>VaR</th><th>CVaR</th><th>按现权益</th>
      <th>尾部样本</th><th>够五个需要</th>
    </tr></thead><tbody>${cvarRows}</tbody></table></div>
    <div class="muted" style="margin-top:8px;font-size:11.5px">
      对照：${R.days} 天里亏了 <b>${R.losing_days}</b> 天，
      平均每个亏损日 <b>${mag(-R.mean_losing_day)}</b>，
      最差一天 <b>${mag(-R.worst_day)}</b>（${h(R.worst_day_date||"—")}，
      ${(R.worst_day*eqNow).toFixed(1)} USDC）。这三个数各有
      ${R.losing_days} / ${R.losing_days} / 1 个观测撑着，
      比上表 95% 那一行诚实得多。</div></div>`;

  // Equity curve plus its least-squares line.  Drawn from the same series
  // the ratios are computed on, so the picture and the numbers are one
  // reading.  Plain SVG in the page's own tokens -- no library, and it
  // survives the light/dark switch because it names variables, not hexes.
  const pts = R.series||[];
  const W = 900, H = 210, PADX = 46, PADY = 16;
  const eq = pts.map(r=>r.equity_usdc);
  const line = pts.map((_,i)=>R.trend_intercept_usdc
    + R.trend_slope_usdc_per_day*(i+1) + R.initial_equity_usdc);
  // What the book said it would be worth by now, accumulated from the same
  // per-day claims that sized every bet.  The gap between this and the
  // green line is the claim layer's whole error, in money.
  const claimed = pts.map(r=>r.claimed_equity_usdc);
  // Two panels, not one.  Drawing the claim line into the equity chart put
  // 1513 and 1028 on one axis and flattened the curve the trend line is
  // supposed to describe -- the divergence was legible and everything else
  // was not.  Each panel gets the scale its own question needs.
  const lo = Math.min(R.initial_equity_usdc, ...eq, ...line);
  const hi = Math.max(R.initial_equity_usdc, ...eq, ...line);
  const span = (hi-lo)||1;
  const X = i => PADX + (W-PADX-10) * (pts.length<2?0:i/(pts.length-1));
  const Y = v => PADY + (H-2*PADY) * (1 - (v-lo)/span);
  const path = (vals)=>vals.map((v,i)=>`${i?"L":"M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  // Peak-to-trough band, so the drawdown number has somewhere to point.
  // The peak can be the opening capital, which has no point on the curve
  // -- that is exactly the case a book that lost on day one produces, and
  // it is the case this book is in.  Anchor the band at the left edge
  // rather than dropping it.
  const troughIdx = pts.findIndex(r=>r.event_date===R.max_drawdown_trough_day);
  const rawPeakIdx = pts.findIndex(r=>r.event_date===R.max_drawdown_peak_day);
  const peakX = rawPeakIdx>=0 ? X(rawPeakIdx) : X(0);
  const ddBand = (troughIdx>=0 && X(troughIdx)>peakX)
    ? `<rect x="${peakX.toFixed(1)}" y="${PADY}"
        width="${(X(troughIdx)-peakX).toFixed(1)}" height="${H-2*PADY}"
        fill="var(--pill-red-bg)"></rect>` : "";
  const ticks = [hi, (hi+lo)/2, lo].map(v=>`
    <text x="6" y="${(Y(v)+4).toFixed(1)}" fill="var(--muted)"
      font-size="11">${v.toFixed(0)}</text>
    <line x1="${PADX-6}" y1="${Y(v).toFixed(1)}" x2="${W-10}"
      y2="${Y(v).toFixed(1)}" stroke="var(--line)" stroke-width="1"></line>`).join("");
  const chart = pts.length<2 ? "" : `<div class="card" style="margin-top:12px">
    <div class="muted">权益曲线与趋势线 · 红带是最大回撤区间（${
      h(R.max_drawdown_peak_day||"—")} → ${h(R.max_drawdown_trough_day||"—")}）</div>
    <div class="scroll"><svg viewBox="0 0 ${W} ${H}" width="100%"
      style="min-width:520px;height:${H}px;display:block;margin-top:6px"
      role="img" aria-label="权益曲线与趋势线">
      ${ddBand}${ticks}
      <line x1="${PADX}" y1="${Y(R.initial_equity_usdc).toFixed(1)}"
        x2="${W-10}" y2="${Y(R.initial_equity_usdc).toFixed(1)}"
        stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3"></line>
      <path d="${path(line)}" fill="none" stroke="var(--accent)"
        stroke-width="1.5" stroke-dasharray="6 4"></path>
      <path d="${path(eq)}" fill="none" stroke="var(--up)"
        stroke-width="2"></path>
    </svg></div>
    <div class="muted" style="font-size:11.5px">
      灰虚线是起始本金 ${R.initial_equity_usdc.toFixed(0)}；蓝虚线是最小二乘趋势线
      <span class="mono">${(R.trend_slope_usdc_per_day>=0?"+":"")}${
        num(R.trend_slope_usdc_per_day)} USDC/天</span>，
      R²=${num(R.trend_r_squared)}——R² 低说明这条线只是把噪声拟合成了方向，
      不是"每天稳定赚这么多"。</div></div>`;

  // Panel two: the same equity line against what the book said it would
  // be worth.  Its own scale, because the claim runs 18x further and one
  // axis for both is what flattened the first panel.
  const lo2 = Math.min(R.initial_equity_usdc, ...eq, ...claimed);
  const hi2 = Math.max(R.initial_equity_usdc, ...eq, ...claimed);
  const span2 = (hi2-lo2)||1;
  const H2 = 150;
  const Y2 = v => PADY + (H2-2*PADY) * (1 - (v-lo2)/span2);
  const path2 = (vals)=>vals.map((v,i)=>`${i?"L":"M"}${X(i).toFixed(1)},${Y2(v).toFixed(1)}`).join("");
  const ticks2 = [hi2, lo2].map(v=>`
    <text x="6" y="${(Y2(v)+4).toFixed(1)}" fill="var(--muted)"
      font-size="11">${v.toFixed(0)}</text>`).join("");
  const claimChart = pts.length<2 ? "" : `<div class="card" style="margin-top:12px">
    <div class="muted">声称 EV 该到的权益 vs 实际权益 · 两条线的落差就是声称层的全部误差</div>
    <div class="scroll"><svg viewBox="0 0 ${W} ${H2}" width="100%"
      style="min-width:520px;height:${H2}px;display:block;margin-top:6px"
      role="img" aria-label="声称权益与实际权益">
      ${ticks2}
      <line x1="${PADX}" y1="${Y2(R.initial_equity_usdc).toFixed(1)}"
        x2="${W-10}" y2="${Y2(R.initial_equity_usdc).toFixed(1)}"
        stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3"></line>
      <path d="${path2(claimed)}" fill="none" stroke="var(--violet)"
        stroke-width="1.5" stroke-dasharray="2 3"></path>
      <path d="${path2(eq)}" fill="none" stroke="var(--up)"
        stroke-width="2"></path>
    </svg></div>
    <div class="muted" style="font-size:11.5px">
      <span style="color:var(--violet)">紫点线</span>：把每天的声称 EV 累加到起始本金上，
      到今天该是 <b>${num(R.initial_equity_usdc + R.claimed_ev_usdc,0)}</b>；
      <span style="color:var(--up)">绿线</span>：实际权益
      <b>${R.final_equity_usdc.toFixed(0)}</b>。
      同一个 EV 数在给每笔定规模——两条线分得越开，说明定规模的输入错得越多。</div></div>`;

  return `<h3 style="margin-top:22px;font-size:15px">组合风险指标</h3>
  <p class="sub">把整本账当一个组合来量：每个事件日一笔收益（当日已结算盈亏 ÷
  当日开盘权益），按 <b>365 天</b>年化（这本账有市场就交易，不跳周末）。</p>
  <div class="why"><b>先看误差棒再看数字。</b>
  ${R.days} 天的样本估夏普，经典标准误是
  <span class="mono">√((1+S²/2)/n)</span>，年化后是 <b>±${num(R.sharpe_standard_error)}</b>——
  比估计值本身大好几倍。所以下面的 ${num(S)} <b>与目标 2 的差距不是发现</b>，
  它和 0 都还分不开。要让这个数字在 95% 置信下离开 0，
  按当前的信噪比需要约 <b>${R.days_to_separate_from_zero==null?"—":R.days_to_separate_from_zero} 个交易日</b>
  （现有 ${R.days} 天）。最大回撤和波动率受样本影响小得多，可以先当真。</div>
  <div class="grid g3">
    ${tile("夏普比率 <span class='muted'>目标 "+TGT.toFixed(1)+"</span>",
      num(S), "95% 区间 ["+num(CI[0])+", "+num(CI[1])+"] · "+
      (decided?"已与 0 可分":"<b>与 0 不可分</b>"), cls)}
    ${tile("Sortino", num(R.sortino),
      "只罚下行波动 "+mag(R.downside_deviation,2)+"/天 · 同样受样本量所限",
      decided?(R.sortino>=TGT?"up":"down"):"muted")}
    ${tile("最大回撤", pct(R.max_drawdown),
      "峰 "+h(R.max_drawdown_peak_day||"—")+" → 谷 "+h(R.max_drawdown_trough_day||"—"),
      R.max_drawdown<=-0.15?"down":"")}
  </div>
  <div class="grid g3" style="margin-top:10px">
    ${tile("年化收益 <span class='muted'>几何</span>",
      pct(R.annualised_return_geometric),
      "算术口径 "+pct(R.annualised_return_arithmetic)+" · 累计 "+pct(R.total_return,2),
      R.annualised_return_geometric>=0?"up":"down")}
    ${tile("年化波动率", mag(R.annualised_volatility),
      "日波动 "+mag(R.daily_volatility,2)+" × √365")}
    ${tile("权益", R.final_equity_usdc.toFixed(2),
      "起始 "+R.initial_equity_usdc.toFixed(0)+" · "+R.days+" 个结算日（"+
      h(R.first_day)+" → "+h(R.last_day)+"）",
      R.final_equity_usdc>=R.initial_equity_usdc?"up":"down")}
  </div>
  <div class="grid g3" style="margin-top:10px">
    ${tile("平均 EV <span class='muted'>声称 · 每 USDC</span>",
      mag(R.claimed_ev_per_usdc),
      "下单当刻自己说的：胜率÷成本−1，按投入加权", "up")}
    ${tile("EV 兑现比 <span class='muted'>按钱加权</span>",
      R.ev_realisation_ratio==null?"—":num(R.ev_realisation_ratio,3)+"×",
      "声称共 "+num(R.claimed_ev_usdc,1)+" USDC · 实得 "+
      num(R.realised_pnl_usdc,1)+" USDC",
      R.ev_realisation_ratio>=0.8?"up":"down")}
    ${tile("EV 兑现比 <span class='muted'>中位日 · 近7日</span>",
      num(R.ev_realisation_daily_median,2)+"× · "+
      num(R.ev_realisation_trailing_7d,2)+"×",
      "跑赢自己声称的日子 "+R.days_beating_claim+" / "+R.days,
      R.ev_realisation_daily_median>=0.8?"up":"")}
  </div>
  <div class="grid g3" style="margin-top:10px">
    ${tile("平均赔率 <span class='muted'>按投入加权</span>",
      num(R.odds_mean,2)+"x",
      "赢一次，每 USDC 净赚这么多 · 平均成本 "+num(R.cost_mean,3))}
    ${tile("保本胜率", mag(R.breakeven_win_rate),
      "这个价钱下，不赚不亏需要的胜率（＝平均成本）")}
    ${tile("实际胜率", mag(R.realised_win_rate_weighted),
      "按投入加权 · 按链数是 "+mag(R.realised_win_rate)+
      "（"+R.chain_count+" 条链）",
      R.realised_win_rate_weighted>R.breakeven_win_rate?"up":"down")}
  </div>
  <div class="why" style="margin-top:10px"><b>赔率这一组读法：</b>
  平均花 ${num(R.cost_mean,3)} 买一份满赔 1.00 的赔付，所以赔率
  ${num(R.odds_mean,2)}x，<b>不赚不亏就要赢
  ${mag(R.breakeven_win_rate)}</b>。按投入加权实际赢了
  ${mag(R.realised_win_rate_weighted)}——只高出
  ${mag(R.realised_win_rate_weighted - R.breakeven_win_rate)}；
  按链数则是 ${mag(R.realised_win_rate)}，<b>反而低于保本线</b>。
  两个口径分居保本线两侧，正是"钱赚在少数大仓、链数上是亏的"的另一种说法，
  也解释了为什么上面的 EV 兑现比按钱算和按天算差六倍。</div>
  <div class="why" style="margin-top:10px"><b>这三块是全页最重的一组，
  但两个兑现比必须一起读。</b>
  EV 不是事后打分，它是<b>给每一笔定规模的输入</b>——Kelly 用它决定下多少。
  这本账按 ${mag(R.claimed_ev_per_usdc)}/U 的预期下注，实际拿回
  ${R.total_staked_usdc>0?mag(R.realised_pnl_usdc/R.total_staked_usdc):"—"}/U。<br>
  <b>按钱加权是 ${num(R.ev_realisation_ratio,3)}×，中位日却是
  ${num(R.ev_realisation_daily_median,2)}×——差六倍，两个都没算错。</b>
  前者被少数几个大亏日按仓位大小拖住（08-15、08-20、08-22 三天各亏掉
  当日声称的一倍以上），后者说的是"平常的一天"。近 7 日是
  ${num(R.ev_realisation_trailing_7d,2)}×。三个数分别回答"钱发生了什么"、
  "典型一天什么样"、"最近怎么样"，${R.days} 天里
  ${R.days_beating_claim} 天跑赢了自己当天的声称。<br>
  <b>不要把近 7 日的回升当成好转</b>——它和夏普来自同一个 ${R.days} 天样本，
  同样分不开噪声。能确定的只有方向：三个口径全部远低于 1.0，
  说明<b>定规模用的那个数偏高</b>，而这个偏差每天都在重复，
  不像夏普要等 ${R.days_to_separate_from_zero} 天才能判。</div>
  ${cvar}
  ${chart}
  ${claimChart}`;
}


function gaps(){
  if(!D.gaps.length) return `<h2>数据缺口</h2>
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
  return `<h2>数据缺口与健康检查</h2>
  <p class="sub">每一条都写清楚"缺了什么、会造成什么、会不会自己好"。
  标"正常等待"的不是故障，是样本还在积累。</p>
  <div class="card"><div class="scroll"><table><thead><tr>
    <th>级别</th><th>缺什么</th><th>影响</th><th>会不会自己好</th>
  </tr></thead><tbody>${rows}</tbody></table></div></div>`;
}

// The live book, cut for the three sections that are otherwise entirely
// paper (user ruling, 2026-09-01: the ledger sections must show the live
// orders).  Every cut comes off one map keyed station|day, so the daily
// row, the cross-day station row and the station-day row cannot disagree
// about the same legs -- three separate SQL cuts is how they would.
//
// Live and paper never merge into a single number.  These sections
// average paper_trade_* fills; a live leg folded into one of those means
// would make two books that do not add up look like one that does.  So
// live gets its own column and its own em dash: a station that minted
// nothing live shows "—", not 0, because "no live intent" and "a live
// intent worth zero" are different claims and only one of them is true
// here.
const LIVEBOOK = ((D.live_readiness||{}).live_orders||{}).book||{};
const LIVE_BY_SD = LIVEBOOK.by_station_day||{};
function liveAgg(pred){
  let count=0,notional=0,reached=0,baskets=0,any=false;
  const states=new Set();
  for(const key in LIVE_BY_SD){
    const cut=key.indexOf("|");
    if(cut<0) continue;
    if(!pred(key.slice(0,cut), key.slice(cut+1))) continue;
    const e=LIVE_BY_SD[key];
    any=true;
    count+=e.count||0; notional+=e.notional_usdc||0;
    reached+=e.legs_reached_exchange||0; baskets+=e.baskets||0;
    String(e.states||"").split(",").filter(Boolean).forEach(s=>states.add(s));
  }
  return any?{count,notional_usdc:notional,legs_reached_exchange:reached,
              baskets,states:[...states].sort()}:null;
}
// "Reached the exchange" is counted off order events, not off the intent
// state: an intent can be cancelled after its order went out, and a panel
// that reports state alone would call that leg untraded.
function liveCell(e){
  if(!e) return `<span class="muted" title="该范围没有铸出过实盘意向">—</span>`;
  const reached=e.legs_reached_exchange||0;
  const cls=reached?"":"down";
  return `<span class="pill red" style="font-size:9.5px">LIVE</span>
    <b class="mono">${e.count}</b><span class="muted"> 腿 · </span>
    <span class="mono">${(e.notional_usdc||0).toFixed(2)}U</span>
    <div class="muted ${cls}" style="font-size:10.5px"
      title="到所＝有订单事件的腿数，按回执数不按意向状态数：撤单后的腿也算走到过交易所">
      ${e.baskets} 篮 · 到所 ${reached}/${e.count}</div>`;
}

// The one table that lives above the tabs, with the health light and the
// strip (user ruling, 2026-08-21).  It is the page's core: one row per
// event day, every stage's account of it on the same line, and it answers
// "what happened yesterday" without choosing a group first.  Unnumbered for
// that reason -- the numbered sections are what the three groups hold, and
// a number here would imply it sits in one of them.
// Why a station-day carries no seat, in the ledger's own words.  Read by
// both the roster (section 12) and the daily table's station breakdown, so
// a reason code added to the ledger needs one entry here rather than two.
const ABSENT_REASON={no_probability:["无概率","这一天没有为该站算出概率"],
    no_market_book:["无盘口","有概率但没抓到可定价的市场"],
    no_book_at_decision:["盘口未落库","每次决策时刻都没有半小时内的盘口快照"],
    incomplete_book:["盘口缺桶","有盘口但缺了模型认得的桶，整站不可定价"],
    selection_failed:["选桶报错","枚举窗口时抛错，该轮跳过"],
    no_legal_window:["无合法窗口","结构闸拿走了整批枚举：断桶、不含中心桶、或长度不够。这些窗口没走到看报价那一步，跟贵不贵无关。约四成的未入座是这一类"],
    no_priced_window:["窗口定不出价","窗口过了全部结构闸，但里面有腿根本没有卖价，成本算不出来。缺的是盘口"],
    negative_edge_window:["边际不敌摩擦","窗口过闸也报得出价，胜率减全包成本仍盖不住逐腿最小跳动——这一条才是市场比模型贵。约六成的未入座是这一类，最好的一篮边际中位数 −0.25"],
    leg_below_floor:["单腿报价过低","窗口里有腿的报价低于一分——那是无人出价那侧的挂牌，不是价格。这是我们自己设的闸，不是市场没给价"],
    no_positive_kelly:["赔率不划算","篮子凑出来也定了价，但赢面低于要价，Kelly 给出零仓位"],
    production_judge_blocked:["生产裁判无法运行","账本研究臂调用生产求解器，但该 scope 解析不到生产政策或求解器 fail-closed"],
    production_judge_hold:["生产裁判：不下注","生产目标函数看过了篮子，给出的目标份额不高于已持仓。加仓日就是按兵不动；开仓日（P0 空链）从 2026-09-05 起只剩一种情况记在这里：有窗口过了整篮正EV闸，但宪章 74 的门票没成（门票超出固化减仓后的资本，或某腿卖一档无量）。没有窗口过闸的开仓日改按拒掉最后一个窗口的那道闸记，多数是「边际不敌摩擦」"],
    shape_gap_too_wide:["分布撕裂","均值离众数超过门限（研究臂，默认关）：撕裂形态下不入座"],
certainty_gate_lit:["市场已定价完毕","中心桶连三次没动，或头两桶已占六成——证据收敛了，溢价市场早收过了，这一轮主动不做，等下一个决策时刻。与「赔率不划算」相反：那是算过嫌贵，这是压根没问"],
    unquoted_ideal_window:["理想窗口没报价","中心±1 里凑不出八成质量有像样卖价的桶。市场不报价常常不是疏忽，是它判定这些桶不会发生。默认关闭，由前向验证决定是否启用"],
    cost_exceeds_payout:["比满赔还贵","整篮成本超过它赢了能赔的 1.00，多高的概率都是亏——模型没被问到，是书的问题"],
    resting_unfilled:["挂单没成交","价格没等到；规模是够的，市场没走到我们这里来。每 60 分钟重挂一次，但过了本站达峰时刻就收摊，不是挂到结算"],
    belief_withdrew_order:["信念转负撤单","不是市场没来，是我们自己撤的：重挂时用最新概率重算整篮期望，转负即全撤。该查模型，不是执行"],
    no_top_up_needed:["无需加仓","这一阶段想要的梯级已经在仓里了，按兵不动"],
    topup_not_complete_basket:["只缺部分腿，整篮不动","篮子完整性重放臂：目标里只有部分腿需要加，整篮不动"],
    p1_execution_evidence:["P1 没带自己的证据","宪章119：软似然 09-02 起停用后，P1 能拿出的证据是观测硬下界＋最外层三个申请者（四层链／区域路径先验／时钟天花板）里说话的那一个。三个都没留回执的那一轮不判——这一步的分布只是模型自己重新归一。同一天更晚的轮次通常就有回执，是一轮的拒绝不是一天的"],
    entry_window_not_open:["P1 入座窗未开","宪章67：P1 只允许在本地 11 时后入座——它的本事是盘中观测，日高在午后才成形。11 时前的轮次一律不判；实测 11 时前入座的加仓净亏 49.5，11–16 时入座净赚 99.8。本地已过 11 时仍见此项＝窗开后温度从未创当日新高、P1 没有触发过重算（阴雨天常见），是天气不是故障"],
    no_chain_growth:["加仓救不动链曲线","宪章66乙款：加仓按整条链的对数增长定规模，从已持仓位出发这一笔要让链的赔率曲线变优才买；求解器给出的最优增量为零——已持仓覆盖，或价格吃尽了增益"],
    chain_cost_at_par:["买入后整链必亏","宪章66甲款：这一笔单独看过得了本阶段的闸，但买下去之后整条链的混合成本≥1.00——满额赔付也回不了本，中了也亏。只做算术不读声称；实测这类链38条中37仍净亏31.9"],
    no_decision_moment:["不是决策时刻（修复前旧行）","宪章77 是按轮的规则，不是站日的原因：这一轮概率身份与当日可入座状态都与本阶段上次记录的决策相同，只是盘口动了，于是沿用上次决策；站日没入座的真因在被沿用的那次决策里。2026-09-05 起账本只按轮计数、不再把它记成站日原因；还挂着它的是修复前已结算的旧行，重走（--rebuild）后改按真因记"],
    topup_requires_probability_change:["概率没变，不加仓","宪章71甲款：加仓以概率变化为前提，这一轮概率身份与上次买入相同，只是盘口或额度变了"],
    topup_increment_edge_negative:["这笔加仓自己不划算","宪章71乙款收紧臂：增量按折后划分的期望赔付不高于本笔成本"],
    decline_clamp_hold_basket:["整篮等下一轮","目标篮里有桶的概率比上一 cycle 低了，这一轮整篮不入座（研究臂：钳位=hold）。生产默认不是这条——它砍掉那条腿照买其余"],
    decline_clamp_emptied_basket:["全篮都在跌","目标篮每一个桶的概率都比上一 cycle 低，砍完没有腿剩下（研究臂：钳位=leg）"],
    chain_never_opened:["P0 未开仓","只有 P0 开仓，后面两阶段是加仓；P0 没坐下这里就没仓可加，真正的原因看 P0"],
    no_depth:["盘口太薄","价格划算但卖方挂出的量填不满最小整篮"],
    exchange_minimum_lot_unaffordable:["不足一手，整篮不下","交易所最小下单量是 5 股，比这更小的单它根本不收。这一篮里有腿抬不到一手——要么额度不够，要么那一档没那么多量——生产在这种时候整篮不下单。2026-09-04 之前账本把不足一手的碎股记成已成交，那是交易所不会接的单"],
    execution_edge_not_authorized:["增量边际不过授权","宪章 73：生产在下单前按入座时的信念再算一次这笔增量的期望赔付减成本，不高于 ε 就不发。第 92 条起门票同样受检。等份额 Kelly 篮子几乎碰不到它"],
    support_floor_after_lift:["抬手后篮子赢了也亏","宪章 93：最小手和一美元底线把各腿抬到整手之后，再按抬后股数和成本算一次支持集内最坏结算，真值落在自己窗口里任何一桶都不得亏损（此前允许亏一成，已归零）。等份形状永远合法，只有抬手抬成不等份时才会撞上"],
    station_loss_floor_exceeded:["最坏结算超过站亏损下限","生产内核的 profit_floor_ok：买完之后，支持集里最坏的那个结算结果（那一桶持有的股数减去整链付出的钱）不能亏过运营给这个站定的上限（当前 15 USDC）。等份额篮子几乎碰不到它；它不是逐腿预算闸，仓位大小由本金的 2% 和阶段份额决定"],
    no_seat_taken:["未入座","有概率也有盘口，目标函数看过后选择不坐"],
    station_not_onboarded:["未接入舰队","这个站有结算时区和已挂牌的市场，但模型从来没为它算过概率——舰队是 45 站，时区册是 48 站，多出来的三站是备好未接入。不是今天算不出来，是从来没算过；接入是舰队身份变更（22 个文件硬编码 45 站）"]};

// The station count, and what the rest of the fleet was doing.
//
// "29" on its own could not tell a day the model had nothing to say about
// from a day the market priced us out; both read as nineteen stations that
// simply are not there (user ruling, 2026-09-02).  The reasons are the
// ledger's own -- one per station-day, the same codes section 12 shows --
// so this groups what is already recorded rather than inferring it.
//
// The live bar is on its own line and deliberately outside the arithmetic:
// it says what the live lane may not trade, while every number to its left
// is the paper book, which seats a barred station like any other.
function stationCell(r){
  const b=r.station_breakdown;
  if(!b) return String(r.station_count);
  const head=`<b>${b.opened}</b><span class="muted">/${b.fleet}</span>`;
  const parts=(b.reasons||[]).map(x=>{
    const lab=ABSENT_REASON[x.reason]||[x.reason,""];
    return `<span title="${h(lab[1]||lab[0])}">${h(lab[0])} ${x.count}</span>`;
  });
  const why=parts.length
    ? `<div class="muted" style="font-size:11px;margin-top:2px">${
        parts.join(`<span class="muted"> · </span>`)}</div>`
    : "";
  const bar=b.live_barred
    ? `<div class="muted" style="font-size:11px" title="实盘禁入名单上的站，属实盘裁定；纸面账本照常入座，所以这个数不参与左边的加减">实盘禁入 ${b.live_barred}</div>`
    : "";
  return head+why+bar;
}

function daily(){
  if(!D.daily || !D.daily.length) return "";
  const STAGES=["P0","P0-M","P1"];
  const pct=v=>v==null?"—":(v*100).toFixed(0)+"%";
  const rows=D.daily.map(r=>{
    const s=r.sides||{};
    const cal=r.calibration_ratio;
    const calCls=cal==null?"":(cal>=0.9&&cal<=1.15?"up":"down");
    const byStage=r.filled_leg_by_stage||{};
    // Three stages of one position, so the split is written out rather
    // than folded into a total: a day built at entry and a day topped up
    // afterwards have the same leg count and are not the same day.
    const stageTxt=STAGES.map(st=>{
      const n=byStage[st]||0;
      if(!n) return `<span class="muted">${st} —</span>`;
      return `<span title="${st}：${n} 腿">${st} <b>${n}</b></span>`;
    }).join(`<span class="muted"> · </span>`);
    const cov=r.cover_rate;
    const covCls=cov==null?"":(cov>=0.6?"up":(cov<0.4?"down":""));
    return `<tr>
      <td class="mono">${h(r.event_date)}</td>
      <td>${stationCell(r)}</td>
      <td>${r.position_count}</td>
      <td class="muted">${s.above||0}/${s.aligned||0}/${s.below||0}</td>
      <td class="${covCls}" title="意向窗各桶概率之和，按站取均值——选桶那一刻就知道，与结算无关">${
        pct(cov)}${
        r.truth_inside_rate!=null?`<span class="muted" title="真值确实落进意向篮子的站比例（要等结算，与左边不是一回事）"> 真值落入 ${
          (r.truth_inside_rate*100).toFixed(0)}%</span>`:""}${
        r.unfilled_intended?`<span class="muted"> +${
          r.unfilled_intended} 缺席</span>`:""}</td>
      <td class="mono">${r.filled_leg_total}
        <span class="muted">均 ${r.filled_leg_mean==null?"—"
          :r.filled_leg_mean.toFixed(1)}</span>
        <div class="muted" style="font-size:11px;margin-top:2px"
          >${stageTxt}</div></td>
      <td>${r.settled_count}</td>
      <td>${r.hit_rate==null?"—":(r.hit_rate*100).toFixed(0)+"%"}</td>
      <td>${r.claimed_win_probability_mean!=null
        ?(r.claimed_win_probability_mean*100).toFixed(0)+"%"
        :r.claimed_open_mean!=null
          ?`<span class="muted" title="本日尚未结算：这是已入座各链最新阶段的声称胜率均值，没有结算样本可比"
            >${(r.claimed_open_mean*100).toFixed(0)}%*</span>`
          :"—"}</td>
      <td title="净盈利概率 Σp·[n>C]：只数「持有的腿里，股数超过整篮成本的那些」。覆盖率把每个中了的桶都算成命中，但 n≤C 的腿中了照样亏 —— 2026-09-06 的 MMMX 覆盖 79%、净盈利概率 11%。">${
        (()=>{const v=r.net_profit_probability_mean!=null
          ?r.net_profit_probability_mean:r.net_profit_probability_open_mean;
          if(v==null) return "—";
          const c=r.claimed_win_probability_mean!=null
            ?r.claimed_win_probability_mean:r.claimed_open_mean;
          const star=r.net_profit_probability_mean==null?"*":"";
          // Amber when the two readings disagree by more than a fifth of the
          // claim: that gap is a basket holding a rung it loses on.
          const wide=(c!=null&&c>0&&(c-v)/c>0.2);
          return `<span class="${wide?"down":""}${star?" muted":""}"
            >${(v*100).toFixed(0)}%${star}</span>`;})()}</td>
      <td class="factor ${calCls}">${cal==null?"—":cal.toFixed(2)+"×"}</td>
      <td>${r.odds_mean==null?"—":r.odds_mean.toFixed(1)+"x"}</td>
      <td class="mono">${r.staked_usdc.toFixed(2)}</td>
      <td class="mono ${r.pnl_usdc>=0?"up":"down"}">${(r.pnl_usdc>=0?"+":"")+r.pnl_usdc.toFixed(2)}</td>
      <td>${liveCell(liveAgg((st,day)=>day===r.event_date))}</td>
    </tr>`;
  }).join("");
  return `<h2>每日统计</h2>
  <p class="sub">按事件日汇总，只算可成交的（整篮成本 ≥1 分）——不足一分的多是无人出价那侧的
  单边报价，名义上有价、实际买不到，混进来会让当天的盈亏读不出意思。</p>
  <div class="why"><b>列的意思：</b>「站数」是当天<b>建仓的站 / 舰队全部站</b>，
  下面按账本自己记的原因拆开没建仓的那些——盘口太薄和边际为负是两回事，
  一个是买不到，一个是不划算；「实盘禁入」单列，那是实盘裁定，
  纸面账本照常入座，不参与这一格的加减；
  「位置」是相对市场偏上/一致/偏下的笔数，
  用的是<b>均值落点桶</b>（宪章 54 生产就座的那个中心桶，直接读账本记录）与市场质心比，
  差 ${(0.35).toFixed(2)} 桶以内算一致；
  「意向窗盖住率」是真值有没有落进 P0<b>意向</b>篮子——评的是选桶质量、与成交无关，
  所以它的站数按 P0 记录的站日算，可以多于当天仓位数，全程没买到、账本只留文字的站日
  单列为「缺席」；
  「建仓桶」是当天所有座位买到的腿数合计（括号里是每座位平均几腿），
  下面一行按 <b>P0 / P0-M / P1</b> 拆开，
  三个阶段是同一个仓位：P0 建仓，后两个只加不减；
  「声称」带 * 的是尚未结算日：取各链最新阶段的声称胜率均值，
  与已结算日的口径不同（那一列只算结算样本），不可直接相比；
  「意向窗盖住率」<b>就是选中那几个桶的概率之和</b>（按站取均值）——选桶当刻即知，
  不等结算，也<b>不是</b>声称胜率或实际命中率（那两个另有其列）。
  它后面的「真值落入 N%」才是事后的：真值确实掉进意向篮子的站比例；
  「投入」是当天真实部署的资金（每站按整链算一次），不等结算；「盈亏」只属于已结算链，
  所以未结算日的这两列不能相除；
  「实现/声称」是命中率÷声称胜率，1.00× 表示概率说得准；
  「赔率」是 (1−成本)/成本。</div>
  <div class="card"><table><thead><tr>
    <th>事件日</th><th>站数<br><span class="muted">建仓/舰队</span></th><th>仓位</th>
    <th>位置<br><span class="muted">均值桶 上/平/下</span></th>
    <th>意向窗盖住率<br><span class="muted">选桶概率和</span></th>
    <th>建仓桶<br><span class="muted">腿数 · 分阶段</span></th>
    <th>已结算</th><th>命中率</th><th>声称</th>
    <th title="净盈利概率 Σp·[n>C]，与左侧的「声称」是两个量：声称数的是窗口盖住的概率，这一列只数「持有的腿里股数超过整篮成本的那些」。n≤C 的腿中了照样亏钱，而声称把那天算成命中。">净盈利<br><span class="muted">Σp·[n&gt;C]</span></th>
    <th>实现/声称</th>
    <th>赔率</th><th>投入</th><th>盈亏</th>
    <th title="实盘意向（live_submit_authorized=1），与左侧纸面各列是两套账，不可相加">实盘<br><span class="muted">腿 · 名义</span></th>
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
  return `<h2>覆盖 · 价格 · EV</h2>
  <p class="sub">只算可成交的（整篮 ≥1 分，排除虚价）。三组数回答同一个问题的三面：
  座位罩住了多少概率、为此付了多少、这笔交换值不值。</p>
  <div class="card"><div class="muted">相对市场的位置（<b>均值落点桶</b>减市场质量中心，单位是桶）
    <span title="宪章 54 生产就座的那个中心桶，直接读账本记录——与「每日统计」同一把尺。
不是模型未取整的质量中心：那是另一个数，两者在部分仓位上不一致。">ⓘ</span></div>
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
  // Barred stations stay in this table on purpose.  Dropping them would
  // hide the very rows a review of the bar has to read, and the numbers
  // here are what the next release decision is argued from -- so they are
  // marked, greyed, and counted separately rather than removed.
  const barred=new Set((D.live_readiness||{}).station_exclusions||[]);
  const probation=new Set((D.live_readiness||{}).station_probation||[]);
  // Difficulty instruments, joined from the readiness payload so this table
  // and that one cannot quote different numbers for the same station.
  const difficulty={};
  for(const rr0 of ((D.live_readiness||{}).stations||[]))
    difficulty[rr0.station]={j:rr0.climate_jump_c,g:rr0.shape_gap_buckets};
  // Station geography, [latitude, coastal].  Copied from
  // calibration_station_forecast_physics rather than added to the payload:
  // an airport does not move, so shipping it per render would only make a
  // frontend-only change need the full 20-minute rebuild.  Coastal means
  // the station sits within roughly 30 km of open sea -- lakes and tidal
  // rivers do not count, which is why Chicago and Toronto (Great Lakes),
  // London City (Thames) and Guangzhou (60 km up the Pearl River estuary,
  // unlike Shenzhen on its mouth) are inland here.
  const GEO={CYYZ:[43.679,0],EDDM:[48.348,0],EFHK:[60.327,1],EGLC:[51.505,0],
    EHAM:[52.315,1],EPWA:[52.163,0],FACT:[-33.965,1],KATL:[33.63,0],
    KAUS:[30.183,0],KBKF:[39.713,0],KDAL:[32.838,0],KHOU:[29.646,1],
    KLAX:[33.938,1],KLGA:[40.779,1],KMIA:[25.788,1],KORD:[41.96,0],
    KSEA:[47.445,1],KSFO:[37.62,1],LEMD:[40.466,0],LFPB:[48.967,0],
    LIMC:[45.631,0],LLBG:[32.011,1],LTAC:[40.128,0],LTFM:[41.262,1],
    MMMX:[19.436,0],MPMG:[8.967,1],NZWN:[-41.331,1],OEJN:[21.685,1],
    OPKC:[24.902,1],RCSS:[25.069,1],RJTT:[35.553,1],RKPK:[35.179,1],
    RKSI:[37.469,1],RPLL:[14.507,1],SAEZ:[-34.822,1],SBGR:[-23.432,0],
    UUWW:[55.592,0],VILK:[26.761,0],WMKK:[2.747,1],WSSS:[1.368,1],
    ZBAA:[40.082,0],ZGGG:[23.392,0],ZGSZ:[22.639,1],ZHHH:[30.783,0],
    ZSPD:[31.146,1],ZSQD:[36.362,1],ZUCK:[29.718,0],ZUUU:[30.576,0]};
  // Band edges at the tropic (23.4), 35 and 55 degrees.  The tropic edge is
  // deliberately the line itself, not a rounded 23.5: Sao Paulo (-23.432)
  // and Guangzhou (23.392) both sit within 5 km of it, and 23.5 would put
  // Sao Paulo -- a 750 m subtropical-highland station -- in the tropics
  // band while the line itself does not.
  const bandOf=lat=>lat>=55?"北半球·高纬":lat>=35?"北半球·温带"
    :lat>=23.4?"北半球·副热带":lat>=-23.4?"热带"
    :lat>=-35?"南半球·副热带":"南半球·温带";
  const BAND_ORDER=["北半球·高纬","北半球·温带","北半球·副热带","热带",
    "南半球·副热带","南半球·温带"];
  const fmtLat=lat=>Math.abs(lat).toFixed(1)+"°"+(lat>=0?"N":"S");
  const rowEntries=ss.map((s,i)=>{
    const sd=s.sides||{};const cal=s.calibration_ratio;
    const rr=s.realization_ratio;const ev=s.ev_per_usdc_mean;
    // A number per station (user ruling, 2026-08-21).  Assigned in the
    // table's natural order -- station id, ascending -- and printed in the
    // row rather than derived from the row's position, so it stays with the
    // station when a column is sorted instead of renumbering under it.
    // Geographic grouping reorders the rows but must not renumber them, so
    // the number keeps coming from this map index, not the display order.
    const off=barred.has(s.station_id);
    const g=GEO[s.station_id];const bd=g?bandOf(g[0]):null;
    const html=`<tr${off?' style="opacity:.55"':""}>
      <td class="mono muted">${i+1}</td>
      <td>${off?'<span class="pill" style="background:#f8514922;color:#f85149;border-color:#f8514955" title="实盘禁入：已判且不合格">⛔ 禁入</span> ':(probation.has(s.station_id)?'<span class="pill amber" title="证据不足，尚未上线；账本继续记账攒证据">⏳ 待观察</span> ':"")}<span class="mono">${h(s.station_id)}</span>
        <span class="muted">${h(s.station_label)}</span></td>
      <td>${bd?h(bd.replace("半球·","")):"—"}
        <span class="muted">${g?fmtLat(g[0]):""}</span></td>
      <td>${g?(g[1]?"沿海":"内陆"):"—"}</td>
      <td>${s.position_count}</td>
      <td>${s.settled_count}</td>
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
      <td class="${rr==null?"":(rr>=1?"up":"down")}">${rr==null?"—":n2(rr,2)+"×"}</td>
      <td class="mono ${(difficulty[s.station_id]||{}).j>1.76?"down":""}">${(difficulty[s.station_id]||{}).j==null?"—":difficulty[s.station_id].j.toFixed(2)}</td>
      <td class="mono ${(difficulty[s.station_id]||{}).g>0.93?"down":""}">${(difficulty[s.station_id]||{}).g==null?"—":difficulty[s.station_id].g.toFixed(2)}</td>
      <td>${liveCell(liveAgg(st=>st===s.station_id))}</td></tr>`;
    return {html,band:bd?BAND_ORDER.indexOf(bd):BAND_ORDER.length,
      coastal:g&&g[1]?0:1,id:s.station_id};
  });
  // Default order walks the globe north to south, coastal before inland
  // inside each band.  Clicking a header still re-sorts freely; this is
  // only where the table starts.
  rowEntries.sort((a,b)=>a.band-b.band||a.coastal-b.coastal
    ||(a.id<b.id?-1:a.id>b.id?1:0));
  const rows=rowEntries.map(e=>e.html).join("");
  // Group subtotals.  Hit and claimed are weighted by settled count -- the
  // only weight under which their ratio stays comparable to the per-station
  // 实现/声称 column -- and the money columns are plain sums, so 每U净益 is
  // the group's actual return, not an average of ratios.
  const groups=new Map();
  ss.forEach(s=>{const g=GEO[s.station_id];if(!g)return;
    const k=bandOf(g[0])+"·"+(g[1]?"沿海":"内陆");
    const a=groups.get(k)||{n:0,pos:0,set:0,wHit:0,wClaim:0,wSet:0,
      stake:0,real:0};
    a.n++;a.pos+=s.position_count||0;a.set+=s.settled_count||0;
    if(s.hit_rate!=null&&s.settled_count){a.wHit+=s.hit_rate*s.settled_count;
      a.wClaim+=(s.claimed_win_probability_mean||0)*s.settled_count;
      a.wSet+=s.settled_count;}
    a.stake+=s.staked_usdc||0;a.real+=s.realized_usdc||0;
    groups.set(k,a);});
  const groupRows=BAND_ORDER.flatMap(bd=>["沿海","内陆"].map(cl=>{
    const a=groups.get(bd+"·"+cl);if(!a)return "";
    const hit=a.wSet?a.wHit/a.wSet:null;
    const claim=a.wSet?a.wClaim/a.wSet:null;
    const cal=(hit!=null&&claim)?hit/claim:null;
    const roi=a.stake>0?a.real/a.stake:null;
    return `<tr><td>${h(bd)}</td><td>${cl}</td><td>${a.n}</td>
      <td>${a.pos}</td><td>${a.set}</td>
      <td>${p2(hit)}</td><td>${p2(claim)}</td>
      <td class="${cal==null?"":(cal>=1?"up":"down")}">${cal==null?"—":n2(cal,2)+"×"}</td>
      <td class="mono">${n2(a.stake,2)}</td>
      <td class="mono ${a.real>=0?"up":"down"}">${(a.real>=0?"+":"")+n2(a.real,2)}</td>
      <td class="${roi==null?"":(roi>=0?"up":"down")}">${roi==null?"—":(roi>=0?"+":"")+n2(roi,2)}</td></tr>`;
  })).join("");
  // Lives inside the section's one card, above the per-station table:
  // shipped as its own card first, and the extra card boundary read as a
  // separate section (user, 2026-08-31).
  const groupBlock=groupRows?`<div style="margin-bottom:14px"><b>分组小计</b>
  <div class="scroll"><table><thead><tr>
    <th>气候带</th><th>海陆</th><th>站数</th><th>仓位</th><th>已结算</th>
    <th>命中率</th><th>声称</th><th>实现/声称</th>
    <th>投入</th><th>实得</th><th>每U净益</th>
  </tr></thead><tbody>${groupRows}</tbody></table></div>
  <div class="muted" style="margin-top:8px">禁入站也计入小计，口径与下表一致。
  读的时候记住季节相位：现在（8 月底）北半球是夏末、南半球是冬末，
  南北两边的命中率不在同一个季节里，别直接横比。</div></div>`:"";
  // Two different absences under one sentence read as one fault.  A fleet
  // station that never took a credible seat is a result; a station the
  // model has never computed at all is not in the fleet yet.
  const offFleet=new Set((S.station_roster||[])
    .filter(e=>e.absent_reason==="station_not_onboarded")
    .map(e=>e.station_id));
  const silentFleet=silent.filter(id=>!offFleet.has(id));
  const silentOff=silent.filter(id=>offFleet.has(id));
  const idList=(ids)=>`<span class="mono">${
    ids.map((id,i)=>`${i+1}.${h(id)}`).join("　")}</span>`;
  const barredHere=ss.map(s=>s.station_id).filter(id=>barred.has(id));
  const barNote=barredHere.length?`<div class="muted" style="margin-top:8px">
    <span class="pill" style="background:#f8514922;color:#f85149;border-color:#f8514955">⛔ 禁入</span>
    ${barredHere.length} 个站已被实盘禁入，仍留在本表里——<b>禁的是下单，不是记账</b>：
    纸面账本继续为它们建仓结算，这些数就是下次复核放不放它们出来的依据。
    控制服务拒绝把它们加进 allowlist：${idList(barredHere)}</div>`:"";
  const note=(silentFleet.length||silentOff.length)
    ?`<div class="muted" style="margin-top:8px">${
    silentFleet.length?`另有 ${silentFleet.length} 个站在这段窗口里一笔可成交仓位都没有，
    因此没有可平均的数，只出现在页顶「逐站逐日明细」里：${idList(silentFleet)}<br>`:""}${
    silentOff.length?`<span class="pill amber">未接入</span> ${
    silentOff.length} 个站有结算时区和已挂牌市场，但模型从未为其算过概率——
    舰队 45 站、时区册 48 站，这是备好未接入的差额，不是这段窗口的失败：${
    idList(silentOff)}`:""}</div>`:"";
  return `<h2>逐站汇总（跨日）</h2>
  <p class="sub">一站一行，把该站 ${h(S.stats_start_event_date)} 起的全部可成交仓位
  （整篮 ≥1 分，排除虚价）平均到一起。<b>这些数不属于任何一天</b>——它们回答的是
  「这个站长期表现如何」，逐日的账在页顶「逐站逐日明细」。</p>
  <div class="why"><b>列的意思：</b>「#」是站的编号，按站号字母序编，
  排序时跟着这一站走，所以可以直接拿它点名某一行；
  「气候带」按站点纬度分：回归线（±23.4°）以内是热带，23.4°–35° 副热带，
  35°–55° 温带，55° 以上高纬，南北半球分开写，灰字是站点纬度；
  「海陆」按站点离开阔海面约 30 公里内算沿海——湖和内河不算，
  所以芝加哥（密歇根湖）、广州（离珠江口约 60 公里）算内陆，
  深圳（就在珠江口上）算沿海。默认行序按气候带从北到南、组内沿海在前；
  「仓位」是参与平均的笔数，样本少于三笔时右侧的
  命中率与实现/声称基本读不出信号；
  「位置」是相对市场偏上/一致/偏下的笔数；
  「概率和/卖价和/赔率/EV」同「覆盖 · 价格 · EV」，只是范围缩到这一个站；
  「实现/声称」是命中率÷声称胜率；「兑现比」是实得÷期望。</div>
  <div class="card">${groupBlock}<div class="scroll"><table><thead><tr>
    <th>#</th><th>站点</th>
    <th>气候带<br><span class="muted">纬度</span></th><th>海陆</th>
    <th>仓位</th><th>已结算</th>
    <th>位置<br><span class="muted">上/平/下</span></th>
    <th>概率和</th><th>卖价和</th><th>赔率</th><th>EV/U</th>
    <th>命中率</th><th>声称</th><th>实现/声称</th>
    <th>投入</th><th>实得</th><th>兑现比</th><th title="该站真值的日际跳变均值(°C)：>1.76 的站在真边际枯竭期实测 −43% vs 留存 +23% (p=0.023)">跳变/日</th><th title="近14天模型分布的均值−众数距离(桶)，跳变的模型层影子(r=+0.32)">形状gap</th>
    <th title="该站全部实盘意向（live_submit_authorized=1）跨日合计，与左侧纸面各列是两套账，不可相加">实盘<br><span class="muted">腿 · 名义</span></th>
  </tr></thead><tbody>${rows}</tbody></table></div>${barNote}${note}</div>`;
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
  const REASON=ABSENT_REASON;
  const short=x=>String(x).replace("°C","").replace("°F","");
  const LAB={above:"偏上",below:"偏下",aligned:"一致",unknown:"—"};
  // Charter 55's own reading, named in its own words: a positive gap
  // (mean above mode) is a warm tail.  Deliberately NOT reusing LAB --
  // "偏上" there means "model above market", a quantity this column no
  // longer shows, and one word for two different readings is how the
  // disagreement between them stayed invisible.
  const SKEW_LAB={"1":"偏暖","-1":"偏冷","0":"无偏斜"};
  // An empty stage cell used to be a dash and nothing else, so a station with
  // no P0 basket looked identical whether the market was never quoted or the
  // objective priced it and declined.  The refusal goes in the cell it
  // belongs to.
  // The funnel enumeration ("枚举 26 = 断桶 0 + 不含中心桶 16 + …") is the same
  // eight terms on every station of every day, and when the ledger names the
  // basket the objective actually wanted, that clause is the only part of
  // the line that is about THIS station.  So the target leads in the cell and
  // the funnel moves to the tooltip, where its checkable equation is still
  // one hover away (user ruling, 2026-08-28).  Split on the ledger's own
  // marker; a detail without one is shown whole, exactly as before.
  const FUNNEL=" · 枚举 ";
  const why=(r)=>{
    if(!r) return "";
    const lab=REASON[r.reason]||[r.reason,""];
    const d=r.detail||"";
    const cut=d.indexOf(FUNNEL);
    const shown=cut>0?d.slice(0,cut):d;
    const tip=cut>0?`${lab[1]}\n\n${d.slice(cut+3)}`:lab[1];
    return `<div class="muted" title="${h(tip)}">${h(lab[0])}</div>`+
      (shown?`<div class="muted mono" style="font-size:11px"
        title="${h(d)}">${h(shown)}</div>`:"");
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
    // Charter 50/50a is a rule about the ladder that was CHOSEN, so it
    // stays; the fill pills beside it do not.  Crossing buys the basket
    // whole, so "整篮" was true on every row and "缺腿" on none -- two
    // badges that never varied, describing a resting lane this book has
    // not run since charter 65 made taker the law.
    const floorBad=e.posted_leg_count<(FLOOR[stage]||3);
    const legLine=`<div class="mono" style="font-size:11px">${legs}
      <span class="muted">${e.posted_leg_count}腿</span>
      ${floorBad?`<span class="pill red" title="宪章 50/50a：${stage} 的桶数下界是 ${FLOOR[stage]||3}">桶数不足${FLOOR[stage]||3}</span>`:""}</div>`;
    // What the position cost.  The three completion rates that used to sit
    // here -- money, shares, rungs -- are all a resting lane's questions:
    // a crossing basket fills whole or refuses, so all three read 100% (or
    // a dash) on every row of this book.
    const money=`<div class="muted" style="font-size:11px">实投 ${
      usd2(e.filled_stake_usdc)}</div>`;
    // What this rung cost, which the share count does not say: the rungs
    // sell at different prices, so the widest leg is routinely not the
    // dearest one and the basket total cannot be split by eye.  Shown as a
    // share of the basket beside the cash, because "where is the money" is
    // the question a four-leg ladder actually raises.
    const approx=e.leg_cash_derived;
    const cashTip=approx
      ? "本行按建仓时的盘口卖一价折算（该行早于逐腿现金记账）；"
        +"吃单走深盘口的部分它看不见，所以是下界"
      : "成交时逐腿实付";
    const legCash=(l)=>{
      const got=l.filled_stake_usdc;
      if(got==null) return "—";
      const share=e.filled_stake_usdc>0
        ? ` <span class="muted">(${(100*got/e.filled_stake_usdc).toFixed(0)}%)</span>`:"";
      return `<span title="${h(cashTip)}">${approx?"≈":""}${
        usd2(got)}</span>${share}`;
    };
    // The walked average the leg actually paid.  A crossing basket records
    // no rung fills, so the price column is empty for every taker row on
    // the page even though the money says exactly what was paid per share.
    const legPrice=(l)=>{
      if(l.empty) return "—";
      if(l.filled_stake_usdc==null||!(l.filled_shares>0)) return "—";
      const tip=approx?"建仓时的盘口卖一价，吃单走深的部分不在里面（下界）"
        :"资金÷份数，即含走深盘口后的实付均价";
      return `<span title="${h(tip)}">${
        approx?"≈":""}${(l.filled_stake_usdc/l.filled_shares).toFixed(4)}</span>`;
    };
    // "目标份" and "档" were both the resting lane's account of a leg:
    // what was posted versus what the book came down to.  Crossing has one
    // number per leg, so the columns held a duplicate and a dash.
    const rows=e.legs.map(l=>`<tr${l.empty?' class="muted"':''}>
      <td>${h(l.bucket)}</td>
      <td>${l.empty?"<b>0</b>":shares(l.filled_shares)}</td>
      <td>${legPrice(l)}</td>
      <td>${legCash(l)}</td></tr>`).join("");
    // A derived column that does not add up to the recorded stake says so
    // rather than letting the reader sum it and trust the answer.
    const gap=(approx&&e.leg_cash_shortfall_usdc>0.005)
      ?`<div class="muted" style="font-size:11px">逐腿合计比实投少
        ${usd2(e.leg_cash_shortfall_usdc)}（吃单走深了盘口，盘口价折算看不见这部分）</div>`:"";
    return legLine+money+`<details style="font-size:11px">
      <summary class="muted">逐腿</summary>
      <table class="mono" style="font-size:11px"><thead><tr>
        <th>腿</th><th>份数</th><th>均价</th>
        <th title="该腿的资金成本（USDC）与占整篮比重">资金</th>
      </tr></thead><tbody>${rows}</tbody></table>${gap}</details>`;
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
      `<div class="muted">均值桶 <span class="mono" style="color:var(--violet)" title="宪章54：概率加权重心落点，窗口必含此桶">${short(s.centre_bucket)}</span></div>`:"";
    // Charter 55/58 read off the seat: the day's skew, which way the
    // seated window leans around the mean centre, the whole-basket EV the
    // price gate admitted on, and what crossing the book would have paid.
    const skewLab=s.skew_sign==null?""
      :s.skew_sign>0?`<span title="重心在众数热侧≥半桶：窗口不得向冷侧倾">右偏</span>`
      :s.skew_sign<0?`<span title="重心在众数冷侧≥半桶：窗口不得向热侧倾">左偏</span>`
      :`<span title="重心与众数差不足半桶：扩边方向不设约束">对称</span>`;
    let leanLab="";
    if(s.centre_bucket&&Array.isArray(s.window)){
      const ci=s.window.indexOf(s.centre_bucket);
      if(ci>=0){
        const net=(s.window.length-1-ci)-ci;
        leanLab=net>0?`窗+${net}热`:net<0?`窗${net}冷`:"窗居中";
      }
    }
    const evLab=(s.edge==null)?""
      :`<span class="${s.edge>0?"up":"down"}" title="宪章58：整篮正EV=概率和−买价和，唯一价格门">EV${s.edge>=0?"+":""}${s.edge.toFixed(3)}${s.edge>0?"✓":"✗"}</span>`;
    const askLab=(s.ask_total==null)?""
      :`<span title="按卖一全吃这套窗口的价格合计">买价和${s.ask_total.toFixed(3)}</span>`;
    const seatFacts=(skewLab||leanLab||evLab||askLab)
      ?`<div class="muted" style="font-size:11px">${[skewLab,leanLab,evLab,askLab].filter(Boolean).join(" · ")}</div>`:"";
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
    return `<td><div class="mono">${w}</div>${rung}${seatFacts}${tag}
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
    // One definition: mean minus mode, which is the predicate charter 55
    // gates window selection on (user ruling, 2026-08-28).  This column
    // used to show model-mean minus market-mean instead -- a different
    // quantity, disagreeing with the skew on 34 of 400 seated rows
    // (above/cold and below/warm) -- so the position a reader saw was not
    // the position that chose the basket.  The market lean is still real
    // information and is kept, but on the hover rather than presented as
    // the answer, because two quantities under one heading is how the
    // disagreement became invisible in the first place.
    const side=any&&any.skew_sign!=null?`<span class="pill ${
      any.skew_sign>0?"amber":any.skew_sign<0?"green":""}"
      title="均值−众数（宪章55 选桶闸的同一把尺）· 模型相对市场：${
        LAB[any.side]||"—"}${any.lean_buckets==null?"":
        "（"+(any.lean_buckets>0?"+":"")+any.lean_buckets.toFixed(2)+" 桶）"}"
      >${SKEW_LAB[String(any.skew_sign)]}</span>`:"";
    const note=e.seated_stage_count?"":
      `<span class="muted">${(REASON[e.absent_reason]||["—",""])[0]}</span>`;
    // "No TX" used to cover both an aerodrome that never publishes one and a
    // station-day the rotation never polled.  The first is a fact about the
    // airport and closes the question; the second is an outage and opens it.
    // ZUUU sat unpolled from 2026-09-02 to 2026-09-06 -- AWC publishing TX35
    // the whole time -- reading as though Chengdu simply did not issue one.
    const TAF_ABSENCE={
      station_issues_no_tx:["该站不发温度组",
        "该机场的 TAF 从不带 TX 组（45 站里有 28 站如此），P0-M 结构上无法服务该站"],
      never_captured:["TAF 未采到",
        "该站会发 TX 组，但这个站日一条前向采集都没有——是采集缺口，不是报文属性"],
      captured_without_tx:["TAF 无温度组",
        "该站日采到了 TAF，但报文里没有覆盖当地峰值的 TX 组"],
    };
    const tafGap=TAF_ABSENCE[e.taf_state]||TAF_ABSENCE.captured_without_tx;
    const taf=e.taf_tx_c==null
      ?`<div class="muted ${e.taf_state==="never_captured"?"down":""}"
         title="${h(tafGap[1])}">${h(tafGap[0])}</div>`
      :`<div class="muted">TAF 最高 <span class="mono" style="color:var(--up)">${
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
      HARD_EXCLUSION_PLUS_CLOCK_CEILING_SOFT_LR_SUSPENDED_V6:"硬下界+天花板(软层暂停)",
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
        <div class="mono">${e.market_slug
          ? `<a class="mkt" target="_blank" rel="noopener noreferrer"
              href="https://polymarket.com/event/${encodeURIComponent(
                e.market_slug)}"
              title="打开 ${h(e.station_id)} ${h(e.event_date)} 的盘口"
              >${h(e.station_id)} ↗</a>`
          : h(e.station_id)}</div>
        <div class="muted">${h(e.station_label)}</div>
        <div class="muted" style="font-size:11px">${STATE[state][1]} ·
          盘口 °${h(unit)}</div></td>
      ${cell(e.stages.P0,"",(e.refusals||{}).P0,"P0")}
      ${cell(e.stages["P0-M"],taf,(e.refusals||{})["P0-M"],"P0-M")}
      ${cell(e.stages.P1,drift,(e.refusals||{}).P1,"P1")}
      <td>${side}${note}</td>
      <td class="mono">${e.settled_tmax_c==null?"—"
        :asUnit(e.settled_tmax_c,unit).toFixed(1)+"°"+h(unit)}</td>
      <td class="mono">${pnl}</td>
      <td>${liveCell(LIVE_BY_SD[e.station_id+"|"+e.event_date])}</td></tr>`;
  };
  const head=`<tr><th>站点</th><th>P0 建仓</th><th>P0-M 确认</th><th>P1 盯盘</th>
    <th title="均值−众数">偏斜</th><th>实测</th><th>实得</th>
    <th title="这一站这一天的实盘意向（live_submit_authorized=1）。左边各列是纸面账本，两套账不可相加">实盘</th></tr>`;
  const dates=S.roster_event_dates||[];
  const block=(day,index)=>{
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
    const got=seats.reduce((s,v)=>s+v.stake_usdc,0);
    const fill=seats.length?` · 实投 ${usd2(got)}${
        short3?`<span class="pill red">桶数低于本阶段下界 ${short3}</span>`:""}`:"";
    return {day:day, count:rows.length, p0:p0, seated:seated,
      pane:`<div data-daypane="${h(day)}"${index?" hidden":""}>
      <div class="card">
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:baseline">
          <b class="mono">${h(day)}</b>
          <span class="muted">${rows.length} 站 · P0 建仓 ${p0} · 任一阶段有座位 ${seated}${ruleNote}${fill}</span></div>
        <div class="scroll" style="margin-top:10px"><table>
          <thead>${head}</thead><tbody>${rows.map(row).join("")}</tbody></table></div>
      </div></div>`};
  };
  // Forty-five stations times thirteen days.  It used to stack thirteen
  // <details>, newest open -- which is already an admission that nobody
  // reads this across dates, and still cost twelve summary bars of height
  // and a click to get anywhere.  Same day tabs as the centre-bias roster
  // (user ruling, 2026-08-26), 全部 for reading it through or searching it
  // with the browser's own find, which cannot see into a hidden pane.
  // The pill carries the P0 seat count, not the station count: every day
  // has all forty-five stations by construction, so that number describes
  // the registry rather than the day and reads as noise on thirteen pills.
  const blocks=dates.map((day,index)=>block(day,index));
  const dayNav=`<nav class="daytabs">${blocks.map((b,i)=>
    `<button type="button" data-day="${h(b.day)}"${i?"":' data-active="1"'}
      title="${b.count} 站 · P0 建仓 ${b.p0} · 任一阶段有座位 ${b.seated}">${h(b.day)}
      <span class="muted">建仓 ${b.p0}</span></button>`).join("")
    }<button type="button" data-day="all">全部 <span class="muted">${
      blocks.length} 天</span></button></nav>`;
  const legend=Object.entries(REASON).map(([k,v])=>
    `<div class="muted">· <b>${v[0]}</b>：${v[1]}</div>`).join("");
  // Newest day open, everything older folded away beneath it.
  return `<h2>逐站逐日明细：选了什么、买到了什么</h2>
  <p class="sub">每个事件日一组，注册表里的每个站都在，包括没有座位、甚至当天没算出概率的
  ——没坐比坐了更值得看清原因。
  每格上行是买到的桶，下方是横梁与胜率 @ 成本 · 赔率；再下一行是这一笔的腿数与实投，
  展开「逐腿」是每条腿的份数 / 均价 / 资金成本
  （该腿实付的 USDC 与它占整篮资金的比重；带 ≈ 的是按建仓盘口价折算的下界，
  早于逐腿现金记账的旧行才会出现）。
  空格里写的是账本自己给出的拒绝理由，不再统一算作「未入座」。
  P1 格里的「预测最高」是当天<b>最后一次重算</b>的点预测（后验加权中心，随盘口单位显示），
  旁边注明它出自哪个条件化器：<b>物理路径</b>是只看观测的 v5 预测器（带生效强度），
  「仅硬下界」表示该次重算没有任何软证据参与——那不是预测，只是排除。
  每行只讲这一天：跨日均值在「市场与执行」的「逐站汇总（跨日）」逐站汇总。日期在下面的页签里选，默认最新一天；
  「全部」是原来的通栏视图，也是浏览器自带查找唯一能搜到所有日期的那一档。</p>
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
  <div class="why"><b>桶数下界是选桶层的规则。</b>宪章 50/50a 要求席位 ≥3 桶
  （P1 是 ≥2），低于下界才标红。宪章 65 之后建仓一律吃单，整篮要么按卖价买齐、
  要么不买，所以这一栏只有一个腿数——从前那套"挂出去几腿、买到几腿、三个完成率"
  是挂单车道的账，那条车道已经不在跑。</div>
  <div class="why">${legend}</div>
  <div data-daytabs="roster">${dayNav}${blocks.map(b=>b.pane).join("")}</div>`;
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
  return `<h2>四大模型的离散度</h2>
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
  return `<h2>四大模型的命中率：原始 vs 校正后</h2>
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


function h5BiasAccuracy(){
  const A = D.h5_bias_accuracy || {};
  if(!A.station_days) return "";
  const worse = A.mean_delta_c > 0;
  const verdict = A.significant
    ? (worse ? `<span class="pill red">H5 显著更差</span>`
             : `<span class="pill green">H5 显著更准</span>`)
    : `<span class="pill amber">样本不足以判定</span>`;
  const dayRows = (A.days||[]).map(d => `<tr>
    <td>${d.event_date}</td><td class="num">${d.stations}</td>
    <td class="num">${d.mean_delta_c>0?"+":""}${d.mean_delta_c.toFixed(4)}</td>
    <td class="num">${d.h5_better}/${d.stations}</td></tr>`).join("");
  const sysRows = (A.systems||[]).map(s => `<tr>
    <td>${s.system}</td><td class="num">${s.cells}</td>
    <td class="num">${s.mean_delta_c>0?"+":""}${s.mean_delta_c.toFixed(4)}</td>
    <td class="num">${s.h5_better}:${s.h5_worse}</td></tr>`).join("");
  return `<h2 style="margin-top:28px">H5 vs H15 订正准度（校准口径） ${verdict}</h2>
  <p class="sub">同一批已结算站日、同一批原始预报，分别用两代偏差值订正，比订正后
  离真值有多远。Δ = H5 误差 − H15 误差，<b>负数代表 H5 更准</b>。这是纯算术反事实，
  不需要重放，所以每多一个结算日样本就自动变厚。</p>
  <div class="kpi" style="margin:10px 0 4px">
    <div class="k muted"><div class="lab">站日样本</div>
      <div class="val">${A.station_days}</div>
      <div class="sub2">共 ${A.cells} 个系统×提前量单元</div></div>
    <div class="k ${A.significant?(worse?"red":"green"):"amber"}">
      <div class="lab">站日平均 Δ（负=H5更准）</div>
      <div class="val">${A.mean_delta_c>0?"+":""}${A.mean_delta_c.toFixed(4)}°C</div>
      <div class="sub2">中位 ${A.median_delta_c>0?"+":""}${A.median_delta_c.toFixed(4)}°C</div></div>
    <div class="k ${A.significant?"red":"amber"}"><div class="lab">H5 更准 : 更差</div>
      <div class="val">${A.h5_better}:${A.h5_worse}</div>
      <div class="sub2">聚类符号检验 z=${A.sign_z}</div></div>
  </div>
  <div class="why"><b>为什么样本按站日数、不按单元数：</b>同一个站同一天的十几个
  单元共享一套天气，不是独立样本。按单元数检验会得出 z=−5.21 那种假显著；
  按站日聚类后是 z=${A.sign_z}，${A.significant?"已过 ±1.96 线":"还没过 ±1.96 线，方向仅供参考"}。
  <b>取版严格按事件日之前</b>：H15 用换代前最后一版，H5 用该事件日之前最后一版，
  两边都不许看见被打分的那一天。</div>
  <table style="margin-top:12px"><thead><tr>
    <th>事件日</th><th>站数</th><th>平均 Δ°C</th><th>H5 更准站数</th>
  </tr></thead><tbody>${dayRows}</tbody></table>
  <h3 style="margin-top:16px">按系统拆</h3>
  <table><thead><tr>
    <th>系统</th><th>单元</th><th>平均 Δ°C</th><th>更准:更差</th>
  </tr></thead><tbody>${sysRows}</tbody></table>`;
}


function h5Readiness(){
  const H = D.h5_readiness || {}; const days = H.days || [];
  if(!days.length) return "";
  const latest = days[days.length-1];
  const ready = (H.scoreable_days || []);
  const pill = latest.share >= 0.9
    ? `<span class="pill green">舰队已换代 ${(100*latest.share).toFixed(0)}%</span>`
    : `<span class="pill amber">换代中 ${(100*latest.share).toFixed(0)}%</span>`;
  const rows = days.map(d => `<tr>
    <td>${d.event_date}</td>
    <td class="num">${d.decisions}</td>
    <td class="num">${d.served_under_h5}</td>
    <td class="num"><b>${(100*d.share).toFixed(1)}%</b></td>
    <td class="num">${d.provisional_only}</td>
    <td class="num">${d.settled_chains || "—"}</td>
    <td>${d.share>=0.9 && d.settled_chains>0 ? "可打分" : ""}</td>
  </tr>`).join("");
  return `<h2 id="h5">偏差半衰期 H5 换代进度 ${pill}</h2>
  <p class="sub">偏差层的半衰期 2026-08-27 从 15 天改为 5 天。要在账本上给这个改动
  打分，那一天的决策必须确实由 H5 拟合的订正表服务过——这里逐条决策数，不是数表。</p>
  <div class="why"><b>为什么按决策数而不按表数：</b>每张订正表有 30 天有效期而舰队
  每天重拟合，所以同一个 scope 会同时躺着约 21 代旧表，而一次决策只读它激活指针
  指名的那一张。按表数会把这些没人读的旧代算进分母——2026-08-29 因此显示 18%，
  而它的决策口径是 100%。<b>临时成员（provisional）单列不计入</b>：它们按设计
  就没有自己的订正表，借用同站已认证成员的参数，不属于任何半衰期。</div>
  <table style="margin-top:12px"><thead><tr>
    <th>事件日</th><th>已标定决策</th><th>其中 H5 服务</th><th>占比</th>
    <th>纯临时成员决策</th><th>已结算链</th><th></th>
  </tr></thead><tbody>${rows}</tbody></table>
  ${ready.length ? `<div class="why" style="margin-top:10px"><b>已可打分：</b>
  ${ready.join("、")}。<b>但首次实跑（2026-08-30，8-29 单日）判定为无判别力</b>：
  H5 臂 31 链 −31.2%、H15 反事实臂 26 链 −8.8%，差额 +19.33 全部由 RPLL 与 SAEZ
  两条链扛着——剔 1 剩 +8.08、剔 2 剩 +0.01、剔 3 转负 −1.21，符号 5:7 反而更多链
  变差，且两臂开出的链集合本身就不同（H5 独有 7 条、H15 独有 2 条）。
  单日 31 条链远低于账本判例要求的量级，<b>结论是"还不能判"，不是"H15 更好"</b>。
  要有结论得攒到几十个事件日。</div>` : ""}`;
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
  // Three answers, not two.  "Incomplete" and "will fill in on its own" were
  // being said as one word, and for the nine 2026-07-30 scopes the second
  // half is false: their day predates their own AWC capture and AWC stopped
  // answering for that date in late August, so a reader told to wait for a
  // backfill waits forever.  The sweep now counts each reason's scopes on
  // both sides of the retention window and this shows whichever apply.
  const expiredTotal = (T.refusals || [])
    .reduce((sum, r) => sum + (r.expired_count || 0), 0);
  const why = (T.refusals || []).map(r => {
    const consistency = r.blocker_class !== "AVAILABILITY";
    const expired = r.expired_count || 0;
    const live = consistency ? 0 : (r.recoverable_count || 0);
    const pills = [];
    if (consistency) pills.push(`<span class="pill red">证据矛盾</span>`);
    if (live) pills.push(`<span class="pill amber">证据不全${
      expired ? " " + live : ""}</span>`);
    if (expired) pills.push(`<span class="pill">源头已过期${
      live || consistency ? " " + expired : ""}</span>`);
    return `<tr>
    <td>${h(r.label || r.reason)}</td>
    <td class="mono">${r.count}</td>
    <td>${pills.join(" ")}</td>
    <td class="mono muted">${h(r.example)}</td>
  </tr>`;
  }).join("");
  return `<h2>真值物化健康度</h2>
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
      「证据矛盾」要人看一眼，不会自己好；${expiredTotal
        ? `「源头已过期」是 AWC 已经不再回答那一天了（保留窗 30 天，${
          h(s.awc_retention_start || "")} 之前的日子问不到），
          ${expiredTotal} 个站日既补不上也不用再等。`
        : "「源头已过期」没有，被挡住的日子都还在 AWC 的 30 天保留窗内。"}</div>
  </div>` : ""}
  ${rows ? `<table style="margin-top:12px"><thead><tr>
    <th>事件日</th><th>在册站数</th><th>已物化</th><th>缺口</th><th>状态</th>
  </tr></thead><tbody>${rows}</tbody></table>` : ""}
  <div class="muted" style="margin-top:8px">${rows
    ? `只列有缺口的事件日；近 ${cov.length} 天里另有 ${cleanDays} 天在册站全部物化，不列。`
    : `近 ${cov.length} 天每一天的在册站都有真值行，没有可列的缺口。`}</div>`;
}

function regional(){
  const R = D.regional || {};
  const lanes = R.lanes || [], ready = R.readiness || [],
        verdicts = R.verdicts || [];
  const pct = x => x==null ? "—" : Math.round(x*100)+"%";
  const laneCards = lanes.map(l => `<div class="card">
    <div class="muted">${h(l.label)} · ${h(l.region)}
      <span class="pill ${l.health==="green"?"green":l.health==="amber"?"amber":"red"}"
        style="float:right">${l.health==="green"?"在采":l.health==="amber"?"待观察":"断采"}</span></div>
    <div class="big">${(l.row_count||0).toLocaleString()} 行</div>
    <div class="muted">${l.station_count||0}/${l.roster_count||0} 站已落行 ·
      ${l.station_days||0} 站日 · 最近采集 ${hours(l.last_capture_age_hours)}前</div>
    <div class="muted">${h(l.transport)}${l.backfillable?"":" · <b>不留历史，错过永失</b>"}</div>
    ${l.note?`<div class="muted" style="margin-top:4px">${h(l.note)}</div>`:""}
  </div>`).join("");
  const readyRows = ready.map(r => `<tr>
    <td>${h((lanes.find(l=>l.system_id===r.ensemble_system_id)||{}).label
      || r.ensemble_system_id)}</td>
    <td class="mono">${r.training_case_count||0} / ${r.required_train_case_count||5}</td>
    <td class="mono">${r.strict_holdout_case_count||0} / ${r.required_strict_holdout_case_count||3}</td>
    <td><span class="pill ${r.state==="ACCUMULATING_STRICT_OOS"?"amber":"green"}">${
      r.state==="ACCUMULATING_STRICT_OOS"?"攒案例中":h(r.state||"")}</span></td>
  </tr>`).join("");
  const verdictRows = verdicts.map(v => `<tr>
    <td class="mono">${h(v.station_id)}</td>
    <td>${h(v.system_label)}</td>
    <td class="mono ${v.passes_all_three?"up":""}">${v.passes_all_three
      ? "三门全过" : Object.entries(v.gates||{}).filter(([,ok])=>!ok)
        .map(([g])=>({crps:"CRPS",nll:"NLL",brier:"Brier"}[g]||g)).join("/")+" 未过"}</td>
    <td class="mono">${pct(v.candidate_exact)} vs ${pct(v.incumbent_exact)}</td>
    <td class="mono">${pct(v.candidate_within_1)}</td>
    <td>${!v.in_roster
      ? `<span class="pill red">实测反向·剔除</span>`
      : v.has_rows ? `<span class="pill green">在采</span>`
      : `<span class="pill amber">在册·等首行</span>`}</td>
  </tr>`).join("");
  return `<h2>区域模型：采集道与赛道</h2>
  <p class="sub">三条高分辨率区域模型采集道（2026-08-28 起）：每小时自动把
  HRRR/NBM/MSM/ICON-EU 的"这个站这天的预报最高温"存进语料库。
  语料攒够、过三道门的站对，赢到的是<b>扩展概率包络的证据席位</b>——
  不是融合成员：单值模型没有散布，做成员会把分布拉尖。</p>
  <div class="why"><b>为什么买它们：</b>全球模型 25km 一格，把台北盆地周围的山、
  城市热岛全平均掉了。8/27 亚特兰大降温，3km 的 HRRR 提前一天报出日最高
  35.3→28.7°C。46 对站×模型实测：过门对的次晨直中中位 38%，在职分布只有 30%。</div>
  <div class="grid g4" style="margin-top:12px">${laneCards}</div>
  ${readyRows ? `<div class="card" style="margin-top:12px">
    <div class="muted">赛道：攒够 5 个训练日 + 3 个留出日就自动评
      CRPS/NLL/Brier 三道门（判据来自 worker 自己的 readiness，不是页面重算）</div>
    <table style="margin-top:8px"><thead><tr>
      <th>系统</th><th>训练案例</th><th>留出案例</th><th>状态</th>
    </tr></thead><tbody>${readyRows}</tbody></table></div>` : `
  <div class="muted" style="margin-top:12px">worker readiness 里还没有这些系统——
    语料落行后会自动出现。</div>`}
  <div class="card" style="margin-top:12px">
    <div class="muted">46 对扫描的判决（钉在案）：直中 = 次晨最可能桶恰好是结算桶的天数占比，
      "vs" 后是在职晨间分布同日的成绩</div>
    <table style="margin-top:8px"><thead><tr>
      <th>站</th><th>模型</th><th>三道门</th><th>直中 vs 在职</th><th>±1 桶</th><th>处置</th>
    </tr></thead><tbody>${verdictRows}</tbody></table>
    <div class="muted" style="margin-top:8px">差 NLL 一门的站也在采（MSM 判例：
      语料是未来闸门重新提问的对象，停采的站永远赢不回席位）；
      "实测反向"= 在职分布反而更准，采了只会花钱买噪声；
      "在册·等首行"= 已裁定要采、还没轮到，不是被剔除。
      处置读的是采集名册（裁定），不是库里有没有行（进度）。</div>
  </div>`;
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
  return `<h2>OOS 打分：CRPS / Brier / NLL</h2>
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
      证据 ${r.serving_days} 天${r.min_qualifying_days
        ? " / 需 " + r.min_qualifying_days + " 天" : ""}${r.serving_cells ?
        " · " + r.serving_cells.toLocaleString() + " 个格子" : ""} ·
      上次拟合 ${hours(r.fit_age_hours)}</div>
    ${r.forward_nll_gain == null ? "" : `<div class="muted">
      前瞻 NLL 收益 ${r.forward_nll_gain.toFixed(4)}${
      r.min_forward_nll_gain == null ? "" :
      "，门槛 " + r.min_forward_nll_gain}</div>`}
    ${r.ast_days_held == null ? "" : `<div class="muted"
      style="margin-top:8px;border-top:1px solid rgba(128,128,128,.25);
             padding-top:8px">
      <b>证据时钟</b>：边界取<b>服务版本</b>
      <span class="mono">${h(r.served_version || "—")}</span>
      ${r.watchdog_discrepancy
        ? `<span class="pill amber" title="服务函数的 AST 在该版本开始服务之后变过：
若那次改动确实动了分布，请升 posterior_version；若只是记账/回放门，无需处理">
           看门狗：算术变了但版本没升</span>`
        : `<span class="pill green">看门狗：版本与算术同步</span>`}
      <br>代码指纹 <span class="mono">${h(r.ast_fingerprint || "")}</span>
      已保持 ${r.ast_days_held.toFixed(2)} 天
      ${r.ast_generations ? "· 累计 " + r.ast_generations + " 代" : ""}
      ${r.ast_longest_quiet_days == null ? "" :
        "· 历史最长安静窗 " + r.ast_longest_quiet_days.toFixed(1) + " 天"}
      <br>2026-08-27 起指纹只报警不清零：它认代码不认分布，
      只补记账的提交曾与真改算术一样清空八天证据。</div>`}
  </div>`).join("");
  return `<h2>中心/宽度两标量</h2>
  <p class="sub">最后一层修正只有两个数：把整条分布左右平移多少（中心 δ），
  再横向拉宽或收窄多少倍（宽度 λ）。δ=0、λ=1 就是原样不动。</p>
  <div class="why"><b>为什么必须按阶段分开拟合：</b>P0 面对的是一堆模型融出来的
  分布，P1 面对的是当天实测已经把它收窄之后的分布——同一对数字不可能对两者都成立。
  一个阶段还没接线或还在攒证据，是按原样服务，不是出错。
  <b>只有前瞻试算确实变好（NLL 收益超过门槛）才会换上去</b>，试算变差就退回原样。<br>
  <b>"攒证据中"为什么可能一直攒不满（2026-08-27 实测）：</b>证据窗从"服务算术上次
  改变"起算，而那个时刻由被服务函数的 AST 指纹判定——<b>指纹只看代码变没变，
  不看分布变没变</b>：一条只补记账字段、或只开一扇回放门的提交，与真正改算术一样
  把时钟清零。当天 P0 四小时内换了三代指纹，P1 自 08-23 起<b>从未</b>持有过一代
  满 8 天。每张卡片底部的"证据时钟"就是这件事的读数：它不满 8 天，这一层就永远
  按原样服务。</div>
  <div class="grid g3" style="margin-top:12px">${cards}</div>`;
}

// Does the distribution the seat is built on actually cover the day?  The
// section above says what the centre/width layer serves; this one scores it
// against the truth, split by how wide the served distribution called
// itself.  Added 2026-09-05 because the answer decided a charter: the core
// of a flat day under-claims, the seat gate refuses windows that really do
// have edge, and the fix went in at the window layer (clause 81) only after
// this table showed the misses are scatter rather than a direction the
// width layer could correct.
function centreLanding(){
  const L = D.center_landing || {};
  const stages = L.stages || [];
  if(!stages.length) return "";
  const sign2 = (v) => v == null ? "—" :
    (v > 0 ? "+" : "") + v.toFixed(2);
  const blocks = stages.map(st => {
    const body = (st.rows || []).map(r => `<tr>
      <td>${h(r.label)}</td>
      <td class="mono">${r.cells.toLocaleString()}</td>
      <td class="mono ${r.significant ? (r.bias_rungs > 0 ? "up" : "down")
        : "muted"}">${sign2(r.bias_rungs)}</td>
      <td class="mono muted">${r.ci_low == null ? "—" :
        "[" + sign2(r.ci_low) + ", " + sign2(r.ci_high) + "]"}</td>
      <td class="mono muted">${sign2(r.median_rungs)}</td>
      <td class="mono">${r.sd_rungs == null ? "—" :
        r.sd_rungs.toFixed(2)}</td>
      <td class="mono muted">${pct1(r.warm_share)}</td>
      <td class="mono muted">${pct1(r.seat_claim)}</td>
      <td class="mono">${pct1(r.seat_cover)}</td>
      <td class="mono ${r.seat_ratio == null ? "" :
        (r.seat_ratio >= 1.15 ? "up" :
         (r.seat_ratio < 0.95 ? "down" : ""))}">${
        r.seat_ratio == null ? "—" : r.seat_ratio.toFixed(2)}</td>
      <td class="mono muted">${pct1(r.mode_cover)}<span
        style="font-size:10px"> ·${pct1(r.same_window_share)}同窗</span></td>
    </tr>`).join("");
    const flat = (st.flat_stations || []).map(f => `<span class="pill ${
      Math.abs(f.bias_rungs) >= 1 ? (f.bias_rungs > 0 ? "red" : "blue")
      : "muted"}" style="font-size:10.5px" title="${h(f.station)}：${
      f.cells} 个格子、${f.days} 个站日">${
      h(f.station)} ${sign2(f.bias_rungs)}</span>`).join(" ");
    return `<div style="margin-top:16px">
      <div style="display:flex;align-items:baseline;gap:10px">
        <b>${h(st.stage)}</b>
        <span class="muted">${h(st.meaning)}</span>
        <span style="flex:1"></span>
        <span class="muted">${st.cells.toLocaleString()} 个格子
          · ${st.days} 天· ${h(st.since)} 起</span>
      </div>
      <table style="margin-top:8px"><thead><tr>
        <th>服务分布自称的宽度（桶）</th>
        <th>格子</th><th>偏差</th><th>95% 区间</th>
        <th>中位</th><th>散度</th><th>偏暖</th>
        <th>席位窗声称</th><th>席位窗实际</th><th>实际/声称</th>
        <th>众数窗实际</th>
      </tr></thead><tbody>${body}</tbody></table>
      ${flat ? `<div class="muted" style="margin-top:6px">
        平日子（自称宽度 ≥${
        L.flat_threshold_rungs} 桶，共 ${st.flat_cells}
        个格子）按站的偏差，
        取样本至少 8 格的前十：
        ${flat}</div>` : ""}
    </div>`;
  }).join("");
  return `<h2>真值落在离服务均值多远</h2>
  <p class="sub">上一节说中心/宽度层<b>做了什么</b>，
  这一节说它服务出去的分布<b>对不对</b>：
  当天的真实最高温，落在分布均值的哪一侧、差几个桶。
  单位是<b>桶</b>不是度：摄氏市场一桶 1°C、华氏市场 2°F，
  换成桶两边才能放在一列里比。</p>
  <div class="why"><b>为什么量这个：</b>席位就建在均值±1
  这三个桶上（宪章 49/79），整篮正 EV 闸拿来和价格比的也是这个窗口的概率和。
  所以“真值能不能被均值±1 兼住”直接决定了这一天的席位值不值。<br>
  <b>怎么读：</b>偏差是平均落点减均值，正数表示实际比模型说的热；
  95% 区间按<b>天</b>重采样（同一天的天气全队共享，按格子重采样会把区间缩成假的），
  <b>区间含 0 就是没有系统性偏向哪一边</b>；
  右边四列是席位那三个桶的账。<br>
  <b>席位窗三列是这一节的正题：</b>「声称」＝均值桶±1 这三个桶的概率和，
  也就是整篮正 EV 闸拿去和价格比的那个数；「实际」＝真值真的落在这三个桶里的比例；
  再右边是两者之比。<b>分布越宽，声称和实际都在降，但实际降得慢</b>——
  比值从 1.0 一路涨到 1.7，越平的日子核心被低报得越狠。这就是宪章 81 抬价的证据：
  闸拿一个偏低的声称去比价格，把本来有边际的窗口拒掉了。<br>
  <b>别把「实际」下降读成模型在平日子更差：</b>宽分布把质量摊到更多桶上，
  固定三桶窗必然覆盖更少，这大半是算术。真正的判据是那个比值，
  而它反过来说：平日子模型比它自己声称的更准。<br>
  <b>偏差几乎为零、区间含 0</b>——错是“散”不是“偏”。散不能靠把分布收窄来治
  （宽度分档 λ 实测已证），只能先在窗口层把被低报的声称抬回来。
  平日子按站确实有号，但每站只有几个站日，现在拟不了。<br>
  <b>末列的众数窗</b>是同样三个桶、但以最高的那个桶为心，后面小字是两个窗完全重合的比例。
  中间几档它覆盖得多一点（均值被尾巴拉偏），最平那档反过来——
  峰值在一堆几乎等高的桶里是噪声，均值更稳。覆盖多不等于赚钱：
  市场把峰值桶定价最贵，账本已裁定席位以均值为心（宪章 54/75）。</div>
  ${blocks}`;
}


// The live flag belongs where the reader's eye already is (user ruling,
// 2026-08-29): it is the one control on this page that changes what the
// system may do, and it was fifteen sections down inside a table of
// station gates.  The banner carries the switch, and since 2026-08-31 the
// readiness section sits directly beneath it in the same head area, so the
// control and its gates are read together.
// What arming does *right now*, not in general.  The three downstream gates
// disagree with each other often enough that "this starts live trading" is
// sometimes false and sometimes a serious understatement.  With no dialog on
// the way up, the hover text is where that sentence lives.
function lcArmEffect(L){
  const C = L.contracts || {};
  const compiled = L.execution_mode === "production_live";
  if(compiled && C.live_submit_authorized === true){
    // No second step any more.  Until 2026-09-02 arming left
    // data/live_paused.flag in place and this string had to tell the
    // operator to go delete a file -- a switch labelled ARMED over a
    // gateway that refused every order.  The switch now lifts the flag on
    // the way up and places it on the way down, so both branches of this
    // sentence became the same sentence.
    return "举旗即刻开始用真实资金下单 —— 开关同时撤掉网关暂停旗";
  }
  return compiled
    ? "举旗：代码已 live，但合同未授权发单，现在铸不出 live 行"
    : "举旗：代码仍编译为 no-live，它不下单，但 go-live 迁移以它为前提";
}

function liveSwitch(){
  const L = D.live_readiness || {};
  if(!L.available) return "";
  const on = !!L.live_enabled;
  return `<span class="pill ${on?"red":""}" id="lc-head"
    style="font-size:13px;display:inline-flex;gap:7px;align-items:center">
    <b>LIVE</b>
    <button type="button" id="lc-live-btn" class="lc-sw" role="switch"
      aria-checked="${on}" aria-label="上线意向旗" disabled onclick="lcLive()"
      title="${h(on ? "点击落旗，即刻停止发单" : lcArmEffect(L))}"></button>
    <span id="lc-live" class="mono">${on?"ARMED":"OFF"}</span>
    <span class="muted" id="lc-head-note" style="font-size:11.5px"
      title="控制服务未连接时开关不可用">控制服务未连接</span>
  </span>`;
}


function liveOrderCard(L){
  // Live rows are live by identity: live_submit_authorized=1 exists only on
  // rows minted after the go-live migration widened the CHECK.  Every number
  // here is counted off those rows at render, so the card and the ledger
  // cannot disagree.
  const O = L.live_orders || {};
  const X = O.exchange || {};
  const P = O.pnl || {};
  // Two different flags, and they disagree today: the production lane is
  // compiled for live, while the runtime contract still refuses to
  // authorise a live submit.  A pill that reads only the first says
  // "实盘发单中" over a book that cannot mint a live row -- the exact
  // reassurance about a system that has stopped being that system which
  // this panel already got wrong once.
  const C = L.contracts || {};
  const compiled = L.execution_mode === "production_live";
  const authorized = C.live_submit_authorized === true;
  const liveMode = compiled && authorized;
  const state = !compiled ? "代码编译为 no-live"
    : !authorized ? "合同未授权发单"
    : L.submission_paused ? "暂停旗在位" : "实盘发单中";
  const u = (v,d=2)=>v==null?"—":v.toFixed(d)+" U";
  const signed = (v,d=2)=>v==null?"—":((v>=0?"+":"")+v.toFixed(d)+" U");
  const pct = (v,d=1)=>v==null?"—":((v>=0?"+":"")+(100*v).toFixed(d)+"%");
  const tile=(lab,val,sub2,klass)=>`<div class="card">
    <div class="muted">${lab}</div>
    <div class="big ${klass||""}">${val}</div>
    <div class="muted" style="font-size:11.5px">${sub2}</div></div>`;
  // Grouped by decision, because the decision is the basket.  The solver
  // sizes the whole support set as one joint action and the selector
  // refuses any proper subset of it -- then the exchange, which has no
  // basket primitive, takes one order per bucket.  A flat leg table shows
  // the exchange's shape and hides ours: read row by row it says the
  // system picks temperature buckets one at a time, when in fact 31°C and
  // 32°C on 2026-08-31 were two legs of a single WMKK basket that stood
  // or fell together.  So legs sit under their basket, and a basket that
  // is one leg wide says so rather than being indistinguishable from a
  // leg that lost its siblings.
  const groups = [];
  const byDecision = new Map();
  for (const r of (O.rows||[])){
    let g = byDecision.get(r.decision_id);
    if (!g){
      g = {decision_id:r.decision_id, station_id:r.station_id||"",
            event_date:r.event_date||"", window:r.execution_window_seconds,
            legs:[], notional:0, latest:r.created_at};
      byDecision.set(r.decision_id, g);
      groups.push(g);
    }
    g.legs.push(r);
    g.notional += (r.requested_notional_usdc||0);
    if (String(r.created_at) > String(g.latest)) g.latest = r.created_at;
  }
  // Legs of one basket carry one deadline; if they ever disagree the
  // narrower one is the one that governs, so show that rather than the
  // first row's.
  for (const g of groups){
    const w = g.legs.map(l=>l.execution_window_seconds).filter(v=>v!=null);
    g.window = w.length ? Math.min(...w) : null;
  }
  // One line per basket, one <details> per event date, settled dates
  // closed by default (user ruling, 2026-09-05: the leg table grew with
  // every order and took the viewport).  A leg is a chip: bucket, price x
  // shares, its end state, and -- when the state alone says nothing --
  // the reason its latest event carried.  A settled date shows the bucket
  // the day landed in and each basket's money.
  const short = (r)=>{
    const v = String(r||"");
    if(!v) return "";
    if(v.startsWith("invalid amount")) return "低于$1";
    if(v==="clob_collateral_evidence_unavailable") return "网关抵押读数缺失";
    if(v==="live_station_held") return "站被暂扣";
    if(v==="live_station_barred") return "站被禁入";
    if(v==="live_station_probation") return "站在考察期";
    if(v==="live_intent_disarmed") return "实盘旗未举";
    if(v==="live_submit_not_authorized") return "合同未授权";
    return v.length>28 ? v.slice(0,26)+"…" : v;
  };
  // What the basket's own plan recorded, keyed by the decision that is the
  // basket.  Nothing here is derived: a basket whose plan is no longer in
  // the store gets no ring and no state, because an unmarked basket beats a
  // guessed one.
  const SEATS = O.plan_state || {};
  const planOf = (g)=>SEATS[g.decision_id] || null;
  const centerOf = (g)=>String((planOf(g)||{}).center||"");
  // The day's shape, in the words charter 75 places the window with.  Left
  // is the cold tail, right the warm one -- named by where the tail runs,
  // not by which way the page happens to draw the ladder.
  const SKEW_LABEL = {
    SYMMETRIC: ["对称日", "均值与众数相差不到半格 —— 宪章 79 把 P0 席位定为均值桶±1"],
    RIGHT_SKEWED: ["右偏 · 暖尾", "均值在众数之上超过半格：尾巴拖向偏热的一侧"],
    LEFT_SKEWED: ["左偏 · 冷尾", "均值在众数之下超过半格：尾巴拖向偏冷的一侧"],
  };
  // Charter 122's band, as the plan recorded it: [lo, hi) in bucket steps,
  // hi null meaning no upper bound.
  const bandText = (plan)=>{
    let b = plan.seat_width_band;
    if(typeof b === "string"){ try { b = JSON.parse(b); } catch(e) { b = null; } }
    if(!Array.isArray(b) || b.length !== 2) return "";
    return ` [${Number(b[0]).toFixed(2)}, ${b[1]==null?"∞":Number(b[1]).toFixed(2)}) 格`;
  };
  const skewChip = (g)=>{
    const plan = planOf(g);
    if(!plan || !plan.skew) return "";
    const label = SKEW_LABEL[String(plan.skew)];
    if(!label) return "";
    const gap = plan.gap_buckets;
    const read = plan.skew_source === "reading";
    const detail = read
      ? `均值−众数 = ${gap==null?"—":(gap>0?"+":"")+Number(gap).toFixed(2)} 格`
        + `（判定阈 ±${plan.threshold_buckets==null?"0.50":Number(plan.threshold_buckets).toFixed(2)} 格）`
        + (plan.sigma_buckets==null ? "" : ` · 服务宽度 σ ${Number(plan.sigma_buckets).toFixed(2)} 格`)
        + (plan.seat_width_in_band==null ? "" :
            (plan.seat_forced
              ? ` · 宽度在档内${bandText(plan)}，席位由第 79 条固定为中心±1`
              : (plan.seat_width_in_band
                  ? ` · 宽度在档内${bandText(plan)}，但当天不是对称日，席位由枚举自选`
                  : ` · 宽度在档外${bandText(plan)}，席位由枚举自选（第 79 条只在它被实测过的宽度上生效）`)))
        + " · 取自该决策自己的计划所记形态读数"
      : "取自该计划记下的方向（宪章 75 下方向就是偏态符号）。"
        + "证据不新鲜的那天一律记 NEUTRAL，所以这一路读数可能把当天读成对称——"
        + "形态读数是 2026-09-06 起才写进计划的。";
    const tone = String(plan.skew)==="SYMMETRIC" ? "" : "down";
    return `<span class="pill ${tone}" style="font-size:10px"
      title="${h(label[1])} · ${h(detail)}">${h(label[0])}${read?"":"*"}</span>`;
  };
  const legChip = (r, truth, center)=>{
    const st = String(r.state||"");
    const hit = truth && String(r.bucket)===truth;
    const cls = st==="filled" ? (truth ? (hit?"up":"down") : "") : "muted";
    const mark = st==="filled" ? (truth ? (hit?"✓中":"✗") : "●")
      : st==="failed" ? "✗" : st==="cancelled" ? "—" : "…";
    const why = (st==="failed"||st==="cancelled") ? short(r.reason) : "";
    // The centre is marked on the frame, not on the text: the text colour
    // already carries the leg's own outcome (filled, hit, refused), and
    // painting the centre green there would make one colour answer two
    // different questions.
    const mid = center && String(r.bucket)===String(center);
    const ring = mid
      ? "border-color:var(--up);box-shadow:inset 0 0 0 1px var(--up)"
      : "border-color:var(--line,#ddd)";
    return `<span class="mono ${cls}" style="display:inline-block;padding:1px 6px;margin:1px 3px 1px 0;border:1px solid;${ring};border-radius:5px;font-size:11.5px;white-space:nowrap"
      title="${h(r.bucket)}${mid?" · 均值中心桶":""} · ${h(r.order_intent_id)} · ${h(st)}${r.reason?" · "+h(r.reason):""} · ${h(r.created_at)}">${h(r.bucket)} ${r.limit_price}×${shares(r.requested_size)} <b>${mark}</b>${why?" <span class='muted'>"+h(why)+"</span>":""}</span>`;
  };
  const byDate = new Map();
  for (const g of groups){
    const key = g.event_date || "?";
    if(!byDate.has(key)) byDate.set(key, []);
    byDate.get(key).push(g);
  }
  const truthOf = (g)=> (g.legs.find(l=>l.settled_bucket)||{}).settled_bucket || "";
  const dates = [...byDate.keys()].sort().reverse();
  // A date folds once its money is counted, or once it is behind us: a
  // past day the paper settler never seated (a held station's refused
  // basket) has no settlement row and would otherwise stay open forever.
  const today = new Date().toISOString().slice(0,10);
  // A basket every leg of which the station bar refused never reached the
  // exchange: no fill, no cost, no payout.  Six of the ten lines on the
  // book this morning were these, and they crowded out the four that
  // bought something (user ruling, 2026-09-06: do not list barred
  // stations).  Dropped from the listing, counted on the date line -- a
  // silently shorter list reads as "this is everything".
  const barred = (g)=> g.legs.length > 0
    && g.legs.every(l=>String(l.reason||"")==="live_station_barred");
  const rows = dates.map(day=>{
    const all = byDate.get(day).slice().sort((a,b)=>String(a.station_id).localeCompare(String(b.station_id)));
    const hidden = all.filter(barred);
    const gs = all.filter(g=>!barred(g));
    const daySettled = gs.length>0 && gs.every(g=>truthOf(g));
    const folded = daySettled || (day < today);
    let legsN=0, filledN=0, cost=0, payout=0;
    const basketLines = gs.map(g=>{
      const truth = truthOf(g);
      const gFilled = g.legs.filter(l=>l.state==="filled");
      const gCost = gFilled.reduce((a,l)=>a+(l.requested_notional_usdc||0),0);
      const gPay = truth ? gFilled.reduce((a,l)=>a+(String(l.bucket)===truth?(l.requested_size||0):0),0) : null;
      legsN += g.legs.length; filledN += gFilled.length; cost += gCost; payout += (gPay||0);
      // A basket whose centre it does not itself hold has no green rung,
      // and an unmarked line is indistinguishable from a line whose plan
      // is missing.  Two of the fifty-three baskets on the book are this:
      // gap fills and top-ups that bought a shoulder without the centre.
      // Say which rung is missing rather than leaving the reader to guess
      // whether the mark is broken.
      const mid = centerOf(g);
      const offCenter = mid && !g.legs.some(l=>String(l.bucket)===mid)
        ? `<span class="muted mono" style="font-size:11px"
            title="中心桶取自该决策自己的计划">中心 ${h(mid)} 不在本篮</span>`
        : "";
      const money = gPay==null
        ? `<span class="muted mono" style="font-size:11px">投入 ${gCost.toFixed(2)} U</span>`
        : `<span class="mono ${gPay-gCost>=0?"up":"down"}" style="font-size:11px">${(gPay-gCost>=0?"+":"")+(gPay-gCost).toFixed(2)} U</span>
           <span class="muted mono" style="font-size:11px">投入 ${gCost.toFixed(2)} · 赔付 ${gPay.toFixed(2)} · 落在 ${h(truth)}</span>`;
      return `<div style="display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:5px 0;border-top:1px dashed var(--line,#eee)">
        <span class="pill red" style="font-size:10px">LIVE</span>
        <b class="mono" title="${h(g.decision_id)}">${h(g.station_id||"?")}</b>
        <span class="muted" style="font-size:11px">${g.legs.length} 腿 · ${gFilled.length} 成交</span>
        ${skewChip(g)}
        <span>${g.legs.map(l=>legChip(l, truth, mid)).join("")}</span>
        ${offCenter}${money}</div>`;
    }).join("");
    const pnl = daySettled ? payout - cost : null;
    const barredNote = hidden.length
      ? ` · <span class="muted" title="${h(hidden.map(g=>g.station_id).join("、"))}"
          >禁入 ${hidden.length} 篮未列</span>`
      : "";
    const head = `${h(day)} · ${gs.length} 篮 · ${legsN} 腿 · ${filledN} 成交 · 投入 ${cost.toFixed(2)} U` + (pnl==null
        ? ` · <span class="muted">未结算</span>`
        : ` · <span class="${pnl>=0?"up":"down"}">${(pnl>=0?"+":"")+pnl.toFixed(2)} U</span> · 已结算`)
      + barredNote;
    // A date whose every basket was barred still gets a line: the day had
    // intents, and a missing date answers a different question than an
    // empty one.
    const body = gs.length ? basketLines
      : `<div class="muted" style="font-size:12px;padding:5px 0">
         这一天铸出的 ${hidden.length} 篮全部被站级禁入挡在交易所之外，未列出。</div>`;
    return `<details ${folded?"":"open"} style="margin-top:6px">
      <summary class="mono" style="cursor:pointer;font-size:12.5px">${head}</summary>
      <div style="margin-left:4px">${body}</div></details>`;
  }).join("");
  const moreGroups = "";
  // Everything above is counted off our own tables.  This one row is not:
  // it is what the exchange says is resting on the account right now.  On
  // 2026-08-31 an order this machine placed and then lost track of rested
  // for eighteen minutes; it appeared in no table here, and only asking the
  // account found it.  So the account gets its own row, and a row that
  // cannot be read says so rather than rendering a reassuring zero.
  const O2 = L.open_orders || {};
  const openRows = (O2.orders||[]).map(o=>`<tr>
    <td><span class="pill red" style="font-size:10px">挂单中</span></td>
    <td class="mono">${o.outcome||"—"}</td><td>${o.side||""}</td>
    <td class="mono">${o.price??"—"}</td>
    <td class="mono">${(o.size_matched==null?"?":shares(o.size_matched))
      +"/"+(o.original_size==null?"?":shares(o.original_size))}</td>
    <td class="mono" title="${o.id}">${String(o.id||"").slice(0,14)}…</td>
    <td class="mono">${o.created_at??""}</td></tr>`).join("");
  const openCard = !O2.available
    ? `<div class="note" style="margin-top:9px">⚠️ <b>账户挂单读不到</b>
       <span class="muted">（网关无响应：<span class="mono">${h(O2.error||"unknown")}</span>）——
       这一行问的是交易所，读不到就不是零。</span></div>`
    : O2.count === 0
      ? `<div class="muted" style="font-size:12px;margin-top:9px">✅ 交易所侧
         <b>账户零挂单</b>（问于 <span class="mono">${h(O2.asked_at||"?")}</span>）</div>`
      : `<div style="margin-top:9px"><b class="mono" style="color:var(--red)">
         交易所侧：账户 ${O2.count} 笔挂单</b>
         <span class="muted mono" style="font-size:11px">问于 ${h(O2.asked_at||"?")}</span>
         <span class="muted" style="font-size:12px">—— 逐笔核对；若某笔不在上面的意向表里，
         它就是本机丢了记录的孤儿单，撤掉它。</span>
         <div style="overflow-x:auto"><table><thead><tr><th></th><th>桶</th><th>侧</th>
         <th>价</th><th>成交/总</th><th>order id</th><th>挂单时刻</th></tr></thead>
         <tbody>${openRows}</tbody></table></div></div>`;
  return `<h3 style="margin-top:22px;font-size:15px">实盘订单 · 数量 / ROI / PnL
    <span class="pill ${liveMode&&!L.submission_paused?"red":""}"
      style="font-size:11px">${state}</span></h3>
  <div class="card">
    <div class="grid g4">
      ${tile("实盘意向", (O.intent_count||0)+" 笔",
        "计划名义 "+u(O.notional_usdc), O.intent_count?"":"muted")}
      ${tile("走到交易所", (X.events||0)+" 回执",
        "已提交 "+(O.submitted_count||0)+" · 受理 "+(O.accepted_count||0)+
        " · 对账 "+(X.reconciliations||0)+" 行", (X.events||0)?"":"muted")}
      ${tile("已成交", (P.filled_legs||0)+"／"+((P.filled_legs||0)+(P.pending_legs||0))+" 腿",
        "成交名义 "+u(X.filled_notional_usdc||0)+" · "+(P.station_days||0)+" 个结算站日",
        (P.filled_legs||0)?"":"muted")}
      ${tile("实盘 PnL", P.available?signed(P.pnl_usdc):"—",
        P.available?("投入 "+u(P.cost_usdc)+" · 赔付 "+u(P.payout_usdc))
          :h(P.reason||"还没有实盘成交"),
        !P.available?"muted":(P.pnl_usdc>=0?"up":"down"))}
      ${tile("实盘 ROI", P.available?pct(P.roi):"—",
        P.available?("命中 "+(P.won_legs||0)+"/"+(P.filled_legs||0)+" 腿"
          +((P.pending_legs||0)?" · "+P.pending_legs+" 腿待结算":""))
          :(authorized?"合同已授权，等第一笔成交"
            :"合同未授权发单 · 分子分母都不存在，不是 0%"),
        !P.available?"muted":((P.roi||0)>=0?"up":"down"))}
    </div>
    ${openCard}
    <details class="note"><summary>实盘 ROI / PnL 是怎么算的 · 为什么现在是「—」而不是 0%</summary>
      <div class="muted">钱<b>全部来自实盘成交列</b>：成本＝对账表报回来的成交名义
      <span class="mono">filled_notional_usdc</span>，赔付＝真值落在该腿桶里时每股 1 USDC、
      否则 0。结算桶标签取自结算行——那个标签是生产结算器<b>按各站自己的档位</b>从真值推出来的
      （°C 站和 2°F 站不共用一套），在这里重推一遍等于手抄生产规则。<br>
      成交了但所在站日还没结算的腿计入<b>待结算</b>，<b>不当亏损算</b>：
      把未结算日读成零赔付，会让实盘账本每天下午都显得比实际差。<br>
      现在显示「—」而不是「0%」，是因为<b>一笔实盘成交都没有</b>——
      分子分母都不存在，写 0% 是在报一个没测过的数。<br>
      当前两个闸的读数：代码执行模式 <span class="mono">${h(L.execution_mode||"—")}</span>、
      合同 <span class="mono">${h(C.execution_mode||"—")}</span>
      （<span class="mono">live_submit_authorized=${C.live_submit_authorized===true?"true":"false"}</span>、
      <span class="mono">calls_order_gateway=${C.calls_order_gateway===true?"true":"false"}</span>），
      读自 <span class="mono">${h(C.runtime_config||"—")}</span>。
      <b>两个都为真才可能铸出实盘行</b>，都是每次渲染现读，不是写死的句子。</div>
    </details>
    ${rows?`<div style="margin-top:11px">${rows}</div>${moreGroups}`
      :`<div class="muted" style="margin-top:9px">尚无实盘意向。第一条出现时此表自动列出。</div>`}
    ${(X.blocking||[]).length?`<div class="muted" style="margin-top:8px;font-size:11.5px">
      对账阻断：${(X.blocking||[]).map(b=>h(b.reason)+" ×"+b.count).join(" · ")}</div>`:""}
  </div>
  ${orderBookCard(O.book||{})}`;
}


// The live intent book, and only it (user ruling, 2026-08-31): paper
// intents are a different book and were being read as this one's rows.
// With no live intent yet the panel says so in one line rather than
// rendering three empty tables -- but it does not disappear, because a
// missing panel and an empty one answer different questions.
function orderBookCard(B){
  const paper = B.paper_excluded||0;
  const foot = `<details class="note"><summary>这里只数实盘 · 纸面意向为什么不算在内</summary>
      <div class="muted">本板块每一个数都过
      <span class="mono">live_submit_authorized=1</span> 这一道筛——
      那一位由 runtime 合同给，只有它为 1 的意向才会真的交到交易所。<br>
      账本里另有 <b>${paper}</b> 笔 <span class="mono">live_submit_authorized=0</span> 的纸面意向，
      <b>不计入上面任何一个数</b>：它们跑的是同一套选桶与定规模，但走到交易所边界就停，
      和实盘是两本账，并排放进同一张表只会被眼睛加到一起。
      纸面那本的成绩在主看板的「三态对比」和「组合风险指标」里。</div>
    </details>`;
  if(!B.total) return `<div class="card" style="margin-top:12px">
    <div class="muted">实盘意向簿</div>
    <div style="margin:6px 0">还没有实盘意向。<span class="muted">第一笔铸出后，
      这里按<b>状态 / 事件日 / 站</b>三个维度分列笔数、名义与均价。</span></div>
    ${foot}</div>`;
  const u = (v,d=2)=>v==null?"—":v.toFixed(d)+" U";
  const share = (n)=>B.total?((100*n/B.total).toFixed(1)+"%"):"—";
  const tile=(lab,val,sub2,klass)=>`<div class="card">
    <div class="muted">${lab}</div>
    <div class="big ${klass||""}">${val}</div>
    <div class="muted" style="font-size:11.5px">${sub2}</div></div>`;
  const stateRows=(B.by_state||[]).map(r=>`<tr>
    <td class="mono">${h(r.state)}</td>
    <td class="mono">${r.count}</td>
    <td class="mono">${share(r.count)}</td>
    <td class="mono">${(r.notional_usdc||0).toFixed(2)}</td></tr>`).join("");
  const dayRows=(B.by_day||[]).map(r=>`<tr>
    <td class="mono">${h(r.event_date)}</td>
    <td class="mono">${r.count}</td>
    <td class="mono">${(r.notional_usdc||0).toFixed(2)}</td>
    <td class="mono">${r.stations}</td></tr>`).join("");
  const stationRows=(B.by_station||[]).map(r=>`<tr>
    <td class="mono">${h(r.station_id)}</td>
    <td class="mono">${r.count}</td>
    <td class="mono">${(r.notional_usdc||0).toFixed(2)}</td>
    <td class="mono">${r.days}</td>
    <td class="mono">${(r.mean_limit_price||0).toFixed(3)}</td></tr>`).join("");
  return `<div class="card" style="margin-top:12px">
    <div class="muted">实盘意向簿 · 只数 <span class="mono">live_submit_authorized=1</span> 的行</div>
    <div class="grid g4" style="margin-top:8px">
      ${tile("实盘意向", B.total+" 笔", "均单 "+u(B.mean_notional_usdc,3))}
      ${tile("计划名义合计", u(B.notional_usdc), "全为 BUY/YES")}
      ${tile("平均限价", (B.mean_limit_price||0).toFixed(4), "按笔简单平均")}
      ${tile("覆盖", B.stations+" 站",
        B.decisions+" 个决策 · "+B.cycles+" 个周期<br>"+
        h(B.first_event_date)+" → "+h(B.last_event_date))}
    </div>
    <div class="grid g3" style="margin-top:12px">
      <div><div class="muted" style="margin-bottom:4px">按状态</div>
        <div class="scroll"><table><thead><tr><th>状态</th><th>笔</th>
          <th>占比</th><th>名义u</th></tr></thead>
          <tbody>${stateRows}</tbody></table></div></div>
      <div><div class="muted" style="margin-bottom:4px">按事件日（近 14 个有单的日子）</div>
        <div class="scroll" style="max-height:300px"><table><thead><tr><th>事件日</th>
          <th>笔</th><th>名义u</th><th>站</th></tr></thead>
          <tbody>${dayRows}</tbody></table></div></div>
      <div><div class="muted" style="margin-bottom:4px">按站（名义前 12）</div>
        <div class="scroll" style="max-height:300px"><table><thead><tr><th>站</th>
          <th>笔</th><th>名义u</th><th>日</th><th>均价</th></tr></thead>
          <tbody>${stationRows}</tbody></table></div></div>
    </div>
    ${foot}
  </div>`;
}


// The three-state comparison is a portfolio reading, not a control: it
// says in money what the bars cost or saved, on the same book the strip at
// the top of the page counts.  It moved out of the live control section
// and onto the main board (user ruling, 2026-08-31) -- it answers "how is
// the account doing", not "may this book go live".  The prose that
// justifies the split folds: it is read once, and it was pushing the four
// numbers it explains below the fold.
function barSplit(){
  const L = D.live_readiness || {};
  const SP = L.bar_split || {};
  if(!SP.barred) return "";
  const money=(v)=>v==null?"—":((v>=0?"+":"")+v.toFixed(2));
  const pct=(v)=>v==null?"—":((v>=0?"+":"")+(100*v).toFixed(1)+"%");
  const cell=(k,label,note)=>{const r=SP[k]||{};return `<div class="card">
    <div class="muted">${label}</div>
    <div class="big ${r.roi==null?"":(r.roi>=0?"up":"down")}">${pct(r.roi)}</div>
    <div class="mono ${(r.pnl_usdc||0)>=0?"up":"down"}">${money(r.pnl_usdc)} U</div>
    <div class="muted" style="font-size:11.5px">投入 ${money(r.staked_usdc)} U ·
      ${r.stations||0} 站 · ${r.settled_days||0} 个结算站日<br>${note}</div></div>`;};
  return `<h3 style="margin-top:22px;font-size:15px">三态对比：上线 / 待观察 / 禁入</h3>
  <div class="card">
    <div class="muted">同一窗口、同一规则指纹，纸面账本对三边一视同仁地建仓结算——
      <b>禁的是下单，不是记账</b>。它衡量的是这些闸拦掉了什么，<b>不是对未来的预测</b>。</div>
    <div class="grid g2" style="margin-top:8px">
      ${cell("live","上线（已判且过关）","真上线只有这一组会下单")}
      ${cell("probation","待观察（证据不足）","够 "+(L.minimum_settlements_to_qualify||5)+" 笔结算才判，判过了自动上线")}
      ${cell("barred","禁入（已判且不合格）","这些钱本来会亏/赚在这里")}
      ${cell("fleet","全舰队","不设任何闸的对照")}
    </div>
    <details class="note"><summary>三态是怎么分的 · 为什么「待观察」不是「禁入」</summary>
      <div class="muted">待观察的站正是靠继续记账攒够证据毕业的。
      <b>默认禁止</b>（2026-08-31 裁定）：新站不上线，攒够
      ${L.minimum_settlements_to_qualify||5} 笔结算被判过且没被禁，才自动上线。
      <b>「待观察」不是「禁入」</b>——只有 2 笔的站不是坏站，把它塞进禁入表还会占掉 15 个名额之一。</div>
    </details>
  </div>`;
}


function liveReadiness(){
  const L = D.live_readiness || {};
  if(!L.available) return "";
  const allow = new Set(L.station_allowlist||[]);
  const barred = new Set(L.station_exclusions||[]);
  const waived = new Set(L.station_probation_waivers||[]);
  const heldSet = new Set(L.station_probation_holds||[]);
  // Probation of either kind gets the hourglass: a station kept out because
  // its settlement count has not got there yet reads the same to a trader as
  // one the operator is holding, and the row's switches say which it is.
  const waiting = new Set(L.station_probation||[]);
  const autoBar = new Set(L.auto_barred||[]);
  const GATE = {claim_honest:"声称诚实", centre_bias:"中心偏差",
    chains:"链数", roi:"账本ROI", truth_fresh:"真值新鲜"};
  // Three states, not two: on the allowlist, merely unselected, or barred.
  // An unselected station can still be added; a barred one is refused by
  // the control server, so it must not read as simply "not chosen".
  //
  // And each of the three is now a switch rather than a reading.  The bar
  // was editable only by hand in JSON until 2026-09-02 -- which is why it
  // moved four times in one day through a text editor and why one of those
  // days is recorded in the file as a same-day round trip -- while probation
  // had no control at all: an operator who wanted a station recorded but not
  // traded had the bar as the only tool, and the bar is a verdict.
  // The five-dimension read shares this row (user ruling, 2026-09-09).
  // Every gated station gets a line whether or not it is flagged: the old
  // separate table listed only stations with at least one flag, which meant
  // a clean station's evidence simply was not on the page.
  const FX = stationFailureIndex();
  const FLAG = {realization:"实现", claim:"声称", centre:"中心",
    coverage:"覆盖", persistence:"持续"};
  const fnum = (v,d,suffix="") => v==null ? "—"
    : ((v>=0&&suffix==="C")?"+":"") + Number(v).toFixed(d) + suffix;
  const EDGE = 'style="border-left:1px solid var(--line)"';
  const fverdict = (station) => {
    if(!FX) return "";
    const s = FX.by[station];
    if(!s) return `<td class="mono muted" ${EDGE}>—</td><td class="muted">—</td>`;
    const bad = Object.keys(FLAG).filter(k=>(s.flags||{})[k]);
    return `<td class="mono ${s.flag_count>=4?"down":""}" ${EDGE}><b>${
        s.flag_count}</b>/5</td>
      <td>${bad.map(k=>`<span class="pill amber">${FLAG[k]}</span>`).join(" ")
        || '<span class="muted">✓</span>'}</td>`;
  };
  const fcells = (station) => {
    if(!FX) return "";
    const s = FX.by[station];
    if(!s) return `<td class="mono muted" ${EDGE}>—</td>`
      + '<td class="mono muted">—</td>'.repeat(6);
    // Thin stations are not good stations, they are unjudged ones, so the
    // count is muted rather than hidden and the cell says so in place.
    const thin = s.settled_count < 6;
    return `<td class="mono${thin?" muted":""}" ${EDGE}>${s.settled_count}${
        thin?" 薄":""}</td>
      <td class="mono">${s.basket_coverage==null?"—":
        (100*s.basket_coverage).toFixed(0)+"%"}</td>
      <td class="mono">${fnum(s.realization_ratio,2)}</td>
      <td class="mono">${fnum(s.calibration_ratio,2)}</td>
      <td class="mono">${fnum(s.centre_residual_c,2,"C")}</td>
      <td class="mono">${s.early_pnl_usdc>=0?"+":""}${
        s.early_pnl_usdc.toFixed(1)}<span class="muted">(${s.early_chains})</span></td>
      <td class="mono">${s.late_pnl_usdc>=0?"+":""}${
        s.late_pnl_usdc.toFixed(1)}<span class="muted">(${s.late_chains})</span></td>`;
  };
  const rows = (L.stations||[]).map(r=>`<tr class="${barred.has(r.station)?"muted":""}">
    <td class="mono" style="white-space:nowrap"><span data-lc-state="${h(r.station)}">${allow.has(r.station)?"✔ ":(barred.has(r.station)?"⛔ ":(waiting.has(r.station)?"⏳ ":""))}</span>${h(r.station)}${r.station_label?` <span class="muted">${h(r.station_label)}</span>`:""}</td>
    <td style="white-space:nowrap">${lcGateButtons(r.station, barred.has(r.station),
      waived.has(r.station), heldSet.has(r.station),
      autoBar.has(r.station))}</td>
    <td class="mono ${r.score>=5?"up":(r.score>=4?"":"down")}">${r.score}/5</td>
    <td class="muted">${r.add_candidate?"☆建议加入 ":""}${r.release_candidate?"★建议放出 ":""}${Object.entries(r.gates).filter(([k,v])=>!v).map(([k])=>GATE[k]).join("、")||"✓"}</td>
    ${fverdict(r.station)}
    <td class="mono" ${EDGE}>${r.ratio==null?"—":r.ratio.toFixed(2)}</td>
    <td class="mono">${r.bias_c==null?"—":(r.bias_c>0?"+":"")+r.bias_c.toFixed(2)}</td>
    <td class="mono">${r.chains}</td>
    <td class="mono ${r.roi==null?"":(r.roi>=0?"up":"down")}">${r.roi==null?"—":((r.roi>=0?"+":"")+(100*r.roi).toFixed(1)+"%")}</td>
    <td class="mono ${r.recent_roi==null?"":(r.recent_roi>=0?"up":"down")}">${r.recent_roi==null?"—":((r.recent_roi>=0?"+":"")+(100*r.recent_roi).toFixed(1)+"%")}</td>
    <td class="mono ${r.climate_jump_c==null?"":(r.climate_jump_c>1.76?"down":"up")}">${r.climate_jump_c==null?"—":r.climate_jump_c.toFixed(2)}</td>
    <td class="mono ${r.shape_gap_buckets==null?"":(r.shape_gap_buckets>0.93?"down":"")}">${r.shape_gap_buckets==null?"—":r.shape_gap_buckets.toFixed(2)}</td>
    ${fcells(r.station)}
  </tr>`).join("");
  const CT = L.contracts || {};
  const authorised = CT.live_submit_authorized === true;
  return `<h2>Live 上线准备</h2>
  <p class="sub">下面的开关是<b>上线意向旗</b>。能不能真发单由<b>两个闸一起决定</b>：
  代码编译的执行模式 <span class="mono">${h(L.execution_mode||"—")}</span>，
  和 runtime 合同 <span class="mono">${h(CT.execution_mode||"—")}</span>
  （<span class="mono">live_submit_authorized=${authorised?"true":"false"}</span>）。
  <b>${authorised?"两个闸都已放行——旗亮即会用真实资金发单。"
    :"合同未授权：旗亮也铸不出 live 契约，意向照造但不出网关。"}</b>
  合同读自 <span class="mono">${h(CT.runtime_config||"—")}</span>${
    CT.minted_at_utc?`（铸于 ${h(CT.minted_at_utc)}）`:""}——
  <b>按执行模式选文件，和生产同一条规则</b>。这里曾经不论模式都读 no-live 那份，
  于是在 go-live 迁移之后，页面对着一个合同说 true 的系统报 false。</p>
  <p class="sub"><b>网关暂停旗归这个开关管（操作者裁定 2026-09-02）。</b>
  举旗时自动撤掉 <span class="mono">data/live_paused.flag</span>，落旗时先把它
  放回去再落旗——<b>没有第二个要记得的文件</b>。在此之前举旗只写意向旗，
  开关写着 ARMED 而网关逐笔拒单，页面只能提示"再去删一个文件"。
  这个文件仍然是随时可用的急停：手动 <span class="mono">touch</span> 它，
  所有车道立刻停。当前
  ${L.submission_paused
    ? `<b style="color:var(--red)">暂停旗在位</b>——它是被手动放上去的（或某次举旗撤旗失败），网关不发单`
    : "暂停旗不在位"}。</p>
  <div class="card">
    <div class="grid g2">
      <div><div class="muted">凯利系数 portfolio_risk_fraction</div>
        <div class="big" id="lc-kelly">${L.kelly==null?"—":L.kelly}</div>
        <button type="button" disabled class="lc-w" onclick="lcAdjust('kelly',-0.05)">−0.05</button>
        <button type="button" disabled class="lc-w" onclick="lcAdjust('kelly',0.05)">＋0.05</button></div>
      <div><div class="muted">预算 target_remaining_budget_usdc</div>
        <div class="big" id="lc-budget">${L.budget_usdc==null?"—":L.budget_usdc}</div>
        <button type="button" disabled class="lc-w" onclick="lcAdjust('budget',-50)">−50</button>
        <button type="button" disabled class="lc-w" onclick="lcAdjust('budget',50)">＋50</button></div>
      <div><div class="muted">单站上限 station_cap_usdc
          <span style="font-size:11px">（默认 15，范围 1–30；三个本金型边界同步派生）</span></div>
        <div class="big"><span id="lc-cap">—</span><span
          style="font-size:13px;font-weight:400"> u</span></div>
        <button type="button" disabled class="lc-w" onclick="lcAdjust('cap',-1)">−1</button>
        <button type="button" disabled class="lc-w" onclick="lcAdjust('cap',1)">＋1</button>
        <button type="button" disabled class="lc-w" onclick="lcAdjust('cap',-5)">−5</button>
        <button type="button" disabled class="lc-w" onclick="lcAdjust('cap',5)">＋5</button></div>
    </div>
    <div class="muted" style="margin-top:8px" id="lc-status">控制服务未连接——启动：
      <span class="mono">.venv/bin/python scripts/live_control_server.py</span>
      （只监听 127.0.0.1:8788，每次改动写审计日志）</div>
  </div>
  ${liveOrderCard(L)}
  <div class="card" style="margin-top:12px">
    <div class="muted">首批站点闸门 · <b>三个开关全是人工的，点击即开启关闭</b>——
      写盘、记审计、下一轮生产立刻照它执行；表里的五闸（实现/声称∈[0.88,1.18]、
      |中心偏差|≤0.8°C、链≥5、ROI＞−8%、真值≤2天新）是给你看的评分，不再自己决定谁上线。
      <br>✔ 上线（${(L.station_allowlist||[]).length}）：<span class="mono" id="lc-batch-live">${(L.station_allowlist||[]).join(" ")||"—"}</span>
      <br>⛔ 禁入（${(L.station_exclusions||[]).length}）：<span class="mono" id="lc-batch-barred">${(L.station_exclusions||[]).join(" ")||"—"}</span>${(L.auto_barred||[]).length?`　其中自动层写的（虚线框）：<span class="mono">${(L.auto_barred||[]).join(" ")}</span>`:""}
      <br>✅ 豁免（证据不够也上线）：<span class="mono" id="lc-batch-waived">${(L.station_probation_waivers||[]).join(" ")||"—"}</span>
      　⏳ 待观察（${(L.station_probation||[]).length}，其中人工按住的：<span class="mono" id="lc-batch-held">${(L.station_probation_holds||[]).join(" ")||"—"}</span>）
      ${((L.add_candidates||[]).length||(L.release_candidates||[]).length)?`<br><span class="pill amber">待复核</span>
      随数据每轮重算的候选（<b>只提示不执行</b>）：${(L.add_candidates||[]).length?`建议加入 <span class="mono">${(L.add_candidates||[]).join(" ")}</span>`:""}
      ${(L.release_candidates||[]).length?`　建议放出 <span class="mono">${(L.release_candidates||[]).join(" ")}</span>`:""}`:""}</div>
    <details class="note"><summary>三个开关分别做什么 · 禁入与候选的判据</summary>
      <div class="muted"><b>⛔ 禁入</b>＝不给这个站下单，<b>但账本照常记账</b>——禁的是下单不是记账，
      不然下次要不要放出来就没有证据可读。点开＝写一条操作者裁定进
      <span class="mono">excluded_stations</span>；点关＝写一条 <span class="mono">released_stations</span>，
      <b>自动层从此不再碰这个站</b>（不写这条的话，下一轮 sweep 几分钟内就会把它禁回去，开关看起来像坏的）。
      <b>没有席位上限</b>（操作者裁定 2026-09-02，取代原来的 15 席）：禁一个站只损失一个跳过的市场，
      而上限做的事是把判决藏起来——它一边逼着人工新禁去挤掉自动层的行（当天 KHOU/SAEZ/ZUUU/VILK/KSEA
      就是这样被放出来的），一边让存下来的名单不再是判据算出来的那份。<br>
      <b>✅ 豁免</b>＝结算笔数不够也直接上线（把"等够 ${L.minimum_settlements_to_qualify||5} 笔"这道门跳过去）。
      <b>⏳ 待观察</b>＝够格了也先不下单，<b>不占禁入席位、也不等于说它坏</b>——账本继续给它建仓结算，
      证据照攒。三者互斥：开一个自动关掉另外两个；已禁入的站，另外两个开关是灰的（服务端也会拒）。<br>
      三个开关都算<b>操作者裁定</b>，自动层一律不碰带裁定的站。<b>2026-09-02 起禁入真的挡下单了</b>：
      在此之前这张名单只活在这个页面、控制服务的 allowlist 写入口和 go-live 迁移的前置检查里，
      发单那条路根本不读它——EDDM、KSEA、LTAC 都留着"被禁期间铸出来的 live 意向"。<br>
      禁入是<b>风险裁定不是测量结论</b>：14 种自动规则做逐日 walk-forward，最好的一个 p=0.346——与随机剔同样多的站无异；
      同一份手挑名单固定不动反而 +17.0 点（p=0.002）。所以<b>操作者裁定的行永远不由机器改</b>；自动层只管操作者没裁定过的站，判据＝第一看兑现比（实得÷期望；期望＜3U 时读不出，改看第二项）、第二看实得差，进出用滞回避免来回摆；<b>不再设站数上限</b>（2026-09-02 解除）：判据说禁就禁，名单即判决，人工新禁也不再挤掉任何人。<br>
      候选判据：加入＝全样本 ROI≤−25% 且链≥5；放出＝已禁且最近 5 个结算日 ROI＞0（≥3 笔）<b>且中心偏差与声称诚实两闸都过</b>——只看 ROI 会把靠盘后 P1 铁板捞回来的站放出去（EPWA 判例）。</div>
    </details>
    ${FX?`<details class="note" style="margin-top:6px"><summary>「不行」的五个维度分别指认哪一层</summary>
      <div class="muted"><b>为什么不能只看亏多少：</b>一个站只有五到十条已结算链，
      单靠运气就能亏得很难看，所以"亏损前十"里永远混着运气差的。能分开的是
      <b>同时在互不重复的维度上不行</b>——每一项指认不同的一层：
      <b>实现</b>（实现/期望 &lt;0，模型想去的日子反而亏，病在声称或执行）、
      <b>声称</b>（命中/声称 &lt;${FX.thresholds.claim_floor}，声称虚高，病在校准层）、
      <b>中心</b>（残差 &gt;${FX.thresholds.centre_limit_c}°C，中心就是错的，病在偏差层，最上游）、
      <b>覆盖</b>（选桶盖住真值 &lt;${(100*(FX.thresholds.coverage_floor||0)).toFixed(0)}%，病在选桶层）、
      <b>持续</b>（前后半段都亏，是站的性格不是那两周）。前后半段分割点 ${h(FX.split)}。<br>
      <b>旗标只列不求和：</b>它们会互相打架且那正是信息——EHAM 赚得远超模型期望
      却另外三项不行，加总成一个分数恰好会把这件事抹掉。
      <b>薄样本标"薄"不剔除：</b>两条链的站不是好站，是没被判过的站。
      <b>这是诊断不是处方：</b>前瞻检验实测，按前半段排名剔掉最差十站，
      后半段只从 −55.7 改善到 −20.4，仍是亏的——剔站买不来一本赚钱的账。<br>
      <b>这五维与左边的五闸是两套测量，不可互相代入</b>：窗口和人口都不同——
      同一天 CYYZ 的闸读 1.04、五维的命中/声称读 0.66，中心偏差与中心残差、
      链与已结算同理。合成一列等于编一个哪一层都没算过的数。</div>
    </details>`:""}
    <div class="scroll" style="max-height:420px;margin-top:6px">
    <table style="white-space:nowrap"><thead><tr><th>站</th><th title="三个开关都是人工裁定，点击即开启关闭，改动立刻写盘并记审计。禁入=不下单（记账照旧）；豁免=证据不够也上线；待观察=够格也先不下单。三者互斥。">闸门（人工）</th><th>过闸</th><th>未过项</th>${
      FX?`<th title="五个维度各指认不同的一层，见上方折叠说明；与左边的五闸是两套测量" style="border-left:1px solid var(--line)">不行</th><th>不行的维度</th>`:""}
      <th style="border-left:1px solid var(--line)">实现/声称</th><th>中心偏差</th>
      <th>链</th><th>ROI</th><th title="最近 5 个结算日">近5日</th><th title="该站真值的日际跳变均值(°C)：天气性格，>1.76 的站在真边际枯竭期实测 −43%（留存组 +23%，p=0.023）。外生、慢变、先于账本。">跳变/日</th><th title="近14天池子里模型分布的均值−众数距离(桶)：跳变在模型层的影子(r=+0.32)，>0.93 为高。">形状gap</th>${
      FX?`<th title="以下七列属五维诊断，窗口与左侧五闸不同" style="border-left:1px solid var(--line)">已结算</th><th>覆盖率</th><th>实现/期望</th><th>命中/声称</th><th>中心残差</th><th>前半段</th><th>后半段</th>`:""}
      </tr></thead><tbody>${rows}</tbody></table></div>
    ${FX?`<div class="muted" style="margin-top:4px;font-size:11.5px">
      五维诊断：${FX.worst.length?h(FX.worst.join("、"))+" 至少四项不行":"没有站四项以上不行"}，
      另有 ${FX.clean} 站一项都不占。</div>`:""}
  </div>`;
}

function lcApi(path, body){
  return fetch("http://127.0.0.1:8788"+path, body?{
    method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)}:{}).then(r=>r.json()).then(j=>{
      // A refused or half-applied request answers with {error}.  Returning
      // it as if it were state let lcRender die on a TypeError whose alert
      // named a missing property instead of the server's reason -- which
      // matters most for the one case that can now half-apply: intent
      // armed, gateway pause flag not lifted.
      if(j && j.error) throw new Error(j.error);
      return j;
    });
}
function lcRender(s){
  if(!s) return;
  const kelly=document.getElementById("lc-kelly");
  const budget=document.getElementById("lc-budget");
  const live=document.getElementById("lc-live");
  if(kelly) kelly.textContent = s.kelly.portfolio_risk_fraction;
  if(budget) budget.textContent = s.budget.target_remaining_budget_usdc;
  const cap=document.getElementById("lc-cap");
  if(cap) cap.textContent = (s.station_cap&&s.station_cap.station_cap_usdc!=null)
    ? s.station_cap.station_cap_usdc
    : ((s.station_cap&&s.station_cap.default_usdc)||15)+"(默认)";
  if(live) live.textContent = s.live.enabled?"ARMED":"OFF";
  // The switch is repainted from the server's answer, never from the click:
  // an optimistic flip would show ARMED over a refused arm, which is the one
  // lie this control must never tell.
  const sw=document.getElementById("lc-live-btn");
  if(sw){
    sw.setAttribute("aria-checked", String(!!s.live.enabled));
    // The hover text carries what a dialog used to say, so it has to follow
    // the flag: left alone it would keep offering to arm a lane that is
    // already armed.
    sw.title = s.live.enabled
      ? "点击落旗：立刻落下网关暂停旗并停止发单"
      : lcArmEffect(D.live_readiness || {});
  }
  // The banner switch carries the same state; paint it red only when the
  // flag is actually up, so a glance at the top of the page answers "may
  // this book go live".
  const head=document.getElementById("lc-head");
  if(head) head.className = "pill" + (s.live.enabled?" red":"");
  const headNote=document.getElementById("lc-head-note");
  if(headNote){
    const L = D.live_readiness || {};
    const C = L.contracts || {};
    const compiled = L.execution_mode === "production_live";
    const authorized = C.live_submit_authorized === true;
    // The pause flag is read from the control server's answer, not from
    // L: the page snapshot was taken at render time and the switch moves
    // the flag on this very click, so L would keep reporting the state the
    // click just left.  Since the switch owns the flag, an armed-and-paused
    // reading no longer means "now go delete a file" -- it means the two
    // disagree, which only a hand `touch` or a failed lift can cause, and
    // the note says so instead of describing a step that no longer exists.
    const paused = s.live.submission_paused === true;
    headNote.textContent = !s.live.enabled ? "控制服务已连接"
      : compiled && authorized && !paused
        ? "实盘中 · 网关正在下单"
        : compiled && authorized
          ? "已举旗但暂停旗仍在位 —— 有人手动放了它，网关不发单"
          : compiled
            ? "旗已举起 · 代码 live，合同未授权发单"
            : "旗已举起 · 代码仍编译为 no-live";
  }
  const status=document.getElementById("lc-status");
  if(status) status.textContent =
    "控制服务已连接 · 每次改动写 data/live_control_audit.jsonl";
  document.querySelectorAll(".lc-w, #lc-live-btn")
    .forEach(b=>b.disabled=false);
  // The station gate repaints from the server's answer like the rest: an
  // optimistic flip would show a bar the server refused, which is the one
  // lie a control that stops orders must never tell.
  lcPaintGates(s);
  window.__lcState = s;
}
function lcAdjust(knob, delta){
  const s = window.__lcState; if(!s) return;
  if(knob==="kelly"){
    const next = Math.round((s.kelly.portfolio_risk_fraction+delta)*100)/100;
    lcApi("/api/kelly", {value: next}).then(lcRender)
      .catch(e=>alert("失败: "+e));
  } else if(knob==="cap"){
    const cur = (s.station_cap&&s.station_cap.station_cap_usdc!=null)
      ? s.station_cap.station_cap_usdc
      : ((s.station_cap&&s.station_cap.default_usdc)||15);
    const next = Math.round((cur+delta)*100)/100;
    lcApi("/api/station-cap", {value: next}).then(lcRender)
      .catch(e=>alert("失败: "+e));
  } else {
    const next = Math.round(
      (s.budget.target_remaining_budget_usdc+delta)*100)/100;
    lcApi("/api/budget", {value: next}).then(lcRender)
      .catch(e=>alert("失败: "+e));
  }
}
function lcLive(){
  const s = window.__lcState; if(!s) return;
  const next = !s.live.enabled;
  // One click, both directions, no dialog -- operator ruling 2026-09-01,
  // reaffirmed after I shipped a confirmation on the arming half.  What the
  // switch is about to do is written beside it in the banner: the state
  // word, the note naming which downstream gates are open, and the readiness
  // section directly beneath.  A modal would repeat what the reader is
  // already looking at, and it would sit in the way of the one action that
  // must never be slowed -- the same click, a moment later, is the stop.
  const btn = document.getElementById("lc-live-btn");
  // Held down for the round trip: the switch is one control over one piece
  // of state, and a second click while the first is in flight sends the
  // opposite instruction against a state that has already moved.
  if(btn) btn.disabled = true;
  // The server still refuses an arm that presents no token; the switch is
  // the confirmation now, so the switch carries it.
  lcApi("/api/live", next
      ? {enabled:true, confirm:"LIVE", by:"dashboard"}
      : {enabled:false, by:"dashboard"})
    .then(lcRender)
    .catch(e=>{ if(btn) btn.disabled=false; alert("失败: "+e); });
}
// The station gate: three switches per row, one click each.
//
// They are drawn disabled and only the control server's answer enables them
// -- same contract as every other control on this page.  A gate that looks
// clickable while nothing is listening would let an operator believe a
// station was barred because the pill turned red.
const LC_GATES = {
  bar:   {path:"/api/station-bar",     key:"barred", label:"⛔禁入"},
  waive: {path:"/api/station-waiver",  key:"waived", label:"✅豁免"},
  hold:  {path:"/api/station-hold",    key:"held",   label:"⏳待观察"},
};
function lcGateTip(kind, on, isAuto){
  if(kind==="bar") return on
    ? (isAuto?"自动层禁的。点击＝人工放出，并记一条操作者裁定，让 sweep 不再碰它"
            :"点击＝人工放出（记 released_stations，自动层从此不碰）")
    : "点击＝人工禁入：不再给这个站下单，账本照常记账";
  if(kind==="waive") return on
    ? "点击＝撤销豁免，回到按结算笔数判"
    : "点击＝豁免：结算笔数不够也上线";
  return on ? "点击＝解除按住，回到按结算笔数判"
            : "点击＝按住：够格也先不下单，不占禁入席位";
}
function lcGateButtons(station, isBarred, isWaived, isHeld, isAuto){
  const state = {bar:isBarred, waive:isWaived, hold:isHeld};
  return Object.keys(LC_GATES).map(kind=>{
    const on = !!state[kind];
    return `<button type="button" class="lc-gate" data-station="${station}"
      data-gate="${kind}" data-owner="${kind==="bar"&&isAuto?"auto":"operator"}"
      role="switch" aria-checked="${on}" disabled
      title="${lcGateTip(kind,on,isAuto)}"
      onclick="lcGate(this)">${LC_GATES[kind].label}</button>`;
  }).join("");
}
function lcGate(btn){
  const kind = btn.dataset.gate, station = btn.dataset.station;
  const spec = LC_GATES[kind]; if(!spec) return;
  const on = btn.getAttribute("aria-checked")==="true";
  // Every switch is held down for the round trip, not only the one clicked:
  // the server clears the other two on the station it moves, so more than
  // one of them may be about to change and none may show a state that has
  // not come back yet.
  document.querySelectorAll(".lc-gate").forEach(b=>{b.disabled=true;});
  const body = {station: station, by: "dashboard"};
  body[spec.key] = !on;
  lcApi(spec.path, body).then(lcRender).catch(e=>{
    // Repaint from the last state the server confirmed, then say why.  A
    // refusal here is usually the server enforcing the exclusivity the row
    // shows (no waiver or hold on a barred station), and leaving the row
    // disabled would look like the page had broken instead.
    lcPaintGates(window.__lcState);
    alert("失败: "+e);
  });
}
function lcPaintGates(s){
  const live = s && s.live; if(!live) return;
  const barred = new Set(live.station_exclusions||[]);
  const waived = new Set(live.station_probation_waivers||[]);
  const held   = new Set(live.station_probation_holds||[]);
  const allow  = new Set(live.station_allowlist||[]);
  const auto   = new Set((live.station_bar_sources||{}).auto||[]);
  document.querySelectorAll(".lc-gate").forEach(b=>{
    const station = b.dataset.station, kind = b.dataset.gate;
    const on = kind==="bar" ? barred.has(station)
             : kind==="waive" ? waived.has(station) : held.has(station);
    const isAuto = kind==="bar" && auto.has(station);
    b.setAttribute("aria-checked", String(on));
    b.dataset.owner = isAuto ? "auto" : "operator";
    b.title = lcGateTip(kind, on, isAuto);
    // The server refuses a waiver or a hold on a barred station rather than
    // accepting one that would be inert, so those two must not look
    // available while the bar is up.
    b.disabled = kind!=="bar" && barred.has(station);
  });
  const waiting = new Set(live.station_probation||[]);
  document.querySelectorAll("[data-lc-state]").forEach(el=>{
    const station = el.dataset.lcState;
    el.textContent = barred.has(station) ? "⛔ "
      : allow.has(station) ? "✔ " : (waiting.has(station) ? "⏳ " : "");
    const row = el.closest("tr");
    if(row) row.className = barred.has(station) ? "muted" : "";
  });
  const put=(id,text)=>{const el=document.getElementById(id);
    if(el) el.textContent = text;};
  put("lc-batch-live",  (live.station_allowlist||[]).join(" ")||"—");
  put("lc-batch-barred",(live.station_exclusions||[]).join(" ")||"—");
  put("lc-batch-waived",(live.station_probation_waivers||[]).join(" ")||"—");
  put("lc-batch-held",  (live.station_probation_holds||[]).join(" ")||"—");
}
function lcInit(){
  if(!document.getElementById("lc-status")) return;
  lcApi("/api/state").then(lcRender).catch(()=>{});
}

// Click a header to sort by that column.  Numbers, percentages, prices and
// odds all read as numbers; anything else falls back to text, and an em dash
// always sinks so empty cells never crowd the top of either direction.
function makeSortable(table){
  // Some tables carry meaning in their row order and cannot be sorted at
  // all: the live order book groups legs under a basket header row, so any
  // column sort interleaves headers with legs from other baskets and the
  // grouping silently becomes a lie.  Those tables opt out here rather
  // than growing a group-aware comparator, and they opt out before the
  // headers are made to look clickable -- a pointer cursor on a header
  // that does nothing is its own small deception.
  if(table.dataset.grouped==="1") return;
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
  if(!S.available) return `<h2>模型校准总检</h2>
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

  return `<h2>模型校准总检：六层各自及格没有</h2>
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
  已并入「band 表」，与产生它的修正系数放在同一条闭环里；本节的校准灯仍由同一份
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
  return `<h2>四大模型的权重：谁在场、占多少</h2>
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
  // Grouped by what the reader is asking, not by the order the sections
  // were built in (user ruling, 2026-08-29).  Five questions: is the data
  // arriving, is the model any good, what shape is the probability and the
  // position it buys, does the market let that make money, and is the
  // machine running.  Each group carries a colour so the tab bar and the
  // group titles agree without the reader matching text.
  {id:"upstream", label:"上游数据", tone:"#58a6ff",
   note:"喂进来的东西到没到：四家模型的到货、区域采集道、缺口",
   sections:[[members,"模型数据到货"],[gaps,"数据缺口"]]},
  {id:"model", label:"模型", tone:"#d2a8ff",
   note:"模型本身准不准：订正与权重、离散度、命中率、OOS、中心/宽度、总检",
   sections:[[tables,"偏差订正与权重"],
     [servingWeights,"四大模型权重"],[regional,"区域模型"],
     [dispersion,"模型离散度"],[hitRate,"模型命中率"],
     [oosScores,"OOS 打分"],[centerWidth,"中心/宽度"],
     [centreLanding,"真值离均值多远"],
     [scorecard,"模型校准总检"],[h5Readiness,"H5 换代进度"],[h5BiasAccuracy,"H5 订正准度"]]},
  {id:"shape", label:"概率与持仓形状", tone:"#7ee787",
   note:"概率怎么被分档修正、声称的便宜兑不兑现、注码怎么被固化减仓、分布和实际持仓差在哪",
   sections:[[bandTable,"band 表"],[consolidation,"固化减仓"],
     [windowDivergence,"分布与持仓分家"]]},
  {id:"market", label:"市场与执行门槛", tone:"#e3b341",
   note:"市场肯不肯让这套东西赚钱：覆盖与 EV、篮子命中率、真值落位",
   sections:[[coverage,"覆盖 · 价格 · EV"],[basketHit,"篮子命中率"],
     [truthLanding,"真值落位"]]},
  {id:"health", label:"系统健康", tone:"#f85149",
   note:"机器在不在转：整条链路每一环、真值物化",
   sections:[[chain,"链路"],[materialization,"真值物化"]]},
];
const tabs = `<nav class="tabs" id="tabs">${
  GROUPS.map(g => `<button type="button" data-tab="${g.id}"
    style="--tone:${g.tone}">${g.label}
    <span class="cnt">${g.sections.length}</span></button>`).join("")
}<span class="spacer"></span><button type="button" class="all"
  data-tab="all">全部</button></nav>`;

// Every section gets its own element and id so the jump bar has somewhere
// to land.  The label the bar shows is read back out of the section's own
// <h2> rather than restated here: a chip that disagrees with the heading it
// scrolls to is worse than no chip.
// The homepage board is one section, not two (user ruling, 2026-08-31):
// the tile strip and the three-state comparison of what the bars cost read
// the same account, and splitting them behind another heading and another
// jump chip made a reader navigate between halves of one question.  Each
// part still renders under its own guard.
//
// The portfolio ratios left this board on 2026-09-09 (user ruling) for the
// head status bar: "is the machine turning" and "what is the book worth"
// are the two things read on arrival, and the ratios sat five thousand
// pixels below the bar that answers the first.
function overviewBoard(){
  return safe(overview, "总览")
    + safe(barSplit, "三态对比");
}

const sec = (fn, name, id) =>
  `<section class="sec" id="${id}" data-name="${name}">${safe(fn, name)}</section>`;

const groupHtml = GROUPS.map(g => `<section class="group" data-group="${g.id}"
  style="--tone:${g.tone}">
  <div class="grouptitle">${g.label}<span class="muted">${g.note}</span></div>
  ${g.sections.map(([fn, name], i) =>
    sec(fn, name, `sec-${g.id}-${i}`)).join("")}</section>`).join("");

// The bar is the first thing on the page (user ruling, 2026-09-09).  It was
// already sticky, but sitting under the banner and the live-readiness
// section meant it only became a navigation after the reader had scrolled
// past the two longest blocks on the page -- which is the scroll it exists
// to save.  First element, stuck at zero, visible from the first pixel.
// Live readiness is no longer one of the sections below the bar: it is
// rendered in the head area, directly under the banner that carries the
// LIVE switch (user ruling, 2026-08-31).  The switch and the gates that
// decide whether flipping it does anything are one question, and they were
// split by the tab bar and the overview strip.  It keeps its own .sec
// wrapper, so it still gets a jump chip -- the first one.
// The day-level account, in the order a reader walks it: the strip, the
// per-day table, the per-station-day detail, and then the three tables
// that answer questions the detail raises -- how each station does across
// days, which books were abnormal, and why a station did not seat (user
// ruling, 2026-08-29).  These three used to sit in the market group, two
// scrolls away from the rows they explain.
// The homepage is a dashboard: the health strip, the portfolio tiles and
// their charts, and the day table they summarise (user ruling,
// 2026-08-30).  The four per-station-day tables that used to sit here are
// a different kind of reading -- you go to them with a question, you do
// not scan them -- so they move into their own area together, keeping the
// order the reader asked for: detail first, then the three tables that
// answer what the detail raises.
const TOP = [[overviewBoard, "总览"],
  [daily, "每日统计"], [roster, "逐站逐日明细"],
  [stationSummary, "逐站汇总（跨日）"],
  [refusalStats, "没建仓是因为什么"]];

// Which sections on this page were reused from the previous build, and how
// old each one is.  A cached section that quietly stops refreshing is the
// failure this whole mechanism can cause, so its age is printed rather than
// left for someone to infer from a number that stopped moving.
function sectionCacheNote(){
  const c=D.section_cache||{};
  const NAME={truth_landing:"真值落点",window_divergence:"窗口分歧",
    scorecard:"校准计分卡",h5_readiness:"H5 就绪",skill:"模型技巧",
    h5_bias_accuracy:"H5 偏差准确度",band_residual_audit:"band 残差审计",
    center_landing:"真值离均值多远"};
  const reused=Object.entries(c).filter(([,v])=>v&&v.reused);
  if(!reused.length){
    const built=Object.keys(c).length;
    return built?`本轮全部区块现算 · `:"";
  }
  const worst=Math.max(...reused.map(([,v])=>Number(v.age_sec)||0));
  const list=reused.map(([k,v])=>
    `${NAME[k]||k} ${Math.round((Number(v.age_sec)||0)/60)} 分钟前`).join("、");
  return `<span title="慢变区块按各自节奏重算，其余每轮现算">沿用上一轮的区块：`
    + `${h(list)}（最旧 ${Math.round(worst/60)} 分钟）</span> · `;
}

document.getElementById("app").innerHTML =
  `<div class="topnav" id="topnav">${tabs}<nav class="jump" id="jump"></nav></div>` +
  safe(head, "页头") +
  sec(riskMetrics, "组合风险指标", "sec-risk") +
  sec(liveReadiness, "Live 上线准备", "sec-live") +
  TOP.map(([fn, name], i) => sec(fn, name, `sec-top-${i}`)).join("") +
  groupHtml +
  `<footer>数据生成于 ${h(D.generated_at_utc)} · 只读页面，不写任何数据 ·
    ${sectionCacheNote()}
    <a href="#" id="footer-top">返回顶部</a></footer>`;

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
  const jump = document.getElementById("jump");
  const bar = document.getElementById("topnav");
  const esc = (t) => String(t).replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;").replace(/</g, "&lt;");
  const chip = (node) => {
    if(!node.firstElementChild) return "";
    const head2 = node.querySelector("h2");
    const full = head2
      ? head2.textContent.trim().replace(/\s+/g, " ")
      : (node.dataset.name || "");
    if(!full) return "";
    const short2 = full.split(/[：（(·]/)[0].trim() || full;
    return `<button type="button" data-goto="${node.id}" title="${esc(full)}"
      >${esc(short2)}</button>`;
  };
  const paintJump = () => {
    if(!jump) return;
    const top = [...document.querySelectorAll("#app > .sec")].map(chip);
    const inGroup = [...document.querySelectorAll(".group:not([hidden]) .sec")]
      .map(chip);
    jump.innerHTML = top.filter(Boolean).join("")
      + (inGroup.filter(Boolean).length ? `<span class="sep"></span>` : "")
      + inGroup.filter(Boolean).join("");
  };
  // Land the heading below the sticky bar instead of underneath it.
  const goTo = (node) => {
    if(!node) return;
    const offset = (bar ? bar.offsetHeight : 0) + 10;
    const y = node.getBoundingClientRect().top + window.scrollY - offset;
    window.scrollTo(0, Math.max(0, y));
  };
  const paint = () => {
    document.querySelectorAll(".group").forEach(sec => {
      sec.hidden = current !== "all" && sec.dataset.group !== current;
    });
    nav.querySelectorAll("button").forEach(b => {
      b.dataset.active = b.dataset.tab === current ? "1" : "";
    });
    paintJump();
  };
  if(jump) jump.addEventListener("click", (ev) => {
    const button = ev.target.closest("button[data-goto]");
    if(!button) return;
    goTo(document.getElementById(button.dataset.goto));
  });
  nav.addEventListener("click", (ev) => {
    const button = ev.target.closest("button[data-tab]");
    if(!button) return;
    current = button.dataset.tab;
    try{ localStorage.setItem("board-tab", current); }catch(e){}
    paint();
    // Switching groups from the top of the page used to change something
    // two thousand pixels below the fold and look like it did nothing.
    if(current !== "all") goTo(document.querySelector(
      `.group[data-group="${current}"]`));
  });
  paint();
})();

// Per-day tab strips inside a card.  Scoped to the card that carries the
// strip, so a second one on the page does not paint the first one's panes.
document.querySelectorAll("[data-daytabs]").forEach(card => {
  const nav = card.querySelector(".daytabs");
  if(!nav) return;
  nav.addEventListener("click", (ev) => {
    const button = ev.target.closest("button[data-day]");
    if(!button) return;
    const day = button.dataset.day;
    nav.querySelectorAll("button[data-day]").forEach(b => {
      b.dataset.active = b.dataset.day === day ? "1" : "";
    });
    card.querySelectorAll("[data-daypane]").forEach(pane => {
      pane.hidden = day !== "all" && pane.dataset.daypane !== day;
    });
    card.dataset.dayall = day === "all" ? "1" : "";
  });
});

// Back to the top, from wherever the reader got to.  The floating button
// only exists once there is a scroll to undo; the footer link is there for
// anyone who reached the end.
(function(){
  const button = document.getElementById("totop");
  const link = document.getElementById("footer-top");
  const up = () => window.scrollTo(0, 0);
  if(button){
    const show = () => { button.dataset.show = window.scrollY > 600 ? "1" : ""; };
    window.addEventListener("scroll", show, {passive: true});
    button.addEventListener("click", up);
    show();
  }
  if(link) link.addEventListener("click", (ev) => { ev.preventDefault(); up(); });
})();

document.querySelectorAll("table").forEach(makeSortable);
lcInit();

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
    print(f"气温篮子全链看板: http://127.0.0.1:{port}/  (Ctrl-C 停止)")
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--html-out", type=Path)
    parser.add_argument(
        "--no-section-cache",
        action="store_true",
        help=(
            "recompute every section against this instant instead of"
            " reusing the slow-moving ones from data/dashboard_section_cache"
        ),
    )
    args = parser.parse_args()
    global SECTION_CACHE_ENABLED
    SECTION_CACHE_ENABLED = not args.no_section_cache
    database = args.db.expanduser().resolve(strict=True)
    if args.json:
        print(json.dumps(
            build_payload(database), ensure_ascii=False, sort_keys=True
        ))
        return 0
    if args.html_out:
        # Render first, write once, and never replace a file that a newer
        # build already wrote.  A full render takes minutes, and the hosted
        # sweep renders on its own timer: a sweep that started before an
        # edit finishes after it and puts pre-edit HTML back on the page,
        # which is indistinguishable from the edit never having landed.
        # Measured repeatedly on 2026-08-26 -- three separate sections
        # "disappeared" from the page this way.  The started-at stamp is
        # taken before the payload is built, so the comparison is between
        # code vintages rather than write moments.
        started_at = time.time()
        rendered = render_html(build_payload(database))
        target = args.html_out
        try:
            existing_mtime = target.stat().st_mtime
        except OSError:
            existing_mtime = None
        if existing_mtime is not None and existing_mtime > started_at:
            print(
                f"{target}: skipped, a newer render landed while this one "
                "was building"
            )
            return 0
        target.write_text(rendered, encoding="utf-8")
        print(str(target))
        return 0
    serve(database, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
