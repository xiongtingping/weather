#!/usr/bin/env python3
"""Build a read-only strict-OOS readiness report for official ICON Global/EPS.

The command consumes retained ``icon_eps_training_facts`` JSON only.  It does
not capture DWD data, read/write SQLite, use Open-Meteo as a fallback, create a
promotion receipt, or activate P0.  Multiple historical batch files may be
passed with repeated ``--batch`` arguments; when omitted, the newest bounded
batch glob is used.
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
from bayes_basket_target.icon_eps_strict_oos import (  # noqa: E402
    build_icon_eps_strict_oos_report,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)


DEFAULT_BATCH_GLOB = ROOT / "output/audits/icon_global_eps_training_facts_*.json"
DEFAULT_OUTPUT = ROOT / "output/audits/icon_global_eps_strict_oos_20260802.json"


def _as_of(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("icon_eps_oos_cli_as_of_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("icon_eps_oos_cli_as_of_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _load(path: Path) -> Mapping[str, Any]:
    value = load_bounded_json(path.expanduser().resolve(strict=True), limit=MAX_JSON_INPUT_BYTES)
    if not isinstance(value, Mapping):
        raise ContractError("icon_eps_oos_cli_batch_mapping_required")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", action="append", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--as-of-utc",
        default=datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    )
    args = parser.parse_args()
    paths = [path.expanduser().resolve() for path in (args.batch or [])]
    if not paths:
        paths = sorted(DEFAULT_BATCH_GLOB.parent.glob(DEFAULT_BATCH_GLOB.name))
    if not paths:
        raise ContractError("icon_eps_oos_cli_training_batch_missing")
    batches = [_load(path) for path in paths]
    report = build_icon_eps_strict_oos_report(
        batches,
        as_of_utc=_as_of(args.as_of_utc),
    )
    output = args.output.expanduser().resolve()
    wrote = write_bounded_json_if_changed(output, report, limit=MAX_JSON_INPUT_BYTES)
    print(json.dumps({
        "schema": report["schema"],
        "state": report["state"],
        "fact_count": report["fact_count"],
        "readiness_scope_count": len(report["readiness"]),
        "candidate_count": report["candidate_count"],
        "formal_p0_eligible": False,
        "promotion_authority_generated": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "output": str(output),
        "wrote": wrote,
        "content_sha256": report["content_sha256"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
