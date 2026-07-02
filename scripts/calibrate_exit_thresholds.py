#!/usr/bin/env python3
import argparse
import dataclasses
import datetime as dt
import math
import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


def parse_dt(s: str) -> Optional[dt.datetime]:
    s = str(s or "").strip()
    if not s:
        return None
    try:
        # Accept both "...Z" and "+00:00"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def iso(dt_obj: dt.datetime) -> str:
    if dt_obj.tzinfo is None:
        return dt_obj.isoformat(timespec="seconds")
    return dt_obj.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def to_float(x) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except Exception:
        return None
    if not math.isfinite(v):
        return None
    return v


def vwap(rows: Sequence[sqlite3.Row]) -> Tuple[Optional[float], float]:
    notional = 0.0
    size = 0.0
    for r in rows:
        px = to_float(r["price"])
        sz = to_float(r["size"])
        if px is None or sz is None or sz <= 0:
            continue
        notional += px * sz
        size += sz
    if size <= 0:
        return None, 0.0
    return notional / size, size


@dataclasses.dataclass(frozen=True)
class TradeKey:
    slug: str
    event_date: str
    outcome: str
    side: str  # YES/NO token


@dataclasses.dataclass
class TradePath:
    key: TradeKey
    entry_at: dt.datetime
    entry_vwap: float
    entry_size: float
    exit_at: Optional[dt.datetime]
    exit_vwap: Optional[float]
    exit_size: float
    ticks: List[Tuple[dt.datetime, float]]  # (ts, mark_price)


@dataclasses.dataclass
class SimResult:
    exit_at: dt.datetime
    exit_price: float
    exit_reason: str  # trail_stop | stop_loss | none
    peak_before_exit: float
    armed: bool
    max_mark: float
    max_mark_after_exit: Optional[float]
    min_mark: float
    min_mark_before_arm: Optional[float]


def load_closed_trade_paths(
    conn: sqlite3.Connection,
    start_date: Optional[str],
    end_date: Optional[str],
    horizon_hours: float,
    post_exit_hours: float,
    min_ticks: int,
) -> List[TradePath]:
    cur = conn.cursor()
    where = ["trade_time IS NOT NULL", "outcome_side IN ('YES','NO')"]
    params: List[object] = []
    if start_date:
        where.append("trade_time >= ?")
        params.append(start_date)
    if end_date:
        where.append("trade_time < ?")
        params.append(end_date)
    where_sql = " AND ".join(where)

    cur.execute(
        f"""
        SELECT slug, event_date, outcome, outcome_side AS side,
               trade_side, price, size, trade_time
        FROM edge_trade_fills
        WHERE {where_sql}
        ORDER BY slug, event_date, outcome, side, trade_time ASC, id ASC
        """,
        params,
    )
    by_key: Dict[TradeKey, List[sqlite3.Row]] = {}
    for r in cur.fetchall():
        key = TradeKey(
            slug=str(r["slug"] or ""),
            event_date=str(r["event_date"] or ""),
            outcome=str(r["outcome"] or ""),
            side=str(r["side"] or "").upper(),
        )
        if not key.slug or not key.event_date or not key.outcome or key.side not in {"YES", "NO"}:
            continue
        by_key.setdefault(key, []).append(r)

    paths: List[TradePath] = []
    for key, fills in by_key.items():
        buys = [r for r in fills if str(r["trade_side"] or "").upper() == "BUY"]
        sells = [r for r in fills if str(r["trade_side"] or "").upper() == "SELL"]
        if not buys or not sells:
            continue
        entry_at = parse_dt(buys[0]["trade_time"])
        exit_at = parse_dt(sells[-1]["trade_time"])
        if entry_at is None or exit_at is None or exit_at <= entry_at:
            continue
        entry_vwap, entry_size = vwap(buys)
        exit_vwap, exit_size = vwap(sells)
        if entry_vwap is None or entry_vwap <= 0 or exit_vwap is None or exit_vwap <= 0:
            continue
        if entry_size <= 0 or exit_size <= 0:
            continue

        # Require "closed enough" to be meaningful.
        if exit_size < min(entry_size * 0.8, entry_size - 1e-9):
            continue

        end_ts = exit_at + dt.timedelta(hours=post_exit_hours)
        max_end = entry_at + dt.timedelta(hours=horizon_hours)
        if end_ts > max_end:
            end_ts = max_end

        tick_rows = conn.execute(
            """
            SELECT created_at, best_bid, best_ask, mid
            FROM event_market_ticks
            WHERE slug = ? AND event_date = ? AND outcome = ? AND side = ?
              AND created_at >= ? AND created_at <= ?
            ORDER BY created_at ASC, id ASC
            """,
            (
                key.slug,
                key.event_date,
                key.outcome,
                key.side,
                iso(entry_at),
                iso(end_ts),
            ),
        ).fetchall()

        ticks: List[Tuple[dt.datetime, float]] = []
        for tr in tick_rows:
            ts = parse_dt(tr["created_at"])
            if ts is None:
                continue
            mark = to_float(tr["best_bid"])
            if mark is None:
                # Keep alignment with runtime: allow ask fallback when bid missing.
                mark = to_float(tr["best_ask"]) or to_float(tr["mid"])
            if mark is None or mark <= 0:
                continue
            ticks.append((ts, mark))

        if len(ticks) < min_ticks:
            continue

        paths.append(
            TradePath(
                key=key,
                entry_at=entry_at,
                entry_vwap=float(entry_vwap),
                entry_size=float(entry_size),
                exit_at=exit_at,
                exit_vwap=float(exit_vwap),
                exit_size=float(exit_size),
                ticks=ticks,
            )
        )

    return paths


def simulate_trail_stop(
    path: TradePath,
    trail_pct: float,
    stop_loss_pct: float,
    premature_peak_buffer_abs: float,
) -> SimResult:
    trail_pct = float(trail_pct)
    stop_loss_pct = float(stop_loss_pct)
    entry = float(path.entry_vwap)
    peak = entry
    armed = False
    min_mark = float("inf")
    min_mark_before_arm: Optional[float] = None

    exit_idx: Optional[int] = None
    exit_reason = "none"

    for i, (ts, mark) in enumerate(path.ticks):
        if mark < min_mark:
            min_mark = mark
        if mark > peak:
            peak = mark
        if not armed and trail_pct > 0 and peak >= entry * (1.0 + trail_pct):
            armed = True
            min_mark_before_arm = min_mark if math.isfinite(min_mark) else None
        if stop_loss_pct > 0 and mark <= entry * (1.0 - stop_loss_pct):
            exit_idx = i
            exit_reason = "stop_loss"
            break
        if armed and trail_pct > 0 and mark <= peak * (1.0 - trail_pct):
            exit_idx = i
            exit_reason = "trail_stop"
            break

    if exit_idx is None:
        exit_idx = len(path.ticks) - 1

    exit_ts, exit_mark = path.ticks[exit_idx]
    peak_before_exit = max(m for _, m in path.ticks[: exit_idx + 1])
    max_mark = max(m for _, m in path.ticks)

    max_after = None
    if exit_idx + 1 < len(path.ticks):
        max_after = max(m for _, m in path.ticks[exit_idx + 1 :])

    # Premature: exited on trail_stop but later broke above prior peak by a buffer.
    # Use an absolute buffer (cents) to avoid weirdness at low prices.
    if (
        exit_reason == "trail_stop"
        and max_after is not None
        and math.isfinite(max_after)
        and max_after >= peak_before_exit + premature_peak_buffer_abs
    ):
        # Encode the reason in exit_reason for downstream aggregation.
        exit_reason = "trail_stop_premature"

    return SimResult(
        exit_at=exit_ts,
        exit_price=float(exit_mark),
        exit_reason=exit_reason,
        peak_before_exit=float(peak_before_exit),
        armed=bool(armed),
        max_mark=float(max_mark),
        max_mark_after_exit=float(max_after) if max_after is not None else None,
        min_mark=float(min_mark) if math.isfinite(min_mark) else float("nan"),
        min_mark_before_arm=float(min_mark_before_arm) if min_mark_before_arm is not None else None,
    )

def profit_capture(entry: float, exit_price: float, max_mark: float) -> Optional[float]:
    # Capture ratio of achievable profit. 1.0 means exited at max mark, 0.0 means exited at entry.
    if entry <= 0 or exit_price <= 0 or max_mark <= 0:
        return None
    if max_mark <= entry:
        return None
    denom = max_mark - entry
    if denom <= 0:
        return None
    cap = (exit_price - entry) / denom
    # Allow negative (sold below entry) but cap upper bound for readability.
    if cap > 1.5:
        cap = 1.5
    if cap < -2.0:
        cap = -2.0
    return cap


def collect_noise_pullbacks(
    ticks: Sequence[Tuple[dt.datetime, float]],
    new_high_buffer_abs: float,
    lookahead_hours: float,
) -> List[float]:
    """
    Collect "noise pullback" drawdowns during an uptrend:
    - take a peak at time i
    - if within lookahead window we later print a new high >= peak + buffer
    - record the max drawdown from that peak until the first such new high
    """
    if not ticks or lookahead_hours <= 0:
        return []
    out: List[float] = []
    for i in range(len(ticks) - 2):
        peak_ts, peak = ticks[i]
        if peak <= 0 or not math.isfinite(peak):
            continue
        end_ts = peak_ts + dt.timedelta(hours=lookahead_hours)
        min_between = peak
        found = False
        for j in range(i + 1, len(ticks)):
            ts, mark = ticks[j]
            if ts > end_ts:
                break
            if mark < min_between:
                min_between = mark
            if mark >= peak + new_high_buffer_abs:
                found = True
                break
        if not found:
            continue
        if min_between <= 0 or not math.isfinite(min_between):
            continue
        dd = (peak - min_between) / peak
        if dd < 0:
            dd = 0.0
        if dd > 5:
            continue
        out.append(dd)
    return out


def percentile(xs: List[float], p: float) -> Optional[float]:
    xs = [float(x) for x in xs if x is not None and math.isfinite(float(x))]
    if not xs:
        return None
    xs.sort()
    if p <= 0:
        return xs[0]
    if p >= 1:
        return xs[-1]
    k = (len(xs) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return xs[int(k)]
    d0 = xs[f] * (c - k)
    d1 = xs[c] * (k - f)
    return d0 + d1


def main() -> int:
    ap = argparse.ArgumentParser(description="Calibrate exit_trail_pct / exit_stop_loss_pct from local fills+ticks.")
    ap.add_argument("--db", default="data/archive.db")
    ap.add_argument("--start", default=None, help="ISO datetime lower bound (trade_time >= start)")
    ap.add_argument("--end", default=None, help="ISO datetime upper bound (trade_time < end)")
    ap.add_argument("--horizon-hours", type=float, default=24.0)
    ap.add_argument("--post-exit-hours", type=float, default=6.0)
    ap.add_argument("--min-ticks", type=int, default=30)
    ap.add_argument("--premature-peak-buffer-abs", type=float, default=0.02, help="absolute price buffer, e.g. 0.02 = 2c")
    ap.add_argument("--uptrend-mfe-min", type=float, default=0.05, help="restrict some analyses to paths with max_mark >= entry*(1+mfe)")
    ap.add_argument("--noise-lookahead-hours", type=float, default=6.0, help="window for defining 'noise pullback' that still makes a new high")
    ap.add_argument("--noise-newhigh-buffer-abs", type=float, default=0.02, help="abs buffer for new high (cents)")
    ap.add_argument("--trail-grid", default="0.05,0.07,0.08,0.10,0.12,0.15")
    ap.add_argument("--stop-grid", default="0.10,0.15,0.20,0.25,0.30")
    ap.add_argument("--pareto-min-armed", type=int, default=3, help="require at least N armed uptrend paths")
    args = ap.parse_args()

    trail_grid = [float(x.strip()) for x in str(args.trail_grid).split(",") if x.strip()]
    stop_grid = [float(x.strip()) for x in str(args.stop_grid).split(",") if x.strip()]

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    paths = load_closed_trade_paths(
        conn,
        start_date=args.start,
        end_date=args.end,
        horizon_hours=float(args.horizon_hours),
        post_exit_hours=float(args.post_exit_hours),
        min_ticks=int(args.min_ticks),
    )
    if not paths:
        print("No eligible closed trades with sufficient ticks.")
        return 2

    print(f"dataset.closed_trades={len(paths)} horizon_hours={args.horizon_hours:g} post_exit_hours={args.post_exit_hours:g} min_ticks={args.min_ticks}")

    # Baseline "path volatility" stats to justify widening trail_stop.
    dd_before_peak: List[float] = []
    uptrend_dd_before_peak: List[float] = []
    for p in paths:
        entry = p.entry_vwap
        peak = entry
        max_dd = 0.0
        # Compute max drawdown prior to the final peak.
        marks = [m for _, m in p.ticks]
        final_peak = max(marks)
        final_peak_idx = marks.index(final_peak)
        for m in marks[: final_peak_idx + 1]:
            if m > peak:
                peak = m
            if peak > 0:
                dd = (peak - m) / peak
                if dd > max_dd:
                    max_dd = dd
        dd_before_peak.append(max_dd)
        if final_peak >= entry * (1.0 + float(args.uptrend_mfe_min)):
            uptrend_dd_before_peak.append(max_dd)
    for label, p in [("p50", 0.5), ("p75", 0.75), ("p90", 0.9)]:
        v = percentile(dd_before_peak, p)
        if v is not None:
            print(f"vol.dd_before_final_peak.{label}={v*100:.1f}%")
    if uptrend_dd_before_peak:
        print(f"uptrend.paths={len(uptrend_dd_before_peak)} (mfe_min={args.uptrend_mfe_min*100:.1f}%)")
        for label, p in [("p50", 0.5), ("p75", 0.75), ("p90", 0.9)]:
            v = percentile(uptrend_dd_before_peak, p)
            if v is not None:
                print(f"uptrend.vol.dd_before_final_peak.{label}={v*100:.1f}%")

    def is_uptrend_path(tp: TradePath) -> bool:
        entry = tp.entry_vwap
        max_mark = max(m for _, m in tp.ticks)
        return max_mark >= entry * (1.0 + float(args.uptrend_mfe_min))

    uptrend_paths = [p for p in paths if is_uptrend_path(p)]

    # Grid search (all paths): maximize avg_ret, but also report premature trail exits.
    rows_all: List[Tuple[float, float, Dict[str, float]]] = []
    for trail in trail_grid:
        for sl in stop_grid:
            rets: List[float] = []
            pnls: List[float] = []
            premature = 0
            trail_hits = 0
            stop_hits = 0
            armed = 0
            total_pnl = 0.0
            total_stake = 0.0
            for p in paths:
                sim = simulate_trail_stop(
                    p,
                    trail_pct=trail,
                    stop_loss_pct=sl,
                    premature_peak_buffer_abs=float(args.premature_peak_buffer_abs),
                )
                ret = sim.exit_price / p.entry_vwap - 1.0
                rets.append(ret)
                size = min(p.entry_size, p.exit_size)
                stake = p.entry_vwap * size
                pnl = (sim.exit_price - p.entry_vwap) * size
                pnls.append(pnl)
                total_pnl += pnl
                total_stake += stake
                if sim.armed:
                    armed += 1
                if sim.exit_reason.startswith("trail_stop"):
                    trail_hits += 1
                if sim.exit_reason == "stop_loss":
                    stop_hits += 1
                if sim.exit_reason == "trail_stop_premature":
                    premature += 1
            avg_ret = sum(rets) / len(rets) if rets else float("nan")
            med_ret = percentile(rets, 0.5) or float("nan")
            p10 = percentile(rets, 0.1) or float("nan")
            p90 = percentile(rets, 0.9) or float("nan")
            avg_pnl = (sum(pnls) / len(pnls)) if pnls else float("nan")
            med_pnl = percentile(pnls, 0.5) or float("nan")
            roi = (total_pnl / total_stake) if total_stake > 0 else float("nan")
            metrics = {
                "avg_ret": avg_ret,
                "med_ret": med_ret,
                "p10": p10,
                "p90": p90,
                "avg_pnl": avg_pnl,
                "med_pnl": med_pnl,
                "total_pnl": total_pnl,
                "total_stake": total_stake,
                "roi": roi,
                "trail_hits": float(trail_hits),
                "stop_hits": float(stop_hits),
                "armed": float(armed),
                "premature": float(premature),
                "premature_rate": (premature / trail_hits) if trail_hits > 0 else 0.0,
            }
            rows_all.append((trail, sl, metrics))

    # Sort: maximize total_pnl, then minimize premature_rate, then fewer stop losses.
    rows_all.sort(
        key=lambda x: (
            x[2]["total_pnl"],
            -x[2]["premature_rate"],
            -x[2]["stop_hits"],
        ),
        reverse=True,
    )

    print("top_configs (sorted by total_pnl, then premature_rate, then stop_hits):")
    for trail, sl, m in rows_all[:10]:
        print(
            f"  trail={trail:.3f} stop={sl:.3f} total_pnl={m['total_pnl']:+.4f} roi={m['roi']*100:+.2f}% "
            f"avg_pnl={m['avg_pnl']:+.4f} med_pnl={m['med_pnl']:+.4f} avg_ret={m['avg_ret']*100:+.2f}% "
            f"p10={m['p10']*100:+.2f}% p90={m['p90']*100:+.2f}% "
            f"trail_hits={int(m['trail_hits'])} stop_hits={int(m['stop_hits'])} armed={int(m['armed'])} "
            f"premature_rate={m['premature_rate']*100:.1f}%"
        )

    best_trail, best_sl, best_m = rows_all[0]

    # Trail selection focused on uptrend subset: minimize premature exits and maximize profit capture.
    if uptrend_paths:
        noise_dds: List[float] = []
        for p in uptrend_paths:
            noise_dds.extend(
                collect_noise_pullbacks(
                    p.ticks,
                    new_high_buffer_abs=float(args.noise_newhigh_buffer_abs),
                    lookahead_hours=float(args.noise_lookahead_hours),
                )
            )
        if noise_dds:
            print(
                f"noise_pullbacks (uptrend, lookahead={args.noise_lookahead_hours:g}h, newhigh_buffer_abs={args.noise_newhigh_buffer_abs:.3f}):"
            )
            for label, pctl in [("p50", 0.5), ("p75", 0.75), ("p90", 0.9), ("p95", 0.95)]:
                v = percentile(noise_dds, pctl)
                if v is not None:
                    print(f"  dd.{label}={v*100:.1f}% (n={len(noise_dds)})")

        print("trail_only_uptrend (stop_loss fixed at 0.20):")
        trail_rows: List[Tuple[float, Dict[str, float]]] = []
        for trail in trail_grid:
            captures: List[float] = []
            premature = 0
            trail_hits = 0
            armed = 0
            for p in uptrend_paths:
                sim = simulate_trail_stop(
                    p,
                    trail_pct=trail,
                    stop_loss_pct=0.20,
                    premature_peak_buffer_abs=float(args.premature_peak_buffer_abs),
                )
                if sim.armed:
                    armed += 1
                if sim.exit_reason.startswith("trail_stop"):
                    trail_hits += 1
                if sim.exit_reason == "trail_stop_premature":
                    premature += 1
                if not sim.armed:
                    continue
                cap = profit_capture(p.entry_vwap, sim.exit_price, sim.max_mark)
                if cap is not None and math.isfinite(cap):
                    captures.append(cap)
            cap_med = percentile(captures, 0.5) or float("nan")
            cap_p25 = percentile(captures, 0.25) or float("nan")
            prem_rate = (premature / trail_hits) if trail_hits > 0 else 0.0
            trail_rows.append(
                (
                    trail,
                    {
                        "armed": float(armed),
                        "trail_hits": float(trail_hits),
                        "premature_rate": prem_rate,
                        "capture_med": cap_med,
                        "capture_p25": cap_p25,
                    },
                )
            )
            print(
                f"  trail={trail:.3f} armed={armed}/{len(uptrend_paths)} trail_hits={trail_hits} "
                f"premature_rate={prem_rate*100:.1f}% profit_capture_med={cap_med*100:.1f}% profit_capture_p25={cap_p25*100:.1f}%"
            )

        # Pareto frontier: maximize capture_med and minimize premature_rate (and prefer larger armed).
        def dominates(a: Dict[str, float], b: Dict[str, float]) -> bool:
            return (
                a["capture_med"] >= b["capture_med"]
                and a["premature_rate"] <= b["premature_rate"]
                and a["armed"] >= b["armed"]
                and (
                    a["capture_med"] > b["capture_med"]
                    or a["premature_rate"] < b["premature_rate"]
                    or a["armed"] > b["armed"]
                )
            )

        candidates = [
            (trail, m)
            for trail, m in trail_rows
            if int(m.get("armed") or 0) >= int(args.pareto_min_armed)
        ]
        frontier: List[Tuple[float, Dict[str, float]]] = []
        for t, m in candidates:
            dominated = False
            for t2, m2 in candidates:
                if t2 == t:
                    continue
                if dominates(m2, m):
                    dominated = True
                    break
            if not dominated:
                frontier.append((t, m))
        frontier.sort(key=lambda x: (x[1]["premature_rate"], -x[1]["capture_med"], -x[1]["armed"]))
        print(f"pareto_frontier_uptrend (min_armed={args.pareto_min_armed}):")
        for t, m in frontier:
            print(
                f"  trail={t:.3f} armed={int(m['armed'])} premature_rate={m['premature_rate']*100:.1f}% "
                f"profit_capture_med={m['capture_med']*100:.1f}%"
            )

        # Stop-loss sensitivity on all paths with trail fixed to best_trail (from all-path ranking).
        print(f"stop_loss_sensitivity (trail fixed at {best_trail:.3f}):")
        for sl in stop_grid:
            sl_hits = 0
            sl_premature = 0
            for p in paths:
                sim = simulate_trail_stop(
                    p,
                    trail_pct=best_trail,
                    stop_loss_pct=sl,
                    premature_peak_buffer_abs=float(args.premature_peak_buffer_abs),
                )
                if sim.exit_reason == "stop_loss":
                    sl_hits += 1
                    # Premature stop-loss: later recovered back above entry by a buffer.
                    if sim.max_mark_after_exit is not None and sim.max_mark_after_exit >= p.entry_vwap + float(args.premature_peak_buffer_abs):
                        sl_premature += 1
            prem_rate = (sl_premature / sl_hits) if sl_hits > 0 else 0.0
            print(f"  stop={sl:.3f} stop_hits={sl_hits}/{len(paths)} stop_premature_rate={prem_rate*100:.1f}%")

    print(
        f"recommended (objective=total_pnl on all paths): exit_trail_pct={best_trail:.3f} exit_stop_loss_pct={best_sl:.3f} "
        f"(total_pnl={best_m['total_pnl']:+.4f}, roi={best_m['roi']*100:+.2f}%, premature_rate={best_m['premature_rate']*100:.1f}%)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
