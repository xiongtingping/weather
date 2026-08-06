#!/usr/bin/env python3
"""Build an ET-day ICON Global/EPS archive from retained native evidence.

This command is strictly offline.  It accepts an explicit, exact set of
already retained cohort/capture receipts, reconstructs the 40-member ET-day
maximum vector, builds the immutable native-arrival index, and binds that
index to the derived archive.  It never searches for or downloads a missing
lead and never writes SQLite, PR04, runtime state, or orders.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.icon_eps_daily_curve import (  # noqa: E402
    build_icon_eps_daily_archive,
)
from bayes_basket_target.icon_eps_native_arrival_index import (  # noqa: E402
    bind_icon_eps_daily_archive_arrival_index,
    build_icon_eps_native_arrival_index,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
)


MAX_INPUTS = 26
SETTLEMENT_TIMEZONE = "America/New_York"


def _load(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    value = load_bounded_json(resolved, limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise ContractError("pr07_icon_eps_et_input_mapping_required")
    return dict(value)


def _paths(values: list[Path], label: str) -> list[Path]:
    resolved = [path.expanduser().resolve(strict=True) for path in values]
    if (
        not resolved
        or len(resolved) > MAX_INPUTS
        or len(set(resolved)) != len(resolved)
        or any(not path.is_file() or path.is_symlink() for path in resolved)
    ):
        raise ContractError(f"pr07_icon_eps_et_{label}_scope_invalid")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", action="append", type=Path, required=True)
    parser.add_argument("--capture", action="append", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--archive-output", type=Path, required=True)
    parser.add_argument("--index-output", type=Path, required=True)
    parser.add_argument("--bound-archive-output", type=Path, required=True)
    args = parser.parse_args()

    cohort_paths = _paths(args.cohort, "cohort")
    capture_paths = _paths(args.capture, "capture")
    cohorts = [_load(path) for path in cohort_paths]
    captures = [_load(path) for path in capture_paths]
    stations = coordinate_registry_manifest()["station_ids"]
    archive = build_icon_eps_daily_archive(
        cohorts=cohorts,
        event_date=args.event_date,
        station_timezones={
            str(station): SETTLEMENT_TIMEZONE for station in stations
        },
        built_at_utc=args.as_of_utc,
    )
    index = build_icon_eps_native_arrival_index(
        cohorts,
        captures,
        indexed_at_utc=args.as_of_utc,
        raw_root=args.raw_root,
    )
    bound = bind_icon_eps_daily_archive_arrival_index(archive, index)

    archive_path = args.archive_output.expanduser().resolve()
    index_path = args.index_output.expanduser().resolve()
    bound_path = args.bound_archive_output.expanduser().resolve()
    write_bounded_json_if_changed(
        archive_path, archive, limit=MAX_JSON_INPUT_BYTES
    )
    write_bounded_json_if_changed(index_path, index, limit=MAX_JSON_INPUT_BYTES)
    write_bounded_json_if_changed(bound_path, bound, limit=MAX_JSON_INPUT_BYTES)
    print(json.dumps({
        "state": "MATERIALIZED_UNPUBLISHED",
        "event_date": args.event_date,
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "station_count": archive["station_count"],
        "member_count": archive["member_count"],
        "lead_count": len(archive["lead_results"]),
        "archive_content_sha256": archive["content_sha256"],
        "native_arrival_index_content_sha256": index["content_sha256"],
        "bound_archive_content_sha256": bound["content_sha256"],
        "native_arrival_index_eligible": bound[
            "native_arrival_index_eligible"
        ],
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "orders_submitted": 0,
        "archive_output": str(archive_path),
        "index_output": str(index_path),
        "bound_archive_output": str(bound_path),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
