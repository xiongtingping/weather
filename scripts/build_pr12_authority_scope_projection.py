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
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.resource_bounds import write_bounded_json_if_changed  # noqa: E402
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)
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
        # The projection is over the fleet -- its validator compares its rows
        # to the coordinate registry -- and calibration_station_registry is
        # not the fleet: onboarding writes a station's row before the station
        # is admitted (LLBG, LTFM and UUWW have had rows since 2026-08-28), and
        # the table is append-only.  Minting from the whole table would build
        # a projection the validator refuses, at any fleet size.
        fleet = set(coordinate_registry_manifest()["station_ids"])
        registry = {
            str(row["station_id"]): dict(row) for row in conn.execute(
                "SELECT station_id,settlement_definition,content_sha256 "
                "FROM calibration_station_registry ORDER BY station_id"
            )
            if str(row["station_id"]).upper() in fleet
        }
        if set(registry) != fleet:
            raise ContractError(
                "pr12_authority_projection_registry_rows_missing:"
                + ",".join(sorted(fleet - set(registry)))
            )
        authorities = {
            station: dict(resolve_global_awc_source_authority(conn, station_id=station))
            for station in sorted(registry)
        }
        # The listing decides the day, per the 2026-08-26 doctrine amendment.
        # Not calibration_station_registry.timezone_name, which is mislabelled
        # for 33 of the 45 and which the doctrine forbids deriving from; the
        # binding table stands behind the listing for a station the current
        # registry snapshot does not cover.
        listed = {
            str(row["station_id"]).upper(): str(row["settlement_timezone"])
            for row in conn.execute(
                "SELECT station_id,settlement_timezone,COUNT(*) AS n "
                "FROM basket_target_market_registry WHERE source_eligible=1 "
                "GROUP BY station_id,settlement_timezone ORDER BY station_id,n"
            )
        }
        bound = {
            str(row["station_id"]).upper(): str(row["settlement_timezone"])
            for row in conn.execute(
                "SELECT station_id,settlement_timezone "
                "FROM calibration_station_market_timezone_binding"
            )
        }
        timezones = {
            station: listed.get(station) or bound[station]
            for station in sorted(registry)
            if station in listed or station in bound
        }
        missing = sorted(set(registry) - set(timezones))
        if missing:
            raise ContractError(
                "pr12_authority_projection_settlement_timezone_missing:"
                + ",".join(missing)
            )
    doctrine = args.doctrine_directory.expanduser().resolve(strict=True)
    projection = build_pr12_authority_scope_projection(
        doctrine_paths=[doctrine / name for name in REQUIRED_DOCTRINE_SHA256],
        station_registry=registry, station_source_authorities=authorities,
        station_settlement_timezones=timezones, as_of_utc=args.as_of_utc,
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
