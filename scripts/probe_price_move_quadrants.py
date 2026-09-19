"""Does the *direction* of the market move, next to the model's own move, carry
information the ledger could use?

For every settled seat in a replay ledger this pairs the seat's window with the
belief and the book the system held H hours earlier and classifies the seat by
(Δp, Δq) sign -- the model's move on the window against the market's move on
the same window over the same interval.  Δp is read off the bridge history
(the probability rows production served at that checkpoint) and Δq off the
first committed book at or after that earlier checkpoint (the book production
compiled it on), so both legs of the comparison are what the system actually
held, not a reconstruction.

It measures; it decides nothing.  Outcome columns are stake-independent
(hit rate, realised/claimed, equal-weighted return per unit stake) so the
verdict does not depend on which arm's stakes happen to sit in the table.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone


def _iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _best_ask(buckets_json: str) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for entry in json.loads(buckets_json):
        levels = entry.get("ask_levels") or []
        out[str(entry.get("bucket"))] = float(levels[0]["price"]) if levels else None
    return out


def _window_sum(table: dict[str, float | None], window: list[str]) -> float | None:
    total = 0.0
    for bucket in window:
        value = table.get(bucket)
        if value is None:
            return None
        total += float(value)
    return total


def _sign(value: float, eps: float) -> str:
    if value > eps:
        return "up"
    if value < -eps:
        return "down"
    return "flat"


def _prior_checkpoint(conn, station_id, event_date, before_utc):
    return conn.execute(
        "SELECT history_id, stage, decision_at_utc FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND decision_at_utc<=?"
        " ORDER BY decision_at_utc DESC LIMIT 1",
        (station_id, event_date, before_utc),
    ).fetchone()


def _book_at_or_after(conn, station_id, event_date, at_utc, not_after_utc):
    row = conn.execute(
        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        "   AND observed_at>=? AND observed_at<=?"
        " ORDER BY observed_at ASC LIMIT 1",
        (station_id, event_date, at_utc, not_after_utc),
    ).fetchone()
    return row


def _book_before(conn, station_id, event_date, before_utc):
    return conn.execute(
        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND observed_at<?"
        " ORDER BY observed_at DESC LIMIT 1",
        (station_id, event_date, before_utc),
    ).fetchone()


def _bridge_probabilities(conn, history_id):
    return dict(
        conn.execute(
            "SELECT bucket, probability FROM basket_probability_bridge_bucket_probability"
            " WHERE history_id=?",
            (history_id,),
        ).fetchall()
    )


def _previous_seat(conn, station_id, event_date, before_utc):
    return conn.execute(
        "SELECT stage, decision_at_utc, probabilities_json, prices_json FROM paper_trade_position"
        " WHERE station_id=? AND event_date=? AND decision_at_utc<?"
        " ORDER BY decision_at_utc DESC LIMIT 1",
        (station_id, event_date, before_utc),
    ).fetchone()


def _a_band(p_now, q_now):
    if not p_now:
        return "n/a"
    a = q_now / p_now
    if a < 0.6:
        return "a<0.60"
    if a < 0.85:
        return "a0.60-0.85"
    return "a>=0.85"


def collect(conn, horizon_hours: float, eps: float, min_gap_hours: float, chain: bool = False):
    rows = []
    seats = conn.execute(
        "SELECT p.position_id, p.station_id, p.event_date, p.stage, p.decision_at_utc,"
        "       p.quote_observed_at_utc, p.window_buckets_json, p.probabilities_json,"
        "       p.prices_json, p.win_probability, p.all_in_cost, p.stake_usdc,"
        "       p.held_before_json, s.hit, s.pnl_usdc, s.stake_usdc"
        " FROM paper_trade_position p JOIN paper_trade_settlement s USING(position_id)"
        " ORDER BY p.decision_at_utc"
    ).fetchall()
    for (
        position_id, station_id, event_date, stage, decision_at, quote_at, window_json,
        probabilities_json, prices_json, win_probability, all_in_cost, stake, held_before,
        hit, pnl, settled_stake,
    ) in seats:
        window = json.loads(window_json)
        p_now = _window_sum(json.loads(probabilities_json), window)
        q_now = _window_sum(json.loads(prices_json), window)
        earlier = _fmt(_iso(decision_at) - timedelta(hours=horizon_hours))
        prior = _prior_checkpoint(conn, station_id, event_date, earlier)
        topup = bool(held_before and held_before not in ("{}", ""))
        record = {
            "position_id": position_id,
            "station_id": station_id,
            "event_date": event_date,
            "stage": stage,
            "topup": topup,
            "decision_at": decision_at,
            "window": window,
            "p_now": p_now,
            "q_now": q_now,
            "claimed": win_probability,
            "hit": int(hit),
            "unit_return": (1.0 / all_in_cost - 1.0) if hit else -1.0,
            "pnl": pnl,
            "settled_stake": settled_stake,
        }
        record["a_band"] = _a_band(p_now, q_now)
        if chain:
            prev_seat = _previous_seat(conn, station_id, event_date, decision_at)
            if prev_seat is None:
                rows.append(record | {"prior": None})
                continue
            prior_stage, prior_at, prev_pj, prev_qj = prev_seat
            p_prev = _window_sum(json.loads(prev_pj), window)
            q_prev = _window_sum(json.loads(prev_qj), window)
            if p_prev is None or q_prev is None or p_now is None or q_now is None:
                rows.append(record | {"prior": "unpriced"})
                continue
            dp, dq = p_now - p_prev, q_now - q_prev
            record.update({
                "prior": prior_stage, "prior_at": prior_at, "p_prev": p_prev, "q_prev": q_prev,
                "dp": dp, "dq": dq, "dp_sign": _sign(dp, eps), "dq_sign": _sign(dq, eps),
                "market_converged": (dq * (p_prev - q_prev)) > 0,
                "model_converged": (dp * (q_prev - p_prev)) > 0,
            })
            rows.append(record)
            continue
        if prior is None:
            # No belief that early: the market alone, over the same horizon.
            before = _book_before(conn, station_id, event_date, earlier)
            if before is not None:
                q_prev = _window_sum(_best_ask(before[1]), window)
                if q_prev is not None and q_now is not None:
                    record.update({"prior": None, "dq": q_now - q_prev, "q_prev": q_prev})
            rows.append(record)
            continue
        history_id, prior_stage, prior_at = prior
        if (_iso(decision_at) - _iso(prior_at)) < timedelta(hours=min_gap_hours):
            rows.append(record | {"prior": "too_close"})
            continue
        p_prev = _window_sum(_bridge_probabilities(conn, history_id), window)
        book = _book_at_or_after(conn, station_id, event_date, prior_at, decision_at)
        q_prev = _window_sum(_best_ask(book[1]), window) if book is not None else None
        if p_prev is None or q_prev is None or p_now is None or q_now is None:
            rows.append(record | {"prior": "unpriced"})
            continue
        dp = p_now - p_prev
        dq = q_now - q_prev
        record.update({
            "prior": prior_stage,
            "prior_at": prior_at,
            "p_prev": p_prev,
            "q_prev": q_prev,
            "dp": dp,
            "dq": dq,
            "dp_sign": _sign(dp, eps),
            "dq_sign": _sign(dq, eps),
            # Market moved toward where the model already stood.
            "market_converged": (dq * (p_prev - q_prev)) > 0,
            # Model moved toward where the market already stood.
            "model_converged": (dp * (q_prev - p_prev)) > 0,
        })
        rows.append(record)
    return rows


def _cell(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0}
    hits = sum(r["hit"] for r in rows)
    claimed = sum(r["claimed"] for r in rows)
    unit = sum(r["unit_return"] for r in rows) / n
    stake = sum(r["settled_stake"] for r in rows)
    pnl = sum(r["pnl"] for r in rows)
    return {
        "n": n,
        "hit_pct": 100.0 * hits / n,
        "claimed_pct": 100.0 * claimed / n,
        "realised_over_claimed": (hits / claimed) if claimed else None,
        "unit_return_pct": 100.0 * unit,
        "stake_roi_pct": (100.0 * pnl / stake) if stake else None,
        "stations": len({r["station_id"] for r in rows}),
    }


def _print_grid(title, rows):
    print(f"\n## {title}  (n={len(rows)})")
    print("| Δp \\ Δq | down | flat | up |")
    print("|---|---|---|---|")
    for dp in ("up", "flat", "down"):
        line = [f"**{dp}**"]
        for dq in ("down", "flat", "up"):
            cell = _cell([r for r in rows if r["dp_sign"] == dp and r["dq_sign"] == dq])
            if cell["n"] == 0:
                line.append("—")
            else:
                line.append(
                    f"n={cell['n']} 命中{cell['hit_pct']:.0f}% 声称{cell['claimed_pct']:.0f}%"
                    f" 单位{cell['unit_return_pct']:+.0f}% 站{cell['stations']}"
                )
        print("| " + " | ".join(line) + " |")


def _print_split(title, rows, key):
    print(f"\n## {title}")
    print("| 组 | n | 命中 | 声称 | 实现/声称 | 单位收益 | 账面ROI | 站数 |")
    print("|---|---|---|---|---|---|---|---|")
    groups = defaultdict(list)
    for r in rows:
        groups[str(r.get(key))].append(r)
    for name in sorted(groups):
        c = _cell(groups[name])
        roc = c["realised_over_claimed"]
        roi = c["stake_roi_pct"]
        print(
            f"| {name} | {c['n']} | {c['hit_pct']:.1f}% | {c['claimed_pct']:.1f}% |"
            f" {roc:.2f} | {c['unit_return_pct']:+.1f}% |"
            f" {roi:+.1f}% | {c['stations']} |" if roc is not None and roi is not None
            else f"| {name} | {c['n']} | — | — | — | — | — | — |"
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--horizons", default="6,12,24")
    ap.add_argument("--eps", type=float, default=0.02)
    ap.add_argument("--min-gap-hours", type=float, default=1.0)
    ap.add_argument("--out")
    ap.add_argument("--chain", action="store_true",
                    help="pair each seat with the chain's previous acted seat instead of a horizon")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    from paper_ledger_reader import attach_paper_ledger  # noqa: PLC0415
    attach_paper_ledger(conn)
    conn.execute("PRAGMA query_only=1")
    dump = {}
    horizons = [0.0] if args.chain else [float(h) for h in args.horizons.split(",")]
    for horizon in horizons:
        rows = collect(conn, horizon, args.eps, args.min_gap_hours, chain=args.chain)
        paired = [r for r in rows if "dp_sign" in r]
        market_only = [r for r in rows if r.get("prior") is None and "dq" in r]
        print(f"\n# 视野 {horizon:g}h  ε={args.eps}  席位 {len(rows)}  可配对 {len(paired)}"
              f"  无先验信念仅市场 {len(market_only)}"
              f"  太近 {sum(1 for r in rows if r.get('prior') == 'too_close')}")
        _print_grid("Δp × Δq（窗口质量 vs 窗口卖一合计）", paired)
        for stage in ("P0", "P1"):
            sub = [r for r in paired if r["stage"] == stage]
            if sub:
                _print_grid(f"{stage}", sub)
        _print_split("市场是否向模型靠拢（Δq 与 p_prev−q_prev 同号）", paired, "market_converged")
        _print_split("模型是否向市场靠拢（Δp 与 q_prev−p_prev 同号）", paired, "model_converged")
        _print_split("首座 vs 加仓", paired, "topup")
        for band in ("a<0.60", "a0.60-0.85", "a>=0.85"):
            sub = [r for r in paired if r["a_band"] == band]
            if sub:
                _print_split(f"成本/声称 {band}：按 Δq 方向", sub, "dq_sign")
        for r in market_only:
            r["dq_sign"] = _sign(r["dq"], args.eps)
        if market_only:
            _print_split("无先验信念的席位：仅市场在此视野内的移动", market_only, "dq_sign")
        dump[f"h{horizon:g}"] = rows
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(dump, fh, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
