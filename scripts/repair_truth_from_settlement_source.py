#!/usr/bin/env python3
"""Charter 225: repair settled truths from each market's own settlement source.

User ruling 2026-09-25, 「全部按结算」.  Two steps, both explicit:

``capture-wu``  fetch the Wunderground daily history page of every case whose
                market settles there, in the market's own unit, and keep the
                raw bytes (gzip) with the fetch time -- the page is the
                settlement source, so its bytes are the evidence.
``repair``      rebuild every case from its captures and hand it to
                ``settlement_source_truth.materialize_settlement_source_truth``;
                a dry run by default, ``--commit`` writes, one short
                BEGIN IMMEDIATE per station-day.

NOAA time-series captures are page-side summaries taken in a browser (the
page fetches its own data with its own key); they sit in the same capture
tree as ``noaa_summary.json`` with a ``.meta`` sidecar.

A case file is a JSON list of ``{station_id, event_date, slug, family, unit,
resolution_source, winning_bucket, closed}`` -- the market's own resolution.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
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
from bayes_basket_target.settlement_source_truth import (  # noqa: E402
    NOAA_FAMILY,
    WU_FAMILY,
    build_evidence,
    materialize_settlement_source_truth,
)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
CAPTURES = ROOT / "data" / "settlement_source_captures"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
WU_OBJECT = "wu_daily_history.html.gz"
NOAA_OBJECT = "noaa_summary.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def wu_capture_url(case: dict) -> str:
    year, month, day = case["event_date"].split("-")
    return case["resolution_source"].rstrip("/") + f"/date/{int(year)}-{int(month)}-{int(day)}"


def capture_wu(cases: list[dict], captures: Path, *, pause_seconds: float) -> list[dict]:
    import httpx  # noqa: PLC0415 - only the capture step touches the network

    done = []
    for case in cases:
        if case["family"] != WU_FAMILY:
            continue
        folder = captures / case["station_id"] / case["event_date"]
        target = folder / WU_OBJECT
        if target.exists():
            done.append({"station_id": case["station_id"], "event_date": case["event_date"], "state": "kept"})
            continue
        folder.mkdir(parents=True, exist_ok=True)
        url = wu_capture_url(case)
        cookie = "wu_units=" + ("e" if case["unit"] == "F" else "m")
        for attempt in range(4):
            try:
                response = httpx.get(
                    url, headers={"User-Agent": USER_AGENT, "Cookie": cookie},
                    timeout=60, follow_redirects=False,
                )
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(5 * (attempt + 1))
        else:
            raise SystemExit(f"capture failed: {url}")
        body = response.content
        target.write_bytes(gzip.compress(body))
        (folder / (WU_OBJECT + ".meta")).write_text(json.dumps({
            "capture_url": url,
            "cookie": cookie,
            "captured_at_utc": _now(),
            "raw_sha256": hashlib.sha256(body).hexdigest(),
            "raw_bytes": len(body),
        }, sort_keys=True))
        done.append({"station_id": case["station_id"], "event_date": case["event_date"], "state": "captured"})
        time.sleep(pause_seconds)
    return done


def load_case(case: dict, captures: Path, conn: sqlite3.Connection) -> dict:
    folder = captures / case["station_id"] / case["event_date"]
    name = WU_OBJECT if case["family"] == WU_FAMILY else NOAA_OBJECT
    path = folder / name
    meta = json.loads((folder / (name + ".meta")).read_text())
    raw = gzip.decompress(path.read_bytes()) if name.endswith(".gz") else path.read_bytes()
    zone = settlement_timezone_for_scope(
        conn, station_id=case["station_id"], event_date=case["event_date"]
    )
    if not zone:
        raise ContractError("settlement_source_settlement_timezone_unresolved")
    return {
        "station_id": case["station_id"],
        "event_date": case["event_date"],
        "settlement_timezone": zone,
        "market": {
            "slug": case["slug"],
            "family": case["family"],
            "unit": case["unit"],
            "resolution_source": case["resolution_source"],
            "winning_bucket": case["winning_bucket"],
            "closed": case["closed"],
        },
        "source": {
            "family": case["family"],
            "capture_url": meta["capture_url"],
            "object_ref": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
            "raw_bytes": raw,
            "raw_sha256": meta["raw_sha256"],
            "captured_at_utc": meta["captured_at_utc"],
        },
    }


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
    cap = sub.add_parser("capture-wu")
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
    if args.command == "capture-wu":
        result = capture_wu(cases, args.captures, pause_seconds=args.pause_seconds)
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
