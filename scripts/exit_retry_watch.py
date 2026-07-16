#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
import os
from typing import Optional

import scan_weather_market as s


def _resolve_token_id(slug: str, event_date: str, outcome: str, side: str) -> str:
    with s.open_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(run_date) FROM event_runs")
        row = cur.fetchone()
        run_date = str(row[0]) if row and row[0] else ""
        trade_token_map, _, _ = s.build_trade_sync_maps(conn, run_date)
    for meta in trade_token_map.values():
        if (
            meta.get("slug") == slug
            and meta.get("event_date") == event_date
            and meta.get("outcome") == outcome
            and meta.get("side") == side
        ):
            return str(meta.get("token_id") or "")
    # fallback from fills
    with s.open_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT token_id
            FROM edge_trade_fills
            WHERE slug=? AND event_date=? AND outcome=? AND outcome_side=?
            ORDER BY trade_time DESC
            LIMIT 1
            """,
            (slug, event_date, outcome, side),
        )
        row = cur.fetchone()
        return str(row[0]) if row and row[0] else ""


def _position_size(slug: str, event_date: str, outcome: str, side: str) -> float:
    with s.open_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT position_size
            FROM edge_trades
            WHERE slug=? AND event_date=? AND outcome=? AND side=?
            """,
            (slug, event_date, outcome, side),
        )
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Retired manual exit watcher. Defaults to dry-run; real SELL requires "
            "--apply and a target_realization_* reason code."
        )
    )
    parser.add_argument("--slug", required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--outcome", required=True)
    parser.add_argument("--side", required=True, choices=["YES", "NO"])
    parser.add_argument("--token-id", default="")
    parser.add_argument("--min-bid", type=float, default=0.0)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--max-tries", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Allow a real SELL after Bayes target_realization exit policy passes.",
    )
    parser.add_argument(
        "--exit-reason-code",
        action="append",
        default=[],
        help="Required with --apply; must include target_realization_*.",
    )
    args = parser.parse_args()

    reason_codes = [
        str(code or "").strip()
        for code in (args.exit_reason_code or [])
        if str(code or "").strip()
    ]
    policy_block = s._exit_order_policy_block_reason(reason_codes)
    if args.apply and args.dry_run:
        raise SystemExit("--apply and --dry-run are mutually exclusive")
    if args.apply and policy_block:
        raise SystemExit(
            "real exit_retry_watch SELL blocked by Bayes exit policy: "
            f"{policy_block}; provide --exit-reason-code target_realization_*"
        )

    token_id = args.token_id or _resolve_token_id(args.slug, args.event_date, args.outcome, args.side)
    if not token_id:
        raise SystemExit("token_id not found")

    if args.dry_run or not args.apply:
        s.AUTO_TRADE_DRY_RUN = True
        print(
            "dry_run_enabled: exit_retry_watch is retired; "
            "real SELL requires --apply and --exit-reason-code target_realization_*"
        )

    for attempt in range(1, args.max_tries + 1):
        pos = _position_size(args.slug, args.event_date, args.outcome, args.side)
        if pos <= 0:
            print("no_position")
            return
        book = s.fetch_orderbook(token_id)
        best_bid, best_ask, spread, bid_depth, ask_depth, best_bid_size, best_ask_size = s.best_bid_ask_and_depth(book, levels=5)
        print(
            f"attempt {attempt}/{args.max_tries} best_bid={best_bid} best_ask={best_ask} "
            f"bid_depth_top5={bid_depth} pos={pos}"
        )
        if best_bid is not None and best_bid >= args.min_bid:
            ok, payload = s._run_node_order(
                "place-order",
                token_id,
                "SELL",
                price=best_bid,
                size=pos,
                order_type="GTC",
                order_context=s._bayes_weather_order_context(
                    slug=args.slug,
                    event_date=args.event_date,
                    outcome=args.outcome,
                    side=args.side,
                    intent_action="exit",
                    submit_source="retired_exit_retry_watch_apply",
                    candidate={
                        "strategy_source": "target_realization_manual_exit_retry",
                        "target_adjustment_reason_codes": reason_codes,
                    },
                ),
            )
            print("order_ok", ok)
            print("payload", payload)
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
