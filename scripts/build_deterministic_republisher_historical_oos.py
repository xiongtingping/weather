#!/usr/bin/env python3
"""Build a bounded Open-Meteo deterministic historical OOS candidate.

The command reads only retained historical forecast receipts and a read-only
AWC truth database.  It writes an audit artifact under ``output/audits``; no
native arrival, P1, production database, or order state is touched.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.deterministic_republisher_oos import (  # noqa: E402
    build_deterministic_republisher_oos,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)


DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = ROOT / "output" / "audits" / (
    "open_meteo_aifs_deterministic_historical_oos_20260802.json"
)


def _as_of(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit("as_of_utc_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SystemExit("as_of_utc_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _load(path: Path) -> Mapping[str, Any]:
    value = load_bounded_json(path.expanduser().resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise SystemExit("capture_mapping_required")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", action="append", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--as-of-utc",
        default=datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    )
    args = parser.parse_args()
    captures = [_load(path) for path in args.capture]
    db = args.db.expanduser().resolve(strict=True)
    if db.is_symlink():
        raise SystemExit("database_symlink_forbidden")
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        report = build_deterministic_republisher_oos(
            captures,
            conn,
            as_of_utc=_as_of(args.as_of_utc),
        )
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, report, limit=MAX_JSON_INPUT_BYTES)
    print(json.dumps({
        "schema": report["schema"],
        "state": report["state"],
        "model_id": report["model_id"],
        "provider_id": report["provider_id"],
        "station_count": report["station_count"],
        "curve_count": report["curve_count"],
        "truth_count": report["truth_count"],
        "candidate_count": report["candidate_count"],
        "shortfall_count": len(report["shortfalls"]),
        "formal_p0_eligible": report["formal_p0_eligible"],
        "writes_production_database": report["oos_policy"]["production_database_written"],
        "native_arrival_index_written": report["oos_policy"]["native_arrival_index_written"],
        "p1_authority_written": report["oos_policy"]["p1_authority_written"],
        "calls_order_gateway": report["oos_policy"]["calls_order_gateway"],
        "orders_submitted": report["oos_policy"]["orders_submitted"],
        "output": str(output),
        "wrote": wrote,
        "content_sha256": report["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if report["state"] == "READY_CANDIDATES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
