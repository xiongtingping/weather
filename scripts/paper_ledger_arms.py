#!/usr/bin/env python3
"""Arms on the paper ledger: one production knob per arm, one walk per arm.

The old ledger's arms were its own hand-written rules behind
``PAPER_LEDGER_*`` switches, scored on a replay extract; they retired with it
on 2026-09-25.  The paper ledger (``scripts/paper_ledger_aligned.py``) runs
production's own compile entries, so an arm is now a production knob -- a
``PM_*`` environment variable, read where production reads it -- and a walk
under it is what production would have decided with that knob set, on the
same recorded books, the same chain history and the same paper capital.

    python3 scripts/paper_ledger_arms.py \\
        --from-event-date 2026-09-21 --to-event-date 2026-09-24 \\
        --arm base \\
        --arm dwell_1h:PM_RESTING_REPRICE_DWELL_SECONDS=3600

    python3 scripts/paper_ledger_arms.py --compare RUN_ID RUN_ID ...

Arms run one after another in the foreground: each walk holds read
snapshots on the SSOT for its whole length, and two at once would double
that.  Every arm's run is stored in ``data/paper_ledger_aligned.db``
labelled ``arm:<name>``, with the ``PM_*`` environment it ran under and the
commit it ran on; the table refuses to call two arms comparable when those
commits differ, because the tree is shared and a rule can land between arms.

A knob only moves what production reads from the environment.  An arm on an
upstream constant -- one baked into the envelopes production already
committed -- is not an arm here: the walk reads those envelopes as recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LEDGER_DB = ROOT / "data" / "paper_ledger_aligned.db"
LEDGER_SCRIPT = ROOT / "scripts" / "paper_ledger_aligned.py"
ARM_LABEL_PREFIX = "arm:"


def parse_arm(text: str) -> tuple[str, dict[str, str]]:
    """``name`` or ``name:PM_A=1,PM_B=x`` -> (name, environment)."""

    name, _, assignments = str(text).partition(":")
    name = name.strip()
    if not name or any(ch in name for ch in " ,="):
        raise ValueError(f"arm_name_invalid:{text}")
    environment: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in assignments.split(","))):
        key, sep, value = part.partition("=")
        key = key.strip()
        if not sep or not key.startswith("PM_"):
            raise ValueError(f"arm_knob_invalid:{part}")
        environment[key] = value.strip()
    return name, environment


def arm_command(args: argparse.Namespace, name: str) -> list[str]:
    command = [
        sys.executable, str(LEDGER_SCRIPT),
        "--capital", args.capital,
        "--opening-cash-usdc", str(args.opening_cash_usdc),
        "--fill-model", args.fill_model,
        "--ledger-db", str(args.ledger_db),
        "--label", ARM_LABEL_PREFIX + name,
        "--progress", "0",
    ]
    for flag, value in (
        ("--from-event-date", args.from_event_date),
        ("--to-event-date", args.to_event_date),
        ("--stations", args.stations),
    ):
        if value:
            command += [flag, str(value)]
    return command


def _run_id(stdout: str) -> str:
    """The walk prints its summary as one JSON document, run id included."""

    try:
        record = json.loads(stdout)
    except ValueError as exc:
        raise RuntimeError("arm_run_printed_no_summary") from exc
    if not isinstance(record, Mapping) or not record.get("run_id"):
        raise RuntimeError("arm_run_printed_no_run_id")
    return str(record["run_id"])


def run_arms(args: argparse.Namespace) -> list[str]:
    arms = [parse_arm(text) for text in args.arm]
    names = [name for name, _env in arms]
    if len(set(names)) != len(names):
        raise ValueError("arm_names_repeat")
    run_ids = []
    for name, environment in arms:
        # The arm's knobs on top of this shell's environment, and no stale
        # knob from a previous arm: each arm is its own process.
        env = {**os.environ, **environment}
        print(f"[arm {name}] {json.dumps(environment, sort_keys=True)}", file=sys.stderr, flush=True)
        done = subprocess.run(
            arm_command(args, name), cwd=ROOT, env=env,
            capture_output=True, text=True, check=False,
        )
        if done.returncode != 0:
            raise RuntimeError(f"arm_{name}_failed:{done.stderr[-2000:]}")
        run_ids.append(_run_id(done.stdout))
    return run_ids


def _stage_pnl(conn: sqlite3.Connection, run_id: str) -> dict[str, dict[str, float]]:
    """Fills and settled PnL by stage and execution family, per order.

    An order's payout is its filled shares when its bucket is the one the
    day settled to; its PnL is that payout less what the fill cost.  Days
    without a production label yet are left out, as in the run's summary.
    """

    out: dict[str, dict[str, float]] = {}
    for stage, candidate, filled, cost, won in conn.execute(
        "SELECT o.stage, o.candidate, o.filled_size, o.filled_cost_usdc,"
        " o.bucket = s.settled_bucket"
        " FROM orders o JOIN settlements s"
        "   ON s.run_id=o.run_id AND s.station_id=o.station_id"
        "  AND s.event_date=o.event_date"
        " WHERE o.run_id=? AND o.filled_size > 0 AND s.settled_bucket IS NOT NULL",
        (run_id,),
    ):
        key = f"{stage or '?'} {candidate or '?'}"
        cell = out.setdefault(key, {"fills": 0, "cost_usdc": 0.0, "pnl_usdc": 0.0})
        cell["fills"] += 1
        cell["cost_usdc"] += float(cost or 0.0)
        cell["pnl_usdc"] += (float(filled or 0.0) if won else 0.0) - float(cost or 0.0)
    return {
        key: {**cell, "cost_usdc": round(cell["cost_usdc"], 4),
              "pnl_usdc": round(cell["pnl_usdc"], 4)}
        for key, cell in sorted(out.items())
    }


def compare(conn: sqlite3.Connection, run_ids: Sequence[str]) -> dict[str, Any]:
    rows = []
    for run_id in run_ids:
        found = conn.execute(
            "SELECT code_head, params_json, summary_json FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if found is None:
            raise ValueError(f"run_not_found:{run_id}")
        params = json.loads(found[1] or "{}")
        summary = json.loads(found[2] or "{}")
        rows.append({
            "run_id": run_id,
            "label": params.get("label") or "",
            "code_head": found[0],
            "pm_environment": params.get("pm_environment") or {},
            "window": [params.get("from_event_date"), params.get("to_event_date")],
            "capital": params.get("capital"),
            "fill_model": summary.get("fill_model"),
            "maker_fill_curves_sha256": summary.get("maker_fill_curves_sha256"),
            "settled_station_days": summary.get("settled_station_days"),
            "settled_cost_usdc": summary.get("settled_cost_usdc"),
            "settled_pnl_usdc": summary.get("settled_pnl_usdc"),
            "settled_return_on_cost": summary.get("settled_return_on_cost"),
            "orders_by_state": summary.get("orders_by_state") or {},
            "by_stage": _stage_pnl(conn, run_id),
        })
    same = {
        key: len({json.dumps(r[key], sort_keys=True) for r in rows}) <= 1
        for key in ("code_head", "window", "capital", "fill_model", "maker_fill_curves_sha256")
    }
    return {"runs": rows, "comparable": all(same.values()), "same": same}


def _print(table: Mapping[str, Any]) -> None:
    if not table["comparable"]:
        differ = [key for key, ok in table["same"].items() if not ok]
        print(f"NOT COMPARABLE: the runs differ in {', '.join(differ)}")
    for row in table["runs"]:
        ret = row["settled_return_on_cost"]
        print(
            f"{row['label'] or row['run_id']:<24} {row['code_head']:<9}"
            f" days {row['settled_station_days']!s:>3}"
            f"  cost {row['settled_cost_usdc']!s:>9}  pnl {row['settled_pnl_usdc']!s:>9}"
            f"  return {'-' if ret is None else f'{100 * ret:+.1f}%'}"
            f"  orders {json.dumps(row['orders_by_state'], sort_keys=True)}"
        )
        for key, cell in row["by_stage"].items():
            print(f"    {key:<18} fills {cell['fills']:>4}  cost {cell['cost_usdc']:>9.2f}"
                  f"  pnl {cell['pnl_usdc']:>+9.2f}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", action="append", default=[],
                        help="name or name:PM_KNOB=value[,PM_KNOB=value]; repeat per arm")
    parser.add_argument("--compare", nargs="+", default=None, metavar="RUN_ID",
                        help="print the table for runs already made, run nothing")
    parser.add_argument("--from-event-date", default=None)
    parser.add_argument("--to-event-date", default=None)
    parser.add_argument("--stations", default="")
    parser.add_argument("--capital", choices=("production", "paper"), default="paper")
    parser.add_argument("--opening-cash-usdc", type=float, default=120.0)
    parser.add_argument("--fill-model", choices=("empirical", "none"), default="empirical")
    parser.add_argument("--ledger-db", default=str(LEDGER_DB))
    parser.add_argument("--json", action="store_true", help="print the table as JSON")
    args = parser.parse_args(argv)
    if args.compare:
        run_ids = list(args.compare)
    else:
        if not args.arm:
            parser.error("give --arm at least once, or --compare")
        if not (args.from_event_date and args.to_event_date):
            parser.error("an arm walk needs --from-event-date and --to-event-date")
        run_ids = run_arms(args)
    conn = sqlite3.connect(f"file:{args.ledger_db}?mode=ro", uri=True)
    try:
        table = compare(conn, run_ids)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(table, ensure_ascii=False, indent=1, sort_keys=True))
    else:
        _print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
