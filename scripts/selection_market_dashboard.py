#!/usr/bin/env python3
"""Every station's daily seat, read against what the market was charging.

Two questions, one page.  Before settlement: where did we sit relative to
the book -- above it, below it, or on it?  After settlement: did the basket
contain the truth, what did the paper book make, at what odds, and which
quotes were not real prices.

The lean is measured as the gap between the model's centre of mass and the
market's, both computed on the same buckets from the same snapshot, in
bucket widths so Fahrenheit and Celsius books read alike:

    lean = model_centre_ordinal - market_centre_ordinal

The market's centre is its own implied distribution -- each bucket's all-in
ask normalised to sum to one.  That is what "the market thinks" means when
the market speaks in prices.

Read-only, and it reads the paper ledger rather than recomputing anything,
so what it shows is exactly what was scored.

    python3 scripts/selection_market_dashboard.py            # serve :8788
    python3 scripts/selection_market_dashboard.py --json
    python3 scripts/selection_market_dashboard.py --html-out page.html
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
INITIAL_CAPITAL_USDC = 1000.0
# Below a one-cent basket cost the fill is almost always a minimum-tick
# offer with nothing bid against it; the paper ledger records these and
# reports them apart rather than letting them set the headline.
DUST_COST_FLOOR = 0.01
# A lean smaller than this is the same view, not a disagreement.
LEAN_TOLERANCE_BUCKETS = 0.35
# Everything before this event date belongs to a different strategy: the
# ladder floor, the dispersion cap and the probability-following rebalance
# all landed on 2026-08-15, so earlier seats were chosen by rules that no
# longer run and averaging them in would describe a system that no longer
# exists.
STATS_START_EVENT_DATE = "2026-08-15"


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _centre_of_mass(
    weights: dict[str, float], ordinals: dict[str, int]
) -> float | None:
    total = sum(max(0.0, v) for v in weights.values())
    if total <= 0:
        return None
    return sum(
        ordinals[b] * max(0.0, v)
        for b, v in weights.items()
        if b in ordinals
    ) / total


def _rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    query = conn.execute(
        "SELECT p.station_id, p.event_date, p.stage, p.decision_at_utc,"
        " p.window_buckets_json, p.ladder_center_bucket, p.win_probability,"
        " p.all_in_cost, p.edge, p.stake_usdc, p.probabilities_json,"
        " p.prices_json, p.window_size,"
        " s.hit, s.settled_tmax_c, s.settled_bucket, s.pnl_usdc,"
        " s.bucket_distance, s.center_error_c"
        " FROM paper_trade_position p"
        " LEFT JOIN paper_trade_settlement s"
        " ON s.position_id = p.position_id"
        " ORDER BY p.event_date DESC, p.station_id, p.stage"
    )
    for r in query:
        if str(r[1]) < STATS_START_EVENT_DATE:
            continue
        probabilities = json.loads(r[10])
        prices = json.loads(r[11])
        ordinals = {
            bucket: index
            for index, bucket in enumerate(probabilities.keys())
        }
        model_centre = _centre_of_mass(probabilities, ordinals)
        market_centre = _centre_of_mass(prices, ordinals)
        lean = (
            None
            if model_centre is None or market_centre is None
            else model_centre - market_centre
        )
        if lean is None:
            side = "unknown"
        elif lean > LEAN_TOLERANCE_BUCKETS:
            side = "above"
        elif lean < -LEAN_TOLERANCE_BUCKETS:
            side = "below"
        else:
            side = "aligned"
        cost = float(r[7])
        win = float(r[6])
        # What the seat covers, what it costs, and what that pair is worth.
        #
        # Coverage is the model's mass inside the window against the market's
        # own implied mass over the same buckets: the market quotes a book
        # whose asks sum to more than one (the vig), so its implied coverage
        # is normalised by that sum rather than taken raw -- otherwise every
        # seat would look cheap by the width of the spread.
        book_ask_total = sum(max(0.0, v) for v in prices.values())
        seat_ask_total = sum(
            max(0.0, prices.get(b, 0.0)) for b in json.loads(r[4])
        )
        market_coverage = (
            seat_ask_total / book_ask_total if book_ask_total > 0 else None
        )
        # EV per USDC staked.  A basket costing C pays 1 when the truth lands
        # inside it, so a unit of stake buys 1/C of payout and returns
        # p/C - 1 in expectation.  This is the same quantity the edge (p - C)
        # describes, restated per unit of capital, which is what makes two
        # seats at different price levels comparable.
        ev_per_usdc = (win / cost - 1.0) if cost > 0 else None
        stake = float(r[9])
        out.append({
            "market_coverage": market_coverage,
            "book_ask_total": book_ask_total,
            "seat_ask_total": seat_ask_total,
            "coverage_gap": (
                None if market_coverage is None else win - market_coverage
            ),
            "ev_per_usdc": ev_per_usdc,
            "ev_usdc": (
                None if ev_per_usdc is None else stake * ev_per_usdc
            ),
            "station_id": r[0],
            "event_date": r[1],
            "stage": r[2],
            "decision_at_utc": r[3],
            "window": json.loads(r[4]),
            "window_size": int(r[12]),
            "centre_bucket": r[5],
            "win_probability": float(r[6]),
            "all_in_cost": cost,
            "edge": float(r[8]),
            "odds": (1.0 - cost) / cost if cost > 0 else None,
            "stake_usdc": float(r[9]),
            "lean_buckets": lean,
            "side": side,
            "dust_offer": cost < DUST_COST_FLOOR,
            "settled": r[13] is not None,
            "hit": None if r[13] is None else int(r[13]),
            "settled_tmax_c": r[14],
            "settled_bucket": r[15],
            "pnl_usdc": r[16],
            "bucket_distance": r[17],
            "center_error_c": r[18],
        })
    return out


def build_payload(db: Path) -> dict[str, Any]:
    conn = _connect(db)
    rows = _rows(conn)
    by_side: dict[str, int] = defaultdict(int)
    for row in rows:
        by_side[row["side"]] += 1

    def _slice(subset: list[dict[str, Any]]) -> dict[str, Any]:
        settled = [r for r in subset if r["settled"]]
        if not settled:
            return {"settled_count": 0, "position_count": len(subset)}
        hits = sum(int(r["hit"]) for r in settled)
        staked = sum(r["stake_usdc"] for r in settled)
        pnl = sum(float(r["pnl_usdc"] or 0.0) for r in settled)
        claimed = sum(r["win_probability"] for r in settled) / len(settled)
        odds = [r["odds"] for r in settled if r["odds"] is not None]
        return {
            "position_count": len(subset),
            "settled_count": len(settled),
            "hit_count": hits,
            "hit_rate": hits / len(settled),
            "claimed_win_probability_mean": claimed,
            "calibration_ratio": (
                (hits / len(settled)) / claimed if claimed > 0 else None
            ),
            "odds_mean": sum(odds) / len(odds) if odds else None,
            "staked_usdc": staked,
            "pnl_usdc": pnl,
            "return_on_stake": pnl / staked if staked > 0 else None,
        }

    stages: dict[str, Any] = {}
    for stage in ("P0", "P0-M", "P1"):
        subset = [r for r in rows if r["stage"] == stage]
        credible = [r for r in subset if not r["dust_offer"]]
        dust = [r for r in subset if r["dust_offer"]]
        stages[stage] = {
            "all": _slice(subset),
            "credible": _slice(credible),
            "dust_offer": _slice(dust),
            "sides": {
                side: sum(1 for r in subset if r["side"] == side)
                for side in ("above", "below", "aligned", "unknown")
            },
        }
    # Quotes that are not really prices, listed so they can be chased rather
    # than quietly carried in the headline.
    anomalies = [
        {
            "station_id": r["station_id"],
            "event_date": r["event_date"],
            "stage": r["stage"],
            "window": r["window"],
            "all_in_cost": r["all_in_cost"],
            "win_probability": r["win_probability"],
            "kind": (
                "basket_under_one_cent"
                if r["dust_offer"]
                else "claim_over_market_by_10x"
            ),
        }
        for r in rows
        if r["dust_offer"]
        or (r["all_in_cost"] > 0 and r["win_probability"] / r["all_in_cost"] > 10.0)
    ]
    # How the seat moved as evidence arrived: the same station-day read at
    # each stage it reached, so a widening, a shift or a hold is visible as
    # one line rather than three rows to cross-reference.
    scopes: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["station_id"], row["event_date"])
        scopes.setdefault(key, {})[row["stage"]] = row
    evolution = []
    for (station, day), by_stage in sorted(
        scopes.items(), key=lambda kv: (kv[0][1], kv[0][0]), reverse=True
    ):
        steps = []
        for stage in ("P0", "P0-M", "P1"):
            row = by_stage.get(stage)
            if row is None:
                steps.append({"stage": stage, "present": False})
                continue
            steps.append({
                "stage": stage,
                "present": True,
                "window": row["window"],
                "window_size": row["window_size"],
                "centre_bucket": row["centre_bucket"],
                "win_probability": row["win_probability"],
                "all_in_cost": row["all_in_cost"],
                "side": row["side"],
                "lean_buckets": row["lean_buckets"],
                "hit": row["hit"],
                "pnl_usdc": row["pnl_usdc"],
                "dust_offer": row["dust_offer"],
            })
        seen = [s for s in steps if s.get("present")]
        first = seen[0]["window"] if seen else []
        last = seen[-1]["window"] if seen else []
        evolution.append({
            "station_id": station,
            "event_date": day,
            "steps": steps,
            "stage_count": len(seen),
            # Did the ladder move at all, and which way?
            "window_changed": first != last,
            "width_delta": (
                (len(last) - len(first)) if seen else 0
            ),
            "settled_tmax_c": next(
                (s.get("settled_tmax_c") for s in [
                    by_stage.get(x) or {} for x in ("P1", "P0-M", "P0")
                ] if s.get("settled_tmax_c") is not None),
                None,
            ),
        })
    # Every registered station, not only the ones that got a seat.  A station
    # missing from the ledger is the more interesting row: it means the
    # objective looked and did not sit, or never got a priced book to look
    # at, and that has to be visible rather than absent.
    latest_day = max((r["event_date"] for r in rows), default=None)
    roster = []
    registered = [
        str(row[0])
        for row in conn.execute(
            "SELECT station_id FROM calibration_station_registry"
            " ORDER BY station_id"
        )
    ]
    seated = {
        (r["station_id"], r["stage"]): r
        for r in rows
        if latest_day and r["event_date"] == latest_day
    }
    for station in registered:
        entry: dict[str, Any] = {"station_id": station, "stages": {}}
        for stage in ("P0", "P0-M", "P1"):
            row = seated.get((station, stage))
            entry["stages"][stage] = (
                None
                if row is None
                else {
                    "window": row["window"],
                    "window_size": row["window_size"],
                    "centre_bucket": row["centre_bucket"],
                    "win_probability": row["win_probability"],
                    "all_in_cost": row["all_in_cost"],
                    "odds": row["odds"],
                    "side": row["side"],
                    "lean_buckets": row["lean_buckets"],
                    "stake_usdc": row["stake_usdc"],
                    "hit": row["hit"],
                    "pnl_usdc": row["pnl_usdc"],
                    "dust_offer": row["dust_offer"],
                }
            )
        entry["seated_stage_count"] = sum(
            1 for v in entry["stages"].values() if v is not None
        )
        # Why a station has nothing: the ledger records a probability
        # snapshot for it or it does not, and a priced book at that moment
        # or not.  Naming which is missing is the difference between "the
        # objective declined" and "the objective never got to look".
        if entry["seated_stage_count"] == 0 and latest_day:
            has_probability = conn.execute(
                "SELECT 1 FROM basket_probability_bridge_history"
                " WHERE station_id=? AND event_date=? LIMIT 1",
                (station, latest_day),
            ).fetchone() is not None
            has_book = conn.execute(
                "SELECT 1 FROM basket_official_ws_snapshot"
                " WHERE station_id=? AND event_date=? LIMIT 1",
                (station, latest_day),
            ).fetchone() is not None
            entry["absent_reason"] = (
                "no_probability" if not has_probability
                else ("no_market_book" if not has_book
                      else "no_seat_taken")
            )
        roster.append(entry)
    # Aggregates over credible fills only; a sub-one-cent basket has an EV
    # per USDC in the hundreds and would set every average by itself.
    credible_rows = [r for r in rows if not r["dust_offer"]]

    def _mean(values: list[float]) -> float | None:
        clean = [v for v in values if v is not None]
        return sum(clean) / len(clean) if clean else None

    settled_credible = [r for r in credible_rows if r["settled"]]
    expected_usdc = sum(
        float(r["ev_usdc"] or 0.0) for r in settled_credible
    )
    realized_usdc = sum(
        float(r["pnl_usdc"] or 0.0) for r in settled_credible
    )
    coverage_stats = {
        "position_count": len(credible_rows),
        "model_coverage_mean": _mean(
            [r["win_probability"] for r in credible_rows]
        ),
        "market_coverage_mean": _mean(
            [r["market_coverage"] for r in credible_rows]
        ),
        "coverage_gap_mean": _mean(
            [r["coverage_gap"] for r in credible_rows]
        ),
        "window_size_mean": _mean(
            [float(r["window_size"]) for r in credible_rows]
        ),
        "all_in_cost_mean": _mean(
            [r["all_in_cost"] for r in credible_rows]
        ),
        "book_ask_total_mean": _mean(
            [r["book_ask_total"] for r in credible_rows]
        ),
        "ev_per_usdc_mean": _mean([r["ev_per_usdc"] for r in credible_rows]),
        "staked_usdc": sum(r["stake_usdc"] for r in settled_credible),
        "expected_usdc": expected_usdc,
        "realized_usdc": realized_usdc,
        # The gap between what the model said the book was worth and what it
        # returned.  A persistent shortfall is the calibration ratio showing
        # up in money rather than in hit rate.
        "realization_ratio": (
            realized_usdc / expected_usdc
            if abs(expected_usdc) > 1e-9 else None
        ),
        "settled_count": len(settled_credible),
    }
    days = sorted({r["event_date"] for r in rows}, reverse=True)
    return {
        "schema": "selection_market_dashboard_v1",
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        "initial_capital_usdc": INITIAL_CAPITAL_USDC,
        "position_count": len(rows),
        "day_count": len(days),
        "latest_day": days[0] if days else None,
        "sides": dict(by_side),
        "stages": stages,
        "anomalies": anomalies[:40],
        "stage_evolution": evolution[:120],
        "coverage_stats": coverage_stats,
        "station_roster": roster,
        "roster_event_date": latest_day,
        "stats_start_event_date": STATS_START_EVENT_DATE,
        "rows": rows[:400],
    }


HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>选桶 vs 市场</title><style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--text:#e6e9ef;--muted:#9aa4b8;
--green:#3fb950;--amber:#d29922;--red:#f85149;--blue:#58a6ff;--purple:#bc8cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:26px 18px 70px}
h1{font-size:23px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 6px}
.sub{color:var(--muted);font-size:13px;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;
padding:15px 17px;margin-bottom:12px}
.grid{display:grid;gap:12px}
.g3{grid-template-columns:repeat(auto-fit,minmax(290px,1fr))}
.g4{grid-template-columns:repeat(auto-fit,minmax(215px,1fr))}
.big{font-size:27px;font-weight:650;letter-spacing:-.5px}
.muted{color:var(--muted);font-size:13px}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px}
.above{background:rgba(188,140,255,.16);color:#d2a8ff}
.below{background:rgba(210,153,34,.16);color:#e3b341}
.aligned{background:rgba(63,185,80,.14);color:#7ee787}
.dust{background:rgba(248,81,73,.15);color:#ff7b72}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;color:var(--muted);font-weight:500;font-size:12.5px;
padding:7px 9px;border-bottom:1px solid var(--line);position:sticky;top:0;
background:var(--card)}
td{padding:7px 9px;border-bottom:1px solid rgba(38,43,54,.55)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.pos{color:#7ee787}.neg{color:#ff7b72}
.why{background:rgba(88,166,255,.07);border-left:2px solid var(--blue);
padding:9px 13px;border-radius:0 7px 7px 0;color:#c9d4e5;font-size:13.4px;margin:9px 0}
.scroll{max-height:560px;overflow:auto}
.bar{display:flex;height:9px;border-radius:99px;overflow:hidden;margin-top:9px}
.bar>i{display:block}
footer{margin-top:32px;color:var(--muted);font-size:12.5px;text-align:center}
</style></head><body><div class="wrap" id="app"></div><script>
const D=__PAYLOAD__;
const h=s=>String(s==null?"":s);
const f=(v,d=2)=>v==null?"—":Number(v).toFixed(d);
const pct=v=>v==null?"—":(v*100).toFixed(1)+"%";
const SIDE={above:["偏上","我们比市场看得更热"],below:["偏下","我们比市场看得更冷"],
 aligned:["一致","与市场盘口基本同view"],unknown:["未知","缺定价"]};

function head(){
  const s=D.sides||{};const tot=D.position_count||1;
  const seg=(k,c)=>`<i style="width:${100*(s[k]||0)/tot}%;background:${c}"></i>`;
  return `<h1>选桶 vs 市场盘口</h1>
  <p class="sub">每一手座位相对市场的位置，以及结算之后它挣到了什么。
  “偏上/偏下”比的是模型的质量中心与市场（按全含费卖价归一）的质量中心，单位是桶。</p>
  <div class="card"><div style="display:flex;gap:20px;flex-wrap:wrap;align-items:baseline">
    <div><div class="muted">记录仓位</div><div class="big">${D.position_count}</div></div>
    <div><div class="muted">覆盖日数</div><div class="big">${D.day_count}</div></div>
    <div><div class="muted">最新事件日</div><div class="big">${h(D.latest_day)}</div></div>
    <div style="flex:1;min-width:260px">
      <div class="muted">相对市场的位置</div>
      <div class="bar">${seg("above","#bc8cff")}${seg("aligned","#3fb950")}${seg("below","#d29922")}${seg("unknown","#39414f")}</div>
      <div class="muted" style="margin-top:6px">
        <span class="pill above">偏上 ${s.above||0}</span>
        <span class="pill aligned">一致 ${s.aligned||0}</span>
        <span class="pill below">偏下 ${s.below||0}</span></div>
    </div></div></div>`;
}

function stages(){
  const cards=Object.entries(D.stages).map(([st,v])=>{
    const c=v.credible||{},d=v.dust_offer||{},sd=v.sides||{};
    if(!c.settled_count&&!d.settled_count&&!(v.all||{}).position_count)
      return `<div class="card"><b>${st}</b><div class="muted">无记录</div></div>`;
    const row=(label,x,cls)=>x.settled_count?
      `<tr><td>${label}</td><td>${x.settled_count}</td>
       <td>${pct(x.hit_rate)}</td><td>${pct(x.claimed_win_probability_mean)}</td>
       <td>${f(x.calibration_ratio)}×</td><td>${f(x.odds_mean)}x</td>
       <td class="${x.pnl_usdc>=0?'pos':'neg'}">${x.pnl_usdc>=0?"+":""}${f(x.pnl_usdc)}U</td></tr>`
      :`<tr><td>${label}</td><td colspan="6" class="muted">无已结算</td></tr>`;
    return `<div class="card">
      <div style="display:flex;align-items:baseline;gap:10px">
        <b style="font-size:16px">${st}</b>
        <span class="muted">${(v.all||{}).position_count||0} 仓位 ·
          ${(v.all||{}).settled_count||0} 已结算</span>
        <span style="flex:1"></span>
        <span class="pill above">偏上 ${sd.above||0}</span>
        <span class="pill aligned">一致 ${sd.aligned||0}</span>
        <span class="pill below">偏下 ${sd.below||0}</span>
      </div>
      <table style="margin-top:10px"><thead><tr>
        <th>成交类别</th><th>已结算</th><th>命中率</th><th>声称胜率</th>
        <th>实现/声称</th><th>平均赔率</th><th>纸面盈亏</th>
      </tr></thead><tbody>
        ${row("可信成交",c)}${row("灰尘报价",d)}
      </tbody></table></div>`;
  }).join("");
  return `<h2>各阶段战绩</h2>
  <p class="sub">“灰尘报价”指整篮成本不足 1 分——多半是最小报价单位上的单边挂单，
  不是真能成交的价格，所以与可信成交分开统计，不让它决定表头数字。</p>
  <div class="why"><b>怎么读：</b>「实现/声称」是命中率除以声称胜率——
  1.00× 表示概率说得准，低于 1 表示系统性高估。「平均赔率」是 (1−成本)/成本，
  4.0x 意味着押 1 块赢 4 块。两者要一起看：高赔率低命中是彩票，
  高命中低赔率是收割，都合法，但校准比必须接近 1。</div>
  <div class="grid g3">${cards}</div>`;
}

function anomalies(){
  if(!D.anomalies.length) return `<h2>异常盘口</h2>
    <div class="card muted">未发现异常。</div>`;
  const rows=D.anomalies.map(a=>`<tr>
    <td class="mono">${h(a.station_id)}</td><td class="mono">${h(a.event_date)}</td>
    <td>${h(a.stage)}</td><td class="mono">${a.window.join(" + ")}</td>
    <td class="mono">${f(a.all_in_cost,3)}</td><td>${pct(a.win_probability)}</td>
    <td><span class="pill dust">${a.kind==="basket_under_one_cent"?"整篮不足1分":"声称超市场10倍"}</span></td>
  </tr>`).join("");
  return `<h2>异常盘口</h2>
  <p class="sub">这些不是机会，是信号：整篮报价不足一分、或我们的声称概率超过市场定价十倍，
  几乎总是无人出价那一侧的挂单，真下单填不满。</p>
  <div class="card scroll"><table><thead><tr>
    <th>站点</th><th>事件日</th><th>阶段</th><th>窗口</th><th>成本</th><th>声称</th><th>类型</th>
  </tr></thead><tbody>${rows}</tbody></table></div>`;
}

function detail(){
  const rows=D.rows.map(r=>{
    const [lab]=SIDE[r.side]||SIDE.unknown;
    return `<tr>
      <td class="mono">${h(r.event_date)}</td><td class="mono">${h(r.station_id)}</td>
      <td>${h(r.stage)}</td>
      <td class="mono">${r.window.join("+")}</td>
      <td class="mono">${h(r.centre_bucket)}</td>
      <td><span class="pill ${r.side}">${lab}</span>
        <span class="muted mono">${r.lean_buckets==null?"":(r.lean_buckets>0?"+":"")+f(r.lean_buckets)}</span></td>
      <td>${pct(r.win_probability)}</td>
      <td class="mono">${f(r.all_in_cost,3)}</td>
      <td class="mono">${f(r.odds)}x</td>
      <td class="mono">${f(r.stake_usdc)}</td>
      <td>${!r.settled?'<span class="muted">未结算</span>':
        (r.hit?'<span class="pos">命中</span>':'<span class="neg">未中</span>')}</td>
      <td class="mono">${r.settled_tmax_c==null?"—":f(r.settled_tmax_c,1)+"°C"}</td>
      <td class="mono ${(r.pnl_usdc||0)>=0?'pos':'neg'}">${r.pnl_usdc==null?"—":((r.pnl_usdc>=0?"+":"")+f(r.pnl_usdc))}</td>
    </tr>`;
  }).join("");
  return `<h2>逐笔明细</h2>
  <p class="sub">“位置”列的数字是模型中心减市场中心，单位是桶：正数=我们更热，负数=更冷。</p>
  <div class="card scroll"><table><thead><tr>
    <th>事件日</th><th>站点</th><th>阶段</th><th>窗口</th><th>横梁</th><th>位置</th>
    <th>胜率</th><th>成本</th><th>赔率</th><th>投入</th><th>结果</th><th>实测</th><th>盈亏</th>
  </tr></thead><tbody>${rows}</tbody></table></div>`;
}

document.getElementById("app").innerHTML =
  head()+stages()+anomalies()+detail()+
  `<footer>生成于 ${h(D.generated_at_utc)} · 只读页面 · 数据来自纸面账本</footer>`;
</script></body></html>"""


def render_html(payload: dict[str, Any]) -> str:
    return HTML.replace(
        "__PAYLOAD__", json.dumps(payload, ensure_ascii=False)
    )


def serve(db: Path, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api"):
                body = json.dumps(
                    build_payload(db), ensure_ascii=False
                ).encode()
                kind = "application/json; charset=utf-8"
            else:
                body = render_html(build_payload(db)).encode()
                kind = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a: Any) -> None:
            return

    print(f"选桶看板: http://127.0.0.1:{port}/  (Ctrl-C 停止)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=8788)
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
