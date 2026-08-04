#!/usr/bin/env python3
"""Bind retained DWD ICON Global/EPS captures to a native arrival index.

All inputs are existing, immutable local evidence.  This command is offline:
it does not capture DWD data, write SQLite, activate P0, or submit orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.icon_eps_native_arrival_index import (  # noqa: E402
    bind_icon_eps_daily_archive_arrival_index,
    build_icon_eps_native_arrival_index,
    build_icon_eps_native_arrival_index_repair_receipt,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)


DEFAULT_COHORT_DIR = ROOT / "output/audits/icon_global_eps_daily_20260802/cohorts"
DEFAULT_CAPTURE_DIR = ROOT / "output/audits/icon_global_eps_daily_20260802/captures"
DEFAULT_RAW_ROOT = ROOT / "data/research/bayes_basket_target/model_arrivals/icon_global_eps_official/raw"
DEFAULT_ARCHIVE = ROOT / "output/audits/icon_global_eps_daily_members_20260802.json"
DEFAULT_INDEX_OUTPUT = ROOT / "output/audits/icon_global_eps_native_arrival_index_20260801.json"
DEFAULT_BOUND_ARCHIVE_OUTPUT = ROOT / "output/audits/icon_global_eps_daily_members_20260802_arrival_indexed.json"
MAX_INPUTS = 26


def _load(path: Path) -> Mapping[str, Any]:
    value = load_bounded_json(path.expanduser().resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise ContractError("icon_eps_arrival_index_cli_input_mapping_required")
    return value


def _files(directory: Path, *, prefix: str) -> list[Path]:
    resolved = directory.expanduser().resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise ContractError("icon_eps_arrival_index_cli_directory_invalid")
    paths = sorted(resolved.glob(prefix + "*.json"))
    if not paths or len(paths) > MAX_INPUTS or any(path.is_symlink() for path in paths):
        raise ContractError("icon_eps_arrival_index_cli_input_count_invalid")
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort-dir", type=Path, default=DEFAULT_COHORT_DIR)
    parser.add_argument("--capture-dir", type=Path, default=DEFAULT_CAPTURE_DIR)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--daily-archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--index-output", type=Path, default=DEFAULT_INDEX_OUTPUT)
    parser.add_argument("--bound-archive-output", type=Path, default=DEFAULT_BOUND_ARCHIVE_OUTPUT)
    parser.add_argument(
        "--indexed-at-utc",
        default=datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    )
    args = parser.parse_args()
    cohorts = [_load(path) for path in _files(args.cohort_dir, prefix="cohort_")]
    captures = [_load(path) for path in _files(args.capture_dir, prefix="capture_")]
    repair = build_icon_eps_native_arrival_index_repair_receipt(
        cohorts,
        captures,
        inspected_at_utc=args.indexed_at_utc,
        raw_root=args.raw_root,
    )
    index_path = args.index_output.expanduser().resolve()
    if repair["state"] == "ARRIVAL_INDEX_READY":
        index = build_icon_eps_native_arrival_index(
            cohorts,
            captures,
            indexed_at_utc=args.indexed_at_utc,
            raw_root=args.raw_root,
        )
        archive = _load(args.daily_archive)
        bound = bind_icon_eps_daily_archive_arrival_index(archive, index)
        index_wrote = write_bounded_json_if_changed(index_path, index, limit=MAX_JSON_INPUT_BYTES)
        archive_path: Path | None = args.bound_archive_output.expanduser().resolve()
        archive_wrote = write_bounded_json_if_changed(archive_path, bound, limit=MAX_JSON_INPUT_BYTES)
        index_payload: Mapping[str, Any] = index
        bound_sha: str | None = bound["content_sha256"]
    else:
        # A repair receipt is not an arrival index and deliberately cannot
        # produce a bound archive.  In particular, do not substitute a newer
        # receipt merely because the raw payload hash happens to match.
        index_wrote = write_bounded_json_if_changed(index_path, repair, limit=MAX_JSON_INPUT_BYTES)
        archive_path = None
        archive_wrote = False
        index_payload = repair
        bound_sha = None
    print(json.dumps({
        "index_output": str(index_path),
        "index_wrote": index_wrote,
        "index_schema": index_payload["schema"],
        "index_state": index_payload["state"],
        "index_content_sha256": index_payload["content_sha256"],
        "entry_count": index_payload.get("entry_count", index_payload.get("verified_object_count", 0)),
        "unresolved_receipt_count": index_payload.get("unresolved_receipt_count", 0),
        "bound_archive_output": str(archive_path) if archive_path is not None else None,
        "bound_archive_wrote": archive_wrote,
        "bound_archive_content_sha256": bound_sha,
        "native_arrival_index_eligible": index_payload.get("native_arrival_index_eligible", False),
        "formal_p0_eligible": False,
        "downloads_raw": False,
        "writes_production_database": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
