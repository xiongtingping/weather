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
from collections import defaultdict
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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


def collect_members(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per family: is it still arriving, how deep is its training history."""

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
        # A family that has not produced a run in 36h has missed at least
        # one full publication cycle of every schedule we capture.
        if run_age is None:
            health = "red"
            note = "从未入库"
        elif run_age > 36:
            health = "red"
            note = f"最新 run 已 {run_age / 24:.1f} 天前"
        elif run_age > 18:
            health = "amber"
            note = f"最新 run {run_age:.0f} 小时前，慢于常态"
        else:
            health = "green"
            note = f"最新 run {run_age:.0f} 小时前"
        rows.append({
            "label": label,
            "system_id": system_id,
            "member_rows": record[0],
            "stations": record[1],
            "training_days": record[2],
            "latest_run_utc": record[3],
            "latest_capture_utc": record[4],
            "run_age_hours": run_age,
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
                " qualifying_cell_count, band_table_sha256"
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
        days = int(row[4])
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
            "fitted_at_utc": row[3],
            "fit_age_hours": _age_hours(row[3]),
            "serving_days": days,
            "min_days": MIN_QUALIFYING_DAYS,
            "station_days": evidence.get("serving_station_day_count"),
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
        settled = [r for r in rows if r.get("settled")]
        hits = sum(int(r["hit"]) for r in settled)
        staked = sum(float(r["stake_usdc"]) for r in settled)
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


def build_payload(db: Path) -> dict[str, Any]:
    conn = _connect(db)
    members = collect_members(conn)
    tables = collect_correction_and_weights(conn)
    stages = collect_band_stages(conn)
    chain = collect_chain(conn)
    gaps = collect_gaps(conn, members, stages, tables)
    p0m = collect_p0m_progress(conn)
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
        "worker": worker,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>校准与链路看板</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--text:#e6e9ef;
--muted:#9aa4b8;--green:#3fb950;--amber:#d29922;--red:#f85149;--accent:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 72px}
h1{font-size:23px;margin:0 0 4px}
h2{font-size:17px;margin:34px 0 6px;display:flex;align-items:center;gap:9px}
.sub{color:var(--muted);font-size:13px;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);
border-radius:11px;padding:16px 18px;margin-bottom:12px}
.grid{display:grid;gap:12px}
.g4{grid-template-columns:repeat(auto-fit,minmax(232px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none}
.green{background:var(--green)}.amber{background:var(--amber)}
.red{background:var(--red)}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;
border:1px solid var(--line);color:var(--muted)}
.pill.green{background:rgba(63,185,80,.14);color:#7ee787;border-color:transparent}
.pill.amber{background:rgba(210,153,34,.16);color:#e3b341;border-color:transparent}
.pill.red{background:rgba(248,81,73,.15);color:#ff7b72;border-color:transparent}
.big{font-size:26px;font-weight:650;letter-spacing:-.4px}
.muted{color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--muted);font-weight:500;font-size:12.5px;
padding:7px 10px;border-bottom:1px solid var(--line);user-select:none}
th:hover{color:var(--text)}
th[data-sorted="1"]{color:var(--accent)}
th[data-sort-dir="asc"]::after{content:" ↑"}
th[data-sort-dir="desc"]::after{content:" ↓"}
td{padding:9px 10px;border-bottom:1px solid rgba(38,43,54,.6);
vertical-align:top}
tr:last-child td{border-bottom:none}
.scroll{max-height:520px;overflow:auto}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.bar{height:6px;border-radius:99px;background:#232833;overflow:hidden;
margin-top:6px}
.bar>i{display:block;height:100%;background:var(--accent)}
.chain{display:flex;flex-direction:column;gap:0}
.link{display:flex;gap:13px;align-items:flex-start;padding:12px 0;
border-bottom:1px solid rgba(38,43,54,.6)}
.link:last-child{border-bottom:none}
.link .body{flex:1;min-width:0}
.link .name{font-weight:600}
.why{background:rgba(88,166,255,.07);border-left:2px solid var(--accent);
padding:9px 13px;border-radius:0 7px 7px 0;color:#c9d4e5;font-size:13.4px;
margin:9px 0 0}
.gap{display:flex;gap:11px;padding:11px 0;border-bottom:1px solid
rgba(38,43,54,.6);align-items:flex-start}
.gap:last-child{border-bottom:none}
.factor{font-weight:650}
.up{color:#7ee787}.down{color:#e3b341}.flat{color:var(--muted)}
footer{margin-top:34px;color:var(--muted);font-size:12.5px;text-align:center}
</style></head><body><div class="wrap" id="app"></div>
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

function members(){
  const rows = D.members.map(m => `<div class="card">
    <div style="display:flex;align-items:center;gap:8px">
      <span class="dot ${m.health}"></span><b>${h(m.label)}</b>
      <span style="flex:1"></span>
      <span class="pill ${m.health}">${m.seats_won} 次入选</span>
    </div>
    <div class="muted" style="margin-top:7px">${h(m.note)}</div>
    <div style="margin-top:9px;font-size:13.5px">
      训练历史 <b>${m.training_days}</b> 天 · ${m.stations} 个站 ·
      ${(m.member_rows/1000).toFixed(0)}k 条成员预报</div>
    ${m.backfillable ? "" :
      `<div class="muted" style="margin-top:6px;color:#e3b341">
        ⚠ 此来源不留历史档案，当天没抓到就永久缺失</div>`}
  </div>`).join("");
  return `<h2>一、四家模型的数据在不在进来</h2>
  <p class="sub">我们同时听四家气象机构的集合预报。"入选次数"是这个模型在
  历史比赛里被选进最终融合的次数——没入选不代表坏，只代表那天它不是最优组合的一员。</p>
  <div class="grid g4">${rows}</div>`;
}

function chain(){
  const rows = D.chain.map(c => `<div class="link">
    <span class="dot ${c.health}" style="margin-top:8px"></span>
    <div class="body">
      <div class="name">${h(c.name)}</div>
      <div class="muted">${h(c.plain)}</div>
    </div>
    <div style="text-align:right;flex:none">
      <div>${h(c.volume)}</div>
      <div class="muted">${h(c.note)}</div>
    </div>
  </div>`).join("");
  return `<h2>二、整条链路每一环是否在动</h2>
  <p class="sub">数据从上到下流：模型下载 → 真值结算 → 算校正 → 算概率修正 →
  出概率 → 盘中更新。任何一环停了，它下面的环节迟早会跟着停。</p>
  <div class="card chain">${rows}</div>`;
}

function tables(){
  const t = D.tables;
  return `<h2>三、每站的偏差订正与融合权重</h2>
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
      ${journal ? `<div class="muted" style="margin-top:14px">最近几次自动拟合</div>
      <table><thead><tr><th>时间</th><th>结果</th><th>样本</th>
      <th>生效档数</th><th>版本</th></tr></thead>
      <tbody>${journal}</tbody></table>` : ""}
    </div>`;
  }).join("");
  return `<h2>四、概率分档校准：三个阶段各自的账</h2>
  <p class="sub">系统会记录"凡是说过 X% 的地方，后来实际发生了多少次"。
  如果长期说 6% 只发生 4%，就把这一档的数字乘一个小于 1 的系数压回去。</p>
  <div class="why"><b>怎么看这张表：</b>「实际兑现」是实测兑现率除以声称概率——
  1.00× 表示说得准，0.69× 表示说大话（只兑现了 69%），1.23× 表示说小话。
  「修正系数」是实际乘上去的那个数，只有证据足够扎实的档才会不等于 1.000，
  而且取的是保守值——宁可少改，不可改过头。</div>
  <div class="grid" style="margin-top:12px">${cards}</div>`;
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
  return `<h2>十一、篮子命中率（按桶数）</h2>
  <p class="sub">结算真值落在篮子里就算命中。只算可成交的（整篮 ≥1 分）。</p>
  <div class="why"><b>这张表回答一件事：</b>多买一个桶买到的命中率，
  值不值它多花的钱。命中率一定随桶数上升——问题是成本也上升，
  最右边的每元回报才是净结果。</div>
  <table><thead><tr><th>篮子宽度</th><th>已结算</th><th>命中</th><th>命中率</th>
    <th>声称</th><th>实现/声称</th><th>均成本</th><th>每元回报</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function makerProbe(){
  const S=D.selection||{};const m=S.maker_probe||{};
  if(!m.available) return "";
  const cells=m.cells||[];
  if(!cells.length) return `<h2>十二、挂单影子探针</h2>
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
    return `<div class="card" style="margin-top:12px">
      <div class="muted">${h(r.stage)} · 挂单实际买到多少</div>
      <div class="big ${(r.rung_completion_rate||0)>=0.8?"up":"down"}">${
        pct(r.rung_completion_rate)}</div>
      <div class="muted">挂出 ${r.leg_count} 条腿的阶梯，成交这个比例的档
        （挂 100 买到 ${r.rung_completion_rate==null?"—":
          Math.round(r.rung_completion_rate*100)}）</div>
      <table style="margin-top:10px"><tbody>
        <tr><td>建仓篮子</td><td class="mono">${r.seated_count}</td></tr>
        <tr><td>缺腿建仓（有腿一档没成交，照样建）</td>
            <td class="mono">${r.partial_basket_count}</td></tr>
        <tr><td>全篮落空（一腿都没成交，放弃）</td>
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
  const reBlock=reCards?`${reCards}
    <div class="why" style="margin-top:12px"><b>三个阶段分开看，不合并：</b>
    P0 在赛前十几小时挂，盘口几乎不动；P1 在盘中挂，是当天最活跃的书；
    P0-M 只挂一次，赶上什么行情算什么。合并成一个成交率是三个不同市场的平均数，
    哪个都不描述。<br>
    <b>逐步建仓，不是全腿成交才建仓：</b>每条腿的每一档都是独立的一张单，
    各自成交。一条腿只成交四档就是四档，一条腿一档没成交就是缺这条腿——
    篮子照样建，中奖概率按<b>实际买到的桶</b>重算，缺的桶不算数。
    只有一条腿都没成交才放弃。<br>
    <b>挂单价格是区间不是单点：</b>每条腿在<b>买价上一跳 ~ 卖价下一跳</b>
    之间均分挂 5 档。上界是硬的——挂到卖价就是吃单，等于把价差付出去。
    价差只有一跳时区间退化成买价一档。</div>`:"";
  return `<h2>十二、挂单影子探针</h2>${reBlock}
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
  if(!D.gaps.length) return `<h2>五、数据缺口</h2>
    <div class="card"><span class="dot green"></span> 当前没有发现缺口。</div>`;
  const sev = {high:["red","要处理"],medium:["amber","留意"],low:["amber","正常等待"]};
  const rows = D.gaps.map(g => {
    const [cls, label] = sev[g.severity] || sev.low;
    return `<div class="gap"><span class="dot ${cls}" style="margin-top:8px"></span>
      <div style="flex:1">
        <div><b>${h(g.what)}</b> <span class="pill ${cls}">${label}</span></div>
        <div class="muted">影响：${h(g.cost)}</div>
      </div>
      <div class="muted" style="flex:none;text-align:right">
        ${g.self_heals ? "会自行恢复" : "需要人工介入"}</div></div>`;
  }).join("");
  return `<h2>五、数据缺口与健康检查</h2>
  <p class="sub">每一条都写清楚"缺了什么、会造成什么、会不会自己好"。
  标"正常等待"的不是故障，是样本还在积累。</p>
  <div class="card">${rows}</div>`;
}

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
      <td>${r.mean_window_size==null?"—":r.mean_window_size.toFixed(1)}</td>
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
  return `<h2>六、每日统计</h2>
  <p class="sub">按事件日汇总，只算可成交的（整篮成本 ≥1 分）——不足一分的多是无人出价那侧的
  挂单，名义上有价、实际买不到，混进来会让当天的盈亏读不出意思。</p>
  <div class="why"><b>列的意思：</b>「位置」是相对市场偏上/一致/偏下的笔数；
  「均桶数」是当天座位平均几桶；「实现/声称」是命中率÷声称胜率，1.00× 表示概率说得准；
  「赔率」是 (1−成本)/成本；「中心偏差」是模型中心减实测，正数表示我们偏热。</p>
  <div class="card"><table><thead><tr>
    <th>事件日</th><th>站数</th><th>仓位</th><th>位置<br><span class="muted">上/平/下</span></th>
    <th>均桶数</th><th>已结算</th><th>命中率</th><th>声称</th><th>实现/声称</th>
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
  return `<h2>七、覆盖 · 价格 · EV</h2>
  <p class="sub">只算可成交的（整篮 ≥1 分，排除虚价）。三组数回答同一个问题的三面：
  座位罩住了多少概率、为此付了多少、这笔交换值不值。</p>
  <div class="card"><div class="muted">相对市场的位置（模型质量中心减市场质量中心，单位是桶）</div>
    <div style="display:flex;height:9px;border-radius:99px;overflow:hidden;margin-top:7px">
      ${seg("above","#bc8cff")}${seg("aligned","#3fb950")}${seg("below","#d29922")}</div>
    <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
      <span class="pill" style="background:rgba(188,140,255,.16);color:#d2a8ff">偏上 ${sd.above||0}</span>
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
  const rows=ss.map(s=>{
    const sd=s.sides||{};const cal=s.calibration_ratio;
    const rr=s.realization_ratio;const ev=s.ev_per_usdc_mean;
    return `<tr>
      <td><span class="mono">${h(s.station_id)}</span>
        <span class="muted">${h(s.station_label)}</span></td>
      <td>${s.position_count}</td>
      <td>${s.settled_count}</td>
      <td>${s.window_size_mean==null?"—":s.window_size_mean.toFixed(1)}</td>
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
    只出现在下一节的逐日明细里：<span class="mono">${silent.map(h).join(" ")}</span></div>`:"";
  return `<h2>八、逐站汇总（跨日）</h2>
  <p class="sub">一站一行，把该站 ${h(S.stats_start_event_date)} 起的全部可成交仓位
  （整篮 ≥1 分，排除虚价）平均到一起。<b>这些数不属于任何一天</b>——它们回答的是
  「这个站长期表现如何」，逐日的账在下一节。</p>
  <div class="why"><b>列的意思：</b>「仓位」是参与平均的笔数，样本少于三笔时右侧的
  命中率与实现/声称基本读不出信号；「位置」是相对市场偏上/一致/偏下的笔数；
  「概率和/卖价和/赔率/EV」同第七节，只是范围缩到这一个站；
  「实现/声称」是命中率÷声称胜率；「兑现比」是实得÷期望。</div>
  <div class="card"><div class="scroll"><table><thead><tr>
    <th>站点</th><th>仓位</th><th>已结算</th><th>均桶数</th>
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
  // rather than the page's guess.  The last three are what "未入座" used to
  // cover indiscriminately, and they are three different problems: an empty
  // shortlist is the market pricing above the model, a refused Kelly is the
  // basket costing more than it can pay, and no depth is a book too thin to
  // fill at any size.
  // The rule set the ledger currently compiles under.  Everything stamped
  // with it is directly comparable; anything else is a leftover from before
  // a rule change and is flagged per basket.
  const CURRENT_RULE=(D.selection||{}).current_selection_rule_label||"";
  const REASON={no_probability:["无概率","这一天没有为该站算出概率"],
    no_market_book:["无盘口","有概率但没抓到可定价的市场"],
    no_book_at_decision:["盘口未落库","每次决策时刻都没有半小时内的盘口快照"],
    incomplete_book:["盘口缺桶","有盘口但缺了模型认得的桶，整站不可定价"],
    selection_failed:["选桶报错","枚举窗口时抛错，该轮跳过"],
    no_priced_window:["无合格窗口","含中心桶且够长的窗口全是负边际——市场比模型贵"],
    leg_below_floor:["单腿报价过低","窗口里有腿的报价低于一分——那是无人出价那侧的挂牌，不是价格。这是我们自己设的闸，不是市场没给价"],
    no_positive_kelly:["赔率不划算","有窗口，但成本高于胜率，Kelly 给出零仓位"],
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
  const cell=(s,extra,refusal)=>{
    if(!s) return `<td class="muted">—${why(refusal)}${extra||""}</td>`;
    const w=s.window.map(short).join("+");
    const res=s.hit==null?"":(s.hit?` <span class="up">中</span>`:` <span class="down">未中</span>`);
    // "Dust offer" is trading jargon and reads as nothing in Chinese; the
    // thing itself is a price that exists on the book but cannot be filled.
    const dust=s.dust_offer?` <span class="pill red">虚价</span>`:"";
    const rung=s.centre_bucket?
      `<div class="muted">横梁 <span class="mono" style="color:#d2a8ff">${short(s.centre_bucket)}</span></div>`:"";
    // Which rule chose this basket.  A day compiled under a retired rule set
    // sits beside one compiled under the current rule and is not comparable
    // to it; without this the page invites exactly that comparison.  Current
    // rule is stated once in the section header, so a basket only carries the
    // tag when it differs from it -- otherwise every cell repeats the same
    // string and the ones that matter stop standing out.
    const tag=(s.selection_rule_label && s.selection_rule_label!==CURRENT_RULE)
      ? `<div><span class="pill amber" title="该篮子按旧规则选出，与当前规则不可直接比较">${h(s.selection_rule_label)}</span></div>`
      : "";
    return `<td><div class="mono">${w}</div>${rung}${tag}
      <div class="muted">${(s.win_probability*100).toFixed(0)}%
      @ ${s.all_in_cost.toFixed(3)} · ${s.odds==null?"—":s.odds.toFixed(1)+"x"}${res}${dust}</div>${extra||""}</td>`;
  };
  const row=(e)=>{
    const any=e.stages.P0||e.stages["P0-M"]||e.stages.P1;
    const side=any?`<span class="pill ${any.side==="below"?"amber":any.side==="aligned"?"green":""}"
      ${any.side==="above"?'style="background:rgba(188,140,255,.16);color:#d2a8ff"':""}>${LAB[any.side]}</span>`:"";
    const note=e.seated_stage_count?"":
      `<span class="muted">${(REASON[e.absent_reason]||["—",""])[0]}</span>`;
    const taf=e.taf_tx_c==null?`<div class="muted">TAF 无温度组</div>`:
      `<div class="muted">TAF 最高 <span class="mono" style="color:#7ee787">${e.taf_tx_c.toFixed(1)}°C</span></div>`;
    // Recomputations and rung moves are different counts; conflating them
    // reads as a rung that thrashed when it never budged.
    const hist=e.p1_history||[];
    const rungs=hist.map(x=>x.centre_bucket).filter(Boolean);
    const path=rungs.filter((x,i)=>i===0||x!==rungs[i-1]);
    const drift=!hist.length?"":
      `<div class="muted">${hist.length} 次重算 ·
       ${path.length<2
         ? `横梁未动 <span class="mono">${path.length?short(path[0]):"—"}</span>`
         : `横梁 <span class="mono">${path.map(short).join(" → ")}</span>`}</div>`;
    const pnl=e.realized_usdc==null?'<span class="muted">—</span>':
      `<span class="${e.realized_usdc>=0?"up":"down"}">${e.realized_usdc>=0?"+":""}${e.realized_usdc.toFixed(2)}</span>`;
    // Only what happened at this station on this day.  The averages that
    // used to sit here -- coverage, ask total, odds, EV, hit rate,
    // calibration -- are computed over the station's whole window, so the
    // same six numbers repeated on every date and read as the day's own
    // account: ZUUU showed an ask total of 0.514 on 2026-08-17, a day whose
    // book never reached the table.  They now live in section eight.
    return `<tr>
      <td><div class="mono">${h(e.station_id)}</div>
        <div class="muted">${h(e.station_label)}</div></td>
      ${cell(e.stages.P0,"",(e.refusals||{}).P0)}
      ${cell(e.stages["P0-M"],taf,(e.refusals||{})["P0-M"])}
      ${cell(e.stages.P1,drift,(e.refusals||{}).P1)}
      <td>${side}${note}</td>
      <td class="mono">${e.settled_tmax_c==null?"—":e.settled_tmax_c.toFixed(1)+"°"}</td>
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
    // What rules this day's baskets were chosen under.  Usually one; more
    // than one means the day straddles a rule change and its totals mix two
    // strategies.
    const dayRules=[...new Set(rows.flatMap(e=>
      ["P0","P0-M","P1"].map(k=>e.stages[k]&&e.stages[k].selection_rule_label)
        .filter(Boolean)))].sort();
    const ruleNote=dayRules.length
      ? ` · 规则 ${dayRules.map(r=>r===CURRENT_RULE
          ? `<span class="pill green">${h(r)}</span>`
          : `<span class="pill amber">${h(r)}</span>`).join(" ")}`
      : "";
    return `<details class="card" ${open?"open":""}>
      <summary><b class="mono">${h(day)}</b>
        <span class="muted">${rows.length} 站 · P0 建仓 ${p0} · 任一阶段有座位 ${seated}${ruleNote}</span></summary>
      <div class="scroll" style="margin-top:10px"><table>
        <thead>${head}</thead><tbody>${rows.map(row).join("")}</tbody></table></div>
    </details>`;
  };
  const legend=Object.entries(REASON).map(([k,v])=>
    `<div class="muted">· <b>${v[0]}</b>：${v[1]}</div>`).join("");
  // Newest day open, everything older folded away beneath it.
  const ruleLine=CURRENT_RULE?`<div class="why"><b>当前选桶规则：</b>
    <span class="pill green">${h(CURRENT_RULE)}</span>
    ——「D-质量」指窗口按模型概率质量排序（含中心桶、向概率大的一侧展开），
    「整篮EV」指整个篮子的概率和必须高于全书成本，
    「腿≥0.01」指窗口里任何一条腿的报价低于一分就整窗作废。
    下面凡是标黄色规则名的篮子，是按已退役的规则选出的，
    不能和绿色的直接比较。</div>`:"";
  return `<h2>九、逐站逐日明细</h2>${ruleLine}
  <p class="sub">每个事件日一组，注册表里的每个站都在，包括没有座位、甚至当天没算出概率的
  ——没坐比坐了更值得看清原因。
  每格上行是桶，下方是横梁与胜率 @ 成本 · 赔率；空格里写的是账本自己给出的拒绝理由，
  不再统一算作「未入座」。每行只讲这一天：跨日均值在上一节。最新一天默认展开，
  更早的日期折叠在下方。</p>
  <div class="why">${legend}</div>
  ${dates.map((day,index)=>block(day,index===0)).join("")}`;
}


function anomalies(){
  const S=D.selection||{};const an=S.anomalies||[];
  if(!an.length) return `<h2>十、异常盘口</h2>
    <div class="card"><span class="dot green"></span> 未发现异常。</div>`;
  const rows=an.map(a=>`<tr>
    <td class="mono">${h(a.station_id)}</td><td class="mono">${h(a.event_date)}</td>
    <td>${h(a.stage)}</td>
    <td class="mono">${a.window.join(" + ")}</td>
    <td class="mono">${a.all_in_cost.toFixed(3)}</td>
    <td>${(a.win_probability*100).toFixed(1)}%</td>
    <td><span class="pill red">${a.kind==="basket_under_one_cent"?"虚价（整篮不足1分）":"声称超市场10倍"}</span></td>
  </tr>`).join("");
  return `<h2>十、异常盘口</h2>
  <p class="sub">这些不是机会，是信号：整篮报价不足一分（虚价）、或声称概率超市场定价十倍，
  几乎总是无人出价那侧的挂单，真下单填不满。</p>
  <div class="card scroll"><table><thead><tr>
    <th>站点</th><th>事件日</th><th>阶段</th><th>窗口</th><th>成本</th><th>声称</th><th>类型</th>
  </tr></thead><tbody>${rows}</tbody></table></div>`;
}


// Click a header to sort by that column.  Numbers, percentages, prices and
// odds all read as numbers; anything else falls back to text, and an em dash
// always sinks so empty cells never crowd the top of either direction.
function makeSortable(table){
  const heads=[...table.querySelectorAll("thead th")];
  heads.forEach((th,index)=>{
    th.style.cursor="pointer";
    th.title="点击排序";
    th.addEventListener("click",()=>{
      const body=table.querySelector("tbody");
      const rows=[...body.querySelectorAll("tr")];
      const asc=th.dataset.sortDir!=="asc";
      heads.forEach(o=>{o.dataset.sortDir="";o.dataset.sorted="";});
      th.dataset.sortDir=asc?"asc":"desc";
      th.dataset.sorted="1";
      const value=(row)=>{
        const cell=row.cells[index];
        if(!cell) return {n:null,s:""};
        const text=cell.textContent.trim();
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

document.getElementById("app").innerHTML =
  head() + members() + chain() + tables() + stages() + gaps() + daily() +
  coverage() + stationSummary() + roster() + anomalies() +
  basketHit() + makerProbe() +
  `<footer>数据生成于 ${h(D.generated_at_utc)} · 只读页面，不写任何数据</footer>`;

document.querySelectorAll("table").forEach(makeSortable);
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
