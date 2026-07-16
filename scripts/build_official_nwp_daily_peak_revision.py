#!/usr/bin/env python3
"""Read immutable official point history and build one daily-peak revision.

No network, archive, posterior, task or order write is performed.  The caller
must state every expected valid time; this command intentionally has no hourly
default because a guessed curve coverage would be false evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from official_nwp_daily_peak_revision_runtime import build_official_nwp_daily_peak_revision  # noqa: E402


def _load_history(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    snapshots: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for object_path in sorted(path.glob("*.json")):
        try:
            value = json.loads(object_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            unreadable.append(str(object_path))
            continue
        if isinstance(value, dict):
            snapshots.append(value)
        else:
            unreadable.append(str(object_path))
    return snapshots, unreadable


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=("ecmwf", "icon", "gfs", "jma"))
    parser.add_argument("--target-event-date", required=True)
    parser.add_argument("--target-timezone", required=True)
    parser.add_argument("--expected-valid-time-utc", action="append", required=True)
    parser.add_argument("--history-dir", type=Path)
    args = parser.parse_args(argv)
    history_dir = args.history_dir or ROOT / "data" / "runtime" / "official_nwp_sources" / args.provider / "history"
    snapshots, unreadable = _load_history(history_dir.expanduser().resolve()) if history_dir.is_dir() else ([], [str(history_dir)])
    result = build_official_nwp_daily_peak_revision(
        provider_id=args.provider,
        point_snapshots=snapshots,
        target_event_date=args.target_event_date,
        target_timezone=args.target_timezone,
        expected_valid_times_utc=list(args.expected_valid_time_utc),
    )
    result["history_dir"] = str(history_dir)
    result["history_snapshot_count"] = len(snapshots)
    result["history_unreadable_paths"] = unreadable
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
