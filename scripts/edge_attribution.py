#!/usr/bin/env python3
"""Charter 132 stage D: every live dollar, explained along the chain.

For each leg the live book actually filled, the cash it made is split in a
fixed order into four pieces that add up to it exactly:

    n * (y - c)  =  n * (p - a)        claimed edge at the decision's ask
                 +  n * (a - c)        execution: what the fill cost against that ask
                 +  n * (m - p)        market correction: the book's reference minus our claim
                 +  n * (y - m)        residual: the outcome against the market's reference

with ``n`` the filled shares, ``c`` the average fill price, ``a`` the best ask
in the snapshot the decision priced against, ``p`` the served weather
probability for that bucket, ``m`` the market reference probability (the
normalised book, ``market_reference_v1``) and ``y`` whether the truth landed
there.  The order is fixed rather than decomposed symmetrically, and says so:
"claimed edge" is what the decision believed it was buying, the next term is
the fill, the next is how much of the claim the market's own reading already
contradicted, and what is left is weather.

A second table asks the question the paper book cannot answer, because its
fills are conditioned on the outcome by construction (charter 129): among
legs that reached the exchange, did the ones that filled land less often than
the ones that did not?  Measured against the market reference at the
decision, so a cheaper bucket filling more is not mistaken for selection.

An order whose outcome at the exchange is unknown is listed and never counted
as filled or unfilled.

    python3 scripts/edge_attribution.py --report docs/research/EDGE_ATTRIBUTION_2026-09-13.md

Read-only.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.label_knowability import knowable_labels  # noqa: E402

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
REACHED = ("resolved_filled", "resolved_partial", "resolved_cancelled")
COMPONENTS = ("claimed_edge", "execution", "market_correction", "residual")
BOOTSTRAP = 400
SEED = 20260913


def decompose(*, shares: float, paid: float, ask: float, p: float, m: float, y: float) -> dict[str, float]:
    """The fixed-order split of one filled leg's cash; the parts sum to total."""

    n = float(shares)
    return {
        "claimed_edge": n * (p - ask),
        "execution": n * (ask - paid),
        "market_correction": n * (m - p),
        "residual": n * (y - m),
        "total": n * (y - paid),
        "cost": n * paid,
    }


def _best_ask(book: Sequence[Mapping[str, Any]], bucket: str) -> float | None:
    for row in book:
        if str(row.get("bucket")) != bucket:
            continue
        prices = [
            float(level["price"]) for level in (row.get("ask_levels") or [])
            if float(level.get("shares") or 0) > 0
        ]
        return min(prices) if prices and row.get("ask_available", True) else None
    return None


def load_legs(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, int]]:
    labels = knowable_labels(conn)
    counts = {
        "intents": 0, "reached_exchange": 0, "unknown_outcome": 0,
        "no_label": 0, "no_market_reference": 0, "no_ask": 0,
    }
    unknown = {
        str(row[0]) for row in conn.execute(
            "SELECT DISTINCT order_intent_id FROM basket_target_order_events"
            " WHERE event_type='UNKNOWN_POST'"
        )
    }
    legs: list[dict[str, Any]] = []
    cursor = conn.execute(
        "SELECT i.order_intent_id, i.bucket, i.limit_price, i.requested_size,"
        "       r.state, r.filled_size, r.filled_notional_usdc,"
        "       f.station_id, f.event_date, f.stage, f.decision_at_utc,"
        "       e.probabilities_json, s.buckets_json"
        " FROM basket_target_order_intents i"
        " LEFT JOIN basket_target_order_reconciliations r"
        "   ON r.order_intent_id = i.order_intent_id"
        " JOIN basket_policy_training_fact f ON f.decision_id = i.decision_id"
        " JOIN basket_probability_envelope e ON e.envelope_id = f.probability_envelope_id"
        " JOIN basket_official_ws_snapshot s ON s.snapshot_id = f.ws_snapshot_id"
        " WHERE i.side='YES' AND i.order_action='BUY'"
    )
    for (intent, bucket, limit_price, requested, state, filled, notional,
         station, day, stage, decided, probs_json, book_json) in cursor:
        counts["intents"] += 1
        state = str(state or "")
        if str(intent) in unknown and state not in REACHED:
            counts["unknown_outcome"] += 1
            continue
        if state not in REACHED:
            continue
        counts["reached_exchange"] += 1
        label = labels.get((str(station), str(day)))
        if label is None:
            counts["no_label"] += 1
            continue
        book = json.loads(book_json or "[]")
        reference = market_reference_v1(book)
        if reference["state"] != "complete":
            counts["no_market_reference"] += 1
            continue
        ask = _best_ask(book, str(bucket))
        if ask is None:
            counts["no_ask"] += 1
            continue
        probs = {k: float(v) for k, v in json.loads(probs_json or "{}").items()}
        total_p = sum(probs.values()) or 1.0
        legs.append({
            "intent": str(intent), "station": str(station), "date": str(day),
            "stage": str(stage), "decided_at": str(decided), "bucket": str(bucket),
            "filled": float(filled or 0.0), "notional": float(notional or 0.0),
            "limit_price": float(limit_price), "requested": float(requested),
            "ask": ask, "p": probs.get(str(bucket), 0.0) / total_p,
            "m": float(reference["vector"].get(str(bucket), 0.0)),
            "y": 1.0 if label["bucket"] == str(bucket) else 0.0,
        })
    return legs, counts


def _bootstrap_roi(groups: Mapping[Any, Sequence[tuple[float, float]]]) -> tuple[float | None, float | None]:
    keys = list(groups)
    if len(keys) < 5:
        return None, None
    rng = random.Random(SEED)
    draws = []
    for _ in range(BOOTSTRAP):
        picked = [groups[keys[rng.randrange(len(keys))]] for _ in keys]
        cost = sum(c for g in picked for _t, c in g)
        if cost > 0:
            draws.append(sum(t for g in picked for t, _c in g) / cost)
    draws.sort()
    return draws[int(0.05 * len(draws))], draws[int(0.95 * len(draws)) - 1]


def attribute(legs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    filled = [leg for leg in legs if leg["filled"] > 0]
    by_stage: dict[str, dict[str, float]] = {}
    clusters: dict[str, dict[tuple[str, str], list[tuple[float, float]]]] = {}
    for leg in filled:
        paid = leg["notional"] / leg["filled"]
        parts = decompose(
            shares=leg["filled"], paid=paid, ask=leg["ask"],
            p=leg["p"], m=leg["m"], y=leg["y"],
        )
        for scope in (leg["stage"], "all"):
            cell = by_stage.setdefault(scope, {k: 0.0 for k in (*COMPONENTS, "total", "cost", "legs", "shares")})
            for key in (*COMPONENTS, "total", "cost"):
                cell[key] += parts[key]
            cell["legs"] += 1
            cell["shares"] += leg["filled"]
            clusters.setdefault(scope, {}).setdefault(
                (leg["station"], leg["date"]), []
            ).append((parts["total"], parts["cost"]))
    for scope, cell in by_stage.items():
        lo, hi = _bootstrap_roi(clusters.get(scope, {}))
        cell["roi"] = cell["total"] / cell["cost"] if cell["cost"] > 0 else None
        cell["roi_ci"] = [lo, hi]
        cell["station_days"] = len(clusters.get(scope, {}))
    # Fill selection among legs that reached the exchange.
    posted = list(legs)
    selection: dict[str, Any] = {}
    for tag, group in (
        ("filled", [leg for leg in posted if leg["filled"] > 0]),
        ("unfilled", [leg for leg in posted if leg["filled"] <= 0]),
    ):
        if not group:
            selection[tag] = None
            continue
        selection[tag] = {
            "legs": len(group),
            "hit_rate": sum(leg["y"] for leg in group) / len(group),
            "market_reference": sum(leg["m"] for leg in group) / len(group),
            "claim": sum(leg["p"] for leg in group) / len(group),
            "outcome_minus_market": sum(leg["y"] - leg["m"] for leg in group) / len(group),
        }
    if selection.get("filled") and selection.get("unfilled"):
        selection["adverse_selection_points"] = (
            selection["unfilled"]["outcome_minus_market"]
            - selection["filled"]["outcome_minus_market"]
        )
    return {"by_stage": by_stage, "selection": selection}


def markdown(result: Mapping[str, Any], counts: Mapping[str, int]) -> str:
    def money(v: float | None) -> str:
        return "—" if v is None else f"{v:+.2f}"

    def pct(v: float | None) -> str:
        return "—" if v is None else f"{v:+.1%}"

    lines = [
        "# Charter 132 实盘兑现归因",
        "",
        "每条实盘成交腿的现金按固定顺序拆成四段，四段精确相加等于该腿盈亏："
        "`n·(y−c) = n·(p−a) + n·(a−c) + n·(m−p) + n·(y−m)`。"
        "p 服务天气概率、a 决策快照最优卖价、c 平均成交价、m 同一快照的市场参考概率、y 是否落桶。",
        "",
        "| 阶段 | 腿 | 站日 | 股 | 成本 $ | 声称边际 $ | 执行 $ | 市场修正 $ | 残差 $ | 合计 $ | 回报 | 90% 区间 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for scope in sorted(result["by_stage"], key=lambda s: (s == "all", s)):
        c = result["by_stage"][scope]
        ci = c.get("roi_ci") or [None, None]
        lines.append(
            f"| {scope} | {int(c['legs'])} | {c['station_days']} | {c['shares']:.1f} |"
            f" {c['cost']:.2f} | {money(c['claimed_edge'])} | {money(c['execution'])} |"
            f" {money(c['market_correction'])} | {money(c['residual'])} |"
            f" {money(c['total'])} | {pct(c['roi'])} | [{pct(ci[0])}, {pct(ci[1])}] |"
        )
    sel = result["selection"]
    lines += [
        "",
        "## 成交选择（到达交易所的腿）",
        "",
        "| | 腿 | 落桶率 | 市场参考 | 模型声称 | 落桶 − 市场参考 |",
        "|---|---|---|---|---|---|",
    ]
    for tag in ("filled", "unfilled"):
        s = sel.get(tag)
        if not s:
            lines.append(f"| {tag} | 0 | — | — | — | — |")
            continue
        lines.append(
            f"| {tag} | {s['legs']} | {s['hit_rate']:.3f} | {s['market_reference']:.3f} |"
            f" {s['claim']:.3f} | {s['outcome_minus_market']:+.3f} |"
        )
    if sel.get("adverse_selection_points") is not None:
        lines += [
            "",
            f"逆选择：没成交的腿比成交的腿相对市场参考多落桶 **{sel['adverse_selection_points']:+.3f}**"
            "（正数＝成交的恰是更差的那批）。",
        ]
    lines += ["", f"计数：`{json.dumps(dict(counts), ensure_ascii=False)}`", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    database = args.db.expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=180.0)
    from scripts.ssot_cold_offload import attach_cold_history  # noqa: PLC0415

    attach_cold_history(conn, ("basket_official_ws_snapshot",))  # charter 238: whole book history
    try:
        legs, counts = load_legs(conn)
    finally:
        conn.close()
    result = attribute(legs)
    whole = result["by_stage"].get("all") or {}
    print(
        f"filled legs {int(whole.get('legs', 0))}; cost ${whole.get('cost', 0):.2f};"
        f" total {whole.get('total', 0):+.2f}; claimed {whole.get('claimed_edge', 0):+.2f}"
        f" execution {whole.get('execution', 0):+.2f} market {whole.get('market_correction', 0):+.2f}"
        f" residual {whole.get('residual', 0):+.2f}; counts {counts}"
    )
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"result": result, "counts": counts}, indent=1, default=str))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(markdown(result, counts), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
