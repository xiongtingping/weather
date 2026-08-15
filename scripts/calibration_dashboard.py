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
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


def build_payload(db: Path) -> dict[str, Any]:
    conn = _connect(db)
    members = collect_members(conn)
    tables = collect_correction_and_weights(conn)
    stages = collect_band_stages(conn)
    chain = collect_chain(conn)
    gaps = collect_gaps(conn, members, stages, tables)
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
    return {
        "schema": "calibration_dashboard_v1",
        "generated_at_utc": _now().isoformat().replace("+00:00", "Z"),
        "overall_health": overall,
        "members": members,
        "tables": tables,
        "stages": stages,
        "chain": chain,
        "gaps": gaps,
        "worker": worker,
    }


HTML_TEMPLATE = """<!doctype html>
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
padding:7px 10px;border-bottom:1px solid var(--line)}
td{padding:9px 10px;border-bottom:1px solid rgba(38,43,54,.6);
vertical-align:top}
tr:last-child td{border-bottom:none}
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

document.getElementById("app").innerHTML =
  head() + members() + chain() + tables() + stages() + gaps() +
  `<footer>数据生成于 ${h(D.generated_at_utc)} · 只读页面，不写任何数据</footer>`;
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
