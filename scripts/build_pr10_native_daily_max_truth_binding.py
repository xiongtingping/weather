#!/usr/bin/env python3
"""Build exact native daily-max→AWC truth bindings or terminals."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.native_daily_max_truth_binding import (  # noqa: E402
    build_native_daily_max_truth_binding_bundle,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    load_bounded_json, write_bounded_json_if_changed,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--daily-max-basis", type=Path, required=True)
    parser.add_argument("--authority-projection", type=Path, required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    basis = load_bounded_json(args.daily_max_basis.expanduser().resolve(strict=True))
    projection = load_bounded_json(
        args.authority_projection.expanduser().resolve(strict=True)
    )
    manifests = basis.get("manifests")
    if not isinstance(manifests, list):
        raise RuntimeError("native_daily_max_basis_manifests_missing")
    database = args.database.expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        bundle = build_native_daily_max_truth_binding_bundle(
            conn, manifests=manifests, as_of_utc=args.as_of_utc,
            authority_scope_projection=projection,
        )
    finally:
        conn.close()
    write_bounded_json_if_changed(
        args.output.expanduser().resolve(), bundle,
        limit=64 * 1024 * 1024, max_nodes=1_000_000,
    )
    print(json.dumps({
        key: bundle[key]
        for key in (
            "state", "scope_count", "binding_count", "terminal_count",
            "blocker_counts", "content_sha256",
        )
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
