#!/usr/bin/env python3
"""Run paper-ledger arms in slices against one replay database, then pair them.

Three things this lane gets right that a hand-rolled loop kept getting
wrong, each of them having cost a re-run:

**One clean state per arm.**  ``--rebuild`` re-derives the positions and
settlements it can and keeps the refusals and the candidate windows.  A
refusal is an ANSWER -- the walk short-circuits on it for any settled
station-day -- so an arm that follows another arm in the same database
inherits the previous arm's noes and never solves those scopes.  Measured
2026-09-11: the second arm of a shared-database pair answered 783 scopes out
of the first arm's table and finished a slice in 59 seconds that takes two
and a half minutes.  Every arm here starts with ``--reset-paper-tables``.

**Slices, oldest first.**  Every seat is sized off the equity the days
before it left behind, so a slice taken newest-first is sized off rows the
next slice then re-derives.  The bound rises; nothing above it is touched.

**The export is not conditional on a report.**  The first attempt hung the
export off ``--report | json.load`` with ``&&``, the pipe returned non-zero,
the export was skipped, and the next arm's ``--rebuild`` ran anyway -- one
whole baseline arm lost, 27 minutes.  Here the export is its own step and
its failure stops the sweep.

Foreground and serial on purpose: a ledger walk holds the write lock.

Run it from a FROZEN tree if arms must be comparable across a session that
is still committing -- ``git archive HEAD | tar -x -C <dir>``, symlink
``data/``, run this copy.  It invokes the ledger beside itself, so a frozen
copy runs the frozen ledger.

    python3 scripts/build_ledger_replay_db.py --since 2026-08-28 \\
        --out /tmp/arms/replay.db
    python3 scripts/run_ledger_arm_slices.py \\
        --db /tmp/arms/replay.db --results /tmp/arms/results.db \\
        --slices 2026-08-30,2026-09-03,2026-09-05,2026-09-07,2026-09-08,2026-09-10 \\
        --arm base= \\
        --arm bcurve_book=PM_CHARTER_72_KELLY=off,PAPER_LEDGER_JUDGE_BANKROLL=book
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "scripts" / "paper_trading_ledger.py"
TABLES = ("position", "settlement", "refusal", "candidate_window")


def _run(command: list[str], env: dict[str, str], *, what: str) -> str:
    result = subprocess.run(
        command, cwd=ROOT, env={**os.environ, **env},
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        raise SystemExit(
            "%s failed (rc=%d):\n%s" % (what, result.returncode,
                                        "\n".join(tail[-25:]))
        )
    return result.stdout


def ledger_command(db: Path, *, bound: str, first_slice: bool) -> list[str]:
    """The command one slice runs.  One definition, so a test can read it.

    The reset belongs to the FIRST slice of each arm and to no other: it
    drops the tables, and a later slice must inherit this arm's own earlier
    days -- every seat is sized off the equity they left behind.
    """

    command = [
        sys.executable, str(LEDGER), "--db", str(db),
        "--backfill", "--recent-days", "0",
    ]
    if first_slice:
        command.append("--reset-paper-tables")
    if bound:
        command += ["--through-date", bound]
    return command


def _walk(db: Path, env: dict[str, str], slices: list[str], label: str) -> None:
    for index, bound in enumerate(slices):
        command = ledger_command(db, bound=bound, first_slice=index == 0)
        out = _run(command, env, what="%s slice %s" % (label, bound or "all"))
        counts = {}
        for line in out.strip().splitlines():
            try:
                counts = json.loads(line).get("counts", {})
            except (ValueError, AttributeError):
                continue
        print(
            "  %-14s through %-10s positions=%-4s settlements=%-4s"
            " inherited=%s"
            % (label, bound or "all", counts.get("positions", "?"),
               counts.get("settlements", "?"),
               counts.get("already_answered_settled", 0)),
            flush=True,
        )


def _export(db: Path, results: Path, label: str) -> dict[str, int]:
    conn = sqlite3.connect(str(db), timeout=60.0)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("ATTACH ? AS res", (str(results),))
        counts = {}
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS res.arm_{label}_{table}")
            conn.execute(
                f"CREATE TABLE res.arm_{label}_{table} AS"
                f" SELECT * FROM paper_trade_{table}"
            )
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM res.arm_{label}_{table}"
            ).fetchone()[0]
        conn.commit()
        return counts
    finally:
        conn.close()


def _chains(conn: sqlite3.Connection, label: str) -> dict:
    out: dict = {}
    for station, date, stake, pnl, hit in conn.execute(
        f"SELECT station_id, event_date, stake_usdc, pnl_usdc, hit"
        f" FROM arm_{label}_settlement"
    ):
        key = (str(station), str(date))
        row = out.setdefault(key, [0.0, 0.0, 0])
        row[0] += float(stake or 0.0)
        row[1] += float(pnl or 0.0)
        row[2] = max(row[2], int(hit or 0))
    return out


def _summary(conn: sqlite3.Connection, label: str) -> dict:
    chains = _chains(conn, label)
    stake = sum(row[0] for row in chains.values())
    pnl = sum(row[1] for row in chains.values())
    hits = sum(row[2] for row in chains.values())
    fingerprints = sorted(
        str(row[0])[:12] for row in conn.execute(
            f"SELECT DISTINCT selection_rule_sha256"
            f" FROM arm_{label}_candidate_window"
        )
    )
    return {
        "label": label,
        "positions": conn.execute(
            f"SELECT COUNT(*) FROM arm_{label}_position"
        ).fetchone()[0],
        "settled_chains": len(chains),
        "stake": round(stake, 2),
        "pnl": round(pnl, 2),
        "roi_pct": round(100.0 * pnl / stake, 2) if stake else None,
        "chain_hit_pct": round(100.0 * hits / len(chains), 1) if chains else None,
        "fingerprints": fingerprints,
        "by_stage": [
            [str(r[0]), r[1], round(r[2], 2), round(r[3], 2)]
            for r in conn.execute(
                f"SELECT stage, COUNT(*), SUM(stake_usdc), SUM(pnl_usdc)"
                f" FROM arm_{label}_settlement GROUP BY 1 ORDER BY 1"
            )
        ],
        "chains": [
            [k[0], k[1], round(v[0], 3), round(v[1], 3), v[2]]
            for k, v in sorted(chains.items())
        ],
    }


def _pair(conn: sqlite3.Connection, base: str, arm: str) -> dict:
    a, b = _chains(conn, base), _chains(conn, arm)
    both = sorted(set(a) & set(b))
    def total(chains: dict, keys) -> tuple[float, float, int]:
        return (
            sum(chains[k][0] for k in keys),
            sum(chains[k][1] for k in keys),
            sum(chains[k][2] for k in keys),
        )
    diffs = sorted((b[k][1] - a[k][1], k) for k in both)
    changed = [d for d in diffs if abs(d[0]) > 1e-9]
    out = {
        "base": base, "arm": arm,
        "sets": {}, "paired_diff": round(sum(d for d, _ in diffs), 2),
        "paired_chains": len(both),
        "changed": len(changed),
        "changed_better": sum(1 for d, _ in changed if d > 0),
        "changed_worse": sum(1 for d, _ in changed if d < 0),
        "drop_largest": {},
        "worst": [[k[0], k[1], round(d, 2)] for d, k in diffs[:3]],
        "best": [[k[0], k[1], round(d, 2)] for d, k in diffs[-3:]],
    }
    for name, keys, chains in (
        ("only_" + base, set(a) - set(b), a),
        ("only_" + arm, set(b) - set(a), b),
        ("both_" + base, both, a),
        ("both_" + arm, both, b),
    ):
        stake, pnl, hits = total(chains, keys)
        out["sets"][name] = {
            "chains": len(keys), "stake": round(stake, 2),
            "pnl": round(pnl, 2),
            "roi_pct": round(100.0 * pnl / stake, 2) if stake else None,
            "hits": hits,
        }
    by_size = sorted(diffs, key=lambda d: -abs(d[0]))
    for k in (1, 2, 3):
        out["drop_largest"][k] = round(sum(d for d, _ in by_size[k:]), 2)
    per_day: dict[str, float] = defaultdict(float)
    for diff, key in diffs:
        per_day[key[1]] += diff
    out["days_better"] = sum(1 for v in per_day.values() if v > 1e-9)
    out["days_worse"] = sum(1 for v in per_day.values() if v < -1e-9)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True,
                        help="replay database (NOT the production SSOT)")
    parser.add_argument("--results", type=Path, required=True,
                        help="where arm_<label>_* tables are kept")
    parser.add_argument("--slices", default="",
                        help="comma-separated --through-date bounds, oldest"
                             " first; empty means one unbounded pass")
    parser.add_argument("--arm", action="append", default=[],
                        help="label=KEY=VAL,KEY=VAL (repeatable; the first"
                             " arm is the pairing baseline)")
    parser.add_argument("--out", type=Path,
                        help="write the summary JSON here as well")
    args = parser.parse_args()

    database = args.db.expanduser().resolve(strict=True)
    slices = [s.strip() for s in args.slices.split(",") if s.strip()] or [""]
    arms: list[tuple[str, dict[str, str]]] = []
    for spec in args.arm:
        label, _, rest = spec.partition("=")
        env = {}
        for pair in rest.split(","):
            if not pair.strip():
                continue
            key, _, value = pair.partition("=")
            env[key.strip()] = value.strip()
        arms.append((label.strip(), env))
    if not arms:
        raise SystemExit("--arm is required")

    for label, env in arms:
        print("arm %s env=%s" % (label, env or "{}"), flush=True)
        _walk(database, env, slices, label)
        print("  exported %s" % _export(database, args.results, label),
              flush=True)

    conn = sqlite3.connect(f"file:{args.results}?mode=ro", uri=True)
    report = {
        "schema": "ledger_arm_slice_sweep_v1",
        "replay_db": database.name,
        "slices": slices,
        "arms": [_summary(conn, label) for label, _ in arms],
        "arm_env": {label: env for label, env in arms},
        "pairs": [
            _pair(conn, arms[0][0], label) for label, _ in arms[1:]
        ],
    }
    conn.close()
    for arm in report["arms"]:
        print(
            "%-14s positions=%-4d chains=%-4d stake=%9.2f pnl=%9.2f"
            " roi=%7s%% hit=%5s%% fp=%s"
            % (arm["label"], arm["positions"], arm["settled_chains"],
               arm["stake"], arm["pnl"], arm["roi_pct"],
               arm["chain_hit_pct"], ",".join(arm["fingerprints"]))
        )
    for pair in report["pairs"]:
        print(
            "%s vs %s: paired %+.2f on %d chains; drop 1/2/3 %+.2f/%+.2f/%+.2f;"
            " changed %d (%d better, %d worse); days %d better / %d worse"
            % (pair["arm"], pair["base"], pair["paired_diff"],
               pair["paired_chains"], pair["drop_largest"][1],
               pair["drop_largest"][2], pair["drop_largest"][3],
               pair["changed"], pair["changed_better"], pair["changed_worse"],
               pair["days_better"], pair["days_worse"])
        )
        for name, row in pair["sets"].items():
            print("    %-22s chains=%-4d stake=%9.2f pnl=%9.2f roi=%7s%%"
                  % (name, row["chains"], row["stake"], row["pnl"],
                     row["roi_pct"]))
    if args.out:
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
