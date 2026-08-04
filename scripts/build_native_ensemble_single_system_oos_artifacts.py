#!/usr/bin/env python3
"""Build read-only GEFS/GEPS single-system strict historical OOS evidence."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.native_ensemble_single_system_oos_artifact import (  # noqa: E402
    build_single_system_oos_artifacts,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    write_bounded_json_if_changed,
)


DEFAULT_DB = ROOT / "data/bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = ROOT / "output/audits/native_ensemble_single_system_strict_oos_20260804.json"
DEFAULT_COMPILED_AT = "2026-08-04T00:00:00Z"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--compiled-at-utc", default=DEFAULT_COMPILED_AT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db = args.db.expanduser().resolve(strict=True)
    if db.is_symlink():
        raise ContractError("native_system_artifact_db_symlink_forbidden")
    with sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        manifest = build_single_system_oos_artifacts(
            conn,
            compiled_at_utc=args.compiled_at_utc,
        )
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, manifest)
    print(__import__("json").dumps({
        "output": str(output),
        "wrote": wrote,
        "artifact_count": manifest["artifact_count"],
        "blocked_scope_count": manifest["blocked_scope_count"],
        "blocked_scope_count_by_system": manifest[
            "blocked_scope_count_by_system"
        ],
        "content_sha256": manifest["content_sha256"],
        "writes_database": False,
        "cross_selection_count": 0,
        "cross_activation_count": 0,
        "p0_count": 0,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
