#!/usr/bin/env python3
"""Charter 225: repair settled truths from each market's own settlement source.

User ruling 2026-09-25, 「全部按结算」.  Two steps, both explicit:

``capture``     fetch the settlement page of every case not captured yet
                (``settlement_source_capture``): Wunderground's page in the
                market's own unit, raw bytes kept; NOAA's series through the
                page's own parameters, page-side summary kept.
``repair``      rebuild every case from its captures and hand it to
                ``settlement_source_truth.materialize_settlement_source_truth``;
                a dry run by default, ``--commit`` writes, one short
                BEGIN IMMEDIATE per station-day.

The first batch's NOAA captures (2026-09-25) were page-side summaries taken
in a browser; they sit in the same capture tree as ``noaa_summary.json``.

A case file is a JSON list of ``{station_id, event_date, slug, family, unit,
resolution_source, winning_bucket, closed}`` -- the market's own resolution.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.market_registry import settlement_timezone_for_scope  # noqa: E402
from bayes_basket_target.settlement_source_capture import (  # noqa: E402
    NOAA_OBJECT,
    WU_OBJECT,
    capture,
    load_case as load_captured_case,
)
from bayes_basket_target.settlement_source_truth import (  # noqa: E402
    WU_FAMILY,
    build_evidence,
    materialize_settlement_source_truth,
)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
CAPTURES = ROOT / "data" / "settlement_source_captures"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def capture_missing(cases: list[dict], captures: Path, *, pause_seconds: float) -> list[dict]:
    """Capture every case not captured yet (both page families)."""

    import httpx  # noqa: PLC0415 - only the capture step touches the network

    from official_nwp_https_transport import official_nwp_proxy_url  # noqa: PLC0415

    done = []
    with httpx.Client(
        proxy=official_nwp_proxy_url() or None, trust_env=False,
        timeout=httpx.Timeout(60.0, connect=20.0), follow_redirects=False,
    ) as client:
        for case in cases:
            name = WU_OBJECT if case["family"] == WU_FAMILY else NOAA_OBJECT
            key = {"station_id": case["station_id"], "event_date": case["event_date"]}
            if (captures / case["station_id"] / case["event_date"] / name).exists():
                done.append({**key, "state": "kept"})
                continue
            try:
                capture(client, market=_market(case), station_id=case["station_id"],
                        event_date=case["event_date"], captures=captures)
                done.append({**key, "state": "captured"})
            except (ContractError, OSError) as exc:
                done.append({**key, "state": "refused", "reason": f"{type(exc).__name__}:{exc}"})
            time.sleep(pause_seconds)
    return done


def _market(case: dict) -> dict:
    return {key: case[key] for key in (
        "slug", "family", "unit", "resolution_source", "winning_bucket", "closed")}


def load_case(case: dict, captures: Path, conn: sqlite3.Connection) -> dict:
    zone = settlement_timezone_for_scope(
        conn, station_id=case["station_id"], event_date=case["event_date"]
    )
    if not zone:
        raise ContractError("settlement_source_settlement_timezone_unresolved")
    return load_captured_case(
        market=_market(case), station_id=case["station_id"], event_date=case["event_date"],
        captures=captures, settlement_timezone=zone, root=ROOT,
    )


def repair(cases: list[dict], captures: Path, db: Path, *, commit: bool) -> list[dict]:
    uri = f"file:{db}" + ("" if commit else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, timeout=600.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=600000")
    results = []
    try:
        for case in cases:
            key = {"station_id": case["station_id"], "event_date": case["event_date"]}
            try:
                loaded = load_case(case, captures, conn)
                if not commit:
                    evidence = build_evidence(loaded)
                    results.append({**key, "state": "dry_run", **evidence["decision"]})
                    continue
                conn.execute("BEGIN IMMEDIATE")
                try:
                    receipt = materialize_settlement_source_truth(
                        conn, case=loaded, as_of_utc=_now()
                    )
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                results.append({**key, **{k: receipt.get(k) for k in (
                    "state", "wrote", "basis", "official_tmax_c", "truth_id",
                    "revision_no", "supersedes")}})
            except (ContractError, OSError, KeyError, ValueError) as exc:
                results.append({**key, "state": "refused", "reason": f"{type(exc).__name__}:{exc}"})
    finally:
        conn.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture")
    cap.add_argument("--cases", type=Path, required=True)
    cap.add_argument("--captures", type=Path, default=CAPTURES)
    cap.add_argument("--pause-seconds", type=float, default=2.0)
    rep = sub.add_parser("repair")
    rep.add_argument("--cases", type=Path, required=True)
    rep.add_argument("--captures", type=Path, default=CAPTURES)
    rep.add_argument("--db", type=Path, default=DB)
    rep.add_argument("--commit", action="store_true")
    rep.add_argument("--receipt-output", type=Path)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())
    if args.command == "capture":
        result = capture_missing(cases, args.captures, pause_seconds=args.pause_seconds)
    else:
        result = repair(cases, args.captures, args.db, commit=args.commit)
    summary = {
        "command": args.command,
        "finished_at_utc": _now(),
        "count": len(result),
        "states": {state: sum(1 for row in result if row["state"] == state) for state in sorted({row["state"] for row in result})},
        "results": result,
    }
    text = json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=True)
    if getattr(args, "receipt_output", None):
        args.receipt_output.write_text(text)
    print(json.dumps({k: summary[k] for k in ("command", "count", "states")}, ensure_ascii=False))
    return 0 if all(row["state"] != "refused" for row in result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
