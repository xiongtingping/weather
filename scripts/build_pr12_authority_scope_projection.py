#!/usr/bin/env python3
"""Build the single sealed 45-station AWC/ET authority parent for PR12."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.pr12_authority_scope_projection import (  # noqa: E402
    REQUIRED_DOCTRINE_SHA256, build_pr12_authority_scope_projection,
)
from bayes_basket_target.resource_bounds import write_bounded_json_if_changed  # noqa: E402
from bayes_basket_target.station_source_authority import (  # noqa: E402
    resolve_global_awc_source_authority,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--doctrine-directory", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database = args.database.expanduser().resolve(strict=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        registry = {
            str(row["station_id"]): dict(row) for row in conn.execute(
                "SELECT station_id,settlement_definition,content_sha256 "
                "FROM calibration_station_registry ORDER BY station_id"
            )
        }
        authorities = {
            station: dict(resolve_global_awc_source_authority(conn, station_id=station))
            for station in sorted(registry)
        }
    doctrine = args.doctrine_directory.expanduser().resolve(strict=True)
    projection = build_pr12_authority_scope_projection(
        doctrine_paths=[doctrine / name for name in REQUIRED_DOCTRINE_SHA256],
        station_registry=registry, station_source_authorities=authorities,
        as_of_utc=args.as_of_utc,
    )
    write_bounded_json_if_changed(args.output.expanduser().resolve(), projection)
    print(json.dumps({
        "state": projection["state"], "station_count": projection["station_count"],
        "content_sha256": projection["content_sha256"], "consumable": False,
        "writes_database": False, "publishes_pr04_pointer": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
