"""Run the execution arms against one replay database and print the table.

Two questions, one sweep.

**G** -- is production's execution family the right one, and should a leg
the book's own centre has ruled out be bought at all?  Measured off the
book (docs/research/VENUE_PRICE_CALIBRATION_2026-09-08.md): taking beats
resting in all three price tiers once the reading is conditioned on
distance, and a leg three rungs out returns -66.7% against -21.7% for the
same price adjacent.  Those are book-level numbers; this puts them through
the ledger, which is the only thing that scores a whole basket.

**W** -- charter 79 measured width 3 > 2 > 1, and that measurement priced
the far leg the way this ledger prices it, not the way production pays for
it.  So the width arms are re-run under BOTH execution families.  If
3 > 2 survives at production's price the charter stands and arm G's
distance gate is the whole of the change; if it inverts, the third leg was
paying for itself only at a price nobody was ever charged.

Every arm runs against the SAME replay database, rebuilt in place between
arms, so the envelopes, the truth and the priors are identical and the only
thing that moves is the switch under test.  Nothing here opens the
production SSOT for writing, and nothing here changes a live config file:
the arms are environment variables on a subprocess.

    python3 scripts/build_ledger_replay_db.py --since 2026-09-01 \\
        --out /tmp/arm_replay.db
    python3 scripts/run_execution_arm_sweep.py --db /tmp/arm_replay.db

WHICH ARMS THIS SWEEP CAN ACTUALLY SETTLE (2026-09-08, verified against the
live book).  ``PAPER_LEDGER_FILL_MODEL`` defaults to ``through``: a resting
rung fills when the book traded through it.  The live maker family filled
46 of 494 posted legs -- 9.3% -- so the model is optimistic by about an
order of magnitude, and whether that optimism is width-dependent is not
measured.  Therefore:

* ``W*-taker`` is the clean width comparison and the one that answers
  charter 79.  Under taker there is no fill model at all: every leg is
  bought at the ask.
* ``G-dist2`` against ``base`` is clean -- same family, same fill model,
  the gate is the only thing that moves.
* ``W*-maker`` and ``G-taker`` both carry the fill model into the answer.
  They are recorded, not adjudicated.  Maker against taker has to be
  settled on the shadow book and the live arm instead.

Foreground and serial on purpose.  A ledger rebuild holds the write lock on
its database, so two arms at once would either block or interleave, and an
interleaved book is not any arm's book.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# name -> the environment that defines the arm.  Empty is the shipped
# default, which is what production runs, and it is measured first so a
# drift in anything else shows up as the baseline moving.
ARMS: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("base", "生产等价：挂单 + 宽度 3-4 + 无距离闸", {}),
    ("G-taker", "只改执行族：吃单", {
        "PAPER_LEDGER_EXECUTION": "taker"}),
    ("G-dist2", "只改距离闸：>=3 格不买", {
        "PAPER_LEDGER_MARKET_DISTANCE_MAX": "2"}),
    ("G", "吃单 + 距离闸<=2", {
        "PAPER_LEDGER_EXECUTION": "taker",
        "PAPER_LEDGER_MARKET_DISTANCE_MAX": "2"}),
    # Width, held at exactly k on both sides of the bound.
    ("W2-maker", "宽度恒2 · 挂单", {
        "PAPER_LEDGER_MIN_SUPPORT_BUCKETS": "2",
        "PAPER_LEDGER_MAX_SUPPORT_BUCKETS": "2"}),
    ("W3-maker", "宽度恒3 · 挂单", {
        "PAPER_LEDGER_MIN_SUPPORT_BUCKETS": "3",
        "PAPER_LEDGER_MAX_SUPPORT_BUCKETS": "3"}),
    ("W4-maker", "宽度恒4 · 挂单", {
        "PAPER_LEDGER_MIN_SUPPORT_BUCKETS": "4",
        "PAPER_LEDGER_MAX_SUPPORT_BUCKETS": "4"}),
    ("W2-taker", "宽度恒2 · 吃单", {
        "PAPER_LEDGER_EXECUTION": "taker",
        "PAPER_LEDGER_MIN_SUPPORT_BUCKETS": "2",
        "PAPER_LEDGER_MAX_SUPPORT_BUCKETS": "2"}),
    ("W3-taker", "宽度恒3 · 吃单", {
        "PAPER_LEDGER_EXECUTION": "taker",
        "PAPER_LEDGER_MIN_SUPPORT_BUCKETS": "3",
        "PAPER_LEDGER_MAX_SUPPORT_BUCKETS": "3"}),
    ("W4-taker", "宽度恒4 · 吃单", {
        "PAPER_LEDGER_EXECUTION": "taker",
        "PAPER_LEDGER_MIN_SUPPORT_BUCKETS": "4",
        "PAPER_LEDGER_MAX_SUPPORT_BUCKETS": "4"}),
)


def _fingerprint(env: dict[str, str]) -> str:
    """The arm's own selection-rule hash, read the way the ledger reads it.

    Imported in a subprocess because the constants are module-level and read
    once: a second import in this process would return the first arm's
    values for every arm after it, and the sweep would report ten identical
    fingerprints over ten different books.
    """

    out = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,'scripts');"
         "import paper_trading_ledger as L;print(L.SELECTION_RULE_SHA256)"],
        cwd=ROOT, env={**os.environ, **env},
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()[:12]


def _rebuild(db: Path, env: dict[str, str], through: str | None) -> None:
    command = [sys.executable, "scripts/paper_trading_ledger.py",
               "--db", str(db), "--rebuild"]
    if through:
        command += ["--through-date", through]
    result = subprocess.run(
        command, cwd=ROOT, env={**os.environ, **env},
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        raise SystemExit("ledger rebuild failed:\n" + "\n".join(tail[-25:]))


def _read(db: Path) -> dict[str, float]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(s.stake_usdc),0),"
        " COALESCE(SUM(s.pnl_usdc),0), COALESCE(SUM(s.hit),0),"
        " COALESCE(AVG(p.window_size),0)"
        " FROM paper_trade_settlement s"
        " JOIN paper_trade_position p ON p.position_id=s.position_id"
    ).fetchone()
    positions = conn.execute(
        "SELECT COUNT(*) FROM paper_trade_position").fetchone()[0]
    conn.close()
    settled, stake, pnl, hits, width = row
    return {
        "positions": int(positions), "settled": int(settled),
        "stake": float(stake), "pnl": float(pnl), "hits": int(hits),
        "hit_rate": (hits / settled) if settled else 0.0,
        "roi": (pnl / stake) if stake else 0.0,
        "mean_width": float(width),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True,
                        help="the REPLAY database, never the production SSOT")
    parser.add_argument("--through-date", default=None)
    parser.add_argument("--only", default="",
                        help="comma-separated arm names; default all")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    database = args.db.expanduser().resolve(strict=True)
    if database.name == "bayes_basket_target_ssot.db":
        raise SystemExit(
            "refusing to run arms against the production SSOT: the ledger"
            " rebuilds paper_trade_* in place and the board reads them."
            "  Build a replay database first"
            " (scripts/build_ledger_replay_db.py).")

    wanted = {n.strip() for n in args.only.split(",") if n.strip()}
    arms = [a for a in ARMS if not wanted or a[0] in wanted]

    results = []
    for name, label, env in arms:
        sha = _fingerprint(env)
        print(f"[{name}] {label}  rule={sha} ...", flush=True)
        _rebuild(database, env, args.through_date)
        stats = _read(database)
        stats.update({"arm": name, "label": label, "rule_sha256": sha,
                      "env": env})
        results.append(stats)
        print(f"    仓位 {stats['positions']:4d}  已结算 {stats['settled']:4d}"
              f"  投入 {stats['stake']:8.2f}  盈亏 {stats['pnl']:+8.2f}"
              f"  ROI {stats['roi']:+7.2%}  命中 {stats['hit_rate']:6.1%}"
              f"  均宽 {stats['mean_width']:.2f}", flush=True)

    # A sweep whose arms share a fingerprint measured one thing ten times.
    distinct = {r["rule_sha256"] for r in results}
    print()
    print(f"{'臂':11}{'规则指纹':14}{'仓位':>6}{'已结算':>7}{'投入':>10}"
          f"{'盈亏':>10}{'ROI':>9}{'命中':>8}{'均宽':>7}  说明")
    for r in results:
        print(f"{r['arm']:11}{r['rule_sha256']:14}{r['positions']:6d}"
              f"{r['settled']:7d}{r['stake']:10.2f}{r['pnl']:+10.2f}"
              f"{r['roi']:+9.2%}{r['hit_rate']:8.1%}{r['mean_width']:7.2f}"
              f"  {r['label']}")
    if len(distinct) < len(results):
        print("\n⚠ 有臂共享规则指纹——它们记的是同一本账，读数不可比。")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "schema": "execution_arm_sweep_v1",
            "db": str(database), "through_date": args.through_date,
            "arms": results,
        }, indent=1, ensure_ascii=False) + "\n")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
